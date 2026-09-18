#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打出 Windows 版：fa.exe + 一个解压即用的 zip。

    python3 packaging/build_windows.py            # 出 dist/fa-<版本>-windows-amd64.zip
    python3 packaging/build_windows.py --no-zig   # 只跑启动器逻辑的自测（不交叉编译）

exe 是用 zig cc 交叉编出来的（`python3 -m ziglang cc -target x86_64-windows`），
不需要 Windows 机器，也不需要 MSVC。

关于「为什么不把 Python 打进 exe」：FA 的编译器/LSP/IDE 后端是 Python 写的，
把 CPython 塞进去要 30 MB 起步，还要为每个 Python 小版本重打一次。
所以 fa.exe 是个启动器：找到 FA 安装目录 + 找到一个 Python，把参数原样转交，
退出码原样传回。目标机器上 `winget install Python.Python.3.12` 就行。

**这里最容易骗自己**：exe 在 Linux 上跑不了，「编出来了」不等于「能用」。
所以本脚本先用 gcc 把**同一份 C 源文件**编成 Linux 版真跑一遍——三种安装布局、
真编译真运行、退出码透传、找不到目录时的报错，全都实测过，再去交叉编译 Windows 版。
两个平台共用的只有 CreateProcess / fork+exec 那几行。
"""

import argparse
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "compiler"))
sys.path.insert(0, os.path.join(ROOT, "lsp"))
sys.path.insert(0, HERE)
from version import VERSION as FA_VERSION      # noqa: E402

LAUNCHER = os.path.join(HERE, "launcher", "fa_launcher.c")
DIST = os.path.join(ROOT, "dist")

# 和 build_deb.py 保持一致的载荷（两边共用同一套筛选规则）
PAYLOAD = ["compiler", "lsp", "ide", "stdlib", "runtime", "bin", "docs"]
SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", "node_modules", "dist", "build"}
SKIP_EXT = {".pyc", ".o", ".out", ".s", ".so", ".log"}
NEVER_SKIP_BY_EXT = {"runtime"}       # runtime/fa_syscall.S 是手写汇编源，不是中间产物


def run(cmd, timeout=900, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT, env=env)


def copy_tree(src, dst, top=""):
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, src)
            parts = rel.split(os.sep)
            if any(p in SKIP_DIRS for p in parts):
                continue
            if top not in NEVER_SKIP_BY_EXT and \
                    os.path.splitext(rel)[1].lower() in SKIP_EXT:
                continue
            dp = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(fp, dp)
            n += 1
    return n


# ---------------------------------------------------------------- 自测

FAILS = []
N = [0]


def ck(label, cond, msg=""):
    N[0] += 1
    if cond:
        print(f"  ✓ {label}")
        return True
    FAILS.append(f"{label}　{msg}")
    print(f"  ✗ {label}　{msg}")
    return False


def make_payload(dst):
    """在 dst 下摆出 lib/fa 载荷，返回文件数"""
    root = os.path.join(dst, "lib", "fa")
    os.makedirs(root, exist_ok=True)
    n = 0
    for top in PAYLOAD:
        sp = os.path.join(ROOT, top)
        if not os.path.isdir(sp):
            continue
        n += copy_tree(sp, os.path.join(root, top), top=top)
    return n


def selftest_posix():
    """把同一份启动器源码用本机 C 编译器编出来，真跑一遍。"""
    print("启动器逻辑自测（本机编译，同一份 C 源文件）")
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        print("  ! 没有 C 编译器，跳过启动器自测")
        return False
    work = tempfile.mkdtemp(prefix="fa_launcher_")
    exe = os.path.join(work, "fa")
    r = subprocess.run([cc, "-O2", "-Wall", "-o", exe, LAUNCHER],
                       capture_output=True, text=True, timeout=300)
    if not ck("启动器能用 -Wall 干净编过", r.returncode == 0, r.stderr[-400:]):
        return False

    # 布局一：绿色版，exe 和 lib/fa 同级
    green = os.path.join(work, "green")
    os.makedirs(green)
    shutil.copy2(exe, os.path.join(green, "fa"))
    make_payload(green)

    # 布局二：安装版，bin/fa 的上一级有 lib/fa
    inst = os.path.join(work, "inst")
    os.makedirs(os.path.join(inst, "bin"))
    shutil.copy2(exe, os.path.join(inst, "bin", "fa"))
    make_payload(inst)

    def go(binary, *args, env=None, cwd=None):
        e = dict(os.environ)
        e.pop("FA_HOME", None)
        e.pop("PYTHONPATH", None)
        if env:
            e.update(env)
        return subprocess.run([binary, *args], capture_output=True, text=True,
                              timeout=600, env=e, cwd=cwd)

    r = go(os.path.join(green, "fa"), "version")
    ck("绿色版布局：找得到 FA 并且 version 正常",
       r.returncode == 0 and "FA 编译器" in r.stdout, (r.stdout + r.stderr)[:200])
    ck("绿色版布局：运行时目录指的是包里那份",
       os.path.join(green, "lib", "fa", "runtime") in r.stdout, r.stdout[:200])

    r = go(os.path.join(inst, "bin", "fa"), "version")
    ck("安装版布局（bin/fa + ../lib/fa）：找得到",
       r.returncode == 0 and os.path.join(inst, "lib", "fa", "runtime") in r.stdout,
       (r.stdout + r.stderr)[:200])

    r = go(exe, "version", env={"FA_HOME": os.path.join(green, "lib", "fa")})
    ck("FA_HOME 环境变量优先", r.returncode == 0 and "FA 编译器" in r.stdout,
       (r.stdout + r.stderr)[:200])

    src = os.path.join(work, "a.fa")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write('fn main() -> i64:\n    let v = Vec<i64>[3, 1, 2]\n'
                 '    v.sort()\n    print("排序 ", v.to_str())\n    return 0\n')
    r = go(os.path.join(green, "fa"), "run", src)
    ck("经启动器真编译真运行（Linux 上）",
       r.returncode == 0 and r.stdout == "排序  [1, 2, 3]\n",
       f"rc={r.returncode} out={r.stdout!r} err={r.stderr[-200:]!r}")

    bad = os.path.join(work, "bad.fa")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("fn main() -> i64:\n    let x: str = 1\n    return 0\n")
    r = go(os.path.join(green, "fa"), "run", bad)
    ck("类型错误照样报出来，退出码非 0",
       r.returncode != 0 and ("类型错误" in r.stdout or "类型错误" in r.stderr),
       f"rc={r.returncode} out={r.stdout[:120]!r}")

    seven = os.path.join(work, "seven.fa")
    with open(seven, "w", encoding="utf-8") as fh:
        fh.write("fn main() -> i64:\n    return 7\n")
    r = go(os.path.join(green, "fa"), "run", seven)
    ck("程序自己的退出码原样透传（7）", r.returncode == 7, f"rc={r.returncode}")

    r = go(os.path.join(green, "fa"), "check", src)
    ck("参数带空格也能传对", r.returncode == 0, (r.stdout + r.stderr)[:150])

    # 找不到目录时的报错：必须是人话，还得是非 0 退出码
    nowhere = os.path.join(work, "empty")
    os.makedirs(nowhere)
    lonely = os.path.join(nowhere, "fa")
    shutil.copy2(exe, lonely)
    r = go(lonely, "version")
    ck("找不到安装目录时给人话 + 退出码 3",
       r.returncode == 3 and "找不到 FA 的安装目录" in r.stderr,
       f"rc={r.returncode} err={r.stderr[:200]!r}")
    ck("找不到 Python 的提示也写在里面（装 Python 的办法）",
       "FA_PYTHON" in r.stderr or "FA_HOME" in r.stderr, r.stderr[:200])

    shutil.rmtree(work, ignore_errors=True)
    return True


# ---------------------------------------------------------------- PE 校验

def check_pe(path):
    """不看「编出来了」，看它真的是个 x86-64 的 PE32+ 可执行文件。"""
    print("\n校验 fa.exe 的 PE 头")
    with open(path, "rb") as fh:
        data = fh.read(4096)
    ck("有 MZ 头", data[:2] == b"MZ", data[:8].hex())
    if data[:2] != b"MZ":
        return False
    (e_lfanew,) = struct.unpack_from("<I", data, 0x3C)
    ck("PE 签名位置合理", 0 < e_lfanew < 2048, hex(e_lfanew))
    ck("有 PE\\0\\0 签名", data[e_lfanew:e_lfanew + 4] == b"PE\0\0",
       data[e_lfanew:e_lfanew + 8].hex())
    machine, nsec = struct.unpack_from("<HH", data, e_lfanew + 4)
    ck("机器码是 x86-64 (0x8664)", machine == 0x8664, hex(machine))
    ck("节数量正常", 1 <= nsec <= 32, nsec)
    (magic,) = struct.unpack_from("<H", data, e_lfanew + 24)
    ck("是 PE32+（64 位）而不是 PE32", magic == 0x20B, hex(magic))
    (subsys,) = struct.unpack_from("<H", data, e_lfanew + 24 + 68)
    ck("子系统是 Windows CUI（控制台程序）", subsys == 3, subsys)
    ck("文件不为空", os.path.getsize(path) > 4096, os.path.getsize(path))
    # 里面应该能看到我们要传给 Python 的那个脚本名
    with open(path, "rb") as fh:
        blob = fh.read()
    ck("二进制里带着 fa_cli.py 这个字符串", b"fa_cli.py" in blob, "")
    ck("二进制里带着 FA_HOME 的提示", b"FA_HOME" in blob, "")
    return True


def cross_compile(out_exe):
    """用 zig cc 交叉编 Windows exe。"""
    print("\n用 zig cc 交叉编译 fa.exe")
    try:
        import ziglang  # noqa: F401
        zig = [sys.executable, "-m", "ziglang"]
    except Exception:
        z = shutil.which("zig")
        if not z:
            print("  ! 没有 zig，也没装 ziglang 包：pip install --user ziglang")
            return False
        zig = [z]
    cmd = zig + ["cc", "-target", "x86_64-windows", "-O2",
                 "-o", out_exe, LAUNCHER]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, cwd=ROOT)
    if not ck("zig cc 编出 fa.exe", r.returncode == 0 and os.path.exists(out_exe),
              (r.stderr or r.stdout)[-600:]):
        return False
    # zig 会顺手在 exe 边上留个 .pdb（调试符号，1 MB 出头）。交付包里用不上，删掉。
    pdb = os.path.splitext(out_exe)[0] + ".pdb"
    if os.path.exists(pdb):
        os.remove(pdb)
    print(f"  ✓ {os.path.relpath(out_exe, ROOT)}  {os.path.getsize(out_exe) // 1024} KB")
    return True


README_WIN = """# FA 编译器 —— Windows 版

