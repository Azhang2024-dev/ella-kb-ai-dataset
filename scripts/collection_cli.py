#!/usr/bin/env python
"""collection_cli 启动器。

真正的命令实现在 core/document_collection/cli.py，这个文件只负责：
1. 把仓库根加进 sys.path（无论从哪里调用都能跑）；
2. 转发退出码。

用法：
    .venv\\Scripts\\python scripts\\collection_cli.py --help
    .venv\\Scripts\\python scripts\\collection_cli.py validate-config
    .venv\\Scripts\\python scripts\\collection_cli.py inventory --per-source-limit 30
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.document_collection.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
