#!/usr/bin/env python3
"""`fa bind` 的端到端测试：绑真头文件 → 检查生成物 → 编译运行用它的程序。

    python3 tests/run_bindgen.py            # 跑全部
    python3 tests/run_bindgen.py -v         # 打印每个头文件的统计

bindgen 的输出是机器生成的、而且依赖本机装了哪些 -dev 包，所以**不入库**：
这里每次都现场生成，再拿 FA 自己的前端和链接器去验。

三件事必须成立：
  1. 生成物过 `fa check`（bindgen 自己会校验并丢掉过不了的声明，这里再验一遍）；
  2. 该绑到的东西真的绑到了（按头文件逐条断言，防止「悄悄少东西」）；
  3. 用生成物写的程序能**编译、链接、跑对** —— 光过类型检查不算数，
     符号名错一个就是链接失败，结构体布局差一个字节就是运行时的垃圾数据。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FA = os.path.join(ROOT, "bin", "fa")
CASES = []          # (头文件, lib, 断言列表, 说明)


def have(header: str) -> bool:
    if os.path.exists(header):
        return True
    p = subprocess.run(["cc", "-E", "-x", "c", "-"],
                       input=f"#include <{header}>\n",
                       capture_output=True, text=True)
    return p.returncode == 0


def run(cmd, cwd=None, timeout=300):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


# ------------------------------------------------------------------ 生成 + 断言
def check_header(td: str, header: str, lib: str, asserts, note: str,
                 verbose: bool, extra=None, tag: str = "") -> bool:
    out = os.path.join(td, (tag or os.path.basename(header).replace(".", "_")) + ".fa")
    cmd = [FA, "bind", header, "-o", out]
    if lib:
        cmd += ["--lib", lib]
    if extra:
        cmd += list(extra)
    r = run(cmd, timeout=600)
    if r.returncode != 0:
        print(f"✗ {header}{' ' + tag if tag else ''}：fa bind 失败\n{r.stdout}{r.stderr}")
        return False
    text = open(out, encoding="utf-8").read()
    ok = True
    for a in asserts:
        if a.startswith("!"):
            if a[1:] in text:
                print(f"✗ {header}：不该出现 {a[1:]!r}")
                ok = False
        elif a not in text:
            print(f"✗ {header}：没绑出 {a!r}")
            ok = False
    # 生成物必须过 fa check
    r2 = run([FA, "check", out], timeout=300)
    if r2.returncode != 0:
        print(f"✗ {header}：生成物过不了 fa check\n{r2.stdout}{r2.stderr}")
        ok = False
    # 「校验时丢掉」意味着 bindgen 自己判断失误，要看得见
    if "已丢掉" in text:
        dropped = [l for l in text.split("\n") if "已丢掉" in l]
        print(f"✗ {header}：有 {len(dropped)} 条声明过不了校验被丢掉：")
        for l in dropped[:5]:
            print("   ", l.strip())
        ok = False
    if verbose or not ok:
        nf = text.count("\n    fn ")
        nc = len([l for l in text.split("\n") if l.startswith("const ")])
        ns = len([l for l in text.split("\n") if l.startswith("struct ")])
        nk = len([l for l in text.split("\n") if l.startswith("#   ")])
        print(f"  {header:<28} 函数 {nf:<4} 常量 {nc:<4} 结构体 {ns:<3} 跳过 {nk:<4} {note}")
    return ok


# ------------------------------------------------------------------ 冒烟程序
SMOKE = {}

SMOKE["regex"] = (["/usr/include/regex.h"], "c", "re.fa", '''
# 用 fa bind 生成的 POSIX regex 绑定做一次真的正则匹配。
use "re.fa"

fn main() -> i64:
    let preg: re_pattern_buffer        # 位域结构体：FA 侧是同样大小的字节数组
    let rc = regcomp(&preg, "[0-9]+", REG_EXTENDED)
    if rc != 0:
        print("regcomp 失败:", rc)
        return 1
    let m: [regmatch_t; 4]
    let text = "订单号 A12345 已发货"
    let hit = regexec(&preg, text, 4, &m as *regmatch_t, 0)
    print("regexec 返回:", hit)
    if hit == 0:
        print("匹配区间:", m[0].rm_so, m[0].rm_eo)
        print("匹配到的内容:", text.slice(m[0].rm_so, m[0].rm_eo))
    regfree(&preg)
    return 0
''', "regexec 返回: 0\n匹配区间: 11 16\n匹配到的内容: 12345\n")

SMOKE["dirent"] = (["/usr/include/dirent.h"], "c", "dir.fa", '''
# 用 fa bind 生成的 dirent 绑定列一个目录（FA 自己没有目录 API）。
use "dir.fa"

fn main() -> i64:
    let d = opendir(".")
    if d == nil:
        print("opendir 失败")
        return 1
    let mut n = 0
    let mut sum = 0
    let mut chars = 0
    let mut dots = 0
    while true:
        let e = readdir(d)
        if e == nil:
            break
        n += 1
        sum += e.d_name[0] as i64
        # d_name 是 char[256]：*char 要和 *u8 一样按 C 字符串读，
        # 不然 str() 打出来是 0x800d2e3 这种地址
        let nm = str(&e.d_name[0])
        chars += nm.len()
        if nm == "." or nm == "..":
            dots += 1
    closedir(d)
    print("条目数 > 0:", n > 0)
    print("名字首字节之和 > 0:", sum > 0)
    print("名字总长 > 0:", chars > 0)
    print("点条目正好两个:", dots == 2)
    return 0
''', "条目数 > 0: true\n名字首字节之和 > 0: true\n"
     "名字总长 > 0: true\n点条目正好两个: true\n")

SMOKE["math"] = (["/usr/include/math.h"], "m", "m.fa", '''
# math.h 里的函数几乎全和 FA 内建同名（sin/cos/pow/sqrt/log/exp/floor/ceil...），
# bindgen 会给它们加 c_ 前缀并用 `= "符号"` 绑回真名；内建的那份还在。
use "m.fa"

fn main() -> i64:
    print("c_sqrt(144)   =", c_sqrt(144.0))
    print("c_pow(2,10)   =", c_pow(2.0, 10.0))
    print("c_floor(3.9)  =", c_floor(3.9))
    print("c_ceil(3.1)   =", c_ceil(3.1))
    print("fabs(-2.5)    =", fabs(-2.5))
    print("c_hypot(3,4)  =", c_hypot(3.0, 4.0))
    print("内建 sqrt(9)  =", sqrt(9.0))
    return 0
''', "c_sqrt(144)   = 12.0\nc_pow(2,10)   = 1024.0\nc_floor(3.9)  = 3.0\n"
      "c_ceil(3.1)   = 4.0\nfabs(-2.5)    = 2.5\nc_hypot(3,4)  = 5.0\n"
      "内建 sqrt(9)  = 3.0\n")

SMOKE["time"] = (["/usr/include/time.h"], "c", "t.fa", '''
# time.h：结构体（timespec / tm）是 bindgen 按真编译器量出来的布局生成的，
# C 那头往里写、FA 这头直接读字段 —— 差一个字节就读到垃圾。
use "t.fa"

fn main() -> i64:
    let ts: timespec
    let rc = clock_gettime(CLOCK_REALTIME, &ts)
    print("clock_gettime =", rc)
    print("秒数合理      =", ts.tv_sec > 1700000000)
    print("纳秒在范围内  =", ts.tv_nsec >= 0 and ts.tv_nsec < 1000000000)
    let t = time(nil)
    print("time() 一致   =", t > 1700000000)
    return 0
''', None)      # 输出随时间变，只断言退出码与关键字


def run_smoke(td: str, name: str, verbose: bool) -> bool:
    headers, lib, bindfile, prog, expect = SMOKE[name]
    for h in headers:
        out = os.path.join(td, bindfile)
        cmd = [FA, "bind", h, "-o", out]
        if lib:
            cmd += ["--lib", lib]
        if name == "math":
            cmd += ["--only", "sqrt,pow,floor,ceil,fabs,hypot,sin,cos,tan,log,exp"]
        if name == "time":
            cmd += ["--only",
                    "clock_gettime,time,timespec,clock_t,time_t,CLOCK_REALTIME"]
        r = run(cmd, timeout=600)
        if r.returncode != 0:
            print(f"✗ 冒烟 {name}：绑定失败\n{r.stdout}{r.stderr}")
            return False
    src = os.path.join(td, f"{name}.fa")
    with open(src, "w", encoding="utf-8") as f:
        f.write(prog)
    r = run([FA, "check", src], timeout=300)
    if r.returncode != 0:
        print(f"✗ 冒烟 {name}：fa check 失败\n{r.stdout}{r.stderr}")
        return False
    r = run([FA, "build", "-O", "2", "-o", os.path.join(td, name), src], timeout=600)
    if r.returncode != 0:
        print(f"✗ 冒烟 {name}：编译/链接失败\n{r.stdout}{r.stderr}")
        return False
    r = run([os.path.join(td, name)], timeout=120)
    if r.returncode != 0:
        print(f"✗ 冒烟 {name}：运行退出码 {r.returncode}\n{r.stdout}{r.stderr}")
        return False
    if expect and r.stdout != expect:
        print(f"✗ 冒烟 {name}：输出对不上\n--- 期望 ---\n{expect}--- 实际 ---\n{r.stdout}")
        return False
    if name == "time":
        for key in ("clock_gettime = 0", "秒数合理      = true", "纳秒在范围内  = true",
                    "time() 一致   = true"):
            if key not in r.stdout:
                print(f"✗ 冒烟 {name}：少了 {key!r}\n{r.stdout}")
                return False
    if verbose:
        head = r.stdout.strip().split("\n")
        print(f"  冒烟 {name:<8} 通过（{len(head)} 行输出）")
    return True


def check_multi_header(td: str, verbose: bool) -> bool:
    """一次绑多个头文件：每个头文件一个 `use c` 块，各自带自己的 lib。

    合成一个块看着省事，可链接就对不上了 —— math.h 的函数要 -lm，
    time.h 的不用；混在一个块里只能整个块挂一个 lib，要么漏链要么多链。
    """
    hs = [h for h in ("/usr/include/dirent.h", "/usr/include/time.h",
                      "/usr/include/math.h") if have(h)]
    if len(hs) < 2:
        print("  跳过多头文件检查：可用的头文件不足两个")
        return True
    out = os.path.join(td, "multi.fa")
    cmd = [FA, "bind"] + hs + ["--only", "opendir,time,c_sqrt", "-o", out]
    r = run(cmd, timeout=600)
    if r.returncode != 0:
        print(f"✗ 多头文件：fa bind 失败\n{r.stdout}{r.stderr}")
        return False
    text = open(out, encoding="utf-8").read()
    ok = True
    blocks = [l for l in text.split("\n") if l.startswith("use c ")]
    if len(blocks) != len(hs):
        print(f"✗ 多头文件：{len(hs)} 个头文件该有 {len(hs)} 个 use c 块，实际 {len(blocks)} 个")
        for b in blocks:
            print("   ", b)
        ok = False
    # math.h 那块得挂 -lm，dirent/time 挂 -lc
    if any(h.endswith("math.h") for h in hs):
        if 'use c "math.h" lib "m":' not in text:
            print("✗ 多头文件：math.h 的块没有自己的 lib \"m\"")
            ok = False
    # 每个块底下只该有它自己那个头文件的函数
    for hdr, fns in (("dirent.h", ["opendir"]), ("time.h", ["time("]),
                     ("math.h", ["c_sqrt"])):
        if not any(h.endswith(hdr) for h in hs):
            continue
        i = text.find(f'use c "{hdr}"')
        j = text.find("use c ", i + 1)
        seg = text[i:] if j < 0 else text[i:j]
        for f in fns:
            if f not in seg:
                print(f"✗ 多头文件：{f} 不在 {hdr} 的块里\n{seg}")
                ok = False
    r2 = run([FA, "check", out], timeout=300)
    if r2.returncode != 0:
        print(f"✗ 多头文件：生成物过不了 fa check\n{r2.stdout}{r2.stderr}")
        ok = False
    if verbose and ok:
        print(f"  ✓ 多头文件：{len(blocks)} 个 use c 块，各带各的 lib")
    return ok


def check_bare_name(td: str, verbose: bool) -> bool:
    """`fa bind sys/stat.h`：给的是**头文件名**而不是路径。

    Debian/Ubuntu 的多架构布局里 sys/stat.h 真身在
    /usr/include/x86_64-linux-gnu/sys/stat.h，按路径找是找不到的。
    用 cc -M 问编译器它到底在哪，才和 C 那边的行为一致。
    """
    out = os.path.join(td, "bare.fa")
    r = run([FA, "bind", "sys/stat.h", "--only", "stat,mkdir,S_IFDIR", "-o", out],
            timeout=600)
    if r.returncode != 0:
        if "找不到头文件" in (r.stdout + r.stderr):
            print("  跳过裸头文件名检查：这台机器上 cc 也找不到 sys/stat.h")
            return True
        print(f"✗ 裸头文件名：fa bind 失败\n{r.stdout}{r.stderr}")
        return False
    text = open(out, encoding="utf-8").read()
    ok = True
    for a in ("fn stat(", "fn mkdir(", "const S_IFDIR:", "struct stat:"):
        if a not in text:
            print(f"✗ 裸头文件名：没绑出 {a!r}")
            ok = False
    # --only 收窄之后，不该把整个头文件的结构体都拖进来
    for a in ("struct statx", "struct file_handle", "fn chmod"):
        if a in text:
            print(f"✗ 裸头文件名：--only 收窄之后不该出现 {a!r}")
            ok = False
    if verbose and ok:
        print("  ✓ 裸头文件名：sys/stat.h 解析到了真身，--only 也确实收窄了")
    return ok


def check_only_struct_ptr(td: str, verbose: bool) -> bool:
    """--only 点名一个返回 `struct X *` 的函数，得真能绑出来。

    `extern struct dirent *readdir (DIR *__dirp);` 这条声明，取名字的函数以前
    看见开头是 struct 就当成结构体定义，名字取成了返回类型里的 tag（dirent）：
    --only readdir 一个都匹配不上，readdir 被悄悄丢掉，连带 struct dirent 也
    不生成（没人引用它了）。C 里返回 struct X* 的函数遍地都是
    （readdir / localtime / getpwnam / getcwd ...），这条不修就绑不动真实库。
    """
    if not have("/usr/include/dirent.h"):
        return True
    out = os.path.join(td, "only_ptr.fa")
    r = run([FA, "bind", "dirent.h", "--only", "opendir,readdir,closedir",
             "-o", out], timeout=600)
    if r.returncode != 0:
        print(f"✗ --only 返回结构体指针的函数：fa bind 失败\n{r.stdout}{r.stderr}")
        return False
    text = open(out, encoding="utf-8").read()
    ok = True
    for a in ("fn readdir(dirp: *void) -> *dirent", "struct dirent:",
              "    d_name: [char; 256]", "fn opendir(", "fn closedir("):
        if a not in text:
            print(f"✗ --only 返回结构体指针的函数：没绑出 {a!r}")
            ok = False
    for a in ("fa_bind_nothing_found", "readdir：--only 点名要它"):
        if a in text:
            print(f"✗ --only 返回结构体指针的函数：不该出现 {a!r}")
            ok = False
    if verbose and ok:
        print("  ✓ --only 认得返回 struct X* 的函数（readdir 连 struct dirent 一起出来了）")
    return ok


def check_nested_struct(td: str, verbose: bool) -> bool:
    """结构体字段引用的**嵌套**结构体也要量到布局，否则外层整个退化。

    struct stat 的 st_atim / st_mtim / st_ctim 是 struct timespec，而 timespec
    的定义在 bits/types/struct_timespec.h —— 那不是目标文件，探针不量它，
    于是「字段映射不了」连累整个 struct stat 退化成 raw: [u8; 144]，
    st_size、st_mode 一个都读不出来。stat/lstat/fstat 是最常用的系统调用之一。
    """
    if not have("/usr/include/x86_64-linux-gnu/sys/stat.h") \
            and not shutil.which("cc"):
        return True
    out = os.path.join(td, "nested.fa")
    r = run([FA, "bind", "sys/stat.h", "--only", "stat", "-o", out], timeout=600)
    if r.returncode != 0:
        if "找不到头文件" in (r.stdout + r.stderr):
            print("  跳过嵌套结构体检查：这台机器上 cc 找不到 sys/stat.h")
            return True
        print(f"✗ 嵌套结构体：fa bind 失败\n{r.stdout}{r.stderr}")
        return False
    text = open(out, encoding="utf-8").read()
    ok = True
    for a in ("    st_size: i64", "    st_mode: u32", "    st_atim: timespec",
              "struct timespec:", "    tv_sec: i64"):
        if a not in text:
            print(f"✗ 嵌套结构体：没绑出 {a!r}")
            ok = False
    if "raw: [u8; 144]" in text:
        print("✗ 嵌套结构体：struct stat 又退化成不透明字节数组了（timespec 没量到）")
        ok = False
    if verbose and ok:
        print("  ✓ 嵌套结构体：struct stat 出真字段（st_size / st_atim: timespec）")
    return ok


def check_only_misses_reported(td: str, verbose: bool) -> bool:
    """--only 点名要、却什么都没生成的名字，必须在「跳过」清单里说出来。

    静默少东西最难查：用户以为头文件里没有，其实是拼错了、或者那是个宏、
    或者类型映射不了。生成文件末尾的跳过清单本来就是干这个的。
    """
    if not have("/usr/include/string.h"):
        return True
    out = os.path.join(td, "misses.fa")
    r = run([FA, "bind", "string.h", "--only", "strlen,fa_bindgen_没有这个函数",
             "-o", out], timeout=600)
    if r.returncode != 0:
        print(f"✗ --only 落空要报告：fa bind 失败\n{r.stdout}{r.stderr}")
        return False
    text = open(out, encoding="utf-8").read()
    ok = True
    if "fn strlen(" not in text:
        print("✗ --only 落空要报告：strlen 没绑出来")
        ok = False
    tail = text.split("跳过的")[-1]
    if "fa_bindgen_没有这个函数" not in tail:
        print("✗ --only 落空要报告：落空的名字没写进跳过清单")
        ok = False
    if verbose and ok:
        print("  ✓ --only 落空的名字写进了跳过清单（不静默少东西）")
    return ok


def main(argv) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    if shutil.which("cc") is None:
        print("跳过：没有 C 编译器（fa bind 要靠它预处理头文件）")
        return 0
    plan = [
        ("/usr/include/math.h", "m",
         ['fn c_sqrt(x: f64) -> f64 = "sqrt"', 'fn c_sin(x: f64) -> f64 = "sin"',
          "fn c_hypot(", "!fn sqrt(x: f64)"],
         "内建同名的都加了 c_ 前缀并绑回真符号"),
        ("/usr/include/string.h", "c",
         ["fn strlen(", "fn strcmp(", "fn memcpy("],
         ""),
        ("/usr/include/regex.h", "c",
         ["fn regcomp(preg: *re_pattern_buffer", "struct regmatch_t:",
          "    rm_so: i32", "const REG_EXTENDED: i64 = 1",
          "struct re_pattern_buffer:", "    raw: [u8; ",
          # 数组参数退化成**带类型**的指针：`regmatch_t pmatch[]` -> *regmatch_t。
          # 以前元素先按参数位置映射（结构体被包了一层指针），再撞上「已经是指针
          # 的不能再包一层」的保护，pmatch 就成了 *void —— 结构体明明就在同一个
          # 文件里生成出来了。
          "pmatch: *regmatch_t",
          # `typedef struct re_pattern_buffer regex_t;` 这种 typedef 别名要能解开，
          # regerror 的 preg 才是 *re_pattern_buffer 而不是 *void
          "fn regerror(errcode: i32, preg: *re_pattern_buffer"],
         "位域结构体退化成同样大小的字节数组，sizeof 是真编译器量的"),
        ("/usr/include/dirent.h", "c",
         ["fn opendir(", "fn readdir(", "struct dirent"],
         ""),
        ("/usr/include/time.h", "c",
         ["fn clock_gettime(", "struct timespec:", "    tv_sec: i64"],
         ""),
        ("/usr/include/stdlib.h", "c",
         ['fn c_free(', "fn malloc(", "fn qsort("],
         "qsort 的比较函数参数是一等函数类型"),
    ]
    plan = [(h, l, a, n) for (h, l, a, n) in plan if have(h)]
    if not plan:
        print("跳过：一个系统头文件都找不到")
        return 0

    fails = 0
    extra_total = 0
    with tempfile.TemporaryDirectory(prefix="fa_bindgen_") as td:
        if verbose:
            print("— 生成并核对 —")
        for h, lib, asserts, note in plan:
            if not check_header(td, h, lib, asserts, note, verbose):
                fails += 1
        if verbose:
            print("— 选项与路径解析 —")
        if have("/usr/include/time.h"):
            extra_total += 1
            if not check_header(
                    td, "/usr/include/time.h", "c",
                    ["fn strptime(s: str, fmt: str, tp: *tm) -> *u8",
                     "!fn strptime(s: str, fmt: str, tp: *tm) -> str"],
                    "--ptr-return：char* 返回值保留 *u8，NULL 才算失败",
                    verbose, extra=["--ptr-return", "strptime",
                                    "--only", "strptime,tm"],
                    tag="ptr_return"):
                fails += 1
        extra_total += 1
        if not check_multi_header(td, verbose):
            fails += 1
        extra_total += 1
        if not check_bare_name(td, verbose):
            fails += 1
        extra_total += 1
        if not check_only_struct_ptr(td, verbose):
            fails += 1
        extra_total += 1
        if not check_nested_struct(td, verbose):
            fails += 1
        extra_total += 1
        if not check_only_misses_reported(td, verbose):
            fails += 1

        if verbose:
            print("— 冒烟程序（编译 + 链接 + 运行）—")
        for name in ("regex", "dirent", "math", "time"):
            hs = SMOKE[name][0]
            if not all(have(x) for x in hs):
                print(f"  跳过冒烟 {name}：缺头文件 {hs}")
                continue
            if not run_smoke(td, name, verbose):
                fails += 1

    total = len(plan) + extra_total + sum(
        1 for n in ("regex", "dirent", "math", "time")
        if all(have(x) for x in SMOKE[n][0]))
    if fails:
        print(f"fa bind：{total - fails} / {total} 通过，{fails} 个失败")
        return 1
    print(f"fa bind：{total} / {total} 通过（{len(plan)} 个头文件 + "
          f"{extra_total} 项选项/路径检查 + 冒烟程序）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
