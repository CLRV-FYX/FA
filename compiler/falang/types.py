"""FA 类型系统。"""

from __future__ import annotations
from typing import List, Dict, Optional, Tuple

INT_TYPES = {"i8": 1, "i16": 2, "i32": 4, "i64": 8, "isize": 8,
             "u8": 1, "u16": 2, "u32": 4, "u64": 8, "usize": 8}
FLOAT_TYPES = {"f32": 4, "f64": 8}
SIGNED = {"i8", "i16", "i32", "i64", "isize"}
UNSIGNED = {"u8", "u16", "u32", "u64", "usize"}

# 引用计数 kind 编号（与 runtime/fa_runtime.c 保持一致）
K_NONE, K_STR, K_VEC, K_MAP, K_PY, K_JOBJ = 0, 1, 2, 3, 4, 5
K_BOX = 6                       # 装箱的纯数据结构体（无内部引用）：释放时直接 free
K_STRUCT_DESC_BASE = 1000       # +desc_id：内联/嵌套结构体，释放字段但不 free
K_BOXED_STRUCT = 2000           # +desc_id：容器里装箱的结构体，释放字段 + free

# 元素「显示类型」编号（与 runtime/fa_runtime.h 的 FA_TY_* 一一对应）。
# kind 只决定怎么释放引用，分不清 i64/u64/f64/bool/char，
# 容器因此额外带一个类型码，to_str / join / print 才能格式化正确。
TY_INT, TY_FLOAT, TY_BOOL, TY_CHAR, TY_STR, TY_VEC, TY_MAP, TY_STRUCT, \
    TY_PYOBJ, TY_JOBJ, TY_PTR, TY_ENUM, TY_ARR, TY_ANY = range(14)


def ty_code(t: "Type") -> int:
    """FA 类型 -> 运行时元素类型码"""
    if t is None:
        return TY_ANY
    return {
        "int": TY_INT, "float": TY_FLOAT, "bool": TY_BOOL, "char": TY_CHAR,
        "str": TY_STR, "vec": TY_VEC, "map": TY_MAP, "struct": TY_STRUCT,
        "enum": TY_ENUM, "arr": TY_ARR, "ptr": TY_PTR, "fn": TY_PTR,
        "pyobj": TY_PYOBJ, "jobj": TY_JOBJ, "any": TY_ANY, "void": TY_ANY,
    }.get(t.kind, TY_ANY)


class Type:
    __slots__ = ("kind", "name", "size", "align", "elem", "count", "fields",
                 "params", "ret", "inner", "variants", "key", "val", "desc_id",
                 "methods", "_hash")

    def __init__(self, kind: str, name: str = "", size: int = 8, align: int = 8):
        self.kind = kind            # int float bool char void ptr str vec map struct enum arr fn any pyobj jobj
        self.name = name
        self.size = size
        self.align = align
        self.elem: Optional[Type] = None
        self.count: int = 0
        self.fields: Optional[List[Tuple[str, "Type", int]]] = None   # (name, ty, offset)
        self.params: List[Type] = []
        self.ret: Optional[Type] = None
        self.inner: Optional[Type] = None
        self.variants: Optional[List[Tuple[str, List[Tuple[str, "Type"]], int]]] = None
        self.key: Optional[Type] = None
        self.val: Optional[Type] = None
        self.desc_id: int = -1
        self.methods: Dict[str, object] = {}
        self._hash = None

    # -------------------------------------------------------------- 判定
    @property
    def is_int(self) -> bool:
        return self.kind == "int"

    @property
    def is_float(self) -> bool:
        return self.kind == "float"

    @property
    def is_num(self) -> bool:
        return self.kind in ("int", "float")

    @property
    def is_bool(self) -> bool:
        return self.kind == "bool"

    @property
    def is_signed(self) -> bool:
        return self.kind == "int" and self.name in SIGNED

    @property
    def is_ptr(self) -> bool:
        return self.kind in ("ptr", "str", "vec", "map", "pyobj", "jobj", "fn")

    @property
    def is_refcounted(self) -> bool:
        """是否需要在作用域结束时 rc_dec"""
        if self.kind in ("str", "vec", "map", "pyobj", "jobj"):
            return True
        if self.kind == "struct":
            return any(t_is_refcounted(f[1]) for f in self.fields)
        if self.kind == "arr":
            return t_is_refcounted(self.elem)
        if self.kind == "enum":
            # 带载荷的变体：只要有一个变体的某个字段需要计数，整个枚举就需要
            return any(t_is_refcounted(f[1])
                       for (_vn, fl, _i) in (self.variants or []) for f in (fl or []))
        return False

    @property
    def rc_kind(self) -> int:
        return {"str": K_STR, "vec": K_VEC, "map": K_MAP,
                "pyobj": K_PY, "jobj": K_JOBJ}.get(self.kind, K_NONE)

    def needs_rc_desc(self) -> bool:
        # 枚举也一样：有载荷字段要释放，就得有描述符 + drop/retain 函数
        return self.kind in ("struct", "enum") and self.is_refcounted

    @property
    def is_aggregate(self) -> bool:
        return self.kind in ("struct", "arr", "enum")

    def __eq__(self, other) -> bool:
        if other is None or not isinstance(other, Type):
            return False
        if self.kind in ("int", "float", "bool", "char", "void", "str", "vec",
                         "map", "any", "ptr", "pyobj", "jobj"):
            return self.kind == other.kind and self.name == other.name
        if self.kind == "struct" or self.kind == "enum":
            return self.name == other.name and self.kind == other.kind
        if self.kind == "arr":
            return other.kind == "arr" and self.count == other.count and self.elem == other.elem
        if self.kind == "fn":
            return other.kind == "fn" and self.params == other.params and self.ret == other.ret
        return False

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash((self.kind, self.name, self.count))
        return self._hash

    def __repr__(self) -> str:
        if self.kind == "arr":
            return f"[{self.elem} x {self.count}]"
        if self.kind == "ptr":
            return f"*{self.inner}"
        if self.kind == "struct" or self.kind == "enum":
            return self.name
        if self.kind == "vec":
            return f"Vec<{self.elem}>"
        if self.kind == "map":
            return f"Map<{self.key},{self.val}>"
        if self.kind == "fn":
            return f"fn({','.join(map(str, self.params))})->{self.ret}"
        return self.name or self.kind


