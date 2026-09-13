"""
FA 词法分析器 (Lexer)
=====================
设计要点
--------
1. 双模式分块：默认「缩进模式」（Python 风格），遇到 `{` 后进入「括号模式」，
   此时换行/缩进被完全忽略，语句必须用 `;` 分隔（C 风格）。
   这样同一套语法既能让零基础用户写缩进，也能让 C/Java 老手写大括号。
2. 括号内的换行一律忽略（自动续行），因此长表达式可以直接折行。
3. 字符串支持 `{expr}` 插值，词法层保留原文，由 parser 二次切分。
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List

# ---------------------------------------------------------------- 关键字
KEYWORDS = {
    "fn", "let", "mut", "const", "return", "if", "elif", "else", "while",
    "for", "in", "loop", "break", "continue", "struct", "enum", "match",
    "use", "as", "extern", "defer", "true", "false", "nil", "and", "or",
    "not", "new", "impl", "pub", "sizeof", "typeof", "unsafe", "cxx",
    "py", "java", "libc", "static", "where", "raise", "try", "catch",
    "asm", "ref", "move", "dyn", "trait", "mod",
}

NUM_SUFFIX = {"i8", "i16", "i32", "i64", "isize",
              "u8", "u16", "u32", "u64", "usize",
              "f32", "f64"}

PRIMITIVE_TYPES = {
    "i8", "i16", "i32", "i64", "isize",
    "u8", "u16", "u32", "u64", "usize",
    "f32", "f64", "bool", "char", "str", "void", "any",
}

# 按长度降序匹配，避免 `>=` 被切成 `>` `=`
OPERATORS = [
    "**=", ">>=", "<<=", "...", "..=", "->", "=>", "==", "!=", "<=",
    ">=", "&&", "||", "::", "**", ">>", "<<", "+=", "-=", "*=", "/=",
    "%=", "&=", "|=", "^=", "..",
    "+", "-", "*", "/", "%", "=", "<", ">", "!", "&", "|", "^", "~", "?", "@",
]

PUNCT = "()[]{},;:."

@dataclass
class Token:
    kind: str          # NUM / FNUM / STR / CHAR / NAME / OP / PUNCT / NEWLINE / INDENT / DEDENT / EOF
    value: object
    line: int
    col: int
    suffix: str = ""   # 数字字面量的类型后缀，如 3.5f32 / 42i8
    raw: str = ""      # 字面量在源码里的原文（自举比对用，避免浮点格式差异）

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{self.kind} {self.value!r} @{self.line}:{self.col}>"


class FaSyntaxError(Exception):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        super().__init__(msg)
        self.msg, self.line, self.col = msg, line, col

    def pretty(self, src: str = "") -> str:
        head = f"语法错误 (行 {self.line}, 列 {self.col}): {self.msg}"
        if src:
            lines = src.split("\n")
            if 1 <= self.line <= len(lines):
                head += "\n    " + lines[self.line - 1]
                head += "\n    " + " " * max(0, self.col - 1) + "^"
        return head


DIGITS = "0123456789"
HEXD = "0123456789abcdefABCDEF"

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\",
           '"': '"', "'": "'", "{": "{", "}": "}", "a": "\a", "b": "\b", "f": "\f"}


def tokenize(src: str) -> List[Token]:
    toks: List[Token] = []
    i, n = 0, len(src)
    line, col = 1, 1
    bol = True            # 是否处于行首（尚未遇到有效 token）
    indent_stack = [0]
    paren_depth = 0       # () 与 [] 的深度
    brace_depth = 0       # {} 的深度（进入 C 风格模式）
    pending_newline = False

    def adv(k: int = 1):
        nonlocal i, col
        for _ in range(k):
            if i < n and src[i] == "\n":
                line_plus_one()
            else:
                col += 1
            i += 1

    def line_plus_one():
        nonlocal line, col
        line += 1
        col = 1

    def emit(kind, value, l, c, suffix="", raw=""):
        toks.append(Token(kind, value, l, c, suffix, raw))

    while i < n:
        ch = src[i]

        # ---------------- 换行与缩进 ----------------
        if ch == "\n":
            adv()
            bol = True
            if paren_depth == 0 and brace_depth == 0:
                pending_newline = True
            continue

        # ---------------- 行首：缩进层级处理 ----------------
        # 必须在遇到本行第一个「有效字符」时处理，无论该行是否以空格开头，
        # 否则「从缩进块回到行首」时不会生成 DEDENT。
        if bol and brace_depth == 0 and paren_depth == 0:
            width = 0
            while i < n and src[i] in " \t":
                if src[i] == " ":
                    width += 1
                else:
                    width += 4 - (width % 4)       # tab = 下一个 4 的倍数
                adv()
            if i >= n:
                break
            if src[i] == "\r":                      # 空行
                adv()
                continue
            if src[i] == "\n":                      # 空行
                continue
            if src[i] == "#" or src.startswith("//", i):   # 整行注释
                while i < n and src[i] != "\n":
                    adv()
                continue
            if src.startswith("/*", i):             # 行首块注释
                adv(2)
                depth = 1
                while i < n and depth:
                    if src.startswith("/*", i):
                        depth += 1; adv(2)
                    elif src.startswith("*/", i):
                        depth -= 1; adv(2)
                    else:
                        adv()
                if i < n and src[i] != "\n":
                    bol = False                     # 注释后还有代码：视为行中
                continue
            if pending_newline and toks and toks[-1].kind not in ("NEWLINE", "INDENT", "DEDENT"):
                emit("NEWLINE", "\n", line, col)
            pending_newline = False
            if width > indent_stack[-1]:
                indent_stack.append(width)
                emit("INDENT", width, line, col)
            elif width < indent_stack[-1]:
                while width < indent_stack[-1]:
                    indent_stack.pop()
                    emit("DEDENT", width, line, col)
                if width != indent_stack[-1]:
                    raise FaSyntaxError("缩进不匹配（与既有层级对不上）", line, col)
            bol = False
            continue                                 # 重新处理本行首个有效字符

        if ch in " \t\r":
            adv()
            continue

        # ---------------- 注释 ----------------
        if ch == "#" or src.startswith("//", i):
            while i < n and src[i] != "\n":
                adv()
            continue
        if src.startswith("/*", i):
            l, c = line, col
            adv(2)
            depth = 1
            while i < n and depth:
                if src.startswith("/*", i):
                    depth += 1; adv(2)
                elif src.startswith("*/", i):
                    depth -= 1; adv(2)
                else:
                    adv()
            if depth:
                raise FaSyntaxError("块注释未闭合", l, c)
            continue

        # 行首第一个真实 token：补发 NEWLINE
        if bol and paren_depth == 0 and brace_depth == 0:
            if pending_newline and toks and toks[-1].kind not in ("NEWLINE", "INDENT", "DEDENT"):
                emit("NEWLINE", "\n", line, col)
            pending_newline = False
        bol = False

        l, c = line, col

        # ---------------- 字符串 ----------------
        if src.startswith('"""', i):            # 三引号多行字符串
            adv(3)
            buf = []
            while not src.startswith('"""', i):
                if i >= n:
                    raise FaSyntaxError('多行字符串未闭合（缺少 """）', l, c)
                if src[i] == "\\" and i + 1 < n:
                    adv()
                    e = src[i]
                    if e == "x":
                        adv(); hx = ""
                        for _ in range(2):
                            if i < n and src[i] in HEXD:
                                hx += src[i]; adv()
                        buf.append(chr(int(hx, 16)) if hx else "")
                        continue
                    if e in "{}":
                        buf.append(e * 2); adv(); continue    # 同上：字面花括号
                    buf.append(ESCAPES.get(e, e)); adv(); continue
                buf.append(src[i]); adv()
            adv(3)
            emit("STR", "".join(buf), l, c)
            continue
        if ch == '"':
            adv()
            buf = []
            idepth = 0                    # 插值表达式 {...} 的嵌套深度
            while True:
                if i >= n or src[i] == "\n":
                    if idepth > 0:
                        raise FaSyntaxError(
                            "字符串里的插值 { 没有配对的 }（要打印一个真的花括号，"
                            "写 {{ 和 }}）", l, c)
                    raise FaSyntaxError("字符串未闭合", l, c)
                if src[i] == '"' and idepth == 0:
                    adv(); break
                if idepth and src[i] in "\"'":
                    # 插值表达式里的字符串/字符字面量，例如 "{m["x"]}"：
                    # 整体吞进 buf。以前它的引号会提前结束外层字符串，
                    # 于是 `print("map: {m["x"]}")` 报「字符串插值 { 未闭合」。
                    q = src[i]
                    buf.append(q); adv()
                    while i < n and src[i] != q:
                        if src[i] == "\\" and i + 1 < n:
                            buf.append(src[i]); adv()
                        buf.append(src[i]); adv()
                    if i >= n or src[i] == "\n":
                        raise FaSyntaxError(
                            "字符串里的插值 { 没有配对的 }（要打印一个真的花括号，"
                            "写 {{ 和 }}）", l, c)
                    buf.append(q); adv()
                    continue
                # `{{` / `}}` 是**字面量**花括号（Python / Rust / C# 都是这个规矩）。
                # 以前只写了「`{{` 不开插值」，可它只跳过第一个 `{`，第二个照样开插值 ——
                # `"{{a}}"` 于是去解析标识符 a，报一句莫名其妙的「未定义的标识符 'a'」，
                # 位置还指到文件头。而没有配对 `}` 的 `"P{a="` 会把后面的引号当成
                # 插值里的字符串一路吞到行尾，报「字符串未闭合」。
                # 原文照抄进 buf，由 split_interpolation 统一还原成一个花括号。
                if idepth == 0 and src.startswith("{{", i):
                    buf.append("{{"); adv(2); continue
                if idepth == 0 and src.startswith("}}", i):
                    buf.append("}}"); adv(2); continue
                if src[i] == "{":
                    idepth += 1; buf.append("{"); adv(); continue
                if src[i] == "}" and idepth > 0:
                    idepth -= 1; buf.append("}"); adv(); continue
                if src[i] == "\\" and idepth == 0:
                    adv()
                    if i >= n:
                        raise FaSyntaxError("转义符后缺少字符", l, c)
                    e = src[i]
                    if e == "x":
                        adv(); hx = ""
                        for _ in range(2):
                            if i < n and src[i] in HEXD:
                                hx += src[i]; adv()
                        if not hx:
                            raise FaSyntaxError("\\x 需要十六进制数字", l, c)
                        buf.append(chr(int(hx, 16)))
                        continue
                    if e == "u":
                        adv()
                        if i < n and src[i] == "{":
                            adv(); hx = ""
                            while i < n and src[i] != "}":
                                hx += src[i]; adv()
                            if i >= n:
                                raise FaSyntaxError("\\u{...} 未闭合", l, c)
                            adv()
                            buf.append(chr(int(hx, 16)))
                            continue
                        # 文档里写的是 \u4F60（4 位十六进制），以前只认 \u{4F60}
                        hx = ""
                        while len(hx) < 4 and i < n and src[i] in HEXD:
                            hx += src[i]; adv()
                        if len(hx) != 4:
                            raise FaSyntaxError("\\u 需要 4 位十六进制（\\u4F60）"
                                                "或花括号形式（\\u{4F60}）", l, c)
                        buf.append(chr(int(hx, 16)))
                        continue
                    if e in "{}":
                        # \{ \} 也是字面花括号：先写成 {{ }}，split_interpolation
                        # 再还原成一个。转义是在**词法层**做的，而插值是在字符串值上
                        # 二次切分的，所以不能直接放一个 { 进去（那会被当成插值的开头）。
                        buf.append(e * 2); adv(); continue
                    if e in ESCAPES:
                        buf.append(ESCAPES[e]); adv(); continue
                    raise FaSyntaxError(f"未知转义序列 \\{e}", l, c)
                buf.append(src[i]); adv()
            emit("STR", "".join(buf), l, c)
            continue

        # ---------------- 字符字面量 ----------------
        if ch == "'":
            adv()
            if i >= n:
                raise FaSyntaxError("字符字面量未闭合", l, c)
            if src[i] == "\\":
                adv()
                e = src[i]; adv()
                if e == "x":
                    hx = ""
                    while len(hx) < 2 and i < n and src[i] in HEXD:
                        hx += src[i]; adv()
                    if len(hx) != 2:
                        raise FaSyntaxError("\\x 需要两位十六进制数字", l, c)
                    chv = chr(int(hx, 16))
                elif e == "u":
                    if i < n and src[i] == "{":
                        adv(); hx = ""
                        while i < n and src[i] != "}":
                            hx += src[i]; adv()
                        if i >= n:
                            raise FaSyntaxError("\\u{...} 未闭合", l, c)
                        adv()
                    else:
                        hx = ""
                        while len(hx) < 4 and i < n and src[i] in HEXD:
                            hx += src[i]; adv()
                        if len(hx) != 4:
                            raise FaSyntaxError("\\u 需要 4 位十六进制（\\u4F60）"
                                                "或花括号形式（\\u{4F60}）", l, c)
                    cp = int(hx, 16)
                    if cp > 0xFF:
                        raise FaSyntaxError(
                            f"char 是单字节（u8），装不下 U+{cp:04X}；"
                            f"多字节字符请写成字符串 \"\\u{cp:04X}\"", l, c)
                    chv = chr(cp)
                else:
                    chv = ESCAPES.get(e)
                    if chv is None:
                        raise FaSyntaxError(
                            f"未知转义 \\{e}（可用：\\n \\t \\r \\\\ \\' \\0 "
                            f"\\xNN \\uNNNN \\u{{...}}）", l, c)
            else:
                chv = src[i]; adv()
            if i >= n or src[i] != "'":
                raise FaSyntaxError("字符字面量只能包含一个字符", l, c)
            adv()
            emit("CHAR", chv, l, c)
            continue

        # ---------------- 数字 ----------------
        if ch in DIGITS or (ch == "." and i + 1 < n and src[i + 1] in DIGITS):
            start = i
            isfloat = False
            if src.startswith(("0x", "0X"), i):
                adv(2)
                while i < n and (src[i] in HEXD or src[i] == "_"):
                    adv()
                value = int(src[start:i].replace("_", ""), 16)
            elif src.startswith(("0b", "0B"), i):
                adv(2)
                while i < n and (src[i] in "01_"):
                    adv()
                value = int(src[start:i].replace("_", ""), 2)
            elif src.startswith(("0o", "0O"), i):
                adv(2)
                while i < n and (src[i] in "01234567_"):
                    adv()
                value = int(src[start:i].replace("_", ""), 8)
            else:
                while i < n and (src[i] in DIGITS or src[i] == "_"):
                    adv()
                # 小数点后面必须真的跟数字，才算浮点字面量。
                # 否则 `42.to_str()` 会被当成「42. 加后缀 to_str」，报一条
                # 「非法数字字面量」的错 —— 而用户想写的是整数 42 调方法。
                if (i < n and src[i] == "." and i + 1 < n
                        and (src[i + 1] in DIGITS or src[i + 1] == "_")
                        and src[i + 1] != "."):
                    isfloat = True
                    adv()
                    while i < n and (src[i] in DIGITS or src[i] == "_"):
                        adv()
                if i < n and src[i] in "eE":
                    j = i + 1
                    if j < n and src[j] in "+-":
                        j += 1
                    if j < n and src[j] in DIGITS:
                        isfloat = True
                        i = j
                        while i < n and (src[i] in DIGITS or src[i] == "_"):
                            adv()
                raw = src[start:i].replace("_", "")
                value = float(raw) if isfloat else int(raw)
            # 类型后缀：42i8 / 3.5f32 / 7u64
            suf = ""
            if i < n and (src[i].isalpha() or src[i] == "_"):
                j = i
                while j < n and (src[j].isalnum() or src[j] == "_"):
                    j += 1
                cand = src[i:j]
                if cand in NUM_SUFFIX:
                    suf, i = cand, j
                else:
                    raise FaSyntaxError(
                        f"非法数字字面量 '{src[start:j]}'（未知后缀 '{cand}'，"
                        f"可用：{' '.join(sorted(NUM_SUFFIX))}）", l, c)
            emit("FNUM" if isfloat else "NUM", value, l, c, suf, src[start:i])
            continue

        # ---------------- 标识符 ----------------
        if ch.isalpha() or ch == "_" or ord(ch) > 127:
            start = i
            while i < n and (src[i].isalnum() or src[i] == "_" or ord(src[i]) > 127):
                adv()
            word = src[start:i]
            if word in KEYWORDS or word in PRIMITIVE_TYPES:
                emit("KW", word, l, c)
            else:
                emit("NAME", word, l, c)
            continue

        # ---------------- 运算符 ----------------
        matched = None
        for op in OPERATORS:
            if src.startswith(op, i):
                matched = op
                break
        if matched:
            adv(len(matched))
            emit("OP", matched, l, c)
            continue

        # ---------------- 标点 ----------------
        if ch in PUNCT:
            adv()
            if ch in "([":
                paren_depth += 1
            elif ch in ")]":
                paren_depth = max(0, paren_depth - 1)
            elif ch == "{":
                brace_depth += 1
                emit("PUNCT", "{", l, c)
                continue
            elif ch == "}":
                brace_depth = max(0, brace_depth - 1)
                emit("PUNCT", "}", l, c)
                continue
            emit("PUNCT", ch, l, c)
            continue

        raise FaSyntaxError(f"无法识别的字符 '{ch}'", l, c)

    # 收尾
    if pending_newline and toks and toks[-1].kind not in ("NEWLINE", "INDENT", "DEDENT"):
        emit("NEWLINE", "\n", line, col)
    while len(indent_stack) > 1:
        indent_stack.pop()
        emit("DEDENT", 0, line, col)
    emit("EOF", None, line, col)
    return toks


