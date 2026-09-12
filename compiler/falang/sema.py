"""FA 语义分析：类型解析、符号表、方法解析、类型检查与标注。"""

from __future__ import annotations
from typing import List, Dict, Optional, Tuple
from .ast import *
from . import types as T
from .types import Type, TYPES, VOID, BOOL, CHAR, STR, ANY, PYOBJ, JOBJ, ptr_to, vec_of, map_of, arr_of, layout_struct, layout_enum

# ----------------------------------------------------------------- 内建
# 由 codegen 特判实现的多态内建（不进入普通符号表解析流程）
BUILTIN_FNS = {
    "print", "println", "write", "len", "push", "pop", "str", "i64", "f64",
    "panic", "assert", "now", "sleep", "sqrt", "sin", "cos", "tan", "pow",
    "abs", "min", "max", "floor", "ceil", "log", "exp", "read_line", "exit",
    "concat", "contains", "keys", "values", "gcd", "random", "env",
    "file_read", "file_write", "cmd",
    "hex", "oct", "bin", "args", "round", "trunc", "log2", "log10", "exp2",
    "hypot", "clamp", "sign", "sum", "sort", "reverse", "join", "chr",
}

# str / Vec / Map / pyobj / jobj 的内建方法
BUILTIN_METHODS = {
    "str": {"len", "at", "slice", "eq", "find", "trim", "split", "contains",
            "to_i64", "to_f64", "to_str", "bytes", "upper", "lower",
            "starts_with", "ends_with", "replace", "chars", "cstr",
            "repeat", "count", "lines", "trim_start", "trim_end"},
    "vec": {"len", "push", "get", "set", "pop", "clear", "contains", "to_str",
            "resize", "sort", "reverse", "join", "sum", "min", "max",
            "index_of"},
    "map": {"len", "get", "set", "has", "del", "clear", "to_str"},
    "arr": {"len"},
    "pyobj": {"to_str", "to_i64", "to_f64", "call", "attr", "to_str_deep"},
    "jobj": {"to_str", "to_i64", "to_f64", "jcall_i64", "jcall_f64",
             "jcall_obj", "jcall_void"},
    "int": {"to_str", "abs", "to_f64"},
    "float": {"to_str", "to_i64", "floor", "ceil", "abs"},
}


class FaTypeError(Exception):
    def __init__(self, msg, line=0, col=0):
        super().__init__(msg)
        self.msg, self.line, self.col = msg, line, col

    def pretty(self, src=""):
        head = f"类型错误 (行 {self.line}, 列 {self.col}): {self.msg}"
        if src:
            lines = src.split("\n")
            if 1 <= self.line <= len(lines):
                head += "\n    " + lines[self.line - 1]
        return head


class VarSym:
    def __init__(self, name, ty, mutable=False, is_param=False, is_global=False):
        self.name, self.ty = name, ty
        self.mutable = mutable
        self.is_param = is_param
        self.is_global = is_global
        self.addr_taken = False
        self.needs_mem = False
        self.slot = -1
        self.assigned = False
        self.captured = False

    def __repr__(self):
        return f"Var({self.name}: {self.ty})"


class FnSym:
    def __init__(self, name, params: List[Type], ret: Type, varargs=False,
                 extern=False, cname=None, decl=None):
        self.name = name
        self.params = params
        self.ret = ret
        self.varargs = varargs
        self.extern = extern
        self.cname = cname or name
        self.decl = decl

    @property
    def symbol(self):
        return self.cname if self.extern else f"fa_{self.name}"

    def __repr__(self):
        return f"Fn({self.name}{self.params}->{self.ret})"


class Scope:
    def __init__(self, parent=None, fn=None):
        self.vars: Dict[str, VarSym] = {}
        self.parent = parent
        self.fn = fn
        self.deferred: List[Expr] = []

    def lookup(self, name):
        s = self
        while s is not None:
            if name in s.vars:
                return s.vars[name]
            s = s.parent
        return None

    def declare(self, name, sym):
        self.vars[name] = sym
        return sym


