"""端到端：真库 + 假传输，跑真实 worker 循环。

**这一组的特殊性**：它跑的是 `loop.run_once` 本身，不直接调 `steps.*`。
所以「分派顺序」「`mark_done` 与 `enqueue` 的先后」「`ai_client=None` 的四条
分支」这些**结构性问题**在这里才验得到——`tests/test_worker_ai_steps.py`
用假连接能验控制流，验不了「真库上 `jobs_pending_uniq` 是否生效」。

本机无 Postgres 时整组跳过（`@pytest.mark.pg`）。在平板上跑：

```bash
KB_PG_HOST=127.0.0.1 KB_PG_USER=u0_a201 python -m pytest tests/test_ai_e2e_pg.py -q
```

`B27`（干净环境缺 `httpx` 时启动即报错）**不需要真库**，所以它在本文件里
单独一条、不带 pg 标记——那是启动阶段的检查，与数据库无关。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kb.ai.outcomes import SummarizeOk
from kb.store.db import connect
from kb.store.migrate import discover, migrate
from kb.worker import loop

ROOT = Path(__file__).resolve().parent.parent


# ── B27：干净环境缺 httpx（不需要真库）────────────────────────


def test_干净环境缺httpx时启动即报错退出_B27():
    """B27：`import kb.ai` 失败时 worker **启动阶段**就报明确错误并退出。

    验证方式：用一个「拦住 httpx」的 `sitecustomize` 模拟干净环境，
    然后 `python -m kb.worker --idle 0.01`，断言：① 退出码非 0；
    ② stderr 含 `httpx` 与 `pip install httpx`。

    为什么不用真的建一个空 venv：那要下载依赖、耗时几十秒，而且 CI 上可能没网。
    `sitecustomize` 的 `sys.meta_path` 注入能在几百毫秒内造出同样的效果，
    且**验的是同一段代码路径**（`ensure_ai_available` 里的 `import kb.ai`）。
    """
    blocker = ROOT / "_b27_tmp"
    shutil.rmtree(blocker, ignore_errors=True)
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\n"
        "\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'httpx' or name.startswith('httpx.'):\n"
        "            raise ImportError('No module named %r' % name)\n"
        "        return None\n"
        "\n"
        "sys.meta_path.insert(0, _Block())\n",
        encoding="utf-8",
    )
    env = {
        "PYTHONPATH": os.pathsep.join([str(blocker), str(ROOT)]),
        "PYTHONIOENCODING": "utf-8",
        # 让 Pool 构造不因缺环境变量而抛别的异常（我们要的是 httpx 那条）
        "KB_PG_HOST": "127.0.0.1",
        "KB_PG_USER": "nobody",
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "kb.worker", "--idle", "0.01"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    finally:
        shutil.rmtree(blocker, ignore_errors=True)

    assert result.returncode != 0, "缺 httpx 时必须退出，不能「看起来在跑」"
    combined = (result.stderr or "") + (result.stdout or "")
    assert "httpx" in combined, f"错误消息里没提包名：{combined}"
    assert "pip install httpx" in combined, f"没给出修复命令：{combined}"


# ── 真库 fixture ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def fresh_db(pg_cfg):
    """重建 kb_test 并跑一遍迁移。"""
    admin = connect(pg_cfg.with_database("postgres"))
    admin.run(f'drop database if exists "{pg_cfg.database}" with (force)')
    admin.run(f'create database "{pg_cfg.database}"')
    admin.close()

    conn = connect(pg_cfg)
    try:
        migrate(conn, discover())
    finally:
        conn.close()
    return pg_cfg


@pytest.fixture
def conn(pg_cfg, fresh_db):
    """每个用例一个事务，结束回滚。"""
    c = connect(pg_cfg)
    c.run("begin")
    try:
        yield c
    finally:
        c.run("rollback")
        c.close()


class _Pool:
    """把真连接包成 `pool.connection()` 的形状。"""

    def __init__(self, c: object) -> None:
        self._conn = c

    def connection(self) -> object:
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield self._conn

        return _cm()


class _Extractor:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    async def __call__(self, url: str) -> object:
        from kb.extract.normalize import ExtractResult, MediaItem
        from kb.extract.router import ExtractOutcome

        self.calls.append(url)
        if not self.ok:
            return ExtractOutcome(
                ok=False, route="parsehub", kind="link", error="被拦截"
            )
        return ExtractOutcome(
            ok=True,
            route="parsehub",
            kind="article",
            data=ExtractResult(
                kind="article",
                title="抓到的标题",
                content_text="抓到的正文" * 400,
                raw_url=url,
                platform="",
                media=[
                    MediaItem(role="cover", url="https://x.invalid/a.jpg", ext="jpg")
                ],
            ),
        )


class _Client:
    """假 AI 客户端。`called` 计数供「零请求」的断言使用。"""

    def __init__(self, ok: bool = True, tags: tuple[str, ...] = ()) -> None:
        self.calls = 0
        self.ok = ok
        self.tags = tags

    def summarize(
        self, title: str, text: str, vocabulary: tuple[tuple[int, str, str], ...] = ()
    ) -> object:
        self.calls += 1
        if not self.ok:
            from kb.ai.outcomes import FailureCategory, SummarizeFailed

            return SummarizeFailed(
                category=FailureCategory.BAD_RESPONSE,
                message="AI解析失败：响应不是合法 JSON",
                snippet="<半截",
            )
        return SummarizeOk(summary="这是一段足够长的中文摘要内容", tags=self.tags)


def _config(conn) -> None:
    """把三键配置写进 settings（每个用例自己调）。"""
    from kb.store import settings as settings_store

    settings_store.set_value(conn, "ai_base_url", "https://api.example.invalid/v1")
    settings_store.set_value(conn, "ai_api_key", "sk-test")
    settings_store.set_value(conn, "ai_model", "test-model")


def _new_item(
    conn,
    *,
    kind: str = "text",
    url: str | None = None,
    title: str = "标题",
    content_text: str | None = None,
) -> int:
    return int(
        str(
            conn.run(
                "insert into items (kind, url, title, content_text) values"
                " (:k, :u, :t, :c) returning id",
                k=kind,
                u=url,
                t=title,
                c=content_text,
            )[0][0]
        )
    )


def _enqueue(conn, item_id: int, step: str) -> int:
    return int(
        str(
            conn.run(
                "insert into jobs (item_id, step) values (:i, :s) returning id",
                i=item_id,
                s=step,
            )[0][0]
        )
    )


def _ensure_job(conn, item_id: int, step: str) -> int:
    """确保有一条该步骤的 pending 任务，返回它的 id。

    **重试用例必须用它而不是 `_enqueue`。** 原因：

    `mark_failed` 第 1、2 次失败后把 job 标回 `pending`（`attempts+1 < 3`），
    所以「再入队一次」会撞上 `jobs_pending_uniq` 部分唯一索引
    （`on jobs (item_id, step) where state in ('pending','running')`）——
    而 `_enqueue` 是裸 `insert`，没有 `on conflict`，直接抛
    `23505 duplicate key`。

    这不是测试在「绕过」什么：生产里 `fetch` 成功入队 `summarize` 用的是
    `jobs_store.enqueue`，它带 `on conflict do nothing`，重复入队是安全的。
    本辅助函数照那条路径写，把「任务已在待办里」当成正常情况。
    """
    rows = conn.run(
        "select id from jobs where item_id = :i and step = :s"
        " and state in ('pending','running') order by id limit 1",
        i=item_id,
        s=step,
    )
    if rows:
        return int(str(rows[0][0]))
    return _enqueue(conn, item_id, step)


def _tag_id(conn, name: str) -> int:
    """取或建一个标签，返回它的 id。

    `select` 用与唯一索引 `tags_sibling_name_uniq` **同样的归一化**
    （`lower(btrim(name))`），否则库里已有「健康」时，精确匹配漏掉它、
    insert 直接撞 `23505`。

    `0004_seed_ai_tags.sql` 预置了 8 个一级标签（含「健康」），
    所以**任何假设「`tags` 表是空的」写法都会在干净测试库上撞**。
    """
    rows = conn.run(
        "select id from tags where lower(btrim(name)) = lower(btrim(:n))"
        " order by id limit 1",
        n=name,
    )
    if rows:
        return int(str(rows[0][0]))
    return int(
        str(conn.run("insert into tags (name) values (:n) returning id", n=name)[0][0])
    )


def _jobs(conn, item_id: int) -> list[dict[str, object]]:
    rows = conn.run(
        "select step, state, attempts, error from jobs where item_id = :i order by id",
        i=item_id,
    )
    return [
        {
            "step": str(r[0]),
            "state": str(r[1]),
            "attempts": int(r[2]),
            "error": None if r[3] is None else str(r[3]),
        }
        for r in rows
    ]


def _job_state(conn, job_id: int) -> str:
    return str(conn.run("select state from jobs where id = :j", j=job_id)[0][0])


def _summary(conn, item_id: int) -> dict[str, object]:
    row = conn.run(
        "select summary, content_text, kind, props from items where id = :i", i=item_id
    )[0]
    props = row[3]
    return {
        "summary": None if row[0] is None else str(row[0]),
        "content_text": None if row[1] is None else str(row[1]),
        "kind": str(row[2]),
        "props": props if isinstance(props, dict) else {},
    }


# ── C5：端到端出摘要 ─────────────────────────────────────────


@pytest.mark.pg
def test_端到端纯文字条目出摘要且状态done_C5(conn):
    """C5：`POST /api/items` 存一条纯文字 → 30 秒内 `summary` 非空且 `status=done`。

    这里直接走 `run_once`（跳过 HTTP 层），验的是「任务链会自己走完」。
    """
    _config(conn)
    item_id = _new_item(conn, title="纯文字标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client()

    # 跑两轮：第一轮 summarize → 入队 tag；第二轮 tag
    loop.run_once(_Pool(conn), _Extractor(), client)
    loop.run_once(_Pool(conn), _Extractor(), client)

    item = _summary(conn, item_id)
    assert item["summary"], "端到端没有产出摘要"
    assert client.calls == 1

    jobs = _jobs(conn, item_id)
    states = {job["step"]: job["state"] for job in jobs}
    assert states.get("summarize") == "done"
    assert states.get("tag") == "done"


@pytest.mark.pg
def test_summarize与tag均为done且attempts为0_A3(conn):
    """A3：两个步骤都 `done` 且 `attempts=0`（一次就成功，没有重试痕迹）。"""
    _config(conn)
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")

    loop.run_once(_Pool(conn), _Extractor(), _Client())
    loop.run_once(_Pool(conn), _Extractor(), _Client())

    for job in _jobs(conn, item_id):
        assert job["attempts"] == 0, f"{job['step']} 有重试痕迹：{job}"


@pytest.mark.pg
def test_两个合法标签写入item_tags且confidence为null_A2(conn):
    """A2：模型返回 2 个合法标签 → `item_tags` 的 `origin='ai'` 行数为 2，
    `confidence` 全为 NULL。
    """
    _config(conn)
    # 0004 已预置「健康」，直接 insert 会撞 `tags_sibling_name_uniq`。
    # 这两个标签用「取或建」，用例不依赖种子的具体内容。
    tag_a = _tag_id(conn, "健康")
    tag_b = _tag_id(conn, "穿搭")
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")

    client = _Client(tags=("健康", "穿搭"))
    loop.run_once(_Pool(conn), _Extractor(), client)
    loop.run_once(_Pool(conn), _Extractor(), client)

    rows = conn.run(
        "select tag_id, origin, confidence from item_tags where item_id = :i"
        " order by tag_id",
        i=item_id,
    )
    assert len(rows) == 2, f"应写 2 行，实际 {len(rows)}"
    assert {int(str(r[0])) for r in rows} == {tag_a, tag_b}
    assert all(str(r[1]) == "ai" for r in rows)
    assert all(r[2] is None for r in rows), "confidence 必须是 NULL"


# ── C6 / B19 / B20：重跑 ────────────────────────────────────


@pytest.mark.pg
def test_重跑后user标签保留ai标签整体替换_B19(conn):
    """B19：重跑 `summarize`+`tag`，`origin='user'` 那行仍在，`origin='ai'` 整体替换。"""
    _config(conn)
    tag_ai = int(
        str(conn.run("insert into tags (name) values ('AI标签') returning id")[0][0])
    )
    tag_user = int(
        str(
            conn.run("insert into tags (name) values ('我自己加的') returning id")[0][0]
        )
    )
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client(tags=("AI标签",))

    loop.run_once(_Pool(conn), _Extractor(), client)
    loop.run_once(_Pool(conn), _Extractor(), client)
    # 用户手动加一个标签
    conn.run(
        "insert into item_tags (item_id, tag_id, origin) values (:i, :t, 'user')",
        i=item_id,
        t=tag_user,
    )

    # 重跑：再入队 summarize（上一次的已 done，索引允许）
    _enqueue(conn, item_id, "summarize")
    loop.run_once(_Pool(conn), _Extractor(), client)
    loop.run_once(_Pool(conn), _Extractor(), client)

    rows = conn.run(
        "select tag_id, origin from item_tags where item_id = :i order by tag_id",
        i=item_id,
    )
    pairs = {(int(str(r[0])), str(r[1])) for r in rows}
    assert (tag_user, "user") in pairs, "用户自己加的标签被删了"
    assert (tag_ai, "ai") in pairs, "AI 标签没被重新写入"
    assert len(rows) == 2, f"AI 标签累加了：{pairs}"


@pytest.mark.pg
def test_重复入库同一条目不抛唯一约束冲突_B20(conn):
    """**B20 的真正守门用例：走 `items.create_item` 这条路径。**

    ⚠️ **这是一个真 bug 在平板真库上的复现点。**

    `_CREATE_SQL` 的 CTE 第二段 `insert into jobs` 原本没有 `on conflict`。
    同一条目**重复入库**（用户重复分享同一个链接）时，`jobs_pending_uniq`
    （部分唯一索引，`where state in ('pending','running')`）直接抛 `23505`，
    整个 `create_item` 失败 —— **用户看到「保存失败」而不是「已存在」**。

    上一条 `test_连续两次入队summarize只有一条pending_B20` 只测了
    `jobs.enqueue`（`kb/store/jobs.py`），而 `kb/store/items.py` 是**另一条
    独立的入队路径**（入库与入队在同一条 CTE 里完成，为了原子性）。
    「`enqueue` 写对了」不等于「`create_item` 也写对了」—— 这条 bug 正是
    这么漏过去的。

    本用例的判据是**不抛异常**：重复入库应当正常返回 `duplicate=True`，
    而不是让调用方收到一个 500。
    """
    from kb.store import items as items_store

    url = "https://x.invalid/dup-b20"
    norm = items_store.normalize_url(url)

    first = items_store.create_item(
        conn,
        kind="link",
        url=url,
        url_norm=norm,
        title="标题",
        source_domain="x.invalid",
        content_text=None,
        step="fetch",
    )
    assert first.duplicate is False

    # 让第一条 job 保持 pending（默认就是），制造唯一索引的冲突条件
    second = items_store.create_item(
        conn,
        kind="link",
        url=url,
        url_norm=norm,
        title="标题",
        source_domain="x.invalid",
        content_text=None,
        step="fetch",
    )
    assert second.duplicate is True
    assert second.id == first.id

    # 且没有产生第二条 pending job
    count = conn.run(
        "select count(*) from jobs where item_id = :i and step = 'fetch'"
        " and state in ('pending','running')",
        i=first.id,
    )[0][0]
    assert int(str(count)) == 1


@pytest.mark.pg
def test_首次入库的第二步骤也能入队_B20(conn):
    """B20 的另一面：`on conflict do nothing` 不能把**首次**入队也吃掉。

    加了 `on conflict` 之后最容易犯的错是「条件写宽了，什么都插不进去」。
    这条用一条全新的条目验证：`create_item(step='summarize')` 之后
    jobs 表里确实有那条 summarize。
    """
    from kb.store import items as items_store

    result = items_store.create_item(
        conn,
        kind="text",
        url=None,
        url_norm=None,
        title="纯文字标题",
        source_domain="",
        content_text="字" * 200,
        step="summarize",
    )
    rows = conn.run("select step, state from jobs where item_id = :i", i=result.id)
    assert len(rows) == 1, f"首次入库应当入队一条，实际 {len(rows)}：{rows}"
    assert str(rows[0][0]) == "summarize"
    assert str(rows[0][1]) == "pending"


@pytest.mark.pg
def test_连续两次入队summarize只有一条pending_B20(conn):
    """B20：`jobs_pending_uniq` 生效——同一 item 同一步骤只允许一条未完成任务。"""
    from kb.store import jobs as jobs_store

    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    assert jobs_store.enqueue(conn, item_id, "summarize") is True
    assert jobs_store.enqueue(conn, item_id, "summarize") is False

    count = conn.run(
        "select count(*) from jobs where item_id = :i and step = 'summarize'"
        " and state in ('pending','running')",
        i=item_id,
    )[0][0]
    assert int(str(count)) == 1


@pytest.mark.pg
def test_重跑抓取成功后summarize再执行一次_C6(conn):
    """C6：`fetch` 成功两次 → `summarize` 被重新执行（出现第二条 summarize）。

    第一次 fetch 入队 summarize；那条 summarize 跑完后是 `done`，
    `jobs_pending_uniq` 是部分索引（`where state in ('pending','running')`），
    不再覆盖已完成的 job，所以第二次 fetch 能插一条新的 summarize。

    ⚠️ 初版失败的原因：用 `run_once` 逐轮推进，但 `claim_one` 按 `run_after`
    排序，`tag` 与 `summarize` 谁先被领取不确定。改成**直接把需要的步骤跑完**
    （用 `steps.*` 显式调用），断言对象仍是「jobs 表里的状态」，不涉及调度顺序。

    这样改不降低强度：本用例要验的契约是「已完成的上游可以重新入队下游」，
    与「worker 先领哪条」无关；调度顺序由 `test_worker_loop.py` 单独覆盖。
    """
    from kb.worker import steps

    _config(conn)
    item_id = _new_item(conn, kind="link", url="https://x.invalid/article")
    client = _Client()
    now = conn.run("select now()")[0][0]

    fetch_id = _enqueue(conn, item_id, "fetch")
    steps.run_fetch(conn, item_id, fetch_id, extractor=_Extractor(ok=True))

    # fetch ① 成功后应入队 summarize
    first_summarize = _ensure_job(conn, item_id, "summarize")
    steps.run_summarize(conn, item_id, first_summarize, ai_client=client, now=now)
    assert _job_state(conn, first_summarize) == "done"

    # 重跑抓取：应该能再入队一条 summarize（前一条已 done）
    fetch2 = _enqueue(conn, item_id, "fetch")
    steps.run_fetch(conn, item_id, fetch2, extractor=_Extractor(ok=True))

    rows = conn.run(
        "select id, state from jobs where item_id = :i and step = 'summarize'"
        " order by id",
        i=item_id,
    )
    assert len(rows) == 2, f"重跑后应有两代 summarize job，实际 {len(rows)}：{rows}"
    states = [str(r[1]) for r in rows]
    assert states[0] == "done", f"第一条 summarize 不是 done：{states}"
    assert states[1] in {"pending", "running"}, f"第二条 summarize 未被入队：{states}"


# ── C1 / C3 / C4 / C7 / C8 ─────────────────────────────────


@pytest.mark.pg
def test_fetch成功后jobs出现summarize_C1(conn):
    item_id = _new_item(conn, kind="link", url="https://x.invalid/a")
    _enqueue(conn, item_id, "fetch")

    loop.run_once(_Pool(conn), _Extractor(ok=True), _Client())

    steps = {job["step"] for job in _jobs(conn, item_id)}
    assert "summarize" in steps


@pytest.mark.pg
def test_fetch失败时没有summarize任务_C2(conn):
    item_id = _new_item(conn, kind="link", url="https://x.invalid/a")
    _enqueue(conn, item_id, "fetch")

    loop.run_once(_Pool(conn), _Extractor(ok=False), _Client())

    steps = {job["step"] for job in _jobs(conn, item_id)}
    assert "summarize" not in steps


@pytest.mark.pg
def test_全程不出现ocr与index_C4(conn):
    _config(conn)
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client(tags=("健康",))

    for _ in range(3):
        loop.run_once(_Pool(conn), _Extractor(), client)

    steps = {job["step"] for job in _jobs(conn, item_id)}
    assert "ocr" not in steps
    assert "index" not in steps


@pytest.mark.pg
def test_纯文字条目全流程后kind仍为text且url仍为NULL_C8(conn):
    _config(conn)
    item_id = _new_item(conn, kind="text", title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client()

    loop.run_once(_Pool(conn), _Extractor(), client)

    item = _summary(conn, item_id)
    assert item["kind"] == "text"
    url = conn.run("select url from items where id = :i", i=item_id)[0][0]
    assert url is None


@pytest.mark.pg
def test_链接与纯文字两条同时跑都有摘要_C7(conn):
    """C7：两个断点都修好。只修 AI-07 则纯文字无摘要；只修 AI-11 则链接无摘要。"""
    _config(conn)
    link_id = _new_item(conn, kind="link", url="https://x.invalid/a")
    text_id = _new_item(conn, kind="text", title="纯文字标题", content_text="字" * 200)
    _enqueue(conn, link_id, "fetch")
    _enqueue(conn, text_id, "summarize")
    client = _Client()

    # 跑够轮次让两条链都走完（fetch → summarize，summarize → tag）
    for _ in range(4):
        loop.run_once(_Pool(conn), _Extractor(ok=True), client)

    assert _summary(conn, link_id)["summary"], "链接条目没有摘要（AI-07 没修）"
    assert _summary(conn, text_id)["summary"], "纯文字条目没有摘要（AI-11 没修）"


# ── E1 / E3 / E4 / D3 / D4 ─────────────────────────────────


@pytest.mark.pg
def test_未配置AI时零请求且推迟30分钟attempts不变_E1(conn):
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client()

    loop.run_once(_Pool(conn), _Extractor(), client)

    assert client.calls == 0
    job = _jobs(conn, item_id)[0]
    assert job["state"] == "pending"
    assert job["attempts"] == 0
    assert "已推迟：" in str(job["error"])
    run_after = conn.run("select run_after from jobs where item_id = :i", i=item_id)[0][
        0
    ]
    delta = run_after - datetime.now(timezone.utc)
    assert delta >= timedelta(minutes=29)


@pytest.mark.pg
def test_未配置时5条都pending没有一个failed_E3(conn):
    """E3：未配置时有 5 条待摘要 → 5 条各产生 1 个 `pending`，`attempts` 全为 0，
    **没有任何任务变 failed**。
    """
    item_ids = [
        _new_item(conn, title=f"标题{idx}", content_text="字" * 200) for idx in range(5)
    ]
    for item_id in item_ids:
        _enqueue(conn, item_id, "summarize")
    client = _Client()

    for _ in range(5):
        loop.run_once(_Pool(conn), _Extractor(), client)

    assert client.calls == 0
    failed = conn.run(
        "select count(*) from jobs where item_id = any(:ids) and state = 'failed'",
        ids=item_ids,
    )[0][0]
    assert int(str(failed)) == 0, "未配置不得产生失败任务"
    pending = conn.run(
        "select count(*) from jobs where item_id = any(:ids) and state = 'pending'",
        ids=item_ids,
    )[0][0]
    assert int(str(pending)) == 5


@pytest.mark.pg
def test_补齐配置后任务被领取并成功_E4(conn):
    """E4：之后补上三键配置，`run_after` 到期后任务被领取并成功。

    这里不等 30 分钟：直接把 `run_after` 改成过去，模拟「时间到了」。
    """
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client()

    loop.run_once(_Pool(conn), _Extractor(), client)  # 未配置 → 推迟
    assert client.calls == 0

    _config(conn)
    conn.run("update jobs set run_after = now() where item_id = :i", i=item_id)
    loop.run_once(_Pool(conn), _Extractor(), client)  # 已配置 → 成功

    assert client.calls == 1
    assert _summary(conn, item_id)["summary"]


@pytest.mark.pg
def test_预算超限零请求且推到下月_D3(conn):
    from kb.store import settings as settings_store

    _config(conn)
    settings_store.set_value(conn, "ai_budget_cny", 1)
    settings_store.set_value(conn, "ai_usage_2026-11", {"calls": 1, "cny": 0.999})
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client()

    loop.run_once(_Pool(conn), _Extractor(), client)

    assert client.calls == 0, "预算超限时不得发请求"
    job = _jobs(conn, item_id)[0]
    assert job["state"] == "pending"
    assert job["attempts"] == 0


@pytest.mark.pg
def test_预算充足时正常调用_D6(conn):
    """D6：调用前预算充足 → 照常完成（不中断在途请求）。"""
    from kb.store import settings as settings_store

    _config(conn)
    settings_store.set_value(conn, "ai_budget_cny", 20)
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client()

    loop.run_once(_Pool(conn), _Extractor(), client)
    assert client.calls == 1


# ── B2 / B13 / B22 / B26：失败落地 ────────────────────────


@pytest.mark.pg
def test_解析失败三次后failed且error含响应前200字_B2(conn):
    _config(conn)
    item_id = _new_item(conn, title="标题", content_text="字" * 200)

    for _ in range(3):
        # 用 _ensure_job 而不是 _enqueue：第 1、2 次失败后 job 会退回 pending，
        # 裸 insert 会撞 jobs_pending_uniq（见 _ensure_job 的说明）。
        _ensure_job(conn, item_id, "summarize")
        loop.run_once(_Pool(conn), _Extractor(), _Client(ok=False))
        conn.run("update jobs set run_after = now() where item_id = :i", i=item_id)

    jobs = [j for j in _jobs(conn, item_id) if j["step"] == "summarize"]
    assert any(j["state"] == "failed" for j in jobs), f"三次后没有 failed：{jobs}"
    failed = next(j for j in jobs if j["state"] == "failed")
    assert "半截" in str(failed["error"]), f"error 里没有响应前 200 字：{failed}"
    assert _summary(conn, item_id)["summary"] is None


@pytest.mark.pg
def test_401第一次就failed且error提示检查api_key_B26(conn):
    from kb.ai.outcomes import FailureCategory, SummarizeFailed

    _config(conn)
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")

    class _AuthFail:
        calls = 0

        def summarize(self, title, text, vocabulary=()):  # noqa: ANN001
            _AuthFail.calls += 1
            return SummarizeFailed(
                category=FailureCategory.AUTH,
                message="AI鉴权失败：HTTP 401；检查 api_key 是否有效或余额是否充足",
                http_status=401,
            )

    loop.run_once(_Pool(conn), _Extractor(), _AuthFail())

    job = _jobs(conn, item_id)[0]
    assert job["state"] == "failed", "401 必须一次判死"
    assert job["attempts"] == 1
    assert "api_key" in str(job["error"])


@pytest.mark.pg
def test_连接超时连续3次后failed且summary仍为NULL_B13(conn):
    from kb.ai.outcomes import FailureCategory, SummarizeFailed

    _config(conn)
    item_id = _new_item(conn, title="标题", content_text="字" * 200)

    class _Timeout:
        def summarize(self, title, text, vocabulary=()):  # noqa: ANN001
            return SummarizeFailed(
                category=FailureCategory.TIMEOUT, message="AI超时：读超时 10 秒无响应"
            )

    for _ in range(3):
        _ensure_job(conn, item_id, "summarize")
        loop.run_once(_Pool(conn), _Extractor(), _Timeout())
        conn.run("update jobs set run_after = now() where item_id = :i", i=item_id)

    assert _summary(conn, item_id)["summary"] is None
    jobs = [j for j in _jobs(conn, item_id) if j["step"] == "summarize"]
    assert any(j["state"] == "failed" for j in jobs)


@pytest.mark.pg
def test_base_url不可达时error含地址_E5(conn):
    """E5：`ai_base_url` 配了但不可达 → 按 B12 处理（配置错误，不是未配置），
    3 次后 `failed`，`jobs.error` 含地址。
    """
    from kb.store import settings as settings_store

    _config(conn)
    settings_store.set_value(conn, "ai_base_url", "https://unreachable.invalid/v1")
    item_id = _new_item(conn, title="标题", content_text="字" * 200)

    class _ConnectFail:
        def summarize(self, title, text, vocabulary=()):  # noqa: ANN001
            from kb.ai.outcomes import FailureCategory, SummarizeFailed

            return SummarizeFailed(
                category=FailureCategory.CONNECT,
                message="AI连接失败：unreachable.invalid 解析不到；3 分钟后重试",
            )

    for _ in range(3):
        _ensure_job(conn, item_id, "summarize")
        loop.run_once(_Pool(conn), _Extractor(), _ConnectFail())
        conn.run("update jobs set run_after = now() where item_id = :i", i=item_id)

    jobs = [j for j in _jobs(conn, item_id) if j["step"] == "summarize"]
    failed = [j for j in jobs if j["state"] == "failed"]
    assert failed, f"三次连接失败没有 failed：{jobs}"
    assert any("unreachable.invalid" in str(j["error"]) for j in failed)


# ── F1 / G1 ────────────────────────────────────────────────


@pytest.mark.pg
def test_empty词表时tag仍done且AI标签行数为0_F1(conn):
    """F1：`tags` 表为空 → 仍出摘要，`tags` 行数不变，`item_tags` 的 AI 行为 0，
    `tag` 任务 `done` 且 `jobs.error` 为空。

    注意 0004 预置了 8 个一级标签，所以「词表为空」得先把它们删掉——但迁移规则
    禁止 `delete from`，这里在事务内删（用例结束回滚）。
    """
    _config(conn)
    conn.run("delete from item_tags")
    conn.run("delete from tags")
    item_id = _new_item(conn, title="标题", content_text="字" * 200)
    _enqueue(conn, item_id, "summarize")
    client = _Client(tags=("健康",))

    loop.run_once(_Pool(conn), _Extractor(), client)
    loop.run_once(_Pool(conn), _Extractor(), client)

    assert _summary(conn, item_id)["summary"], "空词表仍应出摘要"
    tags_count = conn.run("select count(*) from tags")[0][0]
    assert int(str(tags_count)) == 0, "AI 建了标签"
    ai_rows = conn.run(
        "select count(*) from item_tags where item_id = :i and origin = 'ai'",
        i=item_id,
    )[0][0]
    assert int(str(ai_rows)) == 0
    tag_job = next(j for j in _jobs(conn, item_id) if j["step"] == "tag")
    assert tag_job["state"] == "done"
    assert tag_job["error"] is None


@pytest.mark.pg
def test_summarize不改content_text与kind与ocr_text_G1_G2_G3(conn):
    _config(conn)
    item_id = int(
        str(
            conn.run(
                "insert into items (kind, title, content_text, ocr_text)"
                " values ('article', '原标题', '原正文内容', '原OCR文字') returning id"
            )[0][0]
        )
    )
    _enqueue(conn, item_id, "summarize")

    loop.run_once(_Pool(conn), _Extractor(), _Client())

    row = conn.run(
        "select summary, content_text, kind, ocr_text from items where id = :i",
        i=item_id,
    )[0]
    assert row[0], "摘要没写"
    assert str(row[1]) == "原正文内容", "G1：摘要污染了正文"
    assert str(row[2]) == "article", "G2：摘要改了 kind"
    assert str(row[3]) == "原OCR文字", "G3：摘要写了 ocr_text"
