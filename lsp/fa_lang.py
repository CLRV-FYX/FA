#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FA 语言服务：诊断 / 补全 / 悬停 / 符号 / 签名。

三个前端共用这一份 —— LSP 服务器（`fa lsp`）、Web IDE（`fa ide`）、命令行
（`fa diag`），所以「编辑器里画的红线」和「`fa check` 报的错」永远是同一套代码
产出的：这里直接调编译器前端（parse / Sema / CG / generate_asm），
既不 fork 子进程，也不拿正则去猜源码。

几个设计取舍，都是踩过才定下来的：

* **一次只给一条硬错误。** sema 是「撞上第一个错就抛」的（和 `fa check` 一样），
  所以拿不到错误列表。与其把一条错硬拆成几条互相牵连的，不如把第一条说清楚 ——
  编译器给的中文报错本身就带了出路（「想按结构体的某个字段排，用 v.sort_by(...)」）。
  硬错误存在时补全/悬停**照样能用**（走 AST + 文本，不要求 sema 跑通），
  不会像有些语言服务那样一有错就整个罢工。
* **另有一层不依赖编译成功的 lint**：Tab 缩进、全角标点。这两个是 FA 特有的
  一脚踩空：FA 对缩进敏感，混进一个 Tab 层级就错了；中文输入法的全角冒号
  在代码里是语法错误，而报错只会说「期望 ':'」，看不出是全角半角的事。
* **补全按上下文分四种**：`use ` 后给模块名、`.` 后给成员（能定出接收者类型就
  按类型给，定不出来就给全集并标明来源）、`->` 后给类型、其余给关键字/内建/
  自己的声明。说明文字与教程 §27 速查表逐字对齐，免得两处各说各话。
* **行列都是 1 起、按字符数**（和编译器报错一致）。LSP 那边自己转 0 起；
  中日韩文字在 BMP 里，字符数与 UTF-16 码元数相同，只有 emoji 这类增补平面
  字符会差一位 —— 那种情况下红线会偏一点，不影响判断。
"""

from __future__ import annotations

import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if os.path.join(_ROOT, "compiler") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "compiler"))

from falang.lexer import KEYWORDS, FaSyntaxError          # noqa: E402
from falang.parser import parse                           # noqa: E402
from falang.sema import (Sema, FaTypeError, BUILTIN_FNS,  # noqa: E402
                         BUILTIN_METHODS, METHOD_ARITY)
from falang import codegen as CG                          # noqa: E402
from falang.asmgen import generate_asm                    # noqa: E402
from falang.types import TYPES                            # noqa: E402

try:
    from falang.sema import stdlib_modules
except Exception:                                        # 老版本编译器没有这个函数
    stdlib_modules = None

SEV_ERROR = 1
SEV_WARN = 2
SEV_INFO = 3
SEV_NAME = {SEV_ERROR: "错误", SEV_WARN: "警告", SEV_INFO: "提示"}

# 补全项的种类（沿用 LSP 的 CompletionItemKind 编号，前端可以直接用图标）
K_TEXT, K_METHOD, K_FUNCTION, K_FIELD, K_VARIABLE, K_CLASS = 1, 2, 3, 5, 6, 7
K_KEYWORD, K_SNIPPET, K_VALUE, K_ENUM_MEMBER, K_MODULE, K_TYPE = 14, 15, 12, 20, 9, 7
KIND_NAME = {K_TEXT: "文本", K_METHOD: "方法", K_FUNCTION: "函数", K_FIELD: "字段",
             K_VARIABLE: "变量", K_CLASS: "类型", K_KEYWORD: "关键字",
             K_SNIPPET: "片段", K_VALUE: "值", K_ENUM_MEMBER: "枚举变体",
             K_MODULE: "模块", K_TYPE: "类型"}


class Diagnostic:
    """一条诊断。line/col/end_col 都是 1 起、含头不含尾。"""

    __slots__ = ("line", "col", "end_col", "message", "severity", "stage")

    def __init__(self, line, col, end_col, message, severity=SEV_ERROR, stage=""):
        self.line = max(1, int(line or 1))
        self.col = max(1, int(col or 1))
        self.end_col = max(self.col + 1, int(end_col or self.col + 1))
        self.message = message
        self.severity = severity
        self.stage = stage

    def to_dict(self):
        return {"line": self.line, "col": self.col, "endCol": self.end_col,
                "message": self.message, "severity": self.severity,
                "severityName": SEV_NAME.get(self.severity, "?"), "stage": self.stage}

    def text(self):
        where = f"行 {self.line}, 列 {self.col}"
        head = f"{SEV_NAME.get(self.severity, '?')} ({where})"
        return f"{head}: {self.message}" + (f"　[{self.stage}]" if self.stage else "")


class Analysis:
    """一次分析的结果：诊断 + 能拿到的编译产物（sema/AST 给补全和悬停复用）。"""

    def __init__(self, src=""):
        self.src = src
        self.ok = False
        self.stage = ""
        self.error = ""
        self.diags: list = []
        self.mod = None
        self.sema = None
        # Sema.run() 会把 stdlib 各子模块的声明摊平进 mod.decls（sema.py:639-640）：
        # parse 完 7 条，sema 完变 57 条。documentSymbol 只该报本文件的，
        # 所以 parse 一结束就把这批对象的 id 记下来，后面照着筛。
        self.local_ids = set()
        self.elapsed_ms = 0.0

    def fail(self, line, col, msg, stage):
        self.ok = False
        self.stage = stage
        self.error = msg
        e, c = _span(self.src, line, col)
        self.diags.append(Diagnostic(e, c[0], c[1], msg, SEV_ERROR, stage))
        return self

    def finish(self, t0):
        self.elapsed_ms = (time.time() - t0) * 1000.0
        return self


# ------------------------------------------------------------------ 位置工具
def _lines(src):
    return src.split("\n")


def _line_text(src, line):
    ls = _lines(src)
    if 1 <= line <= len(ls):
        return ls[line - 1]
    return ""


def _span(src, line, col):
    """把「行 line 列 col」这一个点扩成一个词的范围：返回 (line, (col, end_col))。

    编译器只给一个点（它自己打的是 ^），编辑器要画波浪线就得有个范围 ——
    从那一列往后吃掉标识符字符，吃不到就至少给一格。
    """
    line = max(1, int(line or 1))
    col = max(1, int(col or 1))
    text = _line_text(src, line)
    i = col - 1
    if i >= len(text):
        return line, (max(1, len(text)), max(2, len(text) + 1))
    j = i
    while j < len(text) and (text[j].isalnum() or text[j] in "_"):
        j += 1
    if j == i:
        j = i + 1
    return line, (col, j + 1)


def _word_at(src, line, col):
    """光标处的词（光标在词中间也算），返回 (word, start_col, end_col)。"""
    text = _line_text(src, line)
    i = min(max(col - 1, 0), len(text))
    a = i
    while a > 0 and (text[a - 1].isalnum() or text[a - 1] in "_"):
        a -= 1
    b = i
    while b < len(text) and (text[b].isalnum() or text[b] in "_"):
        b += 1
    return text[a:b], a + 1, b + 1


# ------------------------------------------------------------------ 诊断
def analyze(src, filename="<ide>.fa", full=True) -> Analysis:
    """跑一遍编译器前端，把第一个硬错误变成一条结构化诊断。

    full=True 时连 codegen / asmgen 一起跑（和 `fa check` 完全同一范围）：
    有些错只在代码生成阶段露出来（比如 str 容器的 min/max）。
    实测 215 行的文件全程 23 毫秒（parse 3.4 / sema 0.6 / codegen 1.9 / asmgen 16.9），
    所以每次按键都跑得起，前端再做 200 毫秒防抖就够了。
    """
    a = Analysis(src)
    t0 = time.time()
    try:
        mod = parse(src, filename)
    except FaSyntaxError as e:
        return a.fail(e.line, e.col, e.msg, "语法分析").finish(t0)
    except Exception as e:                                # 解析器自己崩了也要说出来
        return a.fail(1, 1, f"{type(e).__name__}: {e}", "语法分析（内部错误）").finish(t0)
    a.mod = mod
    a.local_ids = {id(d) for d in (getattr(mod, "decls", None) or [])}
    try:
        a.sema = Sema(mod, filename, src).run()
    except (FaTypeError, FaSyntaxError) as e:
        return a.fail(e.line, e.col, e.msg, "语义分析").finish(t0)
    except Exception as e:
        return a.fail(1, 1, f"{type(e).__name__}: {e}", "语义分析（内部错误）").finish(t0)
    if full:
        try:
            ir = CG.generate(a.sema)
            generate_asm(ir, a.sema, 2)
        except CG.FaCodegenError as e:
            return a.fail(e.line, e.col, e.msg, "代码生成").finish(t0)
        except FaSyntaxError as e:
            return a.fail(e.line, e.col, e.msg, "代码生成").finish(t0)
        except Exception as e:
            return a.fail(1, 1, f"{type(e).__name__}: {e}",
                          "代码生成（内部错误）").finish(t0)
    a.ok = True
    return a.finish(t0)


# 全角标点：中文输入法忘了切回来时最容易混进代码的东西
_FULLWIDTH = "：；，、（）「」『』【】？！“”‘’〈〉《》｜　～…"
_STR_CH = {"'", '"'}


def _code_spans(line):
    """把一行里「属于代码」的列区间挑出来（跳过字符串字面量与注释）。

    三引号字符串跨行，所以调用方要把 in_triple 状态在行之间传下去。
    """
    spans = []
    i, n = 0, len(line)
    start = 0
    quote = ""
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = ""
                start = i           # 字符串结束后，代码区间从这里重新开始
                # 少了这句，spans 会变成 [(0,10), (0,28)] —— 后一段把字符串自己
                # 又圈了进去，于是字符串里的中文标点全被当成「代码里的全角标点」误报
                # （160 个测试文件里误报了 51 条，全是这个原因）
            else:
                i += 1
            continue
        if line.startswith('"""', i):
            spans.append((start, i))
            quote = '"""'
            i += 3
            continue
        if ch in _STR_CH:
            spans.append((start, i))
            quote = ch
            i += 1
            continue
        if ch == "#" or line.startswith("//", i):
            spans.append((start, i))
            return spans, quote
        i += 1
    spans.append((start, n))
    return spans, quote


