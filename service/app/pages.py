"""内置默认页：根路径落地页与统一的 404 页面。

页面不读取任何发布数据，也不区分「不存在」「已到期」「已删除」，
避免默认页本身泄露某条发布是否存在。
"""

from string import Template
import json

STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; display: grid; place-items: center; padding: 24px;
  background: #f5f6f8; color: #171b21;
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
}
.line {
  max-width: 32ch; margin: 0; min-height: 88px;
  display: grid; place-items: center;
  font-size: 26px; font-weight: 600; letter-spacing: -.02em;
  text-align: center; text-wrap: balance;
  transition: opacity .12s ease;
}
.line.is-alt { opacity: .72; }
.dot {
  position: fixed; right: 14px; bottom: 14px;
  width: 44px; height: 44px; display: grid; place-items: center;
  padding: 0; background: none; border: 0; border-radius: 50%; cursor: pointer;
}
.dot span {
  width: 6px; height: 6px; border-radius: 50%; background: #a8b0ba;
  transition: background .15s ease, transform .15s ease;
}
.dot:hover span { background: #0d5f54; transform: scale(1.4); }
.dot:focus-visible { outline: 2px solid #0d5f54; outline-offset: 2px; }
@media (max-width: 480px) { .line { font-size: 21px; min-height: 76px; } }
@media (prefers-reduced-motion: reduce) { .line, .dot span { transition: none; } }
"""

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='7' fill='%230d5f54'/%3E"
    "%3Cpath d='M10 21V11h5.5a3.2 3.2 0 0 1 0 6.4H13' stroke='white' stroke-width='2'"
    " fill='none' stroke-linecap='round'/%3E%3C/svg%3E"
)

PAGE = Template("""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive, noimageindex">
<title>$title</title>
<link rel="icon" href="$favicon">
<style>$style</style>
</head>
<body>
<main><p class="line" id="line" aria-live="polite">$first</p></main>
<button class="dot" id="dot" type="button" aria-label="点一下试试"><span></span></button>
<script>
(function () {
  var line = document.getElementById('line');
  var dot = document.getElementById('dot');
  var first = line.textContent;
  var alt = $alt;
  var timer = null;
  dot.addEventListener('click', function () {
    if (timer) { clearTimeout(timer); }
    line.textContent = alt;
    line.classList.add('is-alt');
    timer = setTimeout(function () {
      line.textContent = first;
      line.classList.remove('is-alt');
      timer = null;
    }, 3000);
  });
})();
</script>
</body>
</html>
""")


def _page(title, first, alt):
    return PAGE.substitute(
        title=title, favicon=FAVICON, style=STYLE, first=first,
        alt=json.dumps(alt, ensure_ascii=False),
    )


LANDING = _page(
    "PageDrop · 临时发布服务",
    "这里没有目录，只有保质期。",
    "好了，这里真的什么都没有。",
)

NOT_FOUND = _page(
    "链接不可用 · PageDrop",
    "这个链接过期了，或者从未存在。",
    "好吧，它真的不在了。",
)


# 解锁页只用于仍可用的受保护发布；不可用链接继续使用 NOT_FOUND，避免泄露条目信息。
UNLOCK = Template("""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive, noimageindex">
<title>需要访问码 · PageDrop</title>
<link rel="icon" href="$favicon">
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
  background: #f5f6f8; color: #171b21;
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
}
form { width: min(100%, 320px); }
label { display: block; margin-bottom: 10px; font-size: 14px; font-weight: 600; }
input {
  width: 100%; padding: 12px 14px; border: 1px solid #cfd5db; border-radius: 10px;
  background: #fff; color: inherit; font: 500 18px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: .18em; text-transform: uppercase;
}
input:focus-visible { outline: 2px solid #0d5f54; outline-offset: 2px; border-color: #0d5f54; }
button {
  width: 100%; margin-top: 12px; padding: 11px 14px; border: 0; border-radius: 10px;
  background: #0d5f54; color: #fff; font: 600 15px/1.4 inherit; cursor: pointer;
}
button:hover { background: #0a4b42; }
button:focus-visible { outline: 2px solid #0d5f54; outline-offset: 3px; }
.hint { margin: 18px 0 0; color: #5d6873; font-size: 13px; line-height: 1.7; }
.error {
  margin: 14px 0 0; padding: 9px 12px; border-radius: 8px;
  background: #f7ece8; color: #8a4230; font-size: 13px;
}
</style>
</head>
<body>
<form method="post" action="_unlock">
  <label for="code">访问码</label>
  <input id="code" name="code" type="text" autocomplete="off" autocapitalize="characters"
         spellcheck="false" maxlength="64" required autofocus>
  <button type="submit">继续</button>
  $error
  <p class="hint">本链接需要访问码；如果不知道口令，请向分享者索取。</p>
</form>
</body>
</html>
""")


def unlock(error=""):
    message = ('<p class="error" role="alert">' + error + "</p>") if error else ""
    return UNLOCK.substitute(favicon=FAVICON, error=message)
