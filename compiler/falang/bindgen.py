"""`fa bind`：把 C 头文件自动翻成 FA 的 `use c` 声明块。

FA 的 C 互操作本来就是全的：函数按 System V ABI 直接 call，`str` 自动转 `const char*`，
C 返回的 `char*` 自动拷成 FA 的 str，FA 的函数可以直接当回调传给 C（qsort / sqlite3_exec /
libcurl 的写回调都是这么用的）。缺的从来不是能力，是**打字**：绑一个中等规模的库要手写
几百行 extern 声明，而每一处类型对不上都是运行时的垃圾数据或者 segfault。

这个模块把「读头文件、算类型、算布局」自动化：

    fa bind /usr/include/zlib.h --lib z -o zlib.fa
    fa bind regex.h --only regcomp,regexec,regfree,regerror -o re.fa

做法与 cgo / cffi 一致：**先用真的 C 预处理器**（`cc -E -dD`）把头文件展开，
平台条件编译（`#ifdef __x86_64__`）、宏替换、typedef 链条全部由 gcc 决定，不用自己猜；
然后只解析展开结果里的声明，并且默认只输出**目标头文件自己**的那些
（否则绑一个 stdio.h 会把整个 glibc 内部都倒出来）。

三条硬规矩：

1. **生成的东西必须能编译。** 默认用 FA 自己的前端把输出检查一遍，过不了的声明
   就地丢掉并在文末写明丢的是什么、为什么（见 `_verify_and_prune`）。
   宁可少绑一个函数，也不给用户一份编译不过的文件。
2. **不能表达的就说清楚，不猜。** `long double`（x86-64 上是 80 位扩展精度、16 字节）、
   按值传结构体、union、位域、函数式宏、外部变量，FA 侧没有对应写法，
   一律跳过 + 在文末列出原因，绝不映射成一个「差不多」的类型。
3. **和 FA 内建函数撞名的自动改名。** C 库里的 free / exit / pow / sqrt / abs / min /
   max / log / exp / len / str / sum / sort / join / keys / values / random / env / cmd
   一大把和 FA 内建同名，同名声明会悄悄顶掉内建的。bindgen 用
   `fn c_free(p: *void) -> void = "free"` 显式改名（见 parser 的 cname 语法）。
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

# ------------------------------------------------------------------ C → FA 类型
BASE_TO_FA = {
    "void": "void",
    "_Bool": "bool",
    "char": "char",
    "signed char": "i8",
    "unsigned char": "u8",
    "short": "i16",
    "short int": "i16",
    "unsigned short": "u16",
    "unsigned short int": "u16",
    "int": "i32",
    "signed int": "i32",
    "unsigned": "u32",
    "unsigned int": "u32",
    "long": "i64",
    "long int": "i64",
    "unsigned long": "u64",
    "unsigned long int": "u64",
    "long long": "i64",
    "long long int": "i64",
    "unsigned long long": "u64",
    "unsigned long long int": "u64",
    "float": "f32",
    "double": "f64",
    "wchar_t": "i32",
    "int8_t": "i8", "int16_t": "i16", "int32_t": "i32", "int64_t": "i64",
    "uint8_t": "u8", "uint16_t": "u16", "uint32_t": "u32", "uint64_t": "u64",
}

# 常见 typedef 的「友好名」：能用 FA 自己的 usize/isize 就别写 u64/i64，
# 读起来更接近头文件，也不会因为平台不同而错位。
FRIENDLY = {
    "size_t": "usize", "ssize_t": "isize", "ptrdiff_t": "isize",
    "intptr_t": "isize", "uintptr_t": "usize", "intmax_t": "i64",
    "uintmax_t": "u64", "off_t": "i64", "off64_t": "i64", "time_t": "i64",
    "clock_t": "i64", "pid_t": "i32", "uid_t": "u32", "gid_t": "u32",
    "mode_t": "u32", "dev_t": "u64", "ino_t": "u64", "ino64_t": "u64",
    "nlink_t": "u64", "blksize_t": "i64", "blkcnt_t": "i64",
    "socklen_t": "u32", "sa_family_t": "u16", "in_port_t": "u16",
    "in_addr_t": "u32", "suseconds_t": "i64", "useconds_t": "u32",
    "id_t": "u32", "key_t": "i32",
    "va_list": "*void", "__gnuc_va_list": "*void", "__builtin_va_list": "*void",
}

UNMAPPABLE = {
    "long double": "long double 在 x86-64 上是 80 位扩展精度（16 字节），FA 的 f64 装不下",
    "_Float128": "_Float128 是 16 字节浮点，FA 没有对应类型",
    "__float128": "__float128 是 16 字节浮点，FA 没有对应类型",
    "__int128": "__int128 是 16 字节整数，FA 最宽只有 i64",
    "unsigned __int128": "__int128 是 16 字节整数，FA 最宽只有 u64",
    "_Complex": "FA 没有复数类型",
    "double _Complex": "FA 没有复数类型",
    "float _Complex": "FA 没有复数类型",
}

# 和 FA 内建函数/关键字/类型名撞车的 C 符号：自动加 c_ 前缀，用 `= "符号"` 绑回真名。
FA_RESERVED = {
    "print", "println", "write", "len", "push", "pop", "str", "i64", "f64",
    "panic", "assert", "now", "sleep", "sqrt", "sin", "cos", "tan", "pow",
    "abs", "min", "max", "floor", "ceil", "log", "exp", "read_line", "exit",
    "concat", "contains", "keys", "values", "gcd", "random", "env",
    "file_read", "file_write", "cmd", "hex", "oct", "bin", "args", "round",
    "trunc", "log2", "log10", "exp2", "hypot", "clamp", "sign", "sum",
    "sort", "reverse", "join", "chr", "free", "sizeof", "new", "raise",
    "fn", "let", "mut", "const", "return", "if", "elif", "else", "while",
    "for", "in", "break", "continue", "struct", "enum", "impl", "use",
    "as", "extern", "unsafe", "nil", "true", "false", "self", "defer",
    "match", "and", "or", "not", "is", "pub", "static", "where", "try",
    "catch", "ref", "move", "dyn", "trait", "mod", "typeof", "Vec", "Map",
    "bool", "char", "void", "any", "i8", "i16", "i32", "u8", "u16", "u32",
    "u64", "usize", "isize", "f32", "pyobj", "jobj",
}

# 头文件名 → 库名（--lib 没给时的猜测；猜不出来就不写 lib，让用户自己补）。
GUESS_LIB = {
    "math.h": "m", "complex.h": "m", "fenv.h": "m",
    "zlib.h": "z", "zconf.h": "z", "sqlite3.h": "sqlite3",
    "curl/curl.h": "curl", "curl.h": "curl",
    "openssl/ssl.h": "ssl", "openssl/evp.h": "crypto", "openssl/sha.h": "crypto",
    "png.h": "png", "jpeglib.h": "jpeg", "tiff.h": "tiff",
    "bzlib.h": "bz2", "lzma.h": "lzma", "zstd.h": "zstd",
    "libxml/parser.h": "xml2", "readline/readline.h": "readline",
    "ffi.h": "ffi", "Python.h": "python3", "gmp.h": "gmp", "mpfr.h": "mpfr",
    "SDL2/SDL.h": "SDL2", "SDL.h": "SDL2", "GL/gl.h": "GL",
    "alsa/asoundlib.h": "asound", "magic.h": "magic", "uuid/uuid.h": "uuid",
    "regex.h": "c", "string.h": "c", "stdio.h": "c", "stdlib.h": "c",
    "time.h": "c", "dirent.h": "c", "unistd.h": "c", "fcntl.h": "c",
    "netdb.h": "c", "pthread.h": "pthread", "dlfcn.h": "dl", "crypt.h": "crypt",
}

SKIP_PREFIX = ("_",)                    # 默认跳过下划线开头的内部符号
_SYS_INC = ("/usr/include/", "/usr/local/include/")

# 绑一个头文件时，默认输出「它自己的 + 它所属那套库的」声明，
# 但**不包括**下面这些公共系统头文件里的 —— 否则绑 math.h 会把整个 glibc 倒出来。
# 反过来，math.h 的函数其实都声明在 bits/mathcalls.h 里（glibc 用 #include 分片），
# 只认目标文件本身就会一个函数都绑不到。所以规则是：目标文件 + 非标准头文件。
# 想只要目标文件自己的：--strict；想要一切：--deep。
STD_HEADERS = {
    "stdio.h", "stdlib.h", "string.h", "strings.h", "unistd.h", "time.h",
    "fcntl.h", "errno.h", "limits.h", "stddef.h", "stdint.h", "stdbool.h",
    "stdarg.h", "signal.h", "setjmp.h", "locale.h", "math.h", "ctype.h",
    "assert.h", "float.h", "complex.h", "fenv.h", "inttypes.h", "tgmath.h",
    "wchar.h", "wctype.h", "dirent.h", "dlfcn.h", "pthread.h", "sched.h",
    "semaphore.h", "netdb.h", "getopt.h", "glob.h", "regex.h", "fnmatch.h",
    "iconv.h", "langinfo.h", "nl_types.h", "poll.h", "spawn.h", "termios.h",
    "utime.h", "aio.h", "mqueue.h", "search.h", "tar.h", "cpio.h", "elf.h",
    "link.h", "malloc.h", "memory.h", "alloca.h", "endian.h", "byteswap.h",
    "crypt.h", "err.h", "error.h", "execinfo.h", "fmtmsg.h", "fstab.h",
    "fts.h", "ftw.h", "gshadow.h", "ifaddrs.h", "lastlog.h", "libgen.h",
    "mntent.h", "monetary.h", "netgroup.h", "obstack.h", "paths.h",
    "printf.h", "pty.h", "pwd.h", "resolv.h", "shadow.h", "stab.h",
    "stdio_ext.h", "syslog.h", "ucontext.h", "ulimit.h", "utmp.h", "utmpx.h",
    "values.h", "wordexp.h", "ar.h", "argp.h", "argz.h", "envz.h",
    "aliases.h", "features.h", "gnu-versions.h", "gnumake.h", "a.out.h",
    "syscall.h", "thread_db.h", "nss.h", "re_comp.h", "regexp.h",
    "sys/types.h", "sys/stat.h", "sys/socket.h", "sys/select.h",
    "sys/time.h", "sys/un.h", "sys/wait.h", "sys/uio.h", "sys/mman.h",
    "sys/ioctl.h", "sys/resource.h", "sys/syscall.h", "sys/utsname.h",
    "sys/param.h", "sys/file.h", "sys/dir.h", "sys/sysmacros.h",
    "sys/random.h", "sys/personality.h", "sys/prctl.h", "sys/epoll.h",
    "sys/eventfd.h", "sys/inotify.h", "sys/signalfd.h", "sys/timerfd.h",
    "sys/sysinfo.h", "sys/timeb.h", "sys/times.h", "sys/msg.h", "sys/sem.h",
    "sys/shm.h", "sys/ipc.h", "sys/mount.h", "sys/statvfs.h",
    "sys/vfs.h", "sys/quota.h", "sys/reboot.h", "sys/sendfile.h",
    "netinet/in.h", "netinet/tcp.h", "netinet/ip.h", "arpa/inet.h",
    "arpa/nameser.h", "net/if.h", "net/route.h", "rpc/types.h",
    "bits/types.h", "bits/typesizes.h", "bits/wordsize.h",
    "bits/timesize.h", "bits/time64.h", "bits/endian.h",
    "bits/endianness.h", "bits/libc-header-start.h", "bits/floatn.h",
    "bits/floatn-common.h", "bits/pthreadtypes.h",
    "bits/pthreadtypes-arch.h", "bits/thread-shared-types.h",
    "bits/struct_mutex.h", "bits/struct_rwlock.h", "bits/select.h",
    "bits/sigset.h", "bits/signum.h", "bits/signum-generic.h",
    "bits/timex.h", "bits/waitflags.h", "bits/waitstatus.h",
    "bits/stdio_lim.h", "bits/sys_errlist.h", "bits/environments.h",
    "bits/confname.h", "bits/posix_opt.h", "bits/posix1_lim.h",
    "bits/posix2_lim.h", "bits/local_lim.h", "bits/atomic_wide_counter.h",
    "stddef.h", "stdalign.h", "stdnoreturn.h", "iso646.h",
}


# ------------------------------------------------------------------ C 词法
_C_PUNCT = ["<<=", ">>=", "...", "->", "<<", ">>", "<=", ">=", "==", "!=", "&&",
            "||", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
            "(", ")", "[", "]", "{", "}", ",", ";", ":", "?", "*", "&", "|",
            "^", "~", "!", "+", "-", "/", "%", "<", ">", "=", ".", "#"]


def _c_tokens(text: str) -> List[Tuple[str, str]]:
    """把一段 C 声明文本切成 (kind, value)：kind ∈ name/num/str/punct。"""
    out: List[Tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            out.append(("name", text[i:j]))
            i = j
            continue
        if c.isdigit():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "."):
                j += 1
            out.append(("num", text[i:j]))
            i = j
            continue
        if c in "\"'":
            q, j = c, i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == q:
                    j += 1
                    break
                j += 1
            out.append(("str", text[i:j]))
            i = j
            continue
        for p in _C_PUNCT:
            if text.startswith(p, i):
                out.append(("punct", p))
                i += len(p)
                break
        else:
            i += 1
    return out


# ------------------------------------------------------------------ C 类型树
class CType:
    """kind: base / struct / union / enum / td / ptr / arr / fn"""

    __slots__ = ("kind", "name", "inner", "count", "ret", "params", "varargs",
                 "qual")

    def __init__(self, kind: str, name: str = "", inner=None, count=None,
                 ret=None, params=None, varargs=False, qual=""):
        self.kind = kind
        self.name = name
        self.inner = inner
        self.count = count
        self.ret = ret
        self.params = params or []
        self.varargs = varargs
        self.qual = qual

    def __repr__(self):
        if self.kind == "ptr":
            return f"*{self.inner!r}"
        if self.kind == "arr":
            return f"{self.inner!r}[{self.count}]"
        if self.kind == "fn":
            return f"fn({self.params!r})->{self.ret!r}"
        return self.name or self.kind


class _DeclParser:
    """C 声明的递归下降解析（够用于头文件里出现的那些写法）。"""

    QUALIFIERS = {"const", "volatile", "restrict", "__restrict", "__restrict__",
                  "__const", "__volatile__", "__extension__", "inline",
                  "__inline", "__inline__", "extern", "static", "_Noreturn",
                  "__attribute__", "__declspec", "register", "__thread",
                  "_Thread_local", "__asm__", "__asm", "asm", "_Restrict_"}
    TYPE_WORDS = {"void", "char", "short", "int", "long", "float", "double",
                  "signed", "unsigned", "_Bool", "bool", "_Complex", "complex",
                  "_Float128", "__float128", "__int128", "_Imaginary"}

    def __init__(self, toks: List[Tuple[str, str]]):
        self.t = toks
        self.i = 0
        self.saw_const = False

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def at(self, kind, val=None):
        k, v = self.peek()
        return k == kind and (val is None or v == val)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def eat(self, kind, val=None):
        if self.at(kind, val):
            return self.next()
        return None

    def _skip_balanced(self):
        """吃掉紧随其后的 ( ... ) 或 "..."（__attribute__ / __asm__ 的参数）。"""
        if self.at("str"):
            self.next()
            return
        if self.at("punct", "("):
            depth = 0
            while True:
                k, v = self.peek()
                if k is None:
                    return
                self.next()
                if v == "(":
                    depth += 1
                elif v == ")":
                    depth -= 1
                    if depth == 0:
                        return

    def parse_specs(self) -> CType:
        """类型说明符 → 基础类型。"""
        words: List[str] = []
        base: Optional[CType] = None
        while True:
            k, v = self.peek()
            if k is None:
                break
            if k == "name" and v in self.QUALIFIERS:
                self.next()
                if v in ("const", "__const"):
                    self.saw_const = True
                if v in ("__attribute__", "__declspec", "__asm__", "__asm", "asm"):
                    self._skip_balanced()
                continue
            if k == "name" and v in self.TYPE_WORDS:
                self.next()
                words.append({"_Complex": "_Complex", "complex": "_Complex",
                              "_Imaginary": "_Complex"}.get(v, v))
                continue
            if k == "name" and v in ("struct", "union", "enum"):
                self.next()
                tag = self.next()[1] if self.at("name") else ""
                base = CType(v, tag)
                continue
            if k == "name" and base is None and not words:
                self.next()                       # typedef 名（size_t / FILE / ...）
                base = CType("td", v)
                continue
            break
        if base is not None:
            return base
        if not words:
            return CType("base", "int")
        return CType("base", _normalize_base(words))

    def parse_declarator(self) -> Tuple[Optional[CType], str]:
        """声明符 → (类型树, 名字)。基础类型的位置留 None，由 _apply_base 填。"""
        stars = 0
        while self.eat("punct", "*"):
            stars += 1
            while True:
                k, v = self.peek()
                if k == "name" and v in self.QUALIFIERS:
                    self.next()
                    if v in ("const", "__const"):
                        self.saw_const = True
                    if v in ("__attribute__", "__asm__", "__asm", "asm"):
                        self._skip_balanced()
                    continue
                break
        return self._parse_direct(stars)

    def _parse_direct(self, stars: int) -> Tuple[Optional[CType], str]:
        name = ""
        ty: Optional[CType] = None
        if self.at("punct", "("):
            save = self.i
            self.next()
            if self.at("punct", "*"):
                # `char (*cmp)(const void *, const void *)`：分组里是指针 + 名字，
                # 分组外是参数表 —— 这才是「指向函数的指针」。
                inner_ty, name = self.parse_declarator()
                self.eat("punct", ")")
                if self.at("punct", "("):
                    params, varargs = self.parse_params()
                    ty = CType("ptr", "", inner=CType("fn", "", ret=inner_ty,
                                                      params=params,
                                                      varargs=varargs))
                else:
                    ty = CType("ptr", "", inner=inner_ty)
                return ty, name
            self.i = save
            self.next()
            ty, name = self._parse_direct(0)
            self.eat("punct", ")")
        elif self.at("name"):
            name = self.next()[1]
        # 星号先套：`char *f(int)` 是「返回 char* 的函数」，星号属于返回类型；
        # 要是先建 fn 节点再套星号，就会读成「指向函数的指针」，返回类型丢掉一个 *。
        for _ in range(stars):
            ty = CType("ptr", "", inner=ty)
        while True:
            if self.at("punct", "["):
                self.next()
                cnt = None
                if not self.at("punct", "]"):
                    cnt = self._const_expr_int()
                self.eat("punct", "]")
                ty = CType("arr", "", inner=ty, count=cnt)
                continue
            if self.at("punct", "("):
                params, varargs = self.parse_params()
                ty = CType("fn", "", inner=None, ret=ty, params=params,
                           varargs=varargs)
                continue
            break
        return ty, name

    def _const_expr_int(self) -> Optional[int]:
        """读掉数组维度里的常量表达式，能算就算。"""
        depth, parts = 0, []
        while True:
            k, v = self.peek()
            if k is None:
                break
            if v == "[":
                depth += 1
            elif v == "]":
                if depth == 0:
                    break
                depth -= 1
            parts.append(v)
            self.next()
        text = "".join(parts)
        if re.fullmatch(r"\d+", text or ""):
            return int(text)
        return None

    def parse_params(self) -> Tuple[List[Tuple[str, CType]], bool]:
        """( 参数表 ) → ([(参数名, 类型)], 是否变参)。"""
        self.eat("punct", "(")
        params: List[Tuple[str, CType]] = []
        varargs = False
        if self.at("punct", ")"):
            self.next()
            return params, False
        guard = 0
        while True:
            guard += 1
            if guard > 500:
                break
            k, v = self.peek()
            if k is None:
                break
            if v == "...":
                varargs = True
                self.next()
                continue
            if k == "punct" and v == ")":
                self.next()
                break
            start = self.i
            base = self.parse_specs()
            ty, pname = self.parse_declarator()
            ty = base if ty is None else _apply_base(ty, base)
            if ty is not None and ty.kind == "ptr" and self.saw_const:
                ty.qual = "const"
            params.append((pname, ty))
            self.saw_const = False
            if self.i == start:
                self.next()
                continue
            self.eat("punct", ",")
        return params, varargs


def _normalize_base(words: List[str]) -> str:
    """把 C 的类型说明符词序归一化成 BASE_TO_FA 的键。"""
    joined = " ".join(words)
    unsigned = "unsigned" in words
    signed = "signed" in words
    longs = words.count("long")
    if "_Complex" in words:
        return ("float _Complex" if "float" in words and "double" not in words
                else "double _Complex")
    if "__int128" in words:
        return "unsigned __int128" if unsigned else "__int128"
    if "_Float128" in words or "__float128" in words:
        return "_Float128"
    if "long double" in joined:
        return "long double"
    if "double" in words:
        return "double"
    if "float" in words:
        return "float"
    if "_Bool" in words or "bool" in words:
        return "_Bool"
    if "char" in words:
        return ("unsigned char" if unsigned else
                "signed char" if signed else "char")
    if "void" in words:
        return "void"
    if "short" in words:
        return "unsigned short" if unsigned else "short"
    if longs >= 2:
        return "unsigned long long" if unsigned else "long long"
    if longs == 1:
        return "unsigned long" if unsigned else "long"
    if unsigned:
        return "unsigned int"
    return "int"


def _apply_base(ty: Optional[CType], base: CType) -> CType:
    """把基础类型填进声明符树里最靠内的空位。

    空位 = ptr/arr 的 inner 为 None，或 fn 的 ret 为 None。
    `const char *f(int)` 的树是 fn(ret=ptr(None))，必须递归到 ptr 里面去填；
    只填最外层就会把星号吃掉（返回类型变成 char）。
    """
    if ty is None:
        return base
    if ty.kind in ("ptr", "arr"):
        if ty.inner is None:
            ty.inner = base
        else:
            _apply_base(ty.inner, base)
        return ty
    if ty.kind == "fn":
        if ty.ret is None:
            ty.ret = base
        else:
            _apply_base(ty.ret, base)
        return ty
    return ty


def _find_fn(ty: Optional[CType]) -> Optional[CType]:
    node = ty
    while node is not None:
        if node.kind == "fn":
            return node
        node = node.inner if node.kind in ("ptr", "arr") else None
    return None


# ------------------------------------------------------------------ 布局
_C_SIZES = {
    "void": (0, 1), "_Bool": (1, 1), "char": (1, 1),
    "signed char": (1, 1), "unsigned char": (1, 1),
    "short": (2, 2), "unsigned short": (2, 2),
    "int": (4, 4), "unsigned int": (4, 4),
    "long": (8, 8), "unsigned long": (8, 8),
    "long long": (8, 8), "unsigned long long": (8, 8),
    "float": (4, 4), "double": (8, 8), "long double": (16, 16),
    "wchar_t": (4, 4), "__int128": (16, 16), "unsigned __int128": (16, 16),
    "_Float128": (16, 16), "double _Complex": (16, 8), "float _Complex": (8, 4),
}
_FA_SIZES = {"i8": 1, "u8": 1, "char": 1, "bool": 1, "i16": 2, "u16": 2,
             "i32": 4, "u32": 4, "f32": 4, "i64": 8, "u64": 8, "f64": 8,
             "usize": 8, "isize": 8, "str": 8}


# ------------------------------------------------------------------ 结果
class BindResult:
    def __init__(self):
        self.text = ""
        self.fns: List[str] = []
        self.consts: List[str] = []
        self.structs: List[str] = []
        self.skipped: List[Tuple[str, str]] = []
        self.dropped: List[Tuple[str, str]] = []
        self.header = ""
        self.lib = ""
        self.header_libs: Dict[str, str] = {}


# ------------------------------------------------------------------ 声明归类
def _categorize(u: str) -> str:
    low = u.lstrip()
    if low.startswith("typedef"):
        return "typedef"
    m = re.match(r"^(struct|union|enum)\b", low)
    if m:
        return m.group(1)
    if low.startswith("extern") and "(" not in u:
        return "var"
    if "(" in u:
        return "fn"
    return "other"


def _split_units(text: str) -> List[str]:
    """按顶层 `;` 切分声明（`{}` 里的分号不算，跳过字符串与注释）。"""
    units, buf = [], []
    depth = 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            q = c
            buf.append(c)
            i += 1
            while i < n:
                buf.append(text[i])
                if text[i] == "\\":
                    i += 1
                    if i < n:
                        buf.append(text[i])
                elif text[i] == q:
                    break
                i += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = (j + 2) if j >= 0 else n
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = j if j >= 0 else n
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == ";" and depth <= 0:
            depth = 0
            units.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if "".join(buf).strip():
        units.append("".join(buf))
    return units


def _match_brace(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _tag_of(u: str) -> str:
    m = re.search(r"\b(struct|union|enum)\s+(\w+)", u)
    if m:
        return m.group(2)
    return ""


def _decl_name(u: str) -> str:
    """从一条声明里取出名字（过滤用，不追求严格）。"""
    m = re.match(r"^#define\s+(\w+)", u.strip())
    if m:
        return m.group(1)
    body = u.rstrip(";").strip()
    body = re.sub(r"^typedef\s+", "", body)
    body = re.sub(r"^(extern|static)\s+", "", body)
    # 返回结构体（指针）的**函数声明**不是结构体定义：
    #   extern struct dirent *readdir (DIR *__dirp);
    # 整条去掉分号后以 `)` 收尾，就是函数声明符，名字取第一个 `(` 前面那个标识符。
    # 以前这条会掉进下面的 struct 分支，名字被取成返回类型里的 tag（dirent），
    # 于是 --only readdir 一个都匹配不上、readdir 整个函数被悄悄丢掉。
    # C 里返回 struct X* 的函数极常见（readdir / localtime / getpwnam / ...），
    # 这一条不修，「绑任何库」就绑不动。
    if body.endswith(")") and "(" in body and "{" not in body:
        lp0 = body.find("(")
        mm0 = re.findall(r"[A-Za-z_]\w*", body[:lp0])
        if mm0:
            cand0 = mm0[-1]
            if cand0 in ("struct", "union", "enum", "sizeof") and len(mm0) > 1:
                cand0 = mm0[-2]
            return cand0
    if re.match(r"^(struct|union|enum)\b", body):
        t = _tag_of(body)
        if t:
            return t
        # 匿名 struct/enum：用 typedef 出来的名字
        m2 = re.search(r"\}\s*(\w+)\s*$", body)
        return m2.group(1) if m2 else ""
    lp = body.find("(")
    if lp > 0:
        mm = re.findall(r"[A-Za-z_]\w*", body[:lp])
        if mm:
            cand = mm[-1]
            if cand in ("struct", "union", "enum", "sizeof") and len(mm) > 1:
                return mm[-2]
            return cand
    mm = re.findall(r"[A-Za-z_]\w*", body)
    return mm[-1] if mm else ""


def _include_spelling(hdr: str, extra_dirs) -> str:
    """生成文件里 `use c "..."` 该写什么路径。

    系统头文件写成相对形式（regex.h / curl/curl.h），换台机器也一样能用；
    项目自己的头文件写绝对路径，因为别人猜不到你放哪了。
    """
    if not os.path.exists(hdr):
        return hdr                      # 只写了名字（sys/stat.h）：原样交回去
    ap = os.path.abspath(hdr)
    for d in _SYS_INC:
        if ap.startswith(d):
            return ap[len(d):]
    for d in extra_dirs or []:
        d = os.path.abspath(d)
        if ap.startswith(d + os.sep):
            return ap[len(d) + 1:]
    return ap


def _c_num(text: str) -> Optional[object]:
    """C 的数字字面量 → Python 的 int/float。带 U/L 后缀、十六进制、浮点都认。"""
    t = text.strip()
    m = re.fullmatch(r"(0[xX][0-9a-fA-F]+|0[bB][01]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)"
                     r"([uUlLfF]*)", t)
    if not m:
        return None
    body, suf = m.group(1), m.group(2).lower()
    try:
        if body.lower().startswith("0x"):
            v = int(body, 16)
        elif body.lower().startswith("0b"):
            v = int(body, 2)
        elif "f" in suf or "." in body or "e" in body.lower():
            return float(body)
        else:
            v = int(body, 8) if (body.startswith("0") and len(body) > 1
                                 and all(c in "01234567" for c in body)) else int(body)
    except ValueError:
        return None
    # C 的无符号回绕：FA 的 i64 是有符号的，按 64 位补码解释
    if isinstance(v, int):
        v &= (1 << 64) - 1
        if v >= (1 << 63):
            v -= (1 << 64)
    return v


# ------------------------------------------------------------------ 主体
class Binder:
    def __init__(self, headers: List[str], lib: str = "", only=None, exclude=None,
                 ptr_return=None,
                 deep: bool = False, private: bool = False, verify: bool = True,
                 cc: str = "cc", include_dirs=None, defines=None,
                 no_probe: bool = False, strict: bool = False):
        self.headers = headers
        self.lib = lib
        self.only = set(only) if only else None
        self.exclude = set(exclude) if exclude else set()
        # 这些函数的 char* 返回值**不要**转成 str，保留 *u8。
        # 转成 str 看着方便（自动拷、自动按 NUL 收尾），可是「返回 NULL」这个信息就没了：
        # strptime 解析失败给 NULL、strchr 没找到给 NULL、getenv 变量不存在给 NULL，
        # 在 FA 侧统统变成 ""，调用方分不清「没有」和「有个空字符串」。
        self.ptr_return = set(ptr_return) if ptr_return else set()
        # 预处理输出里出现过的文件 -> 用户命令行写的那个头文件（谁把它带进来的）
        self.file_owner: Dict[str, str] = {}
        self.deep = deep
        self.private = private
        self.verify = verify
        self.cc = cc
        self.include_dirs = include_dirs or []
        self.defines = defines or []
        self.no_probe = no_probe
        self.strict = strict
        self._tmp_dirs: List[str] = []
        self._real_paths: Dict[str, str] = {}
        self.probed: Dict[str, dict] = {}
        self.typedefs: Dict[str, CType] = {}
        self.structs: Dict[str, Optional[List]] = {}     # tag → [(字段名, CType)] / None=union
        self.struct_bits: Dict[str, bool] = {}
        self.struct_size: Dict[str, int] = {}
        self.struct_src: Dict[str, str] = {}
        self.struct_is_alias: Dict[str, bool] = {}   # typedef 出来的名字（没有 struct tag）
        self.unions: Dict[str, int] = {}
        self.enum_vals: Dict[str, List[Tuple[str, int]]] = {}   # 来源文件#tag → 值
        self.anon_enum_vals: List[Tuple[str, str, List[Tuple[str, int]]]] = []
        self.emitted_structs: Dict[str, str] = {}        # C tag → FA 结构体名
        self.struct_fields: Dict[str, List[Tuple[str, str]]] = {}
        self.struct_kind: Dict[str, str] = {}            # real / raw
        self.struct_note: Dict[str, str] = {}
        self.res = BindResult()
        self._line_owner: Dict[int, str] = {}

    # ---------------------------------------------------------- 预处理
    def resolve_header(self, header: str) -> Tuple[str, List[str]]:
        """返回 (交给 cc 的路径, 算作「目标文件」的名字集合)。

        写 `/usr/include/zlib.h` 就直接用；写 `sys/stat.h` 这种（Debian 上真身在
        /usr/include/x86_64-linux-gnu/sys/stat.h）就生成一个临时 .c 去 include，
        再用 `cc -M` 问出预处理器实际打开的是哪个文件，把它算成目标。
        """
        names = {os.path.realpath(header), os.path.abspath(header),
                 os.path.basename(header), header}
        if os.path.exists(header):
            return header, sorted(names)
        import tempfile
        td = tempfile.mkdtemp(prefix="fa_bind_")
        wrapper = os.path.join(td, "_fa_bind_wrapper.c")
        with open(wrapper, "w") as f:
            f.write(f"#include <{header}>\n")
        self._tmp_dirs.append(td)
        # 问 cc：这个 include 落到哪个文件上
        cmd = [self.cc, "-M", "-D_GNU_SOURCE"]
        for d in self.include_dirs:
            cmd.append(f"-I{d}")
        cmd.append(wrapper)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            for tok in r.stdout.replace("\\", " ").split():
                tok = tok.strip()
                if tok.endswith("/" + header) or tok == header \
                        or os.path.basename(tok) == os.path.basename(header):
                    names.add(os.path.realpath(tok))
                    names.add(tok)
        names.add(header)
        return wrapper, sorted(names)

    # ---------------------------------------------------------- 预处理
    def preprocess(self, header: str) -> Optional[str]:
        cmd = [self.cc, "-E", "-dD", "-D_GNU_SOURCE",
               "-D__extension__=", "-D__attribute__(x)=", "-D__asm__(x)=",
               "-D__asm(x)=", "-D__inline=", "-D__inline__=", "-D__restrict=",
               "-D__restrict__=", "-D__THROW=", "-D__wur=", "-D__nonnull(x)=",
               "-D__REDIRECT_NTH(a,b,c)=", "-D__const="]
        for d in self.defines:
            cmd.append(f"-D{d}")
        for d in self.include_dirs:
            cmd.append(f"-I{d}")
        cmd.append(header)
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else None

    # ---------------------------------------------------------- 扫描
    def scan(self, text: str, owner: str = "") -> List[Tuple[str, str, str]]:
        """[(来源文件, 类别, 声明文本)]。

        预处理输出里带 `# 行号 "文件"` 标记，据此知道每条声明出自哪个文件：
        默认只要目标头文件自己的声明，但 typedef / struct 不论来自哪里都收进表里，
        否则 size_t、FILE 这些名字根本解析不了。

        实现上是**先把每个文件的正文各自攒成一整段，再按顶层分号切声明**。
        以前是逐行攒、遇到文件标记就冲一次，靠花括号计数判断「是不是在声明中间」——
        结构体里嵌套一层 struct/union 就算错，把 `struct re_pattern_buffer`
        从中间截断（截断以后花括号配不上，整个结构体悄悄消失）。
        """
        by_file: Dict[str, List[str]] = {}
        defines: List[Tuple[str, str]] = []
        cur = "<builtin>"
        cont = ""
        for raw in text.split("\n"):
            m = re.match(r'^#\s+\d+\s+"([^"]+)"', raw)
            if m:
                cur = m.group(1)
                # 每个头文件是**各自**预处理的，所以这一段里出现过的文件都是从
                # owner 那个头文件（直接或间接）拉进来的。记下来，输出时才知道
                # 该把声明放进哪个 `use c` 块 —— math.h 的函数真身在
                # bits/mathcalls.h，不记的话它们会被算到命令行第一个头文件头上，
                # 跟着挂错 lib（-lm 的函数挂到 -lc 那块，链接就找不到符号了）。
                if owner and cur not in self.file_owner \
                        and not cur.startswith("<") and os.path.exists(cur):
                    self.file_owner[cur] = owner
                continue
            if raw.startswith("#"):
                line = cont + raw
                if line.rstrip().endswith("\\"):
                    cont = line.rstrip()[:-1]
                    continue
                cont = ""
                if line.startswith("#define ") or line.startswith("#undef "):
                    defines.append((cur, line))
                continue
            cont = ""
            by_file.setdefault(cur, []).append(raw)

        items: List[Tuple[str, str, str]] = []
        for src, dline in defines:
            items.append((src, "define", dline))
        for src, lines in by_file.items():
            for unit in _split_units("\n".join(lines)):
                u = unit.strip()
                if u:
                    items.append((src, _categorize(u), u))
        return items

    # ---------------------------------------------------------- 收集类型
    def collect(self, items):
        for src, cat, u in items:
            try:
                if cat == "typedef":
                    self._collect_typedef(u, src)
                elif cat == "struct":
                    self._collect_struct(u, union=False, src=src)
                elif cat == "union":
                    self._collect_struct(u, union=True, src=src)
                elif cat == "enum":
                    self._collect_enum(u, src)
            except Exception:
                pass

    def _collect_typedef(self, u: str, src: str = ""):
        body = re.sub(r"^typedef\s+", "", u.rstrip(";").strip())
        # `typedef struct {...} Name;` / `typedef enum {...} Name;`：
        # 就地定义的类型也要收进来，而且用 typedef 的名字当 tag。
        m = re.match(r"^(struct|union|enum)\s*(\w+)?\s*\{", body)
        if m:
            tail = re.search(r"\}\s*(\w+)\s*$", body)
            alias = tail.group(1) if tail else ""
            kind, tag = m.group(1), m.group(2) or alias
            if kind == "enum":
                self._collect_enum(u, src, force_tag=alias or tag)
            else:
                self._collect_struct(u, union=(kind == "union"), src=src,
                                     force_tag=tag)
            if alias and alias != tag:
                self.typedefs[alias] = CType(kind, tag)
            if kind != "enum" and not m.group(2):
                self.struct_is_alias[tag] = True      # `typedef struct {...} X;`
            return
        p = _DeclParser(_c_tokens(body))
        base = p.parse_specs()
        ty, name = p.parse_declarator()
        ty = base if ty is None else _apply_base(ty, base)
        if name:
            self.typedefs[name] = ty

    def _collect_struct(self, u: str, union: bool, src: str, force_tag: str = ""):
        m = re.search(r"\b(struct|union)\s+(\w+)?\s*\{", u)
        if not m:
            return
        tag = force_tag or m.group(2) or ""
        if not tag:
            return
        brace = u.find("{", m.start())
        end = _match_brace(u, brace)
        if end < 0:
            return
        inner = u[brace + 1:end]
        fields, bits = self._parse_fields(inner)
        if union:
            self.structs[tag] = None
            self.unions[tag] = max([self._c_sizeof(t) for _n, t in fields] or [0])
            self.struct_src[tag] = src
            return
        self.structs[tag] = fields
        self.struct_bits[tag] = bits
        self.struct_src[tag] = src
        off, maxalign = 0, 1
        for _n, t in fields:
            s_, a_ = self._c_sizeof_align(t)
            if a_ > 0:
                off = (off + a_ - 1) // a_ * a_
            off += s_
            maxalign = max(maxalign, a_)
        self.struct_size[tag] = ((off + maxalign - 1) // maxalign * maxalign
                                 if maxalign else off)
        # `typedef struct X {...} Y;`：把 Y 也登记成同一个结构体
        tm = re.search(r"\}\s*(\w+)\s*$", u.rstrip(";").strip())
        if tm and tm.group(1) != tag:
            self.typedefs.setdefault(tm.group(1), CType("struct", tag))

    def _parse_fields(self, inner: str):
        fields = []
        bits = False
        for unit in _split_units(inner):
            u = unit.strip()
            if not u:
                continue
            if ":" in u and "(" not in u:
                bits = True                       # 位域：整个结构体按不透明处理
            p = _DeclParser(_c_tokens(u))
            try:
                base = p.parse_specs()
                ty, name = p.parse_declarator()
            except Exception:
                continue
            if not name:
                continue
            ty = base if ty is None else _apply_base(ty, base)
            fields.append((name, ty))
        return fields, bits

    def _collect_enum(self, u: str, src: str, force_tag: str = ""):
        m = re.search(r"\benum\s*(\w+)?\s*\{", u)
        if not m:
            return
        tag = force_tag or m.group(1) or ""
        brace = u.find("{", m.start())
        end = _match_brace(u, brace)
        if end < 0:
            return
        inner = u[brace + 1:end]
        vals: List[Tuple[str, int]] = []
        nxt = 0
        for part in _split_top_commas(inner):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                nm, expr = part.split("=", 1)
                v = self._eval_const(expr)
                if v is None or not isinstance(v, int):
                    continue
                nxt = v + 1
                vals.append((nm.strip(), v))
            else:
                vals.append((part, nxt))
                nxt += 1
        key = f"{src}#{tag}" if tag else f"{src}#<anon>{len(self.anon_enum_vals)}"
        self.enum_vals[key] = vals

    # ---------------------------------------------------------- ABI 探针
    def probe_layouts(self, tags: List[str], headers: List[str]) -> Dict[str, dict]:
        """用**真的 C 编译器**量每个结构体的 sizeof 与每个字段的 offsetof。

        自己按对齐规则算大小，算对了没人知道，算错了就是运行时的内存踩踏。
        所以生成一段探针 C：包含同一个头文件，把 sizeof/offsetof 打出来，
        编译、运行、读回真实数字。FA 侧的字段布局要跟它对得上才算绑成功；
        对不上（位域、我们映射不了的字段）就退化成同样大小的字节数组 ——
        这样 C 那头照样能往里写，只是 FA 侧读不了字段。
        """
        out: Dict[str, dict] = {}
        if not tags:
            return out
        import tempfile
        lines = ["#include <stdio.h>", "#include <stddef.h>"]
        for h in headers:
            lines.append(f'#include "{h}"' if not h.startswith("/")
                         else f'#include "{h}"')
        for t in tags:
            sp = self._c_spelling(t)
            lines.append(f'printf("S {t} %zu\\n", sizeof({sp}));')
        for t in tags:
            sp = self._c_spelling(t)
            for f in self.structs.get(t) or []:
                lines.append(f'printf("F {t} {f[0]} %zu\\n", offsetof({sp}, {f[0]}));')
        src = _wrap_main(lines)
        with tempfile.TemporaryDirectory() as td:
            cpath = os.path.join(td, "probe.c")
            binpath = os.path.join(td, "probe")
            with open(cpath, "w") as f:
                f.write(src)
            cmd = [self.cc, "-w", "-D_GNU_SOURCE"]
            for d in self.include_dirs:
                cmd.append(f"-I{d}")
            cmd += [cpath, "-o", binpath]
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                # 整体探针编不过（有的结构体不完整）：一个一个试
                return self._probe_one_by_one(tags, headers)
            r = subprocess.run([binpath], capture_output=True, text=True)
            if r.returncode != 0:
                return out
            for ln in r.stdout.split("\n"):
                parts = ln.split()
                if len(parts) == 3 and parts[0] == "S":
                    out.setdefault(parts[1], {"size": 0, "off": {}})["size"] = int(parts[2])
                elif len(parts) == 4 and parts[0] == "F":
                    out.setdefault(parts[1], {"size": 0, "off": {}})["off"][parts[2]] = int(parts[3])
        return out

    def _c_spelling(self, tag: str) -> str:
        """C 里怎么写这个类型：有 tag 的写 `struct X`，typedef 别名直接写 `X`。"""
        return tag if self.struct_is_alias.get(tag) else f"struct {tag}"

    def _probe_one_by_one(self, tags, headers):
        out = {}
        for t in tags:
            got = self.probe_layouts_single(t, headers)
            if got:
                out[t] = got
        return out

    def probe_layouts_single(self, tag: str, headers: List[str],
                             with_offsets: bool = True) -> Optional[dict]:
        import tempfile
        lines = ["#include <stdio.h>", "#include <stddef.h>"]
        for h in headers:
            lines.append(f'#include "{h}"')
        spell = self._c_spelling(tag)
        lines.append(f'printf("S {tag} %zu\\n", sizeof({spell}));')
        if with_offsets:
            for f in self.structs.get(tag) or []:
                lines.append(f'printf("F {tag} {f[0]} %zu\\n", '
                             f'offsetof({spell}, {f[0]}));')
        src = _wrap_main(lines)
        with tempfile.TemporaryDirectory() as td:
            cpath = os.path.join(td, "p.c")
            binp = os.path.join(td, "p")
            with open(cpath, "w") as f:
                f.write(src)
            cmd = [self.cc, "-w", "-D_GNU_SOURCE"]
            for d in self.include_dirs:
                cmd.append(f"-I{d}")
            cmd += [cpath, "-o", binp]
            if subprocess.run(cmd, capture_output=True, text=True).returncode != 0:
                if with_offsets:
                    # offsetof 对位域字段非法；只量 sizeof 再试一次
                    return self.probe_layouts_single(tag, headers, with_offsets=False)
                return None
            r = subprocess.run([binp], capture_output=True, text=True)
            if r.returncode != 0:
                return None
            res = {"size": 0, "off": {}}
            for ln in r.stdout.split("\n"):
                parts = ln.split()
                if len(parts) == 3 and parts[0] == "S":
                    res["size"] = int(parts[2])
                elif len(parts) == 4 and parts[0] == "F":
                    res["off"][parts[2]] = int(parts[3])
            return res

    # ---------------------------------------------------------- 绑定
    def bind(self) -> BindResult:
        target = set()
        items: List[Tuple[str, str, str]] = []
        ok_headers = []
        real_paths: Dict[str, str] = {}
        for h in self.headers:
            path, names = self.resolve_header(h)
            target.update(names)
            text = self.preprocess(path)
            if text is None:
                self.res.skipped.append(
                    (h, f"预处理失败：{self.cc} 读不了这个头文件（装对应的 -dev 包了吗？）"))
                continue
            ok_headers.append(h)
            real_paths[h] = path
            items.extend(self.scan(text, h))
        if not ok_headers:
            self.cleanup()
            return self.res
        self._real_paths = real_paths
        self.collect(items)

        def in_target(src: str) -> bool:
            """这条声明出自的文件要不要输出。

            默认：目标头文件自己 + 它所属那套库的头文件（math.h 的函数其实都在
            bits/mathcalls.h 里）。公共系统头文件（stdio.h / sys/types.h / bits/types.h
            这些）不算，否则绑什么都等于绑整个 glibc。--strict 只要目标文件，
            --deep 全要。
            """
            if src in target or os.path.realpath(src) in target \
                    or os.path.basename(src) in target:
                return True
            if self.deep:
                return True
            if self.strict:
                return False
            rel = _include_spelling(src, self.include_dirs)
            return rel not in STD_HEADERS and os.path.basename(src) not in STD_HEADERS

        # 结构体先定：函数签名里要写 FA 的结构体名。
        # 遍历收集到的所有结构体（而不是遍历声明单元）—— `typedef struct {...} X;`
        # 这种就地定义在声明层面是 typedef，按单元遍历会漏掉。
        # 只看**会被输出**的那些函数原型：--only 挑了 4 个函数，就没必要把整个
        # 头文件里出现过的结构体（statx / file_handle / random_data ...）都拖进来。
        proto_blob = "\n".join(u for _s, c, u in items
                               if c == "fn" and in_target(_s)
                               and self._name_wanted(_decl_name(u)))
        want_tags = []
        for tag, src in self.struct_src.items():
            if tag in self.exclude:
                continue
            if not self.private and tag.startswith(SKIP_PREFIX):
                continue
            mentioned = re.search(r"\b" + re.escape(tag) + r"\b", proto_blob)
            if not in_target(src):
                # 定义在别的文件里的结构体：bits/dirent.h 的 struct dirent、
                # bits/types/struct_timespec.h 的 struct timespec 都是这种。
                # 只要**要输出的函数原型**里提到了它就得带上 —— 不带的话
                # `struct dirent *readdir(DIR *)` 的返回类型映射不了，
                # readdir 整个函数会被悄悄丢掉（--only readdir 也拿不到东西，
                # 而且没有任何提示）。
                if not mentioned:
                    continue
            elif self.only is not None and tag not in self.only and not mentioned:
                continue
            want_tags.append(tag)
        probe_headers = [self._real_paths.get(h, h) for h in ok_headers]
        # 探针要量的不只是「会被输出」的那些：结构体字段里引用的**嵌套**结构体也得量。
        # sys/stat.h 的 st_atim 是 struct timespec，而 timespec 的定义在
        # bits/types/struct_timespec.h 里 —— 那不是目标文件，不进 want_tags，
        # 于是量不到；_plan_struct 递归到它时发现没探针，只能报「映射不了」，
        # 连累整个 struct stat 退化成 144 字节的不透明数组，st_size 都读不出来。
        probe_tags = list(want_tags)
        seen_probe = set(probe_tags)
        pending = list(want_tags)
        while pending:
            t = pending.pop(0)
            for _fn, fty in self.structs.get(t) or []:
                inner = self._struct_tag_of(fty)
                if not inner or inner not in self.structs or inner in seen_probe:
                    continue
                if inner in self.exclude:
                    continue
                if not self.private and inner.startswith(SKIP_PREFIX):
                    continue
                seen_probe.add(inner)
                probe_tags.append(inner)
                pending.append(inner)
        self.probed = {} if self.no_probe else self.probe_layouts(probe_tags, probe_headers)
        for tag in want_tags:
            self._plan_struct(tag, [])

        fn_lines: List[Tuple[str, str, str]] = []      # (来源头文件, 名字, 声明行)
        const_lines: List[Tuple[str, str]] = []
        struct_lines: List[Tuple[str, str]] = []
        seen_fn: set = set()
        seen_const: set = set()
        pending_defines: List[Tuple[str, str, str]] = []

        for src, cat, u in items:
            if not in_target(src):
                continue
            nm0 = _decl_name(u)
            if cat != "define" and not self._name_wanted(nm0):
                continue
            try:
                if cat == "fn":
                    fname, line = self._emit_fn(u)
                    if line and fname not in seen_fn:
                        seen_fn.add(fname)
                        fn_lines.append(
                            (_owner_header(src, ok_headers, self.file_owner),
                             fname, line))
                elif cat == "define":
                    pending_defines.append((src, nm0, u))   # 过滤在输出阶段做
                elif cat == "enum":
                    key = [k for k in self.enum_vals if k.startswith(src + "#")]
                    for k in key:
                        for nm, v in self.enum_vals[k]:
                            if nm in seen_const or not self._name_wanted(nm):
                                continue
                            seen_const.add(nm)
                            const_lines.append((nm, f"const {nm}: i64 = {v}"))
                elif cat == "struct":
                    tag = _tag_of(u) or nm0
                    fa = self.emitted_structs.get(tag)
                    if fa and fa not in [x[0] for x in struct_lines]:
                        struct_lines.append(self._render_struct(tag, fa))
                elif cat == "union":
                    tag = _tag_of(u) or nm0
                    self.res.skipped.append(
                        (tag, f"union：FA 没有 union（这个占 {self.unions.get(tag, '?')} 字节）。"
                              "要在 FA 侧用就写一个同样大小的字节数组自己解读"))
                elif cat == "var":
                    self.res.skipped.append(
                        (nm0, "外部变量（extern 的数据，不是函数）：FA 目前不能声明外部变量。"
                              "变通办法：让 C 那边给一个取值函数，或用 use lib + dlsym 取地址"))
                elif cat == "other":
                    if u.strip() and not u.strip().startswith("}"):
                        self.res.skipped.append(
                            (nm0 or u[:30], "不是 bindgen 认得的声明形式"))
            except Exception as e:
                self.res.skipped.append((nm0 or u[:30], f"解析失败：{type(e).__name__}: {e}"))

        for nm, line in self._resolve_defines(pending_defines):
            if nm not in seen_const:
                seen_const.add(nm)
                const_lines.append((nm, line))
        # 函数签名里引用到、但还没输出的结构体（--only 的时候常见）
        for tag, fa in list(self.emitted_structs.items()):
            if fa not in [x[0] for x in struct_lines]:
                struct_lines.append(self._render_struct(tag, fa))

        # --only 点名要、结果一个都没生成的名字：必须说出来。
        # 静默少东西最难查 —— 用户以为是头文件里没有，其实是拼错了 / 那是个宏 /
        # 它的类型映射不了。生成文件末尾的「跳过」清单就是干这个的。
        if self.only:
            got = set()
            for _h, n, _l in fn_lines:
                got.add(n)
                got.add(_strip_c_prefix(n))
            for n, _l in const_lines:
                got.add(n)
            for tag, fa in self.emitted_structs.items():
                got.add(tag)
                got.add(fa)
            for nm in sorted(self.only - got):
                self.res.skipped.append(
                    (nm, "--only 点名要它，但没有任何声明生成出来："
                         "名字拼错了？它是个宏（那要在常量里找）？还是它的类型 FA 映射不了"
                         "（看上面几条的原因）"))

        self.res.fns = [n for _h, n, _l in fn_lines]
        self.res.consts = [n for n, _ in const_lines]
        self.res.structs = [n for n, _ in struct_lines]
        self.res.header = ok_headers[0]
        self.res.lib = self.lib or GUESS_LIB.get(
            ok_headers[0], GUESS_LIB.get(os.path.basename(ok_headers[0]), ""))
        # 每个头文件自己要链哪个库（--lib 只给了一个时用这个）
        self.res.header_libs = {
            h: (self.lib or GUESS_LIB.get(h, GUESS_LIB.get(os.path.basename(h), "")))
            for h in ok_headers}
        self.res.text = self._render(ok_headers, fn_lines, const_lines,
                                     struct_lines, self.res.header_libs)
        if self.verify:
            self._verify_and_prune()
        self.cleanup()
        return self.res

    def cleanup(self):
        import shutil
        for d in getattr(self, "_tmp_dirs", []):
            shutil.rmtree(d, ignore_errors=True)
        self._tmp_dirs = []

    # --- 过滤
    def _name_wanted(self, nm: str) -> bool:
        if not nm:
            return False
        if not self.private and nm.startswith(SKIP_PREFIX):
            return False
        if nm in self.exclude:
            return False
        if self.only is not None and nm not in self.only:
            # typedef 名在 only 里、但结构体 tag 不在：也算要
            real = self.typedefs.get(nm)
            if real is not None and real.kind == "struct" and real.name in self.only:
                return True
            # --only 也认**生成物里的名字**。C 的 sqrt 撞上 FA 的内建，输出时写成
            # c_sqrt（后面带 = "sqrt" 绑回真符号）；用户照着生成出来的文件再收窄
            # 一次，写的自然是 c_sqrt。只认 C 原名的话，这个块一个函数都绑不到，
            # 生成出来只剩一句 fa_bind_nothing_found()，看着像 bindgen 坏了。
            fa_nm, _alias = self._fn_name(nm)
            if fa_nm != nm and fa_nm in self.only:
                return True
            return False
        return True

    # --- 函数
    def _emit_fn(self, u: str) -> Tuple[str, str]:
        decl = u.rstrip().rstrip(";").strip()
        decl = re.sub(r"^(extern|static)\s+", "", decl)
        p = _DeclParser(_c_tokens(decl))
        base = p.parse_specs()
        ty, name = p.parse_declarator()
        if not name:
            return "", ""
        fn = _find_fn(ty if ty is not None else base)
        if fn is None:
            self.res.skipped.append((name, "不是函数声明（可能是变量或宏）"))
            return "", ""
        ty = _apply_base(ty, base) if ty is not None else base
        ret = fn.ret if fn.ret is not None else CType("base", "void")
        r_fa, r_note = self.map_type(ret, pos="return", name=name)
        if r_fa is None:
            self.res.skipped.append((name, f"返回值 {r_note}"))
            return "", ""
        params = []
        for i, (cname, pt) in enumerate(fn.params):
            fa, note = self.map_type(pt, pos="param", name=name, index=i)
            if fa is None:
                self.res.skipped.append((name, f"第 {i+1} 个参数（{cname or '?'}）{note}"))
                return "", ""
            params.append(f"{_safe_param_name(cname, i)}: {fa}")
        ps = ", ".join(params)
        if fn.varargs:
            ps = ps + ", ..." if ps else "..."
        ret_txt = "" if r_fa == "void" else f" -> {r_fa}"
        fa_name, alias = self._fn_name(name)
        suffix = f' = "{alias}"' if alias else ""
        note = ""
        if fn.varargs:
            note = ("    # 变参函数：调用时按 C 的规矩传（浮点走 xmm，"
                    "char/short 会提升成 int）\n")
        return fa_name, note + f"fn {fa_name}({ps}){ret_txt}{suffix}"

    def _fn_name(self, c_name: str) -> Tuple[str, Optional[str]]:
        if c_name in FA_RESERVED:
            return f"c_{c_name}", c_name
        return c_name, None

    # --- 常量（#define 与 enum）
    def _resolve_defines(self, pending):
        """宏常量按依赖关系反复求值：#define B (A << 1) 要等 A 先算出来。"""
        out: List[Tuple[str, str]] = []
        known: Dict[str, object] = {}
        waiting = list(pending)
        for _round in range(12):
            still = []
            for src, nm, u in waiting:
                m = re.match(r"^#define\s+(\w+)(\([^)]*\))?\s*(.*)$", u.strip(), re.S)
                if not m:
                    continue
                name, args, val = m.group(1), m.group(2), m.group(3).strip()
                if args:
                    if self._name_wanted(name):
                        self.res.skipped.append(
                            (name, "函数式宏（带参数的 #define）：FA 没有宏，写个 fn 就行"))
                    continue
                if not val:
                    if self._name_wanted(name):
                        self.res.skipped.append((name, "#define 的值是空的"))
                    continue
                val = re.sub(r"/\*.*?\*/", " ", val, flags=re.S).strip()
                val = re.sub(r"\\$", "", val).strip()
                if val.startswith('"') and val.endswith('"') and val.count('"') == 2:
                    known[name] = val
                    if self._name_wanted(name):
                        out.append((name, f"const {name}: str = {val}"))
                    continue
                v = self._eval_const(val, known)
                if isinstance(v, bool):
                    v = int(v)
                if isinstance(v, (int, float)):
                    known[name] = v
                    if self._name_wanted(name):
                        ty = "f64" if isinstance(v, float) else "i64"
                        txt = repr(v) if isinstance(v, float) else str(v)
                        out.append((name, f"const {name}: {ty} = {txt}"))
                else:
                    still.append((src, nm, u))
            if not still or len(still) == len(waiting):
                waiting = still
                break
            waiting = still
        for _src, nm, u in waiting:
            if not self._name_wanted(nm):
                continue                      # 只是中间量，不输出也不算跳过
            m = re.match(r"^#define\s+(\w+)(\([^)]*\))?\s*(.*)$", u.strip(), re.S)
            val = " ".join((m.group(3) if m else u).split())[:70]
            self.res.skipped.append(
                (nm, f"宏的值 FA 编译期算不出来（含 sizeof / 取地址 / 指针转换 / 未知符号）：{val}"))
        return out

    def _eval_const(self, expr: str, extra: Optional[Dict[str, object]] = None):
        """算一个 C 常量表达式。算不出来返回 None（绝不猜）。"""
        e = re.sub(r"/\*.*?\*/", " ", expr, flags=re.S).strip()
        e = re.sub(r"\\\s*$", "", e).strip()
        if not e:
            return None
        e, ptr_cast = _strip_casts(e)
        if ptr_cast:
            return None                            # (void*)0 这类：FA const 装不下指针
        if not e:
            return None
        e = e.replace("&&", " and ").replace("||", " or ")
        if not re.fullmatch(r"[0-9A-Fa-fxXuUlL\s\+\-\*\/\%\(\)\<\>\|\&\^\~\,\.A-Za-z_]*", e):
            return None
        env: Dict[str, object] = {}
        for k, v in self._const_env().items():
            env[k] = v
        if extra:
            env.update(extra)
        # 数字字面量 → Python 能读的形式（去掉 U/L 后缀）
        def num_fix(mm):
            return str(_c_num(mm.group(0)) if _c_num(mm.group(0)) is not None else 0)
        e = re.sub(r"\b(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*\b", num_fix, e)
        if re.search(r"\bsizeof\b|\?", e):
            return None
        try:
            v = eval(e, {"__builtins__": {}}, env)   # 只含数字/运算符/已知常量
        except Exception:
            return None
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            v &= (1 << 64) - 1
            return v - (1 << 64) if v >= (1 << 63) else v
        if isinstance(v, float):
            return v
        return None

    def _const_env(self) -> Dict[str, object]:
        """已经收进来的枚举值，可以在宏表达式里用。"""
        env: Dict[str, object] = {}
        for _k, vals in self.enum_vals.items():
            for nm, v in vals:
                env.setdefault(nm, v)
        return env

    # --- 结构体
    def _plan_struct(self, tag: str, visiting: List[str]):
        """决定一个 C 结构体在 FA 侧长什么样。

        三种结局：
          1. 字段全能映射，且**探针量出来的 sizeof/offsetof 与 FA 的布局逐个对得上**
             → 原样输出 FA 结构体，两边可以直接互传指针。
          2. 有位域 / 有映射不了的字段 / 布局对不上，但探针量到了大小
             → 输出一个同样大小的字节数组结构体（`raw: [u8; N]`）。
             C 那头照样能往里读写，FA 侧只是看不见字段 —— 这比 `*void` 强，
             因为可以在 FA 侧直接分配（`let r: regex_t`）再取地址传过去。
          3. 连大小都量不到 → 跳过，只留 `*void`，并在文末写明原因。
        """
        if tag in self.emitted_structs or tag in visiting:
            return
        fields = self.structs.get(tag)
        if fields is None:
            return                                  # union 或没定义
        if not self.private and tag.startswith(SKIP_PREFIX):
            return
        probe = self.probed.get(tag)
        fa_fields: List[Tuple[str, str, str]] = []   # (FA 字段名, 原字段名, FA 类型)
        problems: List[str] = []
        if self.struct_bits.get(tag):
            problems.append("结构体里有位域（`unsigned x : 3` 这种），FA 没有位域")
        for fname, fty in fields:
            inner_tag = self._struct_tag_of(fty)
            if inner_tag and inner_tag in self.structs:
                self._plan_struct(inner_tag, visiting + [tag])
            fa, note = self.map_type(fty, pos="field", name=f"{tag}.{fname}")
            if fa is None:
                problems.append(f"字段 {fname}：{note}")
                continue
            fa_fields.append((_safe_field_name(fname), fname, fa))
        fa_name = f"C{tag}" if tag in FA_RESERVED else tag

        layout_ok = False
        if not problems and probe and fa_fields:
            layout_ok, detail = self._check_layout(fa_fields, probe, tag)
            if not layout_ok:
                problems.append(f"布局与 C 对不上（{detail}）")
        elif not problems and not probe:
            problems.append("没能量到 C 侧的 sizeof/offsetof（探针没编过），"
                            "不敢断定布局一致")

        if layout_ok:
            self.emitted_structs[tag] = fa_name
            self.struct_fields[fa_name] = [(n, t) for n, _o, t in fa_fields]
            self.struct_kind[fa_name] = "real"
            self.struct_note[fa_name] = (
                f"由 C 的 struct {tag} 生成；sizeof = {probe['size']} 字节，"
                "每个字段的 offsetof 都用真编译器量过，与 FA 的布局逐个对得上，"
                "两边可以直接互传指针")
            return

        size = (probe or {}).get("size")
        if size:
            self.emitted_structs[tag] = fa_name
            self.struct_fields[fa_name] = [("raw", f"[u8; {size}]")]
            self.struct_kind[fa_name] = "raw"
            self.struct_note[fa_name] = (
                f"C 的 struct {tag} 在 FA 侧只能按不透明的 {size} 字节用："
                + "；".join(problems)
                + "。字段 FA 看不见，但大小是对的 —— 可以在 FA 侧分配"
                  "（let x: 名字，全零）再取地址传给 C，由 C 那头填")
            return
        self.res.skipped.append(
            (tag, "；".join(problems) + "（连 sizeof 都没量到，只能按 *void 用）"))

    def _struct_tag_of(self, ty: Optional[CType]) -> str:
        """顺着 typedef / 指针 / 数组找到最里面的 struct tag（用来先规划嵌套结构体）。"""
        hops = 0
        while ty is not None and ty.kind == "td" and hops < 20:
            ty = self.typedefs.get(ty.name)
            hops += 1
        while ty is not None and ty.kind in ("ptr", "arr") and hops < 40:
            ty = ty.inner
            hops += 1
        if ty is not None and ty.kind == "struct":
            return ty.name
        return ""

    def _check_layout(self, fa_fields, probe: dict, tag: str) -> Tuple[bool, str]:
        """按 FA 的对齐规则算一遍偏移，跟探针量出来的逐个比。"""
        off, maxalign = 0, 1
        for fa_name, orig, fa in fa_fields:
            sz, al = self._fa_size_align(fa)
            if al > 0:
                off = (off + al - 1) // al * al
            real = probe["off"].get(orig)
            if real is not None and real != off:
                return False, f"字段 {orig}：C 在 {real}，FA 会放在 {off}"
            off += sz
            maxalign = max(maxalign, al)
        size = (off + maxalign - 1) // maxalign * maxalign if maxalign else off
        if probe.get("size") and size != probe["size"]:
            return False, f"sizeof：C 是 {probe['size']}，FA 会算成 {size}"
        return True, ""

    def _fa_size_align(self, fa: str) -> Tuple[int, int]:
        m = re.fullmatch(r"\[(.+); (\d+)\]", fa)
        if m:
            s_, a_ = self._fa_size_align(m.group(1))
            return s_ * int(m.group(2)), a_
        if fa.startswith("*") or fa in ("str", "usize", "isize"):
            return 8, 8
        if fa in _FA_SIZES:
            n = _FA_SIZES[fa]
            return n, min(n, 8)
        if fa in self.struct_fields:                # 嵌套的、已经规划好的结构体
            return sum(self._fa_size_align(t)[0] for _n, t in self.struct_fields[fa]), 8
        return 8, 8

    def _render_struct(self, tag: str, fa_name: str) -> Tuple[str, str]:
        fields = self.struct_fields.get(fa_name) or []
        lines = [f"struct {fa_name}:"]
        for fname, fa in fields:
            lines.append(f"    {fname}: {fa}")
        lines.append("# " + self.struct_note.get(fa_name, f"由 C 的 struct {tag} 生成"))
        return fa_name, "\n".join(lines)

    # --- 类型映射
    def _td_target(self, nm: str) -> Optional[CType]:
        """typedef 名 -> 它真正指着的类型；认不出来给 None。

        两种情形：
          * `typedef unsigned long size_t;` —— typedefs 表里有；
          * `typedef struct { ... } regmatch_t;` —— **就地定义**，收集器把字段记在
            regmatch_t 名下（structs 表 + struct_is_alias），typedefs 表里没有它。
        第二种以前一律按「认不得的 typedef = 不透明句柄」给 *void，于是
        regexec 的 `regmatch_t pmatch[]`、一堆库的 `xxx_t *` 参数全退化成 *void，
        明明结构体就在同一个文件里生成出来了，用户还得自己 as 一次。
        """
        real = self.typedefs.get(nm)
        if real is not None:
            return real
        if nm in self.structs and self.structs[nm] is not None:
            return CType("struct", name=nm)
        return None

    def map_type(self, ty: Optional[CType], pos: str, name: str, index: int = 0
                 ) -> Tuple[Optional[str], str]:
        if ty is None:
            return None, "类型解析不出来"
        hops = 0
        while ty.kind == "td" and hops < 20:
            nm = ty.name
            if nm in FRIENDLY:
                return FRIENDLY[nm], ""
            if nm in UNMAPPABLE:
                return None, UNMAPPABLE[nm]
            if nm in BASE_TO_FA:
                return BASE_TO_FA[nm], ""
            real = self._td_target(nm)
            if real is None:
                return "*void", ""                 # 认不得的 typedef = 不透明句柄
            ty = real
            hops += 1
        if ty.kind == "base":
            b = ty.name
            if b in UNMAPPABLE:
                return None, UNMAPPABLE[b]
            if b in BASE_TO_FA:
                return BASE_TO_FA[b], ""
            return None, f"C 基本类型 '{b}' FA 侧没有对应写法"
        if ty.kind == "enum":
            return "i32", ""                       # C 的 enum 在 ABI 里就是 int
        if ty.kind in ("struct", "union"):
            tag = ty.name
            if pos == "field":
                if ty.kind == "union":
                    return None, "FA 没有 union"
                if tag in self.emitted_structs:
                    return self.emitted_structs[tag], ""
                if not tag:
                    return None, "匿名结构体（FA 的结构体必须有名字）"
                if tag in self.structs and self.structs[tag] is None:
                    return None, f"union {tag}：FA 没有 union"
                if tag not in self.structs:
                    return None, f"不完整类型 struct {tag}（头文件里只有前向声明）"
                return None, f"结构体 {tag} 映射不了（见它的说明）"
            if ty.kind == "struct" and tag in self.emitted_structs:
                return "*" + self.emitted_structs[tag], ""
            return "*void", ""                     # 不透明句柄 / union / 没绑的结构体
        if ty.kind == "ptr":
            inner = ty.inner
            if inner is None:
                return "*void", ""
            h2 = 0
            while inner.kind == "td" and h2 < 20:
                if inner.name in FRIENDLY:
                    fa = FRIENDLY[inner.name]
                    return ("*void" if fa.startswith("*") else "*" + fa), ""
                if inner.name in UNMAPPABLE:
                    return None, UNMAPPABLE[inner.name]
                real = self._td_target(inner.name)
                if real is None:
                    return "*void", ""
                inner = real
                h2 += 1
            if inner.kind == "fn":
                fa, note = self.map_fn_type(inner)
                return (fa, note) if fa else ("*void", "")
            if inner.kind in ("struct", "union"):
                tag = inner.name
                if inner.kind == "struct" and tag in self.emitted_structs:
                    return "*" + self.emitted_structs[tag], ""
                if inner.kind == "struct" and pos == "field" and tag in self.structs \
                        and self.structs[tag] is not None:
                    return None, f"嵌套结构体 {tag} 没能输出（见它的说明）"
                return "*void", ""
            if inner.kind == "enum":
                return "*i32", ""
            if inner.kind == "ptr":
                # 指针的指针（char **argv）：FA 写得出来，但 `**` 会被词法分析当成
                # 乘方运算符；统一按不透明指针给，要用时 as 转。
                return "*void", ""
            if inner.kind == "arr":
                return "*void", ""
            if inner.kind == "base" and inner.name == "void":
                return "*void", ""
            if inner.kind == "base" and inner.name == "char":
                # const char* → str（FA 自动转；C 返回的 char* 也自动拷成 str）；
                # 非 const 的 char* 通常是要写进去的缓冲区，映射成 *u8 更安全。
                if pos == "return" or "const" in (ty.qual or ""):
                    if pos == "return" and name in self.ptr_return:
                        return "*u8", ""
                    return "str", ""
                return "*u8", ""
            fa, note = self.map_type(inner, pos="inner", name=name)
            if fa is None:
                return None, note
            if fa == "void":
                return "*void", ""
            return "*" + fa, ""
        if ty.kind == "arr":
            inner = ty.inner
            if pos == "field":
                fa, note = self.map_type(inner, pos="inner", name=name)
                if fa is None:
                    return None, note
                if ty.count is None:
                    return None, "不定长数组字段（柔性数组成员）FA 表达不了"
                return f"[{fa}; {ty.count}]", ""
            # 参数/返回位置的数组退化成指针：`regmatch_t pmatch[]` 就是
            # `regmatch_t *pmatch`。元素类型要按**值**映射（结构体给名字，不是给
            # 指针），所以借 pos="field" 那条路 —— 它返回裸类型名。以前按参数位置
            # 映射，结构体先被包了一层指针，再撞上下面「已经是指针的不能再包一层」
            # 的保护，regexec 的 pmatch 就成了 *void（结构体明明生成出来了）。
            fa, note = self.map_type(inner, pos="field", name=name)
            if fa is None:
                return None, note
            # 元素本身就是指针（char *argv[]）：FA 写不出 `**void`
            # （词法分析把 `**` 当乘方运算符），只能按不透明指针给，用时 as 转。
            if fa == "void" or fa.startswith("*"):
                return "*void", ""
            return "*" + fa, ""
        if ty.kind == "fn":
            fa, note = self.map_fn_type(ty)
            return (fa, note) if fa else ("*void", "")
        return None, f"认不得的类型 {ty!r}"

    def map_fn_type(self, fn: CType) -> Tuple[Optional[str], str]:
        """函数指针 → FA 的一等函数类型 `fn(A, B) -> R`。

        FA 的函数就是 C ABI，所以 FA 函数可以直接当回调传给 C
        （qsort / sqlite3_exec / libcurl 的写回调都是这么用的）。
        """
        ps = []
        for pn, pt in fn.params:
            if pt is None:
                return None, "回调参数类型解析不出来"
            fa, note = self.map_type(pt, pos="callback-param", name="<回调>")
            if fa is None:
                return None, f"回调参数 {note}"
            ps.append(fa)
        if fn.ret is None:
            return None, "回调返回类型解析不出来"
        r_fa, note = self.map_type(fn.ret, pos="callback-return", name="<回调>")
        if r_fa is None:
            return None, f"回调返回值 {note}"
        ret = "" if r_fa == "void" else f" -> {r_fa}"
        return f"fn({', '.join(ps)}){ret}", ""

    # --- 布局
    def _c_sizeof_align(self, ty: Optional[CType]) -> Tuple[int, int]:
        hops = 0
        while ty is not None and ty.kind == "td" and hops < 20:
            if ty.name in FRIENDLY:
                fa = FRIENDLY[ty.name]
                if fa.startswith("*"):
                    return 8, 8
                s_ = _FA_SIZES.get(fa, 8)
                return s_, s_
            if ty.name in BASE_TO_FA:
                ty = CType("base", BASE_TO_FA[ty.name])
                break
            real = self.typedefs.get(ty.name)
            if real is None:
                return 8, 8
            ty = real
            hops += 1
        if ty is None:
            return 8, 8
        if ty.kind == "base":
            if ty.name in BASE_TO_FA:
                s_ = _FA_SIZES.get(BASE_TO_FA[ty.name], 8)
                return s_, min(s_, 8) or 1
            s_, a_ = _C_SIZES.get(ty.name, (8, 8))
            return (s_ or 1), a_
        if ty.kind == "enum":
            return 4, 4
        if ty.kind in ("ptr", "fn"):
            return 8, 8
        if ty.kind == "arr":
            s_, a_ = self._c_sizeof_align(ty.inner)
            return s_ * (ty.count or 0), a_
        if ty.kind == "struct":
            sz = self.struct_size.get(ty.name)
            return (sz if sz else 8), 8
        if ty.kind == "union":
            return self.unions.get(ty.name, 8), 8
        return 8, 8

    def _c_sizeof(self, ty: Optional[CType]) -> int:
        return self._c_sizeof_align(ty)[0]

    # ---------------------------------------------------------- 输出
    def _render(self, headers, fn_lines, const_lines, struct_lines,
                header_libs) -> str:
        libs = sorted({l for l in header_libs.values() if l})
        out = ["# 由 `fa bind` 自动生成 —— 不要手改，改了下次生成会覆盖。",
               f"# 命令：fa bind {' '.join(headers)}"
               + (f" --lib {self.lib}" if self.lib else "")
               + (" --deep" if self.deep else "")
               + (" --strict" if self.strict else "")
               + (" --private" if self.private else ""),
               f"# 头文件：{'、'.join(headers)}（预处理：{self.cc} -E -dD -D_GNU_SOURCE）",
               f"# 结果：函数 {len(fn_lines)} 个，常量 {len(const_lines)} 个，"
               f"结构体 {len(struct_lines)} 个，跳过 {len(self.res.skipped)} 项"
               + (f"，链接 {' '.join('-l' + l for l in libs)}" if libs else ""),
               ""]
        self._line_owner = {}
        # 按头文件分组：每组一个 use c 块，写自己的头文件与自己的 lib
        by_header: Dict[str, List[Tuple[str, str]]] = {}
        for h in headers:
            by_header[h] = []
        for h, nm, line in fn_lines:
            by_header.setdefault(h, []).append((nm, line))
        for h in headers:
            group = by_header.get(h) or []
            lib = header_libs.get(h, "")
            libtxt = f' lib "{lib}"' if lib else ""
            out.append(f'use c "{_include_spelling(h, self.include_dirs)}"{libtxt}:')
            if group:
                for nm, line in group:
                    for ln in line.split("\n"):
                        out.append("    " + ln if ln.startswith("fn ") else ln)
                    self._line_owner[len(out)] = nm
            else:
                out.append("    fn fa_bind_nothing_found() -> void")
                self._line_owner[len(out)] = "fa_bind_nothing_found"
            out.append("")
        for nm, body in struct_lines:
            for ln in body.split("\n"):
                out.append(ln)
                if ln.startswith("struct "):
                    self._line_owner[len(out)] = nm
            out.append("")
        for nm, line in const_lines:
            out.append(line)
            self._line_owner[len(out)] = nm
        if const_lines:
            out.append("")
        if self.res.skipped or self.res.dropped:
            out.append("# ---- 跳过的（原因都在这儿，没有悄悄少东西）----")
            seen = set()
            for nm, why in self.res.skipped:
                if (nm, why) in seen:
                    continue
                seen.add((nm, why))
                out.append(f"#   {nm}：{why}")
            for nm, why in self.res.dropped:
                out.append(f"#   {nm}：`fa check` 过不去，已丢掉 —— {why}")
            out.append("")
        return "\n".join(out) + "\n"

    # ---------------------------------------------------------- 校验
    def _verify_and_prune(self):
        """用 FA 自己的前端把生成的文件过一遍：过不了的声明丢掉并记下原因。

        绑真实头文件总会撞上 FA 表达不了、或者本模块判断失误的写法。
        让用户去读一屏类型错误再手工删声明，不如生成时就删干净。
        """
        from .driver import frontend
        for _round in range(80):
            r = frontend(self.res.text, "<fa bind>", 2)
            if r.ok:
                return
            msg = (r.error or "").strip()
            m = re.search(r"行\s*(\d+)", msg)
            first = msg.split("\n")[0][:160]
            if not m:
                self.res.dropped.append(("<整个文件>", first))
                return
            victim = self._line_owner.get(int(m.group(1)))
            if victim is None or not self._drop(victim):
                self.res.dropped.append((victim or "<未定位>", first))
                return
            self.res.dropped.append((victim, first))

    def _drop(self, name: str) -> bool:
        lines = self.res.text.split("\n")
        for i, ln in enumerate(lines):
            if ln.startswith(f"struct {name}:"):
                j = i + 1
                while j < len(lines) and (lines[j].startswith("    ")
                                          or lines[j].startswith("#")):
                    j += 1
                del lines[i:j]
                self.res.text = "\n".join(lines)
                if name in self.res.structs:
                    self.res.structs.remove(name)
                return True
        pat = re.compile(r"^(    fn |const )" + re.escape(name) + r"\b")
        for i, ln in enumerate(lines):
            if pat.match(ln):
                del lines[i]
                self.res.text = "\n".join(lines)
                for lst in (self.res.fns, self.res.consts):
                    if name in lst:
                        lst.remove(name)
                return True
        return False


