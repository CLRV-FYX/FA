"""FA 编译驱动：源码 -> 汇编 -> 目标文件 -> 可执行文件（含 C/C++/Python/Java 互操作链接）。"""

from __future__ import annotations
import os
import sys
import glob
import shutil
import tempfile
import threading
import subprocess
import sysconfig
from typing import List, Optional, Tuple

from .lexer import FaSyntaxError
from .parser import parse
from .sema import Sema, FaTypeError
from . import codegen as CG
from .asmgen import generate_asm

FA_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME_DIR = os.path.join(FA_ROOT, "runtime")
BUILD_DIR = os.path.join(FA_ROOT, "build")

# --------------------------------------------------------------- C 类型映射
def c_type_of(ty) -> str:
    n = ty.name if ty.name else ""
    base = {
        "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
        "isize": "intptr_t", "u8": "uint8_t", "u16": "uint16_t",
        "u32": "uint32_t", "u64": "uint64_t", "usize": "size_t",
        "f32": "float", "f64": "double", "bool": "bool", "char": "char",
        "void": "void", "str": "FaStr*", "any": "uint64_t",
        "pyobj": "void*", "jobj": "void*",
    }.get(n)
    if base:
        return base
    if ty.kind == "ptr":
        inner = ty.inner
        if inner is None or inner.name == "void":
            return "void*"
        if inner.name == "u8":
            return "const char*" if False else "char*"
        return c_type_of(inner) + "*"
    if ty.kind == "struct" or ty.kind == "enum":
        return f"{ty.name}*"
    if ty.kind == "vec" or ty.kind == "map":
        return "void*"
    return "uint64_t"


def c_param_decl(name: str, ty) -> str:
    return f"{c_type_of(ty)} {name}"


# --------------------------------------------------------------- 环境探测
def py_config() -> Tuple[List[str], List[str]]:
    inc = sysconfig.get_paths().get("include") or ""
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    ver = sysconfig.get_config_var("VERSION") or f"{sys.version_info.major}.{sys.version_info.minor}"
    ldl = sysconfig.get_config_var("LDLIBRARY") or f"libpython{ver}.so"
    cflags = [f"-I{inc}"] if inc else []
    ldflags = [f"-L{libdir}"] if libdir else []
    if ldl.startswith("libpython") and ldl.endswith(".so"):
        ldflags.append(f"-lpython{ver}")
        ldflags.append(f"-Wl,-rpath,{libdir}")
    return cflags, ldflags


def py_available() -> Tuple[bool, str]:
    """`use py` 能否真正链接：必须有 Python.h（python3-dev）。

    只报「检测到了 include 目录」是不够的 —— Debian/Ubuntu 上装了 python3 但
    没装 python3-dev 时，sysconfig 依然给出 include 路径，直到 gcc 才炸出
    `fatal error: Python.h: No such file or directory`。这里提前判定，
    好给用户一句可执行的安装建议。
    """
    inc = sysconfig.get_paths().get("include") or ""
    if inc and os.path.exists(os.path.join(inc, "Python.h")):
        return True, os.path.join(inc, "Python.h")
    return False, (f"未找到 Python.h（sysconfig include = {inc or '空'}）。"
                   f"请安装 Python 开发头文件：\n"
                   f"    Debian/Ubuntu: sudo apt install python3-dev\n"
                   f"    Fedora/RHEL  : sudo dnf install python3-devel\n"
                   f"    macOS        : brew install python")


def java_config() -> Tuple[List[str], List[str], str]:
    home = os.environ.get("JAVA_HOME")
    cands = []
    if home:
        cands.append(home)
    cands += sorted(glob.glob("/usr/lib/jvm/*")) + sorted(glob.glob("/usr/java/*"))
    for jh in cands:
        if os.path.exists(os.path.join(jh, "include", "jni.h")):
            libdir = os.path.join(jh, "lib", "server")
            if not os.path.isdir(libdir):
                libdir = os.path.join(jh, "lib")
            return ([f"-I{os.path.join(jh, 'include')}",
                     f"-I{os.path.join(jh, 'include', 'linux')}"],
                    [f"-L{libdir}", "-ljvm", f"-Wl,-rpath,{libdir}"], jh)
    return ([], [], "")


