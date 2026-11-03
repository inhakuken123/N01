"""paths.py — 程序目录与常用路径。

所有模块都从这里拿路径，不各自去猜 __file__ 的层级。
打包成 exe 后（frozen）以可执行文件所在目录为准。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # app/paths.py -> app/ -> 程序根目录
    return Path(__file__).resolve().parent.parent


BASE = _base()
WEB = BASE / "web"
DATA = BASE / "data"