def lint(src):
    """不依赖编译成功的检查：Tab 缩进、全角标点。

    这两条都只在「代码区」判，字符串和注释里出现是正常的（中文注释里当然有全角冒号）。
    """
    out = []
    quote = ""
    for idx, line in enumerate(_lines(src)):
        no = idx + 1
        lead = len(line) - len(line.lstrip())
        if "\t" in line[:lead]:
            out.append(Diagnostic(no, 1, lead + 1,
                                  "缩进里有 Tab。FA 对缩进敏感，Tab 和空格混着用时"
                                  "层级会算错（这一行前面有 %d 个空白字符）；"
                                  "统一用空格，编辑器一般能设成「按 Tab 插入空格」"
                                  % lead, SEV_WARN, "lint"))
        spans, quote = _code_spans(line)
        if quote == '"""' and line.rstrip().endswith('"""'):
            quote = ""                                    # 同一行开又关
        for a, b in spans:
            seg = line[a:b]
            for k, ch in enumerate(seg):
                if ch in _FULLWIDTH:
                    col = a + k + 1
                    what = "全角空格" if ch == "\u3000" else f"全角标点 “{ch}”"
                    out.append(Diagnostic(
                        no, col, col + 1,
                        f"代码里出现了{what}。FA 只认半角符号，这个字符会直接变成"
                        "语法错误（而报错只会说「期望 ':'」这类话，看不出是全角的事）"
                        "—— 切回英文输入法，或者把它挪进字符串/注释里",
                        SEV_WARN, "lint"))

    # ---- 第二遍：块头少了冒号 ----
    # FA 的块头一律以冒号收尾：`fn f() -> i64:` / `if x > 0:` / `struct P:`。
    # 少写一个冒号，编译器报的却是「顶层只允许 use/fn/struct/… 声明，得到 'print'」，
    # 指着**下一行**说话 —— 新手根本看不出是上一行少了冒号。这条 lint 就是为它写的。
    #
    # 三种合法写法不能误报（都是跑过的测试用例里挑出来的）：
    #   1) 大括号风格 `fn main() -> i64 {`（018_braces.fa），后面还可能跟行尾注释
    #      `for k in m.keys() {   # 注释`（061_backend_regressions.fa:39）
    #   2) 条件跨行 `if (a != b\n        or c != d):`（stdlib/time.fa:396）
    #   3) 字符串里的冒号/花括号不算数
    BLOCK_KW = {"fn": "函数签名", "if": "if 条件", "elif": "elif 条件",
                "else": "else", "for": "for 头", "while": "while 条件",
                "match": "match 头", "struct": "struct 头",
                "enum": "enum 头", "impl": "impl 头"}
    lines = _lines(src)
    n = len(lines)
    codes = []                                  # 每行「只剩代码」的文本（去掉注释和字符串）
    for ln in lines:
        spans, _q = _code_spans(ln)
        codes.append("".join(ln[a:b] for a, b in spans))
    pending = 0                                 # 上一行还没闭合的 ( / [ 个数
    for i, ln in enumerate(lines):
        code = codes[i].rstrip()
        st = code.strip()
        bal = code.count("(") + code.count("[") - code.count(")") - code.count("]")
        skip = (pending > 0                      # 上一行括号没闭合 → 这行是续行
                or not st or st.startswith("#") or st.startswith("extern")
                or st.endswith("{")              # 大括号风格同样合法
                or bal > 0)                      # 块头自己跨行（stdlib/time.fa:396 的 if (…)）
        head = st.split(None, 1)[0].rstrip(":") if st else ""
        if not skip and head in BLOCK_KW and ":" not in code:
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            ind_cur = len(ln) - len(ln.lstrip())
            if j < n and (len(lines[j]) - len(lines[j].lstrip())) > ind_cur:
                out.append(Diagnostic(
                    i + 1, ind_cur + 1, len(ln.rstrip()) + 1,
                    f"{BLOCK_KW[head]}末尾少了冒号「:」。FA 的块头一律以冒号收尾再接缩进块"
                    f"（`{head} …:`）；少了它，编译器会指着**下一行**报"
                    "「顶层只允许 use/fn/struct/… 声明，得到 'xxx'」这类看不出所以然的错 —— "
                    "在这行行尾补一个 ':'（想写大括号风格就在行尾补 '{'）",
                    SEV_WARN, "lint"))
        pending = 0 if pending > 0 and pending + bal <= 0 else max(0, pending + bal)

    # ---- 第三遍：从别的语言带过来的写法 ----
    # 这几条编译器都只会说「无法解析的表达式起始」，连 offending token 都不告诉你，
    # 但对写过 C/Java/Go/Python 的人来说，一眼就该看出来是习惯问题。
    FOREIGN = (("&&", "FA 的逻辑与是 `and`（`&&` 不认）"),
               ("||", "FA 的逻辑或是 `or`（`||` 不认）"),
               (":=", "FA 声明变量写 `let x = 1`，没有 `:=`（Go 的短声明 / Python 的海象运算符）"))
    for i, code in enumerate(codes):
        indent = len(lines[i]) - len(lines[i].lstrip())
        for pat, msg in FOREIGN:
            k = code.find(pat)
            while k >= 0:
                out.append(Diagnostic(i + 1, k + 1, k + 1 + len(pat),
                                      f"像是从别的语言带过来的写法：{msg}",
                                      SEV_WARN, "lint"))
                k = code.find(pat, k + len(pat))
        st = code.strip()
        # `case Foo:` —— FA 的 match 分支不写 case（Python 的 match 才写）
        if (st.startswith("case ") or st == "case") and ":" in code:
            out.append(Diagnostic(
                i + 1, indent + 1, len(lines[i].rstrip()) + 1,
                "FA 的 match 没有 case 关键字（那是 C/Java/Python 的写法）。"
                "分支直接写模式、比 match 缩进一级：\n"
                "    match m:\n"
                "        Msg.Move(x, y):\n"
                "            return \"move {x},{y}\"",
                SEV_WARN, "lint"))
    return out


# 别的语言的类型名 -> FA 里该写什么（FA 的类型表就是 TYPES 那些，没有 ptr/int/string）
_TYPE_ALIAS_HINT = {
    "int": "i64", "integer": "i64", "long": "i64", "short": "i16", "uint": "u64",
    "unsigned": "u64", "byte": "u8", "size_t": "usize", "ssize_t": "isize",
    "int8": "i8", "int16": "i16", "int32": "i32", "int64": "i64",
    "uint8": "u8", "uint16": "u16", "uint32": "u32", "uint64": "u64",
    "float": "f64", "double": "f64", "float32": "f32", "float64": "f64", "real": "f64",
    "string": "str", "boolean": "bool", "null": "nil", "None": "void（返回空）或 nil（空值）",
    "undefined": "nil", "list": "Vec<T>", "array": "Vec<T>（定长数组写 [T; N]）",
    "vector": "Vec<T>", "dict": "Map<K,V>", "dictionary": "Map<K,V>", "map": "Map<K,V>",
    "hashmap": "Map<K,V>", "object": "自己 struct 一个", "var": "let（类型自动推）",
    "auto": "let（类型自动推）", "ptr": "*T（指针写在类型前面）", "number": "f64 或 i64",
}


def _enrich(diags):
    """编译器报得没错、但只说「不行」不说「该写什么」的诊断，补一句对照。"""
    for d in diags:
        m = re.search(r"未知类型 '([^']+)'", d.message)
        if m:
            hint = _TYPE_ALIAS_HINT.get(m.group(1))
            if hint:
                d.message += (f"　FA 里这个类型叫 {hint}"
                              f"（FA 的类型只有 i8/i16/i32/i64、u8/u16/u32/u64、"
                              "f32/f64、bool、char、str、void、usize/isize、any，"
                              "容器是 Vec<T> / Map<K,V>，指针写 *T）")
    return diags


def check(src, filename="<ide>.fa", full=True):
    """前端唯一需要调的诊断入口：硬错误 + lint。"""
    a = analyze(src, filename, full)
    diags = list(a.diags)
    # lint 一律全给：有硬错误时它往往正是**成因**（全角冒号会让编译器报
    # 「未定义的标识符 'x：1'」这种看不出所以然的话，lint 那条才点破了是全角的事）
    diags.extend(lint(src))
    # 按位置排：硬错误是编译到哪算哪，lint 是全文扫的，混在一起会前后乱跳
    diags.sort(key=lambda d: (d.line, d.col, 0 if d.severity == SEV_ERROR else 1))
    return _enrich(diags), a


