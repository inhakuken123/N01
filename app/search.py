"""search.py — 本机直接抓 Bing 的结果页，不依赖任何 API key。

两件事：
  · search(query)     给一个短查询词，返回几条「标题 + 摘要 + 来源 + 多久之前」
  · fetch_text(url)   打开某一条结果，读正文（取前几千字）

几个刻意的选择：
  · 只抓 Bing —— DuckDuckGo 这边连不上，百度页面太大且结构常变
  · 不发完整 URL 给模型，只给域名。ta不贴链接，但要知道出处
  · 打开页面按「第几条」而不是 URL —— 模型编地址的概率远高于编序号
  · 请求间隔至少 1 秒，触发限流时会被当成「没查到」
  · 解析不出来就返回空，绝不返回半截垃圾
"""

from __future__ import annotations

import base64
import html
import ipaddress
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

ENDPOINT = "https://www.bing.com/search?q={q}&setlang=zh-CN&count=10"
BLOCK = re.compile(r'<li class="b_algo".*?</li>', re.S)
TITLE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
SNIP = re.compile(r'<p[^>]*>(.*?)</p>', re.S)

MAX_SNIPPET = 140          # 每条摘要截到这么长
MIN_GAP = 1.0              # 两次请求之间至少隔这么久

_lock = threading.Lock()
_last = [0.0]


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _real_url(href: str) -> str:
    """Bing 的链接常包一层 /ck/a?...&u=a1<base64>，解开它。"""
    if "bing.com/ck/a" not in href:
        return href
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        u = (qs.get("u") or [""])[0]
        if u.startswith("a1"):
            u = u[2:]
        pad = "=" * (-len(u) % 4)
        return base64.urlsafe_b64decode(u + pad).decode("utf-8", "replace")
    except Exception:
        return href


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _fetch(query: str, timeout: float = 10.0) -> str:
    url = ENDPOINT.format(q=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(900_000).decode("utf-8", "replace")


def search(query: str, n: int = 5) -> list[dict]:
    """查一次。查不到返回 []，调用方据此告诉模型「没查到」。"""
    query = (query or "").strip()
    if not query:
        return []

    with _lock:                      # 别把自己打成爬虫
        gap = time.time() - _last[0]
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
        _last[0] = time.time()

    body = ""
    for attempt in range(2):
        try:
            body = _fetch(query)
            break
        except Exception:
            if attempt == 1:
                return []
            time.sleep(1.5)

    rows = []
    for block in BLOCK.findall(body)[: n + 3]:
        m = TITLE.search(block)
        if not m:
            continue
        url = _real_url(m.group(1))
        title = _clean(m.group(2))
        if not title:
            continue

        snippet = ""
        p = SNIP.search(block)
        if p:
            snippet = _clean(p.group(1))

        age = ""
        am = re.match(r"^(\d+\s*(?:秒|分钟|小时|天|周|个月|年)\s*之前)\s*[·\-—]?\s*", snippet)
        if am:
            age = am.group(1).replace(" ", "")
            snippet = snippet[am.end():]

        rows.append({
            "title": title[:80],
            "snippet": snippet[:MAX_SNIPPET],
            "domain": _domain(url),
            "age": age,
            "url": url,
        })
        if len(rows) >= n:
            break
    return rows


def as_block(rows: list[dict]) -> str:
    """给模型看的格式：短、带出处、带时间。"""
    if not rows:
        return ""
    out = []
    for i, r in enumerate(rows, 1):
        bits = [x for x in (r.get("domain"), r.get("age")) if x]
        head = f"[{i}] {r['title']}" + (f" · {' · '.join(bits)}" if bits else "")
        out.append(head)
        if r.get("snippet"):
            out.append(f"    {r['snippet']}")
    return "\n".join(out)


# ---------------------------------------------------------------- 打开页面

MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_PAGE_CHARS = 2500          # 给模型看的正文上限，再多就是烧钱
DROP_TAGS = re.compile(r"<(script|style|noscript|svg|iframe|head)[^>]*>.*?</\1>",
                       re.S | re.I)
COMMENT = re.compile(r"<!--.*?-->", re.S)
BLOCK_TAG = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|header|footer)[^>]*>",
                       re.I)
TAG = re.compile(r"<[^>]+>")


def _safe_url(url: str) -> str | None:
    """挡掉本机地址和奇怪协议 —— 模型挑的 URL 不能让它去摸内网。"""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                return None
    except Exception:
        return None
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """跳转前重新校验目标地址。

    urllib 默认会自动跟随 302，而 _safe_url 只查了最初的 URL ——
    公网地址 302 到 127.0.0.1 就能把内网读出来。这里每一跳都过一遍。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _safe_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "跳转到不允许的地址", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_text(url: str, max_chars: int = MAX_PAGE_CHARS) -> dict | None:
    """打开一个页面，扒出正文。返回 {'title','text','chars'} 或 None。"""
    safe = _safe_url(url)
    if not safe:
        return None
    try:
        req = urllib.request.Request(safe, headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
        })
        # 用自定义 opener：每一跳都重新校验，别让 302 把请求带到内网去。
        # （默认的 urlopen 会自动跟随跳转，_safe_url 只管第一个地址。）
        opener = urllib.request.build_opener(_SafeRedirect)
        with opener.open(req, timeout=12) as r:
            final = r.geturl()
            if not _safe_url(final):
                return None
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and not any(t in ctype for t in ("html", "text/plain", "xml")):
                return None
            raw = r.read(MAX_PAGE_BYTES)
    except Exception:
        return None

    for enc in ("utf-8", "gb18030"):
        try:
            body = raw.decode(enc)
            break
        except UnicodeDecodeError:
            body = ""

    title = ""
    tm = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    if tm:
        title = _clean(tm.group(1))[:80]

    text = COMMENT.sub(" ", body)
    text = DROP_TAGS.sub(" ", text)
    text = BLOCK_TAG.sub("\n", text)
    text = TAG.sub(" ", text)
    text = html.unescape(text)

    lines = []
    seen = set()
    for line in text.splitlines():
        s = re.sub(r"\s+", " ", line).strip()
        if len(s) < 4 or s in seen:
            continue
        seen.add(s)
        lines.append(s)
        if sum(len(x) for x in lines) > max_chars * 2:
            break

    out = "\n".join(lines)[:max_chars].strip()
    if len(out) < 50:
        return None
    return {"title": title, "text": out, "chars": len(out)}
