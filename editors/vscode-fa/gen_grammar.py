#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从编译器里读出真实的表，生成 VSCode 的语法高亮和代码片段。

手写语法文件迟早和语言脱节（新加一个关键字，高亮就不认了）。这里反过来：
以 compiler/falang 为准生成 syntaxes/fa.tmLanguage.json；代码片段则直接取
lsp/fa_lang.py 的 SNIPPETS —— 网页 IDE 的补全和 VSCode 的 snippet 因此永远同一份。

    python3 editors/vscode-fa/gen_grammar.py     # 两个文件一起生成
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(ROOT, "compiler"), os.path.join(ROOT, "lsp")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from falang.lexer import KEYWORDS                              # noqa: E402
from falang.types import TYPES                                 # noqa: E402
from falang.sema import BUILTIN_FNS                            # noqa: E402

# 这几个在 FA 里是「控制流/逻辑」性质，跟声明类关键字分两种颜色
CONTROL = {"if", "elif", "else", "for", "while", "loop", "match", "return",
           "break", "continue", "and", "or", "not", "in"}
DECL = {"fn", "struct", "enum", "impl", "const", "let", "use", "extern",
        "trait", "mod", "pub", "static", "unsafe", "mut", "ref", "move",
        "defer", "new", "where", "dyn", "cxx", "py", "java", "libc", "asm",
        "try", "catch", "raise"}


def alternation(words):
    return r"(?<![\w.])(" + "|".join(sorted(words)) + r")(?![\w])"


