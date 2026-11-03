"""main.py — QQ 官方机器人 <-> AI 的桥。

它只做一件事：QQ 里有人跟ta说话，就把话转给本地 AI，再把回复发回去。

    python qqbot/main.py

流程：
    ① AppID + Secret 换 access_token（7200 秒，提前一分钟续）
    ② GET /gateway/bot 拿 wss 地址
    ③ 连上去，op=2 发 identify，intent = GROUP_AND_C2C_EVENT (1<<25)
    ④ 收 C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE
    ⑤ 转发到 127.0.0.1:8765/api/bot/chat，拿回复
    ⑥ 调 /v2/users/{openid}/messages 发回去

凭据放在 qqbot/config.json，不要贴在聊天里。
"""

from __future__ import annotations

import base64
import http.client
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ws import WSClient, WSError  # noqa: E402

CONFIG = HERE / "config.json"
STATE = HERE / "state.json"

INTENT_GROUP_AND_C2C = 1 << 25      # 群聊 + 单聊事件

# 收消息的主循环要保持轻（它还得发心跳），回复交给线程池
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="reply")
_state_lock = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(key: str) -> threading.Lock:
    """同一个人的消息按顺序处理，不同的人并行。"""
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    if not CONFIG.exists():
        raise SystemExit(
            f"缺少 {CONFIG}\n"
            '内容形如：\n'
            '{\n'
            '  "appid": "你的AppID",\n'
            '  "secret": "你的ClientSecret",\n'
            '  "forward_url": "http://127.0.0.1:8765/api/bot/chat",\n'
            '  "bot_token": ""\n'
            '}')
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    for k in ("appid", "secret"):
        if not str(cfg.get(k) or "").strip():
            raise SystemExit(f"config.json 里缺 {k}")
    return cfg


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- HTTP

def http_json(method: str, url: str, body: dict | None = None,
              headers: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} {url} -> {raw[:200]}") from e


