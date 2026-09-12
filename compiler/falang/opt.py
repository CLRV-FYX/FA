"""FA 中端优化：在寄存器分配之前、IR 仍是「虚拟寄存器 + 线性指令表」时运行的通道。

目前实现：
  * licm()  —— 循环不变量外提（Loop-Invariant Code Motion）

设计要点
--------
* 循环通过「回边」识别：跳向更靠前位置的 JMP / BR 目标即循环头，
  循环体是 [循环头, 回边] 这段闭区间。最内层循环体最短，优先处理。
* 只有**单一定义**的临时变量才允许外提。变量（let 声明的名字）在循环里会被反复
  赋值，它的 Temp 有多处定义，因此天然被排除在外。
* 内存读取（LOAD）的外提需要别名分析。核心假设：

      ** 通过 LOAD 取出的指针指向「另一个对象」，不会与宿主对象的头部字节重叠。**

  这条假设对 FA 的所有容器都成立：Vec/Map 的元素缓冲区是独立 malloc 出来的，
  结构体里的指针字段指向别的对象。而通过 LEA 取到的指针（数组元素、结构体字段）
  指向宿主对象内部，因此写它会同时污染宿主对象。
* 未知副作用的函数调用会「污染一切内存」，直接放弃该循环的 LOAD 外提；
  但保证不返回的运行时函数（越界报错、panic）例外——它们执行不到调用点之后。
* 零次迭代的循环也会执行前插块里的代码。被外提的 LOAD 地址是
  「循环外就存在、由 FA 语义保证非空的引用 + 宿主对象内的小常量偏移」，
  因此不会引入原本不存在的缺页错误。
"""

from __future__ import annotations
from typing import Dict, List, Set, Tuple, Optional
from .ir import Instr, IRFunc, Temp, Const, Sym, StrConst, instr_uses

# 纯计算指令：操作数不变则结果不变，且不读内存、不写内存
PURE_OPS = {"BIN", "UN", "SHIFT", "CONV", "CMP", "MOV", "LEA", "LEA_SYM", "BITCAST"}

# 保证不返回、因此不可能「调用之后继续观察内存」的运行时函数
NORETURN_FNS = {"fa_bounds_error", "fa_panic", "fa_out_of_memory", "fa_nil_deref"}

# 会写内存、且写入地址无法静态确定的指令
WRITE_OPS = {"STORE", "MEMCPY", "ZERO", "RCINC", "RCDEC"}

MAX_ROUNDS = 64


def _targets(ins: Instr) -> List[str]:
    if ins.op == "JMP" and isinstance(ins.extra, str):
        return [ins.extra]
    if ins.op == "BR" and isinstance(ins.extra, (tuple, list)):
        return [t for t in ins.extra if isinstance(t, str)]
    return []


def _label_pos(instrs: List[Instr]) -> Dict[str, int]:
    pos: Dict[str, int] = {}
    for i, ins in enumerate(instrs):
        if ins.op == "LABEL" and isinstance(ins.extra, str):
            pos.setdefault(ins.extra, i)
    return pos


def _find_loops(instrs: List[Instr]) -> List[Tuple[int, int, str]]:
    """返回 [(循环头位置, 回边位置, 循环头标签名)]，按循环体长度升序（最内层优先）"""
    pos = _label_pos(instrs)
    loops: List[Tuple[int, int, str]] = []
    for i, ins in enumerate(instrs):
        for t in _targets(ins):
            j = pos.get(t)
            if j is not None and j <= i:
                loops.append((j, i, t))
    loops.sort(key=lambda x: x[1] - x[0])
    return loops


def _def_sites(instrs: List[Instr]) -> Dict[int, List[int]]:
    d: Dict[int, List[int]] = {}
    for i, ins in enumerate(instrs):
        if isinstance(ins.dst, Temp):
            d.setdefault(ins.dst.id, []).append(i)
    return d


def _operand_temps(ins: Instr) -> List[Temp]:
    return instr_uses(ins)


def _points_into(tid: int, defs_by_temp: Dict[int, Instr]) -> Set[int]:
    """t 可能「指向内部」的对象集合。

    LEA 取的是宿主对象内部的地址 -> 把宿主也算进来（并递归）；
    而 LOAD 取出的指针按前述假设指向另一个对象，不再向上传递。
    """
    seen: Set[int] = set()
    stack = [tid]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        ins = defs_by_temp.get(cur)
        if ins is None:
            continue
        if ins.op == "LEA" and ins.args and isinstance(ins.args[0], Temp):
            stack.append(ins.args[0].id)
        elif ins.op in ("MOV", "BITCAST", "CONV") and ins.args \
                and isinstance(ins.args[0], Temp):
            stack.append(ins.args[0].id)
    return seen


def _callee_name(ins: Instr) -> Optional[str]:
    a = ins.args[0] if ins.args else None
    if isinstance(a, Sym):
        return a.name
    return None


