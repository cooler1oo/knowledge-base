"""Meili 薄客户端的协议边界：三类异常各自由谁触发。

**这一组不联网**：`FakeTransport` 记录 `(method, path, body)` 并按剧本返回
`(status, text)`，与 `kb/ai/transport.py` 的假传输同一个手法——不做 monkeypatch，
因为要验的恰好是「客户端在拿到某个状态码时抛哪个异常」，而 monkeypatch
会把这条路径换掉，用例变成自证。

## 五条判据（设计 §9 T02 第一行）

| 输入 | 期望异常 | 为什么不是别的 |
|---|---|---|
| 连接失败 / 超时 | `MeiliUnavailable` | 重试可能成功 |
| 500 | `MeiliUnavailable` | 同上 |
| 401 | `MeiliUnauthorized` | 密钥填错，重试不成功但**也不该判死** |
| 400 | `MeiliRejected` | 请求写错了，重试照样错 |
| 200 + 坏 JSON | `MeiliUnavailable` | 对方不是我们要的那个服务 |
"""

from __future__ import annotations

import json

import httpx
import pytest

from kb.search.client import MeiliClient, _preview, client_from_env
from kb.search.config import SEARCH_TIMEOUT_SEC
from kb.search.models import MeiliRejected, MeiliUnauthorized, MeiliUnavailable


class FakeTransport:
    """按剧本返回 `(status, body_text)`；剧本项是异常时抛它。

    `seen` 记下每次调用的 `(method, url, headers, body)`，断言请求形状用。
    """

    def __init__(self, script: list[object] | None = None) -> None:
        self.script: list[object] = list(script or [])
        self.seen: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, str]:
        self.seen.append((method, url, dict(headers), body))
        if not self.script:
            raise AssertionError("剧本用光了，说明被调用的次数超出预期")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, text = item  # type: ignore[misc]
        return int(status), str(text)

    def json_body(self, index: int = 0) -> object:
        raw = self.seen[index][3]
        assert raw is not None, f"第 {index} 次请求没有请求体"
        return json.loads(raw.decode("utf-8"))


def _client(script: list[object] | None = None) -> tuple[MeiliClient, FakeTransport]:
    transport = FakeTransport(script)
    return MeiliClient("http://127.0.0.1:7700", "k", transport=transport), transport


# ── 三类异常的分界 ─────────────────────────────────────────────


def test_连接失败判不可用():
    """连不上 = 服务没跑。重试可能成功，所以是 `MeiliUnavailable`。"""
    client, _ = _client([httpx.ConnectError("拒绝连接")])
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.search("kb_items", {"q": "x"})
    assert "连不上" in str(excinfo.value)


def test_连接超时判不可用():
    client, _ = _client([httpx.ConnectTimeout("建连超时")])
    with pytest.raises(MeiliUnavailable):
        client.search("kb_items", {"q": "x"})


def test_读超时判不可用且消息带上限():
    """超时消息里要有**上限秒数**：排障时要能判断「是服务慢还是网络不通」。"""
    client, _ = _client([httpx.ReadTimeout("读超时")])
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.search("kb_items", {"q": "x"})
    assert str(SEARCH_TIMEOUT_SEC) in str(excinfo.value)


def test_500判不可用():
    client, _ = _client([(500, '{"message":"internal"}')])
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.search("kb_items", {"q": "x"})
    assert "500" in str(excinfo.value)


def test_401判未授权():
    """401/403 是密钥问题。判 `MeiliUnauthorized` 才能让索引步 defer 600 秒
    而不是 `mark_failed`——密钥填错是可修的配置，判死要手工改库才能恢复。"""
    client, _ = _client([(401, '{"message":"invalid api key"}')])
    with pytest.raises(MeiliUnauthorized) as excinfo:
        client.search("kb_items", {"q": "x"})
    assert "KB_MEILI_KEY" in str(excinfo.value), "错误消息里要写出该检查哪个环境变量"


def test_403判未授权():
    client, _ = _client([(403, "forbidden")])
    with pytest.raises(MeiliUnauthorized):
        client.search("kb_items", {"q": "x"})


def test_400判被拒():
    """400 = 请求写错了（filter 语法错之类）。重试照样错，不该 defer。"""
    client, _ = _client([(400, '{"message":"invalid filter"}')])
    with pytest.raises(MeiliRejected):
        client.search("kb_items", {"q": "x"})


def test_422判被拒():
    client, _ = _client([(422, '{"message":"bad payload"}')])
    with pytest.raises(MeiliRejected):
        client.search("kb_items", {"q": "x"})