class QQ:
    """token 管理 + 发消息。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = str(cfg.get("api_base") or "https://api.bot.qq.com").rstrip("/")
        self._token = ""
        self._expire_at = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expire_at - 60:
            return self._token
        r = http_json("POST", self.base + "/app/getAppAccessToken",
                      {"appId": self.cfg["appid"],
                       "clientSecret": self.cfg["secret"]})
        tok = r.get("access_token")
        if not tok:
            raise RuntimeError(f"取 token 失败：{r}")
        self._token = tok
        self._expire_at = time.time() + float(r.get("expires_in") or 7200)
        log(f"拿到 access_token，有效 {int(self._expire_at - time.time())} 秒")
        return tok

    def gateway(self) -> str:
        r = http_json("GET", self.base + "/gateway/bot",
                      headers={"Authorization": f"QQBot {self.token()}"})
        url = r.get("url")
        if not url:
            raise RuntimeError(f"取网关失败：{r}")
        return url

    def send_c2c(self, openid: str, content: str, msg_id: str | None = None,
                 msg_seq: int = 1) -> dict:
        body = {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq
        return http_json("POST", f"{self.base}/v2/users/{openid}/messages",
                         body, {"Authorization": f"QQBot {self.token()}"})

    def send_group(self, group_openid: str, content: str,
                   msg_id: str | None = None, msg_seq: int = 1) -> dict:
        body = {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq
        return http_json("POST", f"{self.base}/v2/groups/{group_openid}/messages",
                         body, {"Authorization": f"QQBot {self.token()}"})


# ---------------------------------------------------------------- 图片

MAX_IMG = 4 * 1024 * 1024


def collect_images(d: dict) -> list[str]:
    """把事件里的图片下载下来，转成 data URL。

    QQ 给的 url 会过期，所以收到就得立刻下。
    content_type 可能是 image/jpeg，也可能只是 image —— 都认。
    """
    out: list[str] = []
    atts = d.get("attachments") or []
    if atts and not any(str(a.get("content_type") or "").lower().startswith("image")
                        for a in atts):
        # 有附件但不是图片：打出来，方便以后支持别的类型
        kinds = [a.get("content_type") for a in atts]
        log(f"  附件不是图片，先忽略：{kinds}")

    for att in atts[:2]:
        ct = str(att.get("content_type") or "").lower()
        url = str(att.get("url") or "")
        if not url or not ct.startswith("image"):
            continue
        if "/" not in ct:
            ct = "image/jpeg"
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                raw = r.read(MAX_IMG + 1)
            if len(raw) > MAX_IMG:
                log(f"  图片太大，跳过（{len(raw) // 1024}KB）")
                continue
            out.append(f"data:{ct};base64," + base64.b64encode(raw).decode())
            log(f"  收到图片 {len(raw) // 1024}KB")
        except Exception as e:
            log(f"  图片下载失败：{e}")
    return out


MAX_VIDEO_BYTES = 1024 * 1024 * 1024     # 1GB


def download_video(url: str) -> "Path | None":
    """把视频边下载边写盘，不整个读进内存 —— 1GB 也扛得住。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="n01dl_"))
    dest = tmpdir / "v.mp4"
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=120) as r, dest.open("wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise RuntimeError("超过 1GB")
                f.write(chunk)
        log(f"  收到视频 {total // 1024}KB")
        return dest
    except Exception as e:
        log(f"  视频下载失败：{e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


def upload_video(cfg: dict, path: Path) -> str | None:
    """把下载下来的视频流式传给 AI，拿回它在 data/media/ 里的相对路径。"""
    target = str(cfg.get("forward_url") or "http://127.0.0.1:8765/api/bot/chat")
    u = urlparse(target)
    size = path.stat().st_size
    conn = http.client.HTTPConnection(u.hostname or "127.0.0.1", u.port or 80,
                                      timeout=1800)
    try:
        conn.putrequest("POST", "/api/upload")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.putheader("X-Filename", path.name)
        if cfg.get("bot_token"):
            conn.putheader("X-Bot-Token", str(cfg["bot_token"]))
        conn.endheaders()
        with path.open("rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                conn.send(b)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode() or "{}")
        if resp.status != 200 or not data.get("path"):
            log(f"  上传失败：HTTP {resp.status} {data}")
            return None
        return str(data["path"])
    except Exception as e:
        log(f"  上传失败：{e}")
        return None
    finally:
        conn.close()


def collect_videos(cfg: dict, d: dict) -> list[str]:
    """把事件里的视频下载到临时文件，再流式上传，返回相对路径列表。"""
    out: list[str] = []
    for att in (d.get("attachments") or [])[:1]:
        ct = str(att.get("content_type") or "").lower()
        url = str(att.get("url") or "")
        if not url or not ct.startswith("video"):
            continue
        tmp = download_video(url)
        if not tmp:
            continue
        try:
            rel = upload_video(cfg, tmp)
            if rel:
                out.append(rel)
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
    return out

# ---------------------------------------------------------------- 转发

def forward(cfg: dict, openid: str, text: str,
            images: list[str] | None = None,
            video_paths: list[str] | None = None) -> str:
    """把这句转给本地 AI，拿ta的回复。

    刚启动时网页服务可能还没就绪，连接被拒就重试几次，别直接报错。
    """
    headers = {}
    if cfg.get("bot_token"):
        headers["X-Bot-Token"] = str(cfg["bot_token"])
    url = str(cfg.get("forward_url") or "http://127.0.0.1:8765/api/bot/chat")
    payload: dict = {"openid": openid, "text": text}
    if images:
        payload["images"] = images
    if video_paths:
        payload["video_path"] = video_paths

    last = None
    for attempt in range(4):
        try:
            r = http_json("POST", url, payload, headers, timeout=180)
            if "reply" in r:
                return str(r["reply"])
            raise RuntimeError(f"AI 返回异常：{r}")
        except RuntimeError as e:
            last = e
            msg = str(e)
            # 4xx 是「这条请求本身有问题」，重试不会变好 —— 直接报出来，
            # 省得日志里刷三行一样的错。只有连不上/服务没起来才值得重试。
            if any(code in msg for code in ("404", "400", "401", "403")) or attempt == 3:
                raise
            log(f"  转发重试 {attempt + 1}/3（{e}）")
            time.sleep(1.5)
    raise last or RuntimeError("转发失败")


# ---------------------------------------------------------------- 工具

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def split_text(text: str, limit: int = 1200) -> list[str]:
    """QQ 单条消息有长度限制，太长的切开。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            out.append(buf)
            buf = ""
        buf += line
        while len(buf) > limit:
            out.append(buf[:limit])
            buf = buf[limit:]
    if buf:
        out.append(buf)
    return out


# ---------------------------------------------------------------- 主循环

def dispatch(qq: QQ, cfg: dict, st: dict, event: dict) -> None:
    """在收消息的主循环里快速跑完：判断类型、去重，然后把慢活丢给线程池。

    主循环必须保持轻，否则心跳会被ta那几十秒的回复卡住。
    """
    t = event.get("t")
    d = event.get("d") or {}
    if t in ("READY", "RESUMED"):
        return
    if t not in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
        log(f"  （这个事件我不处理：{t}）")
        return

    msg_id = str(d.get("id") or "")
    with _state_lock:
        seen = st.setdefault("seen", [])
        if msg_id and msg_id in seen:
            return                          # 官方会重复推送，去重
        if msg_id:
            seen.append(msg_id)
            del seen[:-500]
            save_state(st)

    _pool.submit(handle_event, qq, cfg, st, event)


def handle_event(qq: QQ, cfg: dict, st: dict, event: dict) -> None:
    """真正干活的：转发给 AI，再把回复发回 QQ。跑在线程池里。"""
    t = event.get("t")
    d = event.get("d") or {}
    content = str(d.get("content") or "").strip()
    author = d.get("author") or {}
    images = collect_images(d)
    videos = collect_videos(cfg, d)
    if not content and not images and not videos:
        return

    msg_id = str(d.get("id") or "")

    if t == "C2C_MESSAGE_CREATE":
        openid = str(author.get("user_openid") or "")
        if not openid:
            return
        who = f"单聊 {openid[:10]}…"
        send = lambda text, seq=1: qq.send_c2c(openid, text, msg_id, seq)   # noqa: E731
    else:
        openid = str(author.get("member_openid") or "")
        group = str(d.get("group_openid") or "")
        if not openid or not group:
            return
        who = f"群 {group[:10]}… / {openid[:10]}…"
        send = lambda text, seq=1: qq.send_group(group, text, msg_id, seq)  # noqa: E731
        if not cfg.get("group_reply", True):
            return

    log(f"{who}：{content[:40] or '（图片）'}")

    with lock_for(openid):                  # 同一个人连发两条，仍然按顺序
        try:
            reply = forward(cfg, openid, content, images, videos)
        except Exception as e:
            log(f"  转发失败：{e}")
            note = "（ta好像没在。等一下再试。）"
            if "404" in str(e):
                note = "（我还不认识你。让管理员把这个 openid 登记一下。）"
                log(f"  这个 openid 还没绑账号，复制它去管理面板绑定：")
                log(f"  {openid}")
            try:
                send(note)
            except Exception:
                pass
            return

        parts = split_text(reply)
        for i, part in enumerate(parts):
            try:
                send(part, i + 1)
            except Exception as e:
                log(f"  回消息失败（第 {i + 1} 段）：{e}")
                break
        log(f"  回了 {len(parts)} 段，共 {len(reply)} 字")


def run() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cfg = load_config()
    st = load_state()
    qq = QQ(cfg)
    intents = int(cfg.get("intents") or INTENT_GROUP_AND_C2C)

    backoff = 3
    while True:
        ws = None
        try:
            url = qq.gateway()
            log(f"连网关 {url}")
            ws = WSClient(url)
            ws.connect()

            # 等 hello
            hb = 30.0
            deadline = time.time() + 15
            while time.time() < deadline:
                text = ws.recv_text(timeout=3)
                if not text:
                    continue
                msg = json.loads(text)
                if msg.get("op") == 10:
                    hb = float((msg.get("d") or {}).get("heartbeat_interval") or 30000) / 1000
                    break

            ws.send_text(json.dumps({
                "op": 2,
                "d": {"token": f"QQBot {qq.token()}", "intents": intents,
                      "shard": [0, 1],
                      "properties": {"$os": "windows", "$browser": "n01"}},
            }, ensure_ascii=False))
            log(f"已 identify，心跳 {hb:.0f} 秒")

            last_hb = time.time()
            seq = 0
            backoff = 3

            while True:
                text = ws.recv_text(timeout=1.0)
                if text:
                    msg = json.loads(text)
                    op = msg.get("op")
                    if op == 0:
                        seq = msg.get("s") or seq
                        t = msg.get("t") or "?"
                        if t in ("READY", "RESUMED"):
                            log(f"会话就绪：{t}")
                        else:
                            # 所有事件都打出来，认不出来的也打，方便排查
                            brief = json.dumps(msg.get("d"), ensure_ascii=False)
                            log(f"事件 {t}  {brief[:160]}")
                        dispatch(qq, cfg, st, msg)
                    elif op == 11:
                        pass                      # 心跳确认
                    elif op in (7, 9):
                        log("服务器要求重连")
                        raise WSError("reconnect")
                    else:
                        log(f"收到 op={op}：{str(text)[:120]}")
                if time.time() - last_hb >= hb:
                    ws.send_text(json.dumps({"op": 1, "d": seq}))
                    last_hb = time.time()

        except KeyboardInterrupt:
            log("手动停止")
            break
        except Exception as e:
            log(f"断开：{e}，{backoff} 秒后重连")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if ws:
                ws.close()


if __name__ == "__main__":
    run()
