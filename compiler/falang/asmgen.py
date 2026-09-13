"""FA: IR -> x86-64 汇编（GNU as, Intel 语法） System V AMD64 ABI。"""

from __future__ import annotations
import struct
from typing import List, Dict, Optional, Tuple
from .ir import Temp, Const, Sym, StrConst, Label, Instr, IRFunc, IRModule
from . import types as T
from .types import Type, TYPES
from .regalloc import (allocate, lower_params, Reg, GP_REGS, FP_REGS, SCRATCH,
                       VOLATILE_GP, CALLEE_SAVED, compute_intervals, is_float_ty)
from .sema import Sema

I64 = TYPES["i64"]
INT_REGS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]

REG8 = {"rax": ("rax", "eax", "ax", "al"), "rbx": ("rbx", "ebx", "bx", "bl"),
        "rcx": ("rcx", "ecx", "cx", "cl"), "rdx": ("rdx", "edx", "dx", "dl"),
        "rsi": ("rsi", "esi", "si", "sil"), "rdi": ("rdi", "edi", "di", "dil"),
        "rbp": ("rbp", "ebp", "bp", "bpl"), "rsp": ("rsp", "esp", "sp", "spl"),
        "r8": ("r8", "r8d", "r8w", "r8b"), "r9": ("r9", "r9d", "r9w", "r9b"),
        "r10": ("r10", "r10d", "r10w", "r10b"), "r11": ("r11", "r11d", "r11w", "r11b"),
        "r12": ("r12", "r12d", "r12w", "r12b"), "r13": ("r13", "r13d", "r13w", "r13b"),
        "r14": ("r14", "r14d", "r14w", "r14b"), "r15": ("r15", "r15d", "r15w", "r15b")}

JCC_S = {"==": "je", "!=": "jne", "<": "jl", "<=": "jle", ">": "jg", ">=": "jge"}
JCC_U = {"==": "je", "!=": "jne", "<": "jb", "<=": "jbe", ">": "ja", ">=": "jae"}
JCC_INV = {"je": "jne", "jne": "je", "jl": "jge", "jge": "jl", "jle": "jg",
           "jg": "jle", "jb": "jae", "jae": "jb", "jbe": "ja", "ja": "jbe"}

SETCC = {"==": "sete", "!=": "setne", "<": "setl", "<=": "setle",
         ">": "setg", ">=": "setge"}
SETCC_U = {"==": "sete", "!=": "setne", "<": "setb", "<=": "setbe",
           ">": "seta", ">=": "setae"}


def _is_imm(s) -> bool:
    """操作数字符串是否是立即数（而非寄存器名）"""
    if not isinstance(s, str) or not s:
        return False
    if s[0].isalpha() or s[0] == "%":
        return False
    try:
        int(s, 0)
        return True
    except ValueError:
        return False


def rn(reg: str, size: int) -> str:
    t = REG8.get(reg)
    if t is None:
        return reg
    return {8: t[0], 4: t[1], 2: t[2], 1: t[3]}[size]


def align16(n: int) -> int:
    return (n + 15) & ~15


class Ctx:
    def __init__(self):
        self.used = set()
        self.i = 0

    def scratch(self, extra=()):
        for r in SCRATCH:
            if r not in self.used and r not in extra:
                self.used.add(r)
                return r
        for r in ["rdx", "r10", "r11"]:
            if r not in self.used and r not in extra:
                self.used.add(r)
                return r
        self.i += 1
        r = f"rax#{self.i}"
        self.used.add(r)
        return r

    def xmm_scratch(self):
        for i in range(8, 16):
            r = f"xmm{i}"
            if r not in self.used:
                self.used.add(r)
                return r
        return "xmm15"


