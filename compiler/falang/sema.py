"""FA 语义分析：类型解析、符号表、方法解析、类型检查与标注。"""

from __future__ import annotations
import os
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
    "free",          # 释放 new / C 那边拿来的指针（引用计数类型不需要它）
}

# str / Vec / Map / pyobj / jobj 的内建方法
BUILTIN_METHODS = {
    "str": {"len", "at", "slice", "eq", "find", "trim", "split", "contains",
            "to_i64", "to_f64", "to_str", "bytes", "upper", "lower",
            "starts_with", "ends_with", "replace", "chars", "cstr",
            "repeat", "count", "lines", "trim_start", "trim_end",
            # UTF-8 码点：char 是一个字节，这组按「字符」而不是按字节算
            "char_len", "char_at", "codepoints", "slice_chars"},
    "vec": {"len", "push", "get", "set", "pop", "clear", "contains", "to_str",
            "resize", "sort", "reverse", "join", "sum", "min", "max",
            "index_of", "copy"},
    # contains 是 has 的别名。以前 Map 只有全局写法 contains(m, k) 编得过
    # （sema 把它 forwarded 到 _builtin_on_container），方法写法 m.contains(k) 被拒，
    # 而 codegen 两条路都没实现 —— 全局写法一路走到后端才报
    # 「未实现的内建方法 .contains（类型 Map<...>）」。Vec 那边两种写法都有，
    # Map 也该一样：都落到 fa_map_has。
    "map": {"len", "get", "set", "has", "contains", "del", "clear", "to_str",
            "keys", "values", "copy"},
    "arr": {"len"},
    "pyobj": {"to_str", "to_i64", "to_f64", "call", "attr", "to_str_deep"},
    "jobj": {"to_str", "to_i64", "to_f64", "jcall_i64", "jcall_f64",
             "jcall_obj", "jcall_void"},
    "int": {"to_str", "abs", "to_f64", "to_i64"},
    # 数学函数既有全局形式 sqrt(x)，也有方法形式 x.sqrt()
    "float": {"to_str", "to_i64", "to_f64", "floor", "ceil", "abs", "round",
              "trunc", "sqrt", "log", "log2", "log10", "exp", "exp2",
              "sin", "cos", "tan"},
}


# 内建方法的参数个数：(最少, 最多)，最多为 None 表示可变参数。
# 以前不查：参数给少了会在代码生成里 `e.args[0]` 越界，抛一条 Python 的
# IndexError traceback 给用户；给多了则被悄悄忽略（`s.len(1)` 照样编译）。
# 两种都改成编译期的中文报错。
METHOD_ARITY = {
    "str": {"len": (0, 0), "at": (1, 1), "slice": (2, 2), "eq": (1, 1),
            "find": (1, 1), "trim": (0, 0), "split": (1, 1), "contains": (1, 1),
            "to_i64": (0, 0), "to_f64": (0, 0), "to_str": (0, 0), "bytes": (0, 0),
            "upper": (0, 0), "lower": (0, 0), "starts_with": (1, 1),
            "ends_with": (1, 1), "replace": (2, 2), "chars": (0, 0),
            "cstr": (0, 0), "repeat": (1, 1), "count": (1, 1), "lines": (0, 0),
            "trim_start": (0, 0), "trim_end": (0, 0), "char_len": (0, 0),
            "char_at": (1, 1), "codepoints": (0, 0), "slice_chars": (2, 2)},
    "vec": {"len": (0, 0), "push": (1, 1), "get": (1, 1), "set": (2, 2),
            "pop": (0, 0), "clear": (0, 0), "contains": (1, 1), "to_str": (0, 0),
            "resize": (1, 2), "sort": (0, 0), "reverse": (0, 0), "join": (1, 1),
            "sum": (0, 0), "min": (0, 0), "max": (0, 0), "index_of": (1, 1),
            "copy": (0, 0)},
    "map": {"len": (0, 0), "get": (1, 1), "set": (2, 2), "has": (1, 1),
            "contains": (1, 1), "del": (1, 1), "clear": (0, 0), "to_str": (0, 0),
            "keys": (0, 0), "values": (0, 0), "copy": (0, 0)},
    "arr": {"len": (0, 0)},
    "pyobj": {"to_str": (0, 0), "to_i64": (0, 0), "to_f64": (0, 0),
              "call": (0, 1), "attr": (1, 1), "to_str_deep": (0, 0)},
    "jobj": {"to_str": (0, 0), "to_i64": (0, 0), "to_f64": (0, 0),
             "jcall_i64": (2, None), "jcall_f64": (2, None),
             "jcall_obj": (2, None), "jcall_void": (2, None)},
    "int": {"to_str": (0, 0), "abs": (0, 0), "to_f64": (0, 0), "to_i64": (0, 0)},
    "float": {"to_str": (0, 0), "to_i64": (0, 0), "to_f64": (0, 0),
              "floor": (0, 0), "ceil": (0, 0), "abs": (0, 0), "round": (0, 0),
              "trunc": (0, 0), "sqrt": (0, 0), "log": (0, 0), "log2": (0, 0),
              "log10": (0, 0), "exp": (0, 0), "exp2": (0, 0), "sin": (0, 0),
              "cos": (0, 0), "tan": (0, 0)},
}