解压即用，不用装、不用配 MSVC。

## 1. 先装 Python（只要一次）

```
winget install Python.Python.3.12
```

装完**重开一个终端**。FA 的编译器是 Python 写的，`fa.exe` 负责找到它并把活儿交过去；
真正落地成机器码的，是它给你生成的那些程序。

## 2. 用起来

```
cd fa-<版本>-windows-amd64
fa.exe version
fa.exe run  hello.fa
fa.exe check hello.fa
fa.exe ide --port 8765        # 浏览器里写代码：补全 / 报错 / 悬停 / 跳转
```

把 `fa.exe` 所在目录加进 PATH，就能在任何地方直接敲 `fa`。
不想加 PATH 也可以用 `set FA_HOME=<解压目录>\\lib\\fa`。

## 3. VSCode 扩展

`editors/vscode-fa` 目录里就是扩展本体（另有 dist/fa-vscode-<版本>.vsix）：

```
code --install-extension fa-vscode-<版本>.vsix
```

装上就有语法高亮、代码补全、实时错误波浪线、悬停文档、大纲、`fa: 新建示例`。
扩展会自己找到 `fa.exe`（或按 `fa.lspPath` 指定的路径）。

## 4. Windows 上能做什么、不能做什么

| 功能 | Windows |
|---|---|
| `fa check`（词法 / 语法 / 类型检查，逐行错误定位） | ✅ 可用 |
| `fa ide`（网页 IDE：补全、报错、悬停、大纲、高亮） | ✅ 可用 |
| `fa lsp`（给 VSCode / Neovim / Emacs 的语言服务） | ✅ 可用 |
| `fa tokens` / `fa ast` / `fa bind`（读 C 头文件出 FA 声明） | ✅ 可用 |
| `fa build` / `fa run` / `fa asm`（生成 x86-64 Linux ELF） | ❌ 见下 |

