#!/usr/bin/env python3
"""FA 内存安全检查器：把用例全部放到 AddressSanitizer 底下跑一遍

用法:
    python3 tests/run_asan.py                 # 扫 tests/cases/*.fa
    python3 tests/run_asan.py --no-leaks      # 只查越界/悬垂，不查泄漏
    python3 tests/run_asan.py 067 069         # 只扫名字里含这些子串的用例
    python3 tests/run_asan.py --examples      # 顺带扫 examples/*.fa
    python3 tests/run_asan.py --keep          # 留下 ASan 版可执行文件（自己再跑）

为什么需要它：run_tests.py 只比对**输出**，而引用计数的 bug 大多不改变输出 ——
多减一次计数、少加一次计数、堆上拷贝不给字段加引用，都要等到内存被复用或者
glibc 巡查到堆元数据时才炸，小字符串往往一声不吭。ASan 在错误发生的那一刻
就报，还告诉你是哪次分配、哪次释放。

它抓到过的问题（都已修，留在这里当动机说明）：
  * 字符串比较把「借来的」操作数登记成本语句拥有的引用 -> 多减一次 rc
    -> heap-use-after-free（2000 字的串直接 corrupted size vs. prev_size）
  * fa_print_bool 分配 "true"/"false" 却从不释放 -> 每打印一个布尔值漏 20 字节
  * 内建 chr() 的结果没有 mark_owned -> 每调一次漏一个 FaStr
  * `new P { name: "x" + "y" }` 只 memcpy 结构体字节、不给字段 rc_inc
    -> 临时字符串语句末尾就没了，堆上那份拷贝的字段悬垂

做法：
  1. 用 -fsanitize=address 重编一份运行时（fa_runtime.c / fa_syscall.S /
     两个 stub），缓存到 build/asan/，源文件没变就不重编。
  2. 每个用例走正常前端的 `fa asm` 出汇编（编译器本身不插桩，但堆都是
     ASan 接管的 malloc，红区和毒化照样生效）。
  3. 链接、运行，解析 ASan/LSan 的报告。

跳过（不算失败）：
  * 声明了 `# expect-compile-error` 的用例 —— 它们本来就该编译失败
  * `use py` / `use java` / `use cxx` —— 需要 libpython / libjvm / g++ shim，
    和 ASan 版运行时混链没有意义
  * 链接不上（缺 C/C++ 夹具库）—— 环境问题，先跑一次 run_tests.py 生成夹具

用例可以声明「允许的泄漏」，用来锁住那些**故意**不释放的场景：
    # expect-asan-leak: 16     允许漏 16 字节（多了少了都算失败）
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "runtime")
BUILD = os.path.join(ROOT, "build", "asan")
FA = os.path.join(ROOT, "bin", "fa")

CC = os.environ.get("CC", "gcc")
ASAN_CFLAGS = ["-O1", "-g", "-fsanitize=address", "-fno-strict-aliasing"]
LINK_LIBS = ["-lm", "-ldl", "-lpthread"]

SHIM = r"""/* FA 自动生成：ASan 版引导（等价于 driver.gen_main_shim 的无参分支） */
#include <stdint.h>
extern int64_t fa_main(void);
extern void fa_set_args(int64_t argc, char** argv);
int main(int argc, char** argv) {
    (void)argc; (void)argv;
    fa_set_args((int64_t)argc, argv);
    return (int)fa_main();
}
"""

SHIM_ARGS = r"""/* FA 自动生成：ASan 版引导（main 带 argc/argv 的版本） */
#include <stdint.h>
extern int64_t fa_main(int64_t argc, char** argv);
extern void fa_set_args(int64_t argc, char** argv);
int main(int argc, char** argv) {
    fa_set_args((int64_t)argc, argv);
    return (int)fa_main((int64_t)argc, argv);
}
"""

SKIP_USE = re.compile(r"^\s*use\s+(py|java|jvm|cxx|cpp)\b", re.M)
EXPECT_COMPILE_ERROR = re.compile(r"#\s*expect-compile-error")
# 用例自己声明「这里允许漏 N 字节」，用来测那些**故意**不释放的场景
# （例如把 C 的 strdup 声明成 -> str：FA 拷一份，C 那块就没人管了，
# docs/04 里明确写了这一点）。字节数对不上仍然算失败。
EXPECT_ASAN_LEAK = re.compile(r"#\s*expect-asan-leak:\s*(\d+)")
LEAKED_BYTES = re.compile(r"(\d+) byte\(s\) leaked")
MAIN_WITH_ARGS = re.compile(r"^fn\s+main\s*\(\s*\w", re.M)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def compile_obj(src, out, extra=()):
    """按 mtime 缓存地编一个目标文件；返回 (路径, 错误信息)"""
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out, None
    cmd = [CC] + ASAN_CFLAGS + [f"-I{RUNTIME}", f"-I{ROOT}"] + list(extra) + \
          ["-c", src, "-o", out]
    r = sh(cmd)
    if r.returncode != 0:
        return None, f"运行时编译失败 ({os.path.basename(src)}):\n{r.stderr.strip()}"
    return out, None


def build_runtime():
    """ASan 版运行时 + 引导 shim。返回 (对象列表, 致命错误)"""
    os.makedirs(BUILD, exist_ok=True)
    objs, err = [], None
    for name, src, extra in (
        ("fa_runtime", os.path.join(RUNTIME, "fa_runtime.c"), ["-std=gnu11"]),
        ("fa_syscall", os.path.join(RUNTIME, "fa_syscall.S"), []),
        # 交互库用 stub：ASan 扫的是 FA 自己的内存管理，不扫 CPython/JVM
        ("fa_python_stub", os.path.join(RUNTIME, "fa_python.c"), ["-std=gnu11"]),
        ("fa_jvm_stub", os.path.join(RUNTIME, "fa_jvm.c"), ["-std=gnu11"]),
    ):
        o, err = compile_obj(src, os.path.join(BUILD, name + ".o"), extra)
        if err:
            return None, err
        objs.append(o)
    shim_c = os.path.join(BUILD, "_fa_asan_shim.c")
    if not os.path.exists(shim_c) or open(shim_c).read() != SHIM:
        with open(shim_c, "w") as f:
            f.write(SHIM)
    shim_o = os.path.join(BUILD, "_fa_asan_shim.o")
    o, err = compile_obj(shim_c, shim_o)
    if err:
        return None, err
    objs.append(o)

    shim2_c = os.path.join(BUILD, "_fa_asan_shim_args.c")
    if not os.path.exists(shim2_c) or open(shim2_c).read() != SHIM_ARGS:
        with open(shim2_c, "w") as f:
            f.write(SHIM_ARGS)
    shim2_o = os.path.join(BUILD, "_fa_asan_shim_args.o")
    o, err = compile_obj(shim2_c, shim2_o)
    if err:
        return None, err
    return objs, None


REPORT = re.compile(r"ERROR: (Address|Leak)Sanitizer: ([^\n]*)")


def classify(out):
    """从 ASan/LSan 输出里提炼一行结论；干净时返回 None"""
    m = REPORT.search(out)
    if not m:
        return None
    kind, what = m.group(1), m.group(2).strip()
    head = f"{'内存错误' if kind == 'Address' else '泄漏'}: {what}"
    frames = [l.strip() for l in out.splitlines()
              if re.match(r"\s+#\d+ 0x", l)][:3]
    where = []
    for f in frames:
        m = re.match(r"#\d+\s+(?:0x\S+ in\s+)?(\S+)", f)
        if m and m.group(1) not in where:
            where.append(m.group(1))
    summary = re.search(r"SUMMARY: \S+ (.*)", out)
    return head + ("（" + " <- ".join(where) + "）" if where else "") + \
        ("\n        " + summary.group(1) if summary else "")


def scan(fa_path, rt_objs, leaks=True, keep=False):
    """扫一个 .fa。返回 ("ok"|"bad"|"skip", 说明)"""
    base = os.path.splitext(os.path.basename(fa_path))[0]
    src = open(fa_path, encoding="utf-8").read()
    if EXPECT_COMPILE_ERROR.search(src):
        return "skip", "预期编译失败"
    if SKIP_USE.search(src):
        return "skip", "需要 py/java/cxx 交互库"
    work = BUILD
    s_path = os.path.join(work, base + ".s")
    o_path = os.path.join(work, base + ".o")
    exe = os.path.join(work, base + ".asan")

    r = sh([FA, "asm", fa_path, "-o", s_path])
    if r.returncode != 0:
        return "bad", "前端失败: " + (r.stdout + r.stderr).strip().splitlines()[0]
    r = sh([CC, "-c", s_path, "-o", o_path])
    if r.returncode != 0:
        return "bad", "汇编失败: " + r.stderr.strip().splitlines()[0]

    shim = os.path.join(work, "_fa_asan_shim_args.o" if MAIN_WITH_ARGS.search(src)
                        else "_fa_asan_shim.o")
    objs = [o for o in rt_objs if o != os.path.join(work, "_fa_asan_shim.o")]
    r = sh([CC, "-g", "-fsanitize=address", "-no-pie", o_path, shim] + objs +
           ["-o", exe] + LINK_LIBS, cwd=os.path.dirname(fa_path))
    if r.returncode != 0:
        first = r.stderr.strip().splitlines()
        hint = first[0] if first else "未知"
        if "cannot find" in r.stderr or "No such file" in r.stderr \
                or "DSO missing" in r.stderr:
            return "skip", "缺夹具库（先跑 run_tests.py）: " + hint
        return "bad", "链接失败: " + hint

    env = dict(os.environ,
               ASAN_OPTIONS=("detect_leaks=1" if leaks else "detect_leaks=0")
               + ":abort_on_error=0")
    r = sh([exe], env=env, cwd=os.path.dirname(fa_path), timeout=180)
    out = r.stdout + r.stderr
    verdict = classify(out)
    note = ""
    allow = EXPECT_ASAN_LEAK.search(src)
    if verdict and allow:
        got = LEAKED_BYTES.search(out)
        if got and int(got.group(1)) == int(allow.group(1)):
            verdict = None          # 用例声明过的泄漏，字节数也对得上
            note = f"允许范围内泄漏 {got.group(1)} 字节"
        elif got:
            verdict = (f"声明允许漏 {allow.group(1)} 字节，实际漏 {got.group(1)} 字节"
                       "（" + verdict + "）")
    if not keep:
        for p in (s_path, o_path, exe):
            if os.path.exists(p):
                os.unlink(p)
    if verdict:
        return "bad", verdict
    return "ok", note


def main():
    ap = argparse.ArgumentParser(add_help=True, description=__doc__.splitlines()[0])
    ap.add_argument("filters", nargs="*", help="只扫名字含这些子串的用例")
    ap.add_argument("--no-leaks", action="store_true", help="关掉泄漏检测")
    ap.add_argument("--examples", action="store_true", help="顺带扫 examples/*.fa")
    ap.add_argument("--keep", action="store_true", help="保留中间产物与可执行文件")
    a = ap.parse_args()

    files = sorted(os.path.join(ROOT, "tests", "cases", f)
                   for f in os.listdir(os.path.join(ROOT, "tests", "cases"))
                   if f.endswith(".fa"))
    if a.examples:
        ex = os.path.join(ROOT, "examples")
        files += sorted(os.path.join(ex, f) for f in os.listdir(ex)
                        if f.endswith(".fa"))
    if a.filters:
        files = [f for f in files
                 if any(k in os.path.basename(f) for k in a.filters)]
    if not files:
        print("没有匹配的用例")
        return 1

    rt_objs, err = build_runtime()
    if err:
        print(err)
        print("（需要 gcc 支持 -fsanitize=address；Debian/Ubuntu 上装 gcc 即可）")
        return 2

    ok = skipped = 0
    bad = []
    for f in files:
        name = os.path.basename(f)
        try:
            verdict, why = scan(f, rt_objs, leaks=not a.no_leaks, keep=a.keep)
        except subprocess.TimeoutExpired:
            verdict, why = "bad", "超时（180 秒）"
        if verdict == "ok":
            ok += 1
            print(f"  \033[32m✓\033[0m {name}" + (f"  {why}" if why else ""))
        elif verdict == "skip":
            skipped += 1
            print(f"  \033[33m-\033[0m {name}  跳过（{why}）")
        else:
            bad.append((name, why))
            print(f"  \033[31m✗\033[0m {name}  {why}")

    print("-" * 64)
    print(f"ASan 干净 {ok} / {len(files)}，跳过 {skipped}"
          + (f"，有问题 {len(bad)}" if bad else "  —— 无内存问题"))
    if bad:
        print("\n提示：报告里的 #0/#1 是运行时的帧；FA 生成的代码没有调试信息，")
        print("      所以要靠用例内容缩小范围（把 main 里的语句逐段删掉再扫）。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
