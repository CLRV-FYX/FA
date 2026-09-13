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
                 verbose: bool) -> bool:
    out = os.path.join(td, os.path.basename(header).replace(".", "_") + ".fa")
    cmd = [FA, "bind", header, "-o", out]
    if lib:
        cmd += ["--lib", lib]
    r = run(cmd, timeout=600)
    if r.returncode != 0:
        print(f"✗ {header}：fa bind 失败\n{r.stdout}{r.stderr}")
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
    while true:
        let e = readdir(d)
        if e == nil:
            break
        n += 1
        sum += e.d_name[0] as i64
    closedir(d)
    print("条目数 > 0:", n > 0)
    print("名字首字节之和 > 0:", sum > 0)
    return 0
''', "条目数 > 0: true\n名字首字节之和 > 0: true\n")

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
          "struct re_pattern_buffer:", "    raw: [u8; "],
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
    with tempfile.TemporaryDirectory(prefix="fa_bindgen_") as td:
        if verbose:
            print("— 生成并核对 —")
        for h, lib, asserts, note in plan:
            if not check_header(td, h, lib, asserts, note, verbose):
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

    total = len(plan) + sum(1 for n in ("regex", "dirent", "math", "time")
                            if all(have(x) for x in SMOKE[n][0]))
    if fails:
        print(f"fa bind：{total - fails} / {total} 通过，{fails} 个失败")
        return 1
    print(f"fa bind：{total} / {total} 通过（{len(plan)} 个头文件 + 冒烟程序）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
