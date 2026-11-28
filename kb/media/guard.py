"""SSRF 判定与钉扎目标构造。**纯逻辑 + 注入解析器，不联网。**

## 攻击面为什么真实

`thumb_url` 与图文 `url` 完全由第三方 CDN 响应决定。若被改成
`http://127.0.0.1:8000/api/items`（打自己）或 `http://100.x.x.x:5432`
（打 Tailscale 内网的 PG），worker 会照做，而这是**服务端**发起的请求，
跨过了所有的同源与鉴权。

## 判据 = 三步叠加，任一步拒绝即拒绝

**① 内嵌 IPv4 的过渡形式一律拒绝，不解析。**

`::ffff:0:0/96`（IPv4-mapped）、`64:ff9b::/96`（NAT64 知名前缀）、
`64:ff9b:1::/48`（NAT64 本地前缀）、`2002::/16`（6to4）、`2001::/32`（Teredo）。

为什么不「解析出内嵌地址再判」：正常 CDN 直链绝不出现这五种，拒绝比解析安全
（解析要为每种形式写对偏移，写错就是漏），也比解析短。**一律拒绝比
「解析后判断内嵌地址是否公网」少一半代码、少一半出错面。**

⚠️ 内嵌的即使是合法公网地址（`::ffff:8.8.8.8`、`64:ff9b::0808:0808`）
**仍然拒绝**。这是有意的，用例里必须包含它们，否则这条会被后来的人
改成「解析后判断」。

**② 硬编码拒绝段。必须比 `version`。**

`v4 地址 in v6 网络` 恒为 `False`，`v6 地址 in v4 网络` 同样。这是
「只列 v4 段 → 整个 IPv6 空间静默放行」的根因——`::1` / `fd00::1` 会全过。

**③ 白名单语义：`is_global` 为真且六个 `is_*` 全为假。**

## 为什么 ① 与 ②③ 不能合并

`is_global` 的实现是 `self not in _public_network and not self.is_private`，
两张表都是 `ipaddress` 里**硬编码的枚举**。CPython 源码对 `2002::/16` 写着
「IANA says N/A, let's consider it not globally reachable to be safe」——
**这是策略选择，不是规范结论**，CPython 完全可能在某版本把它移出表。
① 不依赖任何 `is_*` 属性，所以表变了它仍成立。

## 为什么 ② 与 ③ 并列而不是留一个

只留 ② → 黑名单只能拦「想到的」：实测 `2002:7f00:0001::`（内嵌 127.0.0.1）
在只查纯 IPv4 黑名单时被**放行**。
只留 ③ → `64:ff9b::6440:9c2f`（内嵌 100.64.156.47，Tailscale 段）能过
`is_global` 与 `is_private` 前两项，靠 `is_reserved` 这个**不相干属性**凑巧挡住。
②③ 并列 → 两者都靠枚举，但枚举的内容和目的不同（② 是本项目的安全意图，
③ 是 IANA 注册表语义），一张表改了另一张还在。

## 判定对象是「全部解析结果」，不是任意一个

实测 `localhost` 解析出 `['127.0.0.1', '::1']` 两条。只判第一条就会漏掉第二条，
而攻击者控制 DNS 时可以让连接落到坏的那个上（选哪个由连接的 happy-eyeballs 决定）。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final, Protocol
from urllib.parse import urlsplit

from .models import PinnedTarget


class Resolver(Protocol):
    """域名 → IP 字面量列表。生产实现是 `socket.getaddrinfo` 的薄包装。

    返回 `list[str]` 而不是单个 IP：`getaddrinfo` 天然返回多条，只取第一条会漏掉
    「第二条是 127.0.0.1」这种情形。**判定作用于整个列表，连接才取其中一条。**
    """

    def __call__(self, host: str, port: int) -> list[str]: ...


#: 内嵌 IPv4 的 IPv6 过渡形式。**一律拒绝，不尝试解析出内嵌地址。**
_EMBEDDED_V4: Final[tuple[str, ...]] = (
    "::ffff:0:0/96",  # IPv4-mapped
    "64:ff9b::/96",  # NAT64 知名前缀（实测能过 is_global/is_private）
    "64:ff9b:1::/48",  # NAT64 本地前缀
    "2002::/16",  # 6to4（内嵌地址在第 2-5 字节）
    "2001::/32",  # Teredo
)

#: 硬编码拒绝段。与 `_EMBEDDED_V4` 并列，不互相替代。
_DENY_V4: Final[tuple[str, ...]] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",  # CGNAT：本项目平板的 Tailscale IP 就在这一段
    "127.0.0.0/8",
    "169.254.0.0/16",  # 链路本地，含云元数据 169.254.169.254
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",  # 组播
    "240.0.0.0/4",  # IANA 保留（实测 is_global=False、is_reserved=True，拒得对）
)
_DENY_V6: Final[tuple[str, ...]] = (
    "::/128",
    "::1/128",
    "100::/64",
    "2001:db8::/32",
    "3fff::/20",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)

_EMBEDDED: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = tuple(
    ipaddress.ip_network(net) for net in _EMBEDDED_V4
)
_DENY: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = tuple(
    ipaddress.ip_network(net) for net in _DENY_V4 + _DENY_V6
)

#: 允许的端口。PG 在内网 5432、Redis 6379、SSH 22，这些服务对「任意字节流」的
#: 响应足以被探测是否存在。CDN 图片 URL 从不使用非标准端口。
_ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443})
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


def is_public_ip(ip: str) -> bool:
    """这个 IP 能不能连。三步叠加，顺序固定，三步都不许省、也不许合并。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        # 不是 IP 字面量（调用方传错，或 resolver 吐了脏数据）。**拒绝**：
        # 「解析不出来」与「解析出来是内网」在安全上等价，都不该放行。
        return False

    # ① 内嵌 v4 的过渡形式一律拒绝。放在最前：这一步不依赖任何 is_* 属性，
    #    is_global 未来改行为时它仍成立。
    if isinstance(addr, ipaddress.IPv6Address) and any(
        addr in net for net in _EMBEDDED
    ):
        return False

    # ② 硬编码拒绝段。**必须比版本**：v4 地址 in v6 网络恒为 False，
    #    漏了这一步就是「只列 v4 段 → 整个 IPv6 空间静默放行」。
    for net in _DENY:
        if addr.version == net.version and addr in net:
            return False

    # ③ 白名单语义。与 ② 并列而非替代：② 是人写下来的意图，③ 接住 ② 没想到的段。
    return (
        addr.is_global
        and not addr.is_private
        and not addr.is_loopback
        and not addr.is_link_local
        and not addr.is_multicast
        and not addr.is_reserved
        and not addr.is_unspecified
    )


