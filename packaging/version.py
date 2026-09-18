#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包脚本共用的版本号来源。

以前版本号散在四个地方：falang/__init__.py 写 0.2.0、cli.py 的 version 命令里
又硬编码了一遍 0.2.0、build_deb.py 默认打 1.0.0、扩展的 package.json 也是 1.0.0。
结果 `fa version` 说 0.2.0，装出来的 deb 却叫 fa_1.0.0_amd64.deb —— 同一个东西
两个版本号，用户没法报 bug。

现在只有一个来源：**falang.__version__**。deb、zip、vsix 都从这里取，
package.json 由 build_vsix.py 在打包时对齐（并且扩展自测会检查两边一致）。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

for sub in ("compiler", "lsp"):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from falang import __version__ as VERSION      # noqa: E402

EXT_PKG = os.path.join(ROOT, "editors", "vscode-fa", "package.json")


def extension_version():
    """扩展 package.json 里现在写的版本号（可能和编译器不一致）"""
    import json
    try:
        with open(EXT_PKG, encoding="utf-8") as fh:
            return json.load(fh).get("version")
    except Exception:
        return None


def align_extension_version(version=None, restore=None):
    """把 package.json 的版本号改成和编译器一致。

    restore 传回上一次返回的值就能还原（打包完不该在仓库里留下改动）。
    """
    version = version or VERSION
    with open(EXT_PKG, encoding="utf-8") as fh:
        orig = fh.read()
    import json
    cur = json.loads(orig).get("version")
    if cur == version:
        return None
    patched = json.loads(orig)
    patched["version"] = version
    with open(EXT_PKG, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(patched, ensure_ascii=False, indent=2) + "\n")
    return orig


if __name__ == "__main__":
    print("编译器版本 :", VERSION)
    print("扩展版本   :", extension_version())
    print("deb 文件名 :", f"fa_{VERSION}_amd64.deb")
    print("zip 文件名 :", f"fa-{VERSION}-windows-amd64.zip")
    print("vsix 文件名:", f"fa-vscode-{VERSION}.vsix")