def cc() -> str:
    return os.environ.get("CC", "gcc")


def cxx() -> str:
    return os.environ.get("CXX", "g++")


# --------------------------------------------------------------- 运行时构建
# 运行时 .o 是全进程共享的缓存：同一进程内的多线程（tests/run_tests.py 并行）
# 与不同进程（同时跑多个 fa）都可能同时构建它，因此既要加锁，也要用唯一临时名。
_RUNTIME_LOCK = threading.Lock()


def build_runtime(build_dir: str, with_py: bool, with_java: bool) -> List[str]:
    os.makedirs(build_dir, exist_ok=True)
    py_cflags, _ = py_config()
    java_cflags, _, _ = java_config()

    def obj(name: str, src: str, extra: List[str], tag: str) -> str:
        out = os.path.join(build_dir, f"{name}{tag}.o")
        with _RUNTIME_LOCK:
            if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
                return out
            # 唯一临时名（进程 + 线程）+ 原子改名：并行构建时既不会读到半截 .o，
            # 也不会两个线程抢同一个临时文件。
            tmp_out = f"{out}.{os.getpid()}.{threading.get_ident()}.tmp"
            cmd = [cc(), "-O2", "-std=gnu11", "-fno-strict-aliasing",
                   f"-I{RUNTIME_DIR}", f"-I{os.path.dirname(RUNTIME_DIR)}"]
            cmd += extra + ["-c", src, "-o", tmp_out]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(f"运行时编译失败 ({src}):\n{r.stderr}")
                os.replace(tmp_out, out)
            finally:
                if os.path.exists(tmp_out):
                    os.unlink(tmp_out)
        return out

    objs = [obj("fa_runtime", os.path.join(RUNTIME_DIR, "fa_runtime.c"), [], ""),
            obj("fa_syscall", os.path.join(RUNTIME_DIR, "fa_syscall.S"), [], ""),
            obj("fa_python", os.path.join(RUNTIME_DIR, "fa_python.c"),
                (["-DFA_HAS_PYTHON=1"] + py_cflags) if with_py else [],
                "_py" if with_py else "_stub"),
            obj("fa_jvm", os.path.join(RUNTIME_DIR, "fa_jvm.c"),
                (["-DFA_HAS_JAVA=1"] + java_cflags) if with_java else [],
                "_jvm" if with_java else "_stub")]
    return objs


# --------------------------------------------------------------- 前端
class CompileResult:
    def __init__(self):
        self.ok = True
        self.asm = ""
        self.sema = None
        self.irmod = None
        self.error = ""
        self.stage = ""


def frontend(src: str, filename: str, opt: int = 2) -> CompileResult:
    res = CompileResult()
    try:
        mod = parse(src, filename)
        sema = Sema(mod, filename, src).run()
        irmod = CG.generate(sema)
        asm = generate_asm(irmod, sema, opt)
        res.sema, res.irmod, res.asm = sema, irmod, asm
        return res
    except FaSyntaxError as e:
        res.ok = False; res.stage = "语法分析"; res.error = e.pretty(src)
    except FaTypeError as e:
        res.ok = False; res.stage = "语义分析"; res.error = e.pretty(src)
    except CG.FaCodegenError as e:
        res.ok = False; res.stage = "代码生成"; res.error = e.pretty(src)
    except Exception as e:
        import traceback
        res.ok = False; res.stage = "代码生成"
        res.error = f"{type(e).__name__}: {e}\n" + traceback.format_exc()
    return res


