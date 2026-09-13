"""FA 命令行：fa build / fa run / fa asm / fa check / fa version"""

from __future__ import annotations
import os
import sys
import shutil

USAGE = """FA (FYX-all) 编译器  ——  功能完全 + 极致速度

用法:
  fa <命令> [选项] <文件.fa> [-- 程序参数...]

命令:
  build    编译为可执行文件（默认输出同名可执行文件）
  run      编译并立即运行
  asm      只输出 x86-64 汇编（打到 stdout，并存一份 <源文件名>.s；-o 可指定路径）
  check    只做语法/类型检查，不生成代码
  bind     把 C 头文件自动翻成 FA 的绑定（`fa bind zlib.h --lib z -o zlib.fa`）
  tokens   转储词法分析结果（自举比对用）
  ast      转储语法树（自举比对用）
  version  显示版本与环境信息

选项:
  -o <路径>    指定输出路径
  -O <0-3>     优化级别（默认 2）
  -v           显示详细过程
  -k           保留中间产物
  --emit-asm   build 时额外导出一份汇编文件

示例:
  fa run hello.fa
  fa build -o /tmp/app main.fa
  fa asm fib.fa
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] == "version":
        from .driver import py_available, java_config, RUNTIME_DIR, cc, cxx
        import shutil as _sh
        pyok, pyinfo = py_available()
        _, _, jh = java_config()
        print("FA 编译器 0.2.0 (bootstrap: python)  目标: x86-64 Linux System V")
        print(f"  运行时目录 : {RUNTIME_DIR}")
        print(f"  C 编译器   : {cc()}{'（未找到）' if _sh.which(cc()) is None else ''}"
              f"   C++: {cxx()}{'（未找到）' if _sh.which(cxx()) is None else ''}")
        print(f"  CPython    : {'可用 ' + pyinfo if pyok else '不可用（use py 会报错并给出安装建议）'}")
        print(f"  JVM        : {jh or '未检测到（use java 会报错并给出安装建议）'}")
        return 0

    cmd = argv[0]
    if cmd == "bind":
        # C 头文件 → FA 绑定。参数规矩和 build/run 完全不同（收的是 .h，不是 .fa），
        # 所以在通用解析之前就走掉。
        from .bindgen import main as bind_main
        return bind_main(argv[1:])
    rest = argv[1:]
    out = None
    opt = 2
    verbose = False
    keep = False
    emit_asm = False
    files = []
    prog_args = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            prog_args = rest[i + 1:]
            break
        if a == "-o":
            out = rest[i + 1]; i += 2; continue
        if a == "-O":
            opt = int(rest[i + 1]); i += 2; continue
        if a in ("-v", "--verbose"):
            verbose = True; i += 1; continue
        if a in ("-k", "--keep"):
            keep = True; i += 1; continue
        if a == "--emit-asm":
            emit_asm = True; i += 1; continue
        files.append(a); i += 1

    if not files:
        print(USAGE)
        return 1
    src = files[0]
    if not os.path.exists(src):
        print(f"错误：找不到文件 {src}")
        return 1
    if cmd not in ("build", "run", "asm", "check", "tokens", "ast"):
        print(USAGE)
        return 1

    if cmd in ("tokens", "ast"):
        # 自举用的规范化转储：由 Python 前端产出「黄金输出」，
        # 再用 FA 重写的前端逐字节比对（见 boot/ 目录）
        if not rest:
            print(USAGE); return 1
        from .lexer import tokenize
        from .dump import dump_tokens, dump_ast
        from .parser import parse
        src = open(rest[0]).read()
        if cmd == "tokens":
            sys.stdout.write(dump_tokens(tokenize(src)))
        else:
            sys.stdout.write(dump_ast(parse(src, rest[0])))
        return 0

    from .driver import build, frontend
    if cmd == "check":
        with open(src) as f:
            r = frontend(f.read(), src, opt)
        if r.ok:
            print(f"✓ {src} 检查通过")
            return 0
        print(r.error)
        return 1

    if cmd == "asm":
        # 「只输出汇编」就真的只跑前端：不汇编、不链接（比以前快一个量级），
        # 汇编写到 stdout（可以直接 `fa asm x.fa | less`），同时在源码旁存一份 x.s。
        # 给了 -o 就只写 -o 指定的文件，stdout 只留一行提示。
        with open(src) as f:
            r = frontend(f.read(), src, opt)
        if not r.ok:
            print(f"[{r.stage}] {r.error}")
            return 1
        if out:
            with open(out, "w") as f:
                f.write(r.asm)
            print(f"✓ 汇编已写入 {out}（{r.asm.count(chr(10))} 行）")
        else:
            side = os.path.join(os.path.dirname(os.path.abspath(src)),
                                os.path.splitext(os.path.basename(src))[0] + ".s")
            with open(side, "w") as f:
                f.write(r.asm)
            sys.stdout.write(r.asm)
            sys.stderr.write(f"[FA] 同时存了一份 -> {side}\n")
        return 0

    if cmd == "run":
        # 走临时目录：不在源码旁留可执行文件与 .fa_work/，并且把 `--` 之后的参数传给程序
        from .driver import run_file
        return run_file(src, prog_args, opt=opt, keep=keep, verbose=verbose)
    rc = build(src, out, emit_asm=emit_asm, opt=opt,
               run=False, keep=keep, verbose=verbose)
    return rc


if __name__ == "__main__":
    sys.exit(main())