def _analyze_loop(instrs: List[Instr], L: int, R: int,
                  defs_by_temp: Dict[int, Instr], single_def: Set[int],
                  def_sites: Dict[int, List[int]],
                  undef_temps: Set[int]) -> Tuple[List[int], Set[int]]:
    """返回 (可外提的指令下标集合, 需要保持不变的指令下标集合)"""
    # 1) 内存写入集合：{(基址 temp, 偏移 or None)}
    writes: Set[Tuple[int, Optional[int]]] = set()
    clobbers_all = False
    for idx in range(L, R + 1):
        ins = instrs[idx]
        if ins.op in ("CALL", "CALLPTR"):
            if ins.op == "CALL" and _callee_name(ins) in NORETURN_FNS:
                continue                       # 不返回，观察不到副作用
            clobbers_all = True
        elif ins.op == "ASM":
            clobbers_all = True
        elif ins.op == "STORE":
            base = ins.args[0] if ins.args else None
            if isinstance(base, Temp):
                off = ins.extra if isinstance(ins.extra, int) else None
                writes.add((base.id, off))
                for p in _points_into(base.id, defs_by_temp):
                    writes.add((p, None))
        elif ins.op in ("MEMCPY", "ZERO"):
            base = ins.args[0] if ins.args else None
            if isinstance(base, Temp):
                for p in _points_into(base.id, defs_by_temp) | {base.id}:
                    writes.add((p, None))
        elif ins.op in ("RCINC", "RCDEC"):
            base = ins.args[0] if ins.args else None
            if isinstance(base, Temp):
                # 引用计数可能触发释放，宿主对象整体都算被污染
                for p in _points_into(base.id, defs_by_temp) | {base.id}:
                    writes.add((p, None))

    # 2) 不动点：不断把「操作数已不变的指令」纳入不变集合
    invariant: Set[int] = set()          # 循环内定义的、已判定为不变的 Temp
    hoistable: Set[int] = set()          # 指令下标

    def is_invariant(t: Temp) -> bool:
        return t.id in invariant or t.id in single_def_outside

    # 循环外定义的、以及根本没有定义点的（形参 / 预着色寄存器）都算不变
    single_def_outside: Set[int] = set(undef_temps)
    for tid, sites in def_sites.items():
        if len(sites) == 1 and sites[0] < L:
            single_def_outside.add(tid)

    changed = True
    guard = 0
    while changed and guard < MAX_ROUNDS:
        changed = False
        guard += 1
        for idx in range(L, R + 1):
            if idx in hoistable:
                continue
            ins = instrs[idx]
            if not isinstance(ins.dst, Temp):
                continue
            if ins.dst.id not in single_def:
                continue                       # 多处定义（变量），不外提
            if ins.op in PURE_OPS:
                if all(is_invariant(t) for t in _operand_temps(ins)):
                    hoistable.add(idx)
                    invariant.add(ins.dst.id)
                    changed = True
            elif ins.op == "LOAD":
                if clobbers_all:
                    continue
                base = ins.args[0] if ins.args else None
                if not isinstance(base, Temp):
                    continue
                # 基址 **和** 偏移（可能在 extra 里是另一个 Temp）都必须循环不变
                if not all(is_invariant(t) for t in _operand_temps(ins)):
                    continue
                off = ins.extra if isinstance(ins.extra, int) else None
                if (base.id, off) in writes or (base.id, None) in writes:
                    continue
                if any(p in {b for b, _ in writes} for p in
                       _points_into(base.id, defs_by_temp)):
                    continue
                hoistable.add(idx)
                invariant.add(ins.dst.id)
                changed = True
    return sorted(hoistable), hoistable


def _hoist(fn: IRFunc, L: int, R: int, header: str, hoistable: Set[int]) -> bool:
    """把 hoistable 里的指令搬到循环头之前的前插块。返回是否真的改动了。"""
    if not hoistable:
        return False
    instrs = fn.instrs
    pre = f"{header}_pre{fn.npre}"
    fn.npre += 1

    moved = [instrs[i] for i in sorted(hoistable)]
    keep = [ins for i, ins in enumerate(instrs) if i not in hoistable]

    # keep 里循环头的位置（因为删掉了若干条指令）
    shift = sum(1 for i in hoistable if i < L)
    new_L = L - shift

    # 循环外指向循环头的跳转改指向前插块（循环内的回边保持指向循环头）
    for i, ins in enumerate(keep):
        if i < new_L and ins.op == "JMP" and ins.extra == header:
            ins.extra = pre
        elif i < new_L and ins.op == "BR" and isinstance(ins.extra, (tuple, list)):
            ins.extra = tuple(pre if t == header else t for t in ins.extra)

    block: List[Instr] = [Instr("JMP", extra=pre), Instr("LABEL", extra=pre)] + moved
    fn.instrs = keep[:new_L] + block + keep[new_L:]
    return True


def licm(fn: IRFunc) -> bool:
    """对单个函数做循环不变量外提。返回是否发生过改动。"""
    if not fn.instrs or fn.extern:
        return False
    if not hasattr(fn, "npre"):
        fn.npre = 0
    changed_any = False
    rounds = 0
    while rounds < MAX_ROUNDS:
        rounds += 1
        loops = _find_loops(fn.instrs)
        if not loops:
            break
        defs_by_temp: Dict[int, Instr] = {}
        for ins in fn.instrs:
            if isinstance(ins.dst, Temp) and ins.dst.id not in defs_by_temp:
                defs_by_temp[ins.dst.id] = ins
        sites = _def_sites(fn.instrs)
        single_def = {tid for tid, s in sites.items() if len(s) == 1}
        # 形参 / 预着色寄存器没有定义点，天然不变
        undef_temps: Set[int] = set()
        for ins in fn.instrs:
            for a in _operand_temps(ins):
                if a.id not in sites:
                    undef_temps.add(a.id)
        progress = False
        for (L, R, header) in loops:
            _, hoistable = _analyze_loop(fn.instrs, L, R, defs_by_temp, single_def,
                                         sites, undef_temps)
            if _hoist(fn, L, R, header, hoistable):
                progress = True
                changed_any = True
                break            # 位置变了，重新分析
        if not progress:
            break
    return changed_any