def main():
    types = sorted(set(TYPES) | {"Vec", "Map"})
    builtins = sorted(BUILTIN_FNS)
    control = sorted(CONTROL & set(KEYWORDS))
    decl = sorted(DECL & set(KEYWORDS))
    other_kw = sorted(set(KEYWORDS) - CONTROL - DECL)

    g = {
        "$schema": "https://raw.githubusercontent.com/martinring/tmlanguage/master/tmlanguage.json",
        "name": "FA",
        "scopeName": "source.fa",
        "_generated": "这个文件由 editors/vscode-fa/gen_grammar.py 从编译器生成，别手改",
        "patterns": [
            {"include": "#comments"},
            {"include": "#strings"},
            {"include": "#chars"},
            {"include": "#numbers"},
            {"include": "#decls"},
            {"include": "#keywords-control"},
            {"include": "#keywords-decl"},
            {"include": "#keywords-other"},
            {"include": "#types"},
            {"include": "#constants"},
            {"include": "#builtins"},
            {"include": "#calls"},
            {"include": "#allcaps"},
            {"include": "#operators"},
        ],
        "repository": {
            "comments": {
                "patterns": [
                    {"name": "comment.line.number-sign.fa", "match": r"#.*$"},
                    {"name": "comment.line.double-slash.fa", "match": r"//.*$"},
                ]
            },
            "strings": {
                "patterns": [
                    {
                        "name": "string.quoted.triple.fa",
                        "begin": '"""', "end": '"""',
                        "patterns": [
                            {"name": "constant.character.escape.fa", "match": r"\\."},
                            {"include": "#interp"},
                        ],
                    },
                    {
                        "name": "string.quoted.double.fa",
                        "begin": '"', "end": '"',
                        "patterns": [
                            {"name": "constant.character.escape.fa", "match": r"\\."},
                            {"include": "#interp"},
                        ],
                    },
                ]
            },
            # FA 的字符串插值：print("共 {n} 个")；\{ 是字面花括号
            "interp": {
                "patterns": [
                    {
                        "name": "meta.interpolation.fa",
                        "begin": r"(?<!\\)\{", "end": r"\}",
                        "beginCaptures": {"0": {"name": "punctuation.section.interpolation.begin.fa"}},
                        "endCaptures": {"0": {"name": "punctuation.section.interpolation.end.fa"}},
                        "patterns": [
                            {"include": "#calls"},
                            {"include": "#numbers"},
                            {"include": "#constants"},
                            {"name": "variable.other.interpolation.fa",
                             "match": r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*"},
                        ],
                    }
                ]
            },
            "chars": {
                "patterns": [
                    {"name": "string.quoted.single.fa",
                     "match": r"'(\\.|[^'\\])'"},
                ]
            },
            "numbers": {
                "patterns": [
                    {"name": "constant.numeric.hex.fa", "match": r"(?<![\w.])0[xX][0-9a-fA-F_]+(?![\w])"},
                    {"name": "constant.numeric.binary.fa", "match": r"(?<![\w.])0[bB][01_]+(?![\w])"},
                    {"name": "constant.numeric.float.fa",
                     "match": r"(?<![\w.])\d[\d_]*\.\d[\d_]*([eE][+-]?\d+)?(?![\w])"},
                    {"name": "constant.numeric.float.fa",
                     "match": r"(?<![\w.])\d[\d_]*[eE][+-]?\d+(?![\w])"},
                    {"name": "constant.numeric.integer.fa", "match": r"(?<![\w.])\d[\d_]*(?![\w.])"},
                ]
            },
            "decls": {
                "patterns": [
                    # fn 名字( —— 函数名
                    {"match": r"(?<![\w.])(fn)\s+([A-Za-z_]\w*)",
                     "captures": {
                         "1": {"name": "keyword.declaration.function.fa"},
                         "2": {"name": "entity.name.function.fa"}}},
                    # struct / enum / impl / trait 名字:
                    {"match": r"(?<![\w.])(struct|enum|impl|trait)\s+([A-Za-z_]\w*)",
                     "captures": {
                         "1": {"name": "keyword.declaration.type.fa"},
                         "2": {"name": "entity.name.type.fa"}}},
                ]
            },
            "keywords-control": {
                "patterns": [{"name": "keyword.control.fa", "match": alternation(control)}]
            },
            "keywords-decl": {
                "patterns": [{"name": "keyword.declaration.fa", "match": alternation(decl)}]
            },
            "keywords-other": {
                "patterns": [{"name": "keyword.other.fa", "match": alternation(other_kw)}]
            },
            "types": {
                "patterns": [
                    {"name": "support.type.primitive.fa", "match": alternation(types)},
                    # 首字母大写的标识符多半是自己定义的类型（struct Person / enum Shape）
                    {"name": "entity.name.type.user.fa", "match": r"(?<![\w.])([A-Z][A-Za-z0-9_]*)(?![\w])"},
                ]
            },
            "constants": {
                "patterns": [{"name": "constant.language.fa", "match": alternation(["true", "false", "nil"])}]
            },
            "builtins": {
                "patterns": [{"name": "support.function.builtin.fa", "match": alternation(builtins)}]
            },
            "calls": {
                "patterns": [
                    {"match": r"([A-Za-z_]\w*)\s*(?=\()",
                     "captures": {"1": {"name": "entity.name.function.call.fa"}}},
                    # 方法调用 v.map( —— 点号后面的名字
                    {"match": r"(?<=\.)\s*([A-Za-z_]\w*)\s*(?=\()",
                     "captures": {"1": {"name": "entity.name.function.method.fa"}}},
                ]
            },
            "allcaps": {
                "patterns": [{"name": "constant.other.allcaps.fa",
                              "match": r"(?<![\w.])[A-Z][A-Z0-9_]{1,}(?![\w])"}]
            },
            "operators": {
                "patterns": [{"name": "keyword.operator.fa",
                              "match": r"(->|\+=|-=|\*=|/=|%=|<=|>=|==|!=|\.\.|[-+*/%=<>!&|^~:])"}]
            },
        },
    }

    out = os.path.join(HERE, "syntaxes", "fa.tmLanguage.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(g, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✓ 写出 {os.path.relpath(out, ROOT)}")
    print(f"  控制流关键字 {len(control)} 个，声明关键字 {len(decl)} 个，其他关键字 {len(other_kw)} 个")
    print(f"  类型 {len(types)} 个，内建函数 {len(builtins)} 个")

    # ---- 代码片段：直接取语言内核那份，网页 IDE 和 VSCode 用同一套 ----
    import fa_lang as F
    snips = {}
    for label, body, why in F.SNIPPETS:
        snips[label] = {
            "prefix": label,
            "body": body.split("\n"),
            "description": why,
        }
    # 再补几个补全列表里没有、但天天要写的
    snips["hof"] = {
        "prefix": "hof",
        "body": ["fn ${1:回调}(p: *${2:i64}) -> ${3:bool}: return ${4:p[] > 0}",
                 "${5:v}.${6|filter,map,any,all,index_where,for_each|}($1)"],
        "description": "高阶函数：先写顶层回调，再 filter/map/any/all（FA 没有 lambda）",
    }
    snips["extern"] = {
        "prefix": "externfn",
        "body": ['extern "C" fn ${1:strlen}(s: *char) -> ${2:usize}'],
        "description": "声明一个 C 函数（配合 use c 头文件 lib 库名）",
    }
    sout = os.path.join(HERE, "snippets", "fa.json")
    with open(sout, "w", encoding="utf-8") as f:
        json.dump(snips, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✓ 写出 {os.path.relpath(sout, ROOT)}（{len(snips)} 个片段）")
    return 0


def check():
    """--check：重新生成一遍，和仓库里已有的比。

    词表（关键字/类型/内建函数）是从编译器里读的，snippet 是从 lsp/fa_lang.py 读的。
    改了语言却忘了重跑这个生成器，语法高亮就会悄悄漏词 —— 肉眼看不出来，
    所以让 CI 来盯：不一致就失败，并把差异打出来。
    比完会把原文件还原，--check 不该在仓库里留下改动。
    """
    targets = [os.path.join(HERE, "syntaxes", "fa.tmLanguage.json"),
               os.path.join(HERE, "snippets", "fa.json")]
    before = {}
    for t in targets:
        with open(t, encoding="utf-8") as fh:
            before[t] = fh.read()
    rc = main()
    diffs = []
    for t in targets:
        with open(t, encoding="utf-8") as fh:
            after = fh.read()
        if after != before[t]:
            diffs.append(t)
        with open(t, "w", encoding="utf-8") as fh:      # 还原，别把仓库写脏
            fh.write(before[t])
    if rc != 0:
        print("✗ 生成器自己就报错了")
        return rc
    if diffs:
        print("✗ 这些文件是旧的，请重跑 python3 editors/vscode-fa/gen_grammar.py：")
        for d in diffs:
            print("   ", os.path.relpath(d, ROOT))
        return 1
    print("✓ 语法高亮与代码片段都和编译器/语言内核对得上（没有旧文件）")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    sys.exit(main())
