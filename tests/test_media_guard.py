"""SSRF 判定的完整拒绝/放行表。

**这张表来自设计 §7.1 的 43 条用例**，每一条都是 2026-11-28 在本机实测过的
（Python 3.13.14）。用例照着数字抄，不照感觉写——判据写太严的现象是
「所有封面都下不下来」，日志里只有一句「IP 不可用」；写太松的现象是
worker 被第三方响应引导去打本机 API。

## 三步叠加，任一拒绝即拒绝

① 内嵌 IPv4 的过渡形式一律拒绝（不解析）
② 硬编码拒绝段（**必须比 `version`**）
③ 白名单语义：`is_global` 为真且六个 `is_*` 全为假

## 为什么 `::ffff:8.8.8.8` 与 `64:ff9b::0808:0808` 也拦

它们内嵌的是**合法公网地址**，本方案仍然拒绝。这是有意的——内嵌形式的正常
用途是过渡/翻译机制，CDN 直链不会用；一律拒绝比「解析后判断内嵌地址是否公网」
少一半代码、少一半出错面。**用例里必须包含这两条**，否则「一律拒绝」这条
会被后来的人改成「解析后判断」。
"""

from __future__ import annotations

import pytest

from kb.media.guard import check_url, is_public_ip
from kb.media.models import PinnedTarget

# ── 必须拦住 ─────────────────────────────────────────────────

#: v4 内网与保留段
DENY_V4 = (
    "127.0.0.1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "0.0.0.0",
    "255.255.255.255",
    "198.18.0.1",
    "192.0.2.1",
    "198.51.100.1",
    "203.0.113.1",
    # 240.0.0.0/4 是 IANA 保留段。一开始以为是误拦，实测 is_global=False、
    # is_reserved=True——拒它是对的。
    "240.0.0.1",
)

#: **本项目平板自己的 Tailscale IP**。打 `http://100.124.156.47:8000/api/items`
#: 就是打自己的 API。`is_private` 拦不住这一段，靠 ② 的 `100.64.0.0/10`。
DENY_CGNAT = ("100.124.156.47", "100.64.0.1")

DENY_METADATA = ("169.254.169.254",)

DENY_MULTICAST_V4 = ("224.0.0.1", "239.255.255.250")

#: `::1` / `fd00::1` 靠 ② 拦住——**只列 v4 段会全放行**，这是「比 version」的根因。
DENY_V6 = ("::1", "::", "fd00::1", "fe80::1", "100::1", "2001:db8::1", "3fff::1")

DENY_MULTICAST_V6 = ("ff02::1",)

#: 内嵌 v4 的五种过渡形式。**一律拒绝，不解析。**
DENY_EMBEDDED = (
    "::ffff:127.0.0.1",
    "::ffff:100.124.156.47",
    "::ffff:8.8.8.8",  # 内嵌公网地址也拒（见模块 docstring）
    "64:ff9b::7f00:1",
    "64:ff9b::6440:9c2f",  # 内嵌 Tailscale 段
    "64:ff9b::0808:0808",  # 内嵌公网地址也拒
    "64:ff9b:1::1",
    "2002:7f00:0001::",  # 内嵌 127.0.0.1
    "2002:6440:9c2f::",  # 内嵌 100.64.156.47
    "2002:a9fe:a9fe::",  # 内嵌 169.254.169.254
    "2002:0808:0808::",  # 内嵌 8.8.8.8，同样拒
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
)

# ── 必须放行 ─────────────────────────────────────────────────

ALLOW = (
    "8.8.8.8",
    "1.1.1.1",
    "104.16.123.96",
    "151.101.1.140",
    "93.184.216.34",
    "23.45.67.89",
    "2001:4860:4860::8888",
    "2606:4700::1111",
    "2400:3200::1",
)

DENY_ALL = DENY_V4 + DENY_CGNAT + DENY_METADATA + DENY_MULTICAST_V4 + DENY_V6
DENY_ALL += DENY_MULTICAST_V6 + DENY_EMBEDDED


@pytest.mark.parametrize("ip", DENY_ALL)
def test_内网与保留地址被拒(ip: str) -> None:
    assert not is_public_ip(ip), f"这个地址被放行了：{ip}"


@pytest.mark.parametrize("ip", ALLOW)
def test_真实公网地址放行(ip: str) -> None:
    """这条必须验：判据写太严的现象是「所有封面都下不下来」。

    表里的地址来自真实 CDN 与公共 DNS——`104.16.123.96` / `151.101.1.140`
    是 2026-11-28 实测 `www.cloudflare.com` / `www.python.org` 解析出的 IP，
    `2606:4700::1111` 是 Cloudflare 的公共 DNS。
    """
    assert is_public_ip(ip), f"公网地址被误拦了：{ip}"