def _ip_literal(host: str) -> str | None:
    """hostname 若是 IP 字面量就原样返回，否则 `None`。

    `urlsplit` 已剥掉 IPv6 的方括号（`http://[::1]/` → `hostname='::1'`），
    所以这里不需要处理括号。**十进制/十六进制变形**
    （`http://2130706433/a.jpg` = 127.0.0.1）不在这里特殊处理：
    本机 Windows 的 `getaddrinfo` 对它报 `gaierror`，Linux/glibc 会解析成
    `127.0.0.1`；无论哪种，判定都落在 `is_public_ip` 上，不依赖 libc 行为。
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


def _bracket(ip: str) -> str:
    """IPv6 字面量拼进 URL 时必须带方括号。"""
    return f"[{ip}]" if ":" in ip else ip


def check_url(url: str, *, resolver: Resolver) -> PinnedTarget | str:
    """校验 URL 并给出「钉扎」后的连接目标。返回 `str` 表示**拒绝原因**。

    返回 `str` 而不是抛异常：拒绝是正常业务结果（第三方给的 URL 不可信是常态），
    调用方要把它记成 `SkippedMedia` 继续下一条。抛异常会让「这一张图不能用」
    变成「整个媒体阶段崩了」。

    顺序固定：

    1. `scheme ∈ {http, https}`、`port ∈ {None, 80, 443}`、不带账号密码
    2. `hostname` 若是 IP 字面量，直接走 `is_public_ip`（不经 DNS）；
       否则 resolver 解析出**全部** IP，**每一个**都必须合格
    3. 取第一个合格 IP 构造 `PinnedTarget`

    ## 钉扎为什么必须做

    只做「解析 → 校验 → 交给 httpx」不够：httpx 会**自己再解析一次**，
    两次解析之间域名可以变（DNS rebinding）。所以把 URL 重写成 IP 字面量，
    同时补 `Host` 头与 `sni_hostname`——后者经 `extensions` 透传到 httpcore 的
    TLS 握手，于是「连 IP」与「验证真域名证书」两者同时成立，不需要关掉证书校验。
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        return f"URL 无法解析（{exc}）"

    if parts.scheme not in _ALLOWED_SCHEMES:
        return f"协议不允许（{parts.scheme or '空'}），只允许 http/https"
    if parts.username or parts.password:
        return "URL 不允许带账号密码"

    try:
        port = parts.port
    except ValueError:
        return "端口不是数字"
    if port is not None and port not in _ALLOWED_PORTS:
        return f"端口不允许（{port}），只允许 80/443"

    host = (parts.hostname or "").strip()
    if not host:
        return "URL 没有主机名"

    literal = _ip_literal(host)
    if literal is not None:
        # IP 字面量：不走 DNS，直接判。与「域名解析出 IP」走同一条判定，
        # 所以不需要分支处理十进制/十六进制变形。
        if not is_public_ip(literal):
            return f"IP 不是公网地址（{literal}）"
        chosen = literal
    else:
        try:
            resolved = resolver(host, port or (443 if parts.scheme == "https" else 80))
        except Exception as exc:  # noqa: BLE001 解析失败一律是「这张图不能用」
            # **必须兜 `Exception` 而不是只兜 `OSError`**：本函数的契约是
            # 「永不抛，拒绝就返回 str」，而 `persist.store_media` 的主循环只
            # 认 `_Skip` / `EncodeError`——这里漏出的异常会跳出整个 for 循环，
            # 已经下载好的前几张字节全丢（`stored` 是局部变量，函数异常退出）。
            # 那正好违背「一张图 404 不该让另外 19 张一起丢」。
            #
            # 实测能抛的不止 `OSError`：`system_resolver` 走 `getaddrinfo`，
            # hostname 含非法 IDNA 字符时抛 `UnicodeError`（**不是** OSError）。
            # 因此不能按 socket 的异常类型逐个列举。
            #
            # `Exception` 天然不含 `KeyboardInterrupt` / `SystemExit`，所以
            # 退出信号不会被吞掉。这与 `kb/ai/transport.py` 的「四类异常归一成
            # kind」同一个口径：调用方只需知道「这次拿不到」。
            return f"域名解析失败（{host}）：{type(exc).__name__}: {exc}"
        if not resolved:
            return f"域名解析无结果（{host}）"
        # **每一个**都必须合格。实测 localhost 返回 [127.0.0.1, ::1]，
        # 只判第一个会漏掉第二个。
        for candidate in resolved:
            if not is_public_ip(candidate):
                return f"域名解析到非公网地址（{host} → {candidate}）"
        chosen = resolved[0]

    return PinnedTarget(
        connect_url=_rebuild(parts.scheme, chosen, parts.path, parts.query),
        host_header=host,
        sni_hostname=host,
    )