# ------------------------------------------------------------------ 说明文字
# 措辞与教程 §27.7 方法总表对齐（docs/08_完全教程.md），改一边要改另一边。
STR_DOC = {
    "len": "len() -> i64　字节数（不是字符数；字符数用 char_len）",
    "char_len": "char_len() -> i64　字符数（UTF-8 码点数）",
    "at": "at(i) -> char　第 i 个**字节**；越界 panic",
    "char_at": "char_at(i) -> char　第 i 个**字符**的首字节",
    "slice": "slice(a, b) -> str　按字节切片，不含 b",
    "slice_chars": "slice_chars(a, b) -> str　按字符切片",
    "bytes": "bytes() -> Vec<i64>　每个字节的数值",
    "chars": "chars() -> Vec<char>",
    "codepoints": "codepoints() -> Vec<i64>　码点（可能 > 255，所以不是 char）",
    "find": "find(子串) -> i64　第一次出现的位置，没有给 -1",
    "rfind": "rfind(子串) -> i64　最后一次出现的位置，没有给 -1",
    "contains": "contains(子串) -> bool",
    "starts_with": "starts_with(前缀) -> bool",
    "ends_with": "ends_with(后缀) -> bool",
    "count": "count(子串) -> i64　出现次数",
    "split": "split(分隔符) -> Vec<str>　分隔符连着出现会切出空串",
    "lines": "lines() -> Vec<str>",
    "trim": "trim() -> str　去掉两端空白",
    "trim_start": "trim_start() -> str",
    "trim_end": "trim_end() -> str",
    "upper": "upper() -> str（只对 ASCII）",
    "lower": "lower() -> str（只对 ASCII）",
    "replace": "replace(旧, 新) -> str　全部替换",
    "repeat": "repeat(n) -> str",
    "to_i64": "to_i64() -> i64　认前导零和负号；认不出来给 0",
    "to_f64": "to_f64() -> f64",
    "to_str": "to_str() -> str　对自己就是原样返回",
    "eq": "eq(其它) -> bool　内容相等（== 也行）",
    "cstr": "cstr() -> *u8　拿 C 的 char*（**不加引用**，别存起来）",
}
VEC_DOC = {
    "len": "len() -> i64",
    "push": "push(x) -> void　追加一个元素（结构体元素会装一份新箱）",
    "pop": "pop() -> 元素　弹出末尾；**空表 panic**",
    "get": "get(i) -> 元素　越界 panic（越界报错会把长度和下标都打出来）",
    "set": "set(i, x) -> void　越界 panic",
    "clear": "clear() -> void",
    "resize": "resize(n[, 填充值]) -> void　元素是结构体时变长必须给填充值",
    "contains": "contains(x) -> bool　结构体/枚举/嵌套容器的表**不支持**（比的是地址）",
    "index_of": "index_of(x) -> i64　没有给 -1（不是 nil）",
    "sort": "sort() -> void　原地升序；只有标量/str/指针的表能排，结构体用 sort_by",
    "sort_by": "sort_by(取键函数) -> void　按字段排，**稳定**、升序。键函数 fn(*元素) -> i64/f64/str，传函数名不加括号",
    "reverse": "reverse() -> void　原地翻转",
    "map": "map(f) -> Vec<R>　新表，元素类型 = 回调返回类型；原表不动。f: fn(*元素) -> R",
    "filter": "filter(p) -> Vec<T>　新表，只留判定成立的；p: fn(*元素) -> bool",
    "any": "any(p) -> bool　有一个成立就 true（短路）；空表是 false",
    "all": "all(p) -> bool　全都成立才 true（短路）；空表是 true",
    "index_where": "index_where(p) -> i64　第一个成立的下标，没有给 -1（短路）",
    "for_each": "for_each(f) -> void　f: fn(*元素)，不返回东西，但能通过 *元素**改**元素",
    "sum": "sum() -> i64/f64　数值表；Vec<str> 不行，用 join",
    "min": "min() -> 元素　**空表给 0**，先判长度",
    "max": "max() -> 元素　**空表给 0**，先判长度",
    "join": "join(分隔符) -> str　str 元素不加引号；结构体元素由编译器展开成完整字段",
    "to_str": "to_str() -> str　打印用（str 元素加引号，这是它和 join 的区别）",
    "copy": "copy() -> Vec<T>　**深一层**的拷贝（元素是结构体时箱子里的东西仍共享）",
}
MAP_DOC = {
    "len": "len() -> i64",
    "get": "get(k) -> 值　**取不到时**：值是标量给零值，值是容器直接 panic —— 先 has",
    "set": "set(k, v) -> void",
    "has": "has(k) -> bool",
    "contains": "contains(k) -> bool　has 的别名",
    "del": "del(k) -> void",
    "clear": "clear() -> void",
    "keys": "keys() -> Vec<K>　遍历顺序是哈希顺序，要确定就先 sort()",
    "values": "values() -> Vec<V>",
    "to_str": "to_str() -> str　值是指针时印的是地址，别拿它调试",
    "copy": "copy() -> Map<K,V>",
}
NUM_DOC = {
    "to_str": "to_str() -> str",
    "to_i64": "to_i64() -> i64",
    "to_f64": "to_f64() -> f64",
    "abs": "abs() -> 同类型",
    "round": "round() -> f64",
    "trunc": "trunc() -> f64",
    "floor": "floor() -> f64",
    "ceil": "ceil() -> f64",
    "sqrt": "sqrt() -> f64",
    "log": "log() -> f64　自然对数",
    "log2": "log2() -> f64",
    "log10": "log10() -> f64",
    "exp": "exp() -> f64",
    "exp2": "exp2() -> f64",
    "sin": "sin() -> f64",
    "cos": "cos() -> f64",
    "tan": "tan() -> f64",
}
BUILTIN_DOC = {
    "print": "print(...)　打印，参数之间空格，末尾换行。容器/结构体自动转成完整文本",
    "println": "println(...)　同 print",
    "write": "write(...)　打印但**不换行**",
    "len": "len(x) -> i64　str 的字节数 / Vec、Map 的元素个数",
    "range": ("range(止) / range(起, 止) / range(起, 止, 步长)\n\n"
              "只能写在 for 的遍历位置：`for i in range(0, 10, 2)`。\n"
              "步长可以是负的（倒着走），不能是 0。\n"
              "range 不是一等值 —— 不能存进变量、当参数传、也不能 print；\n"
              "要一个整数序列请用 Vec<i64>。等价写法：`for i in 起..止`（无步长）。"),
    "str": "str(x) -> str　任何值转字符串（等价于 x.to_str()）",
    "i64": "i64(x) -> i64", "f64": "f64(x) -> f64",
    "chr": "chr(码点) -> str　码点转字符串（要字面花括号就 chr(123)）",
    "hex": "hex(n) -> str", "oct": "oct(n) -> str", "bin": "bin(n) -> str",
    "assert": "assert(条件[, 消息])　不成立就 panic",
    "panic": "panic(消息)　打印后退出码 1；后面的代码不执行",
    "raise": 'raise "消息"　panic 的糖',
    "exit": "exit([码])　结束程序；不带参数就是 exit(0)",
    "now": "now() -> f64　Unix 秒（带小数）",
    "sleep": "sleep(毫秒)",
    "random": "random() -> f64　[0,1)",
    "args": "args() -> Vec<str>　args()[0] 是程序路径",
    "env": 'env("名字") -> str　取不到给 ""',
    "cmd": 'cmd("命令") -> str　跑一条命令收它的 stdout',
    "read_line": "read_line() -> str　读一行（EOF 给 \"\"）",
    "file_read": 'file_read("路径") -> str　读不到给 ""',
    "file_write": 'file_write("路径", 内容) -> i64　0 是成功',
    "new": "new T { ... } -> *T　堆上分配；要配 free",
    "free": "free(p)　只释放 new / C 那边拿来的指针；str/Vec/Map 是引用计数的，不用也不能 free",
    "sizeof": "sizeof(类型) -> 编译期常量",
    "concat": "concat(a, b) -> str",
    "gcd": "gcd(a, b) -> i64",
    "min": "min(a, b) / min(v)　两个值取小，或者一张表的最小值",
    "max": "max(a, b) / max(v)",
    "abs": "abs(x)　整数给整数，浮点给浮点",
    "clamp": "clamp(x, lo, hi)",
    "sign": "sign(x) -> i64　-1 / 0 / 1",
    "pow": "pow(a, b) -> f64　也可以用 a ** b（右结合）",
    "hypot": "hypot(a, b) -> f64",
    "round": "round(x) -> f64", "trunc": "trunc(x) -> f64",
    "floor": "floor(x) -> f64", "ceil": "ceil(x) -> f64",
    "sqrt": "sqrt(x) -> f64",
    "log": "log(x) -> f64", "log2": "log2(x) -> f64", "log10": "log10(x) -> f64",
    "exp": "exp(x) -> f64", "exp2": "exp2(x) -> f64",
    "sin": "sin(x) -> f64", "cos": "cos(x) -> f64", "tan": "tan(x) -> f64",
    "sort": "sort(v) -> void　等价于 v.sort()",
    "sort_by": "sort_by(v, 取键函数) -> void　等价于 v.sort_by(f)",
    "reverse": "reverse(v) -> void",
    "push": "push(v, x) / pop(v) / get(v, i) / set(v, i, x)　全局写法",
    "sum": "sum(v)", "join": "join(v, 分隔符)",
    "keys": "keys(m) -> Vec<K>", "values": "values(m) -> Vec<V>",
    "contains": "contains(容器, 元素) / contains(haystack, needle)",
    "cstr": "cstr(p)　C 的 char* -> str",
}
KEYWORD_DOC = {
    "fn": "fn 名字(参数: 类型) -> 返回类型:　不写返回类型就是没有返回值",
    "let": "let x = 1 / let x: i64 = 1 / let x: i64（不给初值就是零值；容器拿到的是真的空容器）",
    "const": "const K = 5　顶层常量，按使用处替换；初值必须编译期算得出来",
    "return": "return 值",
    "if": "if 条件:　也可以是表达式（必须有 else，各分支类型一致）",
    "elif": "elif 条件:",
    "else": "else:",
    "while": "while 条件:",
    "for": "for x in 容器: / for i in 0..n:　没有步进写法；0..n 不含 n，0..=n 含",
    "in": "只能跟在 for 的遍历对象里（range 不能当普通值用）",
    "loop": "loop:　无限循环，配 break",
    "break": "break", "continue": "continue",
    "struct": "struct P:　+ 缩进字段；字段可以带默认值",
    "enum": "enum E:　+ 缩进变体；变体可以带载荷 Just(v: i64)",
    "impl": "impl P:　+ 缩进 fn；带 self 是实例方法，不带是静态方法",
    "match": "match 值:　作表达式要穷尽或带 _；漏掉的分支是**悄悄掉过去**",
    "use": 'use "mod.fa" / use std.fs / use c "h.h" lib "m": / use cxx ... / use lib "./x.so" / use py / use java',
    "as": "x as 类型　转换；截断不报错（300 as u8 = 44）",
    "extern": 'extern "C" { fn ... }　只声明，不能带函数体',
    "defer": "defer 调用　作用域退出时执行，多个 defer 是 LIFO",
    "new": "new T { ... } -> *T　要配 free",
    "sizeof": "sizeof(类型)　编译期常量",
    "asm": 'asm "..."　原样插入汇编，不做插值',
    "and": "and　短路", "or": "or　短路", "not": "not",
    "true": "true", "false": "false", "nil": "nil　空指针；解引用 nil 是运行时 panic",
    "mut": "mut　目前只是风格标注，不改变语义",
    "pub": "pub　目前只是标记，不改变语义",
    "unsafe": "unsafe { }　目前就是普通块",
    "raise": 'raise "消息"　panic 的糖',
    "cxx": "use cxx　C++ 互操作（自动生成 shim，自动加 -lstdc++）",
    "py": "use py　嵌入 CPython", "java": "use java　嵌入 JVM",
    "typeof": "typeof　**占了位置但没实现**，写了是语法错误",
    "static": "static　同上，没实现",
    "where": "where　同上，没实现",
    "try": "try　同上，没实现（FA 没有异常）",
    "catch": "catch　同上，没实现",
    "ref": "ref　同上，没实现",
    "move": "move　同上，没实现",
    "dyn": "dyn　同上，没实现",
    "trait": "trait　同上，没实现（要共用行为就用 impl 给同一个类型加方法）",
    "mod": "mod　同上，没实现（模块是 use \"文件.fa\"）",
    "libc": "libc　use c 里的库名占位",
}
SNIPPETS = [
    ("fn", "fn ${1:名字}(${2:x: i64}) -> ${3:i64}:\n    ${4:return 0}", "函数定义"),
    ("struct", "struct ${1:P}:\n    ${2:x: i64}\n    ${3:y: str = \"\"}", "结构体定义"),
    ("enum", "enum ${1:E}:\n    ${2:Nothing}\n    ${3:Just(v: i64)}", "枚举定义"),
    ("impl", "impl ${1:P}:\n    fn ${2:to_str}(self) -> str:\n        return \"\"", "给类型加方法"),
    ("main", "fn main() -> i64:\n    ${1}\n    return 0", "程序入口"),
    ("for", "for ${1:x} in ${2:v}:\n    ${3}", "遍历容器"),
    ("fori", "for i in 0..${1:n}:\n    ${2}", "按下标走（0..n 不含 n）"),
    ("forrange", "for ${1:i} in range(${2:0}, ${3:n}, ${4:2}):\n    ${5}",
     "带步长地走（步长可以是负的；0 不行）"),
    ("fortwo", "for ${1:i}, ${2:x} in ${3:v}:\n    ${4}",
     "同时拿下标和元素（Vec / 数组 / str）"),
    ("forkv", "for ${1:k}, ${2:v} in ${3:m}:\n    ${4}",
     "同时拿键和值（Map）"),
    ("slice", "${1:v}[${2:1}:${3:3}]",
     "切片：str 和 Vec 都认，返回新值；省掉一头就写 v[1:] / v[:3] / v[:]"),
    ("while", "while ${1:条件}:\n    ${2}", "while 循环"),
    ("if", "if ${1:条件}:\n    ${2}\nelse:\n    ${3}", "if/else"),
    ("match", "match ${1:值}:\n    ${2:变体}:\n        ${3}\n    _:\n        ${4}", "模式匹配"),
    ("usec", 'use c "${1:头文件.h}" lib "${2:m}":\n    fn ${3:名字}(${4}) -> ${5:i64}', "声明 C 函数并链接"),
    ("usestd", "use std.${1|fs,time,re,json,args|}", "导入标准库模块"),
    ("defer", "defer ${1:free(p)}", "作用域退出时执行"),
    ("sortby", "fn ${1:by_key}(p: *${2:P}) -> i64: return p.${3:字段}\n${4:v}.sort_by($1)",
     "按字段排序（sort_by 要一个顶层取键函数）"),
]
TYPE_NAMES = ["i8", "i16", "i32", "i64", "isize", "u8", "u16", "u32", "u64",
              "usize", "f32", "f64", "bool", "char", "str", "void", "any",
              "Vec", "Map"]
