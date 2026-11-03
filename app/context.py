"""context.py — 按人和重要度把旧对话组织成提示词里的两段。

设计来自需求：
  A 段 —— 最近 N 天说过的话，**按人分组**，同一个人说的话排在一起（按时间）
  B 段 —— 按重要度分层去捞：
            3~7 天内，0.5 < s < 0.7
            30 天内， 0.7 < s < 0.9
            不限时间，s = 0.9

为什么要分组：模型读「一人一段」比读「你跟一句我答一句」更容易分辨谁是谁。
为什么要分层：三天全量会把上下文撑爆（实测一天就 560 条 ≈ 1.3 万 token），
所以古老的话只挑重的捞，轻的放弃。
"""

from __future__ import annotations

from datetime import datetime, timedelta


def _fmt(ts: str) -> str:
    try:
        d = datetime.fromisoformat(ts)
        return d.strftime("%m-%d %H:%M")
    except Exception:
        return "??"


def _cut(s: str, n: int) -> str:
    """压掉多余空白；n <= 0 表示不截断。"""
    s = " ".join(str(s).split())
    return s if n <= 0 or len(s) <= n else s[: n - 1] + "…"


def _pending(s) -> bool:
    return s is None or s < 0


def build(turns: list[dict], scores: dict[str, float], *,
          names: dict[str, str], creator: str, char: str = "ta",
          days: int = 3, group_cap: int = 60, total_chars: int = 6000,
          line_chars: int = 80, recent_ids: set[str] | None = None,
          group_by: str = "person",
          tier_cap: int = 12, tier_chars: int = 2500,
          viewer: str | None = None, viewer_skip: int = 0,
          only_uid: str | None = None, start_index: int = 0,
          now: datetime | None = None) -> str:
    """返回两段文本，直接拼进 system prompt。

    viewer / viewer_skip：这个人最近的那几条已经在上面的滚动窗口里了，
    这里跳过，免得同一句出现两次。
    """
    now = now or datetime.now().astimezone()
    recent_ids = recent_ids or set()
    creator = str(creator)
    viewer = str(viewer) if viewer else None

    def is_recent(t: dict) -> bool:
        return t.get("id") in recent_ids

    # 计算「当前说话人最近 N 轮」的时间下界，比它新的不再重复放进来
    skip_after = None
    if viewer and viewer_skip > 0:
        mine = [t for t in turns if str(t.get("uid")) == viewer]
        if len(mine) > viewer_skip:
            skip_after = mine[-viewer_skip]["ts"]

    only_uid = str(only_uid) if only_uid else None

    def skip_it(t: dict, i: int = -1) -> bool:
        # 只给某个人看：别人的话整条不进上下文
        if only_uid and str(t.get("uid")) != only_uid:
            return True
        # 今天的话留给「历史」那一段，快照不要重复装（i 是它在 store 里的位置）
        if start_index > 0 and i >= start_index:
            return True
        if is_recent(t):
            return True
        if skip_after and str(t.get("uid")) == viewer and t["ts"] >= skip_after:
            return True
        return False

    def who(u: str) -> str:
        return names.get(str(u)) or f"uid{u}"

    # ---------------- A 段：最近 N 天 ----------------
    # group_by = "person"：按人分组，一问一答成对（看「谁说过什么」最方便）
    # group_by = "day"   ：按天分区，区内按时间（保留事件先后）
    cut_days = now - timedelta(days=days)
    picked: list[dict] = []
    for i, t in enumerate(turns):
        if str(t.get("uid")) == "char" or skip_it(t, i):
            continue
        try:
            if datetime.fromisoformat(t["ts"]) < cut_days:
                continue
        except Exception:
            continue
        picked.append(t)
    picked.sort(key=lambda r: r["ts"])

    if group_by == "day":
        by_key: dict[str, list[dict]] = {}
        for t in picked:
            day = t["ts"][:10]
            by_key.setdefault(day, []).append(t)
        # 最近的天在前
        keys = sorted(by_key, reverse=True)
        groups = []
        for day in keys:
            rows = by_key[day][-group_cap:] if group_cap > 0 else by_key[day]
            lines = []
            for r in rows:
                hhmm = _fmt(r["ts"])[-5:]
                lines.append(f"[{hhmm}] {who(r['uid'])}：{_cut(r['user'], line_chars)}")
                reply = _cut(r.get("reply") or "", line_chars)
                if reply:
                    lines.append(f"[{hhmm}] {char}：{reply}")
            groups.append((day, None, lines))
        weights = [1] * len(groups)
    else:
        by_uid: dict[str, list[dict]] = {}
        for t in picked:
            by_uid.setdefault(str(t["uid"]), []).append(t)
        groups = []
        for u, rows in by_uid.items():
            rows = rows[-group_cap:] if group_cap > 0 else rows
            lines = []
            for r in rows:
                lines.append(f"[{_fmt(r['ts'])}] {who(u)}：{_cut(r['user'], line_chars)}")
                reply = _cut(r.get("reply") or "", line_chars)
                if reply:
                    lines.append(f"[{_fmt(r['ts'])}] {char}：{reply}")
            groups.append((rows[-1]["ts"] if rows else "", u, lines))

        # 创造者排最前，其余按最近活跃时间倒序
        groups = ([g for g in groups if g[1] == creator]
                  + sorted([g for g in groups if g[1] != creator],
                           key=lambda g: g[0], reverse=True))
        weights = [(2 if u == creator else 1) for _ts, u, _l in groups]

    # 预算分配：total_chars <= 0 表示不限，全部装进去
    a_lines: list[str] = []
    unit = (total_chars / max(1, sum(weights))) if total_chars > 0 else 0
    leftover = 0
    for (idx, (_key, u, lines)) in enumerate(groups):
        allow = (max(500, unit * weights[idx]) + leftover) if total_chars > 0 else 10 ** 9
        header = f"### {who(u)}" if u else f"### {_key}"
        take: list[str] = []
        room = allow - len(header) - 1
        for line in reversed(lines):          # 最近的先说
            if len(line) + 1 > room:
                break
            take.append(line)
            room -= len(line) + 1
        if not take:
            leftover = allow if total_chars > 0 else 0
            continue
        used = len(header) + 1 + sum(len(x) + 1 for x in take)
        a_lines.append(header)
        a_lines.extend(reversed(take))
        leftover = max(0, allow - used) if total_chars > 0 else 0

    # ---------------- B 段：按重要度分三档 ----------------
    def bucket(lo: float, hi: float, dmin: int, dmax: int,
               exact: float | None = None) -> list[dict]:
        out = []
        for i, t in enumerate(turns):
            if str(t.get("uid")) == "char" or skip_it(t, i):
                continue
            s = scores.get(t["id"], t.get("s"))
            if _pending(s):
                continue
            s = float(s)
            if exact is not None:
                if abs(s - exact) > 1e-6:
                    continue
            else:
                # 分数只有 0.1/0.3/0.5/0.7/0.9 五档，所以下界必须含等号，
                # 否则「0.7~0.9」会把 0.7 那一整档漏掉。
                if not (lo <= s < hi):
                    continue
            try:
                d = (now - datetime.fromisoformat(t["ts"])).days
            except Exception:
                continue
            if d < dmin or d > dmax:
                continue
            out.append(t)
        out.sort(key=lambda r: r["ts"], reverse=True)
        return out[:tier_cap] if tier_cap > 0 else out

    tiers = [
        ("最重的（0.9）", bucket(0, 0, 0, 10 ** 6, exact=0.9)),
        ("这一个月里偏重的（0.7~0.9）", bucket(0.7, 0.9, 0, 30)),
        ("三到七天里还行的（0.5~0.7）", bucket(0.5, 0.7, 3, 7)),
    ]

    b_lines: list[str] = []
    for title, rows in tiers:
        if not rows:
            continue
        block = [f"### {title}"]
        for r in rows:
            days_ago = (now - datetime.fromisoformat(r["ts"])).days
            when = "今天" if days_ago == 0 else f"{days_ago} 天前"
            block.append(f"[{when}] {who(r['uid'])}：{_cut(r['user'], line_chars)}")
            reply = _cut(r.get("reply") or "", line_chars)
            if reply:
                block.append(f"[{when}] {char}：{reply}")
        if tier_chars <= 0:                 # 不设上限：整档都要
            b_lines += block
            continue
        cand = sum(len(x) for x in b_lines + block) + len(block)
        if cand > tier_chars:
            # 这一档放不下就只放能放下的那几条，不整档丢掉
            room = tier_chars - sum(len(x) + 1 for x in b_lines)
            for line in block:
                if len(line) + 1 > room:
                    break
                b_lines.append(line)
                room -= len(line) + 1
            continue
        b_lines += block

    out = []
    if a_lines:
        title = "按天分开" if group_by == "day" else "按人分开"
        out.append(f"\n## 这几天说过的话（{title}）\n" + "\n".join(a_lines) + "\n")
    if b_lines:
        out.append(f"\n## 往前想起来的一些事\n" + "\n".join(b_lines) + "\n")
    return "".join(out)
