"""media.py — 图片和视频的收、压、存。

图片规则（按需求定死）：
  · 字节数 ≤ 1MB 且分辨率没超限 → 原样保留，一个字节都不动
  · 字节数 > 1MB → 压到 1MB 以下（先降质，不够再降分辨率）
  · 长边超过 MAX_SIDE → 一律缩到这个尺寸
  · 压过就记下来：原始大小 / 压缩后大小 / 原始尺寸，写进聊天记录

视频规则：
  · 模型只吃图片，所以视频要**抽帧**当图片发
  · 抽帧靠 ffmpeg（系统的，或者 imageio-ffmpeg 自带的那个）
  · 没有 ffmpeg → 视频存下来但不抽帧，交给上层降级成「看不清内容」

模型只认 base64 的 data URL（公网 URL 它不去下载，实测 400），
所以这里的产物直接就是能塞进 messages 的东西。
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

COMPRESS_ABOVE = 1024 * 1024     # 超过 1MB 才压
MAX_SIDE = 1024                  # 分辨率上限（长边）
QUALITY_STEPS = (85, 78, 70, 62, 55, 48)
MAX_BYTES = 8 * 1024 * 1024      # 单张原图硬上限，超过直接拒
OK_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}

MAX_VIDEO = 1024 * 1024 * 1024   # 视频上限 1GB —— 走文件流，不经 base64
VIDEO_MIME = {"video/mp4", "video/quicktime", "video/webm", "video/x-msvideo"}
VIDEO_FRAMES = 3                 # 抽几帧


class MediaError(ValueError):
    pass


def split_data_url(s: str) -> tuple[str, bytes]:
    """'data:image/png;base64,xxx' → ('image/png', b'...')"""
    if not isinstance(s, str) or not s.startswith("data:"):
        raise MediaError("不是 data URL")
    head, _, tail = s.partition(",")
    mime = head[5:].split(";")[0].strip().lower() or "image/jpeg"
    try:
        raw = base64.b64decode(tail, validate=False)
    except (binascii.Error, ValueError) as e:
        raise MediaError("base64 解不开") from e
    if len(raw) > MAX_BYTES:
        raise MediaError(f"图片太大（{len(raw) // 1024}KB > {MAX_BYTES // 1024}KB）")
    return mime, raw


def to_data_url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def _encode(img, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def prepare(data_url: str) -> dict:
    """归一化一张图。

    返回 {
        data_url, mime, bytes, w, h,          # 最终给模型用的
        orig_bytes, orig_w, orig_h,           # 原始尺寸和大小
        compressed: bool, why: str            # 压没压、为什么压
    }
    """
    mime, raw = split_data_url(data_url)
    if mime not in OK_MIME:
        raise MediaError(f"不支持的图片类型：{mime}")

    item = {"data_url": to_data_url(mime, raw), "mime": mime, "bytes": raw,
            "w": 0, "h": 0, "orig_bytes": len(raw), "orig_w": 0, "orig_h": 0,
            "compressed": False, "why": ""}

    try:
        from PIL import Image
    except ImportError:
        item["why"] = "没有 Pillow，不压"
        return item

    try:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
    except Exception:
        item["why"] = "图片读不开，原样发"
        return item

    item["orig_w"], item["orig_h"] = w, h
    item["w"], item["h"] = w, h

    too_big = len(raw) > COMPRESS_ABOVE
    too_wide = max(w, h) > MAX_SIDE
    if not (too_big or too_wide):
        return item                       # 达标，原样保留

    # 1) 先按分辨率缩
    if too_wide:
        scale = MAX_SIDE / max(w, h)
        w, h = max(1, int(w * scale)), max(1, int(h * scale))

    has_alpha = img.mode in ("RGBA", "LA", "P")
    work = img.convert("RGBA" if has_alpha else "RGB")
    if too_wide:
        work = work.resize((w, h), Image.LANCZOS)
    if has_alpha:                          # JPEG 没有透明通道，垫白底
        bg = Image.new("RGB", work.size, "white")
        bg.paste(work, mask=work.split()[-1])
        work = bg

    # 2) 降质直到进 1MB
    out = _encode(work, QUALITY_STEPS[0])
    for q in QUALITY_STEPS[1:]:
        if len(out) <= COMPRESS_ABOVE:
            break
        out = _encode(work, q)

    # 3) 降质还不够（极少见），继续缩分辨率
    guard = 0
    while len(out) > COMPRESS_ABOVE and max(work.size) > 320 and guard < 6:
        w2, h2 = int(work.size[0] * 0.75), int(work.size[1] * 0.75)
        work = work.resize((max(1, w2), max(1, h2)), Image.LANCZOS)
        out = _encode(work, QUALITY_STEPS[2])
        guard += 1

    item.update({
        "data_url": to_data_url("image/jpeg", out),
        "mime": "image/jpeg",
        "bytes": out,
        "w": work.size[0],
        "h": work.size[1],
        "compressed": True,
        "why": ("太大" if too_big else "") + ("+分辨率超限" if too_wide else ""),
    })
    return item


def save(base: Path, item: dict, tag: str = "") -> str:
    """存到 data/media/YYYY-MM/，返回相对 base 的路径。存的是压缩后的那份。"""
    month = datetime.now(timezone.utc).astimezone().strftime("%Y-%m")
    d = base / "data" / "media" / month
    d.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": "png", "image/webp": "webp",
           "image/gif": "gif"}.get(item["mime"], "jpg")
    name = f"{tag + '_' if tag else ''}{uuid.uuid4().hex[:12]}.{ext}"
    (d / name).write_bytes(item["bytes"])
    return f"data/media/{month}/{name}"


def describe(item: dict) -> str:
    """给人看的一行：有没有压、压了多少。"""
    kb0 = item["orig_bytes"] // 1024
    kb1 = len(item["bytes"]) // 1024
    if not item["compressed"]:
        return f"{item['orig_w']}×{item['orig_h']}  {kb0}KB（原样）"
    return (f"{item['orig_w']}×{item['orig_h']} {kb0}KB → "
            f"{item['w']}×{item['h']} {kb1}KB（{item['why']}）")


def read_as_data_url(base: Path, rel: str) -> str | None:
    """把存过的图读回 data URL（调试或重放用）。"""
    p = base / rel
    if not p.exists():
        return None
    ext = p.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "webp": "image/webp",
            "gif": "image/gif"}.get(ext, "image/jpeg")
    return to_data_url(mime, p.read_bytes())


# ---------------------------------------------------------------- 视频

def find_ffmpeg() -> str | None:
    """找一个能用的 ffmpeg。

    顺序：系统的 PATH → Windows 上 winget 装的位置 → imageio-ffmpeg 自带的。
    加中间那条是因为 winget 装完要重开窗口 PATH 才生效，
    但程序不该因此就抽不了帧。
    """
    p = shutil.which("ffmpeg")
    if p:
        return p

    for cand in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links/ffmpeg.exe",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("/usr/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ):
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            pass

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _probe(path: str, exe: str) -> tuple[float, int, int]:
    """从 ffmpeg -i 的 stderr 里抠时长和分辨率。读不到就返回 0。"""
    try:
        out = subprocess.run([exe, "-i", path], capture_output=True,
                             text=True, errors="replace", timeout=30).stderr
    except Exception:
        return 0.0, 0, 0
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    w = h = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    return dur, w, h


def video_frames(raw: bytes, mime: str = "video/mp4",
                 n: int = VIDEO_FRAMES) -> dict:
    """把视频抽成 n 张 JPEG。

    返回 {'ok': True, 'frames': [bytes], 'dur', 'w', 'h'}
    或   {'ok': False, 'error': '...'}
    """
    exe = find_ffmpeg()
    if not exe:
        return {"ok": False, "error": "没有 ffmpeg"}

    tmp = Path(tempfile.mkdtemp(prefix="n01vid_"))
    src = tmp / "in.bin"
    src.write_bytes(raw)
    try:
        return video_frames_file(src, n)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def video_frames_file(src: Path, n: int = VIDEO_FRAMES) -> dict:
    """对磁盘上的视频抽帧。大文件不经过内存 —— 这条才是 1GB 能用的路。"""
    exe = find_ffmpeg()
    if not exe:
        return {"ok": False, "error": "没有 ffmpeg"}

    tmp = Path(tempfile.mkdtemp(prefix="n01frames_"))
    try:
        dur, w, h = _probe(str(src), exe)
        # 按总时长均匀取 n 帧：fps = n / 时长。
        # 不用 thumbnail 滤镜 —— 它按批挑一帧，短视频只出得来 1 张。
        fps = max(0.05, n / dur) if dur > 0 else 0.5
        cmd = [exe, "-y", "-i", str(src),
               "-vf", f"fps={fps:.4f},scale={MAX_SIDE}:-2",
               "-frames:v", str(n), "-q:v", "3", str(tmp / "f%02d.jpg")]
        subprocess.run(cmd, capture_output=True, timeout=180)
        frames = [f.read_bytes() for f in sorted(tmp.glob("f*.jpg"))[:n]]
        if not frames:
            return {"ok": False, "error": "抽帧失败（格式可能不支持）"}
        return {"ok": True, "frames": frames, "dur": dur, "w": w, "h": h}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def new_path(base: Path, ext: str, tag: str = "") -> tuple[Path, str]:
    """给一个还没写的媒体文件占个位置，返回 (绝对路径, 相对路径)。"""
    month = datetime.now(timezone.utc).astimezone().strftime("%Y-%m")
    d = base / "data" / "media" / month
    d.mkdir(parents=True, exist_ok=True)
    name = f"{tag + '_' if tag else ''}{uuid.uuid4().hex[:12]}.{ext}"
    return d / name, f"data/media/{month}/{name}"


def prepare_video(data_url: str) -> dict:
    """收一个 base64 视频（小文件兜底用）。大文件请走 video_frames_file。"""
    head, _, tail = str(data_url).partition(",")
    mime = head[5:].split(";")[0].strip().lower() or "video/mp4"
    try:
        raw = base64.b64decode(tail, validate=False)
    except (binascii.Error, ValueError):
        return {"ok": False, "error": "base64 解不开"}
    if len(raw) > MAX_VIDEO:
        return {"ok": False, "error": f"视频太大（{len(raw)//1024//1024}MB）"}

    got = video_frames(raw, mime)
    if not got["ok"]:
        return {"ok": False, "raw": raw, "mime": mime, "error": got["error"]}

    frames = [to_data_url("image/jpeg", b) for b in got["frames"]]
    return {"ok": True, "raw": raw, "mime": mime, "frames": frames,
            "frame_bytes": got["frames"], "dur": got["dur"],
            "w": got["w"], "h": got["h"]}


def save_raw(base: Path, raw: bytes, ext: str, tag: str = "") -> str:
    """原样存一段二进制（视频用），返回相对路径。"""
    path, rel = new_path(base, ext, tag)
    path.write_bytes(raw)
    return rel
