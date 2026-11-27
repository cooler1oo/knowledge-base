"""密码哈希与会话令牌。

两条硬边界：
1. **密码不落明文、不进日志**。库里存 bcrypt 字符串。
2. **会话令牌库里只存 SHA-256 哈希**（技术方案 §6.2 的 `sessions.token_hash`），
   Cookie 里是明文令牌；库被读走也无法直接拿去登录。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from http.cookies import CookieError, SimpleCookie

import bcrypt

COOKIE_NAME = "kb_session"
SESSION_TTL_DAYS = 30
# bcrypt 只处理前 72 字节，超出部分被静默丢弃——与其「悄悄等同于更短的密码」，
# 不如在入口直接拒绝，让问题看得见。
MAX_PASSWORD_BYTES = 72

# 手机上 12 轮约 0.3–0.5s，登录频率极低，可接受；测试用 4 轮避免拖慢。
DEFAULT_ROUNDS = 12


def bcrypt_rounds() -> int:
    return int(os.environ.get("KB_BCRYPT_ROUNDS", str(DEFAULT_ROUNDS)))


def password_too_long(password: str) -> bool:
    return len(password.encode("utf-8")) > MAX_PASSWORD_BYTES


def hash_password(password: str) -> str:
    if password_too_long(password):
        raise ValueError(f"密码超过 {MAX_PASSWORD_BYTES} 字节")
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=bcrypt_rounds())
    ).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    """任何异常都算验证失败：宁可让用户重输，也不要 500。"""
    if password_too_long(password):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def new_session_token() -> tuple[str, str]:
    """返回 (明文, 哈希)。明文只进 Cookie，哈希只进库。"""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def token_from_cookie_header(header: str | None) -> str | None:
    if not header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(header)
    except (CookieError, ValueError):  # 畸形 Cookie 头不该让请求 500
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else None


def build_set_cookie(
    raw_token: str, secure: bool, ttl_days: int = SESSION_TTL_DAYS
) -> str:
    parts = [
        f"{COOKIE_NAME}={raw_token}",
        "Path=/",
        "HttpOnly",  # JS 读不到，降低 XSS 拿走登录态的后果
        "SameSite=Lax",  # 挡住跨站 POST 带 Cookie
        f"Max-Age={ttl_days * 24 * 3600}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def build_clear_cookie(secure: bool) -> str:
    parts = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)
