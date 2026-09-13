"""FA: AST -> 线性 IR。

约定（务必与 asmgen / runtime 保持一致）
----------------------------------------
* 标量值用虚拟寄存器（Temp）表示；聚合类型（struct/arr/enum）的值统一用「指向存储的指针」表示。
* 所有权：表达式产生的引用类型值是 **owned**；语句结束时未被移动的临时引用会被 rc_dec。
  变量持有 owned 引用，作用域结束时释放；函数形参是 **borrowed**，不释放；
  返回值在 return 时 rc_inc（调用方负责释放）。
"""

from __future__ import annotations
import struct
from typing import List, Optional, Tuple, Any
from .ast import *
from . import types as T
from .types import (Type, TYPES, VOID, BOOL, CHAR, STR, ANY, PYOBJ, JOBJ,
                    ptr_to, vec_of, map_of, arr_of, K_STR, K_VEC, K_MAP,
                    K_PY, K_JOBJ, K_NONE, K_BOX, K_BOXED_STRUCT,
                    K_STRUCT_DESC_BASE)
from .ir import Temp, Const, Sym, StrConst, Label, Instr, IRFunc, IRModule
from .sema import Sema, VarSym, FnSym, BUILTIN_FNS

I64 = TYPES["i64"]
F64 = TYPES["f64"]
U8 = TYPES["u8"]


class FaCodegenError(Exception):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        super().__init__(msg)
        self.msg, self.line, self.col = msg, line, col

    def pretty(self, src: str = "") -> str:
        head = f"代码生成错误 (行 {self.line}, 列 {self.col}): {self.msg}"
        if src:
            lines = src.split("\n")
            if 1 <= self.line <= len(lines):
                head += "\n    " + lines[self.line - 1]
                head += "\n    " + " " * max(0, self.col - 1) + "^"
        return head


class VarLoc:
    __slots__ = ("kind", "val", "ty", "borrowed", "sym", "is_ptr")

    def __init__(self, kind, val, ty, borrowed=False, sym=None, is_ptr=False):
        self.kind = kind        # 'temp' | 'mem'
        self.val = val
        self.ty = ty
        self.borrowed = borrowed
        self.sym = sym
        self.is_ptr = is_ptr    # True: 值本身就是地址（如 self 指针）


class ScopeCtx:
    def __init__(self, parent=None):
        self.vars: dict = {}
        self.drops: List[Tuple[VarLoc, Any]] = []
        self.defers: List[Expr] = []
        self.parent = parent

    def lookup(self, name):
        s = self
        while s:
            if name in s.vars:
                return s.vars[name]
            s = s.parent
        return None


def is_agg(ty: Type) -> bool:
    return ty.kind in ("struct", "arr", "enum")


def vec_esz(ty: Type) -> int:
    """Vec 元素的实际存储宽度（字节）。

    整数 / bool / char 的窄类型紧凑存放（1/2/4 字节），省内存也省带宽；
    浮点与结构体保持 8 字节，避免动到 BITCAST 与装箱逻辑。
    """
    if ty is not None and ty.kind in ("int", "bool", "char") and ty.size in (1, 2, 4):
        return ty.size
    return 8


def elem_kind(ty: Type, sema) -> int:
    """容器元素/映射键值的运行时 kind 编码"""
    if ty is None:
        return K_NONE
    if ty.kind == "str":
        return K_STR
    if ty.kind == "vec":
        return K_VEC
    if ty.kind == "map":
        return K_MAP
    if ty.kind == "pyobj":
        return K_PY
    if ty.kind == "jobj":
        return K_JOBJ
    if ty.kind == "struct":
        # 容器里的结构体元素**一律装箱**（存的是 malloc 出来的副本地址）：
        # 以前 size<=8 的结构体被按值塞进 8 字节槽里，运行时却按
        # 「指向结构体的指针」去 retain/release，于是把字符串指针当成结构体头用。
        if ty.desc_id >= 0:
            return K_BOXED_STRUCT + ty.desc_id
        return K_BOX                  # 装箱的纯数据结构体（无内部引用）
    if ty.kind == "enum":
        if ty.desc_id >= 0:
            return K_BOXED_STRUCT + ty.desc_id
        return K_BOX
    return K_NONE