class Sema:
    def __init__(self, mod: Module, filename="<input>", src=""):
        self.mod = mod
        self.filename = filename
        self.src = src
        self.structs: Dict[str, Type] = {}
        self.enums: Dict[str, Type] = {}
        self.fns: Dict[str, FnSym] = {}
        self.methods: Dict[Tuple[str, str], FnSym] = {}     # (TypeName, method) -> FnSym
        self.globals: Dict[str, VarSym] = {}
        self.uses: List[Use] = []
        self.consts: Dict[str, Expr] = {}
        self.descs: List[Type] = []        # 需要 RC 描述符的结构体
        self.errors: List[str] = []
        self.cur_fn: Optional[FnSym] = None
        self.scope: Optional[Scope] = None
        self.loop_depth = 0
        self.fn_bodies: List[Tuple[FnSym, Block, List[Param]]] = []
        self.py_used = False
        self.java_used = False
        self.lazy_syms: List[tuple] = []      # (符号名, 动态库路径) 运行时 dlsym 绑定
        self.link_libs: List[str] = []        # -lxxx
        self.cxx_shims: List[tuple] = []      # (函数名, 返回类型, 参数类型列表)
        self.c_headers: List[str] = []
        self.runtime_needs = set()

    # ------------------------------------------------------------- 错误
    def error(self, msg, node=None):
        line = col = 0
        if node is not None:
            line = getattr(node, "line", 0)
            col = getattr(node, "col", 0)
        raise FaTypeError(msg, line, col)

    # ------------------------------------------------------------- 类型解析
    def resolve_type(self, node: Type) -> Type:
        if isinstance(node, TPtr):
            return ptr_to(self.resolve_type(node.inner))
        if isinstance(node, TOptional):
            inner = self.resolve_type(node.inner)
            if not inner.is_ptr:
                self.error("可选类型 `T?` 只能用于指针/引用类型", node)
            return inner
        if isinstance(node, TArr):
            elem = self.resolve_type(node.elem)
            if node.size is None:
                self.error("数组必须指定长度：[T; N]（动态数组请用 Vec<T>）", node)
            n = self.const_int(node.size)
            if n is None:
                self.error("数组长度必须是编译期常量", node)
            return arr_of(elem, n)
        if isinstance(node, TFn):
            ps = [self.resolve_type(p) for p in node.params]
            rt = self.resolve_type(node.ret)
            t = Type("fn", "fn", 8, 8)
            t.params, t.ret = ps, rt
            return t
        assert isinstance(node, TName), node
        name = node.name
        if name in TYPES:
            return TYPES[name]
        if name == "Vec":
            if not node.args:
                self.error("Vec 需要元素类型：Vec<T>", node)
            return vec_of(self.resolve_type(node.args[0]))
        if name == "Map":
            if len(node.args) != 2:
                self.error("Map 需要两个类型参数：Map<K, V>", node)
            return map_of(self.resolve_type(node.args[0]),
                          self.resolve_type(node.args[1]))
        if name in self.structs:
            return self.structs[name]
        if name in self.enums:
            return self.enums[name]
        self.error(f"未知类型 '{name}'", node)

    def const_int(self, e: Expr) -> Optional[int]:
        if isinstance(e, NumLit):
            return int(e.value)
        if isinstance(e, Binary):
            a, b = self.const_int(e.left), self.const_int(e.right)
            if a is None or b is None:
                return None
            try:
                return {"+": a + b, "-": a - b, "*": a * b,
                        "//": a // b if b else None,
                        "/": a // b if b else None}[e.op]
            except (KeyError, ZeroDivisionError):
                return None
        if isinstance(e, NameRef) and e.name in self.consts:
            return self.const_int(self.consts[e.name])
        return None

    # ------------------------------------------------------------- 主流程
    def run(self):
        # 第一遍：收集声明
        for d in self.mod.decls:
            if isinstance(d, Use):
                self.uses.append(d)
                if d.kind == "py":
                    self.py_used = True
                if d.kind == "java":
                    self.java_used = True
                for sub in d.body:
                    self.collect_decl(sub)
            else:
                self.collect_decl(d)
        # 结构体先布局
        for d in self.mod.decls:
            if isinstance(d, StructDef):
                self.layout_struct_decl(d)
        # 枚举布局
        for d in self.mod.decls:
            if isinstance(d, EnumDef):
                self.layout_enum_decl(d)
        # 注册函数签名
        for d in self.mod.decls:
            if isinstance(d, FnDef):
                self.register_fn(d)
            elif isinstance(d, ImplDef):
                self.register_impl(d)
        for u in self.uses:
            for sub in u.body:
                if isinstance(sub, FnDef):
                    self.register_fn(sub, extern=True, use=u)
                elif isinstance(sub, StructDef):
                    self.layout_struct_decl(sub)
                elif isinstance(sub, ImplDef):
                    self.register_impl(sub)
        # 第二遍：检查函数体
        for d in self.mod.decls:
            if isinstance(d, FnDef) and d.body is not None:
                self.check_fn(d)
            elif isinstance(d, ImplDef):
                for m in d.methods:
                    if m.body is not None:
                        self.check_method(d.type_name, m)
        return self

    def collect_decl(self, d):
        if isinstance(d, StructDef):
            self.structs.setdefault(d.name, None)     # 占位，稍后布局
        elif isinstance(d, EnumDef):
            self.enums.setdefault(d.name, None)
        elif isinstance(d, Const):
            self.consts[d.name] = d.init

    def layout_struct_decl(self, d: StructDef):
        fields = [(fn, self.resolve_type(ft)) for fn, ft in d.fields]
        t = layout_struct(d.name, fields, getattr(d, "packed", False))
        t.methods = {}
        self.structs[d.name] = t
        d.sym = t
        if t.needs_rc_desc():
            t.desc_id = len(self.descs)
            self.descs.append(t)

    def layout_enum_decl(self, d: EnumDef):
        variants = []
        for i, (vname, vfields, val) in enumerate(d.variants):
            if vfields:
                variants.append((vname, [(fn, self.resolve_type(ft)) for fn, ft in vfields], i))
            else:
                variants.append((vname, [], i))
        t = layout_enum(d.name, variants)
        self.enums[d.name] = t
        d.sym = t
        if t.needs_rc_desc():
            t.desc_id = len(self.descs)
            self.descs.append(t)

    def register_fn(self, d: FnDef, extern=False, use: Use = None):
        params = [self.resolve_type(p.ty) for p in d.params]
        ret = self.resolve_type(d.ret) if d.ret is not None else VOID
        sym = FnSym(d.name, params, ret, varargs=d.varargs,
                    extern=extern or d.extern, cname=d.cname, decl=d)
        if use is not None and use.kind in ("c", "cxx", "lib"):
            sym.extern = True
            if use.kind == "lib":
                sym.lazy = True
                self.lazy_syms.append((d.name, use.path))
            if use.kind == "cxx":
                self.cxx_shims.append(d)
                sym.cname = f"fa_{d.name}"      # shim 里生成的是 extern "C" fa_xxx
            if use.path and use.kind in ("c", "cxx"):
                if use.path not in self.c_headers:
                    self.c_headers.append(use.path)
            if use.lib:
                if use.lib not in self.link_libs:
                    self.link_libs.append(use.lib)
        self.fns[d.name] = sym
        d.sym = sym
        if d.body is not None:
            self.fn_bodies.append((sym, d.body, d.params))
        return sym

    def register_impl(self, d: ImplDef):
        for m in d.methods:
            params = [self.resolve_type(p.ty) for p in m.params
                      if p.name != "self"]
            ret = self.resolve_type(m.ret) if m.ret is not None else VOID
            cname = f"fa_{d.type_name}_{m.name}"
            sym = FnSym(m.name, params, ret, varargs=m.varargs,
                        extern=False, cname=cname, decl=m)
            self.methods[(d.type_name, m.name)] = sym
            m.sym = sym
            m.cname = cname
            if m.body is not None:
                self.fn_bodies.append((sym, m.body, m.params, d.type_name))

    # ------------------------------------------------------------- 函数体检查
    def enter(self, fn=None):
        self.scope = Scope(self.scope, fn)
        return self.scope

    def leave(self):
        self.scope = self.scope.parent

    def check_fn(self, d: FnDef):
        self.check_body(d.sym, d.body, d.params, None)

    def check_method(self, type_name, m: FnDef):
        self.check_body(m.sym, m.body, m.params, type_name)

    def check_body(self, sym: FnSym, body: Block, params: List[Param],
                   self_type: Optional[str]):
        prev = self.cur_fn
        self.cur_fn = sym
        sc = self.enter(sym)
        if self_type is not None:
            st = self.structs.get(self_type) or self.enums.get(self_type)
            v = VarSym("self", st, mutable=True, is_param=True)
            sc.declare("self", v)
        for i, p in enumerate(params):
            if p.name == "self":
                continue                       # self 已由 self_type 声明
            ty = sym.params[i if self_type is None else i - 1]
            sc.declare(p.name, VarSym(p.name, ty, mutable=True, is_param=True))
        self.stmt(body)
        self.leave()
        self.cur_fn = prev

    # ------------------------------------------------------------- 语句
    def stmt(self, s: Stmt):
        if isinstance(s, Block):
            if getattr(s, "flat", False):
                for x in s.stmts:
                    self.stmt(x)
            else:
                sc = self.enter(self.cur_fn)
                for x in s.stmts:
                    self.stmt(x)
                # 作用域退出时释放引用（由 codegen 依据 scope 栈生成）
                self.leave()
        elif isinstance(s, Let):
            ty = self.resolve_type(s.ty) if s.ty is not None else None
            if s.init is not None:
                ity = self.expr(s.init)
                if ty is None:
                    ty = ity
                    if ty.kind == "void":
                        self.error("不能用 void 值初始化变量", s)
                else:
                    self.check_assignable(ty, ity, s, "变量初始化")
            if ty is None:
                self.error(f"变量 '{s.name}' 缺少类型标注且无初值", s)
            sym = VarSym(s.name, ty, mutable=s.mutable)
            self.scope.declare(s.name, sym)
            s.sym = sym
        elif isinstance(s, Assign):
            tt = self.expr(s.target, is_target=True)
            vt = self.expr(s.value)
            self.check_assignable(tt, vt, s, "赋值")
            if isinstance(s.target, NameRef):
                v = self.scope.lookup(s.target.name)
                if v is not None:
                    # 变量默认可重新赋值（降低上手门槛）；`let mut x` 只是显式的风格标注。
                    v.assigned = True
        elif isinstance(s, Return):
            want = self.cur_fn.ret if self.cur_fn else VOID
            if s.value is not None:
                vt = self.expr(s.value)
                self.check_assignable(want, vt, s, "返回值")
            elif want.kind != "void":
                self.error(f"函数声明返回 {want}，但 return 没有值", s)
        elif isinstance(s, If):
            ct = self.expr(s.cond)
            if ct.kind not in ("bool", "int"):
                self.error("if 条件必须是 bool（或整数）", s)
            self.stmt(s.body)
            for c, b in s.elifs:
                self.expr(c)
                self.stmt(b)
            if s.orelse is not None:
                self.stmt(s.orelse)
        elif isinstance(s, While):
            ct = self.expr(s.cond)
            if ct.kind not in ("bool", "int"):
                self.error("while 条件必须是 bool（或整数）", s)
            self.loop_depth += 1
            self.stmt(s.body)
            self.loop_depth -= 1
        elif isinstance(s, Loop):
            self.loop_depth += 1
            self.stmt(s.body)
            self.loop_depth -= 1
        elif isinstance(s, For):
            it = self.expr(s.iter)
            vty = None
            if it.kind == "arr":
                vty = it.elem
            elif it.kind == "vec":
                vty = it.elem
            elif it.kind == "str":
                vty = CHAR
            elif it.kind == "map":
                vty = it.key
            elif it.kind == "range" or isinstance(s.iter, Range):
                vty = TYPES["i64"]
            else:
                self.error(f"无法遍历类型 {it}", s)
            self.loop_depth += 1
            sc = self.enter(self.cur_fn)
            sym = VarSym(s.var, vty)
            sc.declare(s.var, sym)
            s.sym = sym
            self.stmt(s.body)
            self.leave()
            self.loop_depth -= 1
        elif isinstance(s, ForC):
            self.loop_depth += 1
            sc = self.enter(self.cur_fn)
            if s.init is not None:
                self.stmt(s.init)
            if s.cond is not None:
                ct = self.expr(s.cond)
                if ct.kind not in ("int", "bool"):
                    self.error("for 的条件必须是布尔或整数", s)
            if s.step is not None:
                self.stmt(s.step)
            self.stmt(s.body)
            self.leave()
            self.loop_depth -= 1
        elif isinstance(s, Break) or isinstance(s, Continue):
            if self.loop_depth == 0:
                self.error("break/continue 只能出现在循环内", s)
        elif isinstance(s, Defer):
            self.expr(s.call)
        elif isinstance(s, ExprStmt):
            self.expr(s.expr)
        elif isinstance(s, Match):
            self.stmt_match(s)
        elif isinstance(s, Asm):
            pass
        else:
            self.error(f"未处理的语句 {type(s).__name__}", s)

    def stmt_match(self, s: Match):
        st = self.expr(s.subject)
        for arm in s.arms:
            sc = self.enter(self.cur_fn)
            pat = arm.pattern
            if isinstance(pat, str):
                pass
            elif st.kind == "enum" and isinstance(pat, NameRef) \
                    and self.scope.lookup(pat.name) is None:
                # 裸变体名写法： match c: Red: ...
                names = [v[0] for v in st.variants]
                if pat.name not in names:
                    self.error(f"枚举 {st} 没有变体 '{pat.name}'（可用：{names}）", arm)
                pat.is_variant = True
                pat.variant_index = names.index(pat.name)
                pat.ty = st
            else:
                pt = self.expr(pat)
                if st.kind == "enum" and not getattr(pat, "is_variant", False):
                    self.error(f"match 分支需要 {st} 的变体", arm)
                if st.kind == "int" and pt.kind not in ("int", "char"):
                    self.error("match 分支类型与匹配值不兼容", arm)
                elif st == STR and pt.kind != "str":
                    self.error("match 分支类型与匹配值不兼容", arm)
            self.stmt(arm.body)
            self.leave()

    # ------------------------------------------------------------- 表达式
    def expr(self, e: Expr, is_target=False) -> Type:
        if isinstance(e, NumLit):
            k = getattr(e, "kind", "") or ""
            if k.startswith("f"):
                e.ty = TYPES["f64"]      # 标量浮点统一按 f64 运算
            else:
                e.ty = TYPES.get(k) or TYPES["i64"]
            return e.ty
        if isinstance(e, StrLit):
            for kind, part in e.parts:
                if kind == "expr":
                    self.expr(part)
            e.ty = STR
            return e.ty
        if isinstance(e, CharLit):
            e.ty = CHAR
            return e.ty
        if isinstance(e, BoolLit):
            e.ty = BOOL
            return e.ty
        if isinstance(e, NilLit):
            e.ty = ptr_to(TYPES["u8"])
            return e.ty
        if isinstance(e, NameRef):
            return self.expr_name(e)
        if isinstance(e, Binary):
            return self.expr_binary(e)
        if isinstance(e, Unary):
            return self.expr_unary(e, is_target)
        if isinstance(e, Cast):
            ot = self.expr(e.operand)
            tt = self.resolve_type(e.target) if not isinstance(e.target, str) else TYPES[e.target]
            e.ty = tt
            return tt
        if isinstance(e, Call):
            return self.expr_call(e)
        if isinstance(e, MethodCall):
            return self.expr_method(e)
        if isinstance(e, Index):
            ot = self.expr(e.obj)
            it = self.expr(e.index)
            if it.kind != "int":
                self.error("下标必须是整数", e)
            if ot.kind == "arr":
                e.ty = ot.elem
            elif ot.kind == "vec":
                e.ty = ot.elem
            elif ot.kind == "str":
                e.ty = CHAR
            elif ot.kind == "map":
                e.ty = ot.val
            elif ot.kind == "ptr":
                e.ty = ot.inner
            else:
                self.error(f"类型 {ot} 不支持下标访问", e)
            return e.ty
        if isinstance(e, Field):
            ot = self.expr(e.obj)
            if ot.kind == "enum" and getattr(e.obj, "is_type", False):
                # 变体构造器：Color.Red
                for vname, _vfields, vi in ot.variants:
                    if vname == e.name:
                        e.is_variant = True
                        e.variant_index = vi
                        e.ty = ot
                        return ot
                self.error(f"枚举 {ot} 没有变体 '{e.name}'"
                           f"（可用：{[v[0] for v in ot.variants]}）", e)
            if ot.kind in ("struct", "enum"):
                if ot.kind == "enum":
                    self.error(f"枚举值请用 match 或变体构造器访问，不能用 '.{e.name}'", e)
                names = [f[0] for f in ot.fields]
                if e.name not in names:
                    self.error(f"类型 {ot} 没有字段 '{e.name}'（可用：{names}）", e)
                idx = names.index(e.name)
                e.index = idx
                e.ty = ot.fields[idx][1]
                return e.ty
            if ot.kind in ("arr", "vec", "str") and e.name == "len":
                e.ty = TYPES["i64"]
                return e.ty
            if ot.kind == "str" and e.name == "bytes":
                e.ty = TYPES["i64"]
                return e.ty
            self.error(f"类型 {ot} 没有字段 '{e.name}'", e)
        if isinstance(e, ArrayLit):
            if not e.elems:
                self.error("空数组字面量需要类型标注（先用 let a: [i64; 0] = ...）", e)
            et = self.expr(e.elems[0])
            for x in e.elems[1:]:
                t2 = self.expr(x)
                if t2 != et:
                    self.error(f"数组元素类型不一致：{et} 与 {t2}", e)
            e.ty = arr_of(et, len(e.elems))
            return e.ty
        if isinstance(e, StructLit):
            if e.name is None:
                self.error("结构体字面量需要类型名", e)
            st = self.structs.get(e.name)
            if st is None:
                self.error(f"未知结构体 '{e.name}'", e)
            given = {fn: self.expr(fv) for fn, fv in e.fields}
            for fn, fty, off in st.fields:
                if fn not in given:
                    self.error(f"结构体 {e.name} 缺少字段 '{fn}'", e)
                self.check_assignable(fty, given[fn], e, f"字段 '{fn}' 初始化")
            for fn in given:
                if fn not in [f[0] for f in st.fields]:
                    self.error(f"结构体 {e.name} 没有字段 '{fn}'", e)
            e.ty = st
            e.resolved = st
            return st
        if isinstance(e, AddrOf):
            ot = self.expr(e.operand, is_target=True)
            if isinstance(e.operand, NameRef):
                v = self.scope.lookup(e.operand.name)
                if v is not None:
                    v.addr_taken = True
            e.ty = ptr_to(ot)
            return e.ty
        if isinstance(e, Deref):
            ot = self.expr(e.operand)
            if ot.kind != "ptr":
                self.error(f"只能解引用指针，得到 {ot}", e)
            if ot.inner is None:
                self.error("无法解引用 void 指针（请先 as 转换为具体指针类型）", e)
            e.ty = ot.inner
            return e.ty
        if isinstance(e, NewExpr):
            ot = self.expr(e.operand)
            e.ty = ptr_to(ot)
            return e.ty
        if isinstance(e, Range):
            if e.start is not None:
                self.expr(e.start)
            if e.end is not None:
                self.expr(e.end)
            e.ty = TYPES["i64"]
            return e.ty
        if isinstance(e, Ctor):
            if e.name == "Vec":
                et = self.resolve_type(e.targs[0])
                for a in e.args:
                    at = self.expr(a)
                    self.check_assignable(et, at, e, "Vec 元素")
                e.ty = T.vec_of(et)
                return e.ty
            if e.name == "Map":
                kt = self.resolve_type(e.targs[0])
                vt = self.resolve_type(e.targs[1])
                e.ty = T.map_of(kt, vt)
                return e.ty
            self.error(f"未知构造器 '{e.name}'", e)
        if isinstance(e, SizeOf):
            ty = self.resolve_type(e.operand)
            e.ty = TYPES["i64"]
            return e.ty
        if isinstance(e, RawExpr):
            e.ty = ANY
            return e.ty
        self.error(f"未处理的表达式 {type(e).__name__}", e)

    def expr_name(self, e: NameRef) -> Type:
        v = self.scope.lookup(e.name)
        if v is not None:
            e.resolved = v
            e.ty = v.ty
            return v.ty
        f = self.fns.get(e.name)
        if f is not None:
            t = Type("fn", "fn", 8, 8)
            t.params, t.ret = f.params, f.ret
            e.resolved = f
            e.ty = t
            return t
        if e.name in self.structs:
            e.ty = self.structs[e.name]
            e.is_type = True
            return e.ty
        if self.enums.get(e.name):
            e.ty = self.enums[e.name]
            e.is_type = True
            return e.ty
        if e.name in self.consts:
            e.ty = self.expr(self.consts[e.name])
            return e.ty
        if e.name in BUILTIN_FNS:
            t = Type("fn", "fn", 8, 8)
            t.params, t.ret = [], ANY
            e.ty = t
            e.resolved = "builtin"
            return t
        if e.name in ("py", "java", "jvm") and (self.py_used or self.java_used
                                                or e.name == "py"):
            e.resolved = "ns"
            e.ty = Type("ns", e.name, 8, 8)
            return e.ty
        self.error(f"未定义的标识符 '{e.name}'", e)

    def expr_unary(self, e: Unary, is_target=False) -> Type:
        ot = self.expr(e.operand)
        if e.op == "-":
            if ot.kind not in ("int", "float"):
                self.error(f"一元 '-' 需要数值类型，得到 {ot}", e)
            e.ty = ot
            return ot
        if e.op == "+":
            e.ty = ot
            return ot
        if e.op == "!":
            e.ty = BOOL
            return BOOL
        if e.op == "~":
            if ot.kind != "int":
                self.error(f"按位取反 '~' 需要整数，得到 {ot}", e)
            e.ty = ot
            return ot
        if e.op == "*":
            if ot.kind != "ptr":
                self.error(f"只能解引用指针，得到 {ot}", e)
            e.ty = ot.inner
            return e.ty
        if e.op == "&":
            if isinstance(e.operand, NameRef):
                v = self.scope.lookup(e.operand.name)
                if v is not None:
                    v.addr_taken = True
            e.ty = ptr_to(ot)
            return e.ty
        self.error(f"未知一元运算符 '{e.op}'", e)

    def expr_binary(self, e: Binary) -> Type:
        if e.op in ("..", "..="):
            self.expr(e.left)
            self.expr(e.right)
            e.ty = Type("range", "range", 16, 8)
            return e.ty
        lt = self.expr(e.left)
        rt = self.expr(e.right)
        # 逻辑运算
        if e.op in ("and", "or"):
            if lt.kind not in ("bool", "int") or rt.kind not in ("bool", "int"):
                self.error(f"'{e.op}' 需要布尔操作数", e)
            e.ty = BOOL
            return BOOL
        # 比较
        if e.op in ("==", "!=", "<", "<=", ">", ">="):
            if lt.kind == "str" and rt.kind == "str":
                e.ty = BOOL
                return BOOL
            if lt.kind == "ptr" and rt.kind == "ptr":
                e.ty = BOOL
                return BOOL
            if lt.is_num and rt.is_num:
                e.ty = BOOL
                return BOOL
            if lt.kind == "bool" and rt.kind == "bool" and e.op in ("==", "!="):
                e.ty = BOOL
                return BOOL
            if lt == CHAR and rt == CHAR:
                e.ty = BOOL
                return BOOL
            if lt.kind == "ptr" and rt.kind == "int" and rt.name == "i64":
                e.ty = BOOL
                return BOOL
            self.error(f"无法比较 {lt} 与 {rt}", e)
        # 算术
        if lt.kind == "ptr" and rt.kind == "int" and e.op in ("+", "-"):
            e.ty = lt
            return lt
        if lt.is_num and rt.is_num:
            if lt.kind == "float" or rt.kind == "float":
                e.ty = TYPES["f64"] if (lt.name == "f64" or rt.name == "f64") else TYPES["f32"]
            else:
                e.ty = lt if lt.size >= rt.size else rt
            return e.ty
        if lt == STR and rt == STR and e.op == "+":
            e.ty = STR
            return STR
        if lt.kind == "ptr" and rt.kind == "ptr" and e.op == "-":
            e.ty = TYPES["i64"]
            return e.ty
        if lt.kind == "int" and e.op in ("<<", ">>", "&", "|", "^", "%", "//"):
            e.ty = lt
            return lt
        self.error(f"运算符 '{e.op}' 不支持 {lt} 与 {rt}", e)

    def expr_call(self, e: Call) -> Type:
        # 内建多态函数
        if isinstance(e.callee, NameRef) and e.callee.name in BUILTIN_FNS:
            ats = [self.expr(a) for a in e.args]
            name = e.callee.name
            e.resolved = "builtin"
            if name in ("sqrt", "sin", "cos", "tan", "pow", "log", "exp",
                        "floor", "ceil", "to_f64", "now"):
                e.ty = TYPES["f64"]
            elif name == "abs":                       # 跟随实参类型
                e.ty = TYPES["f64"] if (ats and ats[0].is_float) else TYPES["i64"]
            elif name in ("min", "max"):
                e.ty = ats[0] if ats else TYPES["i64"]
            elif name in ("len", "i64", "to_i64", "gcd", "random", "at", "bytes"):
                e.ty = TYPES["i64"]
            elif name in ("str", "to_str", "read_line", "concat", "env",
                          "file_read", "cmd", "hex", "oct", "bin", "chr"):
                e.ty = STR
            elif name == "args":
                e.ty = vec_of(STR)
            elif name in ("round", "trunc", "log2", "log10", "exp2", "hypot", "clamp"):
                e.ty = TYPES["f64"] if (not ats or ats[0].is_float) else TYPES["i64"]
            elif name in ("assert", "print", "println", "write", "exit",
                          "sleep", "panic"):
                e.ty = VOID
            elif name == "contains":
                e.ty = TYPES["bool"]
            elif name in ("sum", "min", "max", "sort"):
                e.ty = ANY          # 由实参类型决定，见 codegen
            elif name in ("keys", "values"):
                e.ty = vec_of(STR)
            else:
                e.ty = ANY
            return e.ty
        # 枚举/结构体构造器式的调用（如 EnumName.Variant 已由 Field 处理）
        ct = self.expr(e.callee)
        if ct.kind == "fn":
            params, ret = ct.params, ct.ret
            resolved = e.callee.resolved if isinstance(e.callee, NameRef) else None
            varargs = bool(getattr(resolved, "varargs", False))
            if varargs:
                if len(e.args) < len(params):
                    self.error(f"函数至少需要 {len(params)} 个实参，实际传入 {len(e.args)} 个", e)
                for i, a in enumerate(e.args[:len(params)]):
                    at = self.expr(a)
                    self.check_assignable(params[i], at, e, f"第 {i+1} 个实参")
                for a in e.args[len(params):]:
                    self.expr(a)
            elif len(params) == len(e.args):
                for i, a in enumerate(e.args):
                    at = self.expr(a)
                    self.check_assignable(params[i], at, e, f"第 {i+1} 个实参")
            else:
                self.error(f"函数需要 {len(params)} 个实参，实际传入 {len(e.args)} 个", e)
            e.ty = ret
            return ret
        if isinstance(e.callee, Field):
            # 枚举变体构造 Enum::Variant(...)
            ot = self.expr(e.callee.obj)
            self.error(f"不支持的调用形式", e)
        self.error(f"不能调用非函数类型 {ct}", e)

    def expr_method(self, e: MethodCall) -> Type:
        ot = self.expr(e.obj)
        for a in e.args:
            self.expr(a)
        # 命名空间：py.* / java.*
        if ot.kind == "ns":
            e.resolved = "builtin-method"
            nsrets = {
                ("py", "import"): PYOBJ, ("py", "eval"): PYOBJ, ("py", "exec"): TYPES["i64"],
                ("py", "call"): PYOBJ, ("py", "from_i64"): PYOBJ, ("py", "from_f64"): PYOBJ,
                ("py", "from_str"): PYOBJ, ("py", "from_list"): PYOBJ, ("py", "init"): TYPES["i64"],
                ("java", "init"): TYPES["i64"], ("java", "class"): JOBJ,
                ("java", "call_i64"): TYPES["i64"], ("java", "call_f64"): TYPES["f64"],
                ("java", "call_obj"): JOBJ, ("java", "call_void"): VOID,
                ("java", "new"): JOBJ, ("java", "str"): JOBJ,
            }
            key = (ot.name if ot.name != "jvm" else "java", e.name)
            if key in nsrets:
                e.ty = nsrets[key]
            else:
                e.ty = ANY
            if ot.name == "py":
                self.py_used = True
            else:
                self.java_used = True
            return e.ty
        key = (ot.name if ot.kind in ("struct", "enum") else ot.kind, e.name)
        fs = self.methods.get(key)
        if fs is not None:
            e.resolved = fs
            e.ty = fs.ret
            return fs.ret
        if ot.kind in ("struct", "enum") and (ot.name, e.name) in self.methods:
            fs = self.methods[(ot.name, e.name)]
            e.resolved = fs
            e.ty = fs.ret
            return fs.ret
        # 内建方法
        kind = ot.kind if ot.kind in BUILTIN_METHODS else None
        if kind and e.name in BUILTIN_METHODS[kind]:
            e.resolved = "builtin-method"
            if e.name == "to_f64":
                e.ty = TYPES["f64"]
            elif e.name in ("len", "at", "to_i64", "find", "bytes"):
                e.ty = TYPES["i64"]
            elif e.name in ("to_str", "slice", "trim", "upper", "lower",
                            "replace", "cstr", "to_str_deep",
                            "repeat", "trim_start", "trim_end", "join"):
                e.ty = STR
            elif e.name in ("split", "chars", "keys", "values", "lines"):
                e.ty = vec_of(STR if e.name != "chars" else CHAR)
            elif e.name == "count":
                e.ty = TYPES["i64"]
            elif e.name == "index_of":
                e.ty = TYPES["i64"]
            elif e.name in ("sum", "min", "max"):
                if ot.kind != "vec":
                    e.ty = ANY
                elif ot.elem is not None and ot.elem.is_float:
                    e.ty = ot.elem
                else:
                    # 求和/极值一律按 64 位整数返回：bool、i8、u16 这些窄类型
                    # 累加起来很容易溢出元素本身的宽度（运行时也是按 i64 累加的）
                    e.ty = TYPES["i64"]
            elif e.name in ("sort", "reverse", "resize"):
                e.ty = VOID
            elif e.name in ("contains", "has", "starts_with", "ends_with", "eq"):
                e.ty = BOOL
            elif e.name == "get":
                e.ty = ot.val if ot.kind == "map" else ot.elem
            elif e.name == "pop":
                e.ty = ot.elem
            elif e.name in ("push", "set", "clear", "del"):
                e.ty = VOID
            elif e.name == "jcall_i64":
                e.ty = TYPES["i64"]
            elif e.name == "jcall_f64":
                e.ty = TYPES["f64"]
            elif e.name == "jcall_obj":
                e.ty = JOBJ
            elif e.name == "jcall_void":
                e.ty = VOID
            elif e.name in ("call", "attr"):
                e.ty = PYOBJ if ot.kind == "pyobj" else JOBJ
            elif e.name in ("floor", "ceil", "abs"):
                e.ty = ot
            else:
                e.ty = ANY
            return e.ty
        # UFCS：自由函数以对象作为首个参数
        fs = self.fns.get(e.name)
        if fs is not None and fs.params and fs.params[0] == ot:
            e.resolved = fs
            e.ty = fs.ret
            return fs.ret
        self.error(f"类型 {ot} 没有方法 '{e.name}'", e)

    # ------------------------------------------------------------- 赋值检查
    def check_assignable(self, want: Type, got: Type, node, ctx=""):
        if want == got:
            return
        if want.kind == "any" or got.kind == "any":
            return
        if want == VOID or got.kind == "void":
            self.error(f"{ctx}: 不能把 void 赋给 {want}", node)
        if want.is_num and got.is_num:
            return                      # 数值间允许隐式提升
        if want.kind == "ptr" and got.kind == "ptr":
            return                      # 指针间互转（等价于 C 的 void* 风格）
        if want.kind == "ptr" and got == STR:
            return                      # C 互操作：str 自动取 C 字符串指针
        if want == STR and got.kind == "ptr":
            return                      # C 互操作：char* 自动包成 str
        if want.kind == "ptr" and got.kind == "int":
            return
        if want == BOOL and got.kind == "int":
            return
        if want.kind == "int" and got == BOOL:
            return
        if want == CHAR and got.kind == "int":
            return
        if want.kind == "int" and got == CHAR:
            return
        self.error(f"{ctx}: 类型不匹配，期望 {want}，实际 {got}", node)