TYPE_DOC = {
    "i64": "i64　有符号整数（默认整数类型），零值 0",
    "f64": "f64　浮点（默认浮点类型），零值 0.0",
    "char": "char　**一个 UTF-8 字节**，不是一个字符（'你' 编不过）",
    "str": "str　不可变字符串，引用计数，零值 \"\"",
    "bool": "bool　true / false",
    "void": "void　没有返回值（只在 fn 的返回位置写）",
    "any": "any　内部用的「随便什么」，日常别写",
    "Vec": "Vec<T>　变长表，引用计数；元素是结构体/枚举时装箱",
    "Map": "Map<K,V>　哈希表；键只能是 str/整数/浮点/bool/char/指针",
    "u8": "u8　无符号字节；*u8 就是 C 的 char*",
}


def _arity_text(kind, name):
    lo_hi = (METHOD_ARITY.get(kind) or {}).get(name)
    if not lo_hi:
        return ""
    lo, hi = lo_hi
    if hi is None:
        return f"（至少 {lo} 个参数）"
    if lo == hi:
        return f"（{lo} 个参数）" if lo else "（不带参数）"
    return f"（{lo}~{hi} 个参数）"


def _item(label, kind, detail="", insert=None, sort=None):
    return {"label": label, "kind": kind, "kindName": KIND_NAME.get(kind, ""),
            "detail": detail, "insertText": insert or label,
            "sortText": sort or label}


# ------------------------------------------------------------------ 名字收集
def _ty_text(t):
    """把一个类型渲染成人看得懂的文本。

    两种来源都得认：sema 的 Type（`__str__` 本来就好看：Vec<P> / Map<str,i64> / *u8），
    以及 parser 的 TName / TPtr / TArr / TFn —— 后者不渲染就会打成
    `TName(name='str', args=[])`（第一版悬停结构体时正是这样，难看且没用）。
    """
    if t is None:
        return ""
    cls = type(t).__name__
    if cls == "TName":
        args = [_ty_text(x) for x in (getattr(t, "args", None) or [])]
        return t.name + (f"<{', '.join(args)}>" if args else "")
    if cls == "TPtr":
        return "*" + _ty_text(getattr(t, "inner", None))
    if cls == "TArr":
        return f"[{_ty_text(getattr(t, 'inner', None))}; {getattr(t, 'count', '?')}]"
    if cls == "TFn":
        ps = ", ".join(_ty_text(x) for x in (getattr(t, "params", None) or []))
        r = _ty_text(getattr(t, "ret", None))
        return "fn(" + ps + ")" + (f" -> {r}" if r and r != "void" else "")
    if cls == "TOptional":
        return _ty_text(getattr(t, "inner", None)) + "?"
    return str(t)


def _expr_text(e):
    """字段默认值这类小表达式渲染成文本；复杂的就给个省略号，不硬猜。"""
    if e is None:
        return ""
    cls = type(e).__name__
    if cls == "NumLit":
        return str(getattr(e, "value", ""))
    if cls == "StrLit":
        return '"' + str(getattr(e, "value", "")) + '"'
    if cls == "CharLit":
        return "'" + str(getattr(e, "value", "")) + "'"
    if cls == "BoolLit":
        return "true" if getattr(e, "value", False) else "false"
    if cls == "NilLit":
        return "nil"
    if cls == "NameRef":
        return str(getattr(e, "name", ""))
    if cls == "Unary":
        return str(getattr(e, "op", "")) + _expr_text(getattr(e, "operand", None))
    return "…"


def _doc_comment_above(src, line):
    """取声明上方那几行连续注释（# 或 //），当悬停文档用。"""
    ls = _lines(src)
    out = []
    # AST 上的行号偶尔会指到文件末尾之外（节点继承父节点位置的兜底逻辑造成的），
    # 不夹一下就会 IndexError —— 取文档注释而已，越界就当没有。
    i = min(int(line or 1) - 2, len(ls) - 1)
    while i >= 0:
        t = ls[i].strip()
        if t.startswith("#") or t.startswith("//"):
            body = t.lstrip("#/").strip()
            out.append(body)
            i -= 1
            continue
        break
    out.reverse()
    while out and not out[0]:
        out.pop(0)
    return "\n".join(out[:12])


