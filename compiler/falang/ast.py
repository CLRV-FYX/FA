"""FA 抽象语法树 (AST) 节点定义。"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Any


class Node:
    """AST 基类。刻意不使用 dataclass，避免默认值字段顺序影响子类。"""
    line = 0
    col = 0


# ------------------------------------------------------------------ 类型
@dataclass
class Type(Node):
    pass


@dataclass
class TName(Type):
    name: str
    args: List["Type"] = field(default_factory=list)


@dataclass
class TPtr(Type):
    inner: Type


@dataclass
class TArr(Type):
    elem: Type
    size: Optional["Expr"] = None     # None = 切片/动态


@dataclass
class TFn(Type):
    params: List[Type]
    ret: Type


@dataclass
class TOptional(Type):
    inner: Type


# ------------------------------------------------------------------ 表达式
@dataclass
class Expr(Node):
    # kw_only：避免该默认字段影响子类后续字段的位置顺序
    ty: Any = field(default=None, kw_only=True)


@dataclass
class NumLit(Expr):
    value: object
    kind: str = "i64"       # i64 / f64


@dataclass
class StrLit(Expr):
    parts: List            # [("lit", str) | ("expr", Expr)]


@dataclass
class CharLit(Expr):
    value: str


@dataclass
class BoolLit(Expr):
    value: bool


@dataclass
class NilLit(Expr):
    pass


@dataclass
class NameRef(Expr):
    name: str
    resolved: Any = None


@dataclass
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass
class Unary(Expr):
    op: str
    operand: Expr


@dataclass
class Cast(Expr):
    operand: Expr
    target: Type


@dataclass
class Call(Expr):
    callee: Expr
    args: List[Expr]


@dataclass
class Index(Expr):
    obj: Expr
    index: Expr


@dataclass
class Slice(Expr):
    """a[lo:hi] —— str 和 Vec 的切片。

    lo / hi 都可以省（a[1:] / a[:3] / a[:]），省掉的那头由运行时按长度夹边，
    和 str.slice(a, b) 是同一套规矩：负的夹到 0、超过长度的夹到长度、
    反了给空结果。返回**新的** str / Vec，原来那个不动（str 本来就不可变，
    Vec 也只有一份数据，共享会让引用计数算不清）。
    """
    obj: Expr
    start: Optional[Expr] = None
    end: Optional[Expr] = None


@dataclass
class Field(Expr):
    obj: Expr
    name: str
    index: int = -1


@dataclass
class MethodCall(Expr):
    obj: Expr
    name: str
    args: List[Expr]
    resolved: Any = None


@dataclass
class ArrayLit(Expr):
    elems: List[Expr]


@dataclass
class StructLit(Expr):
    name: Optional[str]
    fields: List          # [(name, Expr)]
    resolved: Any = None


@dataclass
class AddrOf(Expr):
    operand: Expr


@dataclass
class Deref(Expr):
    operand: Expr


@dataclass
class NewExpr(Expr):
    operand: Expr


@dataclass
class Range(Expr):
    start: Optional[Expr]
    end: Optional[Expr]
    inclusive: bool
    # 步长。`a..b` 语法没有步长（None 就是 1），只有 range(a, b, s) 会填。
    # 可以是负数（倒着走），不能是 0（循环永远不结束 —— 字面量 0 编译期就拦，
    # 运行时才算出来的 0 在进循环前 panic 一句人话）。
    step: Optional[Expr] = None


@dataclass
class Ctor(Expr):
    """泛型容器构造器：Vec<T>(...) / Vec<T>[...] / Map<K,V>()"""
    name: str
    targs: List[Type] = field(default_factory=list)
    args: List[Expr] = field(default_factory=list)


@dataclass
class SizeOf(Expr):
    operand: Type


@dataclass
class RawExpr(Expr):
    """内联汇编 / 内联 C++ 表达式转义口"""
    kind: str
    code: str


# ------------------------------------------------------------------ 语句
@dataclass
class Stmt(Node):
    pass


@dataclass
class Block(Stmt):
    stmts: List[Stmt]
    flat: bool = False      # True：不新建作用域（用于元组解构这类语法糖展开）
    # True：这个分支以 return / break / continue / panic 结尾，永远不会产出值。
    # if / match 当表达式用时由语义分析填好，后端据此跳过「把尾表达式写进结果」。
    diverges: bool = False


@dataclass
class Let(Stmt):
    name: str
    ty: Optional[Type]
    init: Optional[Expr]
    mutable: bool = False
    sym: Any = None


@dataclass
class Assign(Stmt):
    target: Expr
    value: Expr
    op: str = "="


@dataclass
class Return(Stmt):
    value: Optional[Expr]


@dataclass
class If(Stmt):
    cond: Expr
    body: Block
    elifs: List = field(default_factory=list)   # [(Expr, Block)]
    orelse: Optional[Block] = None


@dataclass
class While(Stmt):
    cond: Expr
    body: Block


@dataclass
class For(Stmt):
    var: str
    iter: Expr
    body: Block
    sym: Any = None
    # 第二个循环变量：`for i, x in v` / `for k, v in m`。
    # 规矩是**先给定位，再给内容**：
    #   Vec / 数组 / str  →  var = 下标 (i64)，var2 = 元素 / 字符
    #   Map               →  var = 键，      var2 = 值
    # range 只有一个变量（它给的就是当前值，没有第二个可绑的东西）。
    var2: Optional[str] = None
    sym2: Any = None


@dataclass
class ForC(Stmt):
    """C/Java 风格 for (init; cond; step)"""
    init: Optional[Stmt] = None
    cond: Optional[Expr] = None
    step: Optional[Stmt] = None
    body: Block = None


@dataclass
class Loop(Stmt):
    body: Block


@dataclass
class Break(Stmt):
    pass


@dataclass
class Continue(Stmt):
    pass


@dataclass
class Defer(Stmt):
    call: Expr


@dataclass
class ExprStmt(Stmt):
    expr: Expr


@dataclass
class MatchArm(Node):
    pattern: Any     # Expr 或 "_" 字符串 或 ('enum', name, [bindings])
    body: Block


@dataclass
class Match(Stmt):
    subject: Expr
    arms: List[MatchArm]


@dataclass
class Asm(Stmt):
    code: str
    volatile: bool = True


# ------------------------------------------------------------------ 声明
@dataclass
class Decl(Node):
    pass


@dataclass
class Param(Node):
    name: str
    ty: Type


@dataclass
class FnDef(Decl):
    name: str
    params: List[Param]
    ret: Optional[Type]
    body: Optional[Block]      # None = extern 声明
    extern: bool = False
    varargs: bool = False
    pub: bool = False
    abi: str = "C"
    cname: Optional[str] = None
    sym: Any = None


@dataclass
class StructDef(Decl):
    name: str
    fields: List            # [(name, Type)]
    pub: bool = False
    packed: bool = False
    sym: Any = None
    # 字段默认值：{字段名: 初值表达式}。写了默认值的字段，在结构体字面量里
    # 可以省略（`P { y: 4 }` 会用默认值补上 x）。fields 仍是 (name, Type)
    # 二元组，不动它的形状 —— 消费方太多，多塞一个元素容易漏改。
    defaults: Any = field(default_factory=dict)


@dataclass
class EnumDef(Decl):
    name: str
    variants: List          # [(name, [(fname, Type)] or None, optional_value)]
    sym: Any = None


@dataclass
class ImplDef(Decl):
    type_name: str
    methods: List[FnDef]


@dataclass
class Use(Decl):
    kind: str               # std / file / c / cxx / py / java / lib / rawcxx
    path: str = ""
    alias: str = ""
    lib: str = ""
    body: List[Decl] = field(default_factory=list)
    raw: str = ""


@dataclass
class Const(Decl):
    name: str
    ty: Optional[Type]
    init: Expr


@dataclass
class Global(Decl):
    """顶层 `let`：全局可变变量。

    与 `const` 的区别：const 是编译期常量（每次用到就重新求值一遍初值表达式），
    全局变量有**唯一一份存储**（.bss 里的一个槽），可以被任何函数读写。
    初值在 main 的第一条用户语句之前执行一次（没有初值就是零值）。
    """
    name: str
    ty: Optional[Type]
    init: Optional[Expr]
    mutable: bool = True
    gty: Any = None          # sema 解析出的实际类型
    sym: Any = None          # VarSym(is_global=True)


@dataclass
class Module(Node):
    decls: List[Decl]


# -------------------------------------------------------------- 位置信息
def stamp_positions(n, pline: int = 0, pcol: int = 0) -> None:
    """自顶向下补全缺失的行/列。

    解析器只在少数节点上记了位置，于是绝大多数报错都打印「行 0, 列 0」，
    等于没有定位。这里把父节点的位置继承给还没位置的子节点，
    保证任何诊断至少能指到它所在的那条语句/声明。
    """
    if isinstance(n, Node):
        if not getattr(n, "line", 0):
            n.line, n.col = pline, pcol
        pline, pcol = n.line, n.col
        for v in list(vars(n).values()):
            stamp_positions(v, pline, pcol)
    elif isinstance(n, (list, tuple)):
        for v in n:
            stamp_positions(v, pline, pcol)