def split_interpolation(raw: str):
    """把 `a = {x}, b = {y.z}` 切成 [('lit', str) | ('expr', str)] 片段。

    支持插值表达式里的嵌套大括号（如 {f({1:2})}）；表达式**外面**的 `{{` / `}}`
    是字面量花括号，各还原成一个（想打印 JSON 或者 `P {{ x: 1 }}` 这种文本就靠它）。
    """
    parts, buf, depth = [], [], 0
    i = 0
    while i < len(raw):
        ch = raw[i]
        if depth == 0 and raw.startswith("{{", i):
            buf.append("{"); i += 2; continue        # {{ -> 字面量 {
        if depth == 0 and raw.startswith("}}", i):
            buf.append("}"); i += 2; continue        # }} -> 字面量 }
        if ch == "{":
            if depth == 0:
                if buf:
                    parts.append(("lit", "".join(buf))); buf = []
                depth = 1
                i += 1
                continue
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                parts.append(("expr", "".join(buf))); buf = []
                i += 1
                continue
        if depth:
            buf.append(ch)
        else:
            buf.append(ch)
        i += 1
    if depth:
        raise FaSyntaxError("字符串插值 { 未闭合")
    if buf:
        parts.append(("lit", "".join(buf)))
    return parts


if __name__ == "__main__":  # pragma: no cover
    import sys
    src = open(sys.argv[1]).read()
    for t in tokenize(src):
        print(t)