class FnGen:
    def __init__(self, sema: Sema, mod: IRModule, fnsym: FnSym,
                 body: Block, params: List[Param], self_type: Optional[str] = None):
        self.sema = sema
        self.mod = mod
        self.fnsym = fnsym
        self.body = body
        self.params = params
        self.self_type = self_type
        self.ir: List[Instr] = []
        self.ntemp = 0
        self.nlabel = 0
        self.fn: Optional[IRFunc] = None
        self.scope: Optional[ScopeCtx] = None
        self.owned: List[Tuple[Temp, Type]] = []     # 语句内产生的 owned 临时引用
        self.owned_ids: set = set()
        # 语句内新建的「聚合临时值」（结构体/数组字面量、返回聚合的调用结果）。
        # 它们的存储是栈上的 alloca，语句结束时若没被谁接管就必须就地释放字段。
        self.agg_owned: List[Tuple[Temp, Type]] = []
        self.agg_owned_ids: set = set()
        # (continue 标签, break 标签, continue 要收尾到哪个作用域为止,
        #  break 要收尾到哪个作用域为止)。后两个是「停在这层之外」的意思：
        #  break / continue 会跳出若干层块，被跳过的那些块里的 defer 与引用
        #  释放必须在跳转之前就地补一份（与 return 的 unwind_scopes 同理）。
        self.loop_stack: List[Tuple[str, str, object, object]] = []
        self.temp_tys: dict = {}
        # 目标驱动代码生成：调用方（赋值/let）可以把「结果该写到哪个 Temp」作为提示传进来，
        # 让 x = x + 1 直接生成 add 而不是「算到临时寄存器再搬回去」。
        # 只有最外层表达式节点能取走提示；进入任何子表达式前必须先清空。
        self.hint: Optional[Temp] = None

    # ------------------------------------------------------------ 基础设施
    def err(self, msg, node=None):
        raise FaCodegenError(msg, getattr(node, "line", 0), getattr(node, "col", 0))

    def new_temp(self, ty: Type = None) -> Temp:
        self.ntemp += 1
        t = Temp(self.ntemp, ty)
        self.temp_tys[t.id] = ty
        return t

    def new_label(self, p="L") -> str:
        self.nlabel += 1
        return f".{self.fn.name}_{p}{self.nlabel}"

    def dst_or_new(self, ty: Type) -> Temp:
        """取走目标提示（若类型完全匹配），否则新建临时变量。

        只用于「先把所有操作数算完、最后才写目标」的指令形态
        （BIN / UN / CALL / LOAD）。调用前必须保证子表达式已经求值完毕。
        """
        h = self.hint
        self.hint = None
        if (h is not None and ty is not None and h.ty == ty
                and not is_agg(ty) and not T.t_is_refcounted(ty)):
            return h
        return self.new_temp(ty)

    def take_hint(self) -> Optional[Temp]:
        """取出并清空提示（用于「本节点不用提示」的分支，避免泄漏给子表达式）"""
        h, self.hint = self.hint, None
        return h

    def hint_or_new(self, h: Optional[Temp], ty: Type) -> Temp:
        """手上有提示且类型完全匹配就用提示，否则新建临时变量"""
        if (h is not None and ty is not None and h.ty == ty
                and not is_agg(ty) and not T.t_is_refcounted(ty)):
            return h
        return self.new_temp(ty)

    def emit(self, op, dst=None, args=None, extra=None, ty=None, line=0) -> Instr:
        ins = Instr(op, dst, args, extra, ty, line)
        self.ir.append(ins)
        return ins

    def push_scope(self):
        self.scope = ScopeCtx(self.scope)
        return self.scope

    def pop_scope(self):
        sc = self.scope
        # defer 先执行（后进先出）
        for d in reversed(sc.defers):
            self.gen_deferred(d)
        # 再释放本作用域拥有的引用
        for loc, ty in reversed(sc.drops):
            self.emit_drop(loc, ty)
        self.scope = sc.parent

    # ------------------------------------------------------------ 入口
    def gen(self) -> IRFunc:
        ret = self.fnsym.ret
        sret = is_agg(ret)
        ptemps = []
        if sret:
            p = self.new_temp(ptr_to(ret))
            p.fixed = "rdi"
            ptemps.append(p)
        # 方法：self 指针占用第一个整数寄存器（sret 时退到 rsi）
        self_temp = None
        if self.self_type is not None:
            st = (self.sema.structs.get(self.self_type)
                  or self.sema.enums.get(self.self_type))
            self_temp = self.new_temp(ptr_to(st) if st else ptr_to(I64))
            self_temp.fixed = "rsi" if sret else "rdi"
            ptemps.append(self_temp)
        for i, pty in enumerate(self.fnsym.params):
            t = self.new_temp(pty)
            ptemps.append(t)
        # 前 6 个整数参数寄存器：sret 指针占 rdi，self 再占下一个。
        # 两者**同时**存在时（方法返回结构体）要各让一个位置——
        # 以前只让了一个，于是 `fn clone(self, k: i64) -> P` 里的 k
        # 和 self 都被分到 rsi，k 实际收到的是 sret 指针（一个栈地址）。
        ireg = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
        if sret:
            ireg = ireg[1:]
        if self_temp is not None:
            ireg = ireg[1:]
        # 按 SysV 规则逐个形参分配：整数用 ireg，浮点用 xmm0-xmm7，
        # 放不下的按原顺序进栈槽（槽号从 0 开始，即 [rbp+16+8*slot]）
        nint = nfp = nstack = 0
        for t in ptemps:
            if t is self_temp or (sret and t is ptemps[0]):
                continue                      # sret 指针 / self 已固定
            if t.ty is not None and t.ty.is_float:
                if nfp < 8:
                    t.fixed = f"xmm{nfp}"
                else:
                    t.fixed = f"stack:{nstack}"
                    nstack += 1
                nfp += 1
            else:
                if nint < len(ireg):
                    t.fixed = ireg[nint]
                else:
                    t.fixed = f"stack:{nstack}"
                    nstack += 1
                nint += 1
        self.fn = IRFunc(self.fnsym.symbol, ptemps, ret, sret=sret)
        self.fn.varargs = self.fnsym.varargs
        self.fn.extern = self.fnsym.extern

        sc = self.push_scope()
        # self / 形参落地
        off = 0
        if self.self_type is not None:
            st = self.sema.structs.get(self.self_type) or self.sema.enums.get(self.self_type)
            loc = VarLoc("temp", ptemps[1] if sret else ptemps[0], st,
                         borrowed=True, is_ptr=True)   # self 本身是指针
            sc.vars["self"] = loc
            off = 1
        for i, p in enumerate(self.params):
            if p.name == "self":
                continue                       # self 已在上面绑定
            pty = self.fnsym.params[i if off == 0 else i - 1]
            # ptemps 的排布是 [sret?] + [self?] + 其余形参，而 i 是**AST 形参**
            # 的下标（方法里含 self），所以偏移正好是「有 sret 就 +1」——
            # 以前这里多加了一个 off，凡是带参数的方法（self 之外还有形参）
            # 都会越界，直接 IndexError 崩掉编译器：
            #     impl P: fn add(self, k: i64) -> i64   # docs/02 里的标准写法
            src = ptemps[i + (1 if sret else 0)]
            sym = VarSym(p.name, pty, mutable=True, is_param=True)
            if is_agg(pty) or sym.addr_taken:
                slot = self.emit_alloca(pty.size)
                self.emit_copy(slot, src if is_agg(pty) else src, pty)
                loc = VarLoc("mem", slot, pty, borrowed=True, sym=sym)
            else:
                loc = VarLoc("temp", src, pty, borrowed=True, sym=sym)
            sc.vars[p.name] = loc

        if self.fnsym.name == "main" and getattr(self.sema, "global_decls", None):
            # 顶层 let 的初值：语义上「在 main 之前执行」，实现上放在 main 最前面
            self.gen_global_inits()
        self.gen_block(self.body)

        # 函数体结尾兜底 return
        if not self.ir or self.ir[-1].op != "RET":
            if ret.kind == "void":
                self.emit("RET")
            else:
                z = self.const_zero(ret)
                self.emit("RET", args=[z], ty=ret)
        self.pop_scope()
        self.fn.instrs = self.ir
        return self.fn

    # ------------------------------------------------------------ 内存辅助
    def emit_alloca(self, size: int, align: int = 8) -> Temp:
        d = self.new_temp(ptr_to(U8))
        self.emit("ALLOCA", d, extra=(max(size, 1), align))
        return d

    def add_off(self, off, delta: int):
        """把常量偏移叠加到「可能是 Temp 的偏移」上。

        `a[0].id` 这类嵌套取址会得到一个运行时才算得出来的偏移（Temp），
        以前直接 `off0 + fo` -> Python 层 TypeError（Temp + int），编译器当场崩掉。
        """
        if not delta:
            return off
        if isinstance(off, int):
            return off + delta
        r = self.new_temp(I64)
        self.emit("BIN", r, [off, self.const(delta)], extra="+", ty=I64)
        return r

    def emit_map_set(self, m, kexpr, vexpr, kt: Type, vt: Type):
        """`m.set(k, v)` 与 Map 字面量初值共用的一条路径。

        fa_map_set 内部完成「新键值 inc + 旧键值 dec」，所以这里不再动引用计数；
        键值都按容器的 uint64_t ABI 传：浮点转位模式，结构体/枚举先装箱
        （否则存进去的是栈地址，函数一返回就成了野指针）。
        """
        k = self.gen_expr(kexpr)
        v = self.gen_expr(vexpr)
        kk = self.coerce(k, kexpr.ty, kt)
        if kt.is_float:
            kk = self.bitcast(kk, I64)
        if vt.kind in ("struct", "enum"):
            vv = self.box_agg(v, vt)
        else:
            vv = self.coerce(v, vexpr.ty, vt)
            if vt.is_float:
                vv = self.bitcast(vv, I64)
        self.emit("CALL", None, [Sym("fa_map_set"), m, kk, vv])

    def box_agg(self, v, ty: Type):
        """为容器元素在堆上装一份箱。

        **不在这里 retain**：fa_vec_push / fa_map_set 会按元素 kind 调用
        __fa_retain_<T> 给容器加引用；源若是本语句的临时值，语句结束时释放。
        """
        box = self.new_temp(ptr_to(ty))
        self.emit("CALL", box, [Sym("fa_alloc"), self.const(max(ty.size, 8))],
                  ty=ptr_to(ty))
        self.emit("MEMCPY", args=[box, v], extra=ty.size, ty=ty)
        return box

    def const(self, v, ty: Type = None) -> Const:
        return Const(v, ty or I64)

    def const_zero(self, ty: Type):
        if ty.is_float:
            return Const(0.0, ty)
        return Const(0, ty or I64)

    def emit_copy(self, dstptr: Temp, srcptr: Temp, ty: Type):
        """按字节复制聚合值"""
        self.emit("MEMCPY", args=[dstptr, srcptr], extra=ty.size, ty=ty)

    # ------------------------------------------------------------ 引用计数
    def mark_owned(self, v, ty: Type):
        """登记一个「本语句拥有的」引用；同一个临时值只登记一次（防重复释放）。"""
        if isinstance(v, Temp) and T.t_is_refcounted(ty):
            if v.id in self.owned_ids:
                return
            self.owned_ids.add(v.id)
            self.owned.append((v, ty))

    def take_owned(self, v, ty: Type) -> bool:
        """若 v 是本语句拥有的临时值，则接管其所有权（不再在语句末尾释放）。"""
        if isinstance(v, Temp) and v.id in self.owned_ids:
            self.owned = [x for x in self.owned if x[0].id != v.id]
            self.owned_ids.discard(v.id)
            return True
        return False

    def emit_rcinc(self, v, ty: Type):
        """给一个值加一次引用。

        结构体没有引用计数头，加引用 = 调用编译器为它生成的 __fa_retain_<T>
        （逐字段各加一次）。以前这里调用的是 __fa_drop_inc_<T> —— 一个
        **从未被生成**的符号，于是 `r1 = r2` 这种结构体赋值直接链接失败。
        """
        k = ty.rc_kind
        if k != K_NONE:
            self.emit("RCINC", args=[v], extra=k)
        elif ty.kind in ("struct", "enum") and ty.is_refcounted:
            self.emit("CALL", None, [Sym(f"__fa_retain_{ty.name}"), v])
        elif ty.kind == "arr" and T.t_is_refcounted(ty.elem):
            self.emit_array_rc(v, ty, retain=True)

    def emit_drop(self, loc: VarLoc, ty: Type):
        if not T.t_is_refcounted(ty):
            return
        if loc.kind == "temp":
            self.emit_rcdec_val(loc.val, ty)
        else:
            if ty.kind in ("str", "vec", "map", "pyobj", "jobj"):
                v = self.new_temp(ty)
                self.emit("LOAD", v, [loc.val], extra=0, ty=ty)
                self.emit_rcdec_val(v, ty)
            else:
                self.emit_rcdec_val(loc.val, ty)

    def emit_rcdec_val(self, v, ty: Type):
        k = ty.rc_kind
        if k != K_NONE:
            self.emit("RCDEC", args=[v], extra=k)
        elif ty.kind == "struct":
            self.emit("CALL", None, [Sym(f"__fa_drop_{ty.name}"), v])
        elif ty.kind == "arr":
            self.emit_array_rc(v, ty, retain=False)
        elif ty.kind == "enum" and ty.is_refcounted:
            self.emit("CALL", None, [Sym(f"__fa_drop_{ty.name}"), v])

    def emit_array_rc(self, ptr: Temp, ty: Type, retain: bool):
        """定长数组的批量增减引用。

        元素是**内联**存放的，所以结构体元素要对「元素地址」调用
        __fa_drop_/__fa_retain_<T>，而不是像以前那样先 LOAD 8 字节再处理
        （那对 16 字节以上的结构体元素完全是错的）。统一交给运行时
        fa_drop_arr / fa_retain_arr 循环，省得在 IR 里手写循环。
        """
        et = ty.elem
        if not T.t_is_refcounted(et):
            return
        nested = et.kind in ("struct", "enum") and et.is_refcounted
        if nested:
            fn = self.new_temp(ptr_to(VOID))
            self.emit("LEA_SYM", fn,
                      extra=f"__fa_{'retain' if retain else 'drop'}_{et.name}")
        else:
            fn = self.const(0)
        sym = "fa_retain_arr" if retain else "fa_drop_arr"
        self.emit("CALL", None, [Sym(sym), ptr, self.const(ty.count),
                                 self.const(max(et.size, 1)),
                                 self.const(0 if nested else et.rc_kind), fn])

    # ------------------------------------------------- 聚合值的所有权
    def mark_agg_owned(self, slot, ty: Type):
        """登记一个本语句新建的聚合临时值（结构体/数组字面量、返回聚合的调用）"""
        if isinstance(slot, Temp) and is_agg(ty) and T.t_is_refcounted(ty):
            if slot.id not in self.agg_owned_ids:
                self.agg_owned_ids.add(slot.id)
                self.agg_owned.append((slot, ty))

    def take_agg_owned(self, slot) -> bool:
        """若 slot 是本语句新建的临时值，接管它（源不再释放，所有权直接转移）"""
        if isinstance(slot, Temp) and slot.id in self.agg_owned_ids:
            self.agg_owned = [x for x in self.agg_owned if x[0].id != slot.id]
            self.agg_owned_ids.discard(slot.id)
            return True
        return False

    def emit_init_agg(self, dst, src, ty: Type):
        """把聚合值放进一个**新的**归属地（变量槽 / sret 缓冲 / 结构体字段）。

        拷贝语义：逐字段各自加一次引用，两份值才能独立释放。
        源若是本语句刚创建的临时值，所有权直接转移（不再多加一次）。
        """
        self.emit("MEMCPY", args=[dst, src], extra=ty.size, ty=ty)
        if not self.take_agg_owned(src) and T.t_is_refcounted(ty):
            self.emit_rcinc(dst, ty)

    def emit_assign_agg(self, dst, src, ty: Type):
        """覆盖一个**已经拥有值**的归属地：先给新值加引用，再释放旧值，最后拷贝。
        （顺序很重要：`r.tag = r.tag` 这类自赋值、以及新旧值共享子对象时才不会误删。）"""
        fresh = self.take_agg_owned(src)
        if not fresh and T.t_is_refcounted(ty):
            self.emit_rcinc(src, ty)
        if T.t_is_refcounted(ty):
            self.emit_rcdec_val(dst, ty)
        self.emit("MEMCPY", args=[dst, src], extra=ty.size, ty=ty)

    def flush_owned(self):
        for v, ty in self.owned:
            self.emit_rcdec_val(v, ty)
        self.owned.clear()
        self.owned_ids.clear()
        # 聚合临时值：没被 let / 赋值 / return 接管的，就在这里释放
        # （例如 `print(mk(3).id)` 里那个用完就扔的结构体）
        for slot, ty in self.agg_owned:
            self.emit_rcdec_val(slot, ty)
        self.agg_owned.clear()
        self.agg_owned_ids.clear()

    def flush_owned_since(self, ref_before: set, agg_before: set):
        """释放「快照之后」新登记的 owned 临时引用。

        defer 的实参（插值出来的字符串、方法调用的结果……）在 defer 真正执行时
        才求值，而那时已经不在任何语句的收尾流程里，没人替它释放 —— 循环里写
        `defer write("x{i} ")` 每轮就漏两个 FaStr（str(i) 与拼接结果）。
        只释放快照之后新增的部分：快照里那些可能还被外层表达式拿着。
        """
        for v, ty in list(self.owned):
            if v.id not in ref_before:
                self.emit_rcdec_val(v, ty)
        self.owned = [x for x in self.owned if x[0].id in ref_before]
        self.owned_ids = {x[0].id for x in self.owned}
        for slot, ty in list(self.agg_owned):
            if slot.id not in agg_before:
                self.emit_rcdec_val(slot, ty)
        self.agg_owned = [x for x in self.agg_owned if x[0].id in agg_before]
        self.agg_owned_ids = {x[0].id for x in self.agg_owned}

    def gen_deferred(self, d):
        """执行一条 defer：求值 -> 释放它自己产生的临时引用"""
        ref_before = set(self.owned_ids)
        agg_before = set(self.agg_owned_ids)
        self.gen_expr(d)
        self.flush_owned_since(ref_before, agg_before)

    def unwind_to(self, stop_scope):
        """把 stop_scope **之内**的作用域按 defer + 引用释放收尾（不清空登记表）。

        break / continue 会直接跳走，被跳过的那些块里的 `defer` 和局部引用
        就永远不会执行/释放了：`for i in 0..3 { defer write("d") ; break }`
        的 defer 不跑，`while ... { let s = "a"+"b"; break }` 每轮漏一个 FaStr。
        这里就地补一份清理指令再跳转 —— 和 return 走的 unwind_scopes 是同一套
        道理；正常路径那份清理代码仍在原地，两条路径各自只执行一次。
        """
        sc = self.scope
        while sc is not None and sc is not stop_scope:
            for d in reversed(sc.defers):
                self.gen_deferred(d)
            for loc, ty in reversed(sc.drops):
                self.emit_drop(loc, ty)
            sc = sc.parent

    def unwind_scopes(self):
        """`return` 之前，把当前仍然打开的所有作用域的 defer 与引用释放补上。

        以前 return 直接发 RET，导致：
          * `defer` 只在「函数体自然结束」时执行，写了 return 就永远不执行；
          * 局部引用（str / Vec / 含引用字段的结构体）一个都不释放 -> 每次调用都泄漏。
        这里**不清空**作用域列表：正常路径仍由 pop_scope 收尾，
        而 return 路径已经离开函数，两处代码不会同时执行。
        """
        sc = self.scope
        while sc is not None:
            for d in reversed(sc.defers):
                self.gen_deferred(d)
            for loc, ty in reversed(sc.drops):
                self.emit_drop(loc, ty)
            sc = sc.parent

    # ------------------------------------------------------------ 语句
    def gen_block(self, b: Block):
        sc = self.push_scope()
        for s in b.stmts:
            self.gen_stmt(s)
        self.pop_scope()

    def gen_stmt(self, s: Stmt):
        # 注意：**不要**在这里重置 self.owned。
        # 复合语句（if/while/for）会先求值条件再递归进入子语句，
        # 子语句开头的重置会把条件里产生的临时引用直接丢掉（既不释放也不转移）。
        if isinstance(s, Block):
            if getattr(s, "flat", False):
                for x in s.stmts:
                    self.gen_stmt(x)
            else:
                self.gen_block(s)
        elif isinstance(s, Let):
            self.gen_let(s)
        elif isinstance(s, Assign):
            self.gen_assign(s)
        elif isinstance(s, Return):
            self.gen_return(s)
        elif isinstance(s, If):
            self.gen_if(s)
        elif isinstance(s, While):
            self.gen_while(s)
        elif isinstance(s, For):
            self.gen_for(s)
        elif isinstance(s, ForC):
            self.gen_for_c(s)
        elif isinstance(s, Loop):
            self.gen_loop(s)
        elif isinstance(s, Break):
            if not self.loop_stack:
                self.err("break 不在循环内", s)
            self.unwind_to(self.loop_stack[-1][3])
            self.emit("JMP", extra=self.loop_stack[-1][1])
        elif isinstance(s, Continue):
            if not self.loop_stack:
                self.err("continue 不在循环内", s)
            self.unwind_to(self.loop_stack[-1][2])
            self.emit("JMP", extra=self.loop_stack[-1][0])
        elif isinstance(s, Defer):
            self.scope.defers.append(s.call)
        elif isinstance(s, FnDef):
            pass                    # 嵌套函数已被提升，由 generate() 单独生成
        elif isinstance(s, ExprStmt):
            self.gen_expr(s.expr)
        elif isinstance(s, Match):
            self.gen_match(s)
        elif isinstance(s, Asm):
            self.emit("ASM", extra=s.code)
        else:
            self.err(f"未支持语句 {type(s).__name__}", s)
        self.flush_owned()

    # ------------------------------------------------- 顶层 let（全局可变变量）
    def is_global_name(self, name: str) -> bool:
        """这个名字此刻指的是全局变量吗？（局部/形参同名时局部优先）"""
        return name in self.sema.globals and self.scope.lookup(name) is None

    def global_addr(self, name: str) -> Temp:
        """取全局槽的地址（rip 相对），地位相当于局部变量的 alloca 槽。"""
        g = self.sema.globals[name]
        t = self.new_temp(ptr_to(g.ty))
        self.emit("LEA_SYM", t, extra=g.label)
        return t

    def gen_global_read(self, name: str):
        """读全局：和读局部变量一样给出**借用**引用（需要自己一份的调用方
        ——`let s = g` / `v.push(g)`—— 会各自 inc，规则完全一致）。"""
        gt = self.sema.globals[name].ty
        addr = self.global_addr(name)
        if is_agg(gt):
            return addr                      # 数组：值就是那块存储本身
        t = self.new_temp(gt)
        self.emit("LOAD", t, [addr], extra=0, ty=gt)
        return t

    def gen_global_write(self, name: str, vexpr):
        """写全局：引用计数类型要「新值取得一份 -> 放掉旧值 -> 存进去」。"""
        gt = self.sema.globals[name].ty
        raw = self.gen_expr(vexpr)
        addr = self.global_addr(name)
        if is_agg(gt):
            self.emit_assign_agg(addr, raw, gt)
            return
        v = self.coerce(raw, vexpr.ty, gt)
        if T.t_is_refcounted(gt):
            if not self.take_owned(v, vexpr.ty):
                self.emit_rcinc(v, gt)       # 全局取得自己的一份（活到进程结束）
            old = self.new_temp(gt)
            self.emit("LOAD", old, [addr], extra=0, ty=gt)
            self.emit_rcdec_val(old, gt)     # 放掉被覆盖的旧值
        self.emit("STORE", args=[addr, v], extra=0, ty=gt)

    def gen_global_inits(self):
        """main 的第一条用户语句之前，把每个顶层 let 的初值算一遍。

        没有初值的（`let n: i64`）不用管：槽在 .bss 里，天然就是零值。
        """
        for d in getattr(self.sema, "global_decls", []):
            if d.init is None:
                continue
            self.gen_global_write(d.name, d.init)

    def gen_let(self, s: Let):
        ty = s.sym.ty
        if s.init is None:
            if is_agg(ty):
                slot = self.emit_alloca(ty.size)
                self.emit("ZERO", args=[slot], extra=ty.size)
                loc = VarLoc("mem", slot, ty, sym=s.sym)
            else:
                t = self.new_temp(ty)
                self.emit("MOV", t, [self.const_zero(ty)], ty=ty)
                loc = VarLoc("temp", t, ty, sym=s.sym)
            self.scope.vars[s.name] = loc
            self.scope.drops.append((loc, ty))
            return
        if is_agg(ty):
            v = self.gen_expr(s.init)
            slot = self.emit_alloca(ty.size)
            self.emit_init_agg(slot, v, ty)
            loc = VarLoc("mem", slot, ty, sym=s.sym)
        else:
            t = self.new_temp(ty)
            if not T.t_is_refcounted(ty) and s.init.ty == ty:
                # 目标驱动：让初值表达式直接算进变量自己的寄存器
                self.hint = t
                v = self.gen_expr(s.init)
                self.hint = None
                if v is not t:
                    self.emit("MOV", t, [v], ty=ty)
                loc = VarLoc("temp", t, ty, sym=s.sym)
                self.scope.vars[s.name] = loc
                self.scope.drops.append((loc, ty))
                return
            v = self.gen_expr(s.init)
            self.emit("MOV", t, [self.coerce(v, s.init.ty, ty)], ty=ty)
            loc = VarLoc("temp", t, ty, sym=s.sym)
            if T.t_is_refcounted(ty):
                # 若初值就是本语句新建的临时引用，直接接管所有权；
                # 否则（来自变量/字段等「借用」来源）需要 rc_inc 取得自己的一份。
                if not self.take_owned(v, s.init.ty):
                    self.emit_rcinc(v, s.init.ty)
        self.scope.vars[s.name] = loc
        self.scope.drops.append((loc, ty))

    def gen_assign(self, s: Assign):
        tgt = s.target
        vty = s.value.ty
        if isinstance(tgt, NameRef) and self.is_global_name(tgt.name):
            self.gen_global_write(tgt.name, s.value)
            return
        if isinstance(tgt, NameRef):
            loc = self.scope.lookup(tgt.name)
            if loc is None:
                self.err(f"未定义变量 '{tgt.name}'", s)
            # 目标驱动：标量、非引用计数、类型完全匹配时，让右值直接算进变量自己的寄存器，
            # 省掉「算到新临时寄存器再搬回去」的那条 MOV（循环里每次迭代都能省 1~2 条）。
            use_hint = (loc.kind == "temp" and not is_agg(loc.ty)
                        and not T.t_is_refcounted(loc.ty) and loc.ty == vty)
            if use_hint:
                self.hint = loc.val
            raw = self.gen_expr(s.value)
            self.hint = None
            if use_hint:
                if raw is not loc.val:
                    self.emit("MOV", loc.val, [raw], ty=loc.ty)
                return
            if is_agg(loc.ty) and loc.kind == "mem":
                # 结构体/数组变量赋值：必须「加新值引用 -> 放旧值 -> 拷贝」，
                # 少了第一步就是双重释放（实测 free(): double free detected）。
                self.emit_assign_agg(loc.val, raw, loc.ty)
                return
            v = self.coerce(raw, vty, loc.ty)
            if T.t_is_refcounted(loc.ty):
                self.emit_rcinc(v, loc.ty)
                if loc.kind == "temp":
                    self.emit_rcdec_val(loc.val, loc.ty)
                else:
                    old = self.new_temp(loc.ty)
                    self.emit("LOAD", old, [loc.val], extra=0, ty=loc.ty)
                    self.emit_rcdec_val(old, loc.ty)
            if loc.kind == "temp":
                self.emit("MOV", loc.val, [v], ty=loc.ty)
            else:
                self.emit("STORE", args=[loc.val, v], extra=0, ty=loc.ty)
            return
        # 复合左值：字段 / 下标 / 解引用
        if isinstance(tgt, Index) and tgt.obj.ty is not None \
                and tgt.obj.ty.kind in ("vec", "map"):
            # v[i] = x / m[k] = v：走运行时（要维护引用计数），不能当普通内存写
            self.gen_subscript_write(tgt, s.value)
            return
        ptr, off, fty = self.gen_addr(tgt)
        v = self.coerce(self.gen_expr(s.value), vty, fty)
        if is_agg(fty):
            base = self.new_temp(ptr_to(fty))
            self.emit("LEA", base, [ptr], extra=off)
            self.emit_assign_agg(base, v, fty)
            return
        if T.t_is_refcounted(fty):
            self.emit_rcinc(v, fty)
            old = self.new_temp(fty)
            self.emit("LOAD", old, [ptr], extra=off, ty=fty)
            self.emit_rcdec_val(old, fty)
        self.emit("STORE", args=[ptr, v], extra=off, ty=fty)

    def gen_return(self, s: Return):
        ret = self.fnsym.ret
        if s.value is None:
            self.flush_owned()
            self.unwind_scopes()
            self.emit("RET")
            return
        v = self.gen_expr(s.value)
        if is_agg(ret):
            # 写进调用方给的 sret 缓冲；调用方从此独立拥有这份值
            self.emit_init_agg(self.fn.params[0], v, ret)
            self.flush_owned()
            self.unwind_scopes()
            self.emit("RET", args=[self.fn.params[0]])
            return
        v = self.coerce(v, s.value.ty, ret)
        if T.t_is_refcounted(ret):
            self.emit_rcinc(v, s.value.ty)     # 返回值 owned 转移给调用方
        # 顺序：先给调用方加好引用 -> 释放本语句临时值 -> 执行 defer 与局部释放 -> 返回。
        # （以前 RET 之后才发这些指令，等于全是死代码：defer 不执行、局部引用全泄漏。）
        self.flush_owned()
        self.unwind_scopes()
        self.emit("RET", args=[v], ty=ret)
        return

    def gen_if(self, s: If):
        pairs = [(s.cond, s.body)] + list(s.elifs)
        end = self.new_label("ifend")
        self.gen_if_chain(pairs, s.orelse, end)

    def gen_if_chain(self, pairs, orelse, end, dst=None):
        """dst 不为空时是「if 作为表达式」：每个分支把尾表达式的值写进 dst。"""
        for i, (cond, body) in enumerate(pairs):
            c = self.gen_cond(cond)
            cur = self.new_label("ifbody")
            nxt = self.new_label("elif")
            self.emit("BR", args=[c], extra=(cur, nxt))
            self.emit("LABEL", extra=cur)
            self.gen_block_value(body, dst) if dst is not None else self.gen_block(body)
            self.emit("JMP", extra=end)
            self.emit("LABEL", extra=nxt)
        if orelse is not None:
            self.gen_block_value(orelse, dst) if dst is not None \
                else self.gen_block(orelse)
        self.emit("LABEL", extra=end)

    def _value_slot(self, ty: Type):
        """if / match 表达式的结果归属地：标量用寄存器临时值，聚合用栈槽。"""
        hint = self.take_hint()
        if is_agg(ty):
            slot = self.emit_alloca(ty.size)
            self.mark_agg_owned(slot, ty)
            return slot
        return self.hint_or_new(hint, ty)

    def gen_if_value(self, e: If):
        """`let x = if c { a } else { b }` —— 结果统一放进一个临时变量/栈槽。"""
        r = self._value_slot(e.ty)
        end = self.new_label("ifend")
        self.gen_if_chain([(e.cond, e.body)] + list(e.elifs), e.orelse, end, dst=r)
        return r

    def gen_block_value(self, b: Block, dst):
        """执行分支里的语句，把最后一条表达式语句的值放进 dst。

        引用计数类型的所有权照 `gen_let` 的规则处理：分支里新建的临时引用直接
        交给 dst（`take_owned`），来自变量/字面量的则 rc_inc 一份 —— 这样
        `let s = if c { "a" } else { other }` 既不会漏放也不会双放。
        """
        if getattr(b, "diverges", False):
            self.gen_block(b)          # return / break / panic：不产出值
            return
        # 进分支前先记下「已经存在的」临时引用：它们由外层语句负责释放
        # （条件表达式产生的引用两条分支都可能用到，不能在分支里放掉）
        ref_before = set(self.owned_ids)
        agg_before = set(self.agg_owned_ids)
        self.push_scope()
        # 三条产出路径（值就是 dst / 聚合 / 标量）都要在**分支内**收尾，
        # 所以用 try...finally：漏掉任何一条，分支里的中间引用就会跑到分支外
        # 才释放，另一条分支执行时那些寄存器装的是无关的值。
        try:
            stmts = [x for x in b.stmts if x is not None]
            for st in stmts[:-1]:
                self.gen_stmt(st)
            tail = stmts[-1]
            if isinstance(tail, ExprStmt):
                v, sty = self.gen_expr(tail.expr), tail.expr.ty
            else:
                # 嵌套的 if / match 表达式：尽量让它直接算进 dst
                self.hint = dst
                v = self.gen_expr(tail)
                self.hint = None
                sty = tail.ty
            if v is dst:
                return
            if is_agg(sty):
                # 聚合结果：分支里新建的值直接把所有权移交给 dst（emit_init_agg
                # 内部会 take_agg_owned），来自变量的则拷一份并逐字段加引用。
                # 判定用的是**尾表达式的类型**：dst 是 emit_alloca 出来的槽，
                # 它自己的 .ty 是 *u8（指向槽的指针），不是聚合类型。
                self.emit_init_agg(dst, v, sty)
                return
            if sty != dst.ty:
                v = self.coerce(v, sty, dst.ty)   # 只可能是数值提升（sema 已统一过）
            if T.t_is_refcounted(dst.ty):
                if not self.take_owned(v, sty):
                    self.emit_rcinc(v, sty)
                self.mark_owned(dst, dst.ty)
            self.emit("MOV", dst, [v], ty=dst.ty)
        finally:
            self.pop_scope()
            self.release_new_refs(dst, ref_before, agg_before)

    def release_new_refs(self, keep, ref_before, agg_before):
        """在**分支内部**释放这个分支自己新建的临时引用（`keep` 除外）。

        `self.owned` / `self.agg_owned` 是**语句级**的：if / match 当表达式用时，
        分支里产生的中间引用（例如 `"n{i}"` 插值过程中的 `str(i)` 与 concat 结果）
        要是留到语句末尾才释放，那时已经在分支外面了 —— 走另一条分支的执行路径上，
        那些寄存器里装的是完全无关的值。实测
        `let t = if i % 2 == 0 { mk(i) } else { P { x: i, name: "n{i}" } }`
        会把循环下标当成对象指针做 rc_dec，直接段错误。

        之前就存在的引用（条件表达式产生的）不动，仍由外层语句释放：
        它们的活跃区间横跨整个 if，寄存器分配器不会在分支里复用。
        """
        for v, ty in list(self.owned):
            if v.id not in ref_before and v is not keep:
                self.emit_rcdec_val(v, ty)
        self.owned = [x for x in self.owned if x[0].id in ref_before or x[0] is keep]
        self.owned_ids = {x[0].id for x in self.owned}
        for slot, ty in list(self.agg_owned):
            if slot.id not in agg_before and slot is not keep:
                self.emit_rcdec_val(slot, ty)
        self.agg_owned = [x for x in self.agg_owned
                          if x[0].id in agg_before or x[0] is keep]
        self.agg_owned_ids = {x[0].id for x in self.agg_owned}

    def gen_for_c(self, s):
        """for (init; cond; step) { body } —— 展开为
            init; goto cond;
            body: { body }
            cont: step;
            cond: if (cond) goto body;
        """
        self.push_scope()
        if s.init is not None:
            self.gen_stmt(s.init)
        cond_lbl = self.new_label("fcond")
        body_lbl = self.new_label("fbody")
        cont_lbl = self.new_label("fcont")
        end_lbl = self.new_label("fend")
        self.emit("JMP", extra=cond_lbl)
        self.emit("LABEL", extra=body_lbl)
        # 这层作用域（装 `let i = 0`）在 end_lbl 之后才 pop，break / continue
        # 都会经过那里，所以清理只做到这层为止，不重复释放
        self.loop_stack.append((cont_lbl, end_lbl, self.scope, self.scope))
        self.gen_block(s.body)
        self.loop_stack.pop()
        self.emit("LABEL", extra=cont_lbl)
        if s.step is not None:
            self.gen_stmt(s.step)
        self.emit("LABEL", extra=cond_lbl)
        if s.cond is not None:
            c = self.gen_cond(s.cond)
            self.emit("BR", args=[c], extra=(body_lbl, end_lbl))
        else:
            self.emit("JMP", extra=body_lbl)
        self.emit("LABEL", extra=end_lbl)
        self.pop_scope()

    def gen_while(self, s: While):
        top = self.new_label("while")
        body = self.new_label("wbody")
        end = self.new_label("wend")
        self.emit("LABEL", extra=top)
        c = self.gen_cond(s.cond)
        self.emit("BR", args=[c], extra=(body, end))
        self.emit("LABEL", extra=body)
        outer = self.scope
        self.loop_stack.append((top, end, outer, outer))
        self.gen_block(s.body)
        self.loop_stack.pop()
        self.emit("JMP", extra=top)
        self.emit("LABEL", extra=end)

    def gen_loop(self, s: Loop):
        top = self.new_label("loop")
        end = self.new_label("loopE")
        self.emit("LABEL", extra=top)
        outer = self.scope
        self.loop_stack.append((top, end, outer, outer))
        self.gen_block(s.body)
        self.loop_stack.pop()
        self.emit("JMP", extra=top)
        self.emit("LABEL", extra=end)

    def gen_for(self, s: For):
        it = s.iter
        ity = it.ty
        top = self.new_label("for")
        body = self.new_label("fbody")
        end = self.new_label("fend")
        idx = self.new_temp(I64)
        limit = self.new_temp(I64)
        vloc = None
        # 可迭代对象只求值**一次**，并在整个循环期间持有它。
        # 以前循环体里会再 gen_expr 一遍：对变量只是浪费，对**函数调用**
        # （`for k in m.keys()`）却是每轮都新建一个容器，而它那条 RCDEC 被
        # 语句级的 flush 放进了循环体 —— 于是每轮都放一次，第二轮就把还在用的
        # 对象释放掉了（实测 malloc(): unaligned tcache chunk detected）。
        obj = None
        arr_ptr = arr_off = None
        held = False
        if isinstance(it, Range) or ity.kind == "range":
            if isinstance(it, Range):
                se, ee = it.start, it.end
            else:
                se, ee = it.left, it.right
            start = self.gen_expr(se) if se is not None else self.const(0)
            stop = self.gen_expr(ee) if ee is not None else self.const(0)
            inc = getattr(it, "inclusive", False) or getattr(it, "op", None) == "..="
            if inc:                       # 0..=n 等价于 0..n+1
                one = self.new_temp(I64)
                self.emit("BIN", one, [self.coerce(stop, I64, I64), self.const(1)],
                          extra="+", ty=I64)
                stop = one
            self.emit("MOV", idx, [self.coerce(start, I64, I64)], ty=I64)
            self.emit("MOV", limit, [self.coerce(stop, I64, I64)], ty=I64)
        else:
            if ity.kind == "arr":
                arr_ptr, arr_off, _ = self.gen_addr(it)
            else:
                obj = self.gen_expr(it)
                # 从语句级所有权表里摘出来，循环结束后由本函数自己释放一次
                held = self.take_owned(obj, ity)
            n = self.new_temp(I64)
            if ity.kind == "vec":
                self.emit("CALL", n, [Sym("fa_vec_len"), obj], ty=I64)
            elif ity.kind == "str":
                self.emit("CALL", n, [Sym("fa_str_len"), obj], ty=I64)
            elif ity.kind == "arr":
                n = self.const(ity.count)
            elif ity.kind == "map":
                self.emit("CALL", n, [Sym("fa_map_len"), obj], ty=I64)
            else:
                self.err(f"暂不支持遍历 {ity}", s)
            self.emit("MOV", idx, [self.const(0)], ty=I64)
            self.emit("MOV", limit, [n], ty=I64)
        self.emit("LABEL", extra=top)
        c = self.new_temp(BOOL)
        self.emit("CMP", c, [idx, limit], extra="<", ty=I64)
        self.emit("BR", args=[c], extra=(body, end))
        self.emit("LABEL", extra=body)
        sc = self.push_scope()
        vty = s.sym.ty
        vt = None
        vloc = None
        if isinstance(it, Range) or ity.kind == "range":
            vt = self.new_temp(vty)
            self.emit("MOV", vt, [idx], ty=vty)
        elif ity.kind == "vec":
            et = ity.elem
            if is_agg(et):
                # 结构体/枚举元素在槽里存的是**装箱指针**，所以循环变量要绑成
                # 「指向该对象的指针」（与 v[i] 一致）。以前绑成一个值类型的临时量，
                # 里面装的其实是指针，`for s in v: print(s.a)` 打出来是地址。
                r = self.new_temp(ptr_to(et))
                self.emit("CALL", r, [Sym("fa_vec_get"), obj, idx], ty=ptr_to(et))
                # fa_vec_get 返回借用引用（未 inc），不能登记为 owned
                vloc = VarLoc("temp", r, et, borrowed=True, is_ptr=True)
            else:
                vt = self.new_temp(vty)
                if et.is_float:
                    # fa_vec_get 返回 uint64_t 位模式（在 rax 里）。以前把 CALL 的
                    # 目标类型直接标成 f64，asmgen 就去读 xmm0 —— `for x in Vec<f64>`
                    # 拿到的全是垃圾（实测 4.94e-324）。
                    raw = self.new_temp(I64)
                    self.emit("CALL", raw, [Sym("fa_vec_get"), obj, idx], ty=I64)
                    self.emit("MOV", vt, [self.bitcast(raw, et)], ty=et)
                else:
                    r = self.new_temp(vty)
                    self.emit("CALL", r, [Sym("fa_vec_get"), obj, idx], ty=vty)
                    self.emit("MOV", vt, [self.coerce(r, vty, vty)], ty=vty)
        elif ity.kind == "str":
            vt = self.new_temp(vty)
            r = self.new_temp(I64)
            self.emit("CALL", r, [Sym("fa_str_byte"), obj, idx], ty=I64)
            self.emit("MOV", vt, [self.coerce(r, I64, vty)], ty=vty)
        elif ity.kind == "map":
            vt = self.new_temp(vty)
            kt = ity.key
            if kt is not None and kt.is_float:
                raw = self.new_temp(I64)
                self.emit("CALL", raw, [Sym("fa_map_key_at"), obj, idx], ty=I64)
                self.emit("MOV", vt, [self.bitcast(raw, kt)], ty=kt)
            else:
                r = self.new_temp(vty)
                self.emit("CALL", r, [Sym("fa_map_key_at"), obj, idx], ty=vty)
                self.emit("MOV", vt, [self.coerce(r, vty, vty)], ty=vty)
        elif ity.kind == "arr":
            ptr, off = arr_ptr, arr_off
            i8 = self.new_temp(I64)
            self.emit("BIN", i8, [idx, self.const(max(ity.elem.size, 1))], extra="*", ty=I64)
            t2 = self.new_temp(I64)
            self.emit("BIN", t2,
                      [i8, self.const(off if isinstance(off, int) else 0)],
                      extra="+", ty=I64)
            if is_agg(vty):
                # 元素是结构体/枚举：拷一份到本次迭代的栈槽（引用计数 +1，
                # 作用域退出时照常释放）。以前直接 LOAD 一个 16 字节的「值」，
                # asmgen 的宽度表里只有 1/2/4/8，编译期就 KeyError: 16。
                base = self.new_temp(ptr_to(vty))
                self.emit("LEA", base, [ptr], extra=t2)
                slot = self.emit_alloca(vty.size)
                self.emit_init_agg(slot, base, vty)
                self.mark_agg_owned(slot, vty)
                vloc = VarLoc("mem", slot, vty)
            else:
                vt = self.new_temp(vty)
                self.emit("LOAD", vt, [ptr], extra=t2, ty=vty)
        if vloc is None:
            vloc = VarLoc("temp", vt, vty)
        sc.vars[s.var] = vloc
        # continue 必须跳到「自增之前」，不能跳到循环头：下标自增是在循环体
        # 之后发的，跳到 top 就等于永远不自增 —— `for k in 0..5 { if k == 2 {
        # continue } }` 会**死循环挂住**（while / C 风格 for / loop 各自都对，
        # 只有 for-in 这条把 continue 目标写成了 top）。
        # 标签放在 pop_scope 之前，这样 continue 与正常走完一轮一样，
        # 都会执行本次迭代的作用域清理（defer / 块内引用）再自增。
        step = self.new_label("fstep")
        self.loop_stack.append((step, end, sc, sc.parent))
        self.gen_block(s.body)
        self.loop_stack.pop()
        self.emit("LABEL", extra=step)
        self.pop_scope()
        self.emit("BIN", idx, [idx, self.const(1)], extra="+", ty=I64)
        self.emit("JMP", extra=top)
        self.emit("LABEL", extra=end)
        if held:
            # 循环之后释放一次（break 也跳到 end，所以每条路径都覆盖到）
            self.emit_rcdec_val(obj, ity)

    def bind_variant_payload(self, subj, pat):
        """把当前变体的载荷绑定成局部变量（每个绑定都拥有自己的一份引用）"""
        for name, off, fty in (getattr(pat, "bindings", None) or []):
            sp = self.new_temp(ptr_to(fty))
            self.emit("LEA", sp, [subj], extra=8 + off)     # 8 = tag 宽度
            if is_agg(fty):
                slot = self.emit_alloca(fty.size)
                self.emit_init_agg(slot, sp, fty)
                loc = VarLoc("mem", slot, fty)
            else:
                t = self.new_temp(fty)
                self.emit("LOAD", t, [sp], extra=0, ty=fty)
                if T.t_is_refcounted(fty):
                    self.emit_rcinc(t, fty)
                loc = VarLoc("temp", t, fty)
            self.scope.vars[name] = loc
            self.scope.drops.append((loc, fty))

    def gen_match(self, s: Match, dst=None):
        subj = self.gen_expr(s.subject)
        sty = s.subject.ty
        end = self.new_label("matchend")
        for arm in s.arms:
            body_lbl = self.new_label("arm")
            skip_lbl = self.new_label("armskip")
            self.push_scope()                       # 载荷绑定只在本分支可见
            if isinstance(arm.pattern, str):        # 通配 _
                pass
            else:
                if sty.kind == "enum" and getattr(arm.pattern, "is_variant", False):
                    tag = self.new_temp(I64)
                    self.emit("LOAD", tag, [subj], extra=0, ty=I64)
                    c = self.new_temp(BOOL)
                    self.emit("CMP", c,
                              [tag, self.const(arm.pattern.variant_index)],
                              extra="==", ty=I64)
                    self.emit("BR", args=[c], extra=(body_lbl, skip_lbl))
                    self.emit("LABEL", extra=body_lbl)
                    self.bind_variant_payload(subj, arm.pattern)
                else:
                    pv = self.gen_expr(arm.pattern)
                    c = self.new_temp(BOOL)
                    if sty == STR:
                        r = self.new_temp(I64)
                        self.emit("CALL", r, [Sym("fa_str_eq"), subj, pv], ty=I64)
                        self.emit("CMP", c, [r, self.const(1)], extra="==", ty=I64)
                    else:
                        self.emit("CMP", c,
                                  [self.coerce(subj, sty, I64),
                                   self.coerce(pv, arm.pattern.ty, I64)],
                                  extra="==", ty=I64)
                    self.emit("BR", args=[c], extra=(body_lbl, skip_lbl))
                    self.emit("LABEL", extra=body_lbl)
            if dst is not None:
                self.gen_block_value(arm.body, dst)
            else:
                self.gen_block(arm.body)
            self.pop_scope()
            self.emit("JMP", extra=end)
            self.emit("LABEL", extra=skip_lbl)      # 不匹配 -> 试下一个分支
        self.emit("LABEL", extra=end)

    def gen_match_value(self, s: Match):
        """`let x = match v { ... }` —— 每个分支把尾表达式的值写进同一个归属地。"""
        r = self._value_slot(s.ty)
        self.gen_match(s, dst=r)
        return r

    # ------------------------------------------------------------ 条件
    def gen_cond(self, e: Expr) -> Temp:
        """生成布尔条件（Temp of BOOL）"""
        v = self.gen_expr(e)
        t = e.ty
        if t.kind == "bool":
            if isinstance(v, Const):
                r = self.new_temp(BOOL)
                self.emit("MOV", r, [v], ty=BOOL)
                return r
            return v
        if t.kind == "int":
            c = self.new_temp(BOOL)
            self.emit("CMP", c, [self.coerce(v, t, I64), self.const(0)], extra="!=", ty=I64)
            return c
        if t.kind == "ptr":
            c = self.new_temp(BOOL)
            self.emit("CMP", c, [v, self.const(0)], extra="!=", ty=I64)
            return c
        self.err(f"类型 {t} 不能作为条件", e)

    # ------------------------------------------------------------ 表达式
    def gen_expr(self, e: Expr):
        if isinstance(e, NumLit):
            k = getattr(e, "kind", "") or ""
            if k.startswith("f"):                    # f32 / f64
                # 标量浮点一律按双精度参与运算（f32 仅用于结构体布局与 C 签名）
                return Const(float(e.value), F64)
            return Const(int(e.value), I64)
        if isinstance(e, CharLit):
            return Const(ord(e.value), CHAR)
        if isinstance(e, BoolLit):
            return Const(1 if e.value else 0, BOOL)
        if isinstance(e, NilLit):
            return Const(0, ptr_to(U8))
        if isinstance(e, StrLit):
            return self.gen_string(e)
        if isinstance(e, NameRef):
            return self.gen_nameref(e)
        if isinstance(e, Binary):
            return self.gen_binary(e)
        if isinstance(e, Unary):
            return self.gen_unary(e)
        if isinstance(e, If):
            return self.gen_if_value(e)
        if isinstance(e, Match):
            return self.gen_match_value(e)
        if isinstance(e, Cast):
            return self.gen_cast(e)
        if isinstance(e, Call):
            return self.gen_call(e)
        if isinstance(e, MethodCall):
            return self.gen_method(e)
        if isinstance(e, Index):
            return self.gen_index(e)
        if isinstance(e, Field):
            return self.gen_field(e)
        if isinstance(e, ArrayLit):
            return self.gen_arraylit(e)
        if isinstance(e, StructLit):
            return self.gen_structlit(e)
        if isinstance(e, AddrOf):
            return self.gen_addrof(e)
        if isinstance(e, Deref):
            v = self.gen_expr(e.operand)
            return self.load_ptr(v, e.ty, 0)
        if isinstance(e, NewExpr):
            return self.gen_new(e)
        if isinstance(e, SizeOf):
            return Const(self.sema.resolve_type(e.operand).size, I64)
        if isinstance(e, Range):
            self.err("range 只能用于 for 循环", e)
        if isinstance(e, Ctor):
            return self.gen_ctor(e)
        if isinstance(e, RawExpr):
            self.emit("ASM", extra=e.code)
            return self.new_temp(ANY)
        self.err(f"未支持表达式 {type(e).__name__}", e)

    def var_loc(self, name: str, node) -> VarLoc:
        loc = self.scope.lookup(name) if self.scope else None
        if loc is None:
            self.err(f"未定义变量 '{name}'", node)
        return loc

    def gen_nameref(self, e: NameRef):
        if self.is_global_name(e.name):
            return self.gen_global_read(e.name)
        if e.name in self.sema.consts:
            return self.gen_expr(self.sema.consts[e.name])
        if e.resolved == "ns":
            return Const(0, I64)
        if isinstance(e.resolved, FnSym):
            t = self.new_temp(ptr_to(U8))
            self.emit("LEA_SYM", t, extra=e.resolved.symbol)
            return t
        loc = self.var_loc(e.name, e)
        if is_agg(loc.ty):
            return loc.val
        if loc.kind == "temp":
            return loc.val
        t = self.new_temp(loc.ty)
        self.emit("LOAD", t, [loc.val], extra=0, ty=loc.ty)
        return t

    def gen_string(self, e: StrLit):
        parts = e.parts
        if not parts:
            return self.make_str("")
        # 首段
        if parts[0][0] == "lit":
            cur = self.make_str(parts[0][1])
            rest = parts[1:]
        else:
            cur = self.make_str("")
            rest = parts
        for kind, val in rest:
            if kind == "lit":
                s2 = self.make_str(val)
            else:
                s2 = self.gen_to_str(self.gen_expr(val), val.ty)
            r = self.new_temp(STR)
            self.emit("CALL", r, [Sym("fa_str_concat"), cur, s2], ty=STR)
            # 只有 fa_str_concat 的返回值是 +1（owned）；
            # cur / s2 可能是静态串或「借用」来的变量，绝不能在这里登记释放。
            self.mark_owned(r, STR)
            cur = r
        return cur

    def make_str(self, s: str) -> Temp:
        idx = self.mod.add_string(s)
        t = self.new_temp(STR)
        self.emit("STRCONST", t, extra=idx, ty=STR)
        return t

    def _is_cstr_ptr(self, ty: Type) -> bool:
        """`*u8` —— C 互操作里的 `char *`。"""
        inner = getattr(ty, "inner", None)
        return (ty is not None and ty.kind == "ptr" and isinstance(inner, Type)
                and inner.kind == "int" and inner.name == "u8")

    def gen_to_str(self, v, ty: Type) -> Temp:
        r = self.new_temp(STR)
        if ty == STR:
            self.emit("MOV", r, [v], ty=STR)
            return r
        if ty.kind == "float":
            self.emit("CALL", r, [Sym("fa_str_of_f64"), self.coerce(v, ty, F64)], ty=STR)
        elif ty.kind == "int":
            self.emit("CALL", r, [Sym("fa_str_of_i64"), self.coerce(v, ty, I64)], ty=STR)
        elif ty.kind == "bool":
            self.emit("CALL", r, [Sym("fa_str_of_bool"), v], ty=STR)
        elif ty == CHAR:
            self.emit("CALL", r, [Sym("fa_str_of_char"), v], ty=STR)
        elif ty.kind == "ptr":
            # *u8 就是 C 的 char*：按 NUL 结尾字符串取内容，而不是打印地址
            # （C 函数返回 const char* 时，这是把文本拿回 FA 的唯一途径）
            if self._is_cstr_ptr(ty):
                self.emit("CALL", r, [Sym("fa_str_from_cstr"), v], ty=STR)
            else:
                self.emit("CALL", r, [Sym("fa_str_of_ptr"), v], ty=STR)
        elif ty.kind == "vec" or ty.kind == "map":
            self.emit("CALL", r, [Sym("fa_container_to_str"), v,
                                  self.const(1 if ty.kind == "vec" else 2)], ty=STR)
        elif ty.kind == "pyobj":
            self.emit("CALL", r, [Sym("fa_py_to_str"), v], ty=STR)
        elif ty.kind == "jobj":
            self.emit("CALL", r, [Sym("fa_jvm_to_str"), v], ty=STR)
        elif ty.kind == "struct":
            return self.gen_struct_to_str(v, ty)
        elif ty.kind == "enum":
            return self.gen_enum_to_str(v, ty)
        elif ty.kind == "arr":
            return self.gen_arr_to_str(v, ty)
        else:
            self.emit("CALL", r, [Sym("fa_str_of_ptr"), v], ty=STR)
        self.mark_owned(r, STR)
        return r

    # ------------------------------------------------- 聚合值的可读化打印
    def concat_str(self, a, b) -> Temp:
        """两个 str 拼起来，返回一份**新的拥有**引用（fa_str_concat 的约定）。"""
        r = self.new_temp(STR)
        self.emit("CALL", r, [Sym("fa_str_concat"), a, b], ty=STR)
        self.mark_owned(r, STR)
        return r

    def load_field_value(self, base, fty: Type, off: int):
        """取某个偏移上的值：标量 LOAD 出来，聚合（结构体/枚举/数组）取它的地址。"""
        if is_agg(fty):
            t = self.new_temp(ptr_to(fty))
            self.emit("LEA", t, [base], extra=off)
            return t
        t = self.new_temp(fty)
        self.emit("LOAD", t, [base], extra=off, ty=fty)
        return t

    def gen_struct_to_str(self, v, ty: Type) -> Temp:
        """`P { x: 1, y: "甲" }`。

        以前结构体/枚举/数组一律走 fa_str_of_ptr —— `print(p)` 打出来是个栈地址
        （0x7ffd…）。初学者写的第一件事往往就是 print 一个结构体，看到地址只会懵，
        而字段名、类型、偏移在编译期全都是已知的，直接展开成字符串拼接就行，
        运行时一行都不用改（嵌套结构体/数组/容器字段会递归下去）。
        """
        cur = self.make_str(f"{ty.name} {{ ")
        for i, (fname, fty, off) in enumerate(ty.fields or []):
            if i:
                cur = self.concat_str(cur, self.make_str(", "))
            cur = self.concat_str(cur, self.make_str(f"{fname}: "))
            cur = self.concat_str(cur, self.gen_to_str(
                self.load_field_value(v, fty, off), fty))
        return self.concat_str(cur, self.make_str(" }"))

    def gen_arr_to_str(self, v, ty: Type) -> Temp:
        """`[1, 2, 3]` —— 格式与运行时的 Vec 打印**逐字对齐**：
        str 元素加双引号、char 元素加单引号，其余原样。
        （不对齐就会出现 `print(Vec<str>["甲"])` 是 `["甲"]`、
        `print(["甲"])` 却是 `[甲]` 这种同一种东西两种样子的尴尬。）"""
        et = ty.elem
        quote = '"' if et == STR else ("'" if et == CHAR else "")
        cur = self.make_str("[")
        for i in range(getattr(ty, "count", 0) or 0):
            if i:
                cur = self.concat_str(cur, self.make_str(", "))
            if quote:
                cur = self.concat_str(cur, self.make_str(quote))
            cur = self.concat_str(cur, self.gen_to_str(
                self.load_field_value(v, et, i * max(et.size, 1)), et))
            if quote:
                cur = self.concat_str(cur, self.make_str(quote))
        return self.concat_str(cur, self.make_str("]"))

    def gen_enum_to_str(self, v, ty: Type) -> Temp:
        """`Circle(r: 2)` / `Dot` —— 按 tag 分支，各变体拼各自的载荷。

        每个分支的中间引用必须**在分支内**释放（release_new_refs）：它们是语句级
        登记的，留到语句末尾就已经在分支外面了，走别的变体那条路径上寄存器里装的
        是完全无关的值（这正是 if/match 表达式踩过的那个坑）。
        """
        variants = getattr(ty, "variants", None) or []
        if not variants:
            r = self.new_temp(STR)
            self.emit("CALL", r, [Sym("fa_str_of_ptr"), v], ty=STR)
            self.mark_owned(r, STR)
            return r
        tag = self.new_temp(I64)
        self.emit("LOAD", tag, [v], extra=0, ty=I64)
        slot = self.emit_alloca(8)
        # 兜底：tag 不在任何变体里（不该发生）就打个类型名，别去解引用 0
        self.emit("STORE", args=[slot, self.make_str(ty.name)], extra=0, ty=STR)
        end = self.new_label("etosend")
        for vname, vfields, vi in variants:
            build = self.new_label("etosv")
            nxt = self.new_label("etosn")
            c = self.new_temp(BOOL)
            self.emit("CMP", c, [tag, self.const(vi)], extra="==", ty=I64)
            self.emit("BR", args=[c], extra=(build, nxt))
            self.emit("LABEL", extra=build)
            ref_before = set(self.owned_ids)
            agg_before = set(self.agg_owned_ids)
            cur = self.make_str(vname)
            if vfields:
                cur = self.concat_str(cur, self.make_str("("))
                for j, (fname, fty, off) in enumerate(vfields):
                    if j:
                        cur = self.concat_str(cur, self.make_str(", "))
                    if fname:
                        cur = self.concat_str(cur, self.make_str(f"{fname}: "))
                    # 载荷区从第 8 字节开始（前 8 字节是 tag）
                    cur = self.concat_str(cur, self.gen_to_str(
                        self.load_field_value(v, fty, 8 + off), fty))
                cur = self.concat_str(cur, self.make_str(")"))
            self.take_owned(cur, STR)          # 结果交给槽，别在下面被放掉
            self.emit("STORE", args=[slot, cur], extra=0, ty=STR)
            self.release_new_refs(None, ref_before, agg_before)
            self.emit("JMP", extra=end)
            self.emit("LABEL", extra=nxt)
        self.emit("LABEL", extra=end)
        r = self.new_temp(STR)
        self.emit("LOAD", r, [slot], extra=0, ty=STR)
        self.mark_owned(r, STR)
        return r

    def gen_binary(self, e: Binary):
        op = e.op
        hint = self.take_hint()      # 先收下提示：子表达式一律不许抢
        lt, rt = e.left.ty, e.right.ty
        # 短路逻辑
        if op in ("and", "or"):
            a = self.gen_expr(e.left)
            r = self.new_temp(BOOL)
            self.emit("MOV", r, [self.coerce(a, lt, BOOL)], ty=BOOL)
            l2 = self.new_label("sc")
            end = self.new_label("sce")
            self.emit("BR", args=[r],
                      extra=(end if op == "or" else l2, l2 if op == "or" else end))
            self.emit("LABEL", extra=l2)
            b = self.gen_expr(e.right)
            self.emit("MOV", r, [self.coerce(b, rt, BOOL)], ty=BOOL)
            self.emit("LABEL", extra=end)
            return r
        # 字符串
        if lt == STR and rt == STR:
            a = self.gen_expr(e.left)
            b = self.gen_expr(e.right)
            if op == "+":
                r = self.new_temp(STR)
                self.emit("CALL", r, [Sym("fa_str_concat"), a, b], ty=STR)
                self.mark_owned(r, STR)
                return r
            rr = self.new_temp(I64)
            fn = {"==": "fa_str_eq", "!=": "fa_str_eq", "<": "fa_str_cmp",
                  "<=": "fa_str_cmp", ">": "fa_str_cmp", ">=": "fa_str_cmp"}[op]
            self.emit("CALL", rr, [Sym(fn), a, b], ty=I64)
            c = self.new_temp(BOOL)
            if op == "!=":
                self.emit("CMP", c, [rr, self.const(0)], extra="==", ty=I64)
            elif op == "==":
                self.emit("CMP", c, [rr, self.const(1)], extra="==", ty=I64)
            else:
                self.emit("CMP", c, [rr, self.const(0)], extra=op, ty=I64)
            # 这里**不能**把 a / b 登记成本语句拥有的引用：
            # 它们是操作数（多半是变量里借来的值），不是比较产生的新对象。
            # 以前登记了，语句末尾就多减一次引用计数 —— 堆上的字符串
            # （`"he"+"llo"`、`v.join("-")` 这种）会被提前释放，函数收尾再减一次
            # 就是 use-after-free。小字符串 glibc 一般不吭声，长字符串直接
            # “corrupted size vs. prev_size while consolidating”。
            # 操作数如果本身是新临时值（比如 `("a"+"b") == c` 的左边），
            # 拼接那边已经登记过了，这里不用管。
            return c
        # 指针算术
        if lt.kind == "ptr" and rt.kind == "int" and op in ("+", "-"):
            a = self.gen_expr(e.left)
            b = self.coerce(self.gen_expr(e.right), rt, I64)
            scale = max(lt.inner.size, 1) if lt.inner else 1
            off = self.new_temp(I64)
            self.emit("BIN", off, [b, self.const(scale)], extra="*", ty=I64)
            r = self.new_temp(lt)
            self.emit("BIN", r, [a, off], extra=op, ty=I64)
            return r
        if lt.kind == "ptr" and rt.kind == "ptr" and op == "-":
            a = self.gen_expr(e.left)
            b = self.gen_expr(e.right)
            d = self.new_temp(I64)
            self.emit("BIN", d, [a, b], extra="-", ty=I64)
            scale = max(lt.inner.size, 1) if lt.inner else 1
            r = self.new_temp(I64)
            self.emit("BIN", r, [d, self.const(scale)], extra="/", ty=I64)
            return r
        # 比较
        if op in ("==", "!=", "<", "<=", ">", ">="):
            a = self.gen_expr(e.left)
            b = self.gen_expr(e.right)
            if lt.is_float or rt.is_float:
                a = self.coerce(a, lt, F64)
                b = self.coerce(b, rt, F64)
                c = self.new_temp(BOOL)
                self.emit("CMP", c, [a, b], extra=op, ty=F64)
                return c
            wt = lt if lt.size >= rt.size else rt
            if wt.size < 8:
                wt = I64
            a = self.coerce(a, lt, wt)
            b = self.coerce(b, rt, wt)
            c = self.new_temp(BOOL)
            self.emit("CMP", c, [a, b], extra=op, ty=wt)
            return c
        # 算术
        a = self.gen_expr(e.left)
        b = self.gen_expr(e.right)
        if lt.is_float or rt.is_float:
            a = self.coerce(a, lt, F64)
            b = self.coerce(b, rt, F64)
            fop = {"+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
                   "**": "^"}[op]
            if op == "**":
                r = self.hint_or_new(hint, F64)
                self.emit("CALL", r, [Sym("pow"), a, b], ty=F64)
                return r
            r = self.hint_or_new(hint, F64)
            self.emit("BIN", r, [a, b], extra=fop, ty=F64)
            return r
        wt = e.ty if e.ty.kind == "int" else I64
        if wt.size < 8:
            wt = I64
        a = self.coerce(a, lt, wt)
        b = self.coerce(b, rt, wt)
        if op == "**":
            r = self.hint_or_new(hint, I64)
            self.emit("CALL", r, [Sym("fa_ipow"), a, b], ty=I64)
            return r
        r = self.hint_or_new(hint, wt)
        self.emit("BIN", r, [a, b], extra=op, ty=wt)
        if e.ty.kind == "int" and e.ty.size < wt.size:
            r2 = self.new_temp(e.ty)
            self.emit("CONV", r2, [r], extra=wt, ty=e.ty)
            return r2
        return r

    def gen_unary(self, e: Unary):
        op = e.op
        hint = self.take_hint()
        if op == "&":
            return self.gen_addrof1(e.operand)
        if op == "*":
            v = self.gen_expr(e.operand)
            return self.load_ptr(v, e.ty, 0)
        v = self.gen_expr(e.operand)
        t = e.operand.ty
        if op == "-":
            if t.is_float:
                r = self.hint_or_new(hint, F64)
                self.emit("UN", r, [self.coerce(v, t, F64)], extra="-", ty=F64)
                return r
            r = self.hint_or_new(hint, I64)
            self.emit("UN", r, [self.coerce(v, t, I64)], extra="-", ty=I64)
            return r
        if op == "+":
            return v
        if op == "!":
            r = self.hint_or_new(hint, BOOL)
            self.emit("UN", r, [self.coerce(v, t, BOOL)], extra="!", ty=BOOL)
            return r
        if op == "~":
            r = self.hint_or_new(hint, I64)
            self.emit("UN", r, [self.coerce(v, t, I64)], extra="~", ty=I64)
            return r
        self.err(f"未知一元运算符 '{op}'", e)

    def gen_cast(self, e: Cast):
        v = self.gen_expr(e.operand)
        return self.coerce(v, e.operand.ty, e.ty)

    def gen_addrof(self, e: AddrOf):
        return self.gen_addrof1(e.operand)

    def gen_addrof1(self, operand: Expr) -> Temp:
        if isinstance(operand, NameRef):
            if self.is_global_name(operand.name):
                return self.global_addr(operand.name)   # &全局：拿到 .bss 里的地址
            loc = self.var_loc(operand.name, operand)
            if loc.kind == "mem":
                return loc.val
            # 温度量：溢出到栈
            slot = self.emit_alloca(loc.ty.size)
            self.emit("STORE", args=[slot, loc.val], extra=0, ty=loc.ty)
            slot_ty = slot
            loc.kind = "mem"
            loc.val = slot
            return slot_ty
        ptr, off, ty = self.gen_addr(operand)
        if off == 0:
            return ptr
        r = self.new_temp(ptr_to(ty))
        self.emit("LEA", r, [ptr], extra=off)
        return r

    def gen_addr(self, e: Expr) -> Tuple[Temp, Any, Type]:
        """返回 (基址指针 Temp, 偏移(常量或Temp), 类型)"""
        if isinstance(e, NameRef):
            if self.is_global_name(e.name):
                g = self.sema.globals[e.name]
                return self.global_addr(e.name), 0, g.ty
            loc = self.var_loc(e.name, e)
            if loc.kind == "mem":
                return loc.val, 0, loc.ty
            if getattr(loc, "is_ptr", False):
                return loc.val, 0, loc.ty      # 值本身就是地址，无需落栈
            slot = self.emit_alloca(loc.ty.size)
            self.emit("STORE", args=[slot, loc.val], extra=0, ty=loc.ty)
            loc.kind = "mem"
            loc.val = slot
            return slot, 0, loc.ty
        if isinstance(e, Field):
            ot = e.obj.ty
            if getattr(e, "auto_deref", False):
                # p.field：对象表达式的值**就是**指针，直接当基址用
                st = ot.inner
                base = self.gen_expr(e.obj)
                fo = st.fields[e.index][2]
                if st.kind == "enum":
                    fo += 8
                return base, fo, st.fields[e.index][1]
            if ot.kind in ("struct", "enum"):
                base, off0, _ = self.gen_addr(e.obj)
                fo = ot.fields[e.index][2]
                if ot.kind == "enum":
                    fo += 8
                return base, self.add_off(off0, fo), ot.fields[e.index][1]
            self.err(f"类型 {ot} 不支持字段取址", e)
        if isinstance(e, Index):
            ot = e.obj.ty
            if ot.kind == "arr":
                base, off0, _ = self.gen_addr(e.obj)
                idx = self.coerce(self.gen_expr(e.index), e.index.ty, I64)
                self.bounds_check(idx, self.const(ot.count), e)
                sc = max(ot.elem.size, 1)
                o = self.new_temp(I64)
                self.emit("BIN", o, [idx, self.const(sc)], extra="*", ty=I64)
                return base, self.add_off(o, off0 if isinstance(off0, int) else 0), ot.elem \
                    if isinstance(off0, int) else (base, self.add_off(o, 0), ot.elem)
            if ot.kind == "ptr":
                p = self.gen_expr(e.obj)
                idx = self.coerce(self.gen_expr(e.index), e.index.ty, I64)
                sc = max(ot.inner.size, 1) if ot.inner else 1
                o = self.new_temp(I64)
                self.emit("BIN", o, [idx, self.const(sc)], extra="*", ty=I64)
                return p, o, ot.inner
            if ot.kind in ("vec", "map", "str"):
                # `v[i].field` / `m[k].field`：先按下标把元素取出来
                # （结构体元素在槽里存的是装箱指针），再走函数末尾
                # 「值本身就是指向聚合对象的指针」那条兜底路径。
                v = self.gen_expr(e)                  # -> gen_subscript
                if is_agg(e.ty):
                    return v, 0, e.ty
                self.err(f"{ot} 的下标结果是标量 {e.ty}，不可取址", e)
            self.err(f"类型 {ot} 不支持下标取址", e)
        if isinstance(e, Deref):
            p = self.gen_expr(e.operand)
            return p, 0, e.ty
        if isinstance(e, Unary) and e.op == "*":
            p = self.gen_expr(e.operand)
            return p, 0, e.ty
        # 兜底：值本身就是「指向聚合对象的指针」的表达式 ——
        #   * 返回结构体的函数调用：mk(3).id
        #   * Vec.get(i) 取出的装箱结构体元素：v.get(0).id
        #   * 结构体字段里的内联子对象：o.inner.x（走 Field 分支）
        # 以前这里一律报「表达式不可取址」，导致 `mk(3).id` 这种最常见的写法编译不过。
        if getattr(e, "ty", None) is not None and is_agg(e.ty):
            return self.gen_expr(e), 0, e.ty
        self.err(f"表达式不可取址", e)

    def bounds_check(self, idx: Temp, limit, node):
        ok = self.new_temp(BOOL)
        ge = self.new_temp(BOOL)
        lt = self.new_temp(BOOL)
        self.emit("CMP", ge, [idx, self.const(0)], extra=">=", ty=I64)
        self.emit("CMP", lt, [idx, limit], extra="<", ty=I64)
        self.emit("BIN", ok, [ge, lt], extra="and", ty=BOOL)
        l_ok = self.new_label("bok")
        l_bad = self.new_label("bbad")
        self.emit("BR", args=[ok], extra=(l_ok, l_bad))
        self.emit("LABEL", extra=l_bad)
        msg = self.make_str("下标越界 (index out of range)")
        self.emit("CALL", None, [Sym("fa_panic"), msg])
        self.emit("LABEL", extra=l_ok)

    def load_ptr(self, ptr, ty: Type, off) -> Temp:
        if is_agg(ty):
            # 聚合值用「指向它的指针」表示，但偏移量不能丢：
            # `es[1]`（枚举数组）以前返回的是数组首地址，于是 match 到的
            # 永远是第 0 个元素的 tag。
            if isinstance(off, int) and off == 0:
                return ptr
            r = self.new_temp(ptr_to(ty))
            self.emit("LEA", r, [ptr], extra=off)
            return r
        r = self.new_temp(ty)
        self.emit("LOAD", r, [ptr], extra=off, ty=ty)
        return r

    def gen_index(self, e: Index):
        ot = e.obj.ty
        if ot is not None and ot.kind in ("vec", "map", "str"):
            # v[i] / m[k] / s[i] —— 以前 gen_addr 只认数组和指针，
            # 这三种最常见的下标写法一律报「不支持下标取址」直接编译失败。
            return self.gen_subscript(e)
        ptr, off, ty = self.gen_addr(e)
        return self.load_ptr(ptr, ty, off)

    def gen_subscript(self, e: Index):
        """读 v[i] / m[k] / s[i]"""
        ot = e.obj.ty
        if ot.kind == "str":
            obj = self.gen_expr(e.obj)
            i = self.coerce(self.gen_expr(e.index), e.index.ty, I64)
            # 字符串在运行时是 FaStr*：{ rc, len, data[] }，字节从偏移 16 开始。
            # 直接 [obj + i] 读到的是引用计数和长度的字节（实测打印出 U+FFFD）。
            n = self.new_temp(I64)
            self.emit("LOAD", n, [obj], extra=8, ty=I64)
            self.bounds_check(i, n, e)
            data = self.new_temp(ptr_to(CHAR))
            self.emit("LEA", data, [obj], extra=16)
            r = self.new_temp(CHAR)
            self.emit("LOAD", r, [data], extra=(i, 1), ty=CHAR)
            return r
        mc = MethodCall(obj=e.obj, name="get", args=[e.index])
        mc.ty = e.ty
        mc.line, mc.col = e.line, e.col
        return self.gen_builtin_method(mc)

    def gen_subscript_write(self, e: Index, value: Expr):
        """写 v[i] = x / m[k] = v"""
        mc = MethodCall(obj=e.obj, name="set", args=[e.index, value])
        mc.ty = VOID
        mc.line, mc.col = e.line, e.col
        self.gen_builtin_method(mc)

    def gen_field(self, e: Field):
        # 枚举变体构造器：Color.Green -> 生成带 tag 的枚举值
        if getattr(e, "is_variant", False):
            ot = e.ty
            slot = self.emit_alloca(ot.size)
            self.emit("ZERO", args=[slot], extra=ot.size)
            self.emit("STORE", args=[slot, self.const(e.variant_index)],
                      extra=0, ty=I64)
            self.mark_agg_owned(slot, ot)
            return slot
        ot = e.obj.ty
        if ot.kind in ("struct", "enum") or getattr(e, "auto_deref", False):
            ptr, off, ty = self.gen_addr(e)
            if is_agg(ty):
                base = self.new_temp(ptr_to(ty))
                self.emit("LEA", base, [ptr], extra=off)
                return base
            return self.load_ptr(ptr, ty, off)
        if ot.kind in ("arr", "vec", "str") and e.name == "len":
            n = self.new_temp(I64)
            obj = self.gen_expr(e.obj)
            if ot.kind == "arr":
                return self.const(ot.count)
            if ot.kind == "vec":
                self.emit("CALL", n, [Sym("fa_vec_len"), obj], ty=I64)
            else:
                self.emit("CALL", n, [Sym("fa_str_len"), obj], ty=I64)
            return n
        self.err(f"不支持的字段访问 {ot}.{e.name}", e)

    def gen_arraylit(self, e: ArrayLit):
        ty = e.ty
        if ty.kind == "vec":
            # `let v: Vec<i64> = []` 等价于 `Vec<i64>()`；
            # `let v: Vec<i64> = [1, 2]` 等价于 `Vec<i64>[1, 2]`
            return self.gen_vec_from_elems(ty.elem, e.elems)
        slot = self.emit_alloca(max(ty.size, 1))
        for i, el in enumerate(e.elems):
            v = self.gen_expr(el)
            if is_agg(el.ty):
                d = self.new_temp(ptr_to(el.ty))
                self.emit("LEA", d, [slot], extra=i * max(el.ty.size, 1))
                self.emit_init_agg(d, v, el.ty)
            else:
                self.emit("STORE", args=[slot, self.coerce(v, el.ty, ty.elem)],
                          extra=i * max(ty.elem.size, 1), ty=ty.elem)
                if T.t_is_refcounted(ty.elem):
                    self.emit_rcinc(v, ty.elem)
        self.mark_agg_owned(slot, ty)
        return slot

    def gen_structlit(self, e: StructLit):
        st = e.resolved
        slot = self.emit_alloca(st.size)
        if st.is_refcounted:
            # 字段可能是「未提供」的，先清零，免得 retain/drop 读到栈上的垃圾指针
            self.emit("ZERO", args=[slot], extra=st.size)
        defaults = getattr(self.sema.struct_decls.get(st.name), "defaults", None) or {}
        for fname, fty, off in st.fields:
            expr = dict(e.fields).get(fname)
            if expr is None:
                # 字面量里省略的字段：用声明时写的默认值补上（每次构造都重新
                # 求值一遍，所以 `items: Vec<i64> = []` 每个对象拿到的是各自的空表）
                expr = defaults.get(fname)
                if expr is None:
                    continue                 # 语义阶段已经报过错；这里保持清零

            v = self.gen_expr(expr)
            if is_agg(fty):
                d = self.new_temp(ptr_to(fty))
                self.emit("LEA", d, [slot], extra=off)
                self.emit_init_agg(d, v, fty)
            else:
                self.emit("STORE", args=[slot, self.coerce(v, expr.ty, fty)],
                          extra=off, ty=fty)
                if T.t_is_refcounted(fty):
                    self.emit_rcinc(v, expr.ty)
        self.mark_agg_owned(slot, st)
        return slot

    def gen_new(self, e: NewExpr):
        """new expr -> 在堆上放一份拷贝，返回指针"""
        v = self.gen_expr(e.operand)
        ty = e.operand.ty
        p = self.new_temp(ptr_to(ty))
        self.emit("CALL", p, [Sym("fa_alloc"), self.const(max(ty.size, 8))], ty=ptr_to(ty))
        if is_agg(ty):
            self.emit("MEMCPY", args=[p, v], extra=ty.size, ty=ty)
            # 堆上这份拷贝必须自己拥有一份引用：结构体 / 枚举 / 数组里的
            # str、Vec、Map 字段逐个 rc_inc（编译器为类型生成的 __fa_retain_<T>
            # / fa_retain_arr）。以前只 memcpy 不 retain，`new P { name: "x" + "y" }`
            # 里那个临时字符串在语句末尾就被释放，堆上的字段成了悬垂指针，
            # 下一次读它是 heap-use-after-free（ASan 实测抓到）。
            # 标量路径下面那支本来就 rc_inc 了，聚合路径漏了。
            self.emit_rcinc(p, ty)
        else:
            self.emit("STORE", args=[p, v], extra=0, ty=ty)
            if T.t_is_refcounted(ty):
                self.emit_rcinc(v, ty)
        return p

    def gen_vec_from_elems(self, et: Type, elems) -> Temp:
        """新建一个 Vec<et> 并把 elems 依次 push 进去。

        `Vec<T>[...]`、`Vec<T>(...)` 与「有标注的 [] 字面量」
        （`let v: Vec<i64> = [1, 2]`）三条路共用，免得引用计数/装箱规则走偏。
        """
        vty = vec_of(et)
        v = self.new_temp(vty)
        self.emit("CALL", v, [Sym("fa_vec_new"), self.const(elem_kind(et, self.sema)),
                              self.const(vec_esz(et)),
                              self.const(1 if (et.kind == "int" and et.is_signed) else 0),
                              self.const(T.ty_code(et))], ty=vty)
        self.mark_owned(v, vty)
        for a in elems:
            av = self.gen_expr(a)
            if et.kind in ("struct", "enum"):
                self.emit("CALL", None, [Sym("fa_vec_push"), v, self.box_agg(av, et)])
            else:
                # fa_vec_push 内部已按元素 kind 做 rc_inc
                cv = self.coerce(av, a.ty, et)
                if et.is_float:
                    cv = self.bitcast(cv, I64)
                self.emit("CALL", None, [Sym("fa_vec_push"), v, cv])
        return v

    def gen_ctor(self, e: Ctor):
        """Vec<T>(...) / Map<K,V>() / Vec<T>[...]"""
        if e.name == "Vec":
            return self.gen_vec_from_elems(self.sema.resolve_type(e.targs[0]), e.args)
        if e.name == "Map":
            kt = self.sema.resolve_type(e.targs[0])
            vt = self.sema.resolve_type(e.targs[1])
            m = self.new_temp(map_of(kt, vt))
            self.emit("CALL", m, [Sym("fa_map_new"),
                                  self.const(elem_kind(kt, self.sema)),
                                  self.const(elem_kind(vt, self.sema)),
                                  self.const(T.ty_code(kt)),
                                  self.const(T.ty_code(vt))], ty=map_of(kt, vt))
            self.mark_owned(m, map_of(kt, vt))
            for i in range(0, len(e.args), 2):      # Map<K,V>[k: v, ...] 的初值
                self.emit_map_set(m, e.args[i], e.args[i + 1], kt, vt)
            return m
        self.err(f"未知构造器 '{e.name}'", e)

    # ------------------------------------------------------------ 调用
    def gen_call(self, e: Call):
        callee = e.callee
        if isinstance(callee, NameRef):
            if callee.name in BUILTIN_FNS:
                return self.gen_builtin(callee.name, e)
            # 优先用 sema 解析出来的那个符号：嵌套函数被提升成了 `外层__内层`，
            # 按名字在顶层函数表里是查不到的（会误报「未定义函数 'inner'」）。
            fs = callee.resolved if isinstance(callee.resolved, FnSym) else None
            if fs is None:
                fs = self.sema.fns.get(callee.name)
            if fs is None:
                # 函数指针：`let f: fn(i64) -> i64 = add1` 之后 `f(41)`。
                # sema 已经把 f 定成 fn 类型了，codegen 却只会查函数表，
                # 于是报一句莫名其妙的「未定义函数 'f'」。
                loc = self.scope.lookup(callee.name)
                if loc is not None and loc.ty is not None and loc.ty.kind == "fn":
                    pv = loc.val if loc.kind == "temp" else self.load_ptr(loc.val, loc.ty, 0)
                    return self.gen_call_ptr(pv, e, loc.ty)
                self.err(f"未定义函数 '{callee.name}'", e)
            return self.gen_call_fs(fs, e)
        if isinstance(callee, Field):
            # 枚举变体构造 Enum.Variant(...)
            ot = callee.obj.ty
            if isinstance(callee.obj, NameRef) and ot.kind == "enum":
                return self.gen_enum_ctor(ot, callee.name, e.args, e)
        v = self.gen_expr(callee)
        return self.gen_call_ptr(v, e, callee.ty)

    def gen_call_fs(self, fs: FnSym, e: Call):
        args = []
        hint = self.take_hint()
        ret = fs.ret
        if is_agg(ret):
            slot = self.emit_alloca(ret.size)
            self.mark_agg_owned(slot, ret)
            args.append(slot)
        for i, a in enumerate(e.args):
            if i < len(fs.params):
                pty = fs.params[i]
                av = self.gen_expr(a)
                if fs.extern and pty.kind == "str":
                    # C 侧收的是 char*。FaStr 的头两个字段是 rc/len，
                    # 直接把对象指针传过去，C 读到的是乱码（文档承诺的
                    # 「str 自动转成 char*」以前只在可变参数那条路上做了）
                    args.append(self.coerce(av, a.ty, ptr_to(U8)))
                else:
                    args.append(av if is_agg(pty) else self.coerce(av, a.ty, pty))
            else:
                # 可变参数：按 C 的默认实参提升（float -> double，str -> char*）
                av = self.gen_expr(a)
                if a.ty is not None and a.ty.is_float and a.ty.size < 8:
                    av = self.coerce(av, a.ty, F64)
                elif a.ty == STR:
                    av = self.call1("fa_str_cstr", av, ptr_to(U8))
                args.append(av)
        if is_agg(ret):
            self.emit("CALL", None, [Sym(fs.symbol)] + args, extra=fs)
            self.mark_agg_owned(slot, ret)
            return slot
        if ret.kind == "void":
            self.emit("CALL", None, [Sym(fs.symbol)] + args, extra=fs)
            return self.const(0, VOID)
        r = self.hint_or_new(hint, ret)
        if fs.extern and ret.kind == "str":
            # 对称地：C 返回的 char* 不是 FaStr，得拷一份成 FA 字符串
            pr = self.new_temp(ptr_to(U8))
            self.emit("CALL", pr, [Sym(fs.symbol)] + args, extra=fs, ty=ptr_to(U8))
            self.emit("CALL", r, [Sym("fa_str_from_cstr"), pr], ty=STR)
            self.mark_owned(r, STR)
            return r
        self.emit("CALL", r, [Sym(fs.symbol)] + args, extra=fs, ty=ret)
        if T.t_is_refcounted(ret):
            self.mark_owned(r, ret)
        return r

    def gen_call_ptr(self, ptrv, e: Call, fnty: Type):
        args = []
        for i, a in enumerate(e.args):
            av = self.gen_expr(a)
            args.append(av)
        ret = fnty.ret if fnty.kind == "fn" else ANY
        if ret.kind == "void":
            self.emit("CALLPTR", None, [ptrv] + args)
            return self.const(0, VOID)
        r = self.new_temp(ret)
        self.emit("CALLPTR", r, [ptrv] + args, ty=ret)
        return r

    def gen_enum_ctor(self, ety: Type, vname: str, args: List[Expr], node):
        slot = self.emit_alloca(ety.size)
        vi = None
        for name, flaid, i in ety.variants:
            if name == vname:
                vi, vfields = i, flaid
                break
        if vi is None:
            self.err(f"枚举 {ety.name} 没有变体 '{vname}'", node)
        self.emit("STORE", args=[slot, self.const(vi)], extra=0, ty=I64)
        for i, a in enumerate(args):
            fty = vfields[i][1] if i < len(vfields) else ANY
            v = self.gen_expr(a)
            off = 8 + (vfields[i][2] if i < len(vfields) else 0)
            if is_agg(fty):
                d = self.new_temp(ptr_to(fty))
                self.emit("LEA", d, [slot], extra=off)
                self.emit_init_agg(d, v, fty)
            else:
                self.emit("STORE", args=[slot, self.coerce(v, a.ty, fty)],
                          extra=off, ty=fty)
                if T.t_is_refcounted(fty):
                    self.emit_rcinc(v, a.ty)
        self.mark_agg_owned(slot, ety)
        return slot

    def gen_method(self, e: MethodCall):
        ot = e.obj.ty
        # `m.keys()` / `m.values()`：方法形式转发给内建的全局实现
        # （BUILTIN_FNS 里的 keys/values 收的就是「第 0 个实参是 Map」）
        if ot.kind == "map" and e.name in ("keys", "values"):
            return self.gen_builtin(e.name, Call(NameRef(e.name), [e.obj]))
        # 命名空间（py / java）
        if ot.kind == "ns":
            return self.gen_ns_method(ot.name, e)
        # 枚举变体构造：Shape.Circle(2.0)（sema 标成 enum-ctor）
        if ot.kind == "enum" and e.resolved == "enum-ctor":
            return self.gen_enum_ctor(ot, e.name, e.args, e)
        fs = e.resolved
        if isinstance(fs, FnSym):
            args = []
            objv = self.gen_expr(e.obj)
            if is_agg(ot):
                args.append(objv)
            else:
                args.append(objv)
            for i, a in enumerate(e.args):
                # 方法的 FnSym.params **不含** self（sema.register_impl 过滤掉了），
                # 所以第 i 个实参对应 fs.params[i]。以前写成 i+1：
                # 单参数方法侥幸拿到 ANY（不做转换，看着是对的），
                # 多参数方法就把实参按**错一位**的类型转换 ——
                # `p.combine(1, 2.5, "tail")` 里的 1 被转成 f64、2.5 被转成 str。
                pty = fs.params[i] if i < len(fs.params) else ANY
                av = self.gen_expr(a)
                args.append(av if is_agg(pty) else self.coerce(av, a.ty, pty))
            ret = fs.ret
            if is_agg(ret):
                slot = self.emit_alloca(ret.size)
                self.emit("CALL", None, [Sym(fs.symbol), slot] + args, extra=fs)
                self.mark_agg_owned(slot, ret)
                return slot
            if ret.kind == "void":
                self.emit("CALL", None, [Sym(fs.symbol)] + args, extra=fs)
                return self.const(0, VOID)
            r = self.new_temp(ret)
            self.emit("CALL", r, [Sym(fs.symbol)] + args, extra=fs, ty=ret)
            if T.t_is_refcounted(ret):
                self.mark_owned(r, ret)
            return r
        if e.resolved == "builtin-method":
            return self.gen_builtin_method(e)
        self.err(f"未解析的方法调用 .{e.name}", e)

    def emit_bounds_check(self, bad: Temp):
        """bad 为真时跳到运行时报错（下标越界）"""
        ok = self.new_label("bok")
        self.emit("BR", args=[bad], extra=(self.new_label("bbad"), ok))
        # 用一条 JMP 串联：BR 的真分支先落到报错调用
        lbl_bad = self.ir[-1].extra[0]
        self.emit("LABEL", extra=lbl_bad)
        self.emit("CALL", None, [Sym("fa_bounds_error")])
        self.emit("LABEL", extra=ok)

    def bitcast(self, v, to_ty: Type):
        """同一 64 位数据的类型重解释（i64 <-> f64），用于容器这类按 uint64_t 存取的 ABI"""
        if isinstance(v, Const):
            # 常量必须在**这里**就重解释：否则优化器会把 BITCAST 折叠掉，
            # 调用点看到的是一个 ty=f64 的常量，按浮点 ABI 放进 xmm0 ——
            # 而 fa_vec_push / fa_map_set 收的是 uint64_t 位模式（应在 rsi）。
            # 实测 `push(fv, 2.5)` 存进去的是垃圾位（读回 3.16e-322）。
            if isinstance(v.val, float) and not to_ty.is_float:
                return Const(struct.unpack("<q", struct.pack("<d", v.val))[0], to_ty)
            if isinstance(v.val, int) and to_ty.is_float:
                return Const(struct.unpack("<d", struct.pack("<q", v.val))[0], to_ty)
        r = self.new_temp(to_ty)
        self.emit("BITCAST", r, [v], ty=to_ty)
        return r

    def gen_builtin_method(self, e: MethodCall):
        ot = e.obj.ty
        obj = self.gen_expr(e.obj)
        name = e.name
        # ---- str
        if ot == STR:
            if name in ("repeat", "count"):
                a = self.gen_expr(e.args[0])
                if name == "count":
                    sub = self.gen_to_str(a, e.args[0].ty)
                    return self.call2("fa_str_count", obj, sub, I64)
                n = self.coerce(a, e.args[0].ty, I64)
                r = self.call2("fa_str_repeat", obj, n, STR)
                self.mark_owned(r, STR)
                return r
            fnmap = {"len": ("fa_str_len", I64), "at": ("fa_str_byte", I64),
                     "bytes": ("fa_str_len", I64), "to_i64": ("fa_str_to_i64", I64),
                     "to_f64": ("fa_str_to_f64", F64),
                     "slice": ("fa_str_slice", STR), "trim": ("fa_str_trim", STR),
                     "upper": ("fa_str_upper", STR), "lower": ("fa_str_lower", STR),
                     "split": ("fa_str_split", vec_of(STR)),
                     "chars": ("fa_str_chars", vec_of(CHAR)),
                     "repeat": ("fa_str_repeat", STR),
                     "count": ("fa_str_count", I64),
                     "lines": ("fa_str_lines", vec_of(STR)),
                     "trim_start": ("fa_str_trim_start", STR),
                     "trim_end": ("fa_str_trim_end", STR),
                     # UTF-8 码点：char_len() / char_at(i) / codepoints() /
                     # slice_chars(a, b)
                     "char_len": ("fa_str_char_len", I64),
                     "char_at": ("fa_str_char_at", I64),
                     "codepoints": ("fa_str_codepoints", vec_of(I64)),
                     "slice_chars": ("fa_str_slice_chars", STR)}
            if name in ("find", "contains", "starts_with", "ends_with", "eq", "replace"):
                if name == "find":
                    a = self.gen_expr(e.args[0])
                    r = self.new_temp(I64)
                    self.emit("CALL", r, [Sym("fa_str_find"), obj,
                                          self.gen_to_str(a, e.args[0].ty)], ty=I64)
                    return r
                if name == "contains":
                    a = self.gen_expr(e.args[0])
                    r = self.new_temp(I64)
                    self.emit("CALL", r, [Sym("fa_str_find"), obj,
                                          self.gen_to_str(a, e.args[0].ty)], ty=I64)
                    c = self.new_temp(BOOL)
                    self.emit("CMP", c, [r, self.const(0)], extra=">=", ty=I64)
                    return c
                if name == "starts_with":
                    a = self.gen_expr(e.args[0])
                    r = self.new_temp(I64)
                    self.emit("CALL", r, [Sym("fa_str_starts"), obj,
                                          self.gen_to_str(a, e.args[0].ty)], ty=I64)
                    c = self.new_temp(BOOL)
                    self.emit("CMP", c, [r, self.const(1)], extra="==", ty=I64)
                    return c
                if name == "ends_with":
                    a = self.gen_expr(e.args[0])
                    r = self.new_temp(I64)
                    self.emit("CALL", r, [Sym("fa_str_ends"), obj,
                                          self.gen_to_str(a, e.args[0].ty)], ty=I64)
                    c = self.new_temp(BOOL)
                    self.emit("CMP", c, [r, self.const(1)], extra="==", ty=I64)
                    return c
                if name == "eq":
                    a = self.gen_expr(e.args[0])
                    r = self.new_temp(I64)
                    self.emit("CALL", r, [Sym("fa_str_eq"), obj,
                                          self.gen_to_str(a, e.args[0].ty)], ty=I64)
                    c = self.new_temp(BOOL)
                    self.emit("CMP", c, [r, self.const(1)], extra="==", ty=I64)
                    return c
                if name == "replace":
                    a = self.gen_expr(e.args[0])
                    b = self.gen_expr(e.args[1])
                    r = self.new_temp(STR)
                    self.emit("CALL", r, [Sym("fa_str_replace"), obj,
                                          self.gen_to_str(a, e.args[0].ty),
                                          self.gen_to_str(b, e.args[1].ty)], ty=STR)
                    self.mark_owned(r, STR)
                    return r
            if name in fnmap:
                fn, rt = fnmap[name]
                args = [obj]
                if name in ("slice", "slice_chars"):
                    args.append(self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64))
                    args.append(self.coerce(self.gen_expr(e.args[1]), e.args[1].ty, I64))
                if name == "split":
                    args.append(self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty))
                if name in ("at", "char_at"):
                    args.append(self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64))
                r = self.new_temp(rt)
                self.emit("CALL", r, [Sym(fn)] + args, ty=rt)
                if rt != I64:
                    self.mark_owned(r, rt)
                return r
            if name == "cstr":
                r = self.new_temp(ptr_to(U8))
                self.emit("CALL", r, [Sym("fa_str_cstr"), obj], ty=ptr_to(U8))
                return r
            if name == "to_str":
                r = self.new_temp(STR)
                self.emit("MOV", r, [obj], ty=STR)
                return r
        # ---- vec
        if ot.kind == "vec":
            et = ot.elem
            if name == "len":
                r = self.new_temp(I64)
                self.emit("LOAD", r, [obj], extra=8, ty=I64)
                return r
            if name == "push":
                a = self.gen_expr(e.args[0])
                if et.kind in ("struct", "enum"):
                    # 8 字节以内的结构体也要装箱：槽里存的必须是「指向副本的指针」，
                    # 否则运行时按指针去 retain/release 时会把结构体的第一个字段当成头。
                    self.emit("CALL", None, [Sym("fa_vec_push"), obj, self.box_agg(a, et)])
                else:
                    # fa_vec_push 内部已按元素 kind 做 rc_inc
                    av = self.coerce(a, e.args[0].ty, et if et.kind != "struct" else I64)
                    if et.is_float:
                        av = self.bitcast(av, I64)
                    self.emit("CALL", None, [Sym("fa_vec_push"), obj, av])
                return self.const(0, VOID)
            if name == "get":
                i = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
                # 内联快速路径：越界检查 + 直接取元素（省掉一次函数调用）
                n = self.new_temp(I64)
                self.emit("LOAD", n, [obj], extra=8, ty=I64)
                bad = self.new_temp(BOOL)
                self.emit("CMP", bad, [i, n], extra=">=", ty=I64)
                self.emit_bounds_check(bad)
                data = self.new_temp(ptr_to(I64))
                self.emit("LOAD", data, [obj], extra=32, ty=ptr_to(I64))
                # extra=(下标, 比例) -> 直接用 x86 比例变址 [data + i*esz]，省掉一条 imul
                ez = vec_esz(et)
                raw = self.new_temp(I64)
                self.emit("LOAD", raw, [data], extra=(i, ez),
                          ty=et if ez < 8 else I64)
                return self.bitcast(raw, et) if et.is_float else raw
            if name == "set":
                i = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
                a = self.gen_expr(e.args[1])
                # fa_vec_set 内部完成「新值 inc + 旧值 dec」
                if et.kind in ("struct", "enum"):
                    self.emit("CALL", None, [Sym("fa_vec_set"), obj, i,
                                             self.box_agg(a, et)])
                    return self.const(0, VOID)
                av = self.coerce(a, e.args[1].ty, et)
                if et.is_float:
                    av = self.bitcast(av, I64)
                if T.t_is_refcounted(et):
                    # 需要维护引用计数的元素仍然走运行时
                    self.emit("CALL", None, [Sym("fa_vec_set"), obj, i, av])
                    return self.const(0, VOID)
                # 内联快速路径：越界检查 + 直接写元素
                n = self.new_temp(I64)
                self.emit("LOAD", n, [obj], extra=8, ty=I64)
                bad = self.new_temp(BOOL)
                self.emit("CMP", bad, [i, n], extra=">=", ty=I64)
                self.emit_bounds_check(bad)
                data = self.new_temp(ptr_to(I64))
                self.emit("LOAD", data, [obj], extra=32, ty=ptr_to(I64))
                ez = vec_esz(et)
                self.emit("STORE", args=[data, av], extra=(i, ez),
                          ty=et if ez < 8 else I64)
                return self.const(0, VOID)
            if name == "pop":
                raw = self.new_temp(I64)
                self.emit("CALL", raw, [Sym("fa_vec_pop"), obj], ty=I64)
                r = self.bitcast(raw, et) if et.is_float else raw
                self.mark_owned(r, et)      # fa_vec_pop 转移所有权给调用方
                return r
            if name == "contains":
                v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, et)
                if et.is_float:
                    v = self.bitcast(v, I64)
                r = self.new_temp(I64)
                self.emit("CALL", r, [Sym("fa_vec_contains"), obj, v], ty=I64)
                return r
            if name == "resize":
                n = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
                if len(e.args) > 1:
                    v = self.coerce(self.gen_expr(e.args[1]), e.args[1].ty, et)
                else:
                    v = self.const_zero(et)      # v.resize(n)：新元素补零值
                if et.is_float:
                    v = self.bitcast(v, I64)     # 运行时按 uint64 收，浮点得按位转
                if et.kind in ("struct", "enum"):
                    v = self.box_agg(v, et)
                self.emit("CALL", None, [Sym("fa_vec_resize"), obj, n, v])
                return self.const(0, VOID)
            if name == "sort":
                fn = ("fa_vec_sort_f64" if et.is_float else
                      "fa_vec_sort_str" if et == STR else "fa_vec_sort_i64")
                self.emit("CALL", None, [Sym(fn), obj])
                return self.const(0, VOID)
            if name == "reverse":
                self.emit("CALL", None, [Sym("fa_vec_reverse"), obj])
                return self.const(0, VOID)
            if name == "join":
                sep = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                r = self.call2("fa_vec_join", obj, sep, STR)
                self.mark_owned(r, STR)
                return r
            if name == "sum":
                fn = "fa_vec_sum_f64" if et.is_float else "fa_vec_sum_i64"
                rt = F64 if et.is_float else I64
                return self.call1(fn, obj, rt)
            if name in ("min", "max"):
                if et.is_float:
                    return self.call1(f"fa_vec_{name}_f64", obj, F64)
                if et == STR:
                    self.err("str 容器的 min/max 暂不支持（请先 sort）", e)
                return self.call1(f"fa_vec_{name}_i64", obj, I64)
            if name == "index_of":
                a = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, et)
                if et.is_float:
                    a = self.bitcast(a, I64)
                return self.call2("fa_vec_index_of", obj, a, I64)
            if name == "clear":
                self.emit("CALL", None, [Sym("fa_vec_clear"), obj])
                return self.const(0, VOID)
        # ---- map
        if ot.kind == "map":
            kt, vt = ot.key, ot.val
            if name == "len":
                r = self.new_temp(I64)
                self.emit("CALL", r, [Sym("fa_map_len"), obj], ty=I64)
                return r
            if name == "get":
                k = self.gen_expr(e.args[0])
                kk = self.coerce(k, e.args[0].ty, kt)
                if kt.is_float:
                    kk = self.bitcast(kk, I64)
                raw = self.new_temp(I64)
                self.emit("CALL", raw, [Sym("fa_map_get"), obj, kk], ty=I64)
                return self.bitcast(raw, vt) if vt.is_float else raw
            if name == "set":
                self.emit_map_set(obj, e.args[0], e.args[1], kt, vt)
                return self.const(0, VOID)
            if name == "has":
                k = self.gen_expr(e.args[0])
                r = self.new_temp(I64)
                self.emit("CALL", r, [Sym("fa_map_has"), obj,
                                      self.coerce(k, e.args[0].ty, kt)], ty=I64)
                c = self.new_temp(BOOL)
                self.emit("CMP", c, [r, self.const(1)], extra="==", ty=I64)
                return c
            if name == "del":
                k = self.gen_expr(e.args[0])
                self.emit("CALL", None, [Sym("fa_map_del"), obj,
                                         self.coerce(k, e.args[0].ty, kt)])
                return self.const(0, VOID)
            if name == "clear":
                self.emit("CALL", None, [Sym("fa_map_clear"), obj])
                return self.const(0, VOID)
        # ---- pyobj
        if ot.kind == "pyobj":
            if name == "to_str":
                r = self.new_temp(STR)
                self.emit("CALL", r, [Sym("fa_py_to_str"), obj], ty=STR)
                self.mark_owned(r, STR)
                return r
            if name == "to_i64":
                r = self.new_temp(I64)
                self.emit("CALL", r, [Sym("fa_py_to_i64"), obj], ty=I64)
                return r
            if name == "to_f64":
                r = self.new_temp(F64)
                self.emit("CALL", r, [Sym("fa_py_to_f64"), obj], ty=F64)
                return r
            if name == "attr":
                a = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                r = self.new_temp(PYOBJ)
                self.emit("CALL", r, [Sym("fa_py_attr"), obj, a], ty=PYOBJ)
                self.mark_owned(r, PYOBJ)
                return r
            if name == "call":
                a = self.gen_expr(e.args[0]) if e.args else self.const(0)
                r = self.new_temp(PYOBJ)
                # fa_py_callv(对象, 方法名(FaStr*，直接调用对象本身时传 0), 参数 Vec)
                self.emit("CALL", r, [Sym("fa_py_callv"), obj, self.const(0), a], ty=PYOBJ)
                self.mark_owned(r, PYOBJ)
                return r
        # ---- jobj
        if ot.kind == "jobj":
            if name == "to_str":
                r = self.new_temp(STR)
                self.emit("CALL", r, [Sym("fa_jvm_to_str"), obj], ty=STR)
                self.mark_owned(r, STR)
                return r
            if name == "to_i64":
                r = self.new_temp(I64)
                self.emit("CALL", r, [Sym("fa_jvm_to_i64"), obj], ty=I64)
                return r
            if name == "to_f64":
                r = self.new_temp(F64)
                self.emit("CALL", r, [Sym("fa_jvm_to_f64"), obj], ty=F64)
                return r
            if name in ("jcall_i64", "jcall_f64", "jcall_obj", "jcall_void"):
                mname = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                sig = self.gen_to_str(self.gen_expr(e.args[1]), e.args[1].ty)
                n = len(e.args) - 2
                arr = self.emit_alloca(max(n, 1) * 8)
                for i in range(n):
                    a = e.args[2 + i]
                    av = self.gen_expr(a)
                    if a.ty is not None and a.ty.is_float:
                        self.emit("STORE", args=[arr, self.coerce(av, a.ty, F64)],
                                  extra=i * 8, ty=F64)
                    else:
                        self.emit("STORE", args=[arr, self.coerce(av, a.ty, I64)],
                                  extra=i * 8, ty=I64)
                fn = {"jcall_i64": "fa_jvm_call_i64", "jcall_f64": "fa_jvm_call_f64",
                      "jcall_obj": "fa_jvm_call_obj", "jcall_void": "fa_jvm_call_void"}[name]
                if name == "jcall_f64":
                    r = self.new_temp(F64)
                    self.emit("CALL", r, [Sym(fn), obj, mname, sig,
                                          self.const(n), arr], ty=F64)
                    return r
                if name == "jcall_void":
                    self.emit("CALL", None, [Sym(fn), obj, mname, sig,
                                             self.const(n), arr])
                    return self.const(0, VOID)
                rt = JOBJ if name == "jcall_obj" else I64
                r = self.new_temp(rt)
                self.emit("CALL", r, [Sym(fn), obj, mname, sig,
                                      self.const(n), arr], ty=rt)
                if rt == JOBJ:
                    self.mark_owned(r, JOBJ)
                return r
        # ---- 数值
        if ot.is_num:
            if name == "to_str":
                return self.gen_to_str(obj, ot)
            if name == "abs":
                r = self.new_temp(F64 if ot.is_float else I64)
                fn = "fabs" if ot.is_float else "labs"
                self.emit("CALL", r, [Sym(fn), self.coerce(obj, ot, F64 if ot.is_float else I64)],
                          ty=F64 if ot.is_float else I64)
                return r
            if name in ("to_f64", "to_i64"):
                return self.coerce(obj, ot, F64 if name == "to_f64" else I64)
            if name in ("ceil", "floor", "round", "trunc", "sqrt", "log", "log2",
                        "log10", "exp", "exp2", "sin", "cos", "tan"):
                return self.call1(name, self.coerce(obj, ot, F64), F64)
        # 容器/结构体的 to_str()：复用 print 用的那条字符串化路径
        if name == "to_str" and ot.kind in ("vec", "map", "arr", "struct",
                                            "enum", "bool", "char", "ptr"):
            return self.gen_to_str(obj, ot)
        if ot.kind == "arr" and name == "len":
            return self.const(ot.count)
        self.err(f"未实现的内建方法 .{name}（类型 {ot}）", e)

    # ------------------------------------------------------------ 内建函数
    # 只对容器有意义的内建（写在 sema 的 BUILTIN_FNS 里，但 codegen 一直没实现）
    CONTAINER_FNS = ("sum", "sort", "reverse", "join", "index_of")
    # 这些内建既有 v.push(x) 的方法写法，也有 push(v, x) 的全局写法
    VEC_GLOBAL_FNS = ("push", "pop", "get", "set", "clear", "resize", "contains")
    # 标量/容器两用的内建：实参是容器时走容器实现
    DUAL_FNS = ("min", "max", "contains")

    def gen_builtin(self, name: str, e: Call):
        # 容器版全局函数：sum(v) / sort(v) / join(v, sep) / contains(v, x) / min(v) ...
        # 必须在标量分支之前判断，否则 min(v) 会掉进「二元 min」里越界取 args[1]。
        t0 = e.args[0].ty if e.args else None
        is_cont = t0 is not None and t0.kind in ("vec", "arr", "map", "str")
        if name in self.CONTAINER_FNS or (name in self.DUAL_FNS and is_cont):
            return self._builtin_on_container(name, e)
        # push(v, x) / pop(v) / get(v, i) 这类「全局写法」以前在 gen_builtin 里
        # 另写了一份，漏掉了方法版有的两件事：浮点要按位模式当整数传、
        # 结构体元素要装箱。结果 `push(fv, 2.5)` 存进去的是垃圾位
        # （读回 3.16e-322），而 `fv.push(2.5)` 是对的。统一转发到方法实现。
        if name in self.VEC_GLOBAL_FNS and t0 is not None and t0.kind == "vec":
            return self._builtin_on_container(name, e)
        if name in ("print", "println"):
            for i, a in enumerate(e.args):
                if i:
                    sp = self.make_str(" ")
                    self.emit("CALL", None, [Sym("fa_print_str"), sp])
                v = self.gen_expr(a)
                if a.ty == STR:
                    self.emit("CALL", None, [Sym("fa_print_str"), v])
                elif a.ty.kind == "float":
                    self.emit("CALL", None, [Sym("fa_print_f64"), self.coerce(v, a.ty, F64)])
                elif a.ty.kind == "bool":
                    self.emit("CALL", None, [Sym("fa_print_bool"), v])
                elif a.ty == CHAR:
                    self.emit("CALL", None, [Sym("fa_print_char"), v])
                elif a.ty.kind == "ptr":
                    if self._is_cstr_ptr(a.ty):
                        cs = self.new_temp(STR)
                        self.emit("CALL", cs, [Sym("fa_str_from_cstr"), v], ty=STR)
                        self.emit("CALL", None, [Sym("fa_print_str"), cs])
                        # fa_str_from_cstr 拷出一份新 FaStr，打完就得放：
                        # 以前没登记，print 一个 C 的 char* 就漏一份拷贝。
                        self.mark_owned(cs, STR)
                    else:
                        self.emit("CALL", None, [Sym("fa_print_ptr"), v])
                elif a.ty.kind in ("vec", "map", "pyobj", "jobj"):
                    s = self.gen_to_str(v, a.ty)
                    self.emit("CALL", None, [Sym("fa_print_str"), s])
                elif a.ty.kind in ("struct", "arr", "enum"):
                    s = self.gen_to_str(v, a.ty)
                    self.emit("CALL", None, [Sym("fa_print_str"), s])
                else:
                    self.emit("CALL", None, [Sym("fa_print_i64"),
                                             self.coerce(v, a.ty, I64)])
            self.emit("CALL", None, [Sym("fa_print_nl")])
            return self.const(0, VOID)
        if name == "write":
            for a in e.args:
                v = self.gen_expr(a)
                s = v if a.ty == STR else self.gen_to_str(v, a.ty)
                self.emit("CALL", None, [Sym("fa_print_str"), s])
            return self.const(0, VOID)
        if name == "len":
            a = self.gen_expr(e.args[0])
            t = e.args[0].ty
            r = self.new_temp(I64)
            if t.kind == "vec":
                self.emit("CALL", r, [Sym("fa_vec_len"), a], ty=I64)
            elif t == STR:
                self.emit("CALL", r, [Sym("fa_str_len"), a], ty=I64)
            elif t.kind == "map":
                self.emit("CALL", r, [Sym("fa_map_len"), a], ty=I64)
            elif t.kind == "arr":
                return self.const(t.count)
            elif t.kind == "ptr":
                self.emit("CALL", r, [Sym("strlen"), a], ty=I64)
            else:
                self.err(f"len() 不支持 {t}", e)
            return r
        if name == "str":
            v = self.gen_expr(e.args[0])
            return self.gen_to_str(v, e.args[0].ty)
        if name in ("i64", "to_i64"):
            v = self.gen_expr(e.args[0])
            return self.coerce(v, e.args[0].ty, I64) if e.args[0].ty.kind != "str" \
                else self.call1("fa_str_to_i64", v, I64)
        if name == "f64":
            v = self.gen_expr(e.args[0])
            return self.coerce(v, e.args[0].ty, F64)
        if name == "panic":
            v = self.gen_expr(e.args[0])
            s = v if e.args[0].ty == STR else self.gen_to_str(v, e.args[0].ty)
            self.emit("CALL", None, [Sym("fa_panic"), s])
            return self.const(0, VOID)
        if name == "assert":
            c = self.gen_cond(e.args[0])
            l_ok = self.new_label("asok")
            l_bad = self.new_label("asbad")
            self.emit("BR", args=[c], extra=(l_ok, l_bad))
            self.emit("LABEL", extra=l_bad)
            msg = self.gen_expr(e.args[1]) if len(e.args) > 1 else None
            s = msg if (msg is not None and e.args[1].ty == STR) else (
                self.gen_to_str(msg, e.args[1].ty) if msg is not None
                else self.make_str("断言失败 (assertion failed)"))
            self.emit("CALL", None, [Sym("fa_panic"), s])
            self.emit("LABEL", extra=l_ok)
            return self.const(0, VOID)
        if name == "exit":
            v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
            self.emit("CALL", None, [Sym("fa_exit"), v])
            return self.const(0, VOID)
        if name == "sleep":
            v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
            self.emit("CALL", None, [Sym("fa_sleep_ms"), v])
            return self.const(0, VOID)
        if name == "now":
            return self.call0("fa_now", F64)
        if name == "read_line":
            r = self.new_temp(STR)
            self.emit("CALL", r, [Sym("fa_read_line")], ty=STR)
            self.mark_owned(r, STR)
            return r
        if name in ("sqrt", "sin", "cos", "tan", "log", "exp", "floor", "ceil"):
            v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, F64)
            return self.call1(name, v, F64)
        if name == "pow":
            a = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, F64)
            b = self.coerce(self.gen_expr(e.args[1]), e.args[1].ty, F64)
            return self.call2("pow", a, b, F64)
        if name == "abs":
            v = self.gen_expr(e.args[0])
            if e.args[0].ty.is_float:
                return self.call1("fabs", self.coerce(v, e.args[0].ty, F64), F64)
            return self.call1("labs", self.coerce(v, e.args[0].ty, I64), I64)
        if name in ("min", "max"):
            a = self.gen_expr(e.args[0])
            b = self.gen_expr(e.args[1])
            t = e.args[0].ty
            if t.is_float:
                a, b, tt = self.coerce(a, t, F64), self.coerce(b, t, F64), F64
                fn = "fmin" if name == "min" else "fmax"
            else:
                a, b, tt = self.coerce(a, t, I64), self.coerce(b, t, I64), I64
                fn = "fa_imin" if name == "min" else "fa_imax"
            return self.call2(fn, a, b, tt)
        if name == "random":
            return self.call0("fa_random", I64)
        if name == "free":
            # 释放 `new` 出来的（或 C 那边 malloc 的）指针。
            # 指向的对象如果自己有引用（结构体字段里的 str / Vec / Map、
            # 数组元素），先逐个还掉，再把这块内存还给 malloc —— 只调 libc 的
            # free 会把字段漏掉。
            v = self.gen_expr(e.args[0])
            inner = e.args[0].ty.inner
            if inner is not None and T.t_is_refcounted(inner):
                if inner.kind in ("struct", "enum", "arr"):
                    self.emit_rcdec_val(v, inner)      # 收「对象地址」
                else:
                    held = self.new_temp(inner)        # *str 这种：先取出指针
                    self.emit("LOAD", held, [v], extra=0, ty=inner)
                    self.emit_rcdec_val(held, inner)
            self.emit("CALL", None, [Sym("fa_free"), v])
            return self.const(0, VOID)
        if name == "chr":
            # 码点 -> UTF-8 字符串（1~4 字节）
            v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
            r = self.call1("fa_str_chr", v, STR)
            # 新字符串归本语句所有，收尾要释放。以前漏了这行，chr() 的结果
            # 在 print / 拼接以外没人管 —— 每调一次漏一个 FaStr。
            self.mark_owned(r, STR)
            return r
        if name in ("hex", "oct", "bin"):
            v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
            base = {"hex": 16, "oct": 8, "bin": 2}[name]
            r = self.new_temp(STR)
            self.emit("CALL", r, [Sym("fa_i64_base"), v, self.const(base),
                                  self.const(0)], ty=STR)
            self.mark_owned(r, STR)
            return r
        if name == "args":
            r = self.new_temp(vec_of(STR))
            self.emit("CALL", r, [Sym("fa_args")], ty=vec_of(STR))
            self.mark_owned(r, vec_of(STR))
            return r
        if name in ("round", "trunc"):
            v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, F64)
            return self.call1(name, v, F64)
        if name in ("log2", "log10", "exp2"):
            v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, F64)
            return self.call1(name, v, F64)
        if name == "hypot":
            a = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, F64)
            b = self.coerce(self.gen_expr(e.args[1]), e.args[1].ty, F64)
            return self.call2("hypot", a, b, F64)
        if name == "sign":
            a = self.gen_expr(e.args[0])
            if e.args[0].ty.is_float:
                return self.call1("fa_sign_f64",
                                  self.coerce(a, e.args[0].ty, F64), F64)
            return self.call1("fa_sign_i64",
                              self.coerce(a, e.args[0].ty, I64), I64)
        if name == "clamp":
            a = self.gen_expr(e.args[0])
            lo = self.gen_expr(e.args[1])
            hi = self.gen_expr(e.args[2])
            if e.args[0].ty.is_float:
                return self.call3("fa_clamp_f64",
                                  self.coerce(a, e.args[0].ty, F64),
                                  self.coerce(lo, e.args[1].ty, F64),
                                  self.coerce(hi, e.args[2].ty, F64), F64)
            return self.call3("fa_clamp_i64",
                              self.coerce(a, e.args[0].ty, I64),
                              self.coerce(lo, e.args[1].ty, I64),
                              self.coerce(hi, e.args[2].ty, I64), I64)
        if name in ("keys", "values"):
            m = self.gen_expr(e.args[0])
            mt = e.args[0].ty
            if mt.kind != "map":
                self.err(f"{name}() 需要 Map", e)
            et = mt.key if name == "keys" else mt.val
            k = elem_kind(et, self.sema)
            v = self.new_temp(vec_of(et))
            self.emit("CALL", v, [Sym("fa_vec_new"), self.const(k),
                                  self.const(vec_esz(et)),
                                  self.const(1 if (et.kind == "int" and et.is_signed) else 0),
                                  self.const(T.ty_code(et))],
                      ty=vec_of(et))
            self.mark_owned(v, vec_of(et))
            n = self.new_temp(I64)
            self.emit("CALL", n, [Sym("fa_map_len"), m], ty=I64)
            i = self.new_temp(I64)
            self.emit("MOV", i, [self.const(0)], ty=I64)
            top = self.new_label("keys")
            body = self.new_label("kbody")
            end = self.new_label("kend")
            self.emit("LABEL", extra=top)
            c = self.new_temp(BOOL)
            self.emit("CMP", c, [i, n], extra="<", ty=I64)
            self.emit("BR", args=[c], extra=(body, end))
            self.emit("LABEL", extra=body)
            raw = self.new_temp(I64)
            fn = "fa_map_key_at" if name == "keys" else "fa_map_val_at"
            self.emit("CALL", raw, [Sym(fn), m, i], ty=I64)
            self.emit("CALL", None, [Sym("fa_vec_push"), v, raw])
            self.emit("BIN", i, [i, self.const(1)], extra="+", ty=I64)
            self.emit("JMP", extra=top)
            self.emit("LABEL", extra=end)
            return v
        if name == "file_read":
            v = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
            r = self.new_temp(STR)
            self.emit("CALL", r, [Sym("fa_file_read"), v], ty=STR)
            self.mark_owned(r, STR)
            return r
        if name == "file_write":
            p = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
            c = self.gen_to_str(self.gen_expr(e.args[1]), e.args[1].ty)
            r = self.new_temp(I64)
            self.emit("CALL", r, [Sym("fa_file_write"), p, c], ty=I64)
            return r
        if name == "cmd":
            v = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
            r = self.new_temp(STR)
            self.emit("CALL", r, [Sym("fa_system_capture"), v], ty=STR)
            self.mark_owned(r, STR)
            return r
        if name == "env":
            v = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
            r = self.new_temp(STR)
            self.emit("CALL", r, [Sym("fa_env"), v], ty=STR)
            self.mark_owned(r, STR)
            return r
        if name == "concat":
            v = self.gen_expr(e.args[0])
            return self.gen_to_str(v, e.args[0].ty)
        if name == "gcd":
            a = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
            b = self.coerce(self.gen_expr(e.args[1]), e.args[1].ty, I64)
            return self.call2("fa_gcd", a, b, I64)
        self.err(f"未实现的内建函数 '{name}'", e)

    def _builtin_on_container(self, name: str, e: Call):
        """把 `sum(v)` 这类全局写法转发到 `v.sum()` 的实现"""
        if not e.args:
            self.err(f"{name}() 需要一个容器实参", e)
        a0 = e.args[0]
        t0 = a0.ty
        if t0 is not None and t0.kind not in ("vec", "arr", "map", "str"):
            self.err(f"{name}() 需要 Vec/Map/str 实参，得到 {t0}", e)
        mc = MethodCall(obj=a0, name=name, args=list(e.args[1:]),
                        resolved="builtin-method")
        mc.ty = e.ty
        mc.line, mc.col = e.line, e.col
        return self.gen_builtin_method(mc)

    def call0(self, fn, ty):
        r = self.new_temp(ty)
        self.emit("CALL", r, [Sym(fn)], ty=ty)
        return r

    def call1(self, fn, a, ty):
        r = self.new_temp(ty)
        self.emit("CALL", r, [Sym(fn), a], ty=ty)
        return r

    def call2(self, fn, a, b, ty):
        r = self.new_temp(ty)
        self.emit("CALL", r, [Sym(fn), a, b], ty=ty)
        return r

    def call3(self, fn, a, b, c, ty):
        r = self.new_temp(ty)
        self.emit("CALL", r, [Sym(fn), a, b, c], ty=ty)
        return r

    # ------------------------------------------------------------ 命名空间（py/java）
    def gen_ns_method(self, ns: str, e: MethodCall):
        name = e.name
        if ns == "py":
            self.mod.init_hooks.append(("py", None))
            if name == "import":
                a = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                return self.refcall("fa_py_import", [a], PYOBJ)
            if name == "eval":
                a = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                return self.refcall("fa_py_eval", [a], PYOBJ)
            if name == "exec":
                a = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                r = self.new_temp(I64)
                self.emit("CALL", r, [Sym("fa_py_exec"), a], ty=I64)
                return r
            if name == "call":
                o = self.gen_expr(e.args[0])
                m = self.gen_to_str(self.gen_expr(e.args[1]), e.args[1].ty)
                args = self.gen_expr(e.args[2]) if len(e.args) > 2 else self.const(0)
                return self.refcall("fa_py_callv", [o, m, args], PYOBJ)
            if name == "from_i64":
                v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, I64)
                return self.refcall("fa_py_from_i64", [v], PYOBJ)
            if name == "from_f64":
                v = self.coerce(self.gen_expr(e.args[0]), e.args[0].ty, F64)
                return self.refcall("fa_py_from_f64", [v], PYOBJ)
            if name == "from_str":
                v = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                return self.refcall("fa_py_from_str", [v], PYOBJ)
            if name == "from_list":
                v = self.gen_expr(e.args[0])
                return self.refcall("fa_py_from_vec", [v], PYOBJ)
            if name == "init":
                r = self.new_temp(I64)
                self.emit("CALL", r, [Sym("fa_py_init")], ty=I64)
                return r
            self.err(f"py 没有方法 '{name}'", e)
        if ns in ("java", "jvm"):
            self.mod.init_hooks.append(("java", None))
            if name == "init":
                a = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                r = self.new_temp(I64)
                self.emit("CALL", r, [Sym("fa_jvm_init"), a], ty=I64)
                return r
            if name == "class":
                a = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                return self.refcall("fa_jvm_find_class", [a], JOBJ)
            if name in ("call_i64", "call_f64", "call_obj", "call_void"):
                cls = self.gen_expr(e.args[0])
                mname = self.gen_to_str(self.gen_expr(e.args[1]), e.args[1].ty)
                sig = self.gen_to_str(self.gen_expr(e.args[2]), e.args[2].ty)
                n = len(e.args) - 3
                arr = self.emit_alloca(max(n, 1) * 8)
                for i in range(n):
                    a = e.args[3 + i]
                    av = self.gen_expr(a)
                    if a.ty is not None and a.ty.is_float:
                        # jvalue 是 union：double 必须按位写入，不能转成整数
                        self.emit("STORE", args=[arr, self.coerce(av, a.ty, F64)],
                                  extra=i * 8, ty=F64)
                    else:
                        self.emit("STORE", args=[arr, self.coerce(av, a.ty, I64)],
                                  extra=i * 8, ty=I64)
                fn = {"call_i64": "fa_jvm_call_static_i64",
                      "call_f64": "fa_jvm_call_static_f64",
                      "call_obj": "fa_jvm_call_static_obj",
                      "call_void": "fa_jvm_call_static_void"}[name]
                if name == "call_f64":
                    r = self.new_temp(F64)
                    self.emit("CALL", r, [Sym(fn), cls, mname, sig,
                                          self.const(n), arr], ty=F64)
                    return r
                if name == "call_void":
                    self.emit("CALL", None, [Sym(fn), cls, mname, sig,
                                             self.const(n), arr])
                    return self.const(0, VOID)
                rt = JOBJ if name == "call_obj" else I64
                r = self.new_temp(rt)
                self.emit("CALL", r, [Sym(fn), cls, mname, sig,
                                      self.const(n), arr], ty=rt)
                if rt == JOBJ:
                    self.mark_owned(r, JOBJ)
                return r
            if name == "str":
                a = self.gen_to_str(self.gen_expr(e.args[0]), e.args[0].ty)
                return self.refcall("fa_jvm_str", [a], JOBJ)
            if name == "new":
                cls = self.gen_expr(e.args[0])
                sig = self.gen_to_str(self.gen_expr(e.args[1]), e.args[1].ty)
                n = len(e.args) - 2
                arr = self.emit_alloca(max(n, 1) * 8)
                for i in range(n):
                    av = self.gen_expr(e.args[2 + i])
                    self.emit("STORE", args=[arr, self.coerce(av, e.args[2 + i].ty, I64)],
                              extra=i * 8, ty=I64)
                return self.refcall("fa_jvm_new_obj", [cls, sig, self.const(n), arr], JOBJ)
            self.err(f"java 没有方法 '{name}'", e)
        self.err(f"未知命名空间 '{ns}'", e)

    def refcall(self, fn, args, ty):
        r = self.new_temp(ty)
        self.emit("CALL", r, [Sym(fn)] + args, ty=ty)
        self.mark_owned(r, ty)
        return r

    # ------------------------------------------------------------ 类型转换
    def coerce(self, v, src: Type, dst: Type):
        if src is None or dst is None or src == dst:
            return v
        if dst.kind == "any" or src.kind == "any":
            return v
        # 字符串化（str(x) / 字符串插值上下文）
        if dst == STR and src != STR:
            return self.gen_to_str(v, src)
        # C 互操作：str <-> char*
        if src == STR and dst.kind == "ptr":
            return self.call1("fa_str_cstr", v, dst)
        if src.kind == "ptr" and dst == STR:
            r = self.call1("fa_str_from_cstr", v, STR)
            self.mark_owned(r, STR)
            return r
        # 字符串解析（i64("123") / f64("1.5")）
        if src == STR and dst.kind == "int":
            return self.call1("fa_str_to_i64", v, dst if dst.size == 8 else I64)
        if src == STR and dst.kind == "float":
            return self.call1("fa_str_to_f64", v, F64)
        if src.is_num and dst.is_num:
            r = self.new_temp(dst)
            self.emit("CONV", r, [v], extra=src, ty=dst)
            return r
        if src.kind == "bool" and dst.kind in ("int", "float"):
            r = self.new_temp(dst)
            self.emit("CONV", r, [v], extra=src, ty=dst)
            return r
        if src == CHAR and dst.kind in ("int", "float"):
            r = self.new_temp(dst)
            self.emit("CONV", r, [v], extra=src, ty=dst)
            return r
        if dst.kind == "bool" and src.kind == "int":
            c = self.new_temp(BOOL)
            self.emit("CMP", c, [self.coerce(v, src, I64), self.const(0)],
                      extra="!=", ty=I64)
            return c
        if dst.kind == "ptr" and src.kind == "int":
            return v
        if dst.kind == "int" and src.kind == "ptr":
            return v
        if dst.kind == "ptr" and src.kind == "ptr":
            return v
        return v


# ---------------------------------------------------------------- 模块级
def generate(sema: Sema) -> IRModule:
    mod = IRModule()
    mod.descs = sema.descs
    # 顶层 let 的存储：(汇编标签, 字节数)。asmgen 在 .bss 里逐个开槽。
    mod.gvar_slots = []
    for d in getattr(sema, "global_decls", []):
        if d.sym is None:
            continue                          # 语义阶段已经报过错
        gt = d.sym.ty
        mod.gvar_slots.append((d.sym.label, max(gt.size if is_agg(gt) else 8, 8)))
    for sym, body, params in getattr(sema, "fn_bodies_plain", []):
        pass
    for item in sema.fn_bodies:
        if len(item) == 3:
            sym, body, params = item
            self_type = None
        else:
            sym, body, params, self_type = item
        g = FnGen(sema, mod, sym, body, params, self_type)
        mod.funcs.append(g.gen())
    return mod
