"""llm.py — DeepSeek 客户端。只用标准库，方便打包。

对外两个方法：
    chat(messages)          -> str            一次性返回
    chat_stream(messages)   -> Iterator[str]  逐段返回

换模型/换网关只改 config，不动调用方。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", timeout: int = 120):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _request(self, payload: dict):
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"连不上 {self.base_url}：{e.reason}") from e

    def chat(self, messages: list[dict], temperature: float = 1.0,
             max_tokens: int = 800) -> str:
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens,
                   "stream": False}
        with self._request(payload) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"返回格式异常：{body}") from e

    def chat_stream_ex(self, messages: list[dict], tools: list[dict], sink: dict,
                       temperature: float = 1.0, max_tokens: int = 800,
                       tool_choice: str = "auto"):
        """流式 + 工具。内容逐段 yield，工具调用写进 sink["tool_calls"]。

        sink 用来带回结束时的状态，因为生成器没法方便地返回值。
        """
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens,
                   "stream": True, "tools": tools, "tool_choice": tool_choice}
        calls: dict[int, dict] = {}

        with self._request(payload) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                chunk = delta.get("content")
                if chunk:
                    yield chunk

                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    slot = calls.setdefault(i, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

        if calls:
            sink["tool_calls"] = [
                {"id": v["id"] or f"call_{i}", "type": "function",
                 "function": {"name": v["name"], "arguments": v["arguments"]}}
                for i, v in sorted(calls.items())
            ]

    def chat_stream(self, messages: list[dict], temperature: float = 1.0,
                    max_tokens: int = 800):
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens,
                   "stream": True}
        with self._request(payload) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                chunk = delta.get("content")
                if chunk:
                    yield chunk
