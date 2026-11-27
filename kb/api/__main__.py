"""启动入口：`python -m kb.api [--host 0.0.0.0] [--port 8000]`。

连接池在这里创建、交给应用，应用不自己读环境变量建连——
这样测试可以直接塞一个假的池进来。
"""

from __future__ import annotations

import argparse

import uvicorn
from starlette.applications import Starlette

from kb.store.config import PgConfig
from kb.store.db import Pool

from .app import AppConfig, create_app


def build(argv: list[str] | None = None) -> tuple[Starlette, str, int]:
    """解析命令行。`argv=None` 表示「读 sys.argv」——这是 argparse 的约定，别自己填 []。

    曾经这里写成 `main()` 里 `list(argv or [])`，等于把 None 变成空列表，
    argparse 于是看不到任何参数、全用默认值，`--host 0.0.0.0` 被整份丢弃，
    uvicorn 一直绑在 127.0.0.1。后果是只能从平板本机访问，手机和电脑都进不来。
    """
    parser = argparse.ArgumentParser(prog="python -m kb.api")
    parser.add_argument("--host", default="127.0.0.1", help="默认只听本机")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    pool = Pool(PgConfig.from_env())
    app = create_app(pool, AppConfig.from_env())
    return app, args.host, args.port


def main(argv: list[str] | None = None) -> int:
    app, host, port = build(argv)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
