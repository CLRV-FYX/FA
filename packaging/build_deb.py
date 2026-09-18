#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打 .deb：把 FA 编译器 + 网页版 IDE + LSP 语言服务器装进 /usr/lib/fa，
/usr/bin/fa 是个 12 行的 wrapper。

    python3 packaging/build_deb.py                  # 出 dist/fa_<版本>_amd64.deb
    python3 packaging/build_deb.py --verify-only    # 只验已有的包
    python3 packaging/build_deb.py --version 1.2.3

不需要 root：dpkg-deb --build 普通用户就能打包，装的时候才要 sudo dpkg -i。
打完会**自己验一遍**：解包到临时前缀，跑 fa version / check / run / lsp --selftest，
把输出和仓库里直接跑的结果逐字节比对（包装错了这一步就会露出来）。
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from version import VERSION as FA_VERSION      # noqa: E402  版本号只有一个来源

# 要装进包里的东西（源目录 -> 包内 /usr/lib/fa 下的相对路径）
PAYLOAD = [
    ("compiler", "compiler"),
    ("lsp", "lsp"),
    ("ide", "ide"),
    ("stdlib", "stdlib"),
    ("runtime", "runtime"),
    ("bin/fa_cli.py", "bin/fa_cli.py"),
    ("README.md", "share/README.md"),
    ("docs/08_完全教程.md", "share/docs/08_完全教程.md"),
    ("docs/02_语言参考.md", "share/docs/02_语言参考.md"),
    ("docs/03_标准库.md", "share/docs/03_标准库.md"),
    ("LICENSE", "share/LICENSE"),
]
# 这些不装：中间产物、缓存、编辑器自己的东西
SKIP_DIRS = {"__pycache__", ".fa_work", ".git", "node_modules", ".mypy_cache", ".pytest_cache"}
SKIP_EXT = {".pyc", ".o", ".out", ".s", ".so", ".log"}
# runtime/ 里全是**手写源码**，包括 fa_syscall.S（真汇编源文件，不是 fa asm 的产物）。
# 第一版按扩展名一刀切，把 .S 当成中间产物跳过了，装好的包一跑 build 就报
# 「运行时编译失败」—— 找不到 fa_syscall.S。所以这个目录只按目录名筛。
NEVER_SKIP_BY_EXT = {"runtime"}

WRAPPER = """#!/bin/sh
# FA (FYX-all) 编译器 —— Debian 包装的入口
# 解析自己的真实位置，所以解包到任意前缀都能跑（打包脚本的自测就靠这个）
set -e
SELF="$0"
if command -v readlink >/dev/null 2>&1; then
    SELF="$(readlink -f "$SELF" 2>/dev/null || echo "$SELF")"
fi
BIN="$(cd "$(dirname "$SELF")" && pwd -P)"
# /usr/bin/fa -> /usr/lib/fa：只差一级。以前写成 ../../lib/fa，装好了也找不到，
# 而且 stderr 是空的（sh 直接 127 退出），排查了半天才发现是路径算错。
FA_ROOT="${FA_HOME:-$(cd "$BIN/../lib/fa" 2>/dev/null && pwd -P)}"
if [ ! -f "$FA_ROOT/bin/fa_cli.py" ]; then
    echo "fa: 找不到 $FA_ROOT/bin/fa_cli.py（FA_HOME 没设对？）" >&2
    exit 127
fi
PYTHON="${FA_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python
export FA_HOME="$FA_ROOT"
export PYTHONPATH="$FA_ROOT/compiler${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING=utf-8
exec "$PYTHON" "$FA_ROOT/bin/fa_cli.py" "$@"
"""

DESKTOP = """[Desktop Entry]
Type=Application
Version=1.0
Name=FA IDE
Name[zh_CN]=FA 网页版 IDE
GenericName=Code Editor
GenericName[zh_CN]=代码编辑器
Comment=Write, check and run FA programs in the browser
Comment[zh_CN]=在浏览器里写 FA、边打边报错、F5 直接跑
Exec=fa ide
Terminal=false
Categories=Development;IDE;
StartupNotify=true
Keywords=fa;fayx;ide;compiler;
"""

