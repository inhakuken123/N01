"""where.py — 猜一下这台机器现在从哪儿上网。

只做一件事：拿公网 IP → 换成一个城市名，缓存起来。
不给模型看 IP —— 对ta没意义，还会被念出来、被存进记忆。

设计：
  · 结果缓存在 data/state.json 的 "where" 字段里，进程重启也算数
  · 过期（默认 30 分钟）时**后台刷新**，当前这轮先用旧值 —— 不能为了查位置让对话等半秒
  · 一次都没有过（第一次启动）才同步查一次
  · 查不到就返回 None，提示词里那段直接不出现，不影响其它
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone

API = ("http://ip-api.com/json/?lang=zh-CN"
       "&fields=status,country,regionName,city,timezone,query")


def _fetch(timeout: float = 6.0) -> dict | None:
    try:
        with urllib.request.urlopen(API, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if d.get("status") != "success":
            return None
        return {
            "country": d.get("country") or "",
            "region": d.get("regionName") or "",
            "city": d.get("city") or "",
            "tz": d.get("timezone") or "",
            "ip": d.get("query") or "",
            "ts": time.time(),
        }
    except Exception:
        return None


def pretty(w: dict | None) -> str:
    """给人看的一句：市 + 区，去重。"""
    if not w:
        return ""
    parts = []
    for k in ("country", "region", "city"):
        v = (w.get(k) or "").strip()
        if v and v not in parts:
            parts.append(v)
    return " ".join(parts[-2:]) if parts else ""


class Geo:
    def __init__(self, store, ttl: float = 1800.0):
        self.store = store
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._busy = False
        self._value: dict | None = None
        self._load()

    # ------------------------------------------------------------ 存取

    def _load(self) -> None:
        try:
            w = self.store.state().get("where")
            if isinstance(w, dict) and w.get("city"):
                self._value = w
        except Exception:
            self._value = None

    def _save(self, w: dict) -> None:
        try:
            st = self.store.state()
            st["where"] = w
            self.store.save_state(st)
        except Exception:
            pass

    def _stale(self, w: dict | None) -> bool:
        if not w:
            return True
        try:
            return (time.time() - float(w.get("ts") or 0)) > self.ttl
        except (TypeError, ValueError):
            return True

    # ------------------------------------------------------------ 对外

    def get(self) -> dict | None:
        """当前最好的一份。过期就在后台刷新，这一轮不等。"""
        w = self._value
        if self._stale(w):
            self._kick()
        return w

    def _kick(self) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True

        def run() -> None:
            try:
                got = _fetch()
                if got:
                    self._value = got
                    self._save(got)
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=run, daemon=True, name="geo").start()

    def block_until_ready(self, timeout: float = 5.0) -> dict | None:
        """第一次启动时用：等一会，拿不到就算了。"""
        if self._value:
            return self._value
        got = _fetch(timeout)
        if got:
            self._value = got
            self._save(got)
        return self._value