def test_用例总数与设计一致():
    """这一组至少 42 条，且**两个方向都有**。

    少了这条，把 `is_public_ip` 改成 `return False` 也能让「全部拦截」那半张表通过。
    """
    assert len(DENY_ALL) >= 33, f"拦截用例少了：{len(DENY_ALL)}"
    assert len(ALLOW) >= 9, f"放行用例少了：{len(ALLOW)}"


def test_非IP字面量被拒():
    """resolver 吐脏数据时不能放行。「解析不出来」与「解析出来是内网」在安全上等价。"""
    for text in ("", "not-an-ip", "example.com", "999.999.999.999"):
        assert not is_public_ip(text), f"{text!r} 被当成合法 IP 放行了"


# ── check_url：URL 层判定 ────────────────────────────────────


class _Resolver:
    """固定 IP 表的假解析器。**构造参数注入，不 monkeypatch。**

    解不出（表里没有）时抛 `socket.gaierror`，与 `getaddrinfo` 真实行为一致。
    """

    def __init__(self, table: dict[str, list[str]]) -> None:
        self._table = table
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        if host not in self._table:
            import socket

            raise socket.gaierror(f"假解析器没有 {host}")
        return self._table[host]


def test_ip字面量url直接走is_public_ip不经DNS():
    """`http://1.2.3.4/a.jpg` 与「域名解析出 1.2.3.4」走同一条判定，不需要分支。

    额外好处：十进制/十六进制/八进制变形（`http://2130706433/a.jpg` = 127.0.0.1）
    也因此被兜住，不依赖 libc 的解析行为。
    """
    resolver = _Resolver({})
    target = check_url("http://8.8.8.8/a.jpg", resolver=resolver)
    assert isinstance(target, PinnedTarget)
    assert resolver.calls == [], "IP 字面量不该走 DNS"
    assert target.host_header == "8.8.8.8"
    assert target.connect_url == "http://8.8.8.8/a.jpg"


def test_ipv6字面量url拼上方括号():
    """IPv6 字面量拼进 URL 必须带方括号，否则 URL 解析失败。"""
    target = check_url("https://[2606:4700::1111]/a.jpg", resolver=_Resolver({}))
    assert isinstance(target, PinnedTarget)
    assert target.connect_url == "https://[2606:4700::1111]/a.jpg"


def test_内网IP字面量被拒():
    target = check_url("http://127.0.0.1:80/a.jpg", resolver=_Resolver({}))
    assert isinstance(target, str)
    assert "公网" in target


def test_域名解析出的每一个IP都要合格():
    """实测 `localhost` 返回 `['127.0.0.1', '::1']` 两条。

    只判第一个就会漏掉第二个；攻击者控制 DNS 时可以让连接落到坏的那个上
    （选哪个由连接的 happy-eyeballs 决定）。**判定作用于整个列表。**
    """
    resolver = _Resolver({"evil.example": ["8.8.8.8", "127.0.0.1"]})
    target = check_url("https://evil.example/a.jpg", resolver=resolver)
    assert isinstance(target, str), "列表里第二个 IP 是内网，整条必须拒绝"
    assert "127.0.0.1" in target


def test_全部合格的域名被钉扎到第一个IP():
    resolver = _Resolver({"cdn.example": ["104.16.123.96", "104.16.124.96"]})
    target = check_url("https://cdn.example/img/a.jpg?x=1", resolver=resolver)
    assert isinstance(target, PinnedTarget)
    assert target.connect_url == "https://104.16.123.96/img/a.jpg?x=1"
    assert target.host_header == "cdn.example"
    assert target.sni_hostname == "cdn.example"


def test_钉扎保留query而不保留fragment():
    """签名 URL 的全部信息都在 query 里（`x-expires` / `x-signature`），

    丢了 query 就是 403。fragment 不进 HTTP 请求，留着只会让日志与实际不符。
    """
    resolver = _Resolver({"cdn.example": ["8.8.8.8"]})
    target = check_url(
        "https://cdn.example/a.jpeg?x-expires=1797048000&x-signature=abc#frag",
        resolver=resolver,
    )
    assert isinstance(target, PinnedTarget)
    assert "x-expires=1797048000" in target.connect_url
    assert "x-signature=abc" in target.connect_url
    assert "#frag" not in target.connect_url


