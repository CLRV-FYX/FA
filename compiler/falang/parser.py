"""FA 语法分析器 —— 缩进为主、大括号可选、Pratt 表达式解析。"""

from __future__ import annotations
from typing import List, Optional
from .lexer import tokenize, split_interpolation, FaSyntaxError
from .ast import *

KW = "KW"
NAME = "NAME"
OP = "OP"
P = "PUNCT"

# 二元运算符优先级（低 -> 高）
BIN_PREC = {
    "..": 0, "..=": 0,                      # range 最松：0..n+1 == 0..(n+1)
    "or": 1, "and": 2,
    "|": 3, "^": 4, "&": 5,
    "==": 6, "!=": 6,
    "<": 7, "<=": 7, ">": 7, ">=": 7,
    "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10,
    "**": 12,
}
RIGHT_ASSOC = {"**"}
CMP_OPS = {"==", "!=", "<", "<=", ">", ">="}

TYPE_KEYWORDS = {"i8", "i16", "i32", "i64", "isize", "u8", "u16", "u32",
                 "u64", "usize", "f32", "f64", "bool", "char", "str",
                 "void", "any"}


class Parser:
    def __init__(self, src: str, filename: str = "<input>"):
        self.src = src
        self.filename = filename
        self.toks = tokenize(src)
        self.pos = 0
        self.in_braces = 0          # 处于 {} 内部时，换行无意义，需要 ';'

    # ---------------------------------------------------------- 工具
    def peek(self, k: int = 0):
        i = min(self.pos + k, len(self.toks) - 1)
        return self.toks[i]

    def cur(self):
        return self.toks[self.pos]

    def next(self):
        t = self.toks[self.pos]
        if t.kind != "EOF":
            self.pos += 1
        return t

    def at(self, kind, value=None) -> bool:
        t = self.cur()
        return t.kind == kind and (value is None or t.value == value)

    def at_kw(self, *kws) -> bool:
        t = self.cur()
        return t.kind == KW and t.value in kws

    def at_op(self, *ops) -> bool:
        t = self.cur()
        return t.kind == OP and t.value in ops

    def accept(self, kind, value=None):
        if self.at(kind, value):
            return self.next()
        return None

    def expect(self, kind, value=None):
        t = self.cur()
        if t.kind == kind and (value is None or t.value == value):
            return self.next()
        want = value if value is not None else kind
        got = t.value if t.kind in (OP, P, KW) else t.kind
        raise FaSyntaxError(f"期望 '{want}'，实际得到 '{got}'", t.line, t.col)

    def expect_kw(self, kw):
        return self.expect(KW, kw)

    def err(self, msg, tok=None):
        t = tok or self.cur()
        raise FaSyntaxError(msg, t.line, t.col)

    def skip_terms(self):
        """跳过语句分隔符：换行 / 分号"""
        while self.cur().kind in ("NEWLINE", "INDENT", "DEDENT") or self.at(P, ";"):
            if self.at(P, ";"):
                self.next()
            else:
                if self.cur().kind == "DEDENT":
                    # 只在缩进模式里 DEDENT 才有意义，交给上层
                    if self.in_braces:
                        self.next()
                        continue
                    break
                if self.cur().kind == "INDENT":
                    if self.in_braces:
                        self.next()
                        continue
                    break
                self.next()

    def skip_newlines(self):
        while self.cur().kind == "NEWLINE":
            self.next()

    # ---------------------------------------------------------- 顶层
    def parse_module(self) -> Module:
        decls: List[Decl] = []
        self.skip_terms()
        while not self.at("EOF"):
            d = self.parse_decl()
            if d is not None:
                decls.append(d)
            self.skip_terms()
            if self.at("EOF"):
                break
        return Module(decls)

    def parse_decl(self) -> Optional[Decl]:
        t = self.cur()
        if t.kind in ("NEWLINE", "INDENT", "DEDENT"):
            self.next()
            return None
        if self.at(P, ";"):
            self.next()
            return None
        if self.at_kw("use"):
            return self.parse_use()
        if self.at_kw("pub"):
            self.next()
            d = self.parse_decl()
            if isinstance(d, (FnDef, StructDef, Const)):
                d.pub = True
            return d
        if self.at_kw("fn"):
            return self.parse_fn()
        if self.at_kw("struct"):
            return self.parse_struct()
        if self.at_kw("enum"):
            return self.parse_enum()
        if self.at_kw("impl"):
            return self.parse_impl()
        if self.at_kw("const"):
            return self.parse_const()
        if self.at_kw("extern"):
            return self.parse_extern_block()
        if self.at_kw("unsafe"):
            self.next()
            return self.parse_decl()
        self.err(f"顶层只允许 use/fn/struct/enum/impl/const/extern 声明，得到 '{t.value}'")

    # ---------------------------------------------------------- use
    def parse_use(self) -> Use:
        self.expect_kw("use")
        t = self.cur()
        # use py
        if self.at_kw("py"):
            self.next()
            return Use(kind="py", path="python3", alias="py")
        # use java "...."  /  use java
        if self.at_kw("java"):
            self.next()
            path = ""
            if self.at("STR"):
                path = self.next().value
            alias = ""
            if self.at_kw("as"):
                self.next()
                alias = self.expect(NAME).value
            return Use(kind="java", path=path, alias=alias)
        # use c / use cxx "..."（注意 'c' 不是保留字，按 NAME 识别）
        if self.at_kw("cxx") or self.at_kw("libc") or \
           (self.cur().kind in (KW, NAME) and self.cur().value in ("c", "cxx", "libc")):
            kind = self.next().value
            kind = {"c": "c", "cxx": "cxx", "libc": "c"}[kind]
            path = ""
            if self.at("STR"):
                path = self.next().value
            lib = ""
            if self.at_kw("lib"):
                self.next()
                lib = self.expect("STR").value
            elif self.at(NAME, "lib"):
                self.next()
                lib = self.expect("STR").value
            body = []
            if self.at(P, ":"):
                self.next()
                body = self.parse_use_body()
            return Use(kind=kind, path=path, lib=lib, body=body)
        # use lib "....so"
        if self.at_kw("lib") or self.at(NAME, "lib"):
            self.next()
            path = self.expect("STR").value
            body = []
            if self.at(P, ":"):
                self.next()
                body = self.parse_use_body()
            return Use(kind="lib", path=path, body=body)
        # use raw c++ {{{ ... }}}
        if self.at_kw("cxx") and self.at_op("{"):
            pass
        # use "./mod.fa" as m    /   use std.io
        if self.at("STR"):
            path = self.next().value
            alias = ""
            if self.at_kw("as"):
                self.next()
                alias = self.expect(NAME).value
            return Use(kind="file", path=path, alias=alias)
        if self.at(NAME):
            path = self.next().value
            while self.at(P, "."):
                self.next()
                path += "." + self.expect(NAME).value
            alias = ""
            if self.at_kw("as"):
                self.next()
                alias = self.expect(NAME).value
            return Use(kind="std", path=path, alias=alias)
        self.err("use 后面需要：模块名 / \"头文件\" / py / java / lib")

    def parse_use_body(self) -> List[Decl]:
        """`use ...:` 之后的缩进块或 {} 块，里面放 extern fn / struct 声明"""
        body: List[Decl] = []
        if self.at(P, "{"):
            self.next()
            self.in_braces += 1
            while not self.at(P, "}"):
                self.skip_terms()
                if self.at(P, "}"):
                    break
                d = self.parse_decl()
                if d:
                    body.append(d)
                self.skip_terms()
            self.expect(P, "}")
            self.in_braces -= 1
            return body
        if self.at("NEWLINE"):
            self.next()
        if not self.at("INDENT"):
            self.err("use 声明块需要缩进或使用 {}")
        self.expect("INDENT")
        while not self.at("DEDENT") and not self.at("EOF"):
            self.skip_terms()
            if self.at("DEDENT") or self.at("EOF"):
                break
            d = self.parse_decl()
            if d:
                body.append(d)
            self.skip_terms()
        self.accept("DEDENT")
        return body

    # ---------------------------------------------------------- fn
    def parse_params(self) -> List[Param]:
        self.expect(P, "(")
        params: List[Param] = []
        varargs = False
        while not self.at(P, ")"):
            if self.at_op("..."):
                self.next()
                varargs = True
                break
            pname = self.expect(NAME).value
            if pname == "self" and not self.at(P, ":"):
                # Rust 风格的裸 self：类型由 impl 块决定，这里先留空
                params.append(Param("self", None))
            else:
                self.expect(P, ":")
                pty = self.parse_type()
                params.append(Param(pname, pty))
            if self.at(P, ","):
                self.next()
                continue
            break
        self.expect(P, ")")
        self._last_varargs = varargs
        return params

    def parse_fn(self) -> FnDef:
        self.expect_kw("fn")
        line, col = self.cur().line, self.cur().col
        name = self.expect(NAME).value
        self._last_varargs = False
        params = self.parse_params()
        ret = None
        if self.at_op("->"):
            self.next()
            ret = self.parse_type()
        elif self.at(P, ":") and not self._block_follows():
            # `fn f():` 里的冒号属于块；这里处理 `fn f() -> i64:`
            pass
        fn = FnDef(name=name, params=params, ret=ret, body=None,
                   varargs=self._last_varargs)
        fn.line, fn.col = line, col
        if self.at(P, ";"):            # 纯声明：fn f(...);
            self.next()
            return fn
        if self.at(P, ":") or self.at(P, "{"):   # 缩进体 或 C 风格 { } 体
            body = self.parse_block(scope="fn")
            fn.body = body
            return fn
        return fn                      # 无冒号无分号 => 纯声明（use/extern 块内）

    def _block_follows(self) -> bool:
        """判断 `:` 后面是块还是类型注解（如结构体字段）"""
        if self.at(P, ":"):
            nxt = self.peek(1)
            return nxt.kind == "NEWLINE" or nxt.value == "{"
        return False

    def parse_extern_block(self) -> Decl:
        self.expect_kw("extern")
        abi = "C"
        if self.at("STR"):
            abi = self.next().value
        # extern { ... }  或  extern: ...  或 extern fn f(...)
        if self.at_kw("fn"):
            f = self.parse_fn()
            f.extern = True
            f.abi = abi
            return f
        decls = []
        body = self.parse_block(scope="extern") if not self.at(P, "{") else None
        if body is None:
            self.expect(P, "{")
            self.in_braces += 1
            while not self.at(P, "}"):
                self.skip_terms()
                if self.at(P, "}"):
                    break
                if self.at_kw("fn"):
                    f = self.parse_fn()
                    f.extern = True
                    f.abi = abi
                    decls.append(f)
                elif self.at_kw("struct"):
                    decls.append(self.parse_struct())
                else:
                    self.err("extern 块内只允许 fn / struct 声明")
                self.skip_terms()
            self.expect(P, "}")
            self.in_braces -= 1
            return Use(kind="extern-block", body=decls)
        # 缩进式 extern 块：把内部声明打平返回（用 Use 承载，sema 会展开）
        for d in body.stmts:
            if isinstance(d, FnDef):
                d.extern = True
                d.abi = abi
                decls.append(d)
        return Use(kind="extern-block", body=decls)

    # ---------------------------------------------------------- struct/enum/impl
    def parse_struct(self) -> StructDef:
        self.expect_kw("struct")
        name = self.expect(NAME).value
        fields = []
        if self.at(P, "{") and not self._block_follows():
            self.next()
            self.in_braces += 1
            while not self.at(P, "}"):
                self.skip_terms()
                if self.at(P, "}"):
                    break
                fn = self.expect(NAME).value
                self.expect(P, ":")
                fields.append((fn, self.parse_type()))
                self.skip_terms()
            self.expect(P, "}")
            self.in_braces -= 1
        else:
            if self.at(P, ":"):
                self.next()
            if self.at("NEWLINE"):
                self.next()
            self.expect("INDENT")
            while not self.at("DEDENT"):
                self.skip_terms()
                if self.at("DEDENT"):
                    break
                fn = self.expect(NAME).value
                self.expect(P, ":")
                fields.append((fn, self.parse_type()))
                self.skip_terms()
            self.accept("DEDENT")
        return StructDef(name=name, fields=fields)

    def parse_enum(self) -> EnumDef:
        self.expect_kw("enum")
        name = self.expect(NAME).value
        variants = []
        if self.at(P, "{"):
            self.next()
            self.in_braces += 1
            while not self.at(P, "}"):
                self.skip_terms()
                if self.at(P, "}"):
                    break
                variants.append(self.parse_variant())
                self.skip_terms()
            self.expect(P, "}")
            self.in_braces -= 1
        else:
            if self.at(P, ":"):
                self.next()
            if self.at("NEWLINE"):
                self.next()
            self.expect("INDENT")
            while not self.at("DEDENT"):
                self.skip_terms()
                if self.at("DEDENT"):
                    break
                variants.append(self.parse_variant())
                self.skip_terms()
            self.accept("DEDENT")
        return EnumDef(name=name, variants=variants)

    def parse_variant(self):
        vname = self.expect(NAME).value
        fields = None
        value = None
        if self.at(P, "("):
            self.next()
            fields = []
            idx = 0
            while not self.at(P, ")"):
                fn = f"_{idx}"
                if self.at(NAME) and self.peek(1).value == ":":
                    fn = self.expect(NAME).value
                    self.expect(P, ":")
                fields.append((fn, self.parse_type()))
                idx += 1
                if self.at(P, ","):
                    self.next()
            self.expect(P, ")")
        elif self.at(P, "="):
            self.next()
            value = self.parse_expr()
        return (vname, fields, value)

    def parse_impl(self) -> ImplDef:
        self.expect_kw("impl")
        tname = self.expect(NAME).value
        methods = []
        if self.at(P, "{") and not self._block_follows():
            self.next()
            self.in_braces += 1
            while not self.at(P, "}"):
                self.skip_terms()
                if self.at(P, "}"):
                    break
                methods.append(self.parse_fn())
                self.skip_terms()
            self.expect(P, "}")
            self.in_braces -= 1
        else:
            if self.at(P, ":"):
                self.next()
            if self.at("NEWLINE"):
                self.next()
            self.expect("INDENT")
            while not self.at("DEDENT"):
                self.skip_terms()
                if self.at("DEDENT"):
                    break
                methods.append(self.parse_fn())
                self.skip_terms()
            self.accept("DEDENT")
        return ImplDef(type_name=tname, methods=methods)

    def parse_const(self) -> Const:
        self.expect_kw("const")
        name = self.expect(NAME).value
        ty = None
        if self.at(P, ":"):
            self.next()
            ty = self.parse_type()
        self.expect_op("=")
        init = self.parse_expr()
        return Const(name=name, ty=ty, init=init)

    # ---------------------------------------------------------- 块与语句
    def parse_block(self, scope: str = "block") -> Block:
        if self.at(P, "{"):
            self.next()
            self.in_braces += 1
            stmts = []
            while not self.at(P, "}"):
                self.skip_terms()
                if self.at(P, "}"):
                    break
                s = self.parse_stmt()
                if s is not None:
                    stmts.append(s)
                self.skip_terms()
            self.expect(P, "}")
            self.in_braces -= 1
            return Block(stmts)
        if self.at(P, ":"):
            self.next()
        if self.at("NEWLINE"):
            self.next()
        if not self.at("INDENT"):
            raise FaSyntaxError("这里需要一个缩进代码块（或用 {} 包裹）",
                                self.cur().line, self.cur().col)
        self.expect("INDENT")
        stmts = []
        while not self.at("DEDENT") and not self.at("EOF"):
            self.skip_terms()
            if self.at("DEDENT") or self.at("EOF"):
                break
            s = self.parse_stmt()
            if s is not None:
                stmts.append(s)
            self.skip_terms()
        self.accept("DEDENT")
        return Block(stmts)

    def parse_stmt(self) -> Optional[Stmt]:
        t = self.cur()
        if t.kind in ("NEWLINE", "INDENT") or self.at(P, ";"):
            self.next()
            return None
        if t.kind == "DEDENT":
            return None
        if self.at_kw("let"):
            return self.parse_let()
        if self.at_kw("return"):
            self.next()
            val = None
            if not (self.at("NEWLINE") or self.at(P, ";") or self.at("DEDENT")):
                val = self.parse_expr()
            return Return(value=val)
        if self.at_kw("if"):
            return self.parse_if()
        if self.at_kw("while"):
            self.next()
            cond = self.parse_expr()
            body = self.parse_block()
            return While(cond=cond, body=body)
        if self.at_kw("for"):
            return self.parse_for()
        if self.at_kw("loop"):
            self.next()
            return Loop(body=self.parse_block())
        if self.at_kw("break"):
            self.next()
            return Break()
        if self.at_kw("continue"):
            self.next()
            return Continue()
        if self.at_kw("defer"):
            self.next()
            return Defer(call=self.parse_expr())
        if self.at_kw("match"):
            return self.parse_match()
        if self.at_kw("unsafe"):
            self.next()
            if self.at(P, ":") or self.at(P, "{"):
                return self.parse_block()
            return self.parse_stmt()
        if self.at_kw("asm"):
            self.next()
            code = self.expect("STR").value
            return Asm(code=code)
        if self.at_kw("raise"):
            self.next()
            return ExprStmt(expr=Call(callee=NameRef("__fa_panic"),
                                      args=[self.parse_expr()]))
        # 赋值 / 复合赋值 / 表达式语句
        e = self.parse_expr()
        if self.at_op("=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
                      ">>=", "<<="):
            op = self.next().value
            val = self.parse_expr()
            if op == "=":
                return Assign(target=e, value=val, op="=")
            base = op[:-1]
            return Assign(target=e, value=Binary(base, e, val), op=op)
        return ExprStmt(expr=e)

    def parse_let(self):
        self.expect_kw("let")
        mutable = bool(self.accept_kw("mut"))
        # 元组解构： let (a, b) = (x, y)   —— 直接展开成多条 let
        if self.at(P, "("):
            self.next()
            names = []
            while not self.at(P, ")"):
                names.append(self.expect(NAME).value)
                if self.at(P, ","):
                    self.next()
                else:
                    break
            self.expect(P, ")")
            self.expect_op("=")
            self.expect(P, "(")
            vals = []
            while not self.at(P, ")"):
                vals.append(self.parse_expr())
                if self.at(P, ","):
                    self.next()
                else:
                    break
            self.expect(P, ")")
            if len(names) != len(vals):
                raise FaSyntaxError(
                    f"元组解构左右个数不一致：{len(names)} 个变量 vs {len(vals)} 个值",
                    self.cur().line, self.cur().col)
            # 先求值再赋值，保证 let (a, b) = (b, a) 的交换语义正确
            tmps = []
            for i, v in enumerate(vals):
                tmps.append(Let(name=f"__fa_t{i}", ty=None, init=v, mutable=True))
            outs = [Let(name=n, ty=None,
                        init=NameRef(f"__fa_t{i}"), mutable=mutable)
                    for i, n in enumerate(names)]
            return Block(stmts=tmps + outs, flat=True)
        name = self.expect(NAME).value
        ty = None
        if self.at(P, ":"):
            self.next()
            ty = self.parse_type()
        init = None
        if self.at_op("="):
            self.next()
            init = self.parse_expr()
        return Let(name=name, ty=ty, init=init, mutable=mutable)

    def accept_kw(self, kw) -> bool:
        if self.at_kw(kw):
            self.next()
            return True
        return False

    def parse_if(self) -> If:
        self.expect_kw("if")
        cond = self.parse_expr()
        body = self.parse_block()
        elifs = []
        orelse = None
        while True:
            if self.at_kw("elif"):
                self.next()
                c = self.parse_expr()
                b = self.parse_block()
                elifs.append((c, b))
            elif self.at_kw("else"):
                self.next()
                if self.at_kw("if"):
                    self.next()
                    c = self.parse_expr()
                    b = self.parse_block()
                    elifs.append((c, b))
                else:
                    orelse = self.parse_block()
            else:
                break
        return If(cond=cond, body=body, elifs=elifs, orelse=orelse)

    def parse_for(self):
        self.expect_kw("for")
        # C/Java 风格： for (init; cond; step) { ... }
        if self.at(P, "("):
            self.next()
            self.in_braces += 1
            init = None
            if not self.at(P, ";"):
                init = self.parse_stmt()
            self.expect(P, ";")
            cond = None
            if not self.at(P, ";"):
                cond = self.parse_expr()
            self.expect(P, ";")
            step = None
            if not self.at(P, ")"):
                step = self.parse_stmt()
            self.expect(P, ")")
            self.in_braces -= 1
            body = self.parse_block()
            return ForC(init=init, cond=cond, step=step, body=body)
        var = self.expect(NAME).value
        self.expect_kw("in")
        it = self.parse_expr()
        body = self.parse_block()
        return For(var=var, iter=it, body=body)

    def parse_match(self) -> Match:
        self.expect_kw("match")
        subj = self.parse_expr()
        arms = []
        if self.at(P, "{"):
            self.next()
            self.in_braces += 1
            while not self.at(P, "}"):
                self.skip_terms()
                if self.at(P, "}"):
                    break
                arms.append(self.parse_arm())
                self.skip_terms()
            self.expect(P, "}")
            self.in_braces -= 1
        else:
            if self.at(P, ":"):
                self.next()
            if self.at("NEWLINE"):
                self.next()
            self.expect("INDENT")
            while not self.at("DEDENT"):
                self.skip_terms()
                if self.at("DEDENT"):
                    break
                arms.append(self.parse_arm())
                self.skip_terms()
            self.accept("DEDENT")
        return Match(subject=subj, arms=arms)

    def parse_arm(self) -> MatchArm:
        line, col = self.cur().line, self.cur().col
        if self.at(NAME, "_"):
            self.next()
            pat: Any = "_"
        elif self.at_kw("else"):
            self.next()
            pat = "_"
        else:
            pat = self.parse_expr()
        self.expect(P, ":") if self.at(P, ":") else None
        if self.at_op("=>"):
            self.next()
        body = self.parse_block()
        return MatchArm(pattern=pat, body=body)

    # ---------------------------------------------------------- 类型
    def parse_type(self) -> Type:
        line, col = self.cur().line, self.cur().col
        if self.at_op("*"):
            self.next()
            return TPtr(self.parse_type())
        if self.at(P, "["):
            self.next()
            elem = self.parse_type()
            size = None
            if self.at(P, ";"):
                self.next()
                size = self.parse_expr()
            self.expect(P, "]")
            return TArr(elem, size)
        if self.at_kw("fn"):
            self.next()
            self.expect(P, "(")
            ps = []
            while not self.at(P, ")"):
                ps.append(self.parse_type())
                if self.at(P, ","):
                    self.next()
            self.expect(P, ")")
            ret = TName("void")
            if self.at_op("->"):
                self.next()
                ret = self.parse_type()
            return TFn(ps, ret)
        if self.cur().kind == KW and self.cur().value in TYPE_KEYWORDS:
            name = self.next().value
        else:
            name = self.expect(NAME).value
        if True:
            args = []
            if self.at_op("<") or (self.at(P, "[") is False and self.at_op("<")):
                pass
            if self.at_op("<"):
                self.next()
                while True:
                    args.append(self.parse_type())
                    if self.at(P, ","):
                        self.next(); continue
                    break
                self.expect_op(">")
            t = TName(name, args)
            if self.at_op("?"):
                self.next()
                t = TOptional(t)
            return t
        t = TName(name)
        if self.at_op("?"):
            self.next()
            t = TOptional(t)
        return t

    def expect_op(self, op):
        if self.at_op(op):
            return self.next()
        self.err(f"期望运算符 '{op}'")

    # ---------------------------------------------------------- 表达式
    def parse_expr(self, min_prec: int = 0) -> Expr:
        lhs = self.parse_unary()
        while True:
            t = self.cur()
            if t.kind == OP and t.value in BIN_PREC:
                prec = BIN_PREC[t.value]
                if prec < min_prec:
                    break
                op = self.next().value
                nxt_min = prec if op in RIGHT_ASSOC else prec + 1
                rhs = self.parse_expr(nxt_min)
                lhs = Binary(op, lhs, rhs)
                continue
            if t.kind == KW and t.value in ("and", "or"):
                prec = BIN_PREC[t.value]
                if prec < min_prec:
                    break
                op = self.next().value
                rhs = self.parse_expr(prec + 1)
                lhs = Binary(op, lhs, rhs)
                continue
            break
        return lhs

    def parse_unary(self) -> Expr:
        t = self.cur()
        if t.kind == OP and t.value in ("-", "+", "!", "~"):
            self.next()
            return Unary(t.value, self.parse_unary())
        if t.kind == KW and t.value == "not":
            self.next()
            return Unary("!", self.parse_unary())
        if t.kind == OP and t.value == "*":
            self.next()
            return Deref(self.parse_unary())
        if t.kind == OP and t.value == "&":
            self.next()
            return AddrOf(self.parse_unary())
        if t.kind == KW and t.value == "new":
            self.next()
            return NewExpr(self.parse_postfix(self.parse_primary()))
        if t.kind == KW and t.value == "sizeof":
            self.next()
            self.expect(P, "(")
            ty = self.parse_type()
            self.expect(P, ")")
            return SizeOf(ty)
        if t.kind == KW and t.value == "unsafe":
            self.next()
            return self.parse_unary()
        e = self.parse_postfix(self.parse_primary())
        # cast: expr as Type
        if self.at_kw("as"):
            self.next()
            target = self.parse_type()
            e = Cast(e, target)
        return e

    def parse_postfix(self, e: Expr) -> Expr:
        while True:
            if self.at(P, "("):
                args = self.parse_args()
                e = Call(callee=e, args=args)
            elif self.at(P, "["):
                self.next()
                idx = self.parse_expr()
                self.expect(P, "]")
                e = Index(obj=e, index=idx)
            elif self.at(P, "."):
                self.next()
                t = self.cur()
                if t.kind == NAME or (t.kind == KW and t.value not in ("as",)):
                    name = self.next().value
                else:
                    self.err("'.' 后面需要字段名或方法名")
                if self.at(P, "("):
                    args = self.parse_args()
                    e = MethodCall(obj=e, name=name, args=args)
                else:
                    e = Field(obj=e, name=name)
            else:
                break
        return e

    def parse_args(self) -> List[Expr]:
        self.expect(P, "(")
        args = []
        while not self.at(P, ")"):
            args.append(self.parse_expr())
            if self.at(P, ","):
                self.next()
                continue
            break
        self.expect(P, ")")
        return args

    def parse_primary(self) -> Expr:
        t = self.cur()
        if t.kind == "NUM":
            self.next()
            return NumLit(t.value, getattr(t, "suffix", "") or "i64")
        if t.kind == "FNUM":
            self.next()
            return NumLit(t.value, getattr(t, "suffix", "") or "f64")
        if t.kind == "STR":
            self.next()
            return self.make_string(t.value, t.line, t.col)
        if t.kind == "CHAR":
            self.next()
            return CharLit(t.value)
        if t.kind == KW and t.value == "true":
            self.next()
            return BoolLit(True)
        if t.kind == KW and t.value == "false":
            self.next()
            return BoolLit(False)
        if t.kind == KW and t.value == "nil":
            self.next()
            return NilLit()
        if t.kind == NAME or (t.kind == KW and
                              (t.value in TYPE_KEYWORDS or t.value in ("py", "java", "jvm"))):
            name = self.next().value
            # 泛型构造器：Vec<T>(...) / Vec<T>[...] / Map<K,V>()
            if self.at_op("<") and name in ("Vec", "Map"):
                save = self.pos
                try:
                    self.next()          # '<'
                    targs = []
                    while True:
                        targs.append(self.parse_type())
                        if self.at(P, ","):
                            self.next(); continue
                        break
                    if not self.at_op(">"):
                        raise FaSyntaxError("泛型参数需要以 '>' 结束")
                    self.next()
                    if self.at(P, "("):
                        args = self.parse_args()
                        return Ctor(name, targs, args)
                    if self.at(P, "["):
                        self.next()
                        items = []
                        while not self.at(P, "]"):
                            items.append(self.parse_expr())
                            if self.at(P, ","):
                                self.next()
                                continue
                            break
                        self.expect(P, "]")
                        return Ctor(name, targs, items)
                    raise FaSyntaxError("不是构造器")
                except FaSyntaxError:
                    self.pos = save
            # 结构体字面量 Point { ... } / Point(...)  / Vec<i64>[1,2]
            if name in TYPE_KEYWORDS and self.at(P, "("):
                # 内置转换，如 i64(x)
                args = self.parse_args()
                return Cast(args[0] if args else NilLit(), TName(name))
            if self.at(P, "{"):
                self.next()
                fields = []
                while not self.at(P, "}"):
                    self.skip_terms()
                    if self.at(P, "}"):
                        break
                    fn = self.expect(NAME).value
                    self.expect(P, ":")
                    fields.append((fn, self.parse_expr()))
                    self.skip_terms()
                    if self.at(P, ","):
                        self.next()
                        continue
                self.expect(P, "}")
                return StructLit(name, fields)
            return NameRef(name)
        if self.at(P, "("):
            self.next()
            e = self.parse_expr()
            self.expect(P, ")")
            return e
        if self.at(P, "["):
            self.next()
            elems = []
            while not self.at(P, "]"):
                elems.append(self.parse_expr())
                if self.at(P, ","):
                    self.next()
                    continue
                break
            self.expect(P, "]")
            return ArrayLit(elems)
        self.err(f"无法解析的表达式起始 token '{t.value}'")

    def make_string(self, raw: str, line: int, col: int) -> Expr:
        parts = split_interpolation(raw)
        if not parts:
            return StrLit([("lit", "")])
        if len(parts) == 1 and parts[0][0] == "lit":
            return StrLit([("lit", parts[0][1])])
        out = []
        for kind, text in parts:
            if kind == "lit":
                out.append(("lit", text))
            else:
                sub = Parser(text + "\n", self.filename)
                sub.pos = 0
                e = sub.parse_expr()
                out.append(("expr", e))
        return StrLit(out)


def parse(src: str, filename: str = "<input>") -> Module:
    p = Parser(src, filename)
    return p.parse_module()
