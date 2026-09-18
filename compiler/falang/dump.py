"""FA 自举用的「黄金参考」转储器。

自举的思路是：先用 Python 编译器把一个规范化的转储打到标准输出，
再用 FA 重写同一段逻辑，逐个文件比对输出是否**逐字节一致**。
只要转储足够完整（覆盖行号、列号、字面量原文本、类型后缀），
这个比对就能抓出词法/语法层面任何一处行为差异。

转储格式（每行一个 token，全部是 ASCII，便于两边生成与肉眼比对）

    <行> <列> <KIND> <值编码>[/<数字后缀>]

值编码：

* `NUM`    —— 十进制整数
* `FNUM`   —— `raw:` + 源码原文（避免浮点格式化差异）
* `STR`    —— `s:` + 转义后的内容
* `CHAR`   —— `s:` + 转义后的内容
* `NAME` / `KW` / `OP` / `PUNCT` —— `t:` + 文本
* `NEWLINE` / `EOF` —— `-`
* `INDENT` / `DEDENT` —— 缩进宽度

转义规则（两边必须完全一致）：反斜杠写成 `\\\\`，
其余 0x20~0x7E 原样输出，再其余（含所有非 ASCII 字节）写成 `\\xNN` 大写十六进制。
"""

from __future__ import annotations
from typing import List


def esc(s: str) -> str:
    out = []
    for b in s.encode("utf-8"):
        if b == 0x5C:                       # 反斜杠
            out.append("\\\\")
        elif 0x20 <= b <= 0x7E:             # 可打印 ASCII
            out.append(chr(b))
        else:
            out.append("\\x%02X" % b)
    return "".join(out)


