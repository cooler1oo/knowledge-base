"""部署配置的自检：`deploy/supervisor/*.conf` 必须配齐检索要的环境变量。

## 为什么这要变成一条测试

2026-11-28 真机踩到两次**同一类**故障，两次都不是代码 bug：

1. `kb-api` 的 supervisor 配置里没有 `KB_MEILI_KEY` → Meili 对搜索返回 401 →
   `/api/search` 全部降级成 `fallback`。现象是「搜索能用」，只有 `searchMode`
   字段能看出不对。**没有任何报错**。
2. `kb-worker` 同样没有 key → 索引步全部 defer 600 秒 → 新内容永远搜不到。

两次都不写在代码里，所以单测与 `--arch` 都抓不到。而「配了没配」这件事
**完全可以机器检查**（AGENTS.md §八：能变成机器检查的就不该靠自觉）。

## 检查什么

| 断言 | 违反的后果 |
|---|---|
| 两个 conf 都出现 `KB_MEILI_KEY` | 401 / 静默降级 |
| 两个 conf 的 `KB_MEILI_KEY` **值相同** | api 与 worker 连不同 Meili（一边写一边搜不到） |
| 两个 conf 都出现 `KB_MEILI_URL` | 靠默认值，改地址时看不到 |
| 两个 conf 都把 `HTTP_PROXY` 等置空 | 127.0.0.1 被代理转发，表现为「服务全挂」 |
| `pyproject.toml` 的 packages 含 `kb.search` | 部署后 `import kb.search` 失败 |
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "deploy" / "supervisor"

#: 检索路径必须有的两个变量。名字与 `kb/search/config.py` 的常量一致
#: （`test_api_search.py` 有另一条用例钉住那个一致性）。
REQUIRED_ENV = ("KB_MEILI_KEY", "KB_MEILI_URL")

#: 必须显式置空的代理变量。`httpx` 的 `trust_env=False` 只挡住检索路径，
#: 媒体下载走别的客户端仍读环境变量。
PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy")


def _confs() -> list[Path]:
    files = sorted(SUPERVISOR.glob("*.conf"))
    assert files, f"{SUPERVISOR} 下没有任何 .conf；目录名写错了？"
    return files


def _env_line(path: Path) -> str:
    """取 `environment=` 那一行（超长不折行，取整行）。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("environment="):
            return line
    raise AssertionError(f"{path.name} 里没有 environment= 行")


@pytest.mark.parametrize("path", _confs(), ids=lambda p: p.name)
def test_supervisor配置含检索的两个环境变量(path: Path) -> None:
    """缺 `KB_MEILI_KEY` 会让 `/api/search` 静默降级、让索引步永远 defer。

    「静默」是这条断言存在的全部理由：401 在搜索路径上被设计成降级成
    `fallback`（那是**运行时**的正确选择——用户不该因为配置错就看不到任何结果），
    所以配置缺失在运行时不报错，只能靠**检查配置本身**发现。
    """
    line = _env_line(path)
    for name in REQUIRED_ENV:
        assert name in line, f"{path.name} 的 environment 缺少 {name}"


@pytest.mark.parametrize("path", _confs(), ids=lambda p: p.name)
def test_supervisor配置把代理变量置空(path: Path) -> None:
    """容器里残留的 `HTTP_PROXY` 会把 `127.0.0.1:7700` 的请求塞进代理，
    表现为「Meili 挂了」。`kb/ai/transport.py` 的 `B21` 记着同一个教训。"""
    line = _env_line(path)
    for name in PROXY_ENV:
        assert re.search(rf'{name}=""', line), f"{path.name} 没有把 {name} 置空"


def test_两个进程的meili_key一致() -> None:
    """api 与 worker 的 key 不同时，一边能写索引、另一边搜不到——两个都不报错。

    这是「同一个配置散在两处」的典型故障：不会报错，只是结果不对。
    """
    keys: dict[str, str] = {}
    for path in _confs():
        match = re.search(r'KB_MEILI_KEY="([^"]*)"', _env_line(path))
        assert match, f"{path.name} 里 KB_MEILI_KEY 不是带引号的字面量"
        keys[path.name] = match.group(1)
    assert len(set(keys.values())) == 1, f"两个进程的 KB_MEILI_KEY 不一致：{keys}"
    assert next(iter(keys.values())), "KB_MEILI_KEY 不能是空串"


def test_两个进程的meili_url一致() -> None:
    urls: dict[str, str] = {}
    for path in _confs():
        match = re.search(r'KB_MEILI_URL="([^"]*)"', _env_line(path))
        assert match, f"{path.name} 里 KB_MEILI_URL 不是带引号的字面量"
        urls[path.name] = match.group(1)
    assert len(set(urls.values())) == 1, f"两个进程的 KB_MEILI_URL 不一致：{urls}"


def test_api与worker都有supervisor配置() -> None:
    """两个进程各有自己的 conf。少一个时那个进程不会被 supervisor 拉起，
    而 `/api/health` 的 `workerOk` 会如实报 false——但没人会去看。"""
    names = {path.name for path in _confs()}
    assert "kb-api.conf" in names
    assert "kb-worker.conf" in names


def test_pyproject登记了kb_search() -> None:
    """不登记时 `pip install -e .` 装出来的包缺这个目录：
    测试靠 `pythonpath=["."]` 照跑得通，只有部署到平板后
    `import kb.search` 才失败。`kb.vision` 踩过同一个坑。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"kb.search"' in text