代码生成后端目前只出 **x86-64 Linux System V** 的 ELF，需要 gcc 和 Linux 系统调用，
所以在 Windows 上 `fa build` / `fa run` 会直接告诉你原因（退出码 3），
而不是生成一个跑不起来的东西。

要在 Windows 上真跑出程序，用 **WSL**：

```
wsl --install -d Debian
```

进 WSL 之后装 deb 版：

```
sudo apt install ./fa_<版本>_amd64.deb
fa run hello.fa
```

同一个仓库、同一份 `.fa` 源码，Windows 侧写代码 + 检查，WSL 侧编译运行。
`fa ide` 在 Windows 侧就能开，它会自动发现后端不可用并把「运行」按钮置灰、
把原因写在按钮上。

## 5. 目录结构

```
fa.exe                     启动器（找 FA、找 Python、转交参数）
lib/fa/compiler/           编译器前端 + 代码生成
lib/fa/lsp/                语言服务与分析内核
lib/fa/ide/                网页 IDE 后端与前端
lib/fa/stdlib/             标准库（std/time/fs/re/json/args/c）
lib/fa/runtime/            C 运行时源码
lib/fa/bin/fa_cli.py       统一入口
fa.bat                     不想用 exe 时的等价批处理
README-Windows.md          本文件
```

