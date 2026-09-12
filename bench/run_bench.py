#!/usr/bin/env python3
"""FA / C / Python 性能对比"""
import subprocess, time, os, sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

def timeit(cmd, repeat=5):   # 5 次取最小值，减少机器噪声
    best = None
    out = ""
    for _ in range(repeat):
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, shell=isinstance(cmd, str))
        t1 = time.time()
        if p.returncode != 0:
            return None, f"失败({p.returncode}) {p.stderr[:120]}"
        out = p.stdout.strip()
        best = (t1 - t0) if best is None else min(best, t1 - t0)
    return best, out

BENCH = {
    "fib(38) 递归":   (["./fa_fib"], ["./c_fib"], ["python3", "fib.py"]),
    "1亿次算术循环":   (["./fa_loop"], ["./c_loop"], ["python3", "loop.py"]),
    "200万内素数筛":   (["./fa_primes"], ["./c_primes"], ["python3", "primes.py"]),
}

rows = []
for name, (fa, c, py) in BENCH.items():
    tfa, ofa = timeit(fa)
    tc,  oc  = timeit(c)
    tpy, opy = timeit(py)
    ok = "✓" if ofa == oc else f"✗ 结果不一致 (FA={ofa!r} C={oc!r})"
    def fmt(t, base):
        if t is None: return "—"
        return f"{t*1000:8.1f} ms"
    rows.append((name, tfa, tc, tpy, ok, ofa))
    print(f"{name}:")
    print(f"    FA      {fmt(tfa,tc)}")
    print(f"    C -O2   {fmt(tc,tc)}")
    print(f"    Python  {fmt(tpy,tc)}   (Py 可能超时则记为 —)")
    if tfa and tc:
        print(f"    FA 是 C 的 {tfa/tc:.2f} 倍，" +
              (f"是 Python 的 {tpy/tfa:.0f} 倍快" if tpy else ""))
    print(f"    结果校验 {ok} (期望 {oc}, FA {ofa})")