def _fn_sig(fs):
    """把一个 FnSym / FnDef 拼成人看得懂的签名。"""
    name = getattr(fs, "name", "?")
    params = getattr(fs, "params", None) or []
    ps = []
    for p in params:
        pn = getattr(p, "name", None) or (p[0] if isinstance(p, (tuple, list)) else "?")
        pt = getattr(p, "ty", None) or (p[1] if isinstance(p, (tuple, list)) and len(p) > 1 else None)
        ps.append(f"{pn}: {_ty_text(pt)}" if pt is not None else str(pn))
    ret = getattr(fs, "ret", None)
    rt = _ty_text(ret)
    tail = f" -> {rt}" if rt and rt != "void" else ""
    pre = "extern " if getattr(fs, "extern", False) else ""
    return f"{pre}fn {name}({', '.join(ps)}){tail}"


_STDLIB_REAL = None


def _stdlib_root():
    global _STDLIB_REAL
    if _STDLIB_REAL is None:
        try:
            from falang.sema import STDLIB_DIR
            _STDLIB_REAL = os.path.realpath(STDLIB_DIR)
        except Exception:
            _STDLIB_REAL = ""
    return _STDLIB_REAL


def _is_stdlib_decl(d):
    """这条声明是不是 stdlib 里的（parse 时打上的 file 标记，见 parser.parse）。"""
    f = getattr(d, "file", None)
    if not f:
        return False
    root = _stdlib_root()
    return bool(root) and os.path.realpath(f).startswith(root + os.sep)


def collect_decls(src, a: Analysis):
    """从 AST/sema 收集这个文件里的声明：函数、类型、常量、全局变量、方法。"""
    fns, structs, enums, impls, consts, globals_ = {}, {}, {}, {}, {}, {}
    impls_file = {}                     # 类型名 -> 它的 impl 写在哪个文件（跳定义用）
    mod = a.mod
    if mod is not None:
        for d in getattr(mod, "decls", []) or []:
            cls = type(d).__name__
            ln = getattr(d, "line", 0) or 0
            # 函数、常量、全局变量只收本文件（含 `use "自己写的.fa"` 带进来的）；
            # stdlib 那些是内部件（time_parts_of / s_cstr / fs_walk_into），
            # 混进补全列表纯属噪音 —— 它们的正规入口是 Time. / Fs. 静态方法，
            # 成员补全里给得到。类型（struct/enum）和方法组（impl）仍然全收，
            # 因为 Time、FsStat 这些确实是可以直接写出来的类型名。
            foreign = cls in ("FnDef", "Const", "Global") and _is_stdlib_decl(d)
            if cls == "FnDef" and not foreign:
                fns[d.name] = {"line": ln, "sig": _fn_sig(d),
                               "doc": _doc_comment_above(src, ln),
                               "extern": bool(getattr(d, "extern", False)),
                               "local": True, "file": getattr(d, "file", None)}
            elif cls == "StructDef":
                fields = []
                defaults = getattr(d, "defaults", None) or {}
                for f in (d.fields or []):
                    fn_ = f[0] if isinstance(f, (tuple, list)) else getattr(f, "name", "?")
                    ft = f[1] if isinstance(f, (tuple, list)) and len(f) > 1 else getattr(f, "ty", None)
                    dv = _expr_text(defaults.get(fn_)) if fn_ in defaults else ""
                    # 存**渲染好的文本**（含默认值），后面所有前端直接拼就行
                    fields.append((fn_, _ty_text(ft) + (f" = {dv}" if dv else "")))
                structs[d.name] = {"line": ln, "fields": fields,
                                   "doc": _doc_comment_above(src, ln),
                                   "file": getattr(d, "file", None)}
            elif cls == "EnumDef":
                vs = []
                for v in (d.variants or []):
                    vn = v[0] if isinstance(v, (tuple, list)) else getattr(v, "name", "?")
                    vp = v[1] if isinstance(v, (tuple, list)) and len(v) > 1 else None
                    if vp:
                        vp = ", ".join(
                            f"{x[0]}: {_ty_text(x[1])}" if isinstance(x, (tuple, list)) and len(x) > 1
                            else str(x) for x in vp)
                    vs.append((vn, vp))
                enums[d.name] = {"line": ln, "variants": vs,
                                 "doc": _doc_comment_above(src, ln),
                                 "file": getattr(d, "file", None)}
            elif cls == "ImplDef":
                ms = []
                for m in (d.methods or []):
                    ms.append({"name": m.name, "line": getattr(m, "line", 0) or 0,
                               "sig": _fn_sig(m), "doc": _doc_comment_above(src, getattr(m, "line", 0) or 0)})
                impls.setdefault(d.type_name, []).extend(ms)
                impls_file.setdefault(d.type_name, getattr(d, "file", None))
            elif cls == "Const" and not foreign:
                consts[d.name] = {"line": ln, "ty": _ty_text(getattr(d, "ty", None)),
                                  "local": True, "file": getattr(d, "file", None)}
            elif cls == "Global" and not foreign:
                for g in (getattr(d, "items", None) or []):
                    gn = getattr(g, "name", None)
                    if gn:
                        globals_[gn] = {"line": ln, "ty": _ty_text(getattr(g, "ty", None)),
                                        "file": getattr(d, "file", None)}
    sema = a.sema
    if sema is not None:
        for n, fs in (sema.fns or {}).items():
            fns.setdefault(n, {"line": getattr(fs, "line", 0) or 0, "sig": _fn_sig(fs),
                               "doc": "", "extern": bool(getattr(fs, "extern", False)),
                               "local": False})
        for n, t in (sema.structs or {}).items():
            e = structs.setdefault(n, {"line": 0, "fields": [], "doc": ""})
            if not e["fields"] and getattr(t, "fields", None):
                    e["fields"] = [(f[0], _ty_text(f[1])) for f in t.fields]
        for n, t in (sema.enums or {}).items():
            e = enums.setdefault(n, {"line": 0, "variants": [], "doc": ""})
            if not e["variants"] and getattr(t, "variants", None):
                e["variants"] = [(v[0], ", ".join(
                    f"{x[0]}: {_ty_text(x[1])}" if isinstance(x, (tuple, list)) and len(x) > 1
                    else str(x) for x in v[1]) if len(v) > 1 and v[1] else None)
                    for v in t.variants]
        for n, vs in (sema.globals or {}).items():
            globals_.setdefault(n, {"line": getattr(vs, "line", 0) or 0,
                                    "ty": _ty_text(getattr(vs, "ty", None))})
        for n in (sema.consts or {}):
            consts.setdefault(n, {"line": 0, "ty": None})
    return {"fns": fns, "structs": structs, "enums": enums, "impls": impls,
            "consts": consts, "globals": globals_, "impls_file": impls_file}


def _local_types(mod, sema, local_ids=None, line=0):
    """局部变量名 -> 类型。靠 AST 节点上的 sym（sema 会把 VarSym 挂回去）。

    两个必须做的收窄，都是实测踩出来的：

    1. **只扫本文件的声明**。Sema 会把 stdlib 摊平进 mod.decls，libc.fa 里也有
       叫 `n`、`m`、`t`、`s`、`p` 的局部变量 —— 不设防的话自己写的
       `let n = Vec<i64>[…]` 会被 stdlib 的 `n: usize` 顶掉（setdefault 先到先得），
       于是 `n.` 补全给的是 usize 的成员，全错。
    2. **只扫光标所在的那个函数**。同名变量在不同函数里类型可以完全不同，
       把别的函数的局部变量也端上来，既误导又刷屏。

    同一个块里的同名变量仍然可能类型不同（不做真正的块级作用域），
    补全场景够用；悬停时优先用离光标最近的那一个。
    """
    out = {}
    if mod is None:
        return out
    tops = [d for d in (getattr(mod, "decls", None) or [])
            if not local_ids or id(d) in local_ids]
    if line:
        # 顶层函数不嵌套，所以「起始行 <= 光标行的最后一个 FnDef」就是所在函数
        cur, cur_line = None, 0
        for d in tops:
            if type(d).__name__ == "FnDef":
                ln = getattr(d, "line", 0) or 0
                if ln <= line and ln >= cur_line:
                    cur, cur_line = d, ln
        if cur is not None:
            tops = [cur]
    seen = set()

    def walk(n, depth=0):
        if n is None or depth > 60:
            return
        if isinstance(n, (list, tuple)):
            for x in n:
                walk(x, depth + 1)
            return
        if not hasattr(n, "__dataclass_fields__"):
            return                       # 类型对象、字符串常量这些不用往下走
        if id(n) in seen:
            return                       # AST 里有共享/回指的节点，不设防会转圈
        seen.add(id(n))
        cls = type(n).__name__
        if cls == "Let" and getattr(n, "sym", None) is not None:
            ty = getattr(n.sym, "ty", None)
            if ty is not None and getattr(n, "name", None):
                out.setdefault(n.name, ty)
        elif cls == "For" and getattr(n, "sym", None) is not None:
            # for 的循环变量在 .var 上（不是 .name），sym 一样挂着类型
            ty = getattr(n.sym, "ty", None)
            if ty is not None and getattr(n, "var", None):
                out.setdefault(n.var, ty)
        elif cls == "FnDef":
            for p in (getattr(n, "params", None) or []):
                if getattr(p, "ty", None) is not None:
                    out.setdefault(p.name, p.ty)
        for f in n.__dataclass_fields__:
            if f.startswith("_") or f == "sym":
                continue
            walk(getattr(n, f, None), depth + 1)

    walk(tops)
    return out


