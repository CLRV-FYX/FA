#!/usr/bin/env python3
"""检查 docs/ 里的内部链接：锚点对不对得上标题、相对路径的文件在不在。

    python3 tools/check_doc_links.py            # 检查 docs/ 下所有 .md
    python3 tools/check_doc_links.py docs/08_完全教程.md

锚点按 GitHub 的规则算：标题转小写、去掉标点、空格换连字符（中日韩文字保留）。
还没写的章节会被报出来 —— 那是提醒你去写，不是让你把链接删掉。
"""

from __future__ import annotations
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")


def slug(title: str) -> str:
    t = title.strip().lower()
    t = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", t)
    return t.replace(" ", "-")


def strip_code(text: str) -> str:
    """代码块里的 # 和 []() 不是链接，先挖掉。"""
    return re.sub(r"```.*?```", "", text, flags=re.S)


def anchors_of(path: str) -> set:
    out = set()
    for line in strip_code(open(path, encoding="utf-8").read()).split("\n"):
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            out.add(slug(m.group(1)))
    return out


def main(argv) -> int:
    files = [a for a in argv if not a.startswith("-")]
    if not files:
        files = [os.path.join(DOCS, f) for f in sorted(os.listdir(DOCS)) if f.endswith(".md")]
    cache = {}
    bad = 0
    for path in files:
        if path not in cache:
            cache[path] = anchors_of(path)
        rel = os.path.relpath(path, ROOT)
        for m in re.finditer(r"\]\(([^)\s]+)\)", strip_code(open(path, encoding="utf-8").read())):
            link = m.group(1)
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            line = open(path, encoding="utf-8").read()[:m.start()].count("\n") + 1
            if link.startswith("#"):
                if link[1:] not in cache[path]:
                    print(f"  ✗ {rel}:{line} 锚点不存在 {link}")
                    bad += 1
            else:
                f, _, frag = link.partition("#")
                target = os.path.normpath(os.path.join(os.path.dirname(path), f))
                if not os.path.exists(target):
                    print(f"  ✗ {rel}:{line} 文件不存在 {f}")
                    bad += 1
                elif frag and target.endswith(".md"):
                    if target not in cache:
                        cache[target] = anchors_of(target)
                    if frag not in cache[target]:
                        print(f"  ✗ {rel}:{line} {f} 里没有锚点 #{frag}")
                        bad += 1
    if bad:
        print(f"\n内部链接 {bad} 处对不上")
        return 1
    print("内部链接全部对得上")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
