#!/usr/bin/env python3
"""FA / C / Python 性能基准

用法:
    python3 bench/run_bench.py                # 全部三个基准，各跑 5 次取最小值
    python3 bench/run_bench.py --repeat 3     # 每个基准跑 3 次
    python3 bench/run_bench.py fib            # 只跑名字里含 fib 的基准
    python3 bench/run_bench.py --keep         # 保留构建产物（默认跑完就删）
    python3 bench/run_bench.py --java         # 额外测 JVM（需要 javac/java）

要点：
  * **每次都从源码重新构建**。以前这个脚本只运行 bench/ 下已经存在的
    fa_fib / c_fib，全新克隆里根本没有这些文件 -> 直接 FileNotFoundError；
    更糟的是如果本地残留了旧二进制，测出来的就是「某个历史版本」的成绩。
  * FA 与 C 都用 -O2，产物放在临时目录，互不污染。
  * 结果校验：FA 与 C 的输出必须逐字节相同，否则成绩没有意义。
  * 计时用「多次取最小值」，减少调度噪声；同时打印机器信息，
    这样别人复现时能判断差距是不是来自硬件。
"""

from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FA = os.path.join(ROOT, "bin", "fa")

# 名字 -> (FA 源码, C 源码, Python 源码, 期望输出)
BENCHES = [
    ("fib(38) 递归", "fib.fa", "fib.c", "fib.py"),
    ("1亿次算术循环", "loop.fa", "loop.c", "loop.py"),
    ("200万内素数筛", "primes.fa", "primes.c", "primes.py"),
]


# ------------------------------------------------------------------ 构建
def build_fa(src: str, out: str, opt: int) -> str:
    """返回错误信息（成功为 ""）"""
    cmd = [FA] if os.access(FA, os.X_OK) else ["bash", FA]
    r = subprocess.run(cmd + ["build", src, "-o", out, "-O", str(opt)],
                       capture_output=True, text=True, cwd=HERE)
    if r.returncode != 0:
        return f"FA 构建失败：\n{(r.stdout + r.stderr).strip()[:1500]}"
    if not os.path.exists(out):
        return f"FA 构建没有产出 {out}"
    return ""


def build_c(src: str, out: str, opt: int, extra=()) -> str:
    cc = os.environ.get("CC", "gcc")
    if shutil.which(cc) is None:
        return f"未找到 {cc}"
    r = subprocess.run([cc, f"-O{opt}", *extra, src, "-o", out, "-lm"],
                       capture_output=True, text=True, cwd=HERE)
    if r.returncode != 0:
        return f"C 构建失败：\n{r.stderr.strip()[:1500]}"
    return ""


# 「同算法」对照组：gcc -O2 会把 fib 这种**小递归函数**内联到自己里面再做
# 公共子表达式消除，于是 fib(n-3)/fib(n-4) 只算一次 —— 递归调用次数直接少一个量级
# （实测 fib(38)：72 ms -> 5 ms）。它比的已经不是「同样的活谁干得快」了。
# 加上 -fno-inline 就能得到和 FA 做同样多工作的对照（对循环/筛法几乎无影响）。
FAIR_C_FLAGS = ("-fno-inline",)