class AsmGen:
    def __init__(self, mod: IRModule, sema: Sema, opt: int = 2):
        self.mod = mod
        self.sema = sema
        self.opt = opt
        self.out: List[str] = []
        self.float_consts: Dict[float, str] = {}
        self.cur: Optional[IRFunc] = None
        self.loc: Dict[int, str] = {}
        self.spills: Dict[int, int] = {}
        self.alloca_off: Dict[int, int] = {}
        self.save_slots: Dict[int, int] = {}
        self.intervals: Dict[int, Tuple[int, int]] = {}
        self.nlabel = 0

    # ---------------------------------------------------------------- 工具
    def L(self, s: str):
        self.out.append("    " + s)

    def R(self, s: str):
        self.out.append(s)

    def new_label(self, p="L"):
        self.nlabel += 1
        return f".fa{p}{self.nlabel}"

    def esc(self, s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    def asciz(self, s: str) -> str:
        b = s.encode("utf-8")
        parts = []
        for ch in b:
            if ch == 0:
                parts.append("0")
            elif 32 <= ch < 127 and ch not in (0x22, 0x5c):
                parts.append(f"'{chr(ch)}'")
            else:
                parts.append(str(ch))
        return ", ".join(parts) if parts else "0"

    # ---------------------------------------------------------------- 入口
    def emit(self) -> str:
        o = self.out
        self.R("/* 由 FA 编译器生成 —— 请勿手工修改 */")
        self.R("    .intel_syntax noprefix")
        self.R("    .text")
        # 外部符号声明
        externs = set()
        for f in self.mod.funcs:
            for ins in f.instrs:
                if ins.op in ("CALL",) and isinstance(ins.args[0], Sym):
                    externs.add(ins.args[0].name)
                if ins.op == "CALLPTR":
                    pass
        for e in sorted(externs):
            if e.startswith("fa_") or e in ("memcpy", "memset", "strlen", "pow",
                                            "fabs", "labs", "fmin", "fmax", "sin",
                                            "cos", "tan", "log", "exp", "floor",
                                            "ceil", "sqrt"):
                self.R(f"    .extern {e}")
        # 字符串常量
        if self.mod.strings:
            self.R("    .section .rodata")
            for i, s in enumerate(self.mod.strings):
                self.R(f"__fa_strc_{i}:")
                self.R("    .quad -1                      /* rc = -1 -> 永生 */")
                self.R(f"    .quad {len(s.encode('utf-8'))}")
                self.R(f"    .byte {self.asciz(s)}")
                self.R("    .byte 0")
        # 浮点常量（延迟收集）
        self.float_consts = {}
        # 结构体 RC 描述符
        if self.mod.descs:
            self.R("    .section .data")
            for t in self.mod.descs:
                self.R(f"__fa_desc_{t.name}:")
                # 枚举没有扁平字段表（载荷偏移取决于 tag），它的释放/加引用
                # 完全由下面生成的 __fa_drop_/__fa_retain_ 函数负责，这里只留终止符。
                for fname, fty, off in (t.fields or []):
                    k = fty.rc_kind
                    if k == 0 and fty.kind == "struct" and fty.is_refcounted:
                        k = 1000 + fty.desc_id
                    if k != 0:
                        self.R(f"    .quad {off}")
                        self.R(f"    .quad {k}")
                self.R("    .quad -1")
        # 懒绑定符号全局槽
        if getattr(self.sema, "lazy_syms", None):
            self.R("    .section .data")
            for name, path in self.sema.lazy_syms:
                self.R(f"__fa_lazy_{name}:")
                self.R("    .quad 0")
                self.R(f"__fa_lazy_name_{name}:")
                self.R(f"    .asciz \"{name}\"")
            self.R("__fa_lazy_lib:")
            self.R(f"    .asciz \"{self.sema.lazy_syms[0][1]}\"")
        # 顶层 let 的全局变量槽（.bss 天然清零，所以「没写初值」就等于零值）
        gvars = getattr(self.mod, "gvar_slots", None)
        if gvars:
            self.R("    .section .bss")
            self.R("    .align 8")
            for label, size in gvars:
                self.R(f"{label}:")
                self.R(f"    .zero {size}")
        # 函数
        self.R("    .text")
        for f in self.mod.funcs:
            self.emit_func(f)
        # 结构体/枚举的析构与 retain 函数
        for t in self.mod.descs:
            self.emit_drop_func(t)
            self.emit_retain_func(t)
        # 模块初始化（.init_array）
        self.emit_init()
        # 浮点常量区
        if self.float_consts:
            self.R("    .section .rodata")
            for val, lbl in self.float_consts.items():
                bits = struct.unpack("<Q", struct.pack("<d", val))[0]
                self.R(f"{lbl}:")
                self.R(f"    .quad {bits}")
        self.R("    .section .note.GNU-stack,\"\",@progbits")
        return "\n".join(o) + "\n"

    # ------------------------------------------------- 析构 / retain 函数
    # 结构体是值类型，但可能含有引用计数字段（str / Vec / Map / 嵌套 struct）。
    # 编译器为每个这类的结构体生成两个函数：
    #   __fa_drop_<T>    逐字段释放一次引用（作用域结束 / 覆盖赋值时调用）
    #   __fa_retain_<T>  逐字段各加一次引用（**拷贝**结构体时调用）
    # 有 drop 没 retain，`let b = a` 就会让两个变量共享同一份引用而不加计数
    # —— 作用域结束时双重释放（实测 `free(): double free detected`）。
    #
    # 两个函数都必须把结构体基址放进 rbx：循环里每次 `call` 之后 rdi 都是垃圾
    # （rdi 是调用者保存寄存器），以前直接用 [rdi+off] 读第二个字段 -> 段错误。
    def _field_ops(self, t: Type, base: str, mode: str, fields=None,
                   add: int = 0, tag: str = ""):
        """生成对每个引用计数字段的 inc/dec 指令。mode = 'drop' | 'retain'

        fields/add/tag 给枚举用：枚举的字段偏移是「相对载荷区」的，
        要加上 8 字节的 tag，而且只能处理当前变体的那些字段。
        """
        for fname, fty, foff in (t.fields if fields is None else fields):
            off = foff + add
            lbl = f".fa{'d' if mode == 'drop' else 'r'}{t.name}_{tag}{off}"
            if fty.kind == "arr" and T.t_is_refcounted(fty.elem):
                # 数组字段：交给运行时按元素类型循环处理
                fn = "fa_drop_arr" if mode == "drop" else "fa_retain_arr"
                ek = fty.elem.rc_kind
                nested = (fty.elem.kind == "struct" and fty.elem.is_refcounted)
                self.R(f"    lea rdi, [{base}+{off}]")
                self.R(f"    mov rsi, {fty.count}")
                self.R(f"    mov rdx, {max(fty.elem.size, 1)}")
                self.R(f"    mov rcx, {0 if nested else ek}")
                if nested:
                    self.R(f"    lea r8, [rip+__fa_drop_{fty.elem.name}]"
                           if mode == "drop" else
                           f"    lea r8, [rip+__fa_retain_{fty.elem.name}]")
                else:
                    self.R("    xor r8d, r8d")
                self.R(f"    call {fn}")
                continue
            k = fty.rc_kind
            nested_struct = (k == 0 and fty.kind == "struct" and fty.is_refcounted)
            if k == 0 and not nested_struct:
                continue
            if nested_struct:
                # 嵌套结构体是**内联**的：地址就是 base+off，不是 [base+off]
                # （以前这里 mov rax,[rdi+off] 取到的是内层结构体的第一个字段）
                callee = f"__fa_drop_{fty.name}" if mode == "drop" else f"__fa_retain_{fty.name}"
                self.R(f"    lea rdi, [{base}+{off}]")
                self.R(f"    call {callee}")
                continue
            self.R(f"    mov rax, [{base}+{off}]")
            self.R("    test rax, rax")
            self.R(f"    jz {lbl}")
            self.R("    mov rdi, rax")
            if mode == "drop":
                self.R(f"    mov rsi, {k}")
                self.R("    call fa_rc_dec")
            else:
                self.R("    call fa_rc_inc")
            self.R(f"{lbl}:")

    def _agg_func_header(self, name: str):
        self.R(f"    .globl {name}")
        self.R(f"{name}:")
        self.R("    test rdi, rdi")
        self.R(f"    jz {name}_null")
        self.R("    push rbp")
        self.R("    mov rbp, rsp")
        self.R("    push rbx")
        self.R("    sub rsp, 8")          # 3 次压栈后 rsp 回到 16 字节对齐
        self.R("    mov rbx, rdi")

    def _agg_func_footer(self, name: str):
        self.R("    lea rsp, [rbp-8]")
        self.R("    pop rbx")
        self.R("    pop rbp")
        self.R("    ret")
        self.R(f"{name}_null:")
        self.R("    ret")

    def _enum_ops(self, t: Type, base: str, mode: str):
        """枚举：先读 tag，只碰当前变体的载荷。

        别的变体在载荷区里留下的是**上一个值的垃圾字节**（栈槽是复用的），
        按扁平字段表去释放它们 = free 一个野指针。
        """
        pfx = "d" if mode == "drop" else "r"
        variants = [(vn, fl, i) for (vn, fl, i) in (t.variants or []) if fl]
        if not variants:
            return
        out = f".fae{pfx}{t.name}_out"
        self.R(f"    mov rax, [{base}]")               # tag
        for vi, (vn, fl, i) in enumerate(variants):
            self.R(f"    cmp rax, {i}")
            self.R(f"    je .fae{pfx}{t.name}_{vi}")
        self.R(f"    jmp {out}")
        for vi, (vn, fl, i) in enumerate(variants):
            self.R(f".fae{pfx}{t.name}_{vi}:")
            self._field_ops(t, base, mode, fields=fl, add=8, tag=f"v{vi}_")
            self.R(f"    jmp {out}")
        self.R(f"{out}:")

    def emit_drop_func(self, t: Type):
        name = f"__fa_drop_{t.name}"
        self._agg_func_header(name)
        if t.kind == "enum":
            self._enum_ops(t, "rbx", "drop")
        else:
            self._field_ops(t, "rbx", "drop")
        self._agg_func_footer(name)

    def emit_retain_func(self, t: Type):
        name = f"__fa_retain_{t.name}"
        self._agg_func_header(name)
        if t.kind == "enum":
            self._enum_ops(t, "rbx", "retain")
        else:
            self._field_ops(t, "rbx", "retain")
        self._agg_func_footer(name)

    # ---------------------------------------------------------------- 初始化
    def emit_init(self):
        hooks = getattr(self.mod, "init_hooks", [])
        need_init = bool(hooks) or bool(self.mod.descs) or \
            bool(getattr(self.sema, "lazy_syms", None))
        if not need_init:
            return
        self.R("    .section .init_array,\"aw\"")
        self.R("    .align 8")
        self.R("    .quad __fa_module_init")
        self.R("    .text")
        self.R("__fa_module_init:")
        self.R("    push rbp")
        self.R("    mov rbp, rsp")
        seen = set()
        for kind, _ in hooks:
            if kind == "py" and "py" not in seen:
                seen.add("py")
                self.R("    mov eax, 0")
                self.R("    call fa_py_init")
            # JVM 不能在 .init_array 里启动（libjimage 会在进程尚未初始化完成时崩溃），
            # 改为首次调用 java.* 时惰性创建（见 runtime/fa_jvm.c）。
        for t in self.mod.descs:
            self.R(f"    mov rdi, {t.desc_id}")
            self.R(f"    lea rsi, [rip+__fa_desc_{t.name}]")
            self.R("    call fa_register_desc")
            # retain 函数必须在 .init_array 里注册：容器 (Vec/Map) 在运行时
            # 需要靠它给装箱的结构体元素加引用，编译器不知道那些调用点。
            self.R(f"    mov rdi, {t.desc_id}")
            self.R(f"    lea rsi, [rip+__fa_retain_{t.name}]")
            self.R("    call fa_register_retain")
            # 容器里装箱的结构体/枚举在 fa_rc_dec 时也要走这个函数
            self.R(f"    mov rdi, {t.desc_id}")
            self.R(f"    lea rsi, [rip+__fa_drop_{t.name}]")
            self.R("    call fa_register_drop")
        lazy = getattr(self.sema, "lazy_syms", None)
        if lazy:
            self.R("    lea rdi, [rip+__fa_lazy_lib]")
            self.R("    call fa_dl_open")
            self.R("    mov rbx, rax")
            for name, path in lazy:
                self.R(f"    mov rdi, rbx")
                self.R(f"    lea rsi, [rip+__fa_lazy_{name}]")
                self.R(f"    lea rdx, [rip+__fa_lazy_name_{name}]")
                self.R("    call fa_dl_bind")
        self.R("    mov rsp, rbp")
        self.R("    pop rbp")
        self.R("    ret")

    # ---------------------------------------------------------------- 函数
    def emit_func(self, f: IRFunc):
        if not f.instrs and f.extern:
            return
        if self.opt >= 2:
            from .opt import licm
            licm(f)                      # 必须在 lower_params 之前：形参此时还是「无定义点」
        lower_params(f)
        self.loc, self.spills, self.intervals, used_callee = allocate(f)
        # 序言 push 的就是这一份，RET 的尾声必须用**同一份**（见 epilogue 调用处）
        self.used_callee = used_callee
        self.cur = f
        self.alloca_off = {}
        self.save_slots = {}

        # 1) 为 ALLOCA 分配栈偏移
        alloca_bytes = 0
        for ins in f.instrs:
            if ins.op == "ALLOCA":
                size, align = ins.extra
                size = max(size, 1)
                alloca_bytes = align16(alloca_bytes) if False else alloca_bytes
                alloca_bytes += size
                self.alloca_off[ins.dst.id] = alloca_bytes

        # 2) 计算调用点需要保存的寄存器槽位
        nint_args_max = 0
        for i, ins in enumerate(f.instrs):
            if ins.op in ("CALL", "CALLPTR"):
                n = len(ins.args) - 1
                if n > 6:
                    nint_args_max = max(nint_args_max, n - 6)
        save_area = 0
        for i, ins in enumerate(f.instrs):
            if ins.op not in ("CALL", "CALLPTR", "RCINC", "RCDEC", "MEMCPY", "ZERO"):
                continue
            for tid, r in self.call_saves(f, i):
                if tid not in self.save_slots:
                    save_area += 8
                    self.save_slots[tid] = save_area

        # 栈帧布局（System V AMD64 要求 call 指令处 rsp 16 字节对齐）：
        #   push rbp 之后 rsp ≡ 0 (mod 16)；再 push k 个被调用者保存寄存器后 rsp ≡ 8k。
        #   因此 sub 的帧大小 F 必须满足 F ≡ 8k (mod 16)。
        spill_base = 8 * len(used_callee) + 8
        need = spill_base + f.alloc_slot_bytes + alloca_bytes + save_area
        frame = align16(need) if need else 0
        if (frame % 16) != ((8 * len(used_callee)) % 16):
            frame += 8
        f.nstack = frame
        self.spill_base = spill_base
        self.save_base = spill_base + f.alloc_slot_bytes
        self.alloca_base = self.save_base + save_area

        self.R("")
        self.R(f"    .globl {f.name}")
        self.R(f"{f.name}:")
        self.R("    push rbp")
        self.R("    mov rbp, rsp")
        for r in used_callee:
            self.R(f"    push {r}")
        self.R(f"    sub rsp, {frame}")
        if f.name == "fa_main":
            pass

        uc: Dict[int, int] = {}
        for _ins in f.instrs:
            for _a in (_ins.args or []):
                if isinstance(_a, Temp):
                    uc[_a.id] = uc.get(_a.id, 0) + 1
        self.use_counts = uc

        # 循环头对齐：回边（跳到更靠前的标签）标出所有循环。
        # 循环体若跨越/起始于不理想的地址，取指与解码会掉速（实测最多 40%），
        # 因此把循环头对齐到 16 字节边界（与 gcc -falign-loops 同思路）。
        self.loop_headers = self._find_loop_headers(f) if self.opt >= 1 else set()
        import os as _os
        self.rax_vals = (self._find_rax_vals(f)
                         if (self.opt >= 2 and not _os.environ.get("FA_NO_RAX")) else set())

        for i, ins in enumerate(f.instrs):
            self.emit_instr(f, i, ins)

        # 兜底 epilogue（若最后一条不是 RET）
        if not f.instrs or f.instrs[-1].op != "RET":
            self.epilogue(f, used_callee)

    def _fallthrough_labels(self, f: IRFunc, idx: int) -> set:
        """从 idx 起（跳过 NOP 与紧接着的 LABEL）自然落到的那批标签名。

        用来判断「这次跳转其实可以直接落下」。代码块合并（相邻标签等价）
        也顺带在这里完成——连续几个 LABEL 都指向同一条指令。
        """
        out = set()
        i = idx
        while i < len(f.instrs):
            ins = f.instrs[i]
            if ins.op == "LABEL" and isinstance(ins.extra, str):
                out.add(ins.extra)
                i += 1
            elif ins.op == "NOP":
                i += 1
            else:
                break
        return out

    def _find_rax_vals(self, f: IRFunc) -> set:
        """找出「可以直接留在 rax 里」的调用结果。

        若某个 CALL 的返回值只被紧邻的下一条指令用一次，就没必要先搬到
        被调用者保存寄存器再运算——让它留在 rax 里，省掉一条 mov
        （递归密集的代码里每条都算数）。
        """
        out = set()
        for i, ins in enumerate(f.instrs):
            if ins.op not in ("CALL", "CALLPTR"):
                continue
            d = ins.dst
            if not isinstance(d, Temp) or d.id in self.spills:
                continue
            if is_float_ty(ins.ty):            # 浮点返回值在 xmm0，不在 rax
                continue
            if self.use_counts.get(d.id) != 1:
                continue
            if i + 1 >= len(f.instrs):
                continue
            nx = f.instrs[i + 1]
            # 下一条指令必须「先读 rax 再动它」；下面这些会先把 rax 冲掉
            if nx.op in ("CALL", "CALLPTR"):
                continue
            if nx.op == "BIN" and nx.extra in ("/", "%", "//"):
                continue
            if nx.op not in ("BIN", "UN", "SHIFT", "CMP", "MOV", "STORE", "RET",
                             "LOAD", "CONV", "BITCAST", "BR", "SELECT"):
                continue
            iv = self.intervals.get(d.id)
            if iv is None or iv[1] != i + 1:
                continue
            out.add(d.id)
            self.loc[d.id] = "rax"
        return out

    def _find_loop_headers(self, f: IRFunc) -> set:
        """回边（跳向更靠前位置的跳转）的目标就是循环头"""
        pos: Dict[str, int] = {}
        for i, ins in enumerate(f.instrs):
            if ins.op == "LABEL" and isinstance(ins.extra, str):
                pos.setdefault(ins.extra, i)
        heads = set()
        for i, ins in enumerate(f.instrs):
            tgts = []
            if ins.op == "JMP" and isinstance(ins.extra, str):
                tgts = [ins.extra]
            elif ins.op == "BR" and isinstance(ins.extra, (tuple, list)):
                tgts = [t for t in ins.extra if isinstance(t, str)]
            for t in tgts:
                j = pos.get(t)
                if j is not None and j <= i:
                    heads.add(t)
        return heads

    def epilogue(self, f, used_callee):
        # 注意：被调用者保存寄存器是在 `mov rbp, rsp` 之后 push 的，
        # 因此必须先按逆序弹出它们，再 pop rbp；直接用 `mov rsp, rbp` 会跳过它们。
        k = len(used_callee)
        if k:
            self.R(f"    lea rsp, [rbp-{8*k}]")
        else:
            self.R("    mov rsp, rbp")
        for r in reversed(used_callee):
            self.R(f"    pop {r}")
        self.R("    pop rbp")
        self.R("    ret")

    # ---------------------------------------------------------------- 调用保存
    def call_saves(self, f: IRFunc, idx: int):
        out = []
        ins = f.instrs[idx]
        dst = ins.dst
        for tid, (s, e) in self.intervals.items():
            # 注意 end 用严格小于：最后使用点就是这条调用时，
            # 说明它只是实参，调用返回后即死，没必要保存
            if not (s <= idx < e):
                continue
            if dst is not None and getattr(dst, "id", None) == tid:
                continue
            r = self.loc.get(tid)
            if r is None:
                continue
            if r in VOLATILE_GP or r.startswith("xmm"):
                out.append((tid, r))
        out.sort(key=lambda x: x[1])
        return out

    def emit_call_saves(self, f, idx):
        for tid, r in self.call_saves(f, idx):
            off = self.save_base + self.save_slots[tid]
            if r.startswith("xmm"):
                self.L(f"movsd [rbp-{off}], {r}")
            else:
                self.L(f"mov [rbp-{off}], {r}")

    def emit_call_restores(self, f, idx):
        for tid, r in reversed(self.call_saves(f, idx)):
            off = self.save_base + self.save_slots[tid]
            if r.startswith("xmm"):
                self.L(f"movsd {r}, [rbp-{off}]")
            else:
                self.L(f"mov {r}, [rbp-{off}]")

    # ---------------------------------------------------------------- 操作数
    def mem_off(self, tid: int) -> int:
        return self.spill_base + self.spills[tid]

    def mem(self, t: Temp, size=8) -> str:
        return f"qword [rbp-{self.mem_off(t.id)}]"

    def loc_of(self, t: Temp) -> Optional[str]:
        return self.loc.get(t.id)

    def spilled(self, t: Temp) -> bool:
        return t.id in self.spills

    def float_const(self, val: float) -> str:
        if val not in self.float_consts:
            lbl = f"__fa_fc{len(self.float_consts)}"
            self.float_consts[val] = lbl
        return self.float_consts[val]

    def opreg(self, v, ctx: Ctx, size=8, allow_imm=True):
        """取得存放 v 的寄存器名；必要时生成加载指令。返回字符串。"""
        if isinstance(v, Const):
            val = v.val
            if isinstance(val, bool):
                val = int(val)
            if isinstance(val, float):
                r = ctx.xmm_scratch()
                self.L(f"movsd {r}, [rip+{self.float_const(val)}]")
                return r
            if allow_imm and -2 ** 31 <= val < 2 ** 31:
                return str(val)
            r = ctx.scratch()
            self.L(f"movabs {r}, {val}")
            return r
        if isinstance(v, Reg):
            return v.name
        if isinstance(v, Temp):
            r = self.loc_of(v)
            if r is not None:
                ctx.used.add(r)
                return r
            rr = ctx.scratch()
            if is_float_ty(v.ty):
                self.L(f"movsd {rr}, [rbp-{self.mem_off(v.id)}]")
            else:
                self.L(f"mov {rr}, [rbp-{self.mem_off(v.id)}]")
            return rr
        raise Exception(f"bad operand {v}")

    def addr_str(self, base: str, off, ctx: Ctx) -> str:
        """[base + off]，off 可以是 int、已加载的寄存器名（Temp），
        或 (下标 Temp, 比例) —— 后者直接生成 x86 比例变址 [base + idx*scale]。"""
        if isinstance(off, tuple) and len(off) == 2:
            idx, scale = off
            if isinstance(idx, Const):          # 常量下标 -> 折进位移量
                d = int(idx.val) * int(scale)
                if d == 0:
                    return f"[{base}]"
                return f"[{base}+{d}]" if d > 0 else f"[{base}{d}]"
            ri = self.opreg(idx, ctx)
            return f"[{base}+{ri}*{scale}]"
        if isinstance(off, int):
            if off == 0:
                return f"[{base}]"
            return f"[{base}+{off}]" if off > 0 else f"[{base}{off}]"
        if isinstance(off, Temp):
            ro = self.opreg(off, ctx)
            return f"[{base}+{ro}]"
        return f"[{base}]"

    def dst_reg(self, ins: Instr, ctx: Ctx, prefer=None):
        d = ins.dst
        if d is None:
            return ctx.scratch()
        r = self.loc_of(d)
        if r is not None:
            ctx.used.add(r)
            return r
        # 溢出（无寄存器）的临时值：浮点必须用 xmm 暂存，否则 movsd 会被
        # 汇编成同名的「字符串传送」指令，静默破坏 rsi/rdi。
        if is_float_ty(getattr(d, "ty", None)):
            return ctx.xmm_scratch()
        return ctx.scratch()

    def store_dst(self, ins: Instr, reg: str):
        d = ins.dst
        if d is None:
            return
        r = self.loc_of(d)
        if r is not None:
            if r != reg:
                if is_float_ty(d.ty):
                    self.L(f"movsd {r}, {reg}")
                else:
                    self.L(f"mov {r}, {reg}")
        else:
            if is_float_ty(d.ty):
                self.L(f"movsd [rbp-{self.mem_off(d.id)}], {reg}")
            else:
                self.L(f"mov [rbp-{self.mem_off(d.id)}], {reg}")

    # ---------------------------------------------------------------- 指令
    def emit_instr(self, f: IRFunc, idx: int, ins: Instr):
        ctx = Ctx()
        op = ins.op
        # 若本指令要读「留在 rax 里的调用结果」，先把 rax 标记为占用，
        # 免得中途取暂存寄存器时把它覆盖掉。
        raxv = getattr(self, "rax_vals", ())
        if raxv:
            from .ir import instr_uses
            for t in instr_uses(ins):
                if t.id in raxv:
                    ctx.used.add("rax")
        if op == "NOP":
            return
        if op == "LABEL":
            # 循环头对齐到 **32** 字节：同一个循环只因起始地址不同就能差 40%。
            # 16 字节对齐不够 —— Intel 的 uop cache (DSB) 以 32 字节为窗口，
            # 循环头落在窗口中间时前端会退回传统译码路径。
            # 实测「1 亿次算术循环」（本机 gcc -O2 = 86 ms）：
            #   .p2align 4 -> 123 ms（1.43x）   .p2align 5 -> 98 ms（1.15x）
            #   .p2align 6 -> 104 ms（1.22x，还白占 icache）
            if self.opt >= 1 and isinstance(ins.extra, str) \
                    and ins.extra in getattr(self, "loop_headers", ()):
                self.R("    .p2align 5")
            self.R(f"{ins.extra}:")
            return
        if op == "ASM":
            for line in str(ins.extra).split("\n"):
                self.R("    " + line.strip())
            return
        if op == "JMP":
            nxt = self._fallthrough_labels(f, idx + 1)
            if isinstance(ins.extra, str) and ins.extra in nxt:
                return                     # 跳到下一条指令 = 什么都不用做
            self.L(f"jmp {ins.extra}")
            return
        if op == "BR":
            tlabel, flabel = ins.extra
            ft = self._fallthrough_labels(f, idx + 1)
            cv = self.opreg(ins.args[0], ctx, size=1)
            self.L(f"cmp {rn(cv, 1)}, 0")
            # 布局优化：真分支就是下一条指令时，改成「条件不成立才跳」，
            # 反之则省掉那条多余的 jmp——每次循环都能少一条被执行的跳转。
            if tlabel in ft:
                self.L(f"je {flabel}")
            elif flabel in ft:
                self.L(f"jne {tlabel}")
            else:
                self.L(f"jne {tlabel}")
                self.L(f"jmp {flabel}")
            return
        if op == "BITCAST":
            # 同一 64 位数据的重解释：movq 在 xmm 与通用寄存器之间搬运
            a = self.opreg(ins.args[0], ctx)
            d = self.dst_reg(ins, ctx)
            self.L(f"movq {d}, {a}")
            self.store_dst(ins, d)
            return
        if op == "MOV":
            ty = ins.ty
            if is_float_ty(ty):
                src = self.opreg(ins.args[0], ctx)
                d = self.dst_reg(ins, ctx)
                if isinstance(ins.args[0], Const) and isinstance(ins.args[0].val, float):
                    self.L(f"movsd {d}, [rip+{self.float_const(float(ins.args[0].val))}]")
                else:
                    self.L(f"movsd {d}, {src}")
                self.store_dst(ins, d)
            else:
                src = self.opreg(ins.args[0], ctx)
                d = self.dst_reg(ins, ctx)
                # 小整数类型：保持寄存器高位已扩展
                if not isinstance(ins.args[0], Const):
                    if ty is not None and ty.kind == "int" and ty.size < 8:
                        if ty.is_signed:
                            self.L(f"movsx {d}, {rn(src, ty.size)}")
                        else:
                            if ty.size == 4:
                                self.L(f"mov {rn(d,4)}, {rn(src,4)}")
                            else:
                                self.L(f"movzx {d}, {rn(src, ty.size)}")
                        self.store_dst(ins, d)
                        return
                if isinstance(ins.args[0], Const):
                    val = int(ins.args[0].val)
                    if -2 ** 31 <= val < 2 ** 31:
                        self.L(f"mov {d}, {val}")
                    else:
                        self.L(f"movabs {d}, {val}")
                else:
                    self.L(f"mov {d}, {src}")
                self.store_dst(ins, d)
            return
        if op == "LOADPARAM":
            d = self.dst_reg(ins, ctx)
            self.L(f"mov {d}, [rbp+{ins.extra}]")
            self.store_dst(ins, d)
            return
        if op == "BIN":
            self.emit_bin(ins, ctx)
            return
        if op == "UN":
            self.emit_un(ins, ctx)
            return
        if op == "CMP":
            # 窥孔：若紧跟其后的 BR 是本 CMP 结果的唯一使用者，
            # 则直接生成 cmp + 条件跳转，省掉 setcc/movzx/test 三条指令。
            if idx + 1 < len(f.instrs) and ins.dst is not None and \
                    ins.extra in JCC_S and not is_float_ty(ins.ty) and \
                    self.use_counts.get(ins.dst.id) == 1:
                nx = f.instrs[idx + 1]
                if nx.op == "BR" and isinstance(nx.args[0], Temp) and \
                        nx.args[0].id == ins.dst.id:
                    unsigned = ins.ty is not None and ins.ty.kind == "int" \
                        and not ins.ty.is_signed
                    cc = (JCC_U if unsigned else JCC_S)[ins.extra]
                    a = self.opreg(ins.args[0], ctx)
                    b = self.opreg(ins.args[1], ctx)
                    if _is_imm(a):
                        ta = ctx.scratch()
                        self.L(f"mov {ta}, {a}")
                        a = ta
                    self.L(f"cmp {a}, {b}")
                    tlabel, flabel = nx.extra
                    ft = self._fallthrough_labels(f, idx + 2)
                    if tlabel in ft:
                        self.L(f"{JCC_INV[cc]} {flabel}")
                    elif flabel in ft:
                        self.L(f"{cc} {tlabel}")
                    else:
                        self.L(f"{cc} {tlabel}")
                        self.L(f"jmp {flabel}")
                    nx.op = "NOP"
                    return
            self.emit_cmp(ins, ctx)
            return
        if op == "CONV":
            self.emit_conv(ins, ctx)
            return
        if op == "LOAD":
            ty = ins.ty
            ptr = self.opreg(ins.args[0], ctx)
            off = ins.extra
            a = self.addr_str(ptr, off, ctx)
            d = self.dst_reg(ins, ctx)
            if is_float_ty(ty):
                self.L(f"movsd {d}, {a}" if ty.name == "f64" else f"movss {d}, {a}")
            else:
                sz = ty.size if ty else 8
                if sz == 8:
                    self.L(f"mov {d}, qword ptr {a}")
                elif sz == 4:
                    self.L(f"mov {rn(d,4)}, dword ptr {a}")
                elif sz == 2:
                    self.L(f"movzx {d}, word ptr {a}" if not ty.is_signed else f"movsx {d}, word ptr {a}")
                else:
                    self.L(f"movzx {d}, byte ptr {a}" if not ty.is_signed else f"movsx {d}, byte ptr {a}")
            self.store_dst(ins, d)
            return
        if op == "STORE":
            ty = ins.ty
            ptr = self.opreg(ins.args[0], ctx)
            srcv = ins.args[1]
            off = ins.extra
            a = self.addr_str(ptr, off, ctx)
            if is_float_ty(ty):
                s = self.opreg(srcv, ctx)
                self.L(f"movsd {a}, {s}" if ty.name == "f64" else f"movss {a}, {s}")
            else:
                sz = ty.size if ty else 8
                word = {1: "byte", 2: "word", 4: "dword", 8: "qword"}[sz] + " ptr"
                if (isinstance(srcv, Const) and not isinstance(srcv.val, float)
                        and -2 ** 31 <= int(srcv.val) < 2 ** 31):
                    self.L(f"mov {word} {a}, {int(srcv.val)}")
                else:
                    # mov 到内存只接受 32 位有符号立即数。放不下的（例如 f64 的
                    # 位模式 4621537642612260864，也就是 `fv[0] = 9.5` 走内联
                    # 写元素那条路）必须先 movabs 进寄存器，否则汇编器报
                    # `operand type mismatch for 'mov'`。
                    s = self.opreg(srcv, ctx, allow_imm=False)
                    self.L(f"mov {word} {a}, {rn(s, sz)}")
            return
        if op == "LEA":
            ptr = self.opreg(ins.args[0], ctx)
            off = ins.extra
            d = self.dst_reg(ins, ctx)
            self.L(f"lea {d}, {self.addr_str(ptr, off, ctx)}")
            self.store_dst(ins, d)
            return
        if op == "LEA_SYM":
            d = self.dst_reg(ins, ctx)
            self.L(f"lea {d}, [rip+{ins.extra}]")
            self.store_dst(ins, d)
            return
        if op == "ALLOCA":
            d = self.dst_reg(ins, ctx)
            off = self.alloca_base + self.alloca_off[ins.dst.id]
            self.L(f"lea {d}, [rbp-{off}]")
            self.store_dst(ins, d)
            return
        if op == "STRCONST":
            d = self.dst_reg(ins, ctx)
            self.L(f"lea {d}, [rip+__fa_strc_{ins.extra}]")
            self.store_dst(ins, d)
            return
        if op == "MEMCPY":
            d = self.opreg(ins.args[0], ctx)
            s = self.opreg(ins.args[1], ctx)
            self.emit_call_saves(f, idx)
            self.L(f"mov rdi, {d}")
            self.L(f"mov rsi, {s}")
            self.L(f"mov rdx, {ins.extra}")
            self.L("mov eax, 0")
            self.L("call memcpy")
            self.emit_call_restores(f, idx)
            return
        if op == "ZERO":
            d = self.opreg(ins.args[0], ctx)
            self.emit_call_saves(f, idx)
            self.L(f"mov rdi, {d}")
            self.L("mov rsi, 0")
            self.L(f"mov rdx, {ins.extra}")
            self.L("mov eax, 0")
            self.L("call memset")
            self.emit_call_restores(f, idx)
            return
        if op == "CALL" or op == "CALLPTR":
            self.emit_call(f, idx, ins, ctx)
            return
        if op == "RET":
            if ins.args:
                v = ins.args[0]
                # 返回值的类型以 RET 指令自己带的 ty 为准：`return 1.5` 传下来的是
                # 一个 Const（不是 Temp），只看 v.ty 会把它当整数返回，
                # 于是生成 `mov rax, xmm8` —— GNU as 直接报 operand type mismatch。
                vty = ins.ty if is_float_ty(ins.ty) else \
                    (v.ty if isinstance(v, Temp) else None)
                if is_float_ty(vty):
                    s = self.opreg(v, ctx)
                    self.L(f"movsd xmm0, {s}")
                else:
                    s = self.opreg(v, ctx)
                    if s != "rax":
                        self.L(f"mov rax, {s}")
            # 必须用序言 push 的那一份列表：以前这里按「此刻 self.loc 里出现过哪些
            # 被调用者保存寄存器」重新算一遍，两者并不总是相同（某个寄存器在序言里
            # push 了，但走到这条 RET 时它分到的临时值已经溢出/死亡）。
            # push 了 4 个却只 pop 3 个 -> 最后那个（比如 r14）永远不会被还原，
            # 而 rbx/r12-r15 是**被调用者保存**的：调用方正拿着它存活跃值，
            # 于是一层递归回来数据就变了（实测递归求字符串长度：轻则算错，
            # 重则段错误 / malloc 报堆损坏）。
            self.epilogue(f, self.used_callee)
            return
        if op == "RCINC":
            v = self.opreg(ins.args[0], ctx)
            self.emit_call_saves(f, idx)
            self.L(f"mov rdi, {v}")
            self.L("mov eax, 0")
            self.L("call fa_rc_inc")
            self.emit_call_restores(f, idx)
            return
        if op == "RCDEC":
            v = self.opreg(ins.args[0], ctx)
            self.emit_call_saves(f, idx)
            self.L(f"mov rdi, {v}")
            self.L(f"mov rsi, {ins.extra}")
            self.L("mov eax, 0")
            self.L("call fa_rc_dec")
            self.emit_call_restores(f, idx)
            return
        if op == "NOP":
            return
        self.L(f"/* TODO: {ins} */")

    # ------------------------------------------------------- BIN / UN / CMP
    def emit_bin(self, ins: Instr, ctx: Ctx):
        op = ins.extra
        ty = ins.ty
        if is_float_ty(ty):
            a = self.opreg(ins.args[0], ctx)
            b = self.opreg(ins.args[1], ctx)
            d = self.dst_reg(ins, ctx)
            m = {"+": "addsd", "-": "subsd", "*": "mulsd", "/": "divsd"}[op]
            if d == b and d != a:
                t = ctx.xmm_scratch()
                self.L(f"movsd {t}, {a}")
                self.L(f"{m} {t}, {b}")
                self.L(f"movsd {d}, {t}")
                self.store_dst(ins, d)
                return
            if d != a:
                self.L(f"movsd {d}, {a}")
            self.L(f"{m} {d}, {b}")
            self.store_dst(ins, d)
            return
        a = self.opreg(ins.args[0], ctx)
        if op in ("<<", ">>"):
            b = self.opreg(ins.args[1], ctx)
            d = self.dst_reg(ins, ctx)
            signed = ty.is_signed if (ty and ty.kind == "int") else True
            sh = "sar" if (op == ">>" and signed) else ("shr" if op == ">>" else "shl")
            if d == b and d != a:
                # mov d, a 会先把移位量冲掉，必须走暂存
                t = ctx.scratch(extra=(a, b, d))
                self.L(f"mov {t}, {a}")
                self.L(f"mov rcx, {b}")
                self.L(f"{sh} {t}, cl")
                self.L(f"mov {d}, {t}")
                self.store_dst(ins, d)
                return
            if d != a:
                self.L(f"mov {d}, {a}")
            rc = "rcx"
            if b != "rcx":
                self.L(f"mov rcx, {b}")
            self.L(f"{sh} {d}, cl")
            self.store_dst(ins, d)
            return
        if op in ("/", "%", "//"):
            unsigned = ty is not None and ty.kind == "int" and not ty.is_signed
            # 有符号常量 2 的幂 -> 移位（C 语义的截断除法）
            bv = int(ins.args[1].val) if isinstance(ins.args[1], Const) else None
            if not unsigned and bv and bv > 0 and (bv & (bv - 1)) == 0:
                k = bv.bit_length() - 1
                a2 = self.opreg(ins.args[0], ctx)
                d = self.dst_reg(ins, ctx)
                q = ctx.scratch(extra=("rax", "rdx", d, a2))
                self.L(f"mov {q}, {a2}")
                self.L(f"sar {q}, 63")
                if bv > 1:
                    self.L(f"and {q}, {bv - 1}")
                self.L(f"add {q}, {a2}")
                self.L(f"sar {q}, {k}")
                if op == "%":
                    q2 = ctx.scratch(extra=("rax", "rdx", d, a2, q))
                    self.L(f"mov {q2}, {q}")
                    self.L(f"imul {q2}, {bv}")
                    self.L(f"mov {d}, {a2}")
                    self.L(f"sub {d}, {q2}")
                elif d != q:
                    self.L(f"mov {d}, {q}")
                self.store_dst(ins, d)
                return
            b = self.opreg(ins.args[1], ctx)
            d = self.dst_reg(ins, ctx)
            if a != "rax":
                self.L(f"mov rax, {a}")
            if _is_imm(b):
                tb = ctx.scratch(extra=("rax", "rdx"))
                self.L(f"mov {tb}, {b}")
                b = tb
            if b == "rdx":
                tmp = ctx.scratch(extra=("rax", "rdx"))
                self.L(f"mov {tmp}, {b}")
                b = tmp
            if unsigned:
                self.L("xor edx, edx")
                self.L(f"div {b}")
            else:
                self.L("cqo")
                self.L(f"idiv {b}")
            res = "rdx" if op in ("%",) else "rax"
            self.L(f"mov {d}, {res}")
            self.store_dst(ins, d)
            return
        b = self.opreg(ins.args[1], ctx)
        d = self.dst_reg(ins, ctx)
        m = {"+": "add", "-": "sub", "*": "imul", "&": "and",
             "|": "or", "^": "xor", "and": "and", "or": "or"}[op]
        # 加减常量 -> 一条 lea（1 个 uop，省掉 mov 那条）
        if op in ("+", "-") and isinstance(ins.args[1], Const) and not is_float_ty(ty):
            c = int(ins.args[1].val)
            if -2 ** 31 <= c < 2 ** 31:
                sign = "+" if op == "+" else "-"
                self.L(f"lea {d}, [{a}{sign}{abs(c)}]" if c != 0 else f"mov {d}, {a}")
                self.store_dst(ins, d)
                return
        # 「目标驱动代码生成」会产生 d 与 b 同寄存器的指令（如 x = 10 - x）。
        # 这时 mov d, a 会先把 b 冲掉，必须先算到暂存寄存器再搬回去。
        if d == b and d != a:
            t = ctx.scratch(extra=(a, b, d))
            self.L(f"mov {t}, {a}")
            self.L(f"{m} {t}, {b}")
            self.L(f"mov {d}, {t}")
            self.store_dst(ins, d)
            return
        if op == "*":
            if d != a:
                self.L(f"mov {d}, {a}")
            self.L(f"imul {d}, {b}")
        else:
            if d != a:
                self.L(f"mov {d}, {a}")
            self.L(f"{m} {d}, {b}")
        self.store_dst(ins, d)

    def emit_un(self, ins: Instr, ctx: Ctx):
        op = ins.extra
        ty = ins.ty
        a = self.opreg(ins.args[0], ctx)
        d = self.dst_reg(ins, ctx)
        if op == "-":
            if is_float_ty(ty):
                self.L(f"xorpd {d}, {d}")
                self.L(f"subsd {d}, {a}")
            else:
                if d != a:
                    self.L(f"mov {d}, {a}")
                self.L(f"neg {d}")
        elif op == "!":
            # cmp 的左操作数不能是立即数：`not true` 会生成 cmp 1, 0
            if _is_imm(a):
                ta = ctx.scratch()
                self.L(f"mov {ta}, {a}")
                a = ta
            self.L(f"cmp {a}, 0")
            self.L("sete al")
            self.L(f"movzx {d}, al")
        elif op == "~":
            if d != a:
                self.L(f"mov {d}, {a}")
            self.L(f"not {d}")
        elif op == "f-":
            self.L(f"xorpd {d}, {d}")
            self.L(f"subsd {d}, {a}")
        self.store_dst(ins, d)

    def emit_cmp(self, ins: Instr, ctx: Ctx):
        op = ins.extra
        ty = ins.ty
        a = self.opreg(ins.args[0], ctx)
        b = self.opreg(ins.args[1], ctx)
        d = self.dst_reg(ins, ctx)
        if is_float_ty(ty):
            self.L(f"ucomisd {a}, {b}")
            self.L({"==": "sete", "!=": "setne", "<": "setb", "<=": "setbe",
                    ">": "seta", ">=": "setae"}[op] + " al")
            self.L(f"movzx {d}, al")
        else:
            unsigned = ty is not None and ty.kind == "int" and not ty.is_signed
            if _is_imm(a):
                ta = ctx.scratch()
                self.L(f"mov {ta}, {a}")
                a = ta
            self.L(f"cmp {a}, {b}")
            cc = (SETCC_U if unsigned else SETCC)[op]
            self.L(cc + " al")
            self.L(f"movzx {d}, al")
        self.store_dst(ins, d)

    def emit_conv(self, ins: Instr, ctx: Ctx):
        src_ty = ins.extra
        dst_ty = ins.ty
        # cvtsi2sd / movsx 都不接受立即数，而整数常量默认就是按立即数给出的
        # （`p.combine(1, 2.5, ...)` 里那个 1 要转成 f64）——
        # 于是汇编器报 `operand type mismatch for 'cvtsi2sd'`。
        # 转换的源一律先落到寄存器里。
        a = self.opreg(ins.args[0], ctx, allow_imm=False)
        d = self.dst_reg(ins, ctx)
        # int -> float
        if src_ty.kind in ("int", "bool", "char") and dst_ty.is_float:
            if d in ("rax",) or not d.startswith("xmm"):
                pass
            if src_ty.kind == "int" and src_ty.size == 8 and not src_ty.is_signed:
                l1, l2 = self.new_label("u2f"), self.new_label("u2fE")
                if a != "rax":
                    self.L(f"mov rax, {a}")
                self.L("test rax, rax")
                self.L(f"js {l1}")
                self.L(f"cvtsi2sd {d}, rax")
                self.L(f"jmp {l2}")
                self.R(f"{l1}:")
                self.L("mov rcx, rax")
                self.L("shr rax, 1")
                self.L("and rcx, 1")
                self.L("or rax, rcx")
                self.L(f"cvtsi2sd {d}, rax")
                self.L(f"addsd {d}, {d}")
                self.R(f"{l2}:")
            else:
                if src_ty.size < 8:
                    if src_ty.is_signed:
                        self.L(f"movsx rax, {rn(a, src_ty.size)}")
                    else:
                        self.L(f"movzx rax, {rn(a, src_ty.size)}")
                    self.L(f"cvtsi2sd {d}, rax")
                else:
                    self.L(f"cvtsi2sd {d}, {a}")
            self.store_dst(ins, d)
            return
        # float -> int
        if src_ty.is_float and dst_ty.kind in ("int", "bool", "char"):
            self.L(f"cvttsd2si rax, {a}")
            if dst_ty.size < 8:
                if dst_ty.is_signed:
                    self.L(f"movsx {d}, {rn('rax', dst_ty.size)}")
                elif dst_ty.size == 4:
                    self.L(f"mov {rn(d,4)}, eax")
                else:
                    self.L(f"movzx {d}, {rn('rax', dst_ty.size)}")
            else:
                self.L(f"mov {d}, rax")
            self.store_dst(ins, d)
            return
        # float <-> float
        if src_ty.is_float and dst_ty.is_float:
            if src_ty.size != dst_ty.size:
                self.L(f"{'cvtss2sd' if dst_ty.size == 8 else 'cvtsd2ss'} {d}, {a}")
            elif d != a:
                self.L(f"movsd {d}, {a}")
            self.store_dst(ins, d)
            return
        # int -> int
        if dst_ty.kind in ("int", "bool", "char") and src_ty.kind in ("int", "bool", "char"):
            smap = {1: "byte", 2: "word", 4: "dword"}
            if _is_imm(a):
                # 立即数：值本身已在目标类型取值范围内，直接 mov 即可
                self.L(f"mov {d}, {a}")
                self.store_dst(ins, d)
                return
            if dst_ty.size == 8:
                if src_ty.size == 8 or a == d:
                    if d != a:
                        self.L(f"mov {d}, {a}")
                elif src_ty.is_signed:
                    self.L(f"movsx {d}, {rn(a, src_ty.size)}")
                else:
                    if src_ty.size == 4:
                        self.L(f"mov {rn(d,4)}, {rn(a,4)}")
                    else:
                        self.L(f"movzx {d}, {rn(a, src_ty.size)}")
            else:
                if dst_ty.is_signed:
                    self.L(f"movsx {d}, {rn(a, dst_ty.size)}")
                elif dst_ty.size == 4:
                    self.L(f"mov {rn(d,4)}, {rn(a,4)}")
                else:
                    self.L(f"movzx {d}, {rn(a, dst_ty.size)}")
            self.store_dst(ins, d)
            return
        if d != a:
            self.L(f"mov {d}, {a}")
        self.store_dst(ins, d)

    # ---------------------------------------------------------------- CALL
    def emit_call(self, f: IRFunc, idx: int, ins: Instr, ctx: Ctx):
        is_ptr = ins.op == "CALLPTR"
        callee = ins.args[0]
        args = ins.args[1:]
        ret_ty = ins.ty
        self.emit_call_saves(f, idx)

        # 统计整数/浮点参数
        nint = sum(1 for a in args if not is_float_ty(getattr(a, "ty", None)))
        nfp = len(args) - nint
        # 需要进栈的实参个数：按实参原顺序判定（前 6 个整数 + 前 8 个浮点走寄存器）
        nstack = 0
        _ni = _nf = 0
        for a in args:
            if is_float_ty(getattr(a, "ty", None)):
                if _nf < 8:
                    _nf += 1
                else:
                    nstack += 1
            else:
                if _ni < 6:
                    _ni += 1
                else:
                    nstack += 1
        # 每个实参的目标位置：前 6 个整数走 rdi/rsi/rdx/rcx/r8/r9，
        # 前 8 个浮点走 xmm0-7，其余按原顺序进栈。
        # 间接调用（CALLPTR）的函数指针也要先求出来：它可能正待在某个实参的
        # 目标寄存器里（rdi 是最常见的），等实参搬完再取就已经被冲掉了。
        pv = self.opreg(callee, ctx) if is_ptr else None

        plan = []                       # (实参, 当前所在, 目标寄存器 or None=进栈)
        ireg_i = freg_i = stack_i = 0
        for a in args:
            v = self.opreg(a, ctx)      # 先全部求值，避免后面互相覆盖
            if is_float_ty(getattr(a, "ty", None)):
                tgt = f"xmm{freg_i}" if freg_i < 8 else None
                freg_i += 1
            else:
                tgt = INT_REGS[ireg_i] if ireg_i < 6 else None
                ireg_i += 1
            plan.append([a, v, tgt, stack_i if tgt is None else -1])
            if tgt is None:
                stack_i += 1

        # 「并行搬运」冲突：某个实参此刻正待在**另一个实参的目标寄存器**里。
        # 典型场景是方法里再调函数 —— 形参 k 被固定在 xmm0，而第一个实参也要进
        # xmm0，顺序搬运会把 k 冲掉（实测 `scale(self.x, k)` 算出 x*x）。
        # 办法：把这些值先挪到栈上暂存，再从暂存处搬到目标寄存器。
        tgt_regs = {p[2] for p in plan if p[2]}
        stage_them = [p for p in plan if p[2] and p[1] != p[2] and p[1] in tgt_regs]
        stage_pv = pv is not None and pv in tgt_regs
        nstage = len(stage_them) + (1 if stage_pv else 0)
        stage_base = nstack * 8
        frame = align16(stage_base + nstage * 8) if (nstack or nstage) else 0
        if frame:
            self.L(f"sub rsp, {frame}")

        # 进栈的实参先写：此时还没有任何目标寄存器被覆盖
        for a, v, tgt, soff in plan:
            if tgt is None:
                if is_float_ty(getattr(a, "ty", None)):
                    self.L(f"movsd [rsp+{soff * 8}], {v}")
                else:
                    self.L(f"mov qword ptr [rsp+{soff * 8}], {v}")

        # 需要暂存的实参（以及函数指针）
        si = 0
        if stage_pv:
            self.L(f"mov qword ptr [rsp+{stage_base}], {pv}")
            pv = f"qword ptr [rsp+{stage_base}]"
            si += 1
        for p in stage_them:
            a, v, tgt, _soff = p
            slot = f"[rsp+{stage_base + si * 8}]"
            if is_float_ty(getattr(a, "ty", None)):
                self.L(f"movsd {slot}, {v}")
            else:
                self.L(f"mov qword ptr {slot}, {v}")
            p[1] = slot
            si += 1

        for a, v, tgt, _soff in plan:
            if tgt is None or v == tgt:
                continue
            if is_float_ty(getattr(a, "ty", None)):
                self.L(f"movsd {tgt}, {v}")
            else:
                self.L(f"mov {tgt}, {v}")
        fs = ins.extra
        varargs = getattr(fs, "varargs", False) if fs is not None else False
        # al 只在「可变参数」调用里才有意义（= 使用的向量寄存器个数）。
        # 已知被调函数不是可变参数时直接省掉这条 mov（gcc 也不生成）。
        # **间接调用一律省掉**：FA 的函数指针类型表达不了可变参数，目标函数根本不读
        # al；更要紧的是函数指针此刻可能正好待在 rax 里 —— 以前这里先来一句
        # `mov eax, 0` 再 `call rax`，等于跳到地址 0。实测「把比较函数当参数传进
        # 排序函数」必定段错误，而同一段代码写成直接调用就没事（直接调用的符号
        # 不占 rax）。
        if not is_ptr and (varargs or fs is None):
            self.L(f"mov eax, {freg_i if varargs else 0}")
        if is_ptr:
            if pv == "rax":
                self.L("call rax")
            else:
                self.L(f"mov rax, {pv}")
                self.L("call rax")
        else:
            name = callee.name if isinstance(callee, Sym) else str(callee)
            self.L(f"call {name}")
        if frame:
            self.L(f"add rsp, {frame}")
        # 返回值
        if ins.dst is not None:
            if is_float_ty(ret_ty):
                d = self.dst_reg(ins, ctx)
                self.L(f"movsd {d}, xmm0")
                self.store_dst(ins, d)
            elif ins.dst.id in getattr(self, "rax_vals", ()):
                pass                      # 结果就留在 rax 里，下一条指令直接用
            else:
                d = self.dst_reg(ins, ctx)
                if d != "rax":
                    self.L(f"mov {d}, rax")
                self.store_dst(ins, d)
        self.emit_call_restores(f, idx)


def generate_asm(mod: IRModule, sema: Sema, opt: int = 2) -> str:
    return AsmGen(mod, sema, opt).emit()
