#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fa 的 Python 入口。

`bin/fa` 是 bash 脚本，Windows 上跑不了；VSCode 扩展、打包出来的 .exe、
以及 deb 里的 /usr/bin/fa 都走这里，保证四个地方行为一模一样。

    python3 bin/fa_cli.py run hello.fa
    python3 bin/fa_cli.py ide --port 8765
    python3 bin/fa_cli.py lsp
"""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(HERE, "compiler"), os.path.join(HERE, "lsp"), os.path.join(HERE, "ide")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("FA_HOME", HERE)


def main(argv=None):
    from falang.cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
