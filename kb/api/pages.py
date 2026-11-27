"""两个极简页面：登录页、首页。

不引前端框架、不引构建步骤，一个文件里内联样式。首页的提交框是 R4 的
「最小可用入口」——能在手机上粘一条进来就算达标，真正的取用界面在后续轮次。
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence

from kb.store.items import ItemSummary

_BASE_STYLE = """
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 24px;
    font: 16px/1.6 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif;
    background: #f6f6f7; color: #1c1c1e;
  }
  @media (prefers-color-scheme: dark) {
    body { background: #131315; color: #ececee; }
    .card { background: #1d1d20 !important; border-color: #2c2c31 !important; }
    input { background: #131315 !important; color: #ececee !important;
            border-color: #3a3a40 !important; }
    .muted { color: #9a9aa2 !important; }
    .row { border-color: #2c2c31 !important; }
  }
  .card {
    width: 100%; max-width: 420px; background: #fff; padding: 28px;
    border: 1px solid #e4e4e7; border-radius: 14px;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .muted { color: #6b6b73; font-size: 13px; margin: 0 0 20px; }
  label { display: block; font-size: 13px; margin: 14px 0 6px; }
  input {
    width: 100%; padding: 11px 12px; font-size: 16px; border-radius: 9px;
    border: 1px solid #d7d7dc; background: #fff;
  }
  button {
    width: 100%; margin-top: 20px; padding: 12px; font-size: 16px;
    border: 0; border-radius: 9px; background: #2f6fed; color: #fff;
    font-weight: 600;
  }
  button:disabled { opacity: .6; }
  .err { color: #d93025; font-size: 13px; margin-top: 12px; min-height: 18px; }
  .row { display: flex; justify-content: space-between; gap: 12px;
         padding: 9px 0; border-bottom: 1px solid #eee; font-size: 14px; }
  .row:last-child { border-bottom: 0; }
  code { font-size: 13px; }
  textarea {
    width: 100%; padding: 11px 12px; font-size: 16px; border-radius: 9px;
    border: 1px solid #d7d7dc; background: #fff; resize: vertical;
    font-family: inherit; line-height: 1.5;
  }
  .composer { margin: 0 0 22px; }
  .composer button { margin-top: 10px; }
  .item { align-items: center; }
  .item-main { min-width: 0; }
  .item-title {
    font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .item-meta { font-size: 12px; margin-top: 2px; }
  .badge {
    flex: 0 0 auto; font-size: 12px; padding: 3px 9px; border-radius: 999px;
    background: #eef1f6; color: #41506b; white-space: nowrap;
  }
  .badge.done { background: #e6f4ea; color: #1e6b36; }
  .badge.fail { background: #fdeaea; color: #a32217; }
  .badge.idle { background: transparent; color: #8a8a92; }
  @media (prefers-color-scheme: dark) {
    textarea { background: #131315 !important; color: #ececee !important;
               border-color: #3a3a40 !important; }
    .badge { background: #26262c; color: #b9c2d4; }
    .badge.done { background: #16351f; color: #7fd39a; }
    .badge.fail { background: #3a1c1a; color: #f2887d; }
    .badge.idle { background: transparent; color: #8a8a92; }
  }
"""

_LOGIN_BODY = """
  <div class="card">
    <h1>个人知识库</h1>
    <p class="muted">单人自用 · 登录后即可存取</p>
    <form id="f">
      <label for="u">账号</label>
      <input id="u" name="username" autocomplete="username" autocapitalize="off" required>
      <label for="p">密码</label>
      <input id="p" name="password" type="password" autocomplete="current-password" required>
      <button id="b" type="submit">登录</button>
      <div class="err" id="e"></div>
    </form>
  </div>
  <script>
    const f = document.getElementById('f');
    const e = document.getElementById('e');
    const b = document.getElementById('b');
    f.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      e.textContent = ''; b.disabled = true; b.textContent = '登录中…';
      try {
        const r = await fetch('/api/auth/login', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            username: document.getElementById('u').value,
            password: document.getElementById('p').value,
          }),
        });
        if (r.ok) { location.replace('/'); return; }
        const data = await r.json().catch(() => ({}));
        e.textContent = data.error || ('登录失败（HTTP ' + r.status + '）');
      } catch (err) {
        e.textContent = '网络错误：' + err;
      }
      b.disabled = false; b.textContent = '登录';
    });
  </script>
"""


def login_page(bootstrap: bool = False, error: str = "") -> str:
    """`bootstrap=True` 表示库里还没有账号，这一页同时承担「创建账号」。

    不在这里替用户预置密码：首账号只允许建一次（之后永久 410），
    密码由使用者在手机上自己设，忘了他自己知道该改哪。
    """
    body = _LOGIN_BODY
    if bootstrap:
        body = (
            body.replace("<h1>个人知识库</h1>", "<h1>首次使用 · 创建账号</h1>")
            .replace(
                "单人自用 · 登录后即可存取",
                "这个库只有你自己用，账号只建一次。密码请记牢——忘了只能改数据库",
            )
            .replace("/api/auth/login", "/api/auth/bootstrap")
            .replace(">登录</button>", ">创建账号</button>")
            .replace("b.textContent = '登录中…'", "b.textContent = '创建中…'")
            .replace("b.textContent = '登录'", "b.textContent = '创建账号'")
        )
    if error:
        body = body.replace(
            '<div class="err" id="e"></div>',
            f'<div class="err" id="e">{html.escape(error)}</div>',
        )
    return f"<!doctype html><html lang=zh-CN><head><meta charset=utf-8>\
<meta name=viewport content='width=device-width,initial-scale=1'>\
<title>登录 · 个人知识库</title><style>{_BASE_STYLE}</style></head>\
<body>{body}</body></html>"


def home_page(
    username: str, health: dict[str, object], recent: Sequence[ItemSummary] = ()
) -> str:
    rows = "".join(
        f'<div class="row"><span>{html.escape(label)}</span>'
        f"<code>{html.escape(_fmt(health.get(key)))}</code></div>"
        for key, label in (
            ("ok", "整体"),
            ("dbOk", "数据库"),
            ("meiliOk", "搜索 (Meilisearch)"),
            ("workerOk", "后台 worker"),
            ("diskFreeGb", "剩余空间 (GB)"),
            ("workerHeartbeatAt", "worker 心跳"),
        )
    )
    items_html = (
        "".join(_item_row(item) for item in recent)
        if recent
        else '<p class="muted">还没有内容，上面粘一条试试。</p>'
    )
    payload = html.escape(json.dumps(health, ensure_ascii=False, indent=2))
    return f"""<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>个人知识库</title><style>{_BASE_STYLE}</style></head>
<body><div class="card">
  <h1>你好，{html.escape(username)}</h1>
  <p class="muted">粘一条链接或一段文字进来，先存下，后台慢慢处理。</p>
  <form class="composer" id="composer">
    <textarea id="q" rows="2" placeholder="粘贴链接或一段文字，丢进来就行"></textarea>
    <button id="save" type="submit">存起来</button>
    <div class="err" id="ce"></div>
  </form>
  <p class="muted">最近存进来的（{len(recent)} 条）</p>
  {items_html}
  <details style="margin-top:18px"><summary class="muted">健康检查</summary>
    {rows}
    <pre style="overflow:auto;font-size:12px">{payload}</pre></details>
  <button id="out" type="button">退出登录</button>
</div>
<script>
  const form = document.getElementById('composer');
  const box = document.getElementById('q');
  const btn = document.getElementById('save');
  const err = document.getElementById('ce');
  form.addEventListener('submit', async (ev) => {{
    ev.preventDefault();
    const text = box.value.trim();
    err.textContent = '';
    if (!text) {{ err.textContent = '先粘点东西进来'; return; }}
    // 含链接就按「存链接」发，链接之外的原文一并带上当分享文案；
    // 不含链接就按「存一段文字」发。
    const found = text.match(/https?:\\/\\/\\S+/);
    const body = found ? {{url: found[0], raw_text: text}} : {{text: text}};
    btn.disabled = true; btn.textContent = '存中…';
    try {{
      const r = await fetch('/api/items', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(body),
      }});
      const data = await r.json().catch(() => ({{}}));
      if (r.ok) {{ location.reload(); return; }}
      err.textContent = data.error || ('保存失败（HTTP ' + r.status + '）');
    }} catch (e) {{
      err.textContent = '网络错误：' + e;
    }}
    btn.disabled = false; btn.textContent = '存起来';
  }});
  document.getElementById('out').addEventListener('click', async () => {{
    await fetch('/api/auth/logout', {{method: 'POST'}});
    location.replace('/login');
  }});
</script>
</body></html>"""


_STATUS_LABELS: dict[str, tuple[str, str]] = {
    "pending": ("待处理", "wait"),
    "running": ("处理中", "busy"),
    "done": ("已完成", "done"),
    "failed": ("失败", "fail"),
    "none": ("—", "idle"),
}


def _item_row(item: ItemSummary) -> str:
    label, css = _STATUS_LABELS.get(item.status, ("—", "idle"))
    title = item.title or "未命名"
    meta = item.source_domain or "纯文字"
    return (
        '<div class="row item">'
        f'<div class="item-main"><div class="item-title">{html.escape(title)}</div>'
        f'<div class="item-meta muted">{html.escape(meta)}</div></div>'
        f'<span class="badge {css}">{label}</span></div>'
    )


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "正常" if value else "异常"
    return str(value)
