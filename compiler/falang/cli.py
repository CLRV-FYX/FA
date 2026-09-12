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
  asm      只输出 x86-64 汇编（.s）
  check    只做语法/类型检查，不生成代码
  tokens   转储词法分析结果（自举比对用）
  ast      转储语法树（自举比对用）
  version  显示版本与环境信息

选项:
  -o <路径>    指定输出路径
  -O <0-3>     优化级别（默认 2）
  -v           显示详细过程
  -k           保留中间产物
  --emit-asm   额外导出汇编文件

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
        from .driver import py_config, java_config, RUNTIME_DIR
        pyc, _ = py_config()
        _, _, jh = java_config()
        print("FA 编译器 0.1.0 (bootstrap: python)  目标: x86-64 Linux System V")
        print(f"  运行时目录 : {RUNTIME_DIR}")
        print(f"  CPython    : {pyc or '未检测到'}")
        print(f"  JVM        : {jh or '未检测到'}")
        return 0

    cmd = argv[0]
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

    rc = build(src, out, emit_asm=(emit_asm or cmd == "asm"), opt=opt,
               run=(cmd == "run"), keep=keep, verbose=verbose)
    return rc


if __name__ == "__main__":
    sys.exit(main())
