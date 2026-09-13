#!/usr/bin/env python3
"""把教程里 `@@OUT@@` 占位符换成程序的真实输出。

写教程时最怕的就是「输出是手打的、看着像真的」。这个脚本的规矩跟
tests/check_docs.py 完全一致：找到每个 ```fa 块，如果紧跟的 ```text 块内容
正好是 `@@OUT@@`，就真的编译运行一遍，把 **stdout**（不含 stderr）逐字填进去。
退出码非零 / 编译失败的块不会被填，而是列出来让人去改文档。

    python3 tools/fill_doc_outputs.py                 # 处理 docs/*.md
    python3 tools/fill_doc_outputs.py docs/08_完全教程.md
    python3 tools/fill_doc_outputs.py --dry-run       # 只报告，不写回

填完照例要跑 `python3 tests/check_docs.py` 复核一遍。
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 代码组里不许出现 ```，否则会跨块匹配：一个后面跟着**普通** text 块的 fa 块，
# 会一路吞掉中间的 markdown 直到找到下一个 @@OUT@@（实测抓到一堆反引号，
# 报「无法识别的字符 '`'」，看着像文档写错了，其实是正则写错了）。
BLOCK = re.compile(r"```fa\n((?:(?!```)[\s\S])*)```\n\n```text\n@@OUT@@\n```")


def run(src: str):
    """编译并运行一段源码，返回 (退出码, stdout, 诊断信息)。"""
    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "_fill.fa")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        env = dict(os.environ, FA_FLUSH="1")
        r = subprocess.run(["bash", os.path.join(ROOT, "bin/fa"), "run", path],
                           capture_output=True, text=True, cwd=work, env=env)
        diag = (r.stdout + r.stderr).strip()
        return r.returncode, r.stdout.rstrip("\n"), diag


def main(argv):
    dry = "--dry-run" in argv
    files = [a for a in argv if not a.startswith("-")]
    if not files:
        d = os.path.join(ROOT, "docs")
        files = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".md")]
    total = filled = 0
    bad = []
    for path in files:
        rel = os.path.relpath(path, ROOT)
        text = open(path, encoding="utf-8").read()
        if "@@OUT@@" not in text:
            continue

        def sub(m):
            nonlocal total, filled
            total += 1
            code = m.group(1)
            line = text[:m.start()].count("\n") + 1
            rc, out, diag = run(code)
            if rc != 0:
                bad.append((rel, line, diag.split("\n")[0][:160]))
                return m.group(0)
            filled += 1
            first = code.strip("\n").split("\n")[0][:60]
            print(f"  ✓ {rel}:{line}  {len(out.splitlines())} 行输出   | {first}")
            return "```fa\n" + code + "```\n\n```text\n" + out + "\n```"

        new = BLOCK.sub(sub, text)
        if new != text and not dry:
            open(path, "w", encoding="utf-8").write(new)
    print(f"\n占位符 {total} 个：填好 {filled}，失败 {len(bad)}")
    for rel, line, why in bad:
        print(f"  ✗ {rel}:{line}  {why}")
    left = 0
    for path in files:
        if os.path.exists(path):
            left += open(path, encoding="utf-8").read().count("@@OUT@@")
    if left:
        print(f"文档里还剩 {left} 个 @@OUT@@ 没填")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