def _strip_c_prefix(nm: str) -> str:
    """FA 侧名字 -> C 名字：c_sqrt -> sqrt（本来就是 C 名的原样返回）。"""
    return nm[2:] if nm.startswith("c_") and nm[2:3].islower() else nm


def _owner_header(src: str, headers: List[str],
                  file_owner: Optional[Dict[str, str]] = None) -> str:
    """预处理输出里的文件路径 → 用户在命令行写的那个头文件名。

    两种情况会对不上：
      * 绑 sys/stat.h 时，声明其实来自 /usr/include/x86_64-linux-gnu/sys/stat.h
        （Debian 的多架构布局），可 `use c` 里该写的是用户给的 sys/stat.h；
      * 声明出自被**间接**include 进来的文件：math.h 的函数真身全在
        bits/mathcalls.h 里，dirent.h 会带出 bits/dirent.h。这种要靠 file_owner
        （scan 时记下的「谁把它带进来的」）才知道归属。
    以前没有第二层，间接文件一律算到 headers[0] 头上：一次绑
    dirent.h + time.h + math.h，sqrt 会被写进 dirent.h 那块（挂 -lc），
    math.h 那块反而空了（挂 -lm 却没人用）—— 链接必错。
    """
    if file_owner:
        for key in (src, os.path.realpath(src)):
            if key in file_owner:
                return file_owner[key]
    rp = os.path.realpath(src)
    for h in headers:
        if os.path.exists(h) and os.path.realpath(h) == rp:
            return h
        if src.endswith("/" + h) or src == h or os.path.basename(src) == os.path.basename(h):
            return h
    if file_owner:
        # 还是对不上：找一个「和它同一套」的用户头文件，别一股脑塞给第一个
        base = os.path.dirname(rp)
        for f, h in file_owner.items():
            if os.path.dirname(os.path.realpath(f)) == base and h in headers:
                return h
    return headers[0]


