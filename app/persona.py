"""persona.py — 人设加载。

人设和示例对话现在是分开的文件：

    人格本体.md          设定部分，每轮都注入
    示例_对uid00.md      和 uid00 说话时才注入
    示例_对uidxx.md      和别的 uid 说话时才注入

好处：
  · 改示例不用碰人设，改人设不用管示例
  · 不想要的示例直接把 inject_examples 关掉
  · token 账目清楚，各占多少一眼看得出来

兼容旧格式：如果 人格本体.md 里还留着「## 对 uid00」这种段落，
会自动就地拆开使用（不会写回文件）。
"""

from __future__ import annotations

import re
from pathlib import Path

HEAD = re.compile(r"^##\s*对\s*(uid\s*\d+|uid\s*xx.*?)\s*$", re.I)
_cache: dict[str, tuple[float, str]] = {}


def _read(path) -> str:
    """读文件，带 mtime 缓存。文件改了下一轮自动生效。"""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    key = str(p)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return ""
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    text = p.read_text(encoding="utf-8")
    _cache[key] = (mtime, text)
    return text


def split(text: str) -> tuple[str, dict[str, str], str]:
    """把「主体 + ## 对 uid 段落」拆开。返回 (主体, {uid: 示例}, 其他示例)。"""
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = HEAD.match(line.strip())
        if m:
            starts.append((i, m.group(1)))

    if not starts:
        return text.strip(), {}, ""

    body = "\n".join(lines[:starts[0][0]]).strip()
    examples: dict[str, str] = {}
    other = ""
    for idx, (line_no, label) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        chunk = "\n".join(lines[line_no:end]).strip()
        if "xx" in label.lower():
            other = chunk
        else:
            examples[re.sub(r"\D", "", label)] = chunk
    return body, examples, other


def compose(cfg, uid: str | None = None) -> str:
    """拼出这一轮要注入的人设：主体 + 当前 uid 的示例。"""
    uid = str(uid if uid is not None else cfg.uid)
    body = _read(cfg.persona_file())
    if not body:
        return "你是空。话很少，安静，用短句回答。"

    part = ""
    if cfg.inject_examples:
        part = _read(cfg.example_file(uid))

    return (body + "\n\n" + part).strip() if part else body


def report(cfg) -> str:
    """打印各部分的体量，调 token 时用。"""
    def tok(s: str) -> int:
        cjk = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
        return cjk + (len(s) - cjk) // 4

    body = _read(cfg.persona_file())
    lines = [f"人设主体   {len(body):>5} 字符  ~{tok(body)} tokens"]
    for uid, name in (cfg.data.get("examples") or {}).items():
        txt = _read(cfg._resolve(name))
        lines.append(f"示例 {uid:<6} {len(txt):>5} 字符  ~{tok(txt)} tokens  {name}")
    full = compose(cfg)
    lines.append(f"当前注入   {len(full):>5} 字符  ~{tok(full)} tokens")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    from .config import Config
    from .paths import BASE

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(report(Config(BASE / "config.json")))