def test_坏JSON判不可用():
    """200 但响应体不是 JSON：对方不是 Meili（可能是代理塞了个 HTML 首页）。"""
    client, _ = _client([(200, "<html>not meili</html>")])
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.search("kb_items", {"q": "x"})
    assert "JSON" in str(excinfo.value)


def test_响应是JSON数组而不是对象也判不可用():
    """`json.loads("[1,2]")` 成功，但那不是 search 的响应形状。"""
    client, _ = _client([(200, "[1,2,3]")])
    with pytest.raises(MeiliUnavailable):
        client.search("kb_items", {"q": "x"})


def test_缺hits字段判不可用():
    """200 + 合法 JSON 对象但没有 `hits`：契约不成立，判挂而不是返回空列表。

    返回空列表的后果是「Meili 配置坏了」被伪装成「库里没有这条」——
    用户会反复重试同一个词，越试越确信内容丢了（设计 §6.3 的同一个理由）。
    """
    client, _ = _client([(200, '{"estimatedTotalHits": 0}')])
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.search("kb_items", {"q": "x"})
    assert "hits" in str(excinfo.value)


def test_429判不可用而不是被拒():
    """429 是限流，会自己好。判 Rejected 会让索引步 `mark_failed`，
    把一次临时的限流变成一次永久放弃。"""
    client, _ = _client([(429, "too many requests")])
    with pytest.raises(MeiliUnavailable):
        client.search("kb_items", {"q": "x"})


def test_子类关系成立():
    """三者都继承 `MeiliDown`，调用方可以按子类优先 `except`。"""
    from kb.search.models import MeiliDown

    for cls in (MeiliUnavailable, MeiliUnauthorized, MeiliRejected):
        assert issubclass(cls, MeiliDown)


# ── 请求形状 ───────────────────────────────────────────────────