def _safe_param_name(nm: str, i: int) -> str:
    """C 的参数名 → FA 的参数名：glibc 爱加下划线（__preg），撞关键字的也要换。"""
    if not nm:
        return f"a{i}"
    clean = nm.lstrip("_")
    if not clean:
        return f"a{i}"
    if clean in FA_RESERVED:
        # `rename(const char *old, const char *new)`：new 在 FA 里是关键字，
        # 直接叫 a1 看不出对应哪个参数，加个下标后缀更像原名。
        return f"{clean}_{i}"
    return clean


def _safe_field_name(nm: str) -> str:
    clean = nm.lstrip("_") or nm
    return f"f_{clean}" if clean in FA_RESERVED else clean


def _wrap_main(lines: List[str]) -> str:
    """把 #include 留在外面，printf 放进 main —— 顺序错了任何含 inline 函数的
    头文件都会报 invalid storage class。"""
    n_inc = 0
    while n_inc < len(lines) and lines[n_inc].startswith("#include"):
        n_inc += 1
    return "\n".join(lines[:n_inc] + ["int main(void) {"]
                     + ["  " + l for l in lines[n_inc:]]
                     + ["  return 0;", "}"])


def _match_paren(text: str, start: int) -> int:
    """text[start] 必须是 '('，返回配对的 ')' 下标；配不上返回 -1。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _strip_casts(e: str) -> Tuple[str, bool]:
    """剥掉 C 的强制转换与多余括号，返回 (表达式, 是否遇到指针/void 转换)。

    `(unsigned long int) 1` → ("1", False)；`(void *) 0` → ("0", True)，
    调用方看到 True 就该放弃：FA 的 const 装不下指针。
    """
    ptr_cast = False
    for _ in range(12):
        e = e.strip()
        if not e.startswith("("):
            break
        end = _match_paren(e, 0)
        if end < 0:
            break
        if end == len(e) - 1:
            e = e[1:-1]                            # (X) 整个被括号包着 → 脱掉
            continue
        inner = e[1:end]
        if re.fullmatch(r"[\w\s\*]+", inner) and re.search(r"[A-Za-z_]", inner):
            if "*" in inner or re.search(r"\bvoid\b", inner):
                ptr_cast = True
            e = e[end + 1:]                        # (类型) 表达式 → 留下表达式
            continue
        break
    return e.strip(), ptr_cast


def _split_top_commas(text: str) -> List[str]:
    """按顶层逗号切（枚举值列表用），花括号/圆括号里的逗号不算。"""
    parts, buf, depth = [], [], 0
    for c in text:
        if c in "{([":
            depth += 1
        elif c in "})]":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(c)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


# ------------------------------------------------------------------ 对外入口
def bind(headers: List[str], lib: str = "", only=None, exclude=None, deep=False,
         ptr_return=None,
         private=False, verify=True, cc="cc", include_dirs=None, defines=None,
         no_probe=False, strict=False) -> BindResult:
    b = Binder(list(headers), lib=lib, only=only, exclude=exclude, deep=deep,
               ptr_return=ptr_return,
               private=private, verify=verify, cc=cc,
               include_dirs=include_dirs, defines=defines, no_probe=no_probe,
               strict=strict)
    return b.bind()


USAGE = """fa bind —— 把 C 头文件自动翻成 FA 的绑定

