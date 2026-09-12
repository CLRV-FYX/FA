"""FA 寄存器分配：线性扫描（Linear Scan）+ 溢出 + 调用点活跃值保存。

寄存器分工（x86-64 System V）
----------------------------
* 可分配通用寄存器：rbx r12 r13 r14 r15（被调用者保存） + r10 r11（调用者保存）
* 暂存寄存器（永不存放跨指令活跃值）：rax rcx rdx rsi rdi r8 r9
* 浮点池：xmm0..xmm7（全部调用者保存，跨调用需保存）
"""

from __future__ import annotations
import bisect
from typing import List, Dict, Tuple, Optional
from .ir import Temp, Const, Sym, StrConst, Label, Instr, IRFunc, instr_uses
from .types import Type

GP_REGS = ["rbx", "r12", "r13", "r14", "r15", "r10", "r11"]
FP_REGS = ["xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7"]
SCRATCH = ["rax", "rcx", "rsi", "rdi", "r8", "r9"]
VOLATILE_GP = {"r10", "r11"}
CALLEE_SAVED = ["rbx", "r12", "r13", "r14", "r15"]


def is_float_ty(ty: Optional[Type]) -> bool:
    return ty is not None and ty.kind == "float"


def temps_in(instr: Instr):
    """指令用到的（非定义）临时变量——含 extra 元组里的比例变址下标"""
    return instr_uses(instr)


def def_temp(instr: Instr):
    return instr.dst if isinstance(instr.dst, Temp) else None


def compute_intervals(fn: IRFunc) -> Dict[int, Tuple[int, int]]:
    iv: Dict[int, Tuple[int, int]] = {}
    for i, ins in enumerate(fn.instrs):
        ids = set()
        d = def_temp(ins)
        if d is not None:
            ids.add(d.id)
        for t in temps_in(ins):
            ids.add(t.id)
        for tid in ids:
            if tid in iv:
                s, e = iv[tid]
                iv[tid] = (min(s, i), max(e, i))
            else:
                iv[tid] = (i, i)
    return extend_for_loops(fn, iv)


def extend_for_loops(fn: IRFunc, iv: Dict[int, Tuple[int, int]]) -> Dict[int, Tuple[int, int]]:
    """线性扫描按「指令顺序」计算区间，遇到循环回边会低估活跃范围
    （例如循环上界变量在循环体内被复用）。这里对回边区域做保守扩张直到不动点。"""
    labels = {}
    for idx, ins in enumerate(fn.instrs):
        if ins.op == "LABEL" and isinstance(ins.extra, str):
            labels.setdefault(ins.extra, idx)

    changed = True
    guard = 0
    while changed and guard < 64:
        changed = False
        guard += 1
        for idx, ins in enumerate(fn.instrs):
            targets = []
            if ins.op == "JMP" and isinstance(ins.extra, str):
                targets = [ins.extra]
            elif ins.op == "BR" and isinstance(ins.extra, (tuple, list)):
                targets = [t for t in ins.extra if isinstance(t, str)]
            for t in targets:
                L = labels.get(t)
                if L is None or L > idx:
                    continue                      # 不是回边
                for tid, (s, e) in list(iv.items()):
                    # 只有当「跨过循环头部」或「跨过回边」时才需要扩张：
                    #   - 循环头之前定义、循环体内还在用  -> s < L <= e
                    #   - 循环体内定义、回边之后还要用    -> s <= idx < e
                    # 完全生灭在循环体内部的临时值不该被拉长，否则会白白耗尽寄存器。
                    live_into_loop = (s < L <= e)
                    live_out_of_loop = (s <= idx < e)
                    if not (live_into_loop or live_out_of_loop):
                        continue
                    ns, ne = min(s, L), max(e, idx)
                    if (ns, ne) != (s, e):
                        iv[tid] = (ns, ne)
                        changed = True
    return iv


def lower_params(fn: IRFunc):
    """把「预着色/栈上传参」的形参搬到普通虚拟寄存器，简化后续分配。"""
    new_instrs: List[Instr] = []
    mapping: Dict[int, Temp] = {}
    for p in fn.params:
        if getattr(p, "fixed", None):
            nt = Temp(-p.id - 100000, p.ty)
            nt.id = 900000 + p.id
            mapping[p.id] = nt
            if str(p.fixed).startswith("stack:"):
                off = 16 + 8 * int(str(p.fixed).split(":")[1])
                new_instrs.append(Instr("LOADPARAM", nt, extra=off, ty=p.ty))
            elif str(p.fixed).startswith("xmm"):
                new_instrs.append(Instr("MOV", nt, [Reg(str(p.fixed))], ty=p.ty))
            else:
                new_instrs.append(Instr("MOV", nt, [Reg(str(p.fixed))], ty=p.ty))
        else:
            mapping[p.id] = p

    def repl(v):
        if isinstance(v, Temp) and v.id in mapping:
            return mapping[v.id]
        return v

    for ins in fn.instrs:
        ins.dst = repl(ins.dst)
        ins.args = [repl(a) for a in ins.args]
        if isinstance(ins.extra, Temp):
            ins.extra = repl(ins.extra)
        new_instrs.append(ins)
    fn.instrs = new_instrs
    fn.params = [mapping[p.id] for p in fn.params]
    fn.param_map = mapping
    return fn


class Reg:
    """表示一个固定的物理寄存器（用于 lower_params 搬移）"""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f"%{self.name}"


class Interval:
    __slots__ = ("tid", "start", "end", "reg", "spilled", "ty")

    def __init__(self, tid, start, end, ty=None):
        self.tid = tid
        self.start = start
        self.end = end
        self.reg = None
        self.spilled = False
        self.ty = ty