def test_search_打的是索引的search端点且带POST():
    client, transport = _client([(200, '{"hits": []}')])
    client.search("kb_items", {"q": "三体", "limit": 20})

    method, url, headers, _ = transport.seen[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:7700/indexes/kb_items/search"
    assert headers["Content-Type"] == "application/json"


def test_有密钥时带Bearer头():
    client, transport = _client([(200, '{"hits": []}')])
    client.search("kb_items", {"q": "x"})
    assert transport.seen[0][2]["Authorization"] == "Bearer k"


def test_无密钥时不带Authorization头():
    """本机开发时的 Meili 没有 master key，此时带一个空的 `Bearer ` 会被判 401。"""
    transport = FakeTransport([(200, '{"hits": []}')])
    client = MeiliClient("http://127.0.0.1:7700", "", transport=transport)
    client.search("kb_items", {"q": "x"})
    assert "Authorization" not in transport.seen[0][2]


def test_base_url末尾斜杠被去掉():
    """`http://host:7700/` 与 `http://host:7700` 必须产生同一个 URL，
    否则会出现 `//indexes` 这种被 Meili 判 404 的路径。"""
    transport = FakeTransport([(200, '{"hits": []}')])
    client = MeiliClient("http://127.0.0.1:7700/", "", transport=transport)
    client.search("kb_items", {"q": "x"})
    assert transport.seen[0][1] == "http://127.0.0.1:7700/indexes/kb_items/search"


def test_add_documents的请求体是裸数组() -> None:
    """⚠️ **这条用例第一版写错了，和实现一起错。**

    Meili 1.58.0 的 `POST /indexes/{uid}/documents` 只接受**裸数组**
    `[{"id": 1, ...}]`。第一版按官方 SDK 的 `add_documents(docs)` 直觉
    包成 `{"documents": [...]}`，假传输也照这个假设写剧本，本机全绿。

    真机上包一层对象时服务端返回 **202 + 一个失败任务**：

    ```
    {"status": "failed",
     "error": {"code": "missing_document_id",
               "message": "Document doesn't have a `id` attribute: ..."}}
    ```

    HTTP 202（成功）、`taskUid` 有、`upsert` 返回条数、`mark_done` 照写——
    只有到 Meili 任务列表里才看得到 `failed`，而现象是「搜索永远 0 条命中」。
    """
    client, transport = _client([(202, '{"taskUid": 7}')])
    client.add_documents("kb_items", [{"id": 1, "title": "t"}])

    body = transport.json_body()
    assert isinstance(body, list), f"请求体必须是裸数组，实际是 {type(body).__name__}"
    assert body == [{"id": 1, "title": "t"}]


def test_add_documents返回taskUid():
    client, _ = _client([(202, '{"taskUid": 42}')])
    assert client.add_documents("kb_items", [{"id": 1}]) == 42


def test_add_documents空列表时零请求():
    client, transport = _client([])
    assert client.add_documents("kb_items", []) == 0
    assert transport.seen == []


def test_delete_documents的请求体是裸id数组():
    """`delete-batch` 端点收的是 `[1,2,3]`，不是 `{"ids": [...]}`。"""
    client, transport = _client([(202, '{"taskUid": 3}')])
    client.delete_documents("kb_items", [1, 2, 3])
    assert transport.json_body() == [1, 2, 3]


def test_delete_documents空列表时零请求():
    client, transport = _client([])
    assert client.delete_documents("kb_items", []) == 0
    assert transport.seen == []


def test_patch_settings用PATCH():
    client, transport = _client([(202, '{"taskUid": 5}')])
    client.patch_settings("kb_items", {"searchableAttributes": ["title"]})
    assert transport.seen[0][0] == "PATCH"
    assert transport.seen[0][1].endswith("/indexes/kb_items/settings")


def test_create_index带primaryKey():
    """`primaryKey` 必须是 `id`。写错的后果是 upsert 变成「每次新增一条重复文档」，
    而界面上完全看不出异常——搜索会出现同一内容的多张卡片。"""
    client, transport = _client([(202, '{"taskUid": 1}')])
    client.create_index("kb_items")
    body = transport.json_body()
    assert body == {"uid": "kb_items", "primaryKey": "id"}


def test_index_exists对404返回False():
    """404 在 `index_exists` 里有确定含义：索引还没建。

    这条是 `Indexer.ensure_index` 的前提——不先查就 PATCH，不存在的索引会
    返回 404 → `MeiliRejected` → 每条内容都 `mark_failed`。
    """
    client, _ = _client([(404, '{"message":"index not found"}')])
    assert client.index_exists("kb_items") is False


def test_index_exists对200返回True():
    client, _ = _client([(200, '{"uid":"kb_items"}')])
    assert client.index_exists("kb_items") is True


def test_index_exists对400照样抛出():
    """只有 404 是「不存在」。400 是我们写错了 URL，不该被吞成 False。"""
    client, _ = _client([(400, "bad")])
    with pytest.raises(MeiliRejected):
        client.index_exists("kb_items")


def test_index_document_ids分页拉全量() -> None:
    """真机实测（Meili 1.58.0）：`GET /documents` 返回**对象**
    `{"results": [...], "offset":0, "limit":1000, "total":3}`，不是裸数组。

    ⚠️ **这条用例第一版写错了，而且和实现一起错。**

    第一版把剧本写成 `json.dumps([{"id": i} ...])`（裸数组），实现也按数组解，
    于是本机全绿。到平板跑 `python -m kb.worker.rebuild` 立刻抛
    `MeiliUnavailable: 检索服务响应不是 JSON 数组`——真机返回的是对象。
    教训：**假传输的剧本是照假设写的，假设错了两边一起错**，只有真机能抓。

    现在剧本用真机实测的形状；另有一条用例保留裸数组的兼容。
    """
    first = json.dumps(
        {"results": [{"id": i} for i in range(1000)], "offset": 0, "limit": 1000}
    )
    client, transport = _client([(200, first), (200, '{"results": [], "total": 0}')])
    ids = client.index_document_ids("kb_items")

    assert len(ids) == 1000
    assert ids[0] == 0 and ids[-1] == 999
    assert len(transport.seen) == 2
    assert "offset=1000" in transport.seen[1][1]


def test_index_document_ids兼容裸数组响应() -> None:
    """早期 1.x / 某些兼容层返回裸数组。两种都认的成本是 3 行，
    只认一种时误判成 `MeiliUnavailable` 会把「读错字段」掩盖成「服务挂了」。"""
    client, _ = _client([(200, json.dumps([{"id": 1}, {"id": 2}]))])
    assert client.index_document_ids("kb_items") == [1, 2]


def test_index_document_ids两种形状都不对时判挂() -> None:
    """既不是数组也不含 `results` 数组：这才是真的响应格式错。"""
    client, _ = _client([(200, '{"hits": []}')])
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.index_document_ids("kb_items")
    assert "results" in str(excinfo.value)


def test_index_document_ids不满一页就停() -> None:
    client, transport = _client(
        [(200, json.dumps({"results": [{"id": 1}, {"id": 2}], "total": 2}))]
    )
    assert client.index_document_ids("kb_items") == [1, 2]
    assert len(transport.seen) == 1


# ── 任务等待 ───────────────────────────────────────────────────


def test_wait_task轮询到succeeded():
    client, transport = _client(
        [
            (200, '{"status":"enqueued"}'),
            (200, '{"status":"processing"}'),
            (200, '{"status":"succeeded"}'),
        ]
    )
    client.wait_task(9)
    assert len(transport.seen) == 3
    assert transport.seen[0][1].endswith("/tasks/9")


def test_wait_task失败时抛被拒():
    """任务能提交却被服务端判失败，原因几乎总是设置/请求体写错。
    判成 `Unavailable` 会让索引步无限 defer，而真错留在 Meili 任务列表里没人看。"""
    client, _ = _client(
        [(200, '{"status":"failed","error":{"message":"bad settings"}}')]
    )
    with pytest.raises(MeiliRejected) as excinfo:
        client.wait_task(9)
    assert "bad settings" in str(excinfo.value)


def test_wait_task超时抛不可用(monkeypatch):
    """任务一直不完成时不能永久阻塞 worker。

    ## 时钟怎么给

    `wait_task` 的一轮里有三次 `monotonic()` 调用：`_raw` 的计时起点、计时终点、
    以及 `wait_task` 自己的 deadline 判断。每次 `+0.1` 秒时：

    - 单次请求耗时 0.1s < `SEARCH_TIMEOUT_SEC`(3.0) → 不会先撞「总耗时超限」
    - 每轮 +0.3s，`timeout_sec=1.0` → 第 4 轮左右撞 deadline

    直接让时钟跳到 100 秒会先触发「总耗时超限」那条判据——它是另一条真实存在
    的守卫，用它来验 deadline 会验错东西。
    """
    from kb.search import client as client_module

    monkeypatch.setattr(client_module, "TASK_POLL_INTERVAL_SEC", 0.0)
    now = [0.0]

    def _tick() -> float:
        now[0] += 0.1
        return now[0]

    monkeypatch.setattr(client_module.time, "monotonic", _tick)

    transport = FakeTransport([(200, '{"status":"enqueued"}')] * 20)
    client = MeiliClient("http://127.0.0.1:7700", "", transport=transport)
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.wait_task(9, timeout_sec=1.0)
    assert "仍未完成" in str(excinfo.value)


def test_wait_task的uid为0时零请求():
    """`add_documents` 在空列表时返回 0。调用方无脑 `wait_task(0)` 不该发请求。"""
    client, transport = _client([])
    client.wait_task(0)
    assert transport.seen == []


def test_写操作缺taskUid时判不可用():
    """缺 `taskUid` 说明对方不是 Meili 1.x 的写端点。
    拿着 0 去 `wait_task` 会静默「等到了」——设置没生效却以为生效了。"""
    client, _ = _client([(202, '{"ok":true}')])
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.patch_settings("kb_items", {})
    assert "taskUid" in str(excinfo.value)


# ── 总时长判据 ─────────────────────────────────────────────────


def test_总耗时超过上限时判不可用(monkeypatch):
    """httpx 的 `Timeout` 只覆盖 connect / read / write / pool 四段，
    没有 `total` 字段（实测 0.28.1）。慢速滴流下总时长没有上限，
    所以客户端自己留一个事后判据。"""
    from kb.search import client as client_module

    ticks = iter([0.0, 999.0])
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))

    transport = FakeTransport([(200, '{"hits": []}')])
    client = MeiliClient("http://127.0.0.1:7700", "", transport=transport)
    with pytest.raises(MeiliUnavailable) as excinfo:
        client.search("kb_items", {"q": "x"})
    assert "总耗时" in str(excinfo.value)