用法:
  fa bind <头文件.h> [更多头文件...] [选项]

选项:
  -o <文件.fa>     输出到文件（默认 <头文件名>_fa.fa；写 - 表示打到 stdout）
  --lib <名字>     链接的库（"m" -> -lm；也可以给 .so/.a 的路径）。不给就按头文件名猜
  --only a,b,c     只绑这几个名字（函数 / 常量 / 结构体）。
                   C 名字和生成物里的名字都认：--only sqrt 和 --only c_sqrt 是一回事
                   （sqrt 撞上 FA 内建，输出时会改名成 c_sqrt）
  --exclude a,b    跳过这几个名字
  --ptr-return a,b 这几个函数的 char* 返回值保留成 *u8（不转 str），
                   因为它们返回 NULL 是有意义的：strptime 解析失败给 NULL，
                   strchr 没找到给 NULL，getenv 变量不存在给 NULL
  --include <dir>  预处理时的 -I（可多次给）
  --define <X=1>   预处理时的 -D（可多次给）
  --strict         只要目标头文件自己的声明（默认还要它所属那套库的分片，
                   例如 math.h 的函数其实都声明在 bits/mathcalls.h 里）
  --deep           连被包含的**一切**头文件里的声明一起输出（整个 glibc 都会来）
  --private        连下划线开头的内部符号一起输出（默认跳过）
  --no-verify      不要用 fa check 复核输出（默认会复核，过不了的声明就地丢掉）
  --cc <编译器>    用哪个 C 编译器做预处理（默认 cc）