# --------------------------------------------------------------- C++ shim
def gen_cxx_shim(sema: Sema, out_dir: str, base_dir: str) -> Optional[str]:
    if not sema.cxx_shims:
        return None
    lines = ["// FA 自动生成的 C++ 互操作 shim", '#include "fa_runtime.h"']
    for h in sema.c_headers:
        lines.append(f"#include \"{h}\"")
    lines.append("")
    for d in sema.cxx_shims:
        params = [c_param_decl(p.name, d.sym.params[i]) for i, p in enumerate(d.params)]
        ret = c_type_of(d.sym.ret) if d.sym.ret else "void"
        args = ", ".join(p.name for p in d.params)
        body = f"return {d.name}({args});" if (d.sym.ret and d.sym.ret.kind != "void") \
            else f"{d.name}({args});"
        lines.append(f'extern "C" {ret} fa_{d.name}({", ".join(params)}) {{ {body} }}')
    path = os.path.join(out_dir, "_fa_cxx_shim.cpp")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def dl_c_type(ty) -> str:
    """dlopen 转发 shim 里用的 C 类型。

    和 c_type_of 的区别只有 str：代码生成那边对 extern 函数的 str 参数会先转成
    char*（返回值反过来从 char* 拷一份成 FaStr），所以 shim 必须按 char* 声明，
    不然 C 库里那个 `int f(const char*)` 收到的是一个 FaStr 指针，读到的是乱码。
    """
    if ty is not None and ty.kind == "str":
        return "const char*"
    return c_type_of(ty)


def gen_dl_shim(sema: Sema, out_dir: str, base_dir: str) -> Optional[str]:
    """`use lib "./x.so":` 声明的函数 -> 运行时 dlopen + dlsym 的转发 shim。

    这些符号不参与链接（库要等程序跑起来才打开），所以代码生成那边照常
    `call add2`，链接时找到的就是这个 shim：第一次调用时 dlopen 库、dlsym 符号，
    之后走缓存下来的函数指针。路径在编译期解析成绝对路径（相对源文件），
    这样可执行文件换个目录跑也找得到库。
    """
    if not sema.lazy_syms:
        return None
    L = []
    L.append("/* FA 自动生成：use lib 的运行时 dlopen 转发 */")
    L.append("#include <dlfcn.h>")
    L.append("#include <stdint.h>")
    L.append("#include <stdbool.h>")
    L.append("#include <stdlib.h>")
    L.append("#include <stdio.h>")
    L.append("")
    L.append("static void fa_dl_die(const char *what, const char *where) {")
    L.append('    fputs("panic: ", stderr);')
    L.append("    fputs(what, stderr);")
    L.append('    if (where && *where) { fputs(" ", stderr); fputs(where, stderr); }')
    L.append('    fputs("\\n", stderr);')
    L.append("    exit(1);")
    L.append("}")
    L.append("")
    # 每个库一个句柄变量，第一次用到时 dlopen
    handles = {}
    for d, path in sema.lazy_syms:
        lib = path if (os.path.isabs(path) or "/" not in path) \
            else os.path.abspath(os.path.join(base_dir, path))
        if lib not in handles:
            handles[lib] = "fa_dl_h%d" % len(handles)
    for lib, h in handles.items():
        L.append('static void *%s = 0;   /* %s */' % (h, lib))
    L.append("")
    for idx, (d, path) in enumerate(sema.lazy_syms):
        lib = path if (os.path.isabs(path) or "/" not in path) \
            else os.path.abspath(os.path.join(base_dir, path))
        h = handles[lib]
        sym = sema.fns[d.name]
        ps = [dl_c_type(sym.params[i]) for i in range(len(d.params))]
        names = [p.name for p in d.params]
        sig = ", ".join("%s %s" % (t, n) for t, n in zip(ps, names)) or "void"
        psig = ", ".join(ps) or "void"
        ret = dl_c_type(sym.ret) if sym.ret else "void"
        args = ", ".join(names)
        slot = "fa_dl_p%d" % idx
        has_ret = bool(sym.ret) and sym.ret.kind != "void"
        L.append("static %s (*%s)(%s) = 0;" % (ret, slot, psig))
        L.append("%s %s(%s) {" % (ret, d.name, sig))
        L.append("    if (!%s) {" % slot)
        L.append('        if (!%s) %s = dlopen("%s", RTLD_NOW | RTLD_GLOBAL);' % (h, h, lib))
        L.append('        if (!%s) fa_dl_die("打不开动态库", "%s");' % (h, lib))
        L.append('        %s = (%s (*)(%s))dlsym(%s, "%s");' % (slot, ret, psig, h, d.name))
        L.append('        if (!%s) fa_dl_die("动态库里找不到这个符号", "%s（在 %s 里）");'
                 % (slot, d.name, lib))
        L.append("    }")
        L.append("    %s%s(%s);" % ("return " if has_ret else "", slot, args))
        L.append("}")
        L.append("")
    out = os.path.join(out_dir, "_fa_dl_shim.c")
    with open(out, "w") as f:
        f.write("\n".join(L) + "\n")
    return out