CHANGELOG = """fa ({version}) stable; urgency=medium

  * FA 编译器：Vec 高阶函数（map/filter/any/all/index_where/for_each）、
    sort_by 稳定排序、标准库六个模块（fs/time/re/json/args/c.libc）
  * 网页版 IDE：fa ide —— 补全、波浪线、悬停文档、大纲、跳定义、F5 运行
  * LSP 语言服务器：fa lsp —— VSCode / Neovim / Helix 等编辑器都能接
  * IDE 级检查：块头少冒号、缩进里的 Tab、代码区的全角标点、
    && / || / :=、match 里写 case，这些编译器报得含糊的错直接点破

 -- FA Project <noreply@github.com>  Mon, 01 Jan 2024 00:00:00 +0800
"""


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def control_text(version, arch):
    return f"""Package: fa
Version: {version}
Section: devel
Priority: optional
Architecture: {arch}
Depends: python3 (>= 3.8), gcc | clang | cc
Recommends: nodejs
Maintainer: FA Project <noreply@github.com>
Homepage: https://github.com/CLRV-FYX/FA
Installed-Size: {INSTALLED_KB}
Description: FA (FYX-all) compiler with a web IDE and an LSP server
 FA is a small, indentation-sensitive language that compiles to x86-64
 assembly through a C toolchain. It has structs, enums with payloads,
 pattern matching, generics-free containers (Vec/Map), pointers, defer,
 inline asm, and first-class C/C++/Python/Java interop.
 .
 This package ships three things that share one analysis core:
  * fa build/run/asm/check/bind - the compiler and its driver
  * fa ide                      - a browser IDE (completion, squiggles,
                                  hover docs, outline, go-to-definition,
                                  one-click run) served from localhost
  * fa lsp                      - a Language Server Protocol server for
                                  VSCode, Neovim, Helix, Zed, Emacs...
 .
 Diagnostics go beyond the compiler: missing colon on a block header,
 tabs in indentation, full-width punctuation in code, && / || / :=,
 and `case` inside `match` are all reported in plain language.
 .
 Code generation targets x86-64 Linux (System V ABI) only.
"""


def should_skip(path, top=""):
    parts = path.split(os.sep)
    if any(p in SKIP_DIRS for p in parts):
        return True
    if top in NEVER_SKIP_BY_EXT:
        return False
    return os.path.splitext(path)[1].lower() in SKIP_EXT


def copy_tree(src, dst, top=""):
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, src)
            if should_skip(rel, top):
                continue
            out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(fp, out)
            n += 1
    return n