示例:
  fa bind /usr/include/zlib.h --lib z -o zlib.fa
  fa bind regex.h --only regcomp,regexec,regfree,regerror -o re.fa
  fa bind math.h --lib m --only sqrt,sin,cos,pow -

生成的文件长这样（每个跳过项都写明原因，不悄悄少东西）:
  use c "zlib.h" lib "z":
      fn compress(dest: *u8, destLen: *usize, source: str, sourceLen: usize) -> i32
  const Z_OK: i64 = 0
  struct z_stream_s:
      ...
"""


def main(argv: List[str]) -> int:
    headers: List[str] = []
    out = None
    lib = ""
    only = None
    exclude = None
    ptr_return = None
    deep = private = False
    strict = False
    verify = True
    ccname = "cc"
    incs: List[str] = []
    defs: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-o":
            out = argv[i + 1]; i += 2; continue
        if a == "--lib":
            lib = argv[i + 1]; i += 2; continue
        if a == "--only":
            only = [x for x in argv[i + 1].split(",") if x]; i += 2; continue
        if a == "--exclude":
            exclude = [x for x in argv[i + 1].split(",") if x]; i += 2; continue
        if a == "--ptr-return":
            ptr_return = [x for x in argv[i + 1].split(",") if x]; i += 2; continue
        if a == "--include":
            incs.append(argv[i + 1]); i += 2; continue
        if a == "--define":
            defs.append(argv[i + 1]); i += 2; continue
        if a == "--cc":
            ccname = argv[i + 1]; i += 2; continue
        if a == "--deep":
            deep = True; i += 1; continue
        if a == "--strict":
            strict = True; i += 1; continue
        if a == "--private":
            private = True; i += 1; continue
        if a == "--no-verify":
            verify = False; i += 1; continue
        if a in ("-h", "--help", "help"):
            print(USAGE); return 0
        if a.startswith("-"):
            print(f"错误：不认识的选项 {a}\n")
            print(USAGE)
            return 1
        headers.append(a)
        i += 1
    if not headers:
        print(USAGE)
        return 1
    for h in headers:
        if os.path.exists(h):
            continue
        probe = subprocess.run([ccname, "-E", "-x", "c", "-"],
                               input=f"#include <{h}>\n",
                               capture_output=True, text=True)
        if probe.returncode != 0:
            last = probe.stderr.strip().splitlines()[-1] if probe.stderr.strip() else ""
            print(f"错误：找不到头文件 {h}")
            if last:
                print(f"  {last}")
            print("  提示：需要装对应的 -dev 包（Debian/Ubuntu: apt install <库名>-dev）")
            return 1
    res = bind(headers, lib=lib, only=only, exclude=exclude, deep=deep,
               ptr_return=ptr_return,
               private=private, verify=verify, cc=ccname, include_dirs=incs,
               defines=defs, strict=strict)
    if not res.text:
        print("错误：没能从头文件里绑出任何东西")
        for nm, why in res.skipped[:20]:
            print(f"  {nm}：{why}")
        return 1
    if out == "-":
        import sys as _sys
        _sys.stdout.write(res.text)
    else:
        path = out
        if not path:
            base = os.path.basename(headers[0])
            base = base[:-2] if base.endswith(".h") else base
            path = f"{base}_fa.fa"
        with open(path, "w", encoding="utf-8") as f:
            f.write(res.text)
        print(f"✓ 绑定写入 {path}")
    print(f"  函数 {len(res.fns)} 个，常量 {len(res.consts)} 个，结构体 {len(res.structs)} 个")
    if res.skipped or res.dropped:
        print(f"  跳过 {len(res.skipped)} 项"
              + (f"，校验时丢掉 {len(res.dropped)} 项" if res.dropped else "")
              + "（原因写在生成文件的末尾）")
    if not res.lib:
        print("  没猜出要链接哪个库：生成文件的 use c 没带 lib，需要自己加"
              "（--lib m / --lib z / --lib curl ...）")
    return 0
