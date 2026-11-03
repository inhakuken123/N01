"""server.py — 网页版界面 + 账号登录。

    启动：python server.py        （或双击 启动网页.bat）
    访问：http://127.0.0.1:8765

账号：accounts.json，密码存 PBKDF2 哈希。角色 admin / user。
首次启动若没有账号，会自动建 uid00（admin）并在控制台打印一次性随机密码。
用 python accounts.py 增删账号、改密码、改角色。

记忆不按账号隔离 —— ta记得所有人的话，只在「说什么」上自己拿捏。
会话上下文（最近几条消息）也是共享一份。

接口：
    POST /api/login     {uid,password}  -> 设置会话 cookie
    POST /api/logout
    GET  /api/me
    POST /api/chat      {text}          -> SSE 流
    GET  /api/history
    GET  /api/stats
    GET  /api/admin/users               （仅管理员）
    POST /api/admin/openid             （仅管理员）
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import accounts, media
from .config import Config
from .engine import Engine
from .llm import DeepSeekClient
from .paths import BASE, WEB
from .store import Store

PORT = 8765
SESSION_TTL = 7 * 24 * 3600           # 7 天

_engine: Engine | None = None
_sessions: dict[str, dict] = {}       # sid -> {uid, role, exp}
_locks: dict[str, threading.Lock] = {}   # 每个 uid 一把，不同人互不阻塞
_locks_guard = threading.Lock()


def lock_for(uid: str) -> threading.Lock:
    """同一个人的消息要排队（保证上下文顺序），不同的人并行。"""
    key = str(uid)
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


# ---------------------------------------------------------------- 单例

def get_engine() -> Engine:
    global _engine
    if _engine is None:
        cfg = Config(BASE / "config.json")
        key = cfg.api_key_value()
        if not key:
            raise RuntimeError("没有配置 API key（在网页里填一次就行）")
        client = DeepSeekClient(key, cfg.base_url, cfg.model)
        _engine = Engine(cfg, Store(BASE), client)
        _engine.retry_pending()
        _engine.start_retry_loop(float(cfg.data.get("retry_interval", 600)))
    return _engine


QQ_CONFIG = BASE / "qqbot" / "config.json"


def setup_status() -> dict:
    """给网页判断「要不要显示初始化向导」。密钥只回「有没有」，不回内容。"""
    cfg = Config(BASE / "config.json")
    qq = {}
    if QQ_CONFIG.exists():
        try:
            qq = json.loads(QQ_CONFIG.read_text(encoding="utf-8-sig"))
        except Exception:
            qq = {}
    return {
        "needed": not bool(cfg.api_key_value() or cfg.data.get("api_key")),
        "has_key": bool(cfg.api_key_value()),
        "model": cfg.data.get("model", ""),
        "base_url": cfg.data.get("base_url", ""),
        "qq": {
            "appid": str(qq.get("appid") or ""),
            "has_secret": bool(str(qq.get("secret") or "").strip()),
            "bot_token": str(qq.get("bot_token") or ""),
        },
    }


def save_setup(d: dict) -> dict:
    """把网页填的东西写进 config.json 和 qqbot/config.json。

    只覆盖传进来的字段 —— 空字符串表示「清空这一项」，
    没传的字段保持不动，免得保存一次就把别的配置抹了。
    """
    cfg_path = BASE / "config.json"
    cfg = Config(cfg_path)

    key = str(d.get("api_key") or "").strip()
    if key:
        cfg.data["api_key"] = key
    elif d.get("clear_api_key"):
        cfg.data["api_key"] = ""

    for field in ("model", "base_url"):
        if field in d and str(d[field]).strip():
            cfg.data[field] = str(d[field]).strip()

    cfg.save()

    qq: dict = {}
    if QQ_CONFIG.exists():
        try:
            qq = json.loads(QQ_CONFIG.read_text(encoding="utf-8-sig"))
        except Exception:
            qq = {}
    changed = False
    for field in ("appid", "secret", "bot_token"):
        if field in d:
            val = str(d[field] or "").strip()
            if qq.get(field) != val:
                qq[field] = val
                changed = True
    if changed or not QQ_CONFIG.exists():
        qq.setdefault("api_base", "https://api.bot.qq.com")
        qq.setdefault("forward_url", f"http://127.0.0.1:{PORT}/api/bot/chat")
        qq.setdefault("intents", 33554432)
        qq.setdefault("group_reply", False)
        QQ_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        QQ_CONFIG.write_text(json.dumps(qq, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

    # 让新 key 立刻生效：清掉旧引擎，下次调用重建
    global _engine
    _engine = None
    return setup_status()


def test_key(d: dict) -> dict:
    """拿填进去的 key 真调一次，确认能不能用。

    报错要翻成人话 —— 直接把 HTTP 401 的原始 JSON 摊给用户，
    谁也看不出到底哪里错了。
    """
    # 粘贴时常见的问题：前后空格、换行、被 word 吃掉一半。先把空白全去掉。
    key = "".join(str(d.get("api_key") or "").split())
    cfg = Config(BASE / "config.json")
    key = key or cfg.api_key_value()
    if not key:
        return {"ok": False, "error": "还没填 key"}
    base = str(d.get("base_url") or cfg.data.get("base_url") or
               "https://api.deepseek.com").strip()
    model = str(d.get("model") or cfg.data.get("model") or "deepseek-chat").strip()
    if not key.startswith("sk-"):
        return {"ok": False, "error": f"这个不像 DeepSeek 的 key（应该以 sk- 开头，"
                                      f"你填的是 {key[:6]}…）"}
    if len(key) < 30:
        return {"ok": False, "error": f"key 太短了（{len(key)} 位），多半没复制完整"}
    try:
        client = DeepSeekClient(key, base, model, timeout=30)
        reply = client.chat([{"role": "user", "content": "回一个字：好"}],
                            temperature=0, max_tokens=8)
        return {"ok": True, "reply": (reply or "").strip()[:20]}
    except Exception as e:
        msg = str(e)
        tail = key[-4:]
        if "401" in msg or "Authentication" in msg:
            return {"ok": False,
                    "error": f"这个 key 无效（结尾是 {tail}）。"
                             f"要么复制错了，要么它已经被删掉了。"
                             f"去 platform.deepseek.com 重新建一个。"}
        if "402" in msg or "Insufficient" in msg or "balance" in msg.lower():
            return {"ok": False, "error": "连上了，但这个账号没余额了，去充值。"}
        if "404" in msg:
            return {"ok": False, "error": f"接口地址不对：{base}"}
        if "429" in msg:
            return {"ok": False, "error": "请求太频繁，等一会儿再试。"}
        if any(w in msg for w in ("连不上", "URLError", "timed out", "Timeout",
                                  "getaddrinfo", "Name or service")):
            return {"ok": False, "error": f"连不上 {base}，检查网络或代理。"}
        return {"ok": False, "error": msg[:120]}


def new_session(uid: str, role: str) -> str:
    sid = secrets.token_urlsafe(24)
    _sessions[sid] = {"uid": str(uid), "role": role,
                      "exp": time.time() + SESSION_TTL}
    return sid


def session_of(cookie_header: str | None) -> dict | None:
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "sid" and v in _sessions:
            s = _sessions[v]
            if s["exp"] < time.time():
                _sessions.pop(v, None)
                return None
            s["exp"] = time.time() + SESSION_TTL     # 滑动续期
            return {"sid": v, **s}
    return None


# ---------------------------------------------------------------- 路由

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ 工具

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200, headers: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", headers)

    MAX_JSON_BODY = 64 * 1024 * 1024      # JSON 请求体上限，超了直接拒

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if n > self.MAX_JSON_BODY:
            self._json({"error": "请求体太大"}, 413)
            raise ConnectionAbortedError("body too large")
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _me(self) -> dict | None:
        return session_of(self.headers.get("Cookie"))

    def _require(self, admin: bool = False) -> dict | None:
        me = self._me()
        if not me:
            self._json({"error": "未登录"}, 401)
            return None
        if admin and me.get("role") != "admin":
            self._json({"error": "需要管理员权限"}, 403)
            return None
        return me

    # ------------------------------------------------------------ GET

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, (WEB / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
                return

            if path.startswith("/media/"):
                # 只把 data/media 下的文件吐出去，别的路径一律拒
                if not self._require():
                    return
                rel = unquote(path[len("/media/"):])
                root = (BASE / "data" / "media").resolve()
                try:
                    f = (root / rel).resolve()
                    f.relative_to(root)
                except (ValueError, OSError):
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                if not f.is_file():
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                ext = f.suffix.lower()
                ctype = {".png": "image/png", ".webp": "image/webp",
                         ".gif": "image/gif", ".mp4": "video/mp4",
                         ".webm": "video/webm"}.get(ext, "image/jpeg")
                self._send(200, f.read_bytes(), ctype)
                return

            if path == "/api/setup":
                # 只回「有没有」，不回密钥内容，所以不用登录
                self._json(setup_status())
                return

            if path == "/api/me":
                me = self._me()
                if not me:
                    self._json({"error": "未登录"}, 401)
                    return
                acct = accounts.find(accounts.load(), me["uid"]) or {}
                self._json({"uid": me["uid"], "role": me["role"],
                            "name": acct.get("name", f"uid{me['uid']}"),
                            "relation": acct.get("relation", "")})
                return

            if path == "/api/history":
                me = self._require()
                if not me:
                    return
                self._json({"turns": _history(me["uid"])})
                return

            if path == "/api/stats":
                if not self._require():
                    return
                self._json(get_engine().stats())
                return

            if path == "/api/admin/users":
                if not self._require(admin=True):
                    return
                self._json({"users": accounts.public_list()})
                return

            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ------------------------------------------------------------ POST

    def _setup_allowed(self) -> bool:
        """能不能改设置。

        还没配 key 的时候：只允许本机改（不然谁先访问谁就能把你的 key 换掉）。
        配好之后：只有管理员能改。
        """
        if not setup_status()["has_key"]:
            return self.client_address[0] in ("127.0.0.1", "::1")
        me = self._me()
        return bool(me and me.get("role") == "admin")

    def _setup(self) -> None:
        if not self._setup_allowed():
            self._json({"error": "没有权限（配好之后只有管理员能改）"}, 403)
            return
        d = self._body()
        try:
            self._json({"ok": True, "status": save_setup(d)})
        except Exception as e:
            self._json({"error": f"保存失败：{e}"}, 500)

    def _setup_test(self) -> None:
        if not self._setup_allowed():
            self._json({"error": "没有权限"}, 403)
            return
        self._json(test_key(self._body()))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/setup":
                self._setup()
                return
            if path == "/api/setup/test":
                self._setup_test()
                return
            if path == "/api/login":
                self._login()
                return
            if path == "/api/logout":
                me = self._me()
                if me:
                    _sessions.pop(me["sid"], None)
                self._json({"ok": True},
                           headers={"Set-Cookie": "sid=; Path=/; Max-Age=0"})
                return
            if path == "/api/chat":
                self._chat()
                return

            if path == "/api/admin/users/add":
                self._admin_add()
                return
            if path == "/api/admin/users/passwd":
                self._admin_passwd()
                return
            if path == "/api/admin/users/remove":
                self._admin_remove()
                return
            if path == "/api/admin/users/role":
                self._admin_role()
                return
            if path == "/api/admin/users/relation":
                self._admin_relation()
                return
            if path == "/api/admin/users/openid":
                self._admin_openid()
                return
            if path == "/api/bot/chat":
                self._bot_chat()
                return
            if path == "/api/upload":
                self._upload()
                return
            if path == "/api/me/passwd":
                self._self_passwd()
                return

            self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    # ------------------------------------------------------------ 具体处理

    def _login(self) -> None:
        data = self._body()
        uid = str(data.get("uid") or "").strip().lstrip("u").lstrip("i").lstrip("d")
        pwd = str(data.get("password") or "")
        user = accounts.find(accounts.load(), uid)
        if not user or not accounts.verify(user, pwd):
            time.sleep(0.4)                       # 减速暴力尝试
            self._json({"error": "账号或密码不对"}, 401)
            return

        sid = new_session(uid, user.get("role", "user"))
        self._json({"uid": user["uid"], "role": user["role"],
                    "name": user.get("name", f"uid{uid}")},
                   headers={"Set-Cookie":
                            f"sid={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"})

    def _profile_for(self, user: dict) -> dict:
        """拼出这一轮对话要带的个人信息。管理员给全名单，普通用户只知道自己 + uid00。"""
        users = accounts.public_list()
        if user.get("role") == "admin":
            roster = users
        else:
            roster = [u for u in users
                      if str(u["uid"]) in (str(user["uid"]), "00")]
        return {"uid": str(user["uid"]), "name": user.get("name", ""),
                "relation": user.get("relation", ""),
                "roster": roster, "all_people": users}

    def _bot_chat(self) -> None:
        """给 QQ 官方机器人用的接口。

        识别人靠 openid（单聊事件里的 author.user_openid），不是 QQ 号。
        也接受 union_openid。返回整段回复，不流式。
        """
        d = self._body()
        openid = str(d.get("openid") or d.get("union_openid") or "").strip()
        text = str(d.get("text") or "").strip()
        has_img = bool(d.get("images"))
        has_vid = bool(d.get("video_path"))
        if not openid:
            return self._json({"error": "需要 openid"}, 400)
        # 视频也要算「有内容」—— QQ 发视频时没有文字，
        # 只看 text/images 会把整条消息打回 400。
        if not text and not has_img and not has_vid:
            return self._json({"error": "需要 text、图片或视频"}, 400)

        user = accounts.find_by_openid(openid)
        if not user:
            return self._json({"error": f"openid {openid} 还没绑定账号",
                               "openid": openid}, 404)

        eng = get_engine()
        token = str(eng.cfg.data.get("bot_token") or "")
        # 本机来的（qqbot 就在本机跑）放行；外部必须带对 token。
        # 以前 token 为空时整段校验直接跳过，等于谁都能冒充任意 openid。
        local = self.client_address[0] in ("127.0.0.1", "::1", "localhost")
        if not local:
            if not token:
                return self._json({"error": "没设置 bot_token，外部不能调这个接口"}, 403)
            if self.headers.get("X-Bot-Token") != token:
                return self._json({"error": "token 不对"}, 401)

        profile = self._profile_for(user)
        urls, metas = self._images(d, str(user["uid"]))
        vurls, vmetas = self._videos(d, str(user["uid"]))
        urls += vurls
        metas += vmetas
        with lock_for(user["uid"]):
            reply = "".join(eng.chat_stream(text, uid=str(user["uid"]),
                                            profile=profile,
                                            images=urls, image_meta=metas))
        self._json({"uid": str(user["uid"]), "name": user.get("name", ""),
                    "reply": reply})

    def _images(self, d: dict, tag: str) -> tuple[list[str], list[dict]]:
        """收下客户端传来的图片：压缩 → 落盘 → 返回 (data_url, 元数据)。

        压不压由 media.prepare 决定：≤1MB 且分辨率没超就原样不动。
        """
        raw = d.get("images") or []
        if not isinstance(raw, list):
            return [], []
        urls, metas = [], []
        for item in raw[:2]:                    # 一轮最多两张
            try:
                prepared = media.prepare(str(item))
                rel = media.save(BASE, prepared, tag)
            except Exception as e:
                print(f"[media] 图片被跳过：{e}", flush=True)
                continue
            print(f"[media] {media.describe(prepared)} → {rel}", flush=True)
            urls.append(prepared["data_url"])
            metas.append({
                "type": "image",
                "path": rel,
                "w": prepared["w"], "h": prepared["h"],
                "bytes": len(prepared["bytes"]),
                "orig_bytes": prepared["orig_bytes"],
                "orig_w": prepared["orig_w"], "orig_h": prepared["orig_h"],
                "compressed": prepared["compressed"],
                "why": prepared["why"],
            })
        return urls, metas

    def _videos(self, d: dict, tag: str) -> tuple[list[str], list[dict]]:
        """收视频：抽帧 → 帧当图片发给模型。

        两种来源：
          · video_path：已经在 data/media/ 里的文件（机器人走这条，1GB 也不吃内存）
          · videos：base64（网页小文件兜底）

        没有 ffmpeg 时不抽帧，只留一条「看不清内容」的记录，不阻塞对话。
        """
        frames: list[str] = []
        metas: list[dict] = []

        rels = [str(x) for x in (d.get("video_path") or [])][:1]
        media_root = (BASE / "data" / "media").resolve()
        for rel in rels:
            p = (BASE / rel).resolve()
            # rel 是客户端给的，必须落在 data/media 里 —— 不然 ../../ 能指到任何文件
            try:
                p.relative_to(media_root)
            except ValueError:
                print(f"[media] 拒绝越界路径：{rel}", flush=True)
                metas.append({"type": "video", "path": rel,
                              "error": "路径不对", "frames": []})
                continue
            if not p.is_file():
                print(f"[media] 找不到视频：{rel}", flush=True)
                # 也要留一条记录：不然ta完全不知道对方发过视频，
                # 只会当成「对方什么也没说」。
                metas.append({"type": "video", "path": rel,
                              "error": "文件不见了", "frames": []})
                continue
            got = media.video_frames_file(p)
            if not got.get("ok"):
                print(f"[media] 视频处理失败：{got.get('error')}", flush=True)
                metas.append({"type": "video", "path": rel,
                              "error": got.get("error"), "frames": []})
                continue
            frame_paths = []
            for b in got["frames"]:
                it = media.prepare(media.to_data_url("image/jpeg", b))
                frame_paths.append(media.save(BASE, it, tag))
                frames.append(it["data_url"])
            print(f"[media] 视频 {got['dur']:.1f}s {got['w']}×{got['h']} "
                  f"→ {len(frame_paths)} 帧 → {rel}", flush=True)
            metas.append({"type": "video", "path": rel,
                          "dur": round(got["dur"], 1), "w": got["w"], "h": got["h"],
                          "bytes": p.stat().st_size, "frames": frame_paths})

        raw = d.get("videos") or []
        if isinstance(raw, list):
            for item in raw[:1]:                # base64 兜底，只支持小文件
                got = media.prepare_video(str(item))
                if got.get("raw"):
                    ext = {"video/quicktime": "mov", "video/webm": "webm"}.get(
                        got.get("mime", ""), "mp4")
                    rel = media.save_raw(BASE, got["raw"], ext, tag)
                else:
                    rel = ""
                if not got.get("ok"):
                    print(f"[media] 视频处理失败：{got.get('error')}", flush=True)
                    metas.append({"type": "video", "path": rel,
                                  "error": got.get("error"), "frames": []})
                    continue
                fp = []
                for b in got["frame_bytes"]:
                    it = media.prepare(media.to_data_url("image/jpeg", b))
                    fp.append(media.save(BASE, it, tag))
                    frames.append(it["data_url"])
                metas.append({"type": "video", "path": rel,
                              "dur": round(got["dur"], 1), "w": got["w"],
                              "h": got["h"], "bytes": len(got["raw"]), "frames": fp})
        return frames, metas

    def _upload(self) -> None:
        """流式收一个视频/大文件，边收边写盘 —— 不经过 base64，不吃内存。

        谁可以调：已登录用户，或者带对 bot_token 的机器人，或者本机。
        """
        eng = get_engine()
        token = str(eng.cfg.data.get("bot_token") or "")
        ok = bool(self._me()) or self.client_address[0] in ("127.0.0.1", "::1")
        if not ok and token and self.headers.get("X-Bot-Token") == token:
            ok = True
        if not ok:
            return self._json({"error": "没有权限"}, 401)

        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "空文件"}, 400)
        if n > media.MAX_VIDEO:
            return self._json({"error": f"文件太大（上限 "
                               f"{media.MAX_VIDEO // 1024 // 1024}MB）"}, 413)

        name = str(self.headers.get("X-Filename") or "video.mp4")
        ext = "".join(c for c in name.rsplit(".", 1)[-1].lower() if c.isalnum())[:5] or "mp4"
        dest, rel = media.new_path(BASE, ext)

        left = n
        try:
            with dest.open("wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
        except Exception as e:
            dest.unlink(missing_ok=True)
            return self._json({"error": f"写入失败：{e}"}, 500)

        print(f"[media] 收到上传 {name} {(n - left) // 1024}KB → {rel}", flush=True)
        self._json({"path": rel, "bytes": n - left})

    def _chat(self) -> None:
        me = self._require()
        if not me:
            return
        d = self._body()
        text = (d.get("text") or "").strip()
        urls, metas = self._images(d, me["uid"])
        vurls, vmetas = self._videos(d, me["uid"])
        urls += vurls
        metas += vmetas
        if not text and not urls and not vmetas:
            self._json({"error": "empty"}, 400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            eng = get_engine()
            acct = accounts.find(accounts.load(), me["uid"]) or {}
            profile = self._profile_for(acct or {"uid": me["uid"], "role": me["role"]})
            with lock_for(me["uid"]):              # 只锁自己这一条对话
                for chunk in eng.chat_stream(
                        text, uid=me["uid"], profile=profile,
                        on_tool=lambda q: self._sse({"tool": q}),
                        images=urls, image_meta=metas):
                    self._sse({"t": chunk})
            self._sse({"done": True})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._sse({"error": str(e)})
            except Exception:
                pass

    def _sse(self, obj: dict) -> None:
        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")
                         .encode("utf-8"))
        self.wfile.flush()

    # ------------------------------------------------------------ 账号管理

    @staticmethod
    def _norm_uid(v) -> str:
        s = str(v or "").strip().lower()
        if s.startswith("uid"):
            s = s[3:]
        return s.strip()

    def _users(self) -> list[dict]:
        return accounts.load()["users"]

    def _admin_count(self) -> int:
        return sum(1 for u in self._users() if u.get("role") == "admin")

    def _admin_add(self) -> None:
        if not self._require(admin=True):
            return
        d = self._body()
        uid = self._norm_uid(d.get("uid"))
        name = str(d.get("name") or "").strip() or f"uid{uid}"
        role = str(d.get("role") or "user")
        relation = str(d.get("relation") or "").strip()
        openid = str(d.get("openid") or "").strip()
        union_openid = str(d.get("union_openid") or "").strip()
        pwd = str(d.get("password") or "")

        if not uid:
            return self._json({"error": "uid 不能为空"}, 400)
        if len(pwd) < 4:
            return self._json({"error": "密码至少 4 位"}, 400)
        if role not in accounts.ROLES:
            return self._json({"error": "角色只能是 admin 或 user"}, 400)
        if accounts.find(accounts.load(), uid):
            return self._json({"error": f"uid{uid} 已存在"}, 400)
        if openid and accounts.find_by_openid(openid):
            return self._json({"error": "这个 openid 已经绑给别的账号了"}, 400)

        accounts.add(uid, name, role, pwd, relation, openid, union_openid)
        self._json({"ok": True, "users": accounts.public_list()})

    def _admin_openid(self) -> None:
        if not self._require(admin=True):
            return
        d = self._body()
        uid = self._norm_uid(d.get("uid"))
        openid = str(d.get("openid") or "").strip()
        union_openid = str(d.get("union_openid") or "").strip()
        if not accounts.find(accounts.load(), uid):
            return self._json({"error": f"没有 uid{uid}"}, 404)
        other = accounts.find_by_openid(openid) if openid else None
        if other and str(other["uid"]) != str(uid):
            return self._json({"error": f"这个 openid 已经绑给 uid{other['uid']}"}, 400)
        accounts.set_openid(uid, openid, union_openid)
        self._json({"ok": True, "users": accounts.public_list()})

    def _admin_relation(self) -> None:
        if not self._require(admin=True):
            return
        d = self._body()
        uid = self._norm_uid(d.get("uid"))
        if not accounts.find(accounts.load(), uid):
            return self._json({"error": f"没有 uid{uid}"}, 404)
        accounts.set_relation(uid, str(d.get("relation") or ""))
        self._json({"ok": True, "users": accounts.public_list()})

    def _admin_passwd(self) -> None:
        if not self._require(admin=True):
            return
        d = self._body()
        uid = self._norm_uid(d.get("uid"))
        pwd = str(d.get("password") or "")
        if len(pwd) < 4:
            return self._json({"error": "密码至少 4 位"}, 400)
        if not accounts.set_password(uid, pwd):
            return self._json({"error": f"没有 uid{uid}"}, 404)
        self._json({"ok": True})

    def _admin_remove(self) -> None:
        me = self._require(admin=True)
        if not me:
            return
        uid = self._norm_uid(self._body().get("uid"))
        user = accounts.find(accounts.load(), uid)
        if not user:
            return self._json({"error": f"没有 uid{uid}"}, 404)
        if uid == str(me["uid"]):
            return self._json({"error": "不能删除自己"}, 400)
        if user.get("role") == "admin" and self._admin_count() <= 1:
            return self._json({"error": "至少要留一个管理员"}, 400)

        accounts.remove(uid)
        for sid, s in list(_sessions.items()):      # 踢掉他的会话
            if str(s["uid"]) == uid:
                _sessions.pop(sid, None)
        self._json({"ok": True, "users": accounts.public_list()})

    def _admin_role(self) -> None:
        if not self._require(admin=True):
            return
        d = self._body()
        uid = self._norm_uid(d.get("uid"))
        role = str(d.get("role") or "")
        user = accounts.find(accounts.load(), uid)
        if not user:
            return self._json({"error": f"没有 uid{uid}"}, 404)
        if role not in accounts.ROLES:
            return self._json({"error": "角色只能是 admin 或 user"}, 400)
        if (user.get("role") == "admin" and role == "user"
                and self._admin_count() <= 1):
            return self._json({"error": "至少要留一个管理员"}, 400)

        accounts.set_role(uid, role)
        self._json({"ok": True, "users": accounts.public_list()})

    def _self_passwd(self) -> None:
        me = self._require()
        if not me:
            return
        pwd = str(self._body().get("password") or "")
        if len(pwd) < 4:
            return self._json({"error": "密码至少 4 位"}, 400)
        accounts.set_password(me["uid"], pwd)
        self._json({"ok": True})


def _history(uid: str, limit: int = 40) -> list[dict]:
    """只返回这个 uid 自己的对话。

    记忆检索仍然是全局的 —— ta记得所有人的话，
    但界面上不把别人的聊天记录摊出来。
    """
    try:
        eng = get_engine()
        mine = [t for t in eng.store.turns() if str(t["uid"]) == str(uid)]
        return [{"ts": t["ts"], "uid": t["uid"], "user": t["user"],
                 "reply": t["reply"],
                 "media": [{"type": m.get("type"), "path": m.get("path")}
                           for m in (t.get("media") or []) if m.get("path")]}
                for t in mine[-limit:]]
    except Exception:
        return []


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    port = PORT
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)

    pwd = accounts.seed_if_empty("00", "管理员")
    if pwd:
        print("=" * 52)
        print("  首次启动，已创建管理员账号")
        print("    uid      00")
        print(f"    密码     {pwd}")
        print("  这个密码只显示这一次，请立刻记下。")
        print("  改密码：python accounts.py passwd --uid 00")
        print("=" * 52)

    # 先看端口是不是已经有人在服务。
    # Windows 上 SO_REUSEADDR 不冲突：两个进程能同时 bind 同一端口，
    # 请求随机落到其中一个 —— 表现就是「一会儿能连一会儿 Failed to fetch」。
    def _port_busy(p: int) -> bool:
        import socket as _s
        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as sk:
            sk.settimeout(0.4)
            return sk.connect_ex(("127.0.0.1", p)) == 0

    if _port_busy(port):
        print()
        print(f"[错误] 端口 {port} 已经有一个实例在跑了。")
        print("  要么关掉那个窗口，要么换个端口：python run.py 8766")
        print()
        raise SystemExit(1)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print()
        print(f"[错误] 端口 {port} 起不来：{e}")
        print("  要么关掉那个窗口，要么换个端口：python run.py 8766")
        print()
        raise SystemExit(1)
    url = f"http://127.0.0.1:{port}"
    print(f"N-01   {url}")
    print("Ctrl+C 停止（关掉这个窗口也会停）")

    if "--no-browser" not in sys.argv:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
        if _engine:
            _engine.close()


if __name__ == "__main__":
    main()
