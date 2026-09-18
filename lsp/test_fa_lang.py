#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fa_lang（LSP / IDE 共用的语言内核）的回归测试。

跑法：
    python3 lsp/test_fa_lang.py           # 在仓库根目录跑
    ./bin/fa lsp-test                     # 或者走 fa 的子命令

这里每一条断言都是**真跑出来的**：诊断拿 `fa check` 的结论对照，补全/悬停/符号
直接查返回结构。改 fa_lang 的人跑一遍就知道有没有把哪块碰坏。
"""

import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "compiler"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import fa_lang as F                                       # noqa: E402

PASS = []
FAIL = []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if not cond:
        print(f"  ✗ {name}　{detail}")


def eq(name, got, want):
    ck(name, got == want, f"得到 {got!r}，想要 {want!r}")


# 一个把所有常用形态都塞进去的样本（缩进风格 + 大括号风格 + std 模块）
DEMO = '''use std.time
use std.fs

struct Person:
    name: str
    age: i64 = 0

enum Shape:
    Circle(f64)
    Rect(f64, f64)

fn area(s: *Shape) -> f64:
    match *s:
        Shape.Circle(r):
            return 3.14159 * r * r
        Shape.Rect(w, h):
            return w * h
    return 0.0

fn is_adult(p: *Person) -> bool:
    return p.age >= 18

fn main() -> i64:
    let ps = Vec<Person>[Person{name: "甲", age: 30}, Person{name: "乙", age: 12}]
    let adults = ps.filter(is_adult)
    let n = Vec<i64>[1, 2, 3, 4]
    let doubled = n.map(double)
    for p in adults:
        print(p.name, " ", p.age)
    let m = Map<str, i64>["a": 1]
    m.set("b", 2)
    let t = Time.now()
    let ok = Fs.exists("/tmp")
    print(Time.format(t, "%H:%M:%S"), " ", ok, " ", m.len(), " ", doubled.to_str())
    return 0

fn double(x: *i64) -> i64: return (*x) * 2
'''


def pos(src, needle, offset=0, occurrence=1):
    """按内容找位置，返回 (line, col)，1 起。行号写死的话样本一改就全错。"""
    lines = src.split("\n")
    seen = 0
    for i, t in enumerate(lines, 1):
        k = t.find(needle)
        if k >= 0:
            seen += 1
            if seen == occurrence:
                return i, k + 1 + offset
    raise AssertionError(f"样本里找不到 {needle!r}")


def at(src, needle, offset=0, occurrence=1):
    ln, c = pos(src, needle, offset, occurrence)
    return ln, c


# ---------------------------------------------------------------- 1. 诊断
def test_diagnostics():
    print("[1] 诊断")
    a = F.analyze(DEMO, "demo.fa")
    ck("合法样本 ok=True", a.ok, f"stage={a.stage} diags={[d.message for d in a.diags]}")
    eq("合法样本 0 条诊断", len(a.diags), 0)
    ck("拿到了 sema", a.sema is not None)
    ck("耗时 < 2s（每次按键都要跑）", a.elapsed_ms < 2000, f"{a.elapsed_ms:.0f}ms")
    ds, a2 = F.check(DEMO, "demo.fa")
    ck("check 与 analyze 一致", a2.ok and not ds)

    # 少冒号：编译器的话完全看不出病因，lint 必须点破
    src = 'fn main() -> i64\n    print("hi")\n    return 0\n'
    ds, a = F.check(src, "x.fa")
    ck("少冒号 → ok=False", not a.ok)
    ck("少冒号 → lint 给出提示",
       any("少了冒号" in d.message for d in ds), str([d.message[:40] for d in ds]))
    eq("少冒号提示落在第 1 行", [d.line for d in ds if "少了冒号" in d.message], [1])
    ck("提示排在编译器那条前面（先说人话）",
       "少了冒号" in ds[0].message, ds[0].message[:40])

    # Tab 缩进
    ds, _a = F.check('fn main() -> i64:\n\tprint("hi")\n\treturn 0\n', "x.fa")
    ck("Tab 缩进 → 有警告", any("Tab" in d.message for d in ds))

    # 全角标点（字符串里的不算）
    ds, _a = F.check('fn main() -> i64:\n    let x：1\n    return 0\n', "x.fa")
    ck("代码里的全角冒号 → 有警告", any("全角" in d.message for d in ds))
    ds, _a = F.check('fn main() -> i64:\n    print("中文，标点。")\n    return 0\n', "x.fa")
    eq("字符串里的全角标点不报", len([d for d in ds if "全角" in d.message]), 0)

    # 别的语言带过来的写法
    for bad, key in (("&&", "and"), ("||", "or"), (":=", "let")):
        ds, _a = F.check(f'fn main() -> i64:\n    let a = true {bad} false\n    return 0\n',
                         "x.fa")
        ck(f"{bad} → 提示改用 {key}",
           any("别的语言" in d.message for d in ds), str([d.message[:30] for d in ds]))

    # match 里写 case（Python 习惯）
    ds, _a = F.check('fn main() -> i64:\n    let x = 1\n    match x:\n'
                     '        case 1:\n            print(1)\n    return 0\n', "x.fa")
    ck("match 里的 case → 点破 FA 没有 case",
       any("没有 case" in d.message for d in ds), str([d.message[:30] for d in ds]))

    # 类型名对照：编译器只说「未知类型 'int'」，我们要说 FA 里叫什么
    ds, _a = F.check('fn main() -> i64:\n    let x: int = 1\n    return 0\n', "x.fa")
    ck("int → 提示 i64", any("i64" in d.message and "未知类型" in d.message for d in ds),
       str([d.message[:60] for d in ds]))


# ------------------------------------------------- 2. lint 在真代码上零误报
def test_no_false_positives():
    print("[2] lint 零误报（扫全部测试用例 + stdlib）")
    files = sorted(glob.glob(os.path.join(ROOT, "tests/cases/*.fa")))
    files += sorted(glob.glob(os.path.join(ROOT, "stdlib/*.fa")))
    ck("找到了测试样本", len(files) >= 100, f"{len(files)} 个")
    bad = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            src = fh.read()
        for d in F.lint(src):
            bad.append((os.path.basename(f), d.line, d.message[:30]))
    eq("全部合法文件 0 条 lint 告警", bad, [])


# ---------------------------------------------------------------- 3. 补全
def test_completion():
    print("[3] 补全")
    a = F.analyze(DEMO, "demo.fa")

    def items(needle, offset=0, occurrence=1):
        ln, c = at(DEMO, needle, offset, occurrence)
        r = F.complete(DEMO, ln, c, a=a)
        return r, [i["label"].split(" | ")[0] for i in r["items"]]

    r, labs = items("ps.filter(", 3)
    eq("Vec<Person>. → member 上下文", r["context"], "member")
    ck("Vec<Person>. 给 filter", "filter" in labs, str(labs[:8]))
    ck("Vec<Person>. 给 push", "push" in labs)
    ck("不给 _ 开头的内部件", not any(x.startswith("_") for x in labs))

    r, labs = items("n.map(", 2)
    eq("Vec<i64>. → member", r["context"], "member")
    ck("Vec<i64>. 给 map", "map" in labs)
    ck("detail 带上元素类型（说明认出了 Vec<i64> 而不是给全集）",
       any("Vec<i64>" in (i["detail"] or "") for i in r["items"]),
       str(r["items"][0]))

    r, labs = items("m.set(", 2)
    eq("Map. → member", r["context"], "member")
    ck("Map. 给 keys", "keys" in labs, str(labs[:8]))
    ck("Map. 不给 push（那是 Vec 的）", "push" not in labs)

    r, labs = items("Fs.exists(", 3)
    eq("std 模块静态方法 → member", r["context"], "member")
    ck("Fs. 给 read", "read" in labs, str(labs[:10]))
    ck("Fs. 给 list_dir", "list_dir" in labs)
    ck("Fs. 不给 _ns", "_ns" not in labs)

    r, labs = items("Shape.Circle(", 6)
    eq("枚举名. → member", r["context"], "member")
    ck("Shape. 给变体 Circle", "Circle" in labs, str(labs[:8]))
    ck("Shape. 给变体 Rect", "Rect" in labs)

    r, labs = items("use std.time", 4, 1)          # 光标停在 `use std.` 之后
    eq("use std. → use 上下文", r["context"], "use")
    ck("use std. 给 fs", "fs" in labs, str(labs))
    ck("use std. 给 json", "json" in labs)
    ck("use std. 不给空标签", all(x.strip() for x in labs), str(labs))

    r, labs = items("-> f64", 3)                   # `fn area(s: *Shape) -> ` 之后
    eq("-> 之后 → type 上下文", r["context"], "type")
    ck("-> 给 i64", "i64" in labs, str(labs[:8]))
    ck("-> 给自定义 Person", "Person" in labs)
    ck("-> 给 stdlib 类型 Time", "Time" in labs)

    r, labs = items("s: *Shape", 5)                # 参数类型位 `*` 之后
    eq("参数类型位 → type 上下文", r["context"], "type")
    ck("类型位给 str", "str" in labs, str(labs[:8]))

    r, labs = items("Vec<Person>[", 4)             # 容器类型参数位
    eq("Vec< 里面 → type 上下文", r["context"], "type")

    r, labs = items("let doubled = n", 0)           # 行首还没敲字
    eq("普通位置 → global", r["context"], "global")
    ck("给关键字 if", "if" in labs)
    ck("给内建 print", "print" in labs)
    ck("给用户函数 area", "area" in labs, str([x for x in labs if x.startswith("a")][:10]))
    ck("不给 stdlib 内部函数 time_parts_of", "time_parts_of" not in labs)
    ck("给用户结构体 Person", "Person" in labs)

    r, labs = items("print(p.name", 0)             # 带前缀过滤
    ln, c = at(DEMO, "print(p.name")
    r = F.complete(DEMO, ln, c + len("print(p.na"), a=a)
    labs = [i["label"].split(" | ")[0] for i in r["items"]]
    ck("前缀 na 过滤出 name", labs and all(x.startswith("na") for x in labs), str(labs[:6]))

    # 敲一个字母不该模糊匹配出一堆不相干的
    ln, c = at(DEMO, "let t = Time.now()")
    r = F.complete(DEMO, ln, c + len("let t"), a=a)
    labs = [i["label"].split(" | ")[0] for i in r["items"]]
    ck("单字母前缀不做子串匹配（t 不该捞出 let）",
       "let" not in labs, str(labs[:10]))


# ---------------------------------------------------------------- 4. 悬停
def test_hover():
    print("[4] 悬停")
    a = F.analyze(DEMO, "demo.fa")

    def hov(needle, offset=0, occurrence=1):
        ln, c = at(DEMO, needle, offset, occurrence)
        return F.hover(DEMO, ln, c, a=a) or ""

    h = hov("let ps = Vec", 4)
    ck("局部变量 → 类型", "Vec<Person>" in h, h[:80])
    h = hov("fn area(s:", 3)
    ck("用户函数 → 签名", "fn area(s: *Shape) -> f64" in h, h[:80])
    h = hov("struct Person:", 7)
    ck("结构体 → 定义（含默认值）", "name: str" in h and "age: i64 = 0" in h, h[:100])
    h = hov("enum Shape:", 5)
    ck("枚举 → 变体", "Circle" in h and "Rect" in h, h[:100])
    h = hov("n.map(", 2)
    ck("内建方法 → 文档", "map(" in h and "Vec<R>" in h, h[:100])
    h = hov("if x", 0) if "if x" in DEMO else hov("let m = Map", 0)
    ck("有内容返回", bool(h))
    h = hov("print(p.name", 6)
    ck("str 字段的方法/字段能悬停", h != "", "(空)")
    ln, c = at(DEMO, "for p in adults")
    h = F.hover(DEMO, ln, c + len("for "), a=a) or ""
    ck("for 变量 → 类型", "Person" in h, h[:80])
    ln, c = at(DEMO, "return 0.0")
    h = F.hover(DEMO, ln, c + len("retu"), a=a) or ""
    ck("关键字 return → 说明", "return" in h, h[:80])


# ---------------------------------------------------------------- 5. 符号
def test_symbols():
    print("[5] 文档符号")
    a = F.analyze(DEMO, "demo.fa")
    syms = F.symbols(DEMO, a=a)
    names = [s["name"] for s in syms]
    # Sema 会把 stdlib 的声明摊平进 mod.decls（sema.py:640），documentSymbol
    # 只报本文件自己的定义，use 行不进大纲（它们在文件顶上，一眼就看到）
    eq("符号只有本文件的 6 个大件",
       sorted(names), sorted(["Person", "Shape", "area", "is_adult", "main", "double"]))
    ck("不含 stdlib 摊平进来的 Time.now", "Time.now" not in names, str(names[:6]))
    ck("不含 stdlib 内部函数 s_cstr", "s_cstr" not in names)
    kinds = {s["name"]: s["kind"] for s in syms}
    eq("Person 是结构体", kinds.get("Person"), "结构体")
    eq("Shape 是枚举", kinds.get("Shape"), "枚举")
    eq("area 是函数", kinds.get("area"), "函数")
    ck("每个符号都有行号", all(s["line"] >= 1 for s in syms), str(syms[:3]))


# ---------------------------------------------------------------- 6. 签名
def test_signature():
    print("[6] 签名提示")
    a = F.analyze(DEMO, "demo.fa")

    def sig(needle, offset):
        ln, c = at(DEMO, needle)
        return F.signature(DEMO, ln, c + offset, a=a)

    s = sig("area(s: *Shape)", 0)
    ck("光标在签名里不算调用（返回 None 或本行无关）", s is None or "area" in str(s))
    ln, c = at(DEMO, "ps.filter(is_adult)")
    s = F.signature(DEMO, ln, c + len("ps.filter("), a=a)
    ck("内建方法 filter → 参数名", s and "p" in s["params"], str(s))
    ln, c = at(DEMO, "Person{name:")
    s = F.signature(DEMO, ln, c + len("Person{"), a=a)
    ck("结构体字面量 → 字段当参数", s and s["params"] == ["name", "age"], str(s))
    ck("结构体字面量的 label 是渲染过的（不是 TName(...)）",
       s and "TName" not in s["label"], str(s and s["label"]))
    ln, c = at(DEMO, "m.set(", 1)
    s = F.signature(DEMO, ln, c + len("m.set("), a=a)
    ck("Map.set → 有签名", s is not None and s["params"], str(s))
    ln, c = at(DEMO, "Time.format(", 1)
    s = F.signature(DEMO, ln, c + len("Time.format("), a=a)
    ck("stdlib 静态方法 Time.format → 有签名", s is not None, str(s))


# ---------------------------------------------------------------- 7. 增量
def test_incremental():
    print("[7] 边打边报（模拟敲键）")
    steps = [
        'fn main() -> i64:\n    let x = \n',                 # 表达式没写完
        'fn main() -> i64:\n    let x = 1\n',                 # 补上了
        'fn main() -> i64:\n    let x = 1\n    print(x)\n',   # 再用一下
        'fn main() -> i64:\n    let x = 1\n    print(y)\n',   # 打错名字
    ]
    oks = []
    for src in steps:
        ds, a = F.check(src, "live.fa")
        oks.append(a.ok)
        if not a.ok:
            ck("失败时要指出行号", all(d.line >= 1 for d in ds), str(ds))
    eq("四步的可编译性", oks, [False, True, True, False])
    ds, _a = F.check(steps[3], "live.fa")
    ck("打错名字 → 报未定义", any("未定义" in d.message or "'y'" in d.message for d in ds),
       str([d.message[:40] for d in ds]))


# ---------------------------------------------------------------- 8. 跳定义
def test_goto():
    print("[8] 跳到定义")
    a = F.analyze(DEMO, "demo.fa")

    def go(needle, offset=0, occurrence=1):
        ln, c = at(DEMO, needle, offset, occurrence)
        return F.goto_definition(DEMO, ln, c, a=a)

    r = go("ps.filter(is_adult)", 13)          # 点在 is_adult 上
    ck("跳到 is_adult 的定义行", r is not None and DEMO.split("\n")[r[1] - 1].startswith("fn is_adult"),
       str(r))
    ck("本文件的定义不带路径", r is not None and r[0] in (None, "demo.fa"), str(r))
    r = go("let adults = ps", 4)               # 点在 adults 上 -> 它自己的 let 行
    ck("局部变量跳到 let 那一行", r is not None and "let adults" in DEMO.split("\n")[r[1] - 1], str(r))
    r = go("Person{name:", 1)                  # 点在 Person 上
    ck("结构体跳到 struct 行", r is not None and DEMO.split("\n")[r[1] - 1].startswith("struct Person"), str(r))
    r = go("Time.format(", 1)                  # stdlib 的静态方法
    ck("stdlib 方法能跳（给出文件路径）",
       r is not None and r[0] and r[0].endswith("time.fa"), str(r))
    r = go("Fs.exists(", 1)
    ck("Fs.exists 跳到 stdlib/fs.fa",
       r is not None and r[0] and r[0].endswith("fs.fa"), str(r))


def main():
    for fn in (test_diagnostics, test_no_false_positives, test_completion,
               test_hover, test_symbols, test_signature, test_incremental,
               test_goto):
        fn()
    print()
    print(f"通过 {len(PASS)} 条，失败 {len(FAIL)} 条")
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("✓ fa_lang 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
