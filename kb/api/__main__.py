"""启动入口：`python -m kb.api [--host 0.0.0.0] [--port 8000]`。

连接池在这里创建、交给应用，应用不自己读环境变量建连——
这样测试可以直接塞一个假的池进来。
"""

from __future__ import annotations

import argparse

import uvicorn

from kb.store.config import PgConfig
from kb.store.db import Pool

from .app import AppConfig, create_app


def build(argv: list[str] | None = None) -> tuple[object, str, int]:
    parser = argparse.ArgumentParser(prog="python -m kb.api")
    parser.add_argument("--host", default="127.0.0.1", help="默认只听本机")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    pool = Pool(PgConfig.from_env())
    app = create_app(pool, AppConfig.from_env())
    return app, args.host, args.port


def main(argv: list[str] | None = None) -> int:
    parser_args = list(argv or [])
    app, host, port = build(parser_args)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