def _rebuild(scheme: str, ip: str, path: str, query: str) -> str:
    """拼 `scheme://<ip><path>?<query>`。

    不保留 fragment：HTTP 请求不带 fragment，留着只会让日志里的 URL 与实际请求
    不一致。路径为空时用 `/`——`https://1.2.3.4` 在部分服务端会被规范成
    `https://1.2.3.4/`，显式写出来省一次重定向（而重定向是**不跟随**的）。
    """
    target = f"{scheme}://{_bracket(ip)}{path or '/'}"
    if query:
        target = f"{target}?{query}"
    return target


def system_resolver(host: str, port: int) -> list[str]:
    """生产用解析器：`socket.getaddrinfo` 的薄包装。

    `type=SOCK_STREAM` 与 `proto=IPPROTO_TCP` 让结果只含 TCP 地址，
    避免把 `SOCK_DGRAM` 的条目一起判（两者对同一个域名可能给出不同集合）。

    **去重**：`getaddrinfo` 对同一 IP 的不同 socket 类型/协议会返回重复项，
    去重后用例里的「逐个判定」与错误消息里的地址列表才确定。
    """
    infos = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    seen: dict[str, None] = {}
    for info in infos:
        sockaddr = info[4]
        if isinstance(sockaddr, tuple) and sockaddr:
            seen.setdefault(str(sockaddr[0]), None)
    return list(seen)


def local_now_clock() -> float:
    """默认时钟：`time.monotonic`。

    放在本文件是为了让 `persist` 不必为了一个默认值 import `time`——
    它要的是「一个单调递增的秒数」，不关心实现。
    """
    import time

    return time.monotonic()
