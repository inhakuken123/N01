"""store.py — 存储层。只有这一个文件知道数据住在哪、长什么样。

三个记忆文件全部 append-only，永不改写，也永不删除：
    data/chatlog/YYYY-MM.jsonl   对话原话
    data/recall.jsonl            每次被想起
    data/scores.jsonl            模型打的强度分

另外一个是辅助文件：
    data/state.json              运行状态（位置缓存等）

数据缺口：所有路径都从 base_dir 推导，换前端时不用改这里。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_WRITE_LOCK = threading.Lock()      # 多个用户同时说话时，写文件要排队


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, base_dir: Path):
        self.base = base_dir
        self.data = base_dir / "data"
        self.chat_dir = self.data / "chatlog"
        self.recall_file = self.data / "recall.jsonl"
        self.scores_file = self.data / "scores.jsonl"
        self.state_file = self.data / "state.json"
        for d in (self.data, self.chat_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 底层

    @staticmethod
    def _append(path: Path, rows: list[dict]) -> None:
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8", newline="\n") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

    @staticmethod
    def _load(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # 半行（进程被杀）跳过，不影响其它
        return out

    # ------------------------------------------------------------ 对话

    def add_turn(self, uid: str, user_text: str, reply_text: str,
                 strength: float | None = None,
                 media: list[dict] | None = None) -> tuple[dict, dict]:
        """一次问答写两条。返回 (用户消息, ta的消息)。

        media 只记路径和一句描述，不存图本身 —— 图在 data/media/ 里。
        """
        u = {"id": uuid.uuid4().hex[:12], "ts": now_iso(), "uid": str(uid),
             "role": "user", "text": user_text.strip()}
        if strength is not None:
            u["s"] = round(float(strength), 2)
        if media:
            u["media"] = media
        a = {"id": uuid.uuid4().hex[:12], "ts": now_iso(), "uid": "char",
             "role": "assistant", "text": reply_text.strip()}

        month = datetime.fromisoformat(u["ts"]).strftime("%Y-%m")
        self._append(self.chat_dir / f"{month}.jsonl", [u, a])
        return u, a

    def all_messages(self) -> list[dict]:
        msgs = []
        for path in sorted(self.chat_dir.glob("*.jsonl")):
            msgs.extend(self._load(path))
        msgs.sort(key=lambda m: m.get("ts", ""))
        return msgs

    def turns(self) -> list[dict]:
        """把消息流还原成一问一答。"""
        turns, pending = [], None
        for m in self.all_messages():
            if m.get("role") == "user":
                pending = m
            elif m.get("role") == "assistant" and pending is not None:
                turns.append({
                    "id": pending.get("id"),
                    "uid": str(pending.get("uid")),
                    "ts": pending.get("ts", ""),
                    "user": pending.get("text", ""),
                    "reply": m.get("text", ""),
                    "s": pending.get("s"),
                    # 图/视频的路径，网页用它显示缩略图
                    "media": pending.get("media"),
                })
                pending = None
        return turns

    # ------------------------------------------------------------ 召回 / 打分 / 遗忘

    def mark_recalled(self, ids: list[str]) -> None:
        ts = now_iso()
        self._append(self.recall_file, [{"id": i, "ts": ts} for i in ids])

    def recall_index(self) -> tuple[dict[str, int], dict[str, str]]:
        hits: dict[str, int] = {}
        last: dict[str, str] = {}
        for r in self._load(self.recall_file):
            i = r.get("id")
            if not i:
                continue
            hits[i] = hits.get(i, 0) + 1
            if r["ts"] > last.get(i, ""):
                last[i] = r["ts"]
        return hits, last

    def add_score(self, mid: str, score: float, model: str) -> None:
        self._append(self.scores_file, [{"id": mid, "s": round(float(score), 2),
                                         "ts": now_iso(), "model": model}])

    def scores(self) -> dict[str, float]:
        out = {}
        for r in self._load(self.scores_file):
            if r.get("id") and r.get("s") is not None:
                out[r["id"]] = float(r["s"])
        return out

    # ------------------------------------------------------------ 状态

    def state(self) -> dict:
        if not self.state_file.exists():
            return {}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_state(self, state: dict) -> None:
        """原子写 + 加锁。

        直接 write_text 有两个问题：
          · 两个线程（位置刷新 / 别的任务）读-改-写会互相覆盖；
          · 写到一半崩溃会留下半截 JSON，下次读直接当空处理，缓存全丢。
        """
        tmp = self.state_file.with_suffix(".json.tmp")
        with _WRITE_LOCK:
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.state_file)
