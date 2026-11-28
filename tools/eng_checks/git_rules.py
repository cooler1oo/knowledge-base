"""提交纪律的机器检查。

三类规则，共同点都是「原本写在文档里靠自觉」：
1. 提交信息要有内容（主题格式 + 正文 + 必须写验证）
2. 单次提交不能是大杂烩（文件数/行数有上限，超了要显式放行）
3. 推送前工作区必须干净；交付前不许有未推送的提交

为什么第 3 条用 pre-push 而不是 commit 钩子：能机器判的只有「推之前有没有漏提交」，
「每次改动就提交」本身无法在提交那一刻判定（那是在判断未来）。
所以口径定为：**只要推送，就必须先把改动提交干净**。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SUBJECT_RE = re.compile(
    r"^(feat|fix|docs|test|refactor|chore|ci|perf|build|revert)(\([\w.\-/]+\))?: \S.{2,}$"
)
SUBJECT_MAX = 72
BODY_MIN_LINES = 2

MAX_FILES = 25
MAX_LINES = 800
BYPASS_ENV = "KB_ALLOW_LARGE_COMMIT"


@dataclass(frozen=True)
class Problem:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.detail}"


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def check_commit_message(message: str) -> list[Problem]:
    """检查一条完整提交信息（首行主题 + 空行 + 正文）。"""
    text = message.strip("\n")
    if not text.strip():
        return [Problem("提交信息为空", "至少要写做了什么")]
    lines = text.splitlines()
    subject = lines[0].strip()
    body = [line for line in lines[1:] if line.strip()]

    problems: list[Problem] = []
    if not SUBJECT_RE.match(subject):
        problems.append(
            Problem(
                "主题格式",
                f"「{subject}」不符合 type(scope): 描述；type 取 "
                "feat/fix/docs/test/refactor/chore/ci/perf/build/revert",
            )
        )
    if len(subject) > SUBJECT_MAX:
        problems.append(Problem("主题过长", f"{len(subject)} 字符，上限 {SUBJECT_MAX}"))
    if len(body) < BODY_MIN_LINES:
        problems.append(
            Problem("缺正文", f"正文至少 {BODY_MIN_LINES} 行：做了什么、为什么")
        )
    if "验证" not in text:
        problems.append(
            Problem(
                "缺验证", "正文里必须写「验证：」并附命令或输出，没有验证等于没做完"
            )
        )
    return problems


def staged_stats() -> tuple[int, int]:
    """返回 (暂存文件数, 增删行数合计)。"""
    out = _git("diff", "--cached", "--numstat")
    files = 0
    lines = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        added, deleted = parts[0], parts[1]
        lines += int(added) if added.isdigit() else 0
        lines += int(deleted) if deleted.isdigit() else 0
    return files, lines


def check_commit_size(files: int, lines: int) -> list[Problem]:
    """一次提交塞太多东西，说明把多次改动攒成了一坨。"""
    if os.environ.get(BYPASS_ENV) == "1":
        return []
    problems: list[Problem] = []
    if files > MAX_FILES:
        problems.append(
            Problem(
                "提交过大",
                f"{files} 个文件，上限 {MAX_FILES}（确需批量：设 {BYPASS_ENV}=1）",
            )
        )
    if lines > MAX_LINES:
        problems.append(
            Problem(
                "提交过大",
                f"{lines} 行改动，上限 {MAX_LINES}（确需批量：设 {BYPASS_ENV}=1）",
            )
        )
    return problems


def check_worktree_clean() -> list[Problem]:
    """推送前必须干净：有未提交改动就推，等于把改动留在本地没备份。"""
    dirty = [ln for ln in _git("status", "--porcelain").splitlines() if ln.strip()]
    if not dirty:
        return []
    preview = "；".join(dirty[:5])
    more = f" 等 {len(dirty)} 项" if len(dirty) > 5 else ""
    return [Problem("工作区不干净", f"先提交再推送：{preview}{more}")]


def check_pushed() -> list[Problem]:
    """交付前不许有未推送的提交。没有上游就直说。"""
    try:
        ahead = int(_git("rev-list", "--count", "@{u}..HEAD").strip() or 0)
    except RuntimeError:
        return [
            Problem("未设置上游", "main 还没跟踪 origin/main：git push -u origin main")
        ]
    if ahead:
        return [Problem("有未推送提交", f"{ahead} 个提交只在本地，git push")]
    return []


# ── git 走不走代理 ───────────────────────────────────────────
#
# 为什么需要这条：`curl` 会读 `HTTPS_PROXY`，git **不会**。
# Git for Windows 默认 `http.sslBackend=schannel`，这个后端只用
# `http.proxy` 配置项，不碰环境变量。于是出现过这种事：
#
#   $ echo $HTTPS_PROXY          → http://127.0.0.1:24377   （环境里明明有）
#   $ git ls-remote origin       → schannel: server closed abruptly
#   $ git -c http.proxy=... push → 成功
#
# 报错信息长得像 TLS 问题（`missing close_notify` / `unexpected eof`），
# 实际是请求根本没到代理，直连被墙。抓这个坑花的时间足够写十条检查。
#
# 端口是**动态**的（WorkBuddy 每次会话重新分配），所以这里不写死端口：
# 只判「环境里有代理变量，但 git 读不到」这个矛盾，矛盾在就报警，
# 并把实际端口原样带出来供直接使用。

PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def _env_proxy() -> str | None:
    """环境变量里给的代理地址（优先 https）。"""
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _git_proxy() -> str | None:
    """git 实际生效的代理（仓库级或全局级）。"""
    try:
        return _git("config", "--get", "http.proxy").strip() or None
    except RuntimeError:
        return None


def check_git_proxy() -> list[Problem]:
    """环境有代理但 git 没配 → 推送会以 TLS 报错误导人。

    只在这种「矛盾」存在时报错，环境里本来就没代理时不误报
    （那种情况直连可能本来就通，不该管）。
    """
    env_proxy = _env_proxy()
    if not env_proxy:
        return []
    if _git_proxy():
        return []
    return [
        Problem(
            "git 读不到代理",
            f"环境有 {env_proxy} 但 git 没配 http.proxy。"
            f"Git 的 schannel 后端只用配置项、不读环境变量，"
            f"直接 push 会报 TLS 错。先跑一次："
            f"git config http.proxy {env_proxy}",
        )
    ]


# ── 「测试跑过」的留痕 ───────────────────────────────────────
#
# 为什么记内容哈希而不是 commit 号：测试几乎总是在提交之前跑，提交之后 HEAD 变了
# 但内容没变。用 commit 号比对会让这条检查每次都红，等于废掉。git 的 tree 哈希
# 正好是「这批文件的内容」，提交不改内容，所以比对能对上。

MARKER_REL = Path(".pytest_cache") / "kb-last-green.json"


def working_tree_hash(root: Path | None = None) -> str:
    """工作区内容的哈希：按「路径+内容」算，与有没有提交无关。

    试过 `git stash create` 的 tree，有两个误判：不含未跟踪文件；且同一份内容在
    「未跟踪」与「已提交」两种状态下算出的值不同，于是每次提交后留痕都失效。
    改为直接读工作区文件——集合取 `git ls-files` 加未跟踪文件，提交前后一致。
    """
    base = Path(root) if root is not None else Path.cwd()
    tracked = _git("ls-files", cwd=root).splitlines()
    others = _git("ls-files", "--others", "--exclude-standard", cwd=root).splitlines()
    paths = sorted({p for p in [*tracked, *others] if p.strip()})

    digest = hashlib.sha256()
    for rel in paths:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update((base / rel).read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def write_last_green(
    marker: Path,
    tree: str,
    at: datetime,
    pg_seen: dict[str, int] | None = None,
) -> None:
    """写留痕。`pg_seen` 记这次会话里 `@pytest.mark.pg` 跑过/跳过各多少条。

    为什么要记：pg 用例在本机全部 skip，而「唯一索引冲突」「旧值残留」这类
    缺陷只存在于真实 SQL 语义里。两种绿灯（含 pg / 全跳过 pg）价值不同，
    不区分就等于看不出「真库验过没」。
    """
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"tree": tree, "at": at.isoformat()}
    if pg_seen is not None:
        payload["pgPassed"] = pg_seen.get("passed", 0)
        payload["pgSkipped"] = pg_seen.get("skipped", 0)
    marker.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def read_last_green(marker: Path) -> dict[str, str] | None:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def check_last_green(marker: Path, current_tree: str) -> list[Problem]:
    """交付前必须有「当前这份内容跑过测试且全绿」的记录。

    提示语里特意写成裸 `python -m pytest`：外部还有一层 Stop 门禁，它只认
    **第一段是 python/pytest 的命令**。`cd x && python -m pytest` 会被它判成
    「没跑验证」——尽管测试确实跑了、留痕也确实写了。本项目已连续被这条
    判错 4 次，原因不在测试，在命令形式的包装。所以这里给的永远是能同时
    满足两套机制的形式。
    """
    data = read_last_green(marker)
    if not data:
        return [
            Problem(
                "没跑过测试",
                "先跑 python -m pytest（不要加 cd 前缀，见下方说明）",
            )
        ]
    if not current_tree:
        return [Problem("取不到内容哈希", "确认在 git 仓库里执行")]
    if data.get("tree") != current_tree:
        return [
            Problem(
                "测试不是这份内容跑的",
                "自上次绿灯之后内容变过，重跑 python -m pytest",
            )
        ]
    return []


def pg_coverage_note(marker: Path) -> str:
    """这条绿灯到底含不含真库验证——只作提示，不判失败。

    pg 用例在本机必然全 skip（没有 Postgres），所以「全跳过」不是错误，
    但它意味着唯一索引冲突、旧值残留这类缺陷这次一条都没查。把这件事说出来，
    比让人以为「passed 就是验过了」安全。

    2026-11-28 的教训：本机 1021 passed 全绿时，`mediaTruncated` 残留键的
    bug 一直藏在 skip 后面，到平板真库才炸出来。
    """
    data = read_last_green(marker) or {}
    passed = data.get("pgPassed")
    skipped = data.get("pgSkipped")
    if passed is None and skipped is None:
        return "pg 覆盖未知（这条留痕是旧版写的，重跑一次即可记录）"
    passed_n = int(passed or 0)
    skipped_n = int(skipped or 0)
    if skipped_n and not passed_n:
        return (
            f"pg 用例 {skipped_n} 条**全部跳过**（本机无 Postgres）："
            "唯一索引冲突 / 旧值残留这类缺陷这次没查。"
            "要验请到平板真库跑，见 HANDOFF「平板真库怎么跑测试」"
        )
    if passed_n:
        return f"pg 用例 {passed_n} 条在真库上跑过，{skipped_n} 条跳过"
    return "本次没有 pg 用例"