def build_java(src: str, out_dir: str) -> str:
    if shutil.which("javac") is None or shutil.which("java") is None:
        return "未找到 JDK（javac / java）"
    r = subprocess.run(["javac", "-d", out_dir, os.path.join(HERE, src)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return f"Java 构建失败：\n{r.stderr.strip()[:800]}"
    return ""


# ------------------------------------------------------------------ 计时
def timeit(cmd, repeat: int, timeout: int):
    """返回 (最佳秒数, stdout, 错误信息)"""
    best, out = None, ""
    for _ in range(repeat):
        t0 = time.perf_counter()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, cwd=HERE)
        except subprocess.TimeoutExpired:
            return None, "", f"超时（>{timeout}s）"
        dt = time.perf_counter() - t0
        if p.returncode != 0:
            return None, "", f"运行失败 rc={p.returncode} {p.stderr.strip()[:200]}"
        out = p.stdout.strip()
        best = dt if best is None else min(best, dt)
    return best, out, ""


def machine_info() -> str:
    model, cores = "", os.cpu_count() or 0
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return f"{model or '未知 CPU'}，{cores} 核"


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__)
    ap.add_argument("filters", nargs="*", help="只跑名字里含这些子串的基准")
    ap.add_argument("--repeat", type=int, default=5, help="每个基准重复次数（取最小值）")
    ap.add_argument("--opt", type=int, default=2, help="FA 与 C 的优化级别（默认 2）")
    ap.add_argument("--timeout", type=int, default=300, help="单次运行超时秒数")
    ap.add_argument("--java", action="store_true", help="额外测 JVM 版本")
    ap.add_argument("--keep", action="store_true", help="保留构建产物")
    ap.add_argument("--no-python", action="store_true", help="跳过 CPython（它很慢）")
    a = ap.parse_args()

    sel = [b for b in BENCHES
           if not a.filters or any(k in b[0] or k in b[1] for k in a.filters)]
    if not sel:
        print(f"没有匹配的基准（可选：{[b[0] for b in BENCHES]}）")
        return 2
    if not os.path.exists(FA):
        print(f"找不到编译器入口 {FA}")
        return 2

    tmp = tempfile.mkdtemp(prefix="fa_bench_")
    print(f"FA 性能基准 —— 每次都从源码重新构建（gcc -O{a.opt} / fa -O{a.opt} / CPython）")
    print(f"机器：{machine_info()}    重复 {a.repeat} 次取最小值")
    print("-" * 74)

    rows, failed = [], []
    try:
        for name, fasrc, csrc, pysrc in sel:
            stem = os.path.splitext(fasrc)[0]
            fa_exe = os.path.join(tmp, f"fa_{stem}")
            c_exe = os.path.join(tmp, f"c_{stem}")
            err = build_fa(os.path.join(HERE, fasrc), fa_exe, a.opt)
            if err:
                failed.append((name, err)); print(f"{name}: {err}"); continue
            err = build_c(os.path.join(HERE, csrc), c_exe, a.opt)
            c_ok = not err
            if err:
                print(f"{name}: {err}（只测 FA）")
            fair_exe = os.path.join(tmp, f"cf_{stem}")
            fair_ok = c_ok and not build_c(os.path.join(HERE, csrc), fair_exe,
                                           a.opt, FAIR_C_FLAGS)

            tfa, ofa, efa = timeit([fa_exe], a.repeat, a.timeout)
            if efa:
                failed.append((name, f"FA 运行：{efa}")); print(f"{name}: FA {efa}"); continue
            tc, oc, ec = (timeit([c_exe], a.repeat, a.timeout) if c_ok
                          else (None, "", "未构建"))
            tf, of, ef = (timeit([fair_exe], a.repeat, a.timeout) if fair_ok
                          else (None, "", "未构建"))
            tp, op, ep = ((None, "", "已跳过") if a.no_python
                          else timeit([sys.executable, os.path.join(HERE, pysrc)],
                                      min(a.repeat, 3), a.timeout))
            tj, oj, ej = (None, "", "已跳过")
            if a.java:
                jdir = os.path.join(tmp, f"j_{stem}")
                os.makedirs(jdir, exist_ok=True)
                jsrc = os.path.splitext(fasrc)[0] + ".java"
                jerr = build_java(jsrc, jdir) if os.path.exists(
                    os.path.join(HERE, jsrc)) else f"缺少 {jsrc}"
                if jerr:
                    tj, ej = None, jerr
                else:
                    cls = os.path.splitext(jsrc)[0]
                    tj, oj, ej = timeit(["java", "-cp", jdir, cls],
                                        min(a.repeat, 3), a.timeout)

            same = "✓" if (not c_ok or ofa == oc) else f"✗ 结果不一致 (FA={ofa!r} C={oc!r})"
            if not c_ok or ofa == oc:
                pass
            else:
                failed.append((name, same))

            def ms(t):
                return "      — " if t is None else f"{t * 1000:8.1f}"
            print(f"{name}:")
            print(f"    FA        {ms(tfa)} ms")
            print(f"    C -O{a.opt}    {ms(tc)} ms")
            if a.java:
                print(f"    Java      {ms(tj)} ms" + (f"   ({ej})" if ej not in ("", "已跳过") else ""))
            print(f"    Python    {ms(tp)} ms" + (f"   ({ep})" if ep not in ("", "已跳过") else ""))
            if tfa and tc:
                line = f"    FA 比 C 慢 {tfa / tc:.2f} 倍"
                if tf:
                    line += f"（比「同算法」的 C -fno-inline 慢 {tfa / tf:.2f} 倍）"
                if tp:
                    line += f"，比 CPython 快 {tp / tfa:.1f} 倍"
                print(line)
            if tj and tfa:
                print(f"    FA 比 JVM 快 {tj / tfa:.2f} 倍")
            print(f"    结果校验 {same}（输出 {ofa!r}）")
            rows.append((name, tfa, tc, tf, tp, tj,
                         tfa / tc if (tfa and tc) else None,
                         tfa / tf if (tfa and tf) else None))
    finally:
        if a.keep:
            print(f"\n构建产物保留在 {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 74)
    def ms(t):
        return "—" if t is None else f"{t * 1000:.1f} ms"

    print(f"{'基准':<15}{'FA':>10}{'C -O2':>10}{'C 同算法':>11}"
          f"{'Python':>11}{'FA/C':>8}{'FA/C同':>8}")
    for name, tfa, tc, tf, tp, tj, ratio, ratio_f in rows:
        line = (f"{name:<15}{ms(tfa):>10}{ms(tc):>10}{ms(tf):>11}{ms(tp):>11}"
                + (f"{ratio:>7.2f}x" if ratio else f"{'—':>8}")
                + (f"{ratio_f:>7.2f}x" if ratio_f else f"{'—':>8}"))
        if a.java:
            line += f"   Java {ms(tj)}"
        print(line)
    print("（C 同算法 = gcc -O2 -fno-inline：不给 gcc 把递归函数内联进自己、"
          "再消掉公共子表达式的机会）")
    if failed:
        print(f"\n{len(failed)} 个基准有问题：")
        for n, why in failed:
            print(f"  - {n}: {str(why).splitlines()[0]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