# ------------------------------------------------------------------ 补全
def _members_of(ty, sema, decls):
    """一个类型的成员（字段 + 方法），补全和悬停共用。"""
    items = []
    if ty is None:
        return items
    kind = getattr(ty, "kind", None)
    if kind == "str":
        for n in sorted(BUILTIN_METHODS.get("str", ())):
            items.append(_item(n, K_METHOD, STR_DOC.get(n, "") or _arity_text("str", n),
                               insert=n + "()" if "（不带参数）" in _arity_text("str", n) else n))
    elif kind == "vec":
        et = getattr(ty, "elem", None)
        for n in sorted(BUILTIN_METHODS.get("vec", ())):
            d = VEC_DOC.get(n, "") or _arity_text("vec", n)
            items.append(_item(n, K_METHOD, f"{d}　[Vec<{_ty_text(et)}>]", insert=n))
    elif kind == "map":
        for n in sorted(BUILTIN_METHODS.get("map", ())):
            d = MAP_DOC.get(n, "") or _arity_text("map", n)
            items.append(_item(n, K_METHOD,
                               f"{d}　[Map<{_ty_text(ty.key)},{_ty_text(ty.val)}>]", insert=n))
    elif kind in ("int", "float"):
        for n in sorted(NUM_DOC):
            items.append(_item(n, K_METHOD, NUM_DOC[n], insert=n))
    elif kind == "bool" or kind == "char":
        items.append(_item("to_str", K_METHOD, NUM_DOC["to_str"], insert="to_str"))
    elif kind == "ptr":
        inner = getattr(ty, "inner", None)
        items.append(_item("cstr", K_METHOD, "cstr() -> str　*char/*u8 按 C 字符串取内容", insert="cstr"))
        if inner is not None:
            for it in _members_of(inner, sema, decls):
                it = dict(it)
                it["detail"] = (it.get("detail") or "") + "　（指针会自动解引用）"
                items.append(it)
    if kind in ("struct", "enum"):
        name = getattr(ty, "name", "")
        if kind == "struct":
            for f in (getattr(ty, "fields", None) or []):
                fn_, ft = f[0], f[1]
                items.append(_item(fn_, K_FIELD,
                                   f"{fn_}: {_ty_text(ft)}　[{name} 的字段]", insert=fn_))
        else:
            for v in (getattr(ty, "variants", None) or []):
                vn = v[0] if isinstance(v, (tuple, list)) else getattr(v, "name", "?")
                payload = v[1] if isinstance(v, (tuple, list)) and len(v) > 1 else None
                has = bool(payload)
                items.append(_item(vn, K_ENUM_MEMBER,
                                   f"{name}.{vn}({'…' if has else ''})　枚举变体",
                                   insert=vn + "(" if has else vn))
        if sema is not None:
            for (tn, mn), fs in (sema.methods or {}).items():
                if tn == name:
                    items.append(_item(mn, K_METHOD, f"{_fn_sig(fs)}　[{name} 的方法]", insert=mn))
        for m in (decls or {}).get("impls", {}).get(name, []):
            if not any(i["label"] == m["name"] for i in items):
                items.append(_item(m["name"], K_METHOD, m["sig"], insert=m["name"]))
    return items


def _all_members(sema, decls):
    """定不出接收者类型时的兜底：把所有内建方法都列出来，标明属于谁。"""
    items = []
    for kind, table, doc in (("str", BUILTIN_METHODS.get("str", ()), STR_DOC),
                             ("Vec", BUILTIN_METHODS.get("vec", ()), VEC_DOC),
                             ("Map", BUILTIN_METHODS.get("map", ()), MAP_DOC)):
        for n in sorted(table):
            items.append(_item(n, K_METHOD, f"{doc.get(n, '')}　[{kind} 的方法]",
                               insert=n, sort=f"{kind}.{n}"))
    for n in sorted(NUM_DOC):
        items.append(_item(n, K_METHOD, f"{NUM_DOC[n]}　[数值的方法]", insert=n, sort=f"num.{n}"))
    return items


def _modules():
    mods = []
    if stdlib_modules is not None:
        try:
            for m in stdlib_modules():
                mods.append(m.split("std.", 1)[-1] if m.startswith("std.") else m)
        except Exception:
            mods = []
    if not mods:
        d = os.path.join(_ROOT, "stdlib")
        if os.path.isdir(d):
            mods = sorted(f[:-3] for f in os.listdir(d) if f.endswith(".fa"))
    return sorted(set(mods))


def complete(src, line, col, a: Analysis = None):
    """光标处的补全。返回 {"context": ..., "items": [...]}，items 已按前缀过滤。"""
    if a is None:
        a = analyze(src, full=False)
    decls = collect_decls(src, a)
    text = _line_text(src, line)
    before = text[:max(0, col - 1)]

    # 1) use 后面给模块名 / 路径
    m = re.search(r"\buse\s+(std\.)?([A-Za-z_0-9.]*)$", before)
    if m:
        pre = m.group(2) or ""
        items = [_item(n, K_MODULE, f"use std.{n}　标准库模块（stdlib/{n}.fa）",
                       insert=n) for n in _modules()]
        items.append(_item('use "…"', K_SNIPPET,
                           'use "文件.fa"　导入自己的 FA 文件（路径相对当前文件）',
                           insert='"${1:mod.fa}"', sort="~use"))
        return {"context": "use", "items": _filter(items, pre)}

    # 2) 类型位置：`-> T`、`x: T`、`s: *T`、`Vec<T` 都给类型名
    tm = (re.search(r"->\s*\*?([A-Za-z_0-9\[\]]*)$", before)
          or re.search(r"[A-Za-z_][A-Za-z_0-9]*\s*:\s*\*?([A-Za-z_0-9\[\]]*)$", before)
          or re.search(r"[A-Za-z_][A-Za-z_0-9]*<\s*,?\s*([A-Za-z_0-9\[\]*]*)$", before))
    if tm:
        pre = tm.group(1)
        items = [_item(t, K_TYPE, TYPE_DOC.get(t, f"{t}　类型"), insert=t) for t in TYPE_NAMES]
        items += [_item(n, K_CLASS, f"{n}　你自己定义的结构体", insert=n) for n in decls["structs"]]
        items += [_item(n, K_CLASS, f"{n}　你自己定义的枚举", insert=n) for n in decls["enums"]]
        return {"context": "type", "items": _filter(items, pre)}

    # 3) 成员补全：光标前是 `接收者.部分名字`
    m = re.match(r"^(.*?)([A-Za-z_][A-Za-z_0-9]*(?:\([^()]*\)|\[[^\]]*\])*)"
                 r"\.([A-Za-z_0-9]*)$", before)
    dot = re.search(r"\.([A-Za-z_0-9]*)$", before)
    if dot:
        pre = dot.group(1)
        recv = before[:dot.start()].rstrip()
        ty, why = _receiver_type(recv, a, decls, src, line)
        items = _members_of(ty, a.sema, decls) if ty is not None else []
        if not items:
            items = _all_members(a.sema, decls)
            why = why or "定不出接收者的类型，给的是全集"
        return {"context": "member", "receiver": recv, "why": why or "",
                "items": _filter(items, pre)}

    # 4) 普通补全
    pre_m = re.search(r"([A-Za-z_][A-Za-z_0-9]*)$", before)
    pre = pre_m.group(1) if pre_m else ""
    items = []
    for k in sorted(KEYWORDS):
        items.append(_item(k, K_KEYWORD, KEYWORD_DOC.get(k, f"{k}　关键字"), insert=k))
    for n in sorted(BUILTIN_FNS):
        items.append(_item(n, K_FUNCTION, BUILTIN_DOC.get(n, f"{n}()　内建函数"), insert=n))
    for n, d in decls["fns"].items():
        if not d.get("local", True):
            continue        # stdlib 摊平进来的内部函数（time_parts_of / s_cstr 之类）；
                            # 模块的正规入口是 Time. / Fs. 这些静态方法，成员补全里给
        items.append(_item(n, K_FUNCTION, d["sig"] + ("　[extern]" if d["extern"] else ""),
                           insert=n))
    for n, d in decls["structs"].items():
        fs = ", ".join(f"{x}: {y}" for x, y in d["fields"][:4])
        items.append(_item(n, K_CLASS, f"struct {n} {{ {fs} }}", insert=n))
    for n, d in decls["enums"].items():
        vs = ", ".join(v[0] for v in d["variants"][:5])
        items.append(_item(n, K_CLASS, f"enum {n} {{ {vs} }}", insert=n))
    for n, d in decls["consts"].items():
        items.append(_item(n, K_VALUE, f"const {n}" + (f": {d['ty']}" if d.get("ty") else ""), insert=n))
    for n, d in decls["globals"].items():
        items.append(_item(n, K_VARIABLE, f"全局 {n}" + (f": {d['ty']}" if d.get("ty") else ""), insert=n))
    for n, ty in sorted(_local_types(a.mod, a.sema, a.local_ids, line).items()):
        items.append(_item(n, K_VARIABLE, f"let {n}: {_ty_text(ty)}", insert=n))
    for t in TYPE_NAMES:
        items.append(_item(t, K_TYPE, TYPE_DOC.get(t, f"{t}　类型"), insert=t))
    for label, ins, why in SNIPPETS:
        items.append(_item(label, K_SNIPPET, f"片段：{why}", insert=ins, sort=f"~{label}"))
    return {"context": "global", "items": _filter(items, pre)}