# 「d 可以先拿 a 的寄存器」的指令形态：
#   MOV d, a          纯复制
#   BIN d, a, b       d = a op b（a 之后再不用就可以直接改 a）
#   UN  d, a          d = op a
# 注意 CONV（类型转换）会改变值，不能合并。
COPY_LIKE_OPS = ("MOV", "BIN", "UN", "SHIFT")


def collect_copies(fn: IRFunc, iv):
    """收集可合并的 (dst_id -> src_id)：src 用完后即死，dst 可直接复用其寄存器"""
    copies: Dict[int, int] = {}
    for idx, ins in enumerate(fn.instrs):
        if ins.op not in COPY_LIKE_OPS:
            continue
        if ins.op == "MOV" and ins.extra is not None:
            continue                       # 带 extra 的 MOV 不是纯复制
        d = ins.dst
        a = ins.args[0] if ins.args else None
        if not isinstance(d, Temp) or not isinstance(a, Temp):
            continue
        if d.id == a.id:
            continue
        if is_float_ty(d.ty) != is_float_ty(a.ty):
            continue
        se = iv.get(a.id)
        if se is None or se[1] != idx:     # 必须是 a 的最后一次使用
            continue
        if d.id in copies:
            continue
        copies[d.id] = a.id
    return copies


def allocate(fn: IRFunc):
    """线性扫描分配，返回 (loc, spills, intervals, used_callee)"""
    iv = compute_intervals(fn)
    tys: Dict[int, Type] = {}
    for ins in fn.instrs:
        d = def_temp(ins)
        if d is not None and d.id not in tys:
            tys[d.id] = d.ty
        for t in temps_in(ins):
            if t.id not in tys:
                tys[t.id] = t.ty

    ints = [Interval(tid, s, e, tys.get(tid)) for tid, (s, e) in iv.items()]
    ints.sort(key=lambda x: x.start)

    # 复制合并：MOV d, s 且 s 在此用完后即死 -> d 直接复用 s 的寄存器
    copies = collect_copies(fn, iv)
    by_id: Dict[int, Interval] = {it.tid: it for it in ints}

    # 调用点索引：用于判断某个值的活跃区间是否跨越调用
    call_idx = [i for i, ins in enumerate(fn.instrs)
                if ins.op in ("CALL", "CALLPTR")]

    def crosses_call(itv) -> bool:
        if not call_idx:
            return False
        # 值必须在调用「之后」还要用才算跨调用存活
        # （最后使用点就是 call 本身时，说明它只是实参，调用后就死了）
        i = bisect.bisect_left(call_idx, itv.start)
        return i < len(call_idx) and call_idx[i] < itv.end

    # 寄存器池分成两类：
    #   跨调用存活的值 -> 先用被调用者保存寄存器（免得每次调用都要 save/restore）
    #   不跨调用的值   -> 先用易失寄存器 r10/r11（免得函数开头 push）
    gp_callee = [r for r in GP_REGS if r not in VOLATILE_GP]
    gp_vol = [r for r in GP_REGS if r in VOLATILE_GP]

    gp_free = list(GP_REGS)
    fp_free = list(FP_REGS)
    active: List[Interval] = []

    def pool_of(itv):
        if is_float_ty(itv.ty):
            return fp_free
        a, b = (gp_callee, gp_vol) if crosses_call(itv) else (gp_vol, gp_callee)
        return a if a else b

    def release(itv):
        """把区间占用的寄存器还回对应的池"""
        if itv.reg is None:
            return
        if itv.reg.startswith("xmm"):
            fp_free.append(itv.reg)
        elif itv.reg in VOLATILE_GP:
            gp_vol.append(itv.reg)
        else:
            gp_callee.append(itv.reg)

    def active_pool(itv):
        return [a for a in active if is_float_ty(a.ty) == is_float_ty(itv.ty)]

    for itv in ints:
        # 回收已结束的区间
        keep = []
        for a in active:
            if a.end < itv.start:
                release(a)
            else:
                keep.append(a)
        active = keep
        sid = copies.get(itv.tid)
        src = by_id.get(sid) if sid is not None else None
        if (src is not None and src is not itv and not src.spilled
                and src.reg and src.end == itv.start):
            # 合并成功：dst 与 src 共用一个寄存器，MOV 变成空操作
            itv.reg = src.reg
            if src in active:
                active.remove(src)          # src 在本条 MOV 之后即死
            active.append(itv)
            active.sort(key=lambda x: x.end)
            continue
        pool = pool_of(itv)
        act = active_pool(itv)
        if pool:
            itv.reg = pool.pop(0)
            active.append(itv)
            active.sort(key=lambda x: x.end)
        elif act and act[-1].end > itv.end:
            victim = act[-1]
            active.remove(victim)
            itv.reg = victim.reg
            victim.spilled = True
            victim.reg = None
            active.append(itv)
            active.sort(key=lambda x: x.end)
        else:
            itv.spilled = True

    loc: Dict[int, str] = {}
    spills: Dict[int, int] = {}
    slot = 0
    for itv in ints:
        if itv.spilled:
            size = 8
            ty = itv.ty
            if ty is not None and ty.kind == "struct":
                size = 8
            slot += size
            spills[itv.tid] = slot
        else:
            loc[itv.tid] = itv.reg
    used_callee = sorted({r for r in loc.values() if r in CALLEE_SAVED},
                         key=lambda r: CALLEE_SAVED.index(r))
    fn.intervals = iv
    fn.alloc_slot_bytes = slot
    return loc, spills, iv, used_callee