# ── 环境变量装配 ───────────────────────────────────────────────


def test_client_from_env读KB_MEILI变量(monkeypatch):
    monkeypatch.setenv("KB_MEILI_URL", "http://example.invalid:9999")
    monkeypatch.setenv("KB_MEILI_KEY", "secret")
    client = client_from_env(transport=FakeTransport())
    assert client.base_url == "http://example.invalid:9999"
    assert client.api_key == "secret"


def test_client_from_env的默认URL与api层一致(monkeypatch):
    """`kb/api/app.py` 的 `AppConfig.from_env` 与 worker 必须读同一套默认值。
    两边默认值不同 → api 连 A、worker 连 B，两边都健康但索引写进去搜不到。"""
    monkeypatch.delenv("KB_MEILI_URL", raising=False)
    monkeypatch.delenv("KB_MEILI_KEY", raising=False)
    client = client_from_env(transport=FakeTransport())
    assert client.base_url == "http://127.0.0.1:7700"
    assert client.api_key == ""


def test_preview截断到200字并加省略号():
    """错误消息里带响应原文，但完整响应体可能上千字（一个 HTML 网关页）。"""
    assert _preview("x" * 300).endswith("…")
    assert len(_preview("x" * 300)) == 201
    assert _preview("短") == "短"


def test_preview对非字符串做JSON序列化():
    assert _preview({"a": 1}) == '{"a": 1}'
