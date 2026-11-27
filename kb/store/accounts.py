"""users / sessions 两张表的读写。

只有这一层碰这两张表（技术方案 §7.5 的架构边界）。密码哈希怎么算由
`kb/api/auth.py` 决定，这里只负责存字符串——store 不该知道用的是 bcrypt 还是别的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pg8000.native

SESSION_TTL_DAYS = 30
# 剩余不足这么多天才续期：避免每个请求都写库（单用户也不该白白产生写放大）
RENEW_WHEN_REMAINING_DAYS = 20


def count_users(conn: pg8000.native.Connection) -> int:
    return int(conn.run("select count(*) from users")[0][0])


def create_user(
    conn: pg8000.native.Connection, username: str, password_hash: str
) -> int:
    return int(
        conn.run(
            "insert into users (username, password_hash) values (:u, :h) returning id",
            u=username,
            h=password_hash,
        )[0][0]
    )


def find_user(
    conn: pg8000.native.Connection, username: str
) -> tuple[int, str, str] | None:
    """返回 (id, username, password_hash)。"""
    rows = conn.run(
        "select id, username, password_hash from users where username = :u",
        u=username,
    )
    if not rows:
        return None
    row = rows[0]
    return int(row[0]), str(row[1]), str(row[2])


def create_session(
    conn: pg8000.native.Connection,
    token_hash: str,
    ttl_days: int = SESSION_TTL_DAYS,
) -> None:
    expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    conn.run(
        "insert into sessions (token_hash, expires_at) values (:t, :e)",
        t=token_hash,
        e=expires,
    )


def find_session(
    conn: pg8000.native.Connection, token_hash: str
) -> tuple[int, datetime] | None:
    """只返回未过期的会话；返回 (id, expires_at)。"""
    rows = conn.run(
        "select id, expires_at from sessions "
        "where token_hash = :t and expires_at > now()",
        t=token_hash,
    )
    if not rows:
        return None
    return int(rows[0][0]), rows[0][1]


def touch_session(
    conn: pg8000.native.Connection,
    session_id: int,
    expires_at: datetime,
    ttl_days: int = SESSION_TTL_DAYS,
) -> bool:
    """滑动续期。返回是否真的续了（剩余不足 20 天才会续）。"""
    now = datetime.now(timezone.utc)
    if expires_at - now > timedelta(days=RENEW_WHEN_REMAINING_DAYS):
        conn.run("update sessions set last_seen = now() where id = :i", i=session_id)
        return False
    conn.run(
        "update sessions set last_seen = now(), expires_at = :e where id = :i",
        e=now + timedelta(days=ttl_days),
        i=session_id,
    )
    return True


def delete_session(conn: pg8000.native.Connection, token_hash: str) -> None:
    conn.run("delete from sessions where token_hash = :t", t=token_hash)


def purge_expired(conn: pg8000.native.Connection) -> int:
    """清掉过期会话。启动时跑一次即可，单用户不会有回收压力。"""
    rows = conn.run("delete from sessions where expires_at <= now() returning id")
    return len(rows)


def first_username(conn: pg8000.native.Connection) -> str:
    """库里唯一那个账号的名字。会话表不存 username（单用户），所以由这里补。

    放在 store 而不是 api：api 一碰 `pg8000.native.Connection` 这个类型就得
    import 驱动，等于把数据库边界捅穿了。
    """
    rows = conn.run("select username from users order by id limit 1")
    return str(rows[0][0]) if rows else ""