def dump_tokens(toks) -> str:
    """把 token 流转成规范文本"""
    lines: List[str] = []
    for t in toks:
        k, v = t.kind, t.value
        if k == "NUM":
            vs = str(v)
        elif k == "FNUM":
            vs = "raw:" + esc(getattr(t, "raw", "") or repr(v))
        elif k in ("STR", "CHAR"):
            vs = "s:" + esc(v)
        elif k in ("NAME", "KW", "OP", "PUNCT"):
            vs = "t:" + esc(v)
        elif k in ("NEWLINE", "EOF"):
            vs = "-"
        elif k in ("INDENT", "DEDENT"):
            vs = str(v)
        else:
            vs = "?" + repr(v)
        suf = "/" + t.suffix if getattr(t, "suffix", "") else ""
        lines.append(f"{t.line} {t.col} {k} {vs}{suf}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ AST

def _q(s) -> str:
    if s is None:
        return "-"
    if isinstance(s, bool):
        return "true" if s else "false"
    if isinstance(s, int):
        return str(s)
    return esc(str(s))


def dump_ast(mod) -> str:
    """把 AST 转成规范化的 S 表达式（缩进两格一层）"""
    from . import ast as A
    out: List[str] = []

    def put(depth: int, text: str):
        out.append("  " * depth + text)

    def node(depth: int, n):
        if n is None:
            put(depth, "-")
            return
        name = type(n).__name__
        if isinstance(n, A.Module):
            put(depth, "(Module")
            for d in n.decls:
                node(depth + 1, d)
        elif isinstance(n, A.FnDef):
            put(depth, f'(FnDef "{n.name}" extern={_q(n.extern)} varargs={_q(n.varargs)}')
            put(depth + 1, "(params")
            for p in n.params:
                node(depth + 2, p)
            put(depth + 1, ")")
            put(depth + 1, "(ret")
            node(depth + 2, n.ret)
            put(depth + 1, ")")
            put(depth + 1, "(body")
            node(depth + 2, n.body)
            put(depth + 1, ")")
        elif isinstance(n, A.Param):
            put(depth, f'(Param "{n.name}"')
            node(depth + 1, n.ty)
        elif isinstance(n, A.StructDef):
            put(depth, f'(StructDef "{n.name}"')
            for fname, fty in n.fields:
                put(depth + 1, f'(Field "{fname}"')
                node(depth + 2, fty)
                put(depth + 1, ")")
        elif isinstance(n, A.EnumDef):
            put(depth, f'(EnumDef "{n.name}"')
            for vname, fields, val in n.variants:
                put(depth + 1, f'(Variant "{vname}"')
                if fields:
                    for fname, fty in fields:
                        put(depth + 2, f'(VField "{fname}"')
                        node(depth + 3, fty)
                        put(depth + 2, ")")
                node(depth + 2, val)
                put(depth + 1, ")")
        elif isinstance(n, A.ImplDef):
            put(depth, f'(ImplDef "{n.type_name}"')
            for m in n.methods:
                node(depth + 1, m)
        elif isinstance(n, A.Use):
            put(depth, f'(Use "{n.kind}" "{n.path}" "{n.alias}" "{n.lib}"')
            for d in n.body:
                node(depth + 1, d)
        elif isinstance(n, A.Const):
            put(depth, f'(Const "{n.name}"')
            node(depth + 1, n.ty)
            node(depth + 1, n.init)
        # ---- 类型
        elif isinstance(n, A.TName):
            put(depth, f'(TName "{n.name}"')
            for a in n.args:
                node(depth + 1, a)
            put(depth + 1, ")")
        elif isinstance(n, A.TPtr):
            put(depth, "(TPtr")
            node(depth + 1, n.inner)
            put(depth + 1, ")")
        elif isinstance(n, A.TArr):
            put(depth, "(TArr")
            node(depth + 1, n.elem)
            node(depth + 1, n.size)
            put(depth + 1, ")")
        elif isinstance(n, A.TFn):
            put(depth, "(TFn")
            for p in n.params:
                node(depth + 1, p)
            node(depth + 1, n.ret)
            put(depth + 1, ")")
        elif isinstance(n, A.TOptional):
            put(depth, "(TOptional")
            node(depth + 1, n.inner)
            put(depth + 1, ")")
        # ---- 语句
        elif isinstance(n, A.Block):
            put(depth, f"(Block flat={_q(n.flat)}")
            for s in n.stmts:
                node(depth + 1, s)
            put(depth + 1, ")")
        elif isinstance(n, A.Let):
            put(depth, f'(Let "{n.name}"')
            node(depth + 1, n.ty)
            node(depth + 1, n.init)
            put(depth + 1, ")")
        elif isinstance(n, A.Assign):
            put(depth, f'(Assign "{n.op}"')
            node(depth + 1, n.target)
            node(depth + 1, n.value)
            put(depth + 1, ")")
        elif isinstance(n, A.Return):
            put(depth, "(Return")
            node(depth + 1, n.value)
            put(depth + 1, ")")
        elif isinstance(n, A.If):
            put(depth, "(If")
            node(depth + 1, n.cond)
            node(depth + 1, n.body)
            put(depth + 1, "(elifs")
            for c, b in n.elifs:
                node(depth + 2, c)
                node(depth + 2, b)
            put(depth + 1, ")")
            node(depth + 1, n.orelse)
            put(depth + 1, ")")
        elif isinstance(n, A.While):
            put(depth, "(While")
            node(depth + 1, n.cond)
            node(depth + 1, n.body)
            put(depth + 1, ")")
        elif isinstance(n, A.For):
            put(depth, f'(For "{n.var}"')
            node(depth + 1, n.iter)
            node(depth + 1, n.body)
            put(depth + 1, ")")
        elif isinstance(n, A.ForC):
            put(depth, "(ForC")
            node(depth + 1, n.init)
            node(depth + 1, n.cond)
            node(depth + 1, n.step)
            node(depth + 1, n.body)
            put(depth + 1, ")")
        elif isinstance(n, A.Loop):
            put(depth, "(Loop")
            node(depth + 1, n.body)
            put(depth + 1, ")")
        elif isinstance(n, (A.Break, A.Continue)):
            put(depth, f"({name})")
        elif isinstance(n, A.Defer):
            put(depth, "(Defer")
            node(depth + 1, n.call)
            put(depth + 1, ")")
        elif isinstance(n, A.ExprStmt):
            put(depth, "(ExprStmt")
            node(depth + 1, n.expr)
            put(depth + 1, ")")
        elif isinstance(n, A.Match):
            put(depth, "(Match")
            node(depth + 1, n.subject)
            for arm in n.arms:
                put(depth + 1, "(Arm")
                node(depth + 2, arm.pattern)
                node(depth + 2, arm.body)
                put(depth + 1, ")")
            put(depth + 1, ")")
        elif isinstance(n, A.Asm):
            put(depth, f'(Asm {esc(n.code)}')
            put(depth + 1, ")")
        # ---- 表达式
        elif isinstance(n, A.NumLit):
            put(depth, f'(NumLit {_q(n.value)} "{n.kind}")')
        elif isinstance(n, A.StrLit):
            put(depth, "(StrLit")
            for kind, val in n.parts:
                if kind == "lit":
                    put(depth + 1, f"lit {esc(val)}")
                else:
                    put(depth + 1, "expr")
                    node(depth + 2, val)
            put(depth + 1, ")")
        elif isinstance(n, A.CharLit):
            put(depth, f"(CharLit {esc(n.value)})")
        elif isinstance(n, A.BoolLit):
            put(depth, f"(BoolLit {_q(n.value)})")
        elif isinstance(n, A.NilLit):
            put(depth, "(NilLit)")
        elif isinstance(n, A.NameRef):
            put(depth, f'(NameRef "{n.name}")')
        elif isinstance(n, A.Binary):
            put(depth, f'(Binary "{n.op}"')
            node(depth + 1, n.left)
            node(depth + 1, n.right)
            put(depth + 1, ")")
        elif isinstance(n, A.Unary):
            put(depth, f'(Unary "{n.op}"')
            node(depth + 1, n.operand)
            put(depth + 1, ")")
        elif isinstance(n, A.Cast):
            put(depth, "(Cast")
            node(depth + 1, n.operand)
            node(depth + 1, n.target)
            put(depth + 1, ")")
        elif isinstance(n, A.Call):
            put(depth, "(Call")
            node(depth + 1, n.callee)
            put(depth + 1, "(args")
            for a in n.args:
                node(depth + 2, a)
            put(depth + 1, ")")
            put(depth + 1, ")")
        elif isinstance(n, A.MethodCall):
            put(depth, f'(MethodCall "{n.name}"')
            node(depth + 1, n.obj)
            put(depth + 1, "(args")
            for a in n.args:
                node(depth + 2, a)
            put(depth + 1, ")")
            put(depth + 1, ")")
        elif isinstance(n, A.Index):
            put(depth, "(Index")
            node(depth + 1, n.obj)
            node(depth + 1, n.index)
            put(depth + 1, ")")
        elif isinstance(n, A.Slice):
            # 省掉的那一头打印成 _ ，fa ast 看得到「这是开放端」而不是「这是 0」
            put(depth, "(Slice")
            node(depth + 1, n.obj)
            if n.start is None:
                put(depth + 1, "_")
            else:
                node(depth + 1, n.start)
            if n.end is None:
                put(depth + 1, "_")
            else:
                node(depth + 1, n.end)
            put(depth + 1, ")")
        elif isinstance(n, A.Field):
            put(depth, f'(Field "{n.name}"')
            node(depth + 1, n.obj)
            put(depth + 1, ")")
        elif isinstance(n, A.ArrayLit):
            put(depth, "(ArrayLit")
            for x in n.elems:
                node(depth + 1, x)
            put(depth + 1, ")")
        elif isinstance(n, A.StructLit):
            put(depth, f'(StructLit "{_q(n.name)}"')
            for fname, fv in n.fields:
                put(depth + 1, f'("{_q(fname)}"')
                node(depth + 2, fv)
                put(depth + 1, ")")
            put(depth + 1, ")")
        elif isinstance(n, A.AddrOf):
            put(depth, "(AddrOf")
            node(depth + 1, n.operand)
            put(depth + 1, ")")
        elif isinstance(n, A.Deref):
            put(depth, "(Deref")
            node(depth + 1, n.operand)
            put(depth + 1, ")")
        elif isinstance(n, A.NewExpr):
            put(depth, "(NewExpr")
            node(depth + 1, n.operand)
            put(depth + 1, ")")
        elif isinstance(n, A.Range):
            put(depth, f"(Range {_q(n.inclusive)}")
            node(depth + 1, n.start)
            node(depth + 1, n.end)
            put(depth + 1, ")")
        elif isinstance(n, A.Ctor):
            put(depth, f'(Ctor "{n.name}"')
            for t in n.targs:
                node(depth + 1, t)
            for a in n.args:
                node(depth + 1, a)
            put(depth + 1, ")")
        elif isinstance(n, A.SizeOf):
            put(depth, "(SizeOf")
            node(depth + 1, n.operand)
            put(depth + 1, ")")
        elif isinstance(n, A.RawExpr):
            put(depth, f'(RawExpr "{n.kind}" {esc(n.code)})')
        else:
            put(depth, f"(?{name})")

    node(0, mod)
    return "\n".join(out) + "\n"
