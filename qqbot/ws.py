"""ws.py — 最小 WebSocket 客户端（RFC 6455），只用标准库。

功能范围刚好够连 QQ 机器人的网关：文本帧、ping/pong、close。
不做扩展协商，不做压缩。

    ws = WSClient("wss://api.bot.qq.com/websocket/")
    ws.connect()
    ws.send_text('{"op":2,...}')
    op, data = ws.recv_frame(timeout=1.0)   # 超时返回 (None, None)
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(RuntimeError):
    pass


class WSClient:
    def __init__(self, url: str, origin: str = ""):
        u = urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise WSError(f"不支持的 scheme：{u.scheme}")
        self.secure = u.scheme == "wss"
        self.host = u.hostname or ""
        self.port = u.port or (443 if self.secure else 80)
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.origin = origin or f"{'https' if self.secure else 'http'}://{self.host}"
        self.sock: socket.socket | None = None
        self._buf = b""

    # ------------------------------------------------------------ 连接

    def connect(self, timeout: float = 15.0) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(timeout)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: {self.origin}\r\n"
            f"\r\n"
        )
        raw.sendall(req.encode())

        # 读响应头
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                raise WSError("握手时连接被关闭")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest

        text = header.decode("latin-1")
        status = text.split("\r\n", 1)[0]
        if "101" not in status:
            raise WSError(f"握手失败：{status}")

        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if expect.lower() not in text.lower():
            raise WSError("Sec-WebSocket-Accept 不匹配")

        self.sock.settimeout(None)

    # ------------------------------------------------------------ 收发

    def send_text(self, text: str) -> None:
        self._send_frame(text.encode("utf-8"), 0x1)

    def _send_frame(self, payload: bytes, opcode: int) -> None:
        if not self.sock:
            raise WSError("还没连接")
        n = len(payload)
        head = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def _read(self, n: int, timeout: float | None) -> bytes | None:
        if not self.sock:
            raise WSError("还没连接")
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            while len(self._buf) < n:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WSError("连接被关闭")
                self._buf += chunk
        except socket.timeout:
            return None
        finally:
            if timeout is not None:
                self.sock.settimeout(None)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_frame(self, timeout: float | None = None):
        """返回 (opcode, payload) 或 (None, None)（超时）。"""
        head = self._read(2, timeout)
        if head is None:
            return None, None
        b0, b1 = head[0], head[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F

        if length == 126:
            ext = self._read(2, None)
            if ext is None:
                raise WSError("读取长度失败")
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._read(8, None)
            if ext is None:
                raise WSError("读取长度失败")
            length = struct.unpack(">Q", ext)[0]

        mask = self._read(4, None) if masked else None
        payload = self._read(length, None) if length else b""
        if payload is None:
            raise WSError("读取负载失败")
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def recv_text(self, timeout: float | None = None) -> str | None:
        """只返回文本消息；内部处理 ping/close。"""
        while True:
            op, data = self.recv_frame(timeout)
            if op is None:
                return None
            if op == 0x1:
                return data.decode("utf-8", "replace")
            if op == 0x8:
                raise WSError("服务器关闭了连接")
            if op == 0x9:
                self._send_frame(data, 0xA)     # pong
                continue
            if op == 0xA:
                continue
            # 二进制/分片：忽略
            continue

    def close(self) -> None:
        try:
            if self.sock:
                self._send_frame(b"", 0x8)
        except Exception:
            pass
        finally:
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            self.sock = None