def _filter(items, pre):
    if not pre:
        # 没敲字时把 _ 开头的内部件藏起来（Fs._ns 这类），敲了 _ 再给
        return [i for i in items if not i["label"].startswith("_")]
    low = pre.lower()
    hit = [i for i in items if i["label"].lower().startswith(low)]
    if len(pre) >= 3:
        # 子串匹配只对够长的前缀开：一两个字母就模糊匹配，敲 `t` 能把 `let` 也捞出来
        seen = {h["label"].lower() for h in hit}
        hit += [i for i in items
                if i["label"].lower() not in seen and low in i["label"].lower()]
    return hit


def _receiver_type(recv, a: Analysis, decls, src, line):
    """从接收者文本定类型。只认「一个名字」这种最简单也最常见的情况。

    链式（`v.get(0).`）和下标（`m[k].`）定不出来 —— 那需要把表达式重新做一遍
    类型推导，投入产出不划算；这时返回 None，调用方给全集并说明原因。
    """
    recv = (recv or "").strip()
    if not recv:
        return None, ""
    # `print("map:", n.` 这种：真正的接收者是末尾那个 n，前面的 print( 是另一层。
    # 但 `v.get(0).` / `p.q.` 的接收者是一个表达式的结果，光看名字定不出来 ——
    # 前者认输给全集，后者还能顺着结构体字段类型走一步。
    chain = re.search(r"([A-Za-z_][A-Za-z_0-9]*)\.([A-Za-z_][A-Za-z_0-9]*)$", recv)
    m = re.fullmatch(r"([A-Za-z_][A-Za-z_0-9]*)", recv) or \
        re.search(r"(?:^|[^A-Za-z_0-9.\)\]])([A-Za-z_][A-Za-z_0-9]*)$", recv)
    if not m:
        if chain:
            base, why = _receiver_type(chain.group(1), a, decls, src, line)
            fld = chain.group(2)
            if base is not None and getattr(base, "kind", None) == "struct":
                for f in (getattr(base, "fields", None) or []):
                    if f[0] == fld:
                        return f[1], f"{chain.group(1)}.{fld}: {_ty_text(f[1])}"
            if base is not None and getattr(base, "kind", None) == "ptr":
                inner = getattr(base, "inner", None)
                if inner is not None and getattr(inner, "kind", None) == "struct":
                    for f in (getattr(inner, "fields", None) or []):
                        if f[0] == fld:
                            return f[1], (f"{chain.group(1)}.{fld}: "
                                          f"{_ty_text(f[1])}（指针自动解引用）")
        return None, "接收者是一个表达式的结果（链式调用/下标），定不出类型"
    name = m.group(1)
    # 类型名：Color. 给枚举变体，P. 给静态方法
    if a.sema is not None and name in (a.sema.enums or {}):
        return a.sema.enums[name], f"{name} 是枚举，给它的变体"
    if a.sema is not None and name in (a.sema.structs or {}):
        return a.sema.structs[name], f"{name} 是结构体，给它 impl 里的静态方法"
    if name in decls["enums"]:
        ty = (a.sema.enums or {}).get(name) if a.sema else None
        return ty, f"{name} 是枚举"
    if name in decls["structs"]:
        ty = (a.sema.structs or {}).get(name) if a.sema else None
        return ty, f"{name} 是结构体"
    # 变量：先查局部（AST 上的 sym），再查全局
    loc = _local_types(a.mod, a.sema, getattr(a, "local_ids", None), line)
    ty = loc.get(name)
    if ty is not None:
        return ty, f"{name}: {_ty_text(ty)}"
    if a.sema is not None and name in (a.sema.globals or {}):
        ty = a.sema.globals[name].ty
        return ty, f"{name}: {_ty_text(ty)}（全局）"
    return None, f"定不出 {name} 的类型（可能这一行还有别的错）"


# ------------------------------------------------------------------ 悬停
def hover(src, line, col, a: Analysis = None):
    """光标处的悬停文档（markdown）。没有可说的就返回 None。"""
    if a is None:
        a = analyze(src, full=False)
    word, c0, c1 = _word_at(src, line, col)
    if not word:
        return None
    decls = collect_decls(src, a)
    text = _line_text(src, line)
    before = text[:max(0, c0 - 1)]
    is_member = before.rstrip().endswith(".")

    if is_member:
        recv = before.rstrip()[:-1].strip()
        ty, _why = _receiver_type(recv, a, decls, src, line)
        kind = getattr(ty, "kind", None) if ty is not None else None
        table = {"str": STR_DOC, "vec": VEC_DOC, "map": MAP_DOC}.get(kind)
        if table and word in table:
            rname = _why.split(":")[0].strip() or recv     # why 形如 "n: Vec<i64>"
            return (f"**{word}()**\n\n{table[word]}\n\n"
                    f"*{rname}* 的方法")
        if kind in ("int", "float", "bool", "char") and word in NUM_DOC:
            rname = _why.split(":")[0].strip() or recv
            return (f"**{word}()**\n\n{NUM_DOC[word]}\n\n"
                    f"*{rname}* 的方法")
        if ty is not None and kind in ("struct", "enum"):
            nm = getattr(ty, "name", "")
            if kind == "struct":
                for f in (getattr(ty, "fields", None) or []):
                    if f[0] == word:
                        return (f"**{nm}.{word}**\n\n```fa\n{word}: "
                                f"{_ty_text(f[1])}\n```\n\n{nm} 的字段")
            else:
                for v in (getattr(ty, "variants", None) or []):
                    if v[0] == word:
                        pay = v[1] if len(v) > 1 else None
                        ps = ""
                        if pay:
                            ps = "(" + ", ".join(
                                f"{x[0]}: {x[1]}" if isinstance(x, (tuple, list)) else str(x)
                                for x in pay) + ")"
                        return f"**{nm}.{word}{ps}**\n\n枚举变体"
            if a.sema is not None and (nm, word) in (a.sema.methods or {}):
                fs = a.sema.methods[(nm, word)]
                doc = ""
                for ms in decls["impls"].get(nm, []):
                    if ms["name"] == word:
                        doc = ms.get("doc") or ""
                body = f"```fa\n{_fn_sig(fs)}\n```"
                return f"**{nm}.{word}**\n\n{body}" + (f"\n\n{doc}" if doc else "")
        for it in _all_members(a.sema, decls):
            if it["label"] == word:
                return f"**{word}()**\n\n{it['detail']}"
        return None

    if word in decls["fns"]:
        d = decls["fns"][word]
        out = f"```fa\n{d['sig']}\n```"
        if d["doc"]:
            out += f"\n\n{d['doc']}"
        return out
    if word in decls["structs"]:
        d = decls["structs"][word]
        fs = "\n".join(f"    {x}: {y}" for x, y in d["fields"])
        out = f"```fa\nstruct {word}:\n{fs}\n```"
        return out + (f"\n\n{d['doc']}" if d["doc"] else "")
    if word in decls["enums"]:
        d = decls["enums"][word]
        vs = []
        for vn, pay in d["variants"]:
            if pay:
                ps = ", ".join(f"{x[0]}: {x[1]}" if isinstance(x, (tuple, list)) else str(x)
                               for x in pay)
                vs.append(f"    {vn}({ps})")
            else:
                vs.append(f"    {vn}")
        out = f"```fa\nenum {word}:\n" + "\n".join(vs) + "\n```"
        return out + (f"\n\n{d['doc']}" if d["doc"] else "")
    if word in decls["consts"]:
        return f"**const {word}**\n\n顶层常量，按使用处替换；初值必须编译期算得出来"
    if word in decls["globals"]:
        d = decls["globals"][word]
        return f"**全局 {word}**" + (f": `{d['ty']}`" if d.get("ty") else "")
    loc = _local_types(a.mod, a.sema, getattr(a, "local_ids", None), line)
    if word in loc:
        return f"**let {word}**: `{_ty_text(loc[word])}`"
    if word in BUILTIN_DOC:
        return f"**{word}**\n\n{BUILTIN_DOC[word]}\n\n内建函数"
    if word in KEYWORD_DOC:
        return f"**{word}**\n\n{KEYWORD_DOC[word]}\n\n关键字"
    if word in TYPE_DOC:
        return f"**{word}**\n\n{TYPE_DOC[word]}"
    return None


# ------------------------------------------------------------------ 符号 / 签名
def symbols(src, a: Analysis = None):
    """文件里的大件：函数、结构体、枚举、impl、常量、全局、use。"""
    if a is None:
        a = analyze(src, full=False)
    out = []
    mod = a.mod
    if mod is None:
        return out
    for d in getattr(mod, "decls", []) or []:
        if a.local_ids and id(d) not in a.local_ids:
            continue                    # stdlib 摊平进来的，不属于这个文件
        cls = type(d).__name__
        ln = getattr(d, "line", 0) or 0
        if cls == "FnDef":
            out.append({"name": d.name, "kind": "函数", "line": ln,
                        "detail": _fn_sig(d)})
        elif cls == "StructDef":
            out.append({"name": d.name, "kind": "结构体", "line": ln,
                        "detail": ", ".join(str(f[0]) for f in (d.fields or []))})
        elif cls == "EnumDef":
            out.append({"name": d.name, "kind": "枚举", "line": ln,
                        "detail": ", ".join(str(v[0]) for v in (d.variants or []))})
        elif cls == "ImplDef":
            out.append({"name": f"impl {d.type_name}", "kind": "方法组", "line": ln,
                        "detail": ", ".join(m.name for m in (d.methods or []))})
            for m in (d.methods or []):
                out.append({"name": f"{d.type_name}.{m.name}", "kind": "方法",
                            "line": getattr(m, "line", 0) or 0, "detail": _fn_sig(m)})
        elif cls == "Const":
            out.append({"name": d.name, "kind": "常量", "line": ln, "detail": "const"})
        elif cls == "Global":
            for g in (getattr(d, "items", None) or []):
                out.append({"name": getattr(g, "name", "?"), "kind": "全局",
                            "line": ln, "detail": str(getattr(g, "ty", "") or "")})
        elif cls == "Use":
            out.append({"name": f"use {getattr(d, 'path', '') or getattr(d, 'kind', '')}",
                        "kind": "导入", "line": ln, "detail": getattr(d, "kind", "")})
    return out


