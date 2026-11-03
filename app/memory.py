"""memory.py — 权重与检索。纯计算。

三个因子：
    强度      模型打分（五档 0.1/0.3/0.5/0.7/0.9）
    想起次数  recall 日志里出现几次
    时间      距最后一次想起多少天

    half_life = 14 × (0.6 + 2.4 × 强度)
    w = 0.5^(天数/half_life) × (0.4+0.6×强度) × (1 + 0.2×ln(1+次数))

强度没有规则兜底。模型打分失败时存 -1 作为「待补打」标记，
在那之前按中性 0.5 参与计算，等补分成功再换成真实分。

相关度不参与打分，只做筛选门。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

BANDS = (0.1, 0.3, 0.5, 0.7, 0.9)

PENDING = -1.0        # 打分失败 / 还没打的标记
NEUTRAL = 0.5         # 待补打期间临时按这个算

BASE_HALF_LIFE = 14.0
HALF_LIFE_GAIN = 2.4
HIT_BONUS = 0.2

# 相关度在排序里的占比：rel=0 时拿到底分的 30%，rel=1 时拿满
REL_FLOOR = 0.3

# 按人加权：当前说话的人 1.0，创造者 0.75，其他人 0.5
BIAS_SELF = 1.0
BIAS_CREATOR = 0.75
BIAS_OTHERS = 0.5

# 问题里点了名的人（「某个名字说过什么」），他的记忆往前排
PREFER_BOOST = 4.0


def snap(x: float) -> float:
    x = max(0.0, min(1.0, float(x)))
    return min(BANDS, key=lambda b: abs(b - x))


def is_pending(v: float | None) -> bool:
    return v is None or float(v) < 0


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)} or {t}


def relevance(query: str, turn: dict) -> float:
    q, t = _bigrams(query), _bigrams(turn["user"] + turn["reply"])
    if not q or not t:
        return 0.0
    return len(q & t) / math.sqrt(len(q) * len(t))


def strength_of(turn: dict, scores: dict[str, float]) -> tuple[float, bool]:
    """返回 (用于计算的强度, 是否待补打)。"""
    raw = scores.get(turn["id"], turn.get("s"))
    if is_pending(raw):
        return NEUTRAL, True
    return snap(raw), False


def weight(turn: dict, hits: dict[str, int], last: dict[str, str],
           scores: dict[str, float], now: datetime | None = None) -> dict:
    s, pending = strength_of(turn, scores)

    seen = last.get(turn["id"], turn["ts"])
    then = datetime.fromisoformat(seen)
    now = now or datetime.now(timezone.utc).astimezone()
    if then.tzinfo is None:
        then = then.astimezone()
    days = max(0.0, (now - then).total_seconds() / 86400.0)

    hl = BASE_HALF_LIFE * (0.6 + HALF_LIFE_GAIN * s)
    decay = 0.5 ** (days / hl)
    n = hits.get(turn["id"], 0)
    w = decay * (0.4 + 0.6 * s) * (1 + HIT_BONUS * math.log1p(n))
    return {"s": s, "pending": pending, "days": round(days, 1),
            "hl": round(hl, 1), "hits": n, "w": round(w, 4)}


def person_bias(turn_uid: str, viewer: str | None, creator: str | None) -> float:
    """按人加权：自己 > 创造者 > 别人。"""
    u = str(turn_uid)
    if viewer and u == str(viewer):
        return BIAS_SELF
    if creator and u == str(creator):
        return BIAS_CREATOR
    return BIAS_OTHERS


def retrieve(turns: list[dict], query: str, *,
             hits: dict, last: dict, scores: dict,
             k: int = 3, now: datetime | None = None,
             viewer: str | None = None, creator: str | None = None,
             prefer: set[str] | None = None) -> list[dict]:
    """全局检索，不淘汰任何一条。

    排序 = 权重 × 说话人加成 × 相关度系数 × 指定人加成
      · 权重：时间衰减 × 强度 × 想起次数
      · 说话人加成：自己 1.0 / 创造者 0.75 / 别人 0.5
      · 相关度系数：0.3 + 0.7 × 相关度（不做门槛，只是调制）
      · prefer：问「某某说过什么」时，那个人的记忆要能被翻上来
    """
    prefer = {str(x) for x in (prefer or set())}
    rows = []
    for t in turns:
        rel = relevance(query, t)
        w = weight(t, hits, last, scores, now)
        bias = person_bias(t["uid"], viewer, creator)
        final = w["w"] * bias * (REL_FLOOR + (1 - REL_FLOOR) * rel)
        if prefer and str(t["uid"]) in prefer:
            final *= PREFER_BOOST
        rows.append({**t, "rel": round(rel, 3), "bias": bias,
                     **w, "score": round(final, 4)})
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:k]


def build_memory_block(rows: list[dict], now: datetime | None = None,
                       names: dict[str, str] | None = None,
                       char: str = "你") -> str:
    """把检索结果渲染成给模型看的文本。

    每条带上：日期、距今多少天、当时的强度。ta知道这些才能说出「两个月前」，
    也才能分得清「随口一句」和「郑重说过的事」。

    names 是 uid → 名字，有名字就用名字写，别让ta照着 uid 念。
    """
    if not rows:
        return ""
    now = now or datetime.now(timezone.utc).astimezone()
    names = names or {}
    lines = ["以下是你过去的对话记忆。方括号里是日期、距今天数、"
             "以及那件事当时有多重（0.1 最轻，0.9 最重）。"
             "可以自然地引用，不要逐字复述。"]
    for r in rows:
        when = datetime.fromisoformat(r["ts"])
        days = max(0, (now - when).days)
        rel = "今天" if days == 0 else ("昨天" if days == 1 else f"{days} 天前")
        s = float(r.get("s", 0.5))
        who = names.get(str(r["uid"])) or f"uid{r['uid']}"
        lines += [f"\n[{when:%m月%d日} · {rel} · 强度 {s:.1f}]",
                  f"{who}：{r['user']}",
                  f"{char}：{r['reply']}"]
    return "\n".join(lines)


def pending_turns(turns: list[dict], scores: dict[str, float]) -> list[dict]:
    """需要补打分的对话：没分、或者分数是 -1。"""
    return [t for t in turns if is_pending(scores.get(t["id"]))]
