"""Meilisearch 的薄客户端：**本包唯一发 HTTP 的地方**。

## 为什么自己写而不装官方 SDK

官方 `meilisearch` 0.43.0 的 `requires_dist` 是
`["requests", "camel-converter[pydantic]"]`——为 5 个端点拖进 `requests`
与 `pydantic` 两条依赖链。更要紧的是 `requests` 默认读 `HTTP_PROXY`，
会把 `127.0.0.1:7700` 的请求塞进代理；`kb/ai/transport.py` 已因同类问题
写下 `trust_env=False` 的教训（B21）。自写薄客户端复用项目已有的 `httpx`，
零新增依赖，超时口径与 AI 层一致（设计 §1.2）。

## `trust_env=False` 写死在客户端里，不做参数

平板与 Windows 都可能配了系统代理。`127.0.0.1` 被代理转发会表现为
「Meili 挂了」，而 `/health` 探针（`urllib` 也读代理环境）可能同时报挂——
两个信号一起错，排查方向直接走偏。做成参数意味着「记得传对」这件事
要靠人，而它一旦记错的现象是「搜索不可用」。

## `transport` 是注入点，不是 monkeypatch

默认 `None` = 真发请求。测试注入一个只记录 `(method, path, body)` 并返回
剧本的假实现——与 `kb/ai/transport.py` 的 `client_factory` 同一个手法。
"""

from __future__ import annotations

import json
import time
from typing import Any, Sequence

import httpx

from .config import SEARCH_TIMEOUT_SEC, TASK_WAIT_SEC
from .models import MeiliRejected, MeiliUnauthorized, MeiliUnavailable

#: 一次写操作返回的 `taskUid` 在响应体里的键名。Meili 1.58 用它，
#: 中央写在一处：改版时只改这里，而不是在每个调用点找 `["taskUid"]`。
TASK_UID_KEY = "taskUid"

#: 等任务时的轮询间隔。0.15 秒的依据：Meili 的任务队列在平板上的
#: 单次设置变更实测 20–80ms 落地，0.15 秒轮询意味着最多多等 150ms，
#: 而更密的轮询只是把 CPU 花在无意义的往返上。
TASK_POLL_INTERVAL_SEC = 0.15


class HttpTransport:
    """真实实现：发一次请求，返回 `(status, 响应体文本)`。

    **非 2xx 也返回而不抛**：状态码到异常的映射是 `MeiliClient` 的职责，
    因为只有它知道「搜索路径上 401 算 `MeiliUnavailable`，而索引路径上算
    `MeiliUnauthorized`」（设计 §6.1 与 §3.4 的差异）。
    """

    def __init__(self, *, client_factory: Any = None) -> None:
        self._client_factory: Any = client_factory or httpx.Client

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, str]:
        client = self._client_factory(
            # 见模块 docstring：这一行是「仍然直连」的唯一保证。
            trust_env=False,
            # 显式 Timeout，与 `kb/ai/transport.py` 同口径。读与连接同值：
            # 检索请求的响应体是几百字节的 JSON，不需要区分两段超时。
            timeout=httpx.Timeout(
                connect=SEARCH_TIMEOUT_SEC,
                read=SEARCH_TIMEOUT_SEC,
                write=SEARCH_TIMEOUT_SEC,
                pool=SEARCH_TIMEOUT_SEC,
            ),
            follow_redirects=False,
        )
        try:
            response = client.request(method, url, headers=headers, content=body)
            return int(response.status_code), response.text
        finally:
            client.close()


