"""run.py — 唯一入口。

    python run.py            启动网页版（默认 8765）
    python run.py 9000       换端口
    python run.py --no-browser
"""

import sys

from app.server import main

if __name__ == "__main__":
    main()