# .to_str() 在这些类型上都有实现（codegen 走 print 用的同一套字符串化路径）
TOSTR_OK = ("str", "int", "float", "vec", "map", "arr", "struct", "enum",
            "bool", "char", "ptr", "pyobj", "jobj")


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
                head += "\n    " + " " * max(0, self.col - 1) + "^"
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
        # impl 方法的符号必须带上类型名（register_impl 给的 cname 是
        # `fa_类型_方法`）。以前一律走 `fa_{name}`，于是两个类型有同名方法就撞车：
        #     impl A: fn show(self) -> i64   # 符号 fa_show
        #     impl B: fn show(self) -> i64   # 符号也是 fa_show
        # 汇编器报 `symbol 'fa_show' is already defined`，指着 .s 文件的行号，
        # 源码里看不出是哪两个方法。而 show / area / to_str / eq 这种名字
        # 在多个类型上各写一份是最平常不过的事。
        if self.extern:
            return self.cname
        if self.cname and self.cname != self.name:
            return self.cname
        return f"fa_{self.name}"

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
        # 名字 -> 声明，用于按需布局（自引用类型要先有壳再填字段）
        self.struct_decls: Dict[str, Any] = {}
        self.enum_decls: Dict[str, Any] = {}
        self._laying_out: set = set()
        self.fns: Dict[str, FnSym] = {}
        self.methods: Dict[Tuple[str, str], FnSym] = {}     # (TypeName, method) -> FnSym
        self.globals: Dict[str, VarSym] = {}
        self.global_decls: List[Any] = []    # 顶层 let，按声明顺序（初值也按此顺序执行）
        # 嵌套函数：每层函数体一个「局部名 -> 提升后的 FnSym」表，内层优先
        self.local_fns: List[Dict[str, FnSym]] = []
        # 检查嵌套函数体时，外层函数的作用域链（只为诊断「想捕获局部变量」）
        self.enclosing_scopes: List[Scope] = []
        self.uses: List[Use] = []
        self.consts: Dict[str, Expr] = {}
        self.descs: List[Type] = []        # 需要 RC 描述符的结构体
        self.errors: List[str] = []
        self.cur_fn: Optional[FnSym] = None
        self.scope: Optional[Scope] = None
        self.loop_depth = 0
        self.in_range = 0        # 正在检查 for 的遍历对象（range 只允许出现在这里）
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
    # 结构体/枚举在容器里存的是**装箱指针**，Vec/Map 存的是把手：拿它们当键，
    # 哈希的是地址而不是内容 —— `m.set(P{x:1}, 5)` 之后 `m.get(P{x:1})` 永远查不到
    # （两次装的是不同的盒子）。而且释放路径会把盒子当引用计数对象处理，实测退出时
    # glibc 报 "free(): invalid pointer"；遍历这种 Map 还会让后端撞上 16 字节的
    # 宽度表。与其留着一串坑，编译期就说清楚。
    MAP_KEY_BAD = ("struct", "enum", "vec", "map", "arr")

    def check_index_arg(self, at, node, ctx):
        """下标 / 长度这类实参必须是整数。

        以前不查，`v.get("0")` 会被 coerce 成 fa_str_to_i64("0") = 0，
        静默取到第 0 个元素；`v.resize("3")` 同理。
        """
        if at is not None and at.kind not in ("int", "bool", "char", "any"):
            self.error(f"{ctx}必须是整数，得到 {at}"
                       f"（字符串要先 .to_i64()）", node)

    def check_container_args(self, ot, e):
        """`v.push(x)` / `m.set(k, v)` 的实参类型。

        以前内建方法只查**参数个数**，不查类型，于是容器这条路比赋值松得多：
        `let n: i64 = "42"` 是编译错误，`Vec<i64>` 上 `v.push("42")` 却编得过 ——
        codegen 的 coerce 会把它当 fa_str_to_i64 解析，`v.push("x")` 解析不出数字
        就静默存了个 0；反过来 `Vec<str>.push(7)` 也是靠 coerce 悄悄 to_str。
        下标赋值 `v[0] = "x"` 倒是查的（报「类型不匹配，期望 i64，实际 str」），
        同一个意思两种规矩。现在统一走 check_assignable：**能赋给 T 变量的，
        才能推进 Vec<T> / 当 Map<K,V> 的键值**，要转就写明白（str(7) / s.to_i64()）。
        """
        name, args = e.name, e.args
        ats = [a.ty for a in args]
        if ot.kind == "vec":
            et = ot.elem
            if name == "push" and len(ats) == 1 and et is not None:
                self.check_assignable(et, ats[0], args[0], "Vec 元素")
            elif name == "set" and len(ats) == 2:
                self.check_index_arg(ats[0], args[0], "下标")
                if et is not None:
                    self.check_assignable(et, ats[1], args[1], "Vec 元素")
            elif name == "get" and len(ats) == 1:
                self.check_index_arg(ats[0], args[0], "下标")
            elif name == "resize":
                if ats:
                    self.check_index_arg(ats[0], args[0], "新长度")
                if len(ats) == 2 and et is not None:
                    self.check_assignable(et, ats[1], args[1], "填充值")
                # 不给填充值时能不能变长，要看运行时的长度，所以这里不拦：
                # 真要变长而元素是装箱类型，codegen 会在循环里 panic 一句人话
            elif name in ("contains", "index_of") and len(ats) == 1 and et is not None:
                self.check_assignable(et, ats[0], args[0], f"{name}() 的实参")
        elif ot.kind == "map":
            kt, vt = ot.key, ot.val
            if name == "set" and len(ats) == 2:
                if kt is not None:
                    self.check_assignable(kt, ats[0], args[0], "Map 的键")
                if vt is not None:
                    self.check_assignable(vt, ats[1], args[1], "Map 的值")
            elif (name in ("get", "has", "contains", "del") and len(ats) == 1
                  and kt is not None):
                self.check_assignable(kt, ats[0], args[0], "Map 的键")

    # sort / min / max / contains / index_of / sum 都要**按内容**比较或累加元素。
    # 运行时只实现了三种元素：整数（按位）、浮点（按值）、str（cmp_strp 逐字节
    # memcmp，contains 里 kind==1 也特判了内容比较）。结构体/枚举/容器在表里存的是
    # **装箱指针**，于是这些方法一个都不成立，而且**没有一个会报错**：
    #   sort()            —— qsort 排的是堆地址，实测 Vec<P> 调完原样返回，看不出异常
    #   contains()/index_of() —— 比地址，实测两张表里内容相同的 Vec<i64> 判 false / -1
    #   sum()             —— 把指针加起来，实测打出 8454172 这种垃圾数
    #   min()/max()       —— 返回一个 i64 地址，当结构体用才在别处炸
    # 静默的错答案比崩溃更难查，所以一律在编译期拦住。
    VEC_CONTENT_OPS = ("sort", "min", "max", "contains", "index_of", "sum")
    VEC_BOXED = ("struct", "enum", "vec", "map", "arr")

    def check_vec_content_op(self, et, op, node):
        """Vec 的元素类型撑不撑得起这个按内容比较的操作。"""
        if et is None or op not in self.VEC_CONTENT_OPS:
            return
        if et.kind in self.VEC_BOXED:
            why = {
                "sort": "qsort 排的是堆地址，排不出任何有意义的顺序",
                "contains": "比的是地址，内容相同的两个值也判不出相等（永远 false）",
                "index_of": "比的是地址，内容相同的两个值也找不到（永远 -1）",
                "min": "返回的是一个地址，当不成这个类型用",
                "max": "返回的是一个地址，当不成这个类型用",
                "sum": "把地址加起来，得到一个垃圾数",
            }[op]
            hint = {
                "sort": "想按结构体的某个字段排，就自己写一趟排序（教程 §9.6 有插入排序的例子）",
                "contains": "想找就自己遍历：`for x in v { if x.字段 == 目标 { ... } }`",
                "index_of": "想找下标就自己遍历，记下 i 再 break",
                "min": "想取最小就自己遍历比较字段",
                "max": "想取最大就自己遍历比较字段",
                "sum": "想累加就自己遍历：`for x in v { total += x.字段 }`",
            }[op]
            self.error(
                f"Vec<{et}> 的元素不能 {op}()：{et} 在表里存的是装箱指针，{why}"
                f"（不报错，但结果是错的）。能这样用的是 str / 整数 / 浮点 / bool / char / 指针。{hint}",
                node)
            return
        if et.kind == "str":
            if op == "sum":
                self.error(
                    "Vec<str> 不能 sum()：字符串不能相加。要拼成一条用 v.join(分隔符)，"
                    "要逐条处理就自己遍历", node)
            elif op in ("min", "max"):
                self.error(
                    f"Vec<str> 没有 {op}()：极值只实现了整数和浮点。"
                    f"先 v.sort() 再取 v[0] / v[v.len() - 1]（sort 对字符串是按 UTF-8 字节序）",
                    node)
        # int / float / bool / char / ptr / any：运行时按位处理，成立

    def check_map_key(self, kt, node):
        if kt is not None and kt.kind in self.MAP_KEY_BAD:
            self.error(
                f"Map 的键不能是 {kt}：键要能按内容哈希和比较，"
                f"只有 str / 整数 / 浮点 / bool / char / 指针可以"
                f"（结构体、枚举在容器里存的是装箱指针，哈希的是地址："
                f"set 完再 get 查不到，程序退出时还会 free 出错）。"
                f"想按结构体查，就用它那个唯一字段当键，或者用 Vec 自己找",
                node)

    def resolve_type(self, node: Type) -> Type:
        if isinstance(node, TPtr):
            # 指针目标只要「已登记」就够了，不必现在完成布局：
            # 自引用结构体（next: *Node）否则会在布局里无限递归。
            if isinstance(node.inner, TName):
                nm = node.inner.name
                tgt = self.structs.get(nm) or self.enums.get(nm)
                if tgt is not None:
                    return ptr_to(tgt)
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
            kt = self.resolve_type(node.args[0])
            self.check_map_key(kt, node)
            return map_of(kt, self.resolve_type(node.args[1]))
        if name in self.structs:
            t = self.structs[name]
            if t.fields is None:                    # 还没布局：现在就补上
                self.layout_struct_decl(self.struct_decls[name])
                t = self.structs[name]
            return t
        if name in self.enums:
            t = self.enums[name]
            if t.variants is None:
                self.layout_enum_decl(self.enum_decls[name])
                t = self.enums[name]
            return t
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
    # ------------------------------------------------------- FA 模块导入
    def expand_file_uses(self, mod: "Module", base_dir: str, seen: set) -> None:
        """把 `use "other.fa"` 展开成那个文件的顶层声明。

        语法分析器一直认这个写法（kind="file"），但语义分析直接把它丢进
        self.uses 就不管了 —— 于是 `use "math.fa"` 之后调用里面的函数只会得到
        「未定义的标识符」，看起来像是自己写错了名字。
        这里就地展开：导入的声明进入当前模块，之后的布局/注册/检查流程一概不变。
        被导入的文件自己也可以再 `use`，用 realpath 去重防循环导入。
        """
        from .parser import parse                     # 延迟导入，避免模块级环
        out: List[Decl] = []
        for d in mod.decls:
            if isinstance(d, Use) and d.kind == "std":
                self.error("FA 没有可导入的标准库模块（内建的 print / len / Vec / Map "
                           "等直接可用，不需要 use）", d)
            if not (isinstance(d, Use) and d.kind == "file"):
                out.append(d)
                continue
            if d.alias:
                self.error('暂不支持 `use "x.fa" as 别名` 的命名空间写法；'
                           '去掉 as，导入的声明会直接进入当前文件', d)
            path = d.path if os.path.isabs(d.path) else os.path.join(base_dir, d.path)
            rp = os.path.realpath(path)
            if not os.path.exists(rp):
                self.error(f'找不到要导入的 FA 模块 "{d.path}"'
                           f'（在 {base_dir or "."} 下找过）', d)
            if rp in seen:
                continue                              # 循环导入：只展开一次
            seen.add(rp)
            with open(rp, encoding="utf-8") as f:
                sub_src = f.read()
            try:
                sub = parse(sub_src, rp)
            except Exception as e:
                self.error(f'导入 "{d.path}" 失败：{e}', d)
            self.expand_file_uses(sub, os.path.dirname(rp), seen)
            out.extend(sub.decls)
        mod.decls = out

    def run(self):
        # 第 0 遍：展开 `use "xxx.fa"` 多文件模块
        self.expand_file_uses(
            self.mod,
            os.path.dirname(os.path.abspath(self.filename))
            if self.filename and self.filename != "<input>" else os.getcwd(),
            {os.path.realpath(self.filename)} if self.filename else set())
        # 顶层重名检查：以前两个同名 fn 会一路走到汇编器，
        # 报一句 `symbol "xx" is already defined`，用户看不出是哪两行。
        seen_names: Dict[str, Any] = {}
        for d in self.mod.decls:
            nm = getattr(d, "name", None)
            if not nm or isinstance(d, Use):
                continue
            if nm in seen_names:
                self.error(f"'{nm}' 重复定义（第一次在第 {seen_names[nm].line} 行）", d)
            seen_names[nm] = d

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
        # 结构体字段默认值（要在函数签名注册之后：默认值里可以调用函数）
        self.check_struct_defaults()
        # 顶层 let：全局变量的类型与初值（此时结构体/枚举已布局、函数签名已注册，
        # 所以初值里可以写 `Vec<str>[]`、结构体字面量、甚至调用函数）
        for d in self.global_decls:
            self.check_global(d)
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
            # 立刻登记一个「空壳」类型对象（fields=None 表示尚未布局），
            # 而不是 None 占位：这样链表/树这类自引用结构体里的 `*Node`
            # 能拿到**同一个**对象，稍后布局结果就地填进去，所有引用自动生效。
            # 以前占位是 None，resolve_type 把 *Node 解析成 ptr_to(None)，
            # 报「类型 *None 没有字段 'val'」，等于递归结构体完全不能用。
            if d.name not in self.structs:
                shell = Type("struct", d.name, 8, 8)
                shell.fields = None
                self.structs[d.name] = shell
                self.struct_decls[d.name] = d
        elif isinstance(d, EnumDef):
            if d.name not in self.enums:
                shell = Type("enum", d.name, 16, 8)
                shell.variants = None
                self.enums[d.name] = shell
                self.enum_decls[d.name] = d
        elif isinstance(d, Const):
            self.check_const_init(d)
            self.consts[d.name] = d.init
        elif isinstance(d, Global):
            self.global_decls.append(d)

    def check_const_init(self, d):
        """const 的初值必须是**编译期算得出来**的。

        const 在实现上是「按使用处替换初值表达式」，不是「求值一次存起来」。
        所以初值里要是有函数调用，每用一次就调一次：

            let n: i64 = 0
            fn tick() -> i64: n += 1; return n
            const C: i64 = tick()
            print(C, C, C)          # 实测 1 2 3，不是 1 1 1

        「常量」打出三个不同的数，这是标准的悄悄给错答案。容器/结构体字面量
        也一样（每用一次建一个新的）。要「算一次的值」就用全局 let。
        """
        def ok(e):
            if isinstance(e, (NumLit, StrLit, CharLit, BoolLit, NameRef)):
                return True
            if isinstance(e, Unary):
                return ok(e.operand)
            if isinstance(e, Binary):
                return ok(e.left) and ok(e.right)
            return False

        if d.init is not None and not ok(d.init):
            self.error(
                f"常量 '{d.name}' 的初值必须是编译期算得出来的"
                f"（字面量、别的常量、它们之间的算术）。const 是**按使用处替换**的，"
                f"初值里调函数的话每用一次就调一次（print(C, C) 会打出两个不同的数）；"
                f"要「只求一次的值」请用全局 let", d)

    def check_struct_defaults(self):
        """检查 `struct P: x: i64 = 3` 这类字段默认值。

        默认值在任何函数之外求值（和顶层 let 的初值一样）：能用字面量、
        容器构造、const、全局和函数调用，没有 self、不能 return。
        """
        for name, d in self.struct_decls.items():
            defaults = getattr(d, "defaults", None) or {}
            if not defaults:
                continue
            st = self.structs.get(name)
            fty = {fn: t for fn, t, off in (st.fields or [])}
            for fn, ex in defaults.items():
                if fn not in fty:
                    self.error(f"结构体 {name} 没有字段 '{fn}'，默认值写错了地方", d)
                    continue
                self.enter(None)
                try:
                    got = self.expr(ex, expect=fty[fn])
                finally:
                    self.leave()
                self.check_assignable(fty[fn], got, ex, f"字段 '{fn}' 的默认值")

    def check_global(self, d):
        """检查一条顶层 `let`，并登记成全局符号。

        初值表达式在「不属于任何函数」的作用域里检查：没有 self、不能 return /
        break，能用的只有字面量、其它全局、const 和函数调用。
        """
        self.enter(None)
        try:
            ity = self.expr(d.init) if d.init is not None else None
        finally:
            self.leave()
        if d.ty is not None:
            want = self.resolve_type(d.ty)
            if ity is not None:
                self.check_assignable(want, ity, d, "全局变量初值")
            d.gty = want
        elif ity is not None:
            d.gty = ity
        else:
            self.error("全局变量必须有初值或类型标注", d)
            return
        if d.gty.kind in ("struct", "enum"):
            # 结构体/枚举是值类型，槽要按 ty.size 开；读写还要走「拷贝 + 逐字段
            # 引用计数」那套（emit_init_agg / emit_assign_agg）。先不做，给一句
            # 明确的错，而不是生成半对的代码。
            self.error(f"全局变量暂不支持结构体/枚举类型 {d.gty}"
                       f"（改用 Vec<{d.gty.name}> / Map / 指针，或把字段拆成单独的全局）", d)
            return
        if d.gty.kind == "fn":
            self.error("全局变量暂不支持函数类型（用 const 或把函数名直接当值传）", d)
            return
        sym = VarSym(d.name, d.gty, mutable=True, is_global=True)
        # 汇编标签加前缀，免得和 C 符号 / 函数名（fa_xxx）撞上
        sym.label = f"__fa_g_{d.name}"
        sym.decl = d
        d.sym = sym
        self.globals[d.name] = sym

    def layout_struct_decl(self, d: StructDef):
        t = self.structs.get(d.name)
        if t is not None and t.fields is not None:
            d.sym = t
            return                                  # 已布局（模块导入会重复调用）
        if t is None:
            t = Type("struct", d.name, 8, 8)
            t.fields = None
            self.structs[d.name] = t
        if d.name in self._laying_out:
            self.error(f"结构体 '{d.name}' 按值包含了自己（大小无限）；"
                       f"递归结构请用指针字段，例如 next: *{d.name}", d)
        self._laying_out.add(d.name)
        try:
            fields = [(fn, self.resolve_type(ft)) for fn, ft in d.fields]
        finally:
            self._laying_out.discard(d.name)
        laid = layout_struct(d.name, fields, getattr(d, "packed", False))
        self._fill_in_place(t, laid)                # 就地填充，保持已有引用有效
        t.methods = {}
        d.sym = t
        if t.needs_rc_desc():
            t.desc_id = len(self.descs)
            self.descs.append(t)

    def layout_enum_decl(self, d: EnumDef):
        t = self.enums.get(d.name)
        if t is not None and t.variants is not None:
            d.sym = t
            return
        if t is None:
            t = Type("enum", d.name, 16, 8)
            t.variants = None
            self.enums[d.name] = t
        if d.name in self._laying_out:
            self.error(f"枚举 '{d.name}' 的载荷按值包含了自己；请用指针 *{d.name}", d)
        self._laying_out.add(d.name)
        try:
            variants = []
            for i, (vname, vfields, val) in enumerate(d.variants):
                if vfields:
                    variants.append((vname, [(fn, self.resolve_type(ft))
                                             for fn, ft in vfields], i))
                else:
                    variants.append((vname, [], i))
        finally:
            self._laying_out.discard(d.name)
        laid = layout_enum(d.name, variants)
        self._fill_in_place(t, laid)
        d.sym = t
        if t.needs_rc_desc():
            t.desc_id = len(self.descs)
            self.descs.append(t)

    @staticmethod
    def _fill_in_place(t: Type, laid: Type) -> None:
        for slot in Type.__slots__:
            if slot in ("name", "_hash"):
                continue
            setattr(t, slot, getattr(laid, slot))
        t._hash = None

    def register_fn(self, d: FnDef, extern=False, use: Use = None):
        params = [self.resolve_type(p.ty) for p in d.params]
        ret = self.resolve_type(d.ret) if d.ret is not None else VOID
        sym = FnSym(d.name, params, ret, varargs=d.varargs,
                    extern=extern or d.extern, cname=d.cname, decl=d)
        if d.cname and d.body is not None:
            # `fn f(a: i64) -> i64 = "g"` 后面还跟了函数体：符号名是给**外部**
            # 已经存在的函数用的别名，自己写了体就没有「另一个符号」可指。
            self.error(
                f"'{d.name}' 既给了 C 符号名 '{d.cname}' 又写了函数体："
                "= 符号名 只能用在 extern / use c / use lib 的**声明**上"
                "（声明 C 那边已经存在的函数，顺便在 FA 侧换个不撞车的名字）", d)
            d.cname = None
        if sym.extern and d.body is not None:
            # `extern "C": fn fa_add(a: i64, b: i64) -> i64: return a + b` 以前一路
            # 走到代码生成，在那儿炸出 AttributeError（extern 的符号没有解析过
            # 形参/返回值的布局，可它有函数体，两边对不上）。extern 只是**声明**，
            # 不能带体 —— 这句话在这儿说清楚，比一个 Python 栈回溯有用。
            self.error(
                f"extern 的 '{d.name}' 不能带函数体：extern 是用来**声明** C/C++ 那边"
                "已经存在的函数的（只写签名，不写体）。"
                "想让 C 反过来调用 FA 函数目前还不支持", d)
            return sym
        if use is not None and use.kind in ("c", "cxx", "lib"):
            sym.extern = True
            # C/C++ 互操作只按**指针**传聚合值：FA 的 struct/enum/数组在 ABI 里就是
            # 指向它的指针，而 C 侧 `void f(Point p)` 是按值收的（16 字节进 rdi:rsi），
            # 两边对不上时读到的是垃圾 —— 以前要跑到运行时才发现，这里提前拦住。
            for pi, pt in enumerate(params):
                if pt.kind in ("vec", "map", "pyobj", "jobj"):
                    pn = d.params[pi].name if pi < len(d.params) else f"#{pi+1}"
                    self.error(
                        f"extern 函数 '{d.name}' 的参数 '{pn}' 是 FA 的 {pt}，"
                        f"C 侧没有这个类型。请传 `{pn}: *T` 加一个长度参数，"
                        f"或者用 str（会自动转成 char*）", d)
                if pt.kind in ("struct", "enum", "arr"):
                    pn = d.params[pi].name if pi < len(d.params) else f"#{pi+1}"
                    self.error(
                        f"extern 函数 '{d.name}' 的参数 '{pn}' 是 {pt}（按值）。"
                        f"C 互操作只支持按指针传结构体/枚举/数组："
                        f"FA 侧写 `{pn}: *{pt.name}`，C 侧写 `{pt.name} *{pn}`", d)
            if ret.kind in ("vec", "map", "pyobj", "jobj"):
                self.error(
                    f"extern 函数 '{d.name}' 返回 FA 的 {ret}，C 侧造不出这个类型。"
                    f"请让 C 返回指针 + 长度，在 FA 侧自己组装容器", d)
            if ret.kind in ("struct", "enum", "arr"):
                self.error(
                    f"extern 函数 '{d.name}' 按值返回 {ret}，C 互操作不支持。"
                    f"请让 C 函数把结果写进指针参数"
                    f"（`void {d.name}({ret.name} *out, ...)`），"
                    f"FA 侧声明成 `-> void` 并传 `&out`", d)
            if use.kind == "lib":
                # `use lib "./x.so":` = 运行时 dlopen。这些符号不参与链接，
                # 由 driver 生成一个 dlopen+dlsym 的转发 shim（见 gen_dl_shim）。
                if d.varargs:
                    self.error(
                        f"extern 函数 '{d.name}' 带可变参数（...），没法用 use lib 转发"
                        "（变参的实参类型只有调用点知道）。改成链接期导入："
                        'use c "头文件.h" lib "./x.so":', d)
                sym.lazy = True
                self.lazy_syms.append((d, use.path))
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
        # `impl Pont:`（把 Point 打错了）以前一声不响：方法注册进了
        # methods[("Pont", ...)]，可谁也不会用类型名 Pont 去调，于是这些方法
        # 就这么消失了，而 `p.norm()` 报的是「类型 Point 没有方法 'norm'」——
        # 看着像方法没写，其实是 impl 的名字错了。
        if d.type_name not in self.structs and d.type_name not in self.enums:
            self.error(f"impl 的类型 '{d.type_name}' 不存在"
                       f"（结构体要用 struct 声明，枚举要用 enum 声明）", d)
            return
        seen_methods = {}
        for m in d.methods:
            # 同一个 impl 里同名方法写两遍：后一个会把前一个从 methods 表里顶掉，
            # 而两个函数体的符号一样，一路走到汇编器才报 `symbol ... is already defined`。
            if m.name in seen_methods:
                self.error(f"类型 {d.type_name} 的方法 '{m.name}' 定义了两次"
                           f"（第一次在第 {seen_methods[m.name]} 行）", m)
            seen_methods[m.name] = getattr(m, "line", 0)
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
        self.local_fns.append({})
        # impl 里也可以写**不带 self** 的方法（`P.create(1, 2)` 这种工厂/构造器）。
        # 以前一律按「有 self」处理：作用域里凭空声明一个 self，参数下标还整体减一 ——
        # `fn mk(name: str, n: i64)` 里 name 拿到的是 sym.params[-1]（也就是 n 的类型），
        # 类型错位，写 `P.mk("甲", 1)` 时字符串被当整数查。
        has_self = any(p.name == "self" for p in params)
        if self_type is not None and has_self:
            st = self.structs.get(self_type) or self.enums.get(self_type)
            v = VarSym("self", st, mutable=True, is_param=True)
            sc.declare("self", v)
        for i, p in enumerate(params):
            if p.name == "self":
                continue                       # self 已由 self_type 声明
            ty = sym.params[i - 1] if has_self else sym.params[i]
            sc.declare(p.name, VarSym(p.name, ty, mutable=True, is_param=True))
        self.stmt(body)
        self.leave()
        self.local_fns.pop()
        self.cur_fn = prev

    def check_nested_fn(self, d: FnDef):
        """函数体里定义的 fn：提升成一个独立函数。

        FA 还没有闭包，所以提升是**唯一**说得通的做法：内层函数变成一个普通的
        顶层函数，符号名是 `fa_外层__内层`（外层再嵌套就继续拼）。代价是它看不见
        外层的局部变量 —— 真要传值就当参数传。检查内层函数体时故意挂一条
        **没有父作用域**的新链，于是引用外层局部变量会落到「未定义」那条路上，
        再由 enclosing_scopes 给出一句能看懂的错，而不是一句莫名其妙的
        「未定义的标识符 'x'」。
        """
        outer = self.cur_fn
        local_name = d.name
        d.local_name = local_name
        if outer is not None:
            d.name = f"{outer.name}__{local_name}"
        if d.name in self.fns:
            first = self.fns[d.name].decl
            where = f"（第一次在第 {getattr(first, 'line', 0)} 行）" if first is not None else ""
            self.error(f"嵌套函数 '{local_name}' 重复定义{where}", d)
            return
        self.register_fn(d)
        if self.local_fns:
            self.local_fns[-1][local_name] = d.sym
        # 用一条独立的作用域链检查内层函数体（看不见外层局部变量）
        self.enclosing_scopes.append(self.scope)
        outer_scope, self.scope = self.scope, None
        try:
            self.check_body(d.sym, d.body, d.params, None)
        finally:
            self.scope = outer_scope
            self.enclosing_scopes.pop()

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
                ity = self.expr(s.init, expect=ty)
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
            vt = self.expr(s.value, expect=tt)
            self.check_assignable(tt, vt, s, "赋值")
            if isinstance(s.target, NameRef):
                v = self.scope.lookup(s.target.name)
                if v is not None:
                    # 变量默认可重新赋值（降低上手门槛）；`let mut x` 只是显式的风格标注。
                    v.assigned = True
                elif s.target.name in self.consts and s.target.name not in self.globals:
                    # 以前这里不拦，一路走到代码生成才报「未定义变量 'K'」——
                    # 常量在符号表里没有存储位置，报错却说得像名字打错了。
                    self.error(f"'{s.target.name}' 是常量（const），不能赋值；"
                               f"要能改的值用全局 let 或者局部 let", s)
        elif isinstance(s, Return):
            want = self.cur_fn.ret if self.cur_fn else VOID
            if s.value is not None:
                vt = self.expr(s.value, expect=want)
                self.check_assignable(want, vt, s, "返回值")
                self.check_no_local_addr(s.value, s)
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
            # range 只有在 for 的遍历位置才有意义，检查期间打开这个开关
            self.in_range += 1
            try:
                it = self.expr(s.iter)
            finally:
                self.in_range -= 1
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
        elif isinstance(s, FnDef):
            self.check_nested_fn(s)
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
            elif st.kind == "enum" and isinstance(pat, (Call, MethodCall)):
                self.bind_variant_pattern(pat, st, arm)
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
    def expr(self, e: Expr, is_target=False, expect: Optional[Type] = None) -> Type:
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
            if ot.kind == "map":
                # m[k] / m[k] = v —— 等价于 m.get(k) / m.set(k, v)。
                # 键类型要跟 Map 声明的键类型对得上，不再一律要求整数。
                self.check_assignable(ot.key, it, e, "Map 键")
                e.ty = ot.val
                return e.ty
            if it.kind != "int":
                # 以前不管什么类型都报「下标必须是整数」，用 m["x"] 的人
                # 完全看不出真正的问题（Map 得用 .get/.set）。
                self.error("下标必须是整数", e)
            if is_target and ot.kind == "str":
                self.error("str 不可变（字面量放在只读数据段），不能按下标赋值；"
                           "需要可变的字符序列请用 Vec<char>", e)
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
            # 指针自动解引用：p.field（p: *S）等价于 (*p).field。
            # 以前直接报「类型 *S 没有字段 'a'」，于是 new S{...} 拿到的指针
            # 根本没法用（只能写 p[0].a），C/C++/Rust 过来的人第一反应都是 p.a。
            if ot.kind == "ptr" and ot.inner is not None \
                    and ot.inner.kind in ("struct", "enum"):
                e.auto_deref = True
                ot = ot.inner
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
                # `let v: Vec<i64> = []`：元素类型从上下文标注里拿。
                # 以前这里一律报错，逼用户改写 `Vec<i64>()`——可标注明明已经写了。
                if expect is not None and expect.kind in ("vec", "arr"):
                    e.ty = expect if expect.kind == "vec" else arr_of(expect.elem, 0)
                    return e.ty
                if expect is not None and expect.kind == "str":
                    e.ty = STR
                    return e.ty
                if expect is not None and expect.kind == "map":
                    # 标注明明写的是 Map，却按「没有上下文」报错，还举的全是 Vec 的例子
                    # ——`let m: Map<str, i64> = []` 得到的提示是「例如 let v: Vec<i64> = []」，
                    # 照着改还是错。空 Map 没有「元素」可列，只能写构造器或带 K/V 的字面量。
                    self.error("空的 [] 建不出 Map（键值对写不出来）：空表写 "
                               "Map<K, V>() 或 Map<K, V>[]，带初值写 "
                               'Map<K, V>["甲": 1]', e)
                self.error("空的 [] 需要上下文类型，例如 let v: Vec<i64> = [] "
                           "或 let a: [i64; 3] = [1, 2, 3]", e)
            if expect is not None and expect.kind == "vec":
                # `let v: Vec<i64> = [1, 2]`、`Task { hist: [1, 2] }`：
                # 标注明明写的是 Vec，就不必再逼用户写一遍 `Vec<i64>[1, 2]`。
                # 元素类型以标注为准，所以 `Vec<f64> = [1, 2]` 这种整数升浮点也允许。
                for x in e.elems:
                    self.check_assignable(expect.elem, self.expr(x, expect=expect.elem),
                                          x, "Vec 元素")
                e.ty = expect
                return e.ty
            if expect is not None and expect.kind == "arr":
                # `let a: [u8; 4] = [1, 2, 3, 4]`、`let g: [f64; 2] = [1, 2]`：
                # 标注明明写了元素类型，字面量里的整数就该按它转（和上面 Vec 那条一样）。
                # 以前只有 vec 有这条路，数组一律先按第一个元素推成 [i64 x N] 再和标注比，
                # 于是 `[u8; 4] = [1,2,3,4]` 报「期望 [u8 x 4]，实际 [i64 x 4]」——
                # 而 `[u8; 4]` 正是写字节缓冲最自然的写法。个数仍然要对上
                # （推出来的是 [u8 x 2] 就还是和 [u8 x 4] 不匹配）。
                for x in e.elems:
                    self.check_assignable(expect.elem, self.expr(x, expect=expect.elem),
                                          x, "数组元素")
                e.ty = arr_of(expect.elem, len(e.elems))
                return e.ty
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
            fty_by_name = {fn: t for fn, t, off in st.fields}
            # `Task { name: "a", name: "b" }` 以前悄悄取后一个（given 是字典推导，
            # 重复键后者覆盖前者）—— 打错字段名或者复制粘贴忘删的时候，
            # 程序照跑，值却是另一个，谁也看不出来。直接报错。
            seen_fields = set()
            for fn, fv in e.fields:
                if fn in seen_fields:
                    self.error(f"结构体 {e.name} 的字段 '{fn}' 写了两次", e)
                seen_fields.add(fn)
            given = {fn: self.expr(fv, expect=fty_by_name.get(fn))
                     for fn, fv in e.fields}
            defaults = getattr(self.struct_decls.get(e.name), "defaults", None) or {}
            for fn, fty, off in st.fields:
                if fn not in given:
                    if fn in defaults:
                        continue             # 有默认值：字面量里可以省略
                    self.error(f"结构体 {e.name} 缺少字段 '{fn}'（它也没有默认值）", e)
                    continue
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
                    # 把元素类型当上下文传下去：`Vec<Vec<i64>>[[1], [2, 3]]` 里
                    # 那个 [1] 才知道自己该是 Vec<i64> 而不是数组 [i64 x 1]
                    at = self.expr(a, expect=et)
                    self.check_assignable(et, at, e, "Vec 元素")
                e.ty = T.vec_of(et)
                return e.ty
            if e.name == "Map":
                kt = self.resolve_type(e.targs[0])
                self.check_map_key(kt, e)       # Map<K,V>() / Map<K,V>[k: v] 这条路
                vt = self.resolve_type(e.targs[1])
                if len(e.args) % 2:
                    self.error("Map 字面量要成对写：Map<K, V>[键: 值, ...]", e)
                for i in range(0, len(e.args), 2):
                    self.check_assignable(kt, self.expr(e.args[i], expect=kt),
                                          e.args[i], "Map 的键")
                    self.check_assignable(vt, self.expr(e.args[i + 1], expect=vt),
                                          e.args[i + 1], "Map 的值")
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
        if isinstance(e, If):
            return self.expr_if(e)
        if isinstance(e, Match):
            return self.expr_match(e)
        self.error(f"未处理的表达式 {type(e).__name__}", e)

    # ------------------------------------------------ if / match 作为表达式
    #: 这些内建函数不会返回，以它们结尾的分支算「发散」，不产出值
    DIVERGING_FNS = ("panic", "exit")

    def _block_tail(self, b, node):
        """分支的值 = 块里最后一条**表达式语句**；发散分支返回 None。"""
        stmts = [x for x in (b.stmts if b is not None else []) if x is not None]
        if stmts:
            last = stmts[-1]
            diverges = isinstance(last, (Return, Break, Continue))
            if not diverges and isinstance(last, ExprStmt) \
                    and isinstance(last.expr, Call) \
                    and isinstance(last.expr.callee, NameRef) \
                    and last.expr.callee.name in self.DIVERGING_FNS:
                diverges = True
            if diverges:
                # `let y = if x > 0 { return 99 } else { x * 2 }`：
                # 这个分支根本走不到后面，不该要求它有值、也不参与类型统一
                b.diverges = True
                return None
        last = stmts[-1] if stmts else None
        if isinstance(last, ExprStmt):
            return last.expr
        if isinstance(last, (If, Match)):
            # 嵌套写法：`if a { if b { 1 } else { 2 } } else { 3 }`
            # 块里最后一条本身就是 if / match，那它就是分支的值
            if isinstance(last, If):
                self.expr_if(last)
            else:
                self.expr_match(last)
            return last                     # 类型已经挂在节点上（.ty）
        self.error("if / match 用作表达式时，每个分支的最后一条语句必须是表达式"
                   "（它就是该分支的值）；不需要值的话别写在 = 右边", node)

    #: 分支类型统一时按「数值」对待的 kind（与 check_assignable 的隐式提升一致）
    NUMERIC_KINDS = ("int", "float", "bool", "char")

    def _unify_tails(self, tails, node, what) -> Type:
        live = [t for t in tails if t is not None]
        if not live:
            self.error(f"作为表达式的 {what}，每个分支都发散（return / panic），"
                       f"没有值可用；直接把它当语句写就行", node)
        # `nil` 的默认类型是 *u8，但它其实能当任何指针用：先把它排除在统一之外
        tys = [t.ty for t in live if not isinstance(t, NilLit) and t.ty is not None]
        if not tys:
            return ptr_to(TYPES["u8"])
        ty = tys[0]
        for t in tys[1:]:
            if t == ty:
                continue
            if ty.kind in self.NUMERIC_KINDS and t.kind in self.NUMERIC_KINDS:
                # 数值分支：有一边是浮点就整体按 f64，否则按 i64
                ty = TYPES["f64"] if "float" in (ty.kind, t.kind) else TYPES["i64"]
                continue
            if ty.kind == "ptr" and t.kind == "ptr":
                continue                   # 指针之间允许互转（同 check_assignable）
            self.error(f"作为表达式的 {what}，各分支类型不一致：{ty} 与 {t}", node)
        for t in live:                     # nil 分支跟上统一后的指针类型
            if isinstance(t, NilLit):
                if ty.kind != "ptr":
                    self.error(f"作为表达式的 {what}，nil 分支不能和 {ty} 分支混用", node)
                t.ty = ty
        if ty.kind == "void":
            self.error(f"作为表达式的 {what}，分支不能是 void", node)
        return ty

    def expr_if(self, e: If) -> Type:
        self.stmt(e)                       # 条件、分支、作用域全部复用语句那套检查
        if e.orelse is None:
            self.error("作为表达式的 if 必须有 else 分支（否则可能没有值）", e)
        tails = [self._block_tail(e.body, e)]
        tails += [self._block_tail(b, e) for _, b in e.elifs]
        tails.append(self._block_tail(e.orelse, e))
        e.ty = self._unify_tails(tails, e, "if")
        return e.ty

    def _enum_covered(self, e: Match) -> bool:
        """枚举的每个变体都有分支覆盖（此时不需要 `_` 兜底）。"""
        st = e.subject.ty
        if st is None or st.kind != "enum" or not st.variants:
            return False
        got = {getattr(a.pattern, "variant_index", None) for a in e.arms}
        return all(i in got for i in range(len(st.variants)))

    def expr_match(self, e: Match) -> Type:
        self.stmt(e)
        has_wild = any(isinstance(a.pattern, str) and a.pattern == "_" for a in e.arms)
        if not has_wild and not self._enum_covered(e):
            self.error("作为表达式的 match 必须有 `_` 兜底分支，或覆盖枚举的全部变体"
                       "（否则可能没有值）", e)
        e.ty = self._unify_tails([self._block_tail(a.body, e) for a in e.arms],
                                 e, "match")
        return e.ty

    def expr_name(self, e: NameRef) -> Type:
        v = self.scope.lookup(e.name)
        if v is not None:
            e.resolved = v
            e.ty = v.ty
            return v.ty
        g = self.globals.get(e.name)
        if g is not None:
            # 局部作用域里没有同名变量时才落到全局（和 C/Python 的作用域规则一致）
            e.resolved = g
            e.ty = g.ty
            return g.ty
        for tbl in reversed(self.local_fns):
            f = tbl.get(e.name)
            if f is not None:
                t = Type("fn", "fn", 8, 8)
                t.params, t.ret = f.params, f.ret
                e.resolved = f
                e.ty = t
                return t
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
        for sc in self.enclosing_scopes:
            if sc is not None and sc.lookup(e.name) is not None:
                self.error(f"嵌套函数不能捕获外层局部变量 '{e.name}'"
                           f"（FA 还没有闭包：把它当参数传进去，或把函数提到顶层）", e)
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
            lt = self.expr(e.left)
            rt = self.expr(e.right)
            if lt.kind == "range" or rt.kind == "range":
                # `0..10..2` 被解析成 (0..10)..2。FA 的 range 只有「起..止」，
                # 没有步进 —— 以前这里照样给个 range 类型，一路走到 asmgen 的
                # 二元运算符表才 KeyError: '..'，把 Python 异常糊在用户脸上。
                self.error("范围运算符不能连用：FA 的 range 只有 `起..止` / `起..=止`，"
                           "没有步进写法。要跳着走请用 while，"
                           "或 `for i in 0..n { let j = i * 2 }`", e)
            for side, t in (("左", lt), ("右", rt)):
                if t.kind not in ("int", "bool", "char"):
                    self.error(f"range 的{side}端点必须是整数（或 char），得到 {t}", e)
            if self.in_range == 0:
                # `print(0..3)` / `let r = 0..10`：range 不是一等值，以前会一路
                # 走到 asmgen 的二元运算符表 KeyError: '..'，甩一条 Python traceback。
                self.error("范围 `起..止` 只能写在 for 的遍历位置（FA 的 range 不是"
                           "一等值：不能存进变量、当参数传、也不能 print）。"
                           "要一个整数序列请用 Vec<i64>，或直接 `for i in 起..止`", e)
            e.ty = Type("range", "range", 16, 8)
            return e.ty
        lt = self.expr(e.left)
        rt = self.expr(e.right)
        # 整数除以字面量 0：编译期就能断定是错的，别等到运行时 SIGFPE。
        # （浮点除零是 IEEE 754 的 ±inf / nan，属于合法运算，不拦。）
        if e.op in ("/", "%", "//") and isinstance(e.right, NumLit) \
                and not str(getattr(e.right, "kind", "") or "").startswith("f") \
                and e.right.value == 0 and lt.kind != "float":
            self.error(f"整数 '{e.op}' 的右操作数是常量 0（运行时必然是除零陷阱）", e)
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
            if (lt == CHAR and rt.kind in ("int", "bool")) \
                    or (rt == CHAR and lt.kind in ("int", "bool")):
                e.ty = BOOL              # char 就是一个字节，能和整数比较
                return BOOL
            if lt.kind == "ptr" and rt.kind == "int" and rt.name == "i64":
                e.ty = BOOL
                return BOOL
            # 枚举：变体**不带载荷**时（就是 C 那种枚举）`==` / `!=` 比 tag，
            # 这是最常写的判断（`if state == State.On`），以前一律报
            # 「无法比较 Color 与 Color」，只能拿 match 绕。
            # 带载荷的仍然拦：只比 tag 会让 Circle(2.0) == Circle(3.0) 成立，
            # 那是悄悄给错答案，比报错糟糕得多。
            if lt.kind == "enum" and rt.kind == "enum" and e.op in ("==", "!="):
                if lt.name != rt.name:
                    self.error(f"无法比较 {lt} 与 {rt}（不是同一个枚举）", e)
                if any(fl for _vn, fl, _vi in (lt.variants or [])):
                    # 举例要用**这个**枚举自己的变体名：写成 Circle(2.0) == Circle(3.0)
                    # 的话，比较 Maybe 时报出来一堆 Circle，看着像串了台。
                    ex = next((vn for vn, fl, _vi in (lt.variants or []) if fl), None)
                    hint = (f"只比 tag 的话，{ex}(…) 和 {ex}(…) 载荷不同也会算相等"
                            if ex else "只比 tag 会漏掉载荷")
                    self.error(
                        f"枚举 {lt.name} 的变体带载荷，'{e.op}' 比不出来："
                        f"{hint}（悄悄给错答案）。"
                        f"要么用 match 分支处理，要么给这个类型写个 eq 方法", e)
                e.ty = BOOL
                return BOOL
            self.error(f"无法比较 {lt} 与 {rt}", e)
        # 算术
        if lt.kind == "ptr" and rt.kind == "int" and e.op in ("+", "-"):
            e.ty = lt
            return lt
        # char 参与算术时按 C 的整型提升处理：`b - 'A' + 'a'` 这种大小写转换
        # 是最常见的写法，以前直接报「运算符 '-' 不支持 char 与 char」。
        # 结果是 i64（要当字符用就再 chr() 一次）。
        if (lt == CHAR or rt == CHAR) and e.op in ("+", "-", "*", "/", "%",
                                                   "&", "|", "^", "<<", ">>"):
            other = rt if lt == CHAR else lt
            if other == CHAR or other.kind in ("int", "bool"):
                e.ty = TYPES["i64"]
                return e.ty
            if other.kind == "float":
                e.ty = TYPES["f64"]
                return e.ty
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
        # 内建多态函数。用户自己定义了同名函数时**让用户赢**：
        # 内建分支以前排在最前面，于是 `fn sign(n: i64) -> str` 定义得好好的，
        # 调用却被悄悄换成内建的 sign（返回 -1/0/1 的 i64）—— 不报错、
        # 返回类型都不一样，是最难查的一类。名字解析（expr_name）本来就是
        # 先查 self.fns 再查 BUILTIN_FNS，这里跟上就好。
        if isinstance(e.callee, NameRef) and e.callee.name in BUILTIN_FNS \
                and e.callee.name not in self.fns \
                and not any(e.callee.name in t for t in self.local_fns):
            ats = [self.expr(a) for a in e.args]
            name = e.callee.name
            e.resolved = "builtin"
            if name in ("sqrt", "sin", "cos", "tan", "pow", "log", "exp",
                        "floor", "ceil", "to_f64", "now"):
                e.ty = TYPES["f64"]
            elif name == "abs":                       # 跟随实参类型
                e.ty = TYPES["f64"] if (ats and ats[0].is_float) else TYPES["i64"]
            elif name in ("min", "max"):
                # min(a, b) -> 标量类型；min(v) -> 容器元素类型
                if ats and ats[0].kind == "vec":
                    self.check_vec_content_op(ats[0].elem, name, e)
                if len(ats) == 1 and ats[0].kind in ("vec", "arr", "map", "str"):
                    e.ty = self._container_elem(ats[0])
                else:
                    e.ty = ats[0] if ats else TYPES["i64"]
            elif name in ("len", "i64", "to_i64", "gcd", "random", "at", "bytes"):
                e.ty = TYPES["i64"]
            elif name in ("str", "to_str", "read_line", "concat", "env",
                          "file_read", "cmd", "hex", "oct", "bin", "chr"):
                if name == "concat" and not e.args:
                    self.error("concat() 至少要一个参数（要拼接的字符串或值）", e)
                e.ty = STR
            elif name == "args":
                e.ty = vec_of(STR)
            elif name == "free":
                if len(ats) != 1:
                    self.error(f"free(p) 需要 1 个参数（要释放的指针），"
                               f"这里给了 {len(ats)} 个", e)
                if ats[0].kind != "ptr":
                    self.error(f"free() 只能释放指针（`new` 出来的，或 C 那边 malloc 的），"
                               f"得到 {ats[0]}。str / Vec / Map 是引用计数的，"
                               "出作用域自动释放，不用也不能 free", e)
                e.ty = VOID
            elif name in ("round", "trunc", "log2", "log10", "exp2", "hypot", "clamp"):
                e.ty = TYPES["f64"] if (not ats or ats[0].is_float) else TYPES["i64"]
            elif name in ("assert", "print", "println", "write", "exit",
                          "sleep", "panic"):
                if name == "exit" and len(e.args) > 1:
                    self.error(f"exit() 最多一个参数（退出码），这里给了 {len(e.args)} 个", e)
                e.ty = VOID
            elif name == "contains":
                if ats and ats[0].kind == "vec":
                    self.check_vec_content_op(ats[0].elem, name, e)
                e.ty = TYPES["bool"]
            elif name == "sum":
                if ats and ats[0].kind == "vec":
                    self.check_vec_content_op(ats[0].elem, name, e)
                e.ty = self._container_elem(ats[0]) if ats else TYPES["i64"]
            elif name in ("sort", "reverse", "push", "clear", "resize"):
                if name == "sort" and ats and ats[0].kind == "vec":
                    self.check_vec_content_op(ats[0].elem, name, e)
                e.ty = VOID
            elif name == "join":
                e.ty = STR
            elif name == "pop":
                e.ty = self._container_elem(ats[0]) if ats else ANY
            elif name == "keys":
                e.ty = vec_of(ats[0].key) if (ats and ats[0].kind == "map") else vec_of(STR)
            elif name == "values":
                e.ty = vec_of(ats[0].val) if (ats and ats[0].kind == "map") else vec_of(STR)
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
            # 枚举变体构造 Enum.Variant(...)
            if getattr(e.callee, "is_variant", False):
                return self.check_variant_ctor(e, ct)
            ot = self.expr(e.callee.obj)
            self.error(f"不支持的调用形式", e)
        self.error(f"不能调用非函数类型 {ct}", e)

    # ------------------------------------------------------- 枚举变体
    def variant_fields(self, ety: Type, vi: int):
        """(变体名, [(字段名, 类型, 相对载荷区的偏移)])"""
        for (vn, fl, i) in (ety.variants or []):
            if i == vi:
                return vn, (fl or [])
        return "?", []

    def check_variant_args(self, e, ety: Type, vi: int) -> Type:
        """带载荷的变体构造：Shape.Rect(3.0, 4.0)"""
        vn, fl = self.variant_fields(ety, vi)
        if len(fl) != len(e.args):
            self.error(f"枚举变体 {ety.name}.{vn} 需要 {len(fl)} 个载荷，"
                       f"实际给了 {len(e.args)} 个", e)
        for i, a in enumerate(e.args):
            at = self.expr(a)
            self.check_assignable(fl[i][1], at, a,
                                  f"{ety.name}.{vn} 的第 {i+1} 个载荷")
        e.resolved = "enum-ctor"
        e.variant_index = vi
        e.ty = ety
        return ety

    def check_variant_ctor(self, e: Call, ety: Type) -> Type:
        return self.check_variant_args(e, ety, e.callee.variant_index)

    def bind_variant_pattern(self, pat, st: Type, arm):
        """match 里的 `Shape.Rect(w, h)`：把载荷绑定到 w / h 两个局部变量。

        注意 `Shape.Rect(w, h)` 会被语法分析器当成**方法调用**（obj=Shape, name=Rect），
        只有省略枚举名写成 `Rect(w, h)` 时才是 Call(callee=NameRef)，
        而 `f().Rect(w, h)` 那种才是 Call(callee=Field)。三种形态都要认。
        """
        vi, args = None, []
        if isinstance(pat, MethodCall):
            ot = self.expr(pat.obj)
            if ot.kind == "enum":
                names = [v[0] for v in (ot.variants or [])]
                if pat.name in names:
                    vi, st = names.index(pat.name), ot
            args = pat.args
        elif isinstance(pat, Call):
            callee = pat.callee
            if isinstance(callee, Field):
                self.expr(callee)                   # 解析出 is_variant / variant_index
                if getattr(callee, "is_variant", False):
                    vi = callee.variant_index
            elif isinstance(callee, NameRef):       # 允许省略枚举名：Rect(w, h)
                names = [v[0] for v in (st.variants or [])]
                if callee.name in names:
                    vi = names.index(callee.name)
            args = pat.args
        if vi is None:
            self.error(f"match 分支需要 {st} 的变体", arm)
        vn, fl = self.variant_fields(st, vi)
        if len(fl) != len(args):
            self.error(f"变体 {st.name}.{vn} 有 {len(fl)} 个载荷，"
                       f"模式里写了 {len(args)} 个绑定名", arm)
        binds = []
        for i, a in enumerate(args):
            if not isinstance(a, NameRef):
                self.error("载荷模式只能是绑定名，例如 Rect(w, h)", a)
            fty = fl[i][1]
            a.ty = fty
            if a.name != "_":
                self.scope.declare(a.name, VarSym(a.name, fty))
                binds.append((a.name, fl[i][2], fty))
        pat.is_variant = True
        pat.variant_index = vi
        pat.ty = st
        pat.bindings = binds

    def check_no_local_addr(self, e, s):
        """`return &a`（a 是本函数的局部变量或形参）拿到的是悬垂指针。

        函数一返回那块栈就没了。实测这种代码常常「碰巧还对」（栈上那几个字节
        还没被覆盖，最简形式真能打出正确的值），换个函数、多一层调用就变垃圾 ——
        典型的悄悄给错答案，比直接崩难查得多。要在堆上建对象就写 new。
        """
        if not isinstance(e, AddrOf) or not isinstance(e.operand, NameRef):
            return
        v = self.scope.lookup(e.operand.name) if self.scope else None
        if isinstance(v, VarSym):
            self.error(
                f"不能返回局部变量 '{e.operand.name}' 的地址：函数一返回那块栈就没了，"
                f"调用方拿到的是悬垂指针（常常「碰巧还对」，换个调用就变垃圾值）。"
                f"要返回堆上的对象用 new，要返回一份值就直接返回它", s)

    def method_lookup_type(self, ot: Type) -> Type:
        """找方法时看哪个类型：`*P` 上看 `P` 的（自动解引用）。

        字段访问一直是自动解引用的（`p.x`，p 是 `*P`），方法却不认，
        报「类型 *P 没有方法 'bump'」—— 同一种东西两套规矩，谁都会踩。
        代码生成那边不用改：方法的 self 本来就是指针，`gen_expr(*P 变量)`
        给出的正是那个指针。
        """
        if getattr(ot, "kind", None) == "ptr" and getattr(ot, "inner", None) is not None \
                and ot.inner.kind in ("struct", "enum"):
            return ot.inner
        return ot

    def check_method_receiver(self, e: MethodCall, ot: Type, fs: FnSym):
        """分清楚「静态方法」（impl 里不带 self）和普通方法（带 self）。

        两头都得拦：
          * `P.norm()`：norm 要 self，可接收者是**类型名**，没有实例可传。
            以前语义层放行，代码生成去查一个叫 P 的变量，崩在
            「代码生成错误：未定义变量 'P'」。
          * `p.make(2.0)`：make 没有 self，可接收者是个值。以前语义层放行，
            代码生成把 p 当成第一个实参塞进去 —— 形参对不上号，
            `fn make(a: f64)` 收到的是结构体的地址当浮点位模式用，
            悄悄算出一个垃圾数。
        """
        decl = getattr(fs, "decl", None)
        has_self = bool(decl is not None and any(
            getattr(p, "name", "") == "self"
            for p in (getattr(decl, "params", None) or [])))
        names = set(self.structs or {}) | set(self.enums or {})
        recv_is_type = isinstance(e.obj, NameRef) and e.obj.name in names
        if recv_is_type and has_self:
            self.error(f"方法 '{e.name}' 带 self，得用实例调用："
                       f"let p = {e.obj.name} {{...}}，然后 p.{e.name}(...)"
                       f"；想直接用类型名调用，就把形参里的 self 去掉", e)
        elif not recv_is_type and not has_self \
                and self.method_lookup_type(ot).kind in ("struct", "enum"):
            lk = self.method_lookup_type(ot)
            self.error(f"方法 '{e.name}' 没有 self（静态方法），要用类型名调用："
                       f"{lk.name}.{e.name}(...)", e)

    def expr_method(self, e: MethodCall) -> Type:
        ot = self.expr(e.obj)
        # 枚举变体构造器在语法上和方法调用一模一样：Shape.Circle(2.0)
        if ot.kind == "enum":
            names = [v[0] for v in (ot.variants or [])]
            if e.name in names:
                return self.check_variant_args(e, ot, names.index(e.name))
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
        lk = self.method_lookup_type(ot)
        key = (lk.name if lk.kind in ("struct", "enum") else lk.kind, e.name)
        fs = self.methods.get(key)
        if fs is None and lk.kind in ("struct", "enum"):
            fs = self.methods.get((lk.name, e.name))
        if fs is not None:
            self.check_method_receiver(e, ot, fs)
            e.resolved = fs
            e.ty = fs.ret
            return fs.ret
        # 内建方法
        kind = ot.kind if ot.kind in BUILTIN_METHODS else None
        if kind and e.name in BUILTIN_METHODS[kind]:
            lo, hi = METHOD_ARITY[kind].get(e.name, (0, None))
            n = len(e.args)
            if n < lo or (hi is not None and n > hi):
                want = f"{lo} 个" if lo == hi else (
                    f"{lo}~{hi} 个" if hi is not None else f"至少 {lo} 个")
                self.error(f"{ot}.{e.name}() 需要 {want}参数，这里给了 {n} 个", e)
            e.resolved = "builtin-method"
            if ot.kind in ("vec", "map"):
                self.check_vec_content_op(ot.elem if ot.kind == "vec" else None,
                                          e.name, e)
                self.check_container_args(ot, e)
            if e.name == "to_f64":
                e.ty = TYPES["f64"]
            elif e.name in ("len", "at", "to_i64", "find", "bytes",
                            "char_len", "char_at"):
                e.ty = TYPES["i64"]
            elif e.name == "codepoints":
                # 码点可能 > 255，char（u8）装不下，所以是 Vec<i64>
                e.ty = vec_of(TYPES["i64"])
            elif e.name == "cstr":
                # 返回的是 FaStr 内部字节区的裸指针（**不加引用**），不是 str。
                # 以前标成 STR，于是 `let p = s.cstr()` 会对这个 char* 调 rc_inc，
                # 把字符串数据当成对象头去写 —— 实测直接段错误。
                e.ty = ptr_to(TYPES["u8"])
            elif e.name in ("to_str", "slice", "trim", "upper", "lower",
                            "replace", "to_str_deep",
                            "repeat", "trim_start", "trim_end", "join",
                            "slice_chars"):
                e.ty = STR
            elif e.name in ("split", "chars", "keys", "values", "lines"):
                # Map 的 keys()/values() 元素类型跟着 K / V 走（不是 str）
                if ot.kind == "map" and e.name in ("keys", "values"):
                    e.ty = vec_of(ot.key if e.name == "keys" else ot.val)
                else:
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
            elif e.name == "copy":
                # v.copy() / m.copy()：另起一份容器，类型和接收者完全一样
                e.ty = ot
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
            elif e.name in ("round", "trunc", "sqrt", "log", "log2", "log10",
                            "exp", "exp2", "sin", "cos", "tan"):
                # 这些一律按 f64 返回：漏掉的话类型是 ANY，
                # print 会把它当整数打（1.5.round() 打出 1 而不是 2.0）
                e.ty = TYPES["f64"]
            else:
                e.ty = ANY
            return e.ty
        # .to_str() 对任何有实现的类型都成立，等价于全局的 str(x)。
        # 以前只有 str/int/float/vec/map 放行，结构体、数组、bool、char、指针上
        # 调用会被拒 —— 而 codegen 里那条通用字符串化分支其实早就支持它们了。
        if e.name == "to_str" and ot.kind in TOSTR_OK:
            if e.args:
                self.error(f"{ot}.to_str() 不接受参数，这里给了 {len(e.args)} 个", e)
            e.resolved = "builtin-method"
            e.ty = STR
            return STR
        # UFCS：自由函数以对象作为首个参数
        fs = self.fns.get(e.name)
        if fs is not None and fs.params and fs.params[0] == ot:
            e.resolved = fs
            e.ty = fs.ret
            return fs.ret
        if ot.kind == "ptr" and getattr(ot, "inner", None) is not None \
                and ot.inner.kind in ("vec", "map", "str", "arr"):
            # `vp.push(3)`（vp 是 *Vec<i64>）：容器的值**本身就是一个指针**，
            # 再取一层地址就是指针的指针，运行时的 fa_vec_push 会把外层地址
            # 当成 FaVec 头去读 —— 不是报错，是踩内存。这里说清楚两条出路。
            self.error(
                f"{ot} 上不能直接调 .{e.name}()：容器和 str 本身就是引用，"
                f"要改到原对象，形参直接写 {ot.inner}（不用加 *）；"
                f"确实拿到指针的话，先解引用再调：(*p).{e.name}(...)", e)
        self.error(f"类型 {ot} 没有方法 '{e.name}'", e)

    # ------------------------------------------------------------- 赋值检查
    def _container_elem(self, t: Type) -> Type:
        """容器实参的元素类型（给 sum/min/max/pop/keys/values 推断返回类型用）"""
        if t is None:
            return ANY
        if t.kind == "vec":
            return t.elem or ANY
        if t.kind == "arr":
            return t.elem or ANY
        if t.kind == "map":
            return t.val or ANY
        if t == STR:
            return TYPES["char"]
        return t if t.kind in ("int", "float") else ANY

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