def _local_decl_line(mod, sema, local_ids, line, name):
    """本文件里、光标所在函数中，这个名字是在哪一行 let / for 出来的。"""
    if mod is None or not name:
        return 0
    tops = [d for d in (getattr(mod, "decls", None) or [])
            if not local_ids or id(d) in local_ids]
    cur, cur_line = None, 0
    if line:
        for d in tops:
            if type(d).__name__ == "FnDef":
                ln = getattr(d, "line", 0) or 0
                if ln <= line and ln >= cur_line:
                    cur, cur_line = d, ln
    roots = [cur] if cur is not None else tops
    hit = [0]
    seen = set()

    def walk(x, depth=0):
        if x is None or depth > 60 or hit[0]:
            return
        if isinstance(x, (list, tuple)):
            for y in x:
                walk(y, depth + 1)
            return
        if not hasattr(x, "__dataclass_fields__") or id(x) in seen:
            return
        seen.add(id(x))
        cls = type(x).__name__
        if cls == "Let" and getattr(x, "name", None) == name:
            hit[0] = getattr(x, "line", 0) or 0
            return
        if cls == "For" and getattr(x, "var", None) == name:
            hit[0] = getattr(x, "line", 0) or 0
            return
        if cls == "FnDef" and any(getattr(pp, "name", None) == name
                                  for pp in (getattr(x, "params", None) or [])):
            hit[0] = getattr(x, "line", 0) or 0
            return
        for f in x.__dataclass_fields__:
            if f.startswith("_") or f == "sym":
                continue
            walk(getattr(x, f, None), depth + 1)

    for r in roots:
        walk(r)
        if hit[0]:
            break
    return hit[0]


def goto_definition(src, line, col, a: Analysis = None):
    """跳到定义。返回 (文件路径 or None 表示本文件, 行, 列)，找不到返回 None。

    stdlib 的符号也能跳 —— parse 给每条顶层声明打了 file 标记（parser.parse），
    所以在 IDE 里点 Time.format 会直接打开 stdlib/time.fa 那一行。
    """
    if a is None:
        a = analyze(src, full=False)
    word, wcol, _wend = _word_at(src, line, col)
    if not word:
        return None
    text = _line_text(src, line)
    decls = collect_decls(src, a)

    # 1) 点在 `X.method` 的 method 上：X 是类型名就找它的 impl
    pre = text[:max(0, wcol - 1)]
    m = re.search(r"([A-Za-z_][A-Za-z_0-9]*)\s*\.\s*$", pre)
    if m:
        for mm in (decls["impls"].get(m.group(1)) or []):
            if mm.get("name") == word and mm.get("line"):
                return (decls["impls_file"].get(m.group(1)), mm["line"], 1)

    # 2) 局部变量 / 形参：找所在函数里的那一行
    ln = _local_decl_line(a.mod, a.sema, a.local_ids, line, word)
    if ln:
        return (None, ln, 1)

    # 3) 顶层声明（函数、结构体、枚举、常量、全局）
    for key in ("fns", "structs", "enums", "consts", "globals"):
        d = decls[key].get(word)
        if d and d.get("line"):
            return (d.get("file"), d["line"], 1)
    return None


def signature(src, line, col, a: Analysis = None):
    """光标在 `f(a, b|` 里时给签名提示。返回 {label, params, active} 或 None。"""
    if a is None:
        a = analyze(src, full=False)
    text = _line_text(src, line)
    upto = text[:max(0, col - 1)]
    depth, cut = 0, -1
    for i in range(len(upto) - 1, -1, -1):
        ch = upto[i]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth == 0:
                cut = i
                break
            depth -= 1
    if cut < 0:
        return None
    head = upto[:cut].rstrip()
    m = re.search(r"([A-Za-z_][A-Za-z_0-9.]*)$", head)
    if not m:
        return None
    name = m.group(1)
    active = upto[cut + 1:].count(",")
    decls = collect_decls(src, a)
    params, label = None, None
    short = name.split(".")[-1]
    if short in decls["structs"]:
        # 结构体字面量 P{ ... } / P( ... )：把字段当「参数」提示，
        # 写 P{ 的时候最容易记不清字段叫什么、有没有默认值
        d = decls["structs"][short]
        fs = [f"{x}: {y}" for x, y in d["fields"]]
        return {"label": f"struct {short} {{ " + ", ".join(fs) + " }",
                "params": [x for x, _ in d["fields"]],
                "active": min(active, max(0, len(d["fields"]) - 1)), "name": name}
    if "." in name:
        # `Time.format(` / `Fs.read(` 这种模块静态方法（stdlib 的公开入口就是它）
        tname, mname = name.rsplit(".", 1)
        fs = None
        for m2 in (decls["impls"].get(tname) or []):
            if getattr(m2, "name", None) == mname:
                fs = m2
                break
        if fs is None and a.sema is not None:
            fs = (getattr(a.sema, "methods", None) or {}).get((tname, mname))
        if fs is not None:
            ps = [getattr(x, "name", str(x)) for x in (getattr(fs, "params", None) or [])]
            return {"label": f"{tname}." + _fn_sig(fs), "params": ps,
                    "active": min(active, max(0, len(ps) - 1)), "name": name}

    doc = None
    for table in (VEC_DOC, STR_DOC, MAP_DOC, NUM_DOC, BUILTIN_DOC):
        if short in table:
            doc = table[short]
            break
    if doc is not None:
        mm = re.match(r"([\w.]+\([^)]*\)[^　\n]*)", doc)
        head = mm.group(1) if mm else doc.split("　")[0]
        pm = re.search(r"\(([^)]*)\)", head)
        raw = pm.group(1).strip() if pm else ""
        params = []
        for x in raw.split(","):
            x = x.strip().lstrip("*")
            if not x:
                continue
            params.append(x.split("[")[0].strip().rstrip("]") or x)
        return {"label": head, "params": params,
                "active": min(active, max(0, len(params) - 1)),
                "name": name, "doc": doc}
    if short in decls["fns"]:
        label = decls["fns"][short]["sig"]
    elif a.sema is not None and short in (a.sema.fns or {}):
        fs = a.sema.fns[short]
        label = _fn_sig(fs)
        params = [getattr(p, "name", str(p)) for p in (fs.params or [])]
    if label is None:
        return None
    if params is None:
        mm = re.search(r"\((.*)\)", label, re.S)
        params = [x.strip().split(":")[0].strip() for x in mm.group(1).split(",")] if mm and mm.group(1).strip() else []
    return {"label": label, "params": params,
            "active": min(active, max(0, len(params) - 1)), "name": name}


# ------------------------------------------------------------------ 命令行自检
def _selftest():
    """`python3 lsp/fa_lang.py` 直接跑：拿仓库里的真文件当输入，看各项服务出不出货。"""
    root = _ROOT
    src = open(os.path.join(root, "tests/cases/154_vec_hof.fa"), encoding="utf-8").read()
    diags, a = check(src, os.path.join(root, "tests/cases/154_vec_hof.fa"))
    print(f"[诊断] ok={a.ok} 用时 {a.elapsed_ms:.1f}ms 条数={len(diags)}")
    for d in diags:
        print("   ", d.text())
    bad = "fn main() -> i64:\n\tlet x = 1\n    print(x：1)\n    return 0\n"
    diags, a2 = check(bad)
    print(f"[诊断·错例] ok={a2.ok} 条数={len(diags)}")
    for d in diags:
        print("   ", d.text())
    ls = src.split("\n")
    for i, t in enumerate(ls):
        if "n.map(double)" in t:
            col = t.index("n.map") + 3      # 1 起，正好停在 "n." 之后
            r = complete(src, i + 1, col)
            print(f"[补全·成员] 上下文={r['context']} 接收者={r.get('receiver')} "
                  f"理由={r.get('why','')} 项数={len(r['items'])}")
            for it in r["items"][:5]:
                print("   ", it["label"], "|", it["detail"][:60])
            hc = t.index("n.map") + 4      # 停在 map 这个词上（1 起）
            print("[悬停]", (hover(src, i + 1, hc) or "(无)").replace("\n", " ⏎ ")[:140])
            sig = signature(src, i + 1, t.index("map(") + 5)
            print("[签名·内建方法]", sig)
            break
    for i, t in enumerate(ls):
        if t.strip().startswith("let n = Vec<i64>"):
            r = complete(src, i + 1, 9)
            print(f"[补全·普通] 上下文={r['context']} 项数={len(r['items'])}")
            break
    sy = symbols(src)
    print(f"[符号] {len(sy)} 个：", ", ".join(f"{x['name']}({x['kind']}@{x['line']})" for x in sy[:6]))
    for i, t in enumerate(ls):
        if "P{name:" in t:
            print("[签名·结构体字面量]", signature(src, i + 1, t.index("P{") + 4))
            break
    print("[模块]", _modules())


if __name__ == "__main__":
    _selftest()