def t_is_refcounted(t: Type) -> bool:
    if t is None:
        return False
    if t.kind in ("str", "vec", "map", "pyobj", "jobj"):
        return True
    if t.kind == "struct":
        return any(t_is_refcounted(f[1]) for f in (t.fields or []))
    if t.kind == "arr":
        return t_is_refcounted(t.elem)
    if t.kind == "enum":
        return any(t_is_refcounted(f[1])
                   for (_vn, fl, _i) in (t.variants or []) for f in (fl or []))
    return False


# -------------------------------------------------------------- 内建类型实例
VOID = Type("void", "void", 0, 1)
BOOL = Type("bool", "bool", 1, 1)
CHAR = Type("char", "char", 1, 1)
STR = Type("str", "str", 8, 8)
ANY = Type("any", "any", 8, 8)
PYOBJ = Type("pyobj", "pyobj", 8, 8)
JOBJ = Type("jobj", "jobj", 8, 8)

TYPES = {n: Type("int", n, s, s) for n, s in INT_TYPES.items()}
for _n, _s in FLOAT_TYPES.items():
    TYPES[_n] = Type("float", _n, _s, _s)
TYPES.update({"bool": BOOL, "char": CHAR, "str": STR, "void": VOID,
              "any": ANY, "pyobj": PYOBJ, "jobj": JOBJ})


def ptr_to(inner: Type) -> Type:
    t = Type("ptr", f"*{inner}", 8, 8)
    t.inner = inner
    return t


def vec_of(elem: Type) -> Type:
    t = Type("vec", f"Vec<{elem}>", 8, 8)
    t.elem = elem
    return t


def map_of(k: Type, v: Type) -> Type:
    t = Type("map", f"Map<{k},{v}>", 8, 8)
    t.key, t.val = k, v
    return t


def arr_of(elem: Type, count: int) -> Type:
    t = Type("arr", f"[{elem} x {count}]", elem.size * max(count, 1), elem.align)
    t.elem, t.count = elem, count
    return t


U8P = ptr_to(TYPES["u8"])


def layout_struct(name: str, fields: List[Tuple[str, Type]], packed=False) -> Type:
    off, maxalign = 0, 1
    laid = []
    for fname, fty in fields:
        a = 1 if packed else fty.align
        off = (off + a - 1) // a * a
        laid.append((fname, fty, off))
        off += fty.size
        maxalign = max(maxalign, a)
    size = (off + maxalign - 1) // maxalign * maxalign
    t = Type("struct", name, max(size, 1), maxalign)
    t.fields = laid
    return t


def layout_enum(name: str, variants: List[Tuple[str, Optional[List[Tuple[str, Type]]], int]]) -> Type:
    """枚举内存布局：{ i64 tag; u8 payload[max] }，整体按 8 字节对齐。"""
    maxpay = 0
    laid = []
    for i, (vname, vfields, val) in enumerate(variants):
        if vfields:
            # 多字段变体视为匿名结构体内联
            foff, fal = 0, 1
            flaid = []
            for fn, fty in vfields:
                foff = (foff + fty.align - 1) // fty.align * fty.align
                flaid.append((fn, fty, foff))
                foff += fty.size
                fal = max(fal, fty.align)
            pay = max(foff, 1)
            laid.append((vname, flaid, i))
            maxpay = max(maxpay, pay)
        else:
            laid.append((vname, [], i))
    pay = (maxpay + 7) // 8 * 8
    t = Type("enum", name, 8 + pay, 8)
    t.variants = laid
    return t