def build(version, arch, outdir):
    global INSTALLED_KB
    if not shutil.which("dpkg-deb"):
        print("✗ 没有 dpkg-deb，装不了也打不了 .deb（Debian/Ubuntu: sudo apt install dpkg）")
        return None
    stage = tempfile.mkdtemp(prefix="fa_deb_")
    try:
        deb = os.path.join(stage, "debroot")
        lib = os.path.join(deb, "usr", "lib", "fa")
        os.makedirs(os.path.join(deb, "DEBIAN"), exist_ok=True)
        os.makedirs(os.path.join(deb, "usr", "bin"), exist_ok=True)
        os.makedirs(os.path.join(deb, "usr", "share", "applications"), exist_ok=True)
        os.makedirs(os.path.join(deb, "usr", "share", "doc", "fa"), exist_ok=True)

        total = 0
        for src, dst in PAYLOAD:
            sp = os.path.join(ROOT, src)
            if not os.path.exists(sp):
                print(f"  ! 跳过不存在的 {src}")
                continue
            dp = os.path.join(lib, dst)
            if os.path.isdir(sp):
                total += copy_tree(sp, dp, top=src.split("/")[0])
            else:
                os.makedirs(os.path.dirname(dp), exist_ok=True)
                shutil.copy2(sp, dp)
                total += 1
        print(f"  装了 {total} 个文件到 usr/lib/fa")

        # 三个 wrapper：fa / fa-ide / fa-lsp
        for name, extra in (("fa", ""), ("fa-ide", "ide"), ("fa-lsp", "lsp")):
            body = WRAPPER
            if extra:
                # fa-ide / fa-lsp 直接把子命令塞进去，省得用户记
                body = body.replace('exec "$PYTHON" "$FA_ROOT/bin/fa_cli.py" "$@"',
                                    f'exec "$PYTHON" "$FA_ROOT/bin/fa_cli.py" {extra} "$@"')
            p = os.path.join(deb, "usr", "bin", name)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(p, 0o755)

        with open(os.path.join(deb, "usr/share/applications/fa-ide.desktop"), "w",
                  encoding="utf-8") as fh:
            fh.write(DESKTOP)

        size = 0
        for root, _d, files in os.walk(deb):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        INSTALLED_KB = str(max(1, size // 1024))

        with open(os.path.join(deb, "DEBIAN", "control"), "w", encoding="utf-8") as fh:
            fh.write(control_text(version, arch))
        with open(os.path.join(deb, "DEBIAN", "changelog"), "w", encoding="utf-8") as fh:
            fh.write(CHANGELOG.format(version=version))
        with open(os.path.join(deb, "DEBIAN", "copyright"), "w", encoding="utf-8") as fh:
            fh.write("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
                     "Upstream-Name: FA\nUpstream-Contact: https://github.com/CLRV-FYX/FA\n\n"
                     "Files: *\nCopyright: 2024-2026 FA Project\nLicense: MIT\n")
        postinst = os.path.join(deb, "DEBIAN", "postinst")
        with open(postinst, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nset -e\n"
                     "if command -v update-desktop-database >/dev/null 2>&1; then\n"
                     "    update-desktop-database -q /usr/share/applications || true\nfi\n"
                     "echo 'FA 装好了：fa version 看看环境，fa ide 打开网页版 IDE'\n")
        os.chmod(postinst, 0o755)

        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, f"fa_{version}_{arch}.deb")
        if os.path.exists(out):
            os.remove(out)
        r = run(["dpkg-deb", "--build", "--root-owner-group", "-Zxz", deb, out])
        if r.returncode != 0:
            print("✗ dpkg-deb 失败：\n" + r.stderr)
            return None
        kb = os.path.getsize(out) / 1024
        print(f"✓ 打出 {out}（{kb:.0f} KB，安装后约 {INSTALLED_KB} KB）")
        return out
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ------------------------------------------------------------------ 验证
def verify(deb, verbose=False):
    """解包到临时前缀，把包里的 fa 当真的一样使，和仓库里直接跑的结果比对。"""
    ok = True
    checks = []

    def ck(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        if not cond:
            print(f"  ✗ {name}　{detail}")

    print(f"\n验证 {os.path.basename(deb)}")
    r = run(["dpkg-deb", "-I", deb])
    ck("dpkg-deb -I 读得出控制信息", r.returncode == 0, r.stderr[:120])
    info = r.stdout
    for key in ("Package: fa", "Architecture:", "Depends:", "Description:"):
        ck(f"控制信息里有 {key.strip()}", key in info, info[:200])
    ck("Depends 里写了 python3", "python3" in info, info[:300])
    ck("Depends 里写了 C 编译器", ("gcc" in info or "cc" in info), info[:300])

    r = run(["dpkg-deb", "-c", deb])
    listing = r.stdout
    for need in ("./usr/bin/fa", "./usr/lib/fa/bin/fa_cli.py",
                 "./usr/lib/fa/compiler/falang/sema.py", "./usr/lib/fa/lsp/fa_lsp.py",
                 "./usr/lib/fa/lsp/fa_lang.py", "./usr/lib/fa/ide/server.py",
                 "./usr/lib/fa/ide/static/app.js", "./usr/lib/fa/stdlib/fs.fa",
                 "./usr/lib/fa/runtime/fa_runtime.c",
                 "./usr/share/applications/fa-ide.desktop"):
        ck(f"包里有 {need}", need in listing, "")
    ck("没有把 __pycache__ 打进去", "__pycache__" not in listing)
    ck("没有把 .pyc 打进去", ".pyc" not in listing)
    ck("没有把 .git 打进去", "/.git/" not in listing)
    # runtime/ 是手写源码，fa_syscall.S 少了就编不出任何程序（第一版正是漏了它）
    for need in ("./usr/lib/fa/runtime/fa_syscall.S", "./usr/lib/fa/runtime/fa_runtime.h",
                 "./usr/lib/fa/runtime/fa_python.c", "./usr/lib/fa/runtime/fa_jvm.c"):
        ck(f"运行时源码齐全：{need.split('/')[-1]}", need in listing, "")

    # 解包，用里面的 fa 真跑
    pre = tempfile.mkdtemp(prefix="fa_deb_verify_")
    try:
        r = run(["dpkg-deb", "-x", deb, pre])
        ck("能解包", r.returncode == 0, r.stderr[:120])
        fa = os.path.join(pre, "usr", "bin", "fa")
        ck("解出来的 fa 可执行", os.access(fa, os.X_OK))
        env = dict(os.environ)
        env.pop("FA_HOME", None)
        env.pop("PYTHONPATH", None)

        def pkg(args, **kw):
            return run([fa] + args, env=env, **kw)

        r = pkg(["version"])
        ck("fa version 能跑", r.returncode == 0 and "FA 编译器" in r.stdout, r.stderr[:200])
        ck("version 报的运行时目录是包里的", os.path.join(pre, "usr/lib/fa/runtime") in r.stdout,
           r.stdout[:200])

        r = pkg(["--help"])
        ck("fa --help 列出了 ide 和 lsp", "ide" in r.stdout and "lsp" in r.stdout, r.stdout[:300])

        work = tempfile.mkdtemp(prefix="fa_deb_work_")
        hello = os.path.join(work, "hello.fa")
        with open(hello, "w", encoding="utf-8") as fh:
            fh.write('use std.time\n\nfn double(x: *i64) -> i64: return (*x) * 2\n'
                     'fn is_adult(age: *i64) -> bool: return *age >= 18\n\n'
                     'fn main() -> i64:\n'
                     '    let v = Vec<i64>[7, 19, 3, 45]\n'
                     '    print("翻倍 ", v.map(double).to_str())\n'
                     '    print("成年 ", v.filter(is_adult).to_str())\n'
                     '    print("有成年 ", v.any(is_adult))\n'
                     '    print("全成年 ", v.all(is_adult))\n'
                     '    print("时间戳 ", Time.now() > 0)\n'
                     '    return 0\n')
        r = pkg(["check", hello])
        ck("包里的 fa check 通过", r.returncode == 0 and "检查通过" in r.stdout,
           (r.stdout + r.stderr)[:200])
        r = pkg(["run", hello])
        # 这些字面值是把上面那份源码在仓库里 ./bin/fa run 一遍抄下来的，
        # 不是凭印象写的（第一版写了「成年  [3, 4]」，可样本里根本没有成年人，
        # filter 返回空是对的 —— 是断言错了，不是包错了）。
        want = ("翻倍  [14, 38, 6, 90]\n成年  [19, 45]\n"
                "有成年  true\n全成年  false\n时间戳  true\n")
        ck("包里的 fa run 跑出来了", r.returncode == 0 and r.stdout == want,
           f"包={r.stdout[:120]!r}\n期望={want[:120]!r}")
        ck("stdlib 也打进去了（Time.now / 高阶函数都能用）",
           "成年  [19, 45]" in r.stdout and "时间戳  true" in r.stdout, r.stdout[:200])

        # 和仓库里直接跑的结果逐字节比对
        repo = run([os.path.join(ROOT, "bin", "fa"), "run", hello])
        ck("和仓库里跑的输出逐字节一致", repo.stdout == r.stdout,
           f"包={r.stdout[:60]!r} 仓库={repo.stdout[:60]!r}")

        # LSP：把协议自检跑一遍（用的是包里那份 fa_lsp.py）
        r = pkg(["lsp", "--selftest"])
        ck("包里的 fa lsp --selftest 全绿",
           r.returncode == 0 and "LSP 自检全部通过" in r.stdout, (r.stdout + r.stderr)[-300:])

        # IDE 后端：起在随机端口上打一遍接口
        r = pkg(["ide", "--selftest"], timeout=180)
        ck("包里的 fa ide --selftest 全绿",
           r.returncode == 0 and "IDE 后端自测全部通过" in r.stdout, (r.stdout + r.stderr)[-300:])

        # 平台守卫不该在 Linux 上拦人
        r = pkg(["asm", hello])
        ck("fa asm 在 Linux 上照常出汇编", r.returncode == 0, (r.stdout + r.stderr)[:200])
        shutil.rmtree(work, ignore_errors=True)
    finally:
        shutil.rmtree(pre, ignore_errors=True)

    good = sum(1 for _n, c, _d in checks if c)
    print(f"  {good} / {len(checks)} 条通过")
    return good == len(checks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=FA_VERSION,
                    help="包版本号，默认取 falang.__version__")
    ap.add_argument("--arch", default="amd64")
    ap.add_argument("--outdir", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--verify-only", default="")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args()

    deb = a.verify_only or build(a.version, a.arch, a.outdir)
    if not deb:
        return 1
    if a.no_verify:
        return 0
    return 0 if verify(deb) else 1


INSTALLED_KB = "1"

if __name__ == "__main__":
    sys.exit(main())