# --------------------------------------------------------------- main 引导
def gen_main_shim(sema: Sema, out_dir: str) -> str:
    fn = sema.fns.get("main")
    nparams = len(fn.params) if fn else 0
    ret = fn.ret if fn else None
    retty = "int64_t" if (ret is None or ret.kind != "void") else "void"
    if nparams >= 2:
        sig = ("extern int64_t fa_main(int64_t argc, char** argv);\n"
               "extern void fa_set_args(int64_t argc, char** argv);")
        call = "fa_set_args((int64_t)argc, argv); return (int)fa_main((int64_t)argc, argv);"
    else:
        sig = (f"extern {retty} fa_main(void);\n"
               f"extern void fa_set_args(int64_t argc, char** argv);")
        call = ("fa_set_args((int64_t)argc, argv); return (int)fa_main();"
                if retty == "int64_t"
                else "fa_set_args((int64_t)argc, argv); fa_main(); return 0;")
    src = f"""/* FA 自动生成 */
#include <stdint.h>
{sig}
int main(int argc, char** argv) {{ (void)argc; (void)argv; {call} }}
"""
    path = os.path.join(out_dir, "_fa_main_shim.c")
    with open(path, "w") as f:
        f.write(src)
    return path


# --------------------------------------------------------------- 构建入口
def build(src_path: str, out_path: str = None, emit_asm: bool = False,
          opt: int = 2, run: bool = False, keep: bool = False,
          verbose: bool = False) -> int:
    src_path = os.path.abspath(src_path)
    base = os.path.splitext(os.path.basename(src_path))[0]
    out_dir = os.path.dirname(os.path.abspath(out_path)) if out_path else os.path.dirname(src_path)
    os.makedirs(out_dir, exist_ok=True)
    out_path = out_path or os.path.join(out_dir, base)
    work = os.path.join(out_dir, ".fa_work")
    os.makedirs(work, exist_ok=True)

    with open(src_path) as f:
        src = f.read()

    r = frontend(src, src_path, opt)
    if not r.ok:
        sys.stderr.write(f"[{r.stage}] {r.error}\n")
        return 1
    sema = r.sema

    # 依赖预检：把「还缺什么才能编译」说成人话，而不是让 gcc 抛一堆 fatal error
    if sema.py_used:
        ok, info = py_available()
        if not ok:
            sys.stderr.write(f"[依赖缺失] 源码用了 `use py`，需要内嵌 CPython。\n{info}\n")
            return 1
    if sema.java_used:
        _, _, jh = java_config()
        if not jh:
            sys.stderr.write("[依赖缺失] 源码用了 `use java`，但找不到 JDK"
                             "（需要 <jdk>/include/jni.h 与 libjvm.so）。\n"
                             "    Debian/Ubuntu: sudo apt install default-jdk\n"
                             "    或：export JAVA_HOME=/path/to/jdk\n")
            return 1
    if sema.cxx_shims and shutil.which(cxx()) is None:
        sys.stderr.write(f"[依赖缺失] 源码用了 `use cxx`（需要自动生成并编译 shim），"
                         f"但找不到 {cxx()}。\n")
        return 1

    asm_path = os.path.join(work, base + ".s")
    with open(asm_path, "w") as f:
        f.write(r.asm)
    if emit_asm:
        shutil.copy(asm_path, os.path.join(out_dir, base + ".s"))
        if verbose:
            print(f"[FA] 汇编已输出 -> {os.path.join(out_dir, base + '.s')}")

    # 汇编
    obj_path = os.path.join(work, base + ".o")
    cmd = [cc(), "-c", asm_path, "-o", obj_path]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(f"[汇编失败] {p.stderr}\n")
        sys.stderr.write(f"  汇编文件：{asm_path}\n")
        return 1

    link_objs = [obj_path]
    link_flags: List[str] = []

    # C++ shim
    shim = gen_cxx_shim(sema, work, os.path.dirname(src_path))
    if shim:
        shim_obj = os.path.join(work, "_fa_cxx_shim.o")
        cmd = [cxx(), "-O2", "-std=c++17", f"-I{RUNTIME_DIR}",
               f"-I{os.path.dirname(src_path)}", "-c", shim, "-o", shim_obj]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            sys.stderr.write(f"[C++ shim 编译失败] {p.stderr}\n")
            sys.stderr.write(f"  已生成的 shim：{shim}（可手工修改后重新编译链接）\n")
            return 1
        link_objs.append(shim_obj)

    # `use lib "./x.so"` 的运行时 dlopen 转发
    dlshim = gen_dl_shim(sema, work, os.path.dirname(src_path))
    if dlshim:
        dl_obj = os.path.join(work, "_fa_dl_shim.o")
        cmd = [cc(), "-O2", f"-I{RUNTIME_DIR}", "-c", dlshim, "-o", dl_obj]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            sys.stderr.write(f"[dlopen shim 编译失败] {p.stderr}\n")
            sys.stderr.write(f"  已生成的 shim：{dlshim}\n")
            return 1
        link_objs.append(dl_obj)

    # main 引导
    main_c = gen_main_shim(sema, work)
    main_obj = os.path.join(work, "_fa_main_shim.o")
    p = subprocess.run([cc(), "-O2", "-c", main_c, "-o", main_obj],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(f"[main 引导编译失败] {p.stderr}\n")
        return 1
    link_objs.append(main_obj)

    # 运行时
    with_py = bool(sema.py_used)
    with_java = bool(sema.java_used)
    link_objs += build_runtime(BUILD_DIR, with_py, with_java)

    # 链接
    link_flags += ["-lm", "-ldl", "-lpthread"]
    if with_py:
        _, pyld = py_config()
        link_flags += pyld
    if with_java:
        _, jld, _ = java_config()
        link_flags += jld
    src_dir = os.path.dirname(src_path)
    for l in sema.link_libs:
        if l.startswith("-"):
            link_flags.append(l)
        elif "/" in l or l.endswith((".so", ".a", ".dylib")):
            # 直接给出库文件路径（相对源文件的路径需要解析成绝对路径）
            pth = l if os.path.isabs(l) else os.path.normpath(os.path.join(src_dir, l))
            link_flags.append(pth)
            d = os.path.dirname(pth)
            if d:
                link_flags.append(f"-Wl,-rpath,{os.path.abspath(d)}")
        else:
            link_flags.append(f"-l{l}")

    cmd = [cc(), f"-O{opt}", "-no-pie"] + link_objs + ["-o", out_path] + link_flags
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(f"[链接失败] {p.stderr}\n")
        sys.stderr.write("  " + " ".join(cmd) + "\n")
        return 1
    if verbose:
        print(f"[FA] 构建完成 -> {out_path}")
    if run:
        return subprocess.run([out_path], cwd=os.path.dirname(src_path)).returncode
    return 0


def run_file(src_path: str, args=None, opt: int = 2, keep: bool = False,
             verbose: bool = False) -> int:
    """编译到临时目录并运行（`fa run` 与测试都走这里）。

    刻意**不在源码目录留任何产物**：以前 `fa run x.fa` 会在 x.fa 旁边生成
    可执行文件和 .fa_work/，跑一次示例就把仓库弄脏了。
    """
    tmp = tempfile.mkdtemp(prefix="fa_run_")
    try:
        out = os.path.join(tmp, "a.out")
        rc = build(src_path, out, opt=opt, keep=True, verbose=verbose)
        if rc != 0:
            return rc
        return subprocess.run([out] + list(args or []),
                              cwd=os.path.dirname(os.path.abspath(src_path))).returncode
    finally:
        if keep:
            print(f"[FA] 中间产物保留在 {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