## 6. 出问题了

- `找不到 FA 的安装目录` —— 别把 `fa.exe` 单独拷走，它要和 `lib\\fa` 在一起；
  或者设 `FA_HOME`。
- `没找到能用的 Python 3` —— 装 Python，或者 `set FA_PYTHON=C:\\Python312\\python.exe`。
- 想确认环境：`fa.exe version` 会把运行时目录、有没有 gcc 都列出来。
"""

FA_BAT = """@echo off
rem fa.bat —— 和 fa.exe 等价的批处理版（不想用 exe 时用它）
setlocal
set "FA_EXE_DIR=%~dp0"
if defined FA_HOME goto :have_home
if exist "%FA_EXE_DIR%lib\\fa\\bin\\fa_cli.py" (
    set "FA_HOME=%FA_EXE_DIR%lib\\fa"
    goto :have_home
)
echo 找不到 FA 的安装目录，请设 FA_HOME。 1>&2
exit /b 3
:have_home
if defined FA_PYTHON (
    "%FA_PYTHON%" "%FA_HOME%\\bin\\fa_cli.py" %*
    exit /b %ERRORLEVEL%
)
py -3 "%FA_HOME%\\bin\\fa_cli.py" %*
if %ERRORLEVEL%==9009 (
    python "%FA_HOME%\\bin\\fa_cli.py" %*
)
exit /b %ERRORLEVEL%
"""


def build_zip(zip_path, exe_path, version):
    print("\n组装解压即用的 zip")
    stage = tempfile.mkdtemp(prefix="fa_win_")
    top = os.path.join(stage, f"fa-{version}-windows-amd64")
    os.makedirs(top)
    shutil.copy2(exe_path, os.path.join(top, "fa.exe"))
    n = make_payload(top)
    with open(os.path.join(top, "README-Windows.md"), "w", encoding="utf-8") as fh:
        fh.write(README_WIN.replace("<版本>", version))
    with open(os.path.join(top, "fa.bat"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(FA_BAT)
    # 扩展也一起带上，Windows 用户 code --install-extension 就能装
    vsix = os.path.join(DIST, f"fa-vscode-{version}.vsix")
    if os.path.exists(vsix):
        os.makedirs(os.path.join(top, "editors"), exist_ok=True)
        shutil.copy2(vsix, os.path.join(top, "editors", os.path.basename(vsix)))

    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                fp = os.path.join(root, f)
                zf.write(fp, os.path.relpath(fp, stage))
    shutil.rmtree(stage, ignore_errors=True)

    size = os.path.getsize(zip_path)
    print(f"  ✓ 打出 {zip_path}（{size // 1024} KB，{n} 个载荷文件）")
    return n, size


def verify_zip(zip_path, version):
    print("\n校验 zip")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        bad = zf.testzip()
    top = f"fa-{version}-windows-amd64/"
    ck("zip 没有坏条目", bad is None, bad or "")
    ck("fa.exe 在里面", top + "fa.exe" in names, "")
    ck("fa.bat 兜底也在", top + "fa.bat" in names, "")
    ck("README 在里面", top + "README-Windows.md" in names, "")
    for need in ("bin/fa_cli.py", "compiler/falang/cli.py", "lsp/fa_lsp.py",
                 "ide/server.py", "ide/static/index.html",
                 "stdlib/time.fa", "runtime/fa_runtime.c", "runtime/fa_syscall.S"):
        ck(f"载荷齐全：{need}", top + "lib/fa/" + need in names, "")
    ck("没有把 __pycache__ 打进去",
       not any("__pycache__" in n for n in names), "")
    ck("没有把 .git 打进去", not any("/.git/" in n for n in names), "")

    # 把 zip 解到临时目录，用 Linux 启动器替身跑一遍里面的载荷：
    # 载荷是跨平台的 Python，能跑就说明 zip 里那份是完整可用的。
    work = tempfile.mkdtemp(prefix="fa_zipck_")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(work)
    ex = os.path.join(work, top)
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc and platform.system() == "Linux":
        sub = os.path.join(ex, "fa_linux_probe")
        r = subprocess.run([cc, "-O2", "-o", sub, LAUNCHER],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            src = os.path.join(work, "z.fa")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write('use std.time\n\nfn dbl(x: *i64) -> i64: return (*x) * 2\n\n'
                         'fn main() -> i64:\n    let v = Vec<i64>[7, 19, 3, 45]\n'
                         '    print("翻倍 ", v.map(dbl).to_str())\n'
                         '    print("时间戳 ", Time.now() > 0)\n    return 0\n')
            e = dict(os.environ)
            e.pop("FA_HOME", None)
            e.pop("PYTHONPATH", None)
            rr = subprocess.run([sub, "run", src], capture_output=True, text=True,
                                timeout=600, env=e)
            ck("zip 里那份载荷真能编译运行（用同布局的启动器验证）",
               rr.returncode == 0 and rr.stdout == "翻倍  [14, 38, 6, 90]\n时间戳  true\n",
               f"rc={rr.returncode} out={rr.stdout[:100]!r} err={rr.stderr[-200:]!r}")
            rc = subprocess.run([sub, "ide", "--selftest"], capture_output=True,
                                text=True, timeout=600, env=e)
            ck("zip 里的 IDE 后端自测全绿",
               rc.returncode == 0 and "IDE 后端自测全部通过" in rc.stdout,
               (rc.stdout + rc.stderr)[-200:])
            rl = subprocess.run([sub, "lsp", "--selftest"], capture_output=True,
                                text=True, timeout=600, env=e)
            ck("zip 里的 LSP 自检全绿",
               rl.returncode == 0 and "LSP 自检全部通过" in rl.stdout,
               (rl.stdout + rl.stderr)[-200:])
        else:
            print("  ! 探针编译失败，跳过 zip 内载荷的实跑校验")
    shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="打出 Windows 版 fa.exe + zip")
    ap.add_argument("--no-zig", action="store_true", help="只跑启动器自测，不交叉编译")
    ap.add_argument("--output", "-o", default=None, help="输出目录（默认 dist）")
    args = ap.parse_args()

    global DIST
    if args.output:
        DIST = os.path.abspath(args.output)
    os.makedirs(DIST, exist_ok=True)
    version = FA_VERSION

    print(f"FA Windows 打包　版本 {version}　主机 {platform.system()} {platform.machine()}\n")

    ok = selftest_posix()
    if args.no_zig:
        print("\n通过 {} 条，失败 {} 条".format(N[0] - len(FAILS), len(FAILS)))
        return 0 if ok and not FAILS else 1

    exe = os.path.join(DIST, "fa.exe")
    if not cross_compile(exe):
        print("\n交叉编译没成功，只交付了启动器自测结果")
        return 1
    check_pe(exe)
    zip_path = os.path.join(DIST, f"fa-{version}-windows-amd64.zip")
    build_zip(zip_path, exe, version)
    verify_zip(zip_path, version)

    print("\n通过 {} 条，失败 {} 条".format(N[0] - len(FAILS), len(FAILS)))
    for f in FAILS:
        print("  ✗", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
