#!/usr/bin/env python3
"""文档里「反面教材」的报错也要是真的。

tests/check_docs.py 会跳过带 ❌ 的代码块（它们本来就编译不过 / 运行会 panic），
于是那些块后面印的**报错文本**没人管：改了报错措辞、动了行列号，文档就悄悄对不上了。
这个脚本补上这一块 —— 它把每个 ❌ 块真的交给编译器（能编译的就真的跑一遍），
把输出和文档里印的逐字比对。

    python3 tools/check_doc_errors.py               # 扫 docs/*.md
    python3 tools/check_doc_errors.py docs/08_完全教程.md
    python3 tools/check_doc_errors.py -v            # 连对上的也打出来
    python3 tools/check_doc_errors.py --fix         # 把对不上的直接换成真实输出

比对规则：
  * 编译器输出开头的 `[语义分析] ` / `[语法分析] ` / `[代码生成] ` 前缀不算，
    文档里按惯例只印后面那部分；
  * 文档里印的可以是**前若干行**（报错后面的「提示：…」之类不用抄），
    所以按「文档行数」逐行比；
  * 运行期 panic 的块（`fa check` 过得去）比的是 stderr + stdout 合并后的前几行。
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ```fa 块（含 ❌）紧跟一个 ```text 块
NEG = re.compile(r"```fa\n((?:(?!```)[\s\S])*❌(?:(?!```)[\s\S])*)```\n\n```text\n"
                 r"((?:(?!```)[\s\S])*)```")
STAGE = re.compile(r"^\[(?:语法分析|语义分析|代码生成|运行)\]\s*")


def fa(*args, cwd):
    env = dict(os.environ, FA_FLUSH="1")
    r = subprocess.run(["bash", os.path.join(ROOT, "bin/fa")] + list(args),
                       capture_output=True, text=True, cwd=cwd, env=env)
    return r.returncode, (r.stdout + r.stderr)


MULTIFILE = re.compile(r"^//\s*(\S+\.fa)\s*$", re.M)


def real_output(code: str):
    """这段反面教材的真实输出：编译不过就是报错，编得过就真跑一遍看它 panic 什么。"""
    with tempfile.TemporaryDirectory() as work:
        # 一个块里写了多个文件（`// dup.fa` 接着 `// main.fa`）：和 tests/check_docs.py
        # 同一套规矩 —— 各写各的文件，查最后那个（主文件），并把 use 的路径改成
        # 刚写出来的绝对路径。以前这里不分文件，多文件反面教材只会报
        # 「找不到要导入的 FA 模块」，而模块路径里还带着一个每次都变的临时目录名，
        # 于是这个块永远对不上。
        parts = MULTIFILE.split(code)
        if len(parts) > 1:
            for i in range(1, len(parts) - 1, 2):
                with open(os.path.join(work, parts[i]), "w", encoding="utf-8") as f:
                    f.write(parts[i + 1])
            code = re.sub(r'use\s+"([^"]+\.fa)"',
                          lambda mm: 'use "%s"' % os.path.join(work, mm.group(1)),
                          parts[-1])
        src = os.path.join(work, "_neg.fa")
        open(src, "w", encoding="utf-8").write(code)
        rc, out = fa("check", src, cwd=work)
        if rc == 0:
            rc, out = fa("run", src, cwd=work)
        return strip_stage(out).rstrip("\n")


def strip_stage(text: str) -> str:
    out = []
    for line in text.split("\n"):
        out.append(STAGE.sub("", line))
    return "\n".join(out)


def main(argv):
    verbose = "-v" in argv
    fix = "--fix" in argv
    files = [a for a in argv if not a.startswith("-")]
    if not files:
        d = os.path.join(ROOT, "docs")
        files = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".md")]
    total = ok = 0
    bad = []
    for path in files:
        rel = os.path.relpath(path, ROOT)
        text = open(path, encoding="utf-8").read()
        fixed = 0

        def repl(m):
            nonlocal fixed
            code, want = m.group(1), m.group(2).rstrip("\n")
            if not want.strip():
                return m.group(0)
            got = real_output(code)
            if got is None or got == want:
                return m.group(0)
            # 文档里印的可能是**前几行**（长报错只抄开头）：这种情况只要前缀对得上
            # 就不动，免得把作者故意截短的地方拉长
            wl, gl = want.split("\n"), got.split("\n")
            if len(gl) >= len(wl) and gl[:len(wl)] == wl:
                return m.group(0)
            fixed += 1
            return "```fa\n" + code + "```\n\n```text\n" + got + "\n```"

        if fix:
            new = NEG.sub(repl, text)
            if new != text:
                open(path, "w", encoding="utf-8").write(new)
                print(f"  {rel}: 按真实输出改了 {fixed} 处")
            text = new
        for m in NEG.finditer(text):
            code, want = m.group(1), m.group(2).rstrip("\n")
            if not want.strip():
                continue                      # 只写了 ❌ 没印报错的，不管
            total += 1
            line = text[:m.start()].count("\n") + 1
            got = real_output(code)
            want_lines = want.split("\n")
            got_lines = got.split("\n")
            same = (len(got_lines) >= len(want_lines)
                    and got_lines[:len(want_lines)] == want_lines)
            if same:
                ok += 1
                if verbose:
                    print(f"  ✓ {rel}:{line}")
            else:
                bad.append((rel, line, want, got))
    for rel, line, want, got in bad:
        print(f"  ✗ {rel}:{line}")
        print("    文档印的：")
        for l in want.split("\n")[:6]:
            print(f"      | {l}")
        print("    实际输出：")
        for l in got.split("\n")[:6]:
            print(f"      | {l}")
    print(f"\n反面教材 {total} 个：对得上 {ok}，对不上 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
