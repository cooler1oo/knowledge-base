"""共享 fixture。

集成测试的开关是「有没有显式配置数据库」：

- 配了 `KB_PG_*` 就等于承诺能连上，连不上是**失败**，不是跳过。
  原因：2026-11-26 21:54:48 出现过一次「16 个用例在 70ms 内全跳过、
  `pytest -q` 退出码 0、看起来全绿」的事故，真实故障被 `skip` 吞掉了。
- 没配就是「本机没装库」，跳过并在 `-ra` 里显示原因。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kb.store.config import TEST_DATABASE, PgConfig
from kb.store.db import connect

# 出现任意一个就视为「显式承诺能连库」
_EXPLICIT_ENV = ("KB_PG_HOST", "KB_PG_PORT", "KB_PG_USER", "KB_PG_DATABASE")


def explicit_env() -> list[str]:
    return [name for name in _EXPLICIT_ENV if os.environ.get(name)]


def pg_test_config_or_skip() -> PgConfig:
    """返回指向 kb_test 的配置；未显式配置且连不上时跳过。"""
    cfg = PgConfig.from_env(TEST_DATABASE)
    explicit = explicit_env()
    try:
        conn = connect(cfg.with_database("postgres"))
    except Exception as exc:  # noqa: BLE001 任何连接异常转成跳过或失败
        detail = f"Postgres 不可达（{cfg.host}:{cfg.port}，用户 {cfg.user}）：{exc}"
        if explicit:
            pytest.fail(f"已显式设置 {'/'.join(explicit)}，视为承诺连库；{detail}")
        pytest.skip(f"未设置 KB_PG_*，按「本机无库」跳过。{detail}")
    conn.close()
    return cfg


@pytest.fixture(scope="session")
def pg_cfg() -> PgConfig:
    return pg_test_config_or_skip()


def pytest_sessionfinish(session, exitstatus) -> None:
    """全绿的会话留一条痕迹，供 `tools.eng_checks --all` 判断「这份内容跑过测试」。

    记内容哈希而不是 commit：测试几乎总在提交前跑，提交后 HEAD 变了、内容没变。
    **只认全量运行**（命令行没带路径）：否则跑一个测试文件也会留痕，--all 就以为
    整库验证过了，这条检查等于没写。
    写失败不影响测试结果：留痕是附加信息，不该让绿灯变红。
    """
    if exitstatus != 0:
        return
    # 裸跑 `pytest` 时 config.args 会被 ini 的 testpaths 填上（实测是 ["tests"]），
    # 所以不能判空；改成「位置参数集合 == testpaths 集合」才算全量。
    positional = {
        Path(a).as_posix() for a in session.config.args if not str(a).startswith("-")
    }
    wanted = {
        Path(t).as_posix() for t in (session.config.getini("testpaths") or ["tests"])
    }
    if positional and positional != wanted:
        return
    try:
        from datetime import datetime, timezone

        from tools.eng_checks import git_rules

        tree = git_rules.working_tree_hash()
        if tree:
            git_rules.write_last_green(
                Path(git_rules.MARKER_REL),
                tree,
                datetime.now(timezone.utc),
                pg_seen=_pg_stats(),
            )
    except Exception:  # noqa: BLE001 任何连接异常转成跳过或失败
        pass


def _pg_stats() -> dict[str, int]:
    """这次会话里「需要真库」的用例跳过了多少条。

    **为什么要把这个写进留痕**：真库用例在本机（无 Postgres）全部 skip，
    而「旧值删不掉」「唯一索引冲突」这类缺陷只存在于真实 SQL 语义里，假连接
    永远碰不到。2026-11-28 就因此漏过一个真 bug（`mediaTruncated` 残留键）：
    本机 1021 passed 全绿，到平板真库才炸出 3 条红。

    写进留痕后，`--all` 能看出「这次绿灯含不含真库验证」。

    ## 判据是「为什么跳过」，不是文件名

    按文件名 `_pg` 判会错两次（2026-11-28 两次都踩了）：

    1. `test_tags_store_pg.py` 里有 14 条**静态断言**（查源码字符串确认没有
       `insert into tags`），根本不碰数据库，却被算成「真库用例通过」。
    2. **跳过发生在 setup 阶段**（fixture 抛 skip），只判 `when == "call"`
       会把所有 skip 漏掉，`pgSkipped` 恒为 0——正是「看不出有没有验真库」
       的那个毛病。

    改用跳过原因：真库用例的 skip 消息统一含「KB_PG_*」或「Postgres 不可达」
    （见 `pg_test_config_or_skip`）。reason 就在 report 上，比文件名精确。

    只统计**跳过**的条数，不猜「通过的里有几条是真库跑过的」——那个从
    report 反推不出来（通过的用例没有 reason）。宁可少说，不说错。
    """
    skipped = 0
    for report in _REPORTS:
        if not report.skipped:
            continue
        reason = str(getattr(report, "longrepr", "") or "")
        if "KB_PG_*" in reason or "Postgres 不可达" in reason:
            skipped += 1
    return {"passed": 0, "skipped": skipped}


#: 收集每条用例的 report。挂在模块上而不是 session 上：
#: `pytest.TestReport` **没有** `session` 属性（实测 getattr 返回 None），
#: 原写法因此一条都没收集到、`pgSkipped` 恒为 0。
_REPORTS: list[pytest.TestReport] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """记下每条 report，供 `pytest_sessionfinish` 统计真库覆盖。"""
    _REPORTS.append(report)