@pytest.mark.parametrize(
    ("url", "keyword"),
    [
        ("ftp://cdn.example/a.jpg", "协议"),
        ("file:///etc/passwd", "协议"),
        ("javascript:alert(1)", "协议"),
        ("", "协议"),
        # 打自己最典型的形态：本机 API 在 8000
        ("http://127.0.0.1:8000/api/items", "端口"),
        ("https://cdn.example:5432/a.jpg", "端口"),
        ("https://cdn.example:6379/a.jpg", "端口"),
        ("https://cdn.example:22/a.jpg", "端口"),
        ("https://user:pass@cdn.example/a.jpg", "账号"),
        ("https://user@cdn.example/a.jpg", "账号"),
        ("https:///a.jpg", "主机名"),
    ],
)
def test_非法URL被拒(url: str, keyword: str) -> None:
    resolver = _Resolver({"cdn.example": ["8.8.8.8"]})
    reason = check_url(url, resolver=resolver)
    assert isinstance(reason, str), f"这条 URL 不该通过：{url}"
    assert keyword in reason, f"拒绝原因没说清是{keyword}问题：{reason}"


def test_默认端口80和443被接受():
    """`https://cdn.example/a.jpg` 的 `.port` 是 `None`（未显式写），必须放行。"""
    resolver = _Resolver({"cdn.example": ["8.8.8.8"]})
    assert isinstance(
        check_url("https://cdn.example/a.jpg", resolver=resolver), PinnedTarget
    )
    assert isinstance(
        check_url("http://cdn.example:80/a.jpg", resolver=resolver), PinnedTarget
    )
    assert isinstance(
        check_url("https://cdn.example:443/a.jpg", resolver=resolver), PinnedTarget
    )


def test_解析失败返回拒绝原因而不是抛异常():
    """域名解析不了是**正常业务结果**，不是异常。

    抛异常会让「这一张图不能用」变成「整个媒体阶段崩了」。
    """
    reason = check_url("https://no-such-host.example/a.jpg", resolver=_Resolver({}))
    assert isinstance(reason, str)
    assert "解析失败" in reason


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("resolver 内部炸了"),
        UnicodeError("hostname 含非法 IDNA 字符"),
        ValueError("假的解析器"),
        MemoryError(),
        TimeoutError("解析超时"),
        KeyError("假设的字典查找"),
    ],
)
def test_resolver抛非OSError也返回拒绝原因(error: Exception) -> None:
    """**`check_url` 的契约是「永不抛」，不是「只兜 OSError」。**

    ## 为什么只兜 `OSError` 不够

    `system_resolver` 走 `socket.getaddrinfo`，它能抛的不止 `OSError`：
    hostname 含非法 IDNA 字符时抛 `UnicodeError`（**不是** OSError 的子类）。
    而注入的 `Resolver` 是任意实现，抛什么都可能。

    ## 漏出去的实际后果

    `persist.store_media` 的主循环认 `_Skip` / `EncodeError` / `Exception`
    三种（第三者是兜底）。若这里漏出异常，即使 persist 兜住了，错误也会变成
    「未预期异常」而不是「域名解析失败（原因）」——用户看到的 `props.mediaError`
    无法指向真正的原因（这个域名的 DNS 坏了）。

    `Exception` 天然不含 `KeyboardInterrupt` / `SystemExit`，所以退出信号
    不会被吞。
    """

    def boom(host: str, port: int) -> list[str]:
        raise error

    result = check_url("https://x.example/a.jpg", resolver=boom)
    assert isinstance(result, str), f"check_url 契约是永不抛，实际抛了（{error!r}）"
    assert "解析失败" in result
    assert type(error).__name__ in result, f"拒绝原因里没写异常类型：{result}"


def test_解析出空列表也被拒():
    """空列表意味着「解析成功但一个地址都没有」，绝不能当成「没有坏地址」。"""
    reason = check_url(
        "https://empty.example/a.jpg", resolver=_Resolver({"empty.example": []})
    )
    assert isinstance(reason, str)
    assert "无结果" in reason


def test_重定向不改变首次判定():
    """`check_url` 只判第一个 URL；跳转的目标由 `fetch` 用
    `follow_redirects=False` 拦住。

    这条是**结构断言**：一个 `302 → http://127.0.0.1:8000/` 能绕过全部三层，
    而跟随并重新校验每一跳会让代码量与用例数都翻倍，CDN 图片地址是直链、
    不需要跳转。所以守门放在「不跟随」而不是「重新校验」。
    """
    import inspect

    from kb.media import fetch

    source = inspect.getsource(fetch.HttpxDownloader.get)
    assert "follow_redirects=False" in source, "重定向必须不跟随"


def test_生产解析器去重且只取TCP():
    """`getaddrinfo` 对同一 IP 会因 socket 类型/协议返回重复项。

    不去重的后果：错误消息里同一个地址出现三次（排障时以为有三个坏地址），
    而「逐个判定」在重复项上做的也是同一件事。
    """
    from kb.media.guard import system_resolver

    ips = system_resolver("127.0.0.1", 80)
    assert ips, "本机解析 127.0.0.1 应该有结果"
    assert len(ips) == len(set(ips)), f"没去重：{ips}"
    assert "127.0.0.1" in ips