class MeiliClient:
    """5 个端点的薄封装。**只做协议，不做编排**（编排在 `Indexer` / `SearchService`）。

    ## 写操作返回 `taskUid`，由调用方决定要不要等

    - **写文档不等任务**：Meili 的写是异步队列，等它会把一次 200 条的批量
      变成秒级阻塞。
    - **改设置与建索引要等**：后续的第一次写入依赖设置生效，不等会写进默认设置下
      ——现象是「`filterableAttributes` 明明设了但过滤报 `not filterable`」。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout_sec: float = SEARCH_TIMEOUT_SEC,
        transport: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        #: 总时长的记录点。`HttpTransport` 已经设了 `httpx.Timeout`，
        #: 但那个超时是**每段**的（connect / read 各 3 秒），
        #: 慢速滴流下总时长可能远超 3 秒。这一行只在 `_request` 里做一次
        #: 事后判据，代价为零，收益是「总时长上限」这件事有代码可查。
        self._transport: Any = transport if transport is not None else HttpTransport()

    # ── 公开端点（设计 §3.5 的精确签名）────────────────────────

    def search(self, index_uid: str, payload: dict[str, object]) -> dict[str, object]:
        """搜一次。返回 Meili 的原始响应体。

        **缺 `hits` 就判挂**：一个 200 但没有 `hits` 的响应说明对方不是我们要的
        那个服务（例如反向代理返回了一个 HTML 首页并被当成了 JSON）。
        """
        body = self._request("POST", f"/indexes/{index_uid}/search", payload)
        if "hits" not in body:
            raise MeiliUnavailable(f"检索服务响应缺少 hits 字段：{_preview(body)}")
        return body

    def add_documents(self, index_uid: str, docs: Sequence[dict[str, object]]) -> int:
        """upsert 一批文档，返回 `taskUid`。**不等任务**（见类 docstring）。

        ## ⚠️ 请求体是**裸数组**，不是 `{"documents": [...]}`（真机实测）

        Meili 1.58.0 的 `POST /indexes/{uid}/documents` 只接受
        `[{"id": 1, ...}, ...]`。包一层对象时服务端返回 **202 + 一个失败任务**：

        ```
        {"status": "failed",
         "error": {"code": "missing_document_id",
                   "message": "Document doesn't have a `id` attribute:
                               `{\"documents\":[{\"id\":999,...}]}`"}}
        ```

        **这个错极难发现**：HTTP 是 202（成功），`taskUid` 也拿到了，
        `upsert` 返回条数、`mark_done` 照写——只有到 Meili 的任务列表里
        才看得到 `failed`。而现象是「搜索永远 0 条命中」，看起来像分词或索引
        设置的问题。

        第一版按官方 SDK 的 `add_documents(docs)` 直觉写成 `{"documents": ...}`
        （SDK 会自己包装），假传输照同一个假设写剧本，于是本机全绿
        （`tests/test_search_indexer.py` 里那条「传的是 meili 字典」也是拿包好的
        形状当断言）。修完补了一条「请求体是裸数组」的用例。

        官方 SDK 之所以能包，是因为它**把 list 当 `documents` 参数**再自己摊平；
        直接用 HTTP 时没有那一层。
        """
        if not docs:
            return 0
        body = self._request(
            "POST",
            f"/indexes/{index_uid}/documents",
            [dict(doc) for doc in docs],
        )
        return _task_uid(body)

    def delete_documents(self, index_uid: str, ids: Sequence[int]) -> int:
        """按主键删一批文档，返回 `taskUid`。`ids` 为空时零请求。"""
        if not ids:
            return 0
        body = self._request(
            "POST",
            f"/indexes/{index_uid}/documents/delete-batch",
            [int(i) for i in ids],
        )
        return _task_uid(body)

    def patch_settings(self, index_uid: str, settings: dict[str, object]) -> int:
        """PATCH 索引设置，返回 `taskUid`。调用方**要等**这个任务。"""
        body = self._request("PATCH", f"/indexes/{index_uid}/settings", settings)
        return _task_uid(body)

    def index_exists(self, index_uid: str) -> bool:
        """索引是否存在。`404` 是「不存在」这一条正常答案，不是错误。

        ## 为什么要这个方法而不是「建索引失败就忽略」

        `POST /indexes` 对已存在的 uid 返回 **409**，而 409 落在薄客户端的
        4xx 兜底分支里会变成 `MeiliUnavailable`——那句话的意思是「重试可能成功」，
        实际重试永远一样。先查再建让「已存在」显式成为一个分支，
        而不是把一次正常的幂等操作伪装成一次暂时性故障。
        """
        try:
            self._request("GET", f"/indexes/{index_uid}", None)
            return True
        except MeiliRejected as exc:
            # 薄客户端把 404 判成 Rejected（「请求或资源不对」）。
            # 在这个方法里 404 的含义是确定的：索引还没建。
            if "HTTP 404" in str(exc):
                return False
            raise

    def create_index(self, index_uid: str, primary_key: str = "id") -> int:
        """建索引，返回 `taskUid`。**调用方要等这个任务。**

        `primary_key` 默认 `"id"`：与 `IndexDoc.id`（= `items.id`）对应。
        写错这个键的后果是 upsert 变成「每次新增一条重复文档」，
        而界面上完全看不出异常——搜索会出现同一内容的多张卡片。
        """
        body = self._request(
            "POST", "/indexes", {"uid": index_uid, "primaryKey": primary_key}
        )
        return _task_uid(body)

    def index_document_ids(self, index_uid: str) -> list[int]:
        """取索引里现存全部文档的 id。rebuild 用它算「库里没有的孤儿」。

        用 `/documents?fields=id&limit=...` 而不是 `/stats`：stats 只给条数，
        而我们要的是差集的**两端**。`fields=id` 让每条只有几十字节。

        ## ⚠️ 响应是**对象**不是数组（真机实测，Meili 1.58.0）

        `GET /indexes/{uid}/documents` 返回的是
        `{"results": [...], "offset": 0, "limit": 1000, "total": 3}`
        ——裸数组的形状是**错的**。第一版按数组解，真机跑 rebuild 立刻抛
        `MeiliUnavailable: 检索服务响应不是 JSON 数组`。

        这个 bug **只有真机才看得见**：假传输的剧本是照我的假设写的，
        两边一起错，单测全绿（`tests/test_search_client.py` 里那条
        「分页拉全量」正是拿 JSON 数组当剧本）。修完补了一条「对象形状
        也要能解」的用例，并保留「裸数组照样能解」的兼容——Meili 1.x 的
        早期版本返回过数组，两种都认不会错。

        `total` 不能当「已取完」的判据：写入是异步的，刚灌完立即查可能拿到
        `total: 0` 而 `results` 里实际有数据。所以翻页判据只能是
        「本页条数 < 每页条数」。
        """
        ids: list[int] = []
        offset = 0
        # 与 `config.MAX_TOTAL_HITS` 同一个量级：单用户库远低于 1000，
        # 循环只是为了让「将来超过 1000」不变成一个静默截断。
        page = 1000
        while True:
            path = (
                f"/indexes/{index_uid}/documents?fields=id&limit={page}&offset={offset}"
            )
            rows = self._request_documents_page(path)
            for row in rows:
                if isinstance(row, dict) and "id" in row:
                    ids.append(int(str(row["id"])))
            if len(rows) < page:
                return ids
            offset += page

    def wait_task(self, task_uid: int, *, timeout_sec: float = TASK_WAIT_SEC) -> None:
        """轮询到任务 `succeeded`。失败或超时都抛 `MeiliRejected` / `MeiliUnavailable`。

        为什么任务失败是 `MeiliRejected`：能提交却被服务端判失败的任务，
        原因几乎总是请求体或设置写错了（例如 `filterableAttributes` 里写了
        不存在的字段），重试照样错。判成 `Unavailable` 会让索引步无限 defer，
        而真正的错误只留在 Meili 的任务列表里没人看。
        """
        if task_uid <= 0:
            return
        deadline = time.monotonic() + timeout_sec
        while True:
            body = self._request("GET", f"/tasks/{task_uid}", None)
            status = str(body.get("status", ""))
            if status == "succeeded":
                return
            if status in {"failed", "canceled"}:
                error = body.get("error") or {}
                raise MeiliRejected(f"索引任务 {task_uid} 失败：{_preview(error)}")
            if time.monotonic() >= deadline:
                raise MeiliUnavailable(
                    f"等待索引任务 {task_uid} 超过 {timeout_sec:.0f} 秒仍未完成"
                )
            time.sleep(TASK_POLL_INTERVAL_SEC)

    # ── 内部 ─────────────────────────────────────────────────

    def _request(
        self, method: str, path: str, payload: object | None
    ) -> dict[str, object]:
        """发一次请求并把状态码/响应体收成 dict 或抛出三类异常之一。

        `payload` 是 `object` 而不是 `dict`：`delete-batch` 端点收的是**裸数组**
        （`[1, 2, 3]`），不是对象。把签名收窄成 `dict` 会让那一个端点在类型层
        被拦住——而 Meili 的文档里它确实是数组。
        """
        raw = self._raw(method, path, payload)
        try:
            decoded = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise MeiliUnavailable(
                f"检索服务响应不是合法 JSON（{type(exc).__name__}）：{_preview(raw)}"
            ) from exc
        if not isinstance(decoded, dict):
            raise MeiliUnavailable(f"检索服务响应不是 JSON 对象：{_preview(decoded)}")
        return decoded

    def _request_documents_page(self, path: str) -> list[Any]:
        """GET `/documents` 并取出文档数组。**两种响应形状都认。**

        | 形状 | 来源 |
        |---|---|
        | `{"results": [...], ...}` | Meili 1.58.0 实测（真机） |
        | `[...]` | 早期 1.x / 某些兼容层 |

        为什么两种都认而不是只认实测到的那种：只认一种时，`{"results": []}`
        会被判成「响应格式错」并抛 `MeiliUnavailable`——那句话的意思是
        「服务挂了，重试可能成功」，而实际上服务好好的、只是我读错了字段。
        错判的方向比错判本身更贵：它会把一个 5 行就能修的 bug 掩盖成
        「去查 Meili 为什么挂」。
        """
        raw = self._raw("GET", path, None)
        try:
            decoded = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as exc:
            raise MeiliUnavailable(
                f"检索服务响应不是合法 JSON（{type(exc).__name__}）：{_preview(raw)}"
            ) from exc
        if isinstance(decoded, list):
            return decoded
        if isinstance(decoded, dict) and isinstance(decoded.get("results"), list):
            rows: object = decoded["results"]
            if isinstance(rows, list):
                return rows
        raise MeiliUnavailable(
            f"检索服务响应既不是文档数组也不含 results 字段：{_preview(decoded)}"
        )

    def _raw(self, method: str, path: str, payload: object | None) -> str:
        """发请求并做状态码分类。**这里是三类异常的唯一产生点。**"""
        url = self.base_url + path
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")

        started = time.monotonic()
        try:
            result: tuple[int, str] = self._transport.request(
                method, url, headers, body
            )
            status, text = result
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise MeiliUnavailable(f"检索服务连不上（{url}）：{exc}") from exc
        except httpx.ReadTimeout as exc:
            raise MeiliUnavailable(
                f"检索服务读超时（{url}，上限 {self.timeout_sec:.1f} 秒）：{exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise MeiliUnavailable(
                f"检索服务请求失败（{type(exc).__name__}）：{exc}"
            ) from exc
        except OSError as exc:
            # 假传输之外的连接层错误（DNS 解析失败、socket 被重置）
            raise MeiliUnavailable(f"检索服务不可达（{url}）：{exc}") from exc

        # 总时长判据：httpx 的 Timeout 是「每段」的，慢速滴流下总时长没有上限。
        # 放在这里而不是放进传输层，是因为假传输不走 httpx 的超时，
        # 而这条判据应该在**两种传输**下都成立。
        elapsed = time.monotonic() - started
        if elapsed > self.timeout_sec:
            raise MeiliUnavailable(
                f"检索服务总耗时 {elapsed:.1f} 秒超过上限 {self.timeout_sec:.1f} 秒"
            )

        if status in (401, 403):
            raise MeiliUnauthorized(
                f"检索服务拒绝访问（HTTP {status}）；检查 {_key_env_hint()}"
            )
        if status in (400, 404, 422):
            raise MeiliRejected(
                f"检索服务拒绝了请求（HTTP {status}，{method} {path}）：{_preview(text)}"
            )
        if status >= 500:
            raise MeiliUnavailable(
                f"检索服务内部错误（HTTP {status}）：{_preview(text)}"
            )
        if status >= 400:
            # 其余 4xx（402 / 429 之类）。判 Unavailable 而不是 Rejected：
            # 429 是限流、402 是配额，两者都会自己好，不是「请求写错了」。
            raise MeiliUnavailable(f"检索服务返回 HTTP {status}：{_preview(text)}")
        return text


def _task_uid(body: dict[str, object]) -> int:
    """从响应体取 `taskUid`。缺失时抛 `MeiliUnavailable`。

    缺 `taskUid` 说明对方不是 Meili 1.x 的写端点（或是代理塞进来的假 200），
    调用方拿着 0 去 `wait_task` 会静默「等到了」——设置没生效却以为生效了。
    """
    if TASK_UID_KEY not in body:
        raise MeiliUnavailable(f"检索服务响应缺少 {TASK_UID_KEY}：{_preview(body)}")
    return int(str(body[TASK_UID_KEY]))


def _preview(value: object, limit: int = 200) -> str:
    """错误消息里带一小段原文。**排障时要看到对方说了什么**，
    而完整响应体可能上千字（一个 HTML 网关页），只取前 200 字。"""
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    return text[:limit] + ("…" if len(text) > limit else "")


def _key_env_hint() -> str:
    """错误消息里的「下一步该做什么」。用户非技术背景，
    「MeiliUnauthorized」对他无信息量（设计 §10「错误文案」）。"""
    from .config import MEILI_KEY_ENV

    return f"{MEILI_KEY_ENV} 是否正确"


def client_from_env(*, transport: Any = None) -> MeiliClient:
    """按环境变量造客户端。worker 与 api 都走这一条，读同一套变量名。"""
    from .config import meili_key, meili_url

    return MeiliClient(meili_url(), meili_key(), transport=transport)
