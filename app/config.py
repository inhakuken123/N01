"""config.py — 配置解析。

密钥只有一处：config.json 的 `api_key`。
网页里的「初始化」和「接口设置」写的就是它。

密钥永远不打印、不写日志。第一次运行会自动生成 config.json。
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "uid": "00",
    "display_name": "uid00",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "char_name": "ta",
    "creator_uid": "00",
    "persona_path": r"人格本体.md",
    "persona_fallback": "persona.md",
    "examples": {"00": "示例_对uid00.md", "other": "示例_对uidxx.md"},
    "examples_fallback": False,
    "inject_examples": True,
    "temperature": 1.0,
    "max_tokens": 800,
    "prefix_user": True,

    # 上下文（0 表示不限）
    "history_window": 20,
    "history_mode": "append",
    "context_blocks": True,
    "ctx_days": 3,
    "ctx_group": "day",
    "ctx_group_cap": 0,
    "ctx_chars": 0,
    "ctx_line_chars": 0,
    "ctx_refresh": 86400,
    "tier_cap": 0,
    "tier_chars": 0,

    # 记忆 / 工具
    "memory_tool": False,
    "memory_tool_k": 5,
    "private_creator": False,
    "tool_temperature": 0.6,
    "tool_rounds": 3,

    # 上网
    "search": True,
    "search_k": 5,
    "search_per_turn": 2,
    "fetch_per_turn": 2,

    # 其它
    "geo": True,
    "geo_ttl": 1800,
    "score_async": True,
    "retry_interval": 600,
    "bot_token": "",
}


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data = dict(DEFAULTS)
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8-sig")))
            except Exception:
                pass
        else:
            self.save()

    def __getattr__(self, name):
        try:
            return self.data[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------ 路径

    def _resolve(self, p: str):
        """相对路径一律相对程序目录，不依赖当前工作目录。"""
        if not p:
            return None
        path = Path(p)
        return path if path.is_absolute() else (self.path.parent / path)

    # ------------------------------------------------------------ 密钥

    def api_key_value(self) -> str:
        """key 只从 config.json 的 api_key 读。

        以前还支持 api.txt 和环境变量，现在统一到一处：
        出问题只看一个地方，也不会出现「改了 api.txt 却不生效」这种事。
        """
        return (self.data.get("api_key") or "").strip()

    # ------------------------------------------------------------ 人设

    def persona_file(self):
        """返回人设 md 的真实路径；找不到返回 None。"""
        p = self._resolve(self.data.get("persona_path", ""))
        if p and p.exists():
            return p
        fb = self._resolve(self.data.get("persona_fallback", "persona.md"))
        if fb and fb.exists():
            return fb
        return None

    def persona_text(self) -> str:
        p = self.persona_file()
        if p:
            return p.read_text(encoding="utf-8")
        return "你是空。话很少，安静，用短句回答。"

    # ------------------------------------------------------------ 示例

    def example_file(self, uid: str):
        """返回这个 uid 对应的示例文件。

        先找专属示例（examples["23"] → 示例_对uid23.md）。
        找不到时，只有 examples_fallback 为真才退回 examples["other"]；
        默认不退回 —— 没有这个人的专属示例就什么都不注入，免得串台。
        """
        m = self.data.get("examples") or {}
        name = m.get(str(uid))
        if not name and self.data.get("examples_fallback"):
            name = m.get("other")
        if not name:
            return None
        p = self._resolve(name)
        return p if p and p.exists() else None
