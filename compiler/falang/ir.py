"""FA 线性 IR（三地址码）。值 = 虚拟寄存器 / 常量 / 符号 / 字符串常量。"""

from __future__ import annotations
from typing import List, Optional, Any
from .types import Type


class Temp:
    __slots__ = ("id", "ty", "fixed")

    def __init__(self, id: int, ty: Type = None, fixed: str = None):
        self.id = id
        self.ty = ty
        self.fixed = fixed          # 预着色（如参数寄存器）

    def __repr__(self):
        return f"t{self.id}"


class Const:
    __slots__ = ("val", "ty")

    def __init__(self, val, ty: Type = None):
        self.val = val
        self.ty = ty

    def __repr__(self):
        return f"#{self.val}"


class Sym:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f"@{self.name}"


class StrConst:
    __slots__ = ("val", "idx")

    def __init__(self, val: str, idx: int = -1):
        self.val = val
        self.idx = idx

    def __repr__(self):
        return f'"{self.val}"'


class Label:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f"<{self.name}>"


class Instr:
    __slots__ = ("op", "dst", "args", "extra", "ty", "line")

    def __init__(self, op, dst=None, args=None, extra=None, ty=None, line=0):
        self.op = op
        self.dst = dst
        self.args = args or []
        self.extra = extra
        self.ty = ty
        self.line = line

    def __repr__(self):
        parts = [self.op]
        if self.dst is not None:
            parts.append(f"{self.dst} =")
        parts += [str(a) for a in self.args]
        if self.extra is not None:
            parts.append(f"[{self.extra}]")
        return " ".join(parts)


def instr_uses(ins: "Instr"):
    """指令读取的全部 Temp：args 里的，以及 extra 里的（extra 可能是 Temp，
    也可能是 (基址/下标 Temp, 比例) 这样的元组——比例变址寻址用）。"""
    out = []
    for a in ins.args:
        if isinstance(a, Temp):
            out.append(a)
    e = ins.extra
    if isinstance(e, Temp):
        out.append(e)
    elif isinstance(e, (tuple, list)):
        for x in e:
            if isinstance(x, Temp):
                out.append(x)
    return out


class IRFunc:
    def __init__(self, name: str, params: List[Temp], ret: Type, sret: bool = False):
        self.name = name
        self.params = params
        self.ret = ret
        self.sret = sret
        self.instrs: List[Instr] = []
        self.nstack = 0            # 本函数需要的栈帧大小（寄存器分配后回填）
        self.spills: dict = {}
        self.varargs = False
        self.extern = False
        self.is_main = False


class IRModule:
    def __init__(self):
        self.funcs: List[IRFunc] = []
        self.strings: List[str] = []
        self.descs: List[Any] = []     # 结构体类型（需要生成 drop 函数与描述符）
        self.globals: List[Instr] = []
        self.data: List[str] = []      # 额外汇编数据段
        self.init_hooks: List[str] = []  # 需要在 main 之前调用的初始化符号

    def add_string(self, s: str) -> int:
        if s in self.strings:
            return self.strings.index(s)
        self.strings.append(s)
        return len(self.strings) - 1
