#!/usr/bin/env python3
"""FA 回归测试运行器

用法:
    python3 tests/run_tests.py                # 运行全部用例并比对 .expected
    python3 tests/run_tests.py --record       # 把当前实际输出记录为期望输出（改动需人工复核）
    python3 tests/run_tests.py 003 008        # 只跑指定用例（支持子串匹配）
    python3 tests/run_tests.py --list         # 列出全部用例与依赖
    python3 tests/run_tests.py -j4            # 并行 4 个进程（默认自动取 CPU 数）

约定：
    tests/cases/NNN_name.fa           测试用例
    tests/cases/NNN_name.expected     期望的标准输出

    用例开头的注释可以声明「预期失败」，用来测 panic / assert / 越界 / 编译期报错：
        # expect-exit: 134            进程应当以这个码退出（默认 0）
        # expect-stdout: 子串          标准输出里必须出现这个子串
        # expect-stderr: 子串          标准错误里必须出现这个子串
        # expect-compile-error: 子串   编译必须失败，且报错信息里含这个子串
    声明了 expect-exit / expect-compile-error 的用例不再比对 .expected。

设计要点（都是踩过的坑）：
  * **单个用例失败绝不打断整个套件**：编译期抛出的任何异常（RuntimeError /
    FaCodegenError / Python 内部 TypeError……）都会被捕获并记为该用例失败，
    这样一次运行就能看到全貌，而不是只看到第一个崩溃点。
  * **环境依赖自动降级为 SKIP**：`use py` 需要 Python 开发头文件，`use java`
    需要 JDK。缺了就标记跳过并说明原因，不算失败 —— 但在 --strict 下算失败。
  * **C++ 夹具按需构建**：tests/cases/cpp/libmathlib.so 是构建产物，不入库；
    首次运行时用 g++ 从同目录的 mathlib.cpp 编出来。
"""

from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "tests", "cases")
CPP_FIXTURE = os.path.join(CASES, "cpp")
sys.path.insert(0, os.path.join(ROOT, "compiler"))

from falang.driver import build      # noqa: E402


# ------------------------------------------------------------------ 环境探测
def python_headers_available() -> bool:
    """`use py` 需要 Python.h（通常由 python3-dev 提供）"""
    inc = sysconfig.get_paths().get("include") or ""
    return bool(inc) and os.path.exists(os.path.join(inc, "Python.h"))


def jvm_available() -> bool:
    """`use java` 需要某个 JDK 的 include/jni.h"""
    import glob
    cands = []
    if os.environ.get("JAVA_HOME"):
        cands.append(os.environ["JAVA_HOME"])
    cands += sorted(glob.glob("/usr/lib/jvm/*")) + sorted(glob.glob("/usr/java/*"))
    return any(os.path.exists(os.path.join(c, "include", "jni.h")) for c in cands)


def build_cpp_fixture() -> str:
    """确保 tests/cases/cpp/libmathlib.so 存在，返回错误信息（成功为 ""）"""
    so = os.path.join(CPP_FIXTURE, "libmathlib.so")
    src = os.path.join(CPP_FIXTURE, "mathlib.cpp")
    if os.path.exists(so) and os.path.getmtime(so) >= os.path.getmtime(src):
        return ""
    if not os.path.exists(src):
        return f"缺少 C++ 夹具源码 {src}"
    cxx = os.environ.get("CXX", "g++")
    if shutil.which(cxx) is None:
        return f"未找到 {cxx}，无法构建 C++ 夹具"
    r = subprocess.run([cxx, "-O2", "-std=c++17", "-fPIC", "-shared", src, "-o", so],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return f"C++ 夹具编译失败：\n{r.stderr.strip()}"
    return ""


def case_requirements(src: str):
    """返回 [(依赖名, 是否满足, 缺失原因)]"""
    reqs = []
    if re.search(r"^\s*use\s+py\b", src, re.M):
        reqs.append(("CPython 头文件", python_headers_available(),
                     "需要 python3-dev（Python.h）"))
    if re.search(r"^\s*use\s+java\b", src, re.M):
        reqs.append(("JDK", jvm_available(), "需要 JDK（libjvm.so + jni.h）"))
    if re.search(r"^\s*use\s+cxx\b", src, re.M):
        err = build_cpp_fixture()
        reqs.append(("C++ 夹具", err == "", err or ""))
    return reqs


def case_expectations(src: str) -> dict:
    """解析用例开头的 `# expect-*: 值` 指令"""
    exp = {"exit": 0, "stdout": [], "stderr": [], "compile_error": []}
    for line in src.split("\n"):
        m = re.match(r"\s*#\s*expect-([a-z_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "exit":
            exp["exit"] = int(val)
        elif key == "stdout":
            exp["stdout"].append(val)
        elif key == "stderr":
            exp["stderr"].append(val)
        elif key in ("compile-error", "compile_error"):
            exp["compile_error"].append(val)
    return exp


# ------------------------------------------------------------------ 用例执行
def list_cases(filters=None):
    out = []
    for f in sorted(os.listdir(CASES)):
        if not f.endswith(".fa"):
            continue
        if filters and not any(k in f for k in filters):
            continue
        out.append(f)
    return out


PASS, FAIL, SKIP = "pass", "fail", "skip"

# 优化级别：默认 2。`--opt 0/1/3` 可以用同一批用例做差分测试，
# 专门抓优化器的 bug（外提、复制合并、窥孔……任何一级结果都必须一样）。
OPT = 2


def run_case(name: str, record: bool = False):
    """返回 (状态, 说明)。任何异常都被兜住，不会打断整个套件。"""
    src_path = os.path.join(CASES, name)
    base = os.path.splitext(name)[0]
    exp_path = os.path.join(CASES, base + ".expected")
    tmp = tempfile.mkdtemp(prefix="fa_test_")
    try:
        with open(src_path) as f:
            src = f.read()
        for dep, ok, why in case_requirements(src):
            if not ok:
                return SKIP, f"缺少 {dep}：{why}"

        exp = case_expectations(src)

        if exp["compile_error"]:
            # 这个用例就是要在编译期报错：直接看前端的诊断信息
            from falang.driver import frontend
            try:
                res = frontend(src, name)
            except Exception as e:
                import traceback
                return FAIL, f"编译异常 {type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}"
            if res.ok:
                return FAIL, "期望编译失败，但编译通过了"
            missing = [k for k in exp["compile_error"] if k not in res.error]
            if missing:
                return FAIL, f"报错信息里没找到 {missing}：\n{res.error}"
            return PASS, ""

        exe = os.path.join(tmp, "prog")
        try:
            rc = build(src_path, exe, opt=OPT)
        except Exception as e:                       # 编译期任何异常都算用例失败
            import traceback
            return FAIL, f"编译异常 {type(e).__name__}: {e}\n{traceback.format_exc()[-1200:]}"
        if rc != 0:
            return FAIL, "编译失败（详见上方 stderr）"

        env = dict(os.environ)
        env["FA_FLUSH"] = "1"       # 逐行刷新，保证崩溃时也能看到已产生的输出
        try:
            p = subprocess.run([exe], cwd=CASES, capture_output=True, text=True,
                               timeout=120, env=env)
        except subprocess.TimeoutExpired:
            return FAIL, "运行超时（120s）"
        actual = p.stdout
        if exp["exit"] or exp["stdout"] or exp["stderr"]:
            # 「预期失败」用例：只校验退出码与输出里的关键子串
            if p.returncode != exp["exit"]:
                sig = f"（信号 {-p.returncode}）" if p.returncode < 0 else ""
                return FAIL, (f"退出码 {p.returncode}{sig}，期望 {exp['exit']}\n"
                              f"--- stdout ---\n{actual}\n--- stderr ---\n{p.stderr[-600:]}")
            for needle in exp["stdout"]:
                if needle not in actual:
                    return FAIL, f"stdout 里没有 {needle!r}\n--- stdout ---\n{actual}"
            for needle in exp["stderr"]:
                if needle not in p.stderr:
                    return FAIL, f"stderr 里没有 {needle!r}\n--- stderr ---\n{p.stderr[-600:]}"
            return PASS, ""
        if p.returncode != 0:
            sig = f"（信号 {-p.returncode}）" if p.returncode < 0 else ""
            return FAIL, (f"运行退出码 {p.returncode}{sig}\n--- stdout ---\n{actual}"
                          f"\n--- stderr ---\n{p.stderr[-800:]}")
        if record:
            with open(exp_path, "w") as f:
                f.write(actual)
            return PASS, "已记录期望输出"
        if not os.path.exists(exp_path):
            return FAIL, "缺少 .expected 文件（请先运行 --record 并人工复核）"
        with open(exp_path) as f:
            expected = f.read()
        if actual != expected:
            import difflib
            diff = "\n".join(difflib.unified_diff(
                expected.splitlines(), actual.splitlines(),
                fromfile="期望", tofile="实际", lineterm=""))
            return FAIL, "输出不一致:\n" + diff
        return PASS, ""
    except Exception as e:                           # 兜底：运行器自身的问题也不许炸
        import traceback
        return FAIL, f"运行器异常 {type(e).__name__}: {e}\n{traceback.format_exc()[-1200:]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    argv = sys.argv[1:]
    record = "--record" in argv
    strict = "--strict" in argv
    listing = "--list" in argv
    jobs, explicit_jobs, args = 0, False, []
    global OPT
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--opt":
            i += 1
            OPT = int(argv[i])
        elif a.startswith("--opt="):
            OPT = int(a.split("=", 1)[1])
        elif a.startswith("-j") and len(a) > 2:
            jobs, explicit_jobs = max(1, int(a[2:])), True
        elif a in ("--jobs", "-j"):
            i += 1
            jobs, explicit_jobs = max(1, int(argv[i])), True
        elif not a.startswith("-"):
            args.append(a)
        i += 1
    if not explicit_jobs:
        # 默认并行：绝大部分时间是等外部进程（gcc/as），并行收益明显
        jobs = min(8, (os.cpu_count() or 1) * 2)

    cases = list_cases(args)
    if not cases:
        print("没有匹配的用例")
        return 1

    if listing:
        for c in cases:
            with open(os.path.join(CASES, c)) as f:
                src = f.read()
            deps = [d for d, _, _ in case_requirements(src)]
            print(f"  {c}" + (f"   [需要: {', '.join(deps)}]" if deps else ""))
        return 0

    print(f"FA 测试套件 —— 共 {len(cases)} 个用例"
          f"{'（记录模式）' if record else ''}，并行 {jobs}，-O{OPT}")
    print("-" * 64)

    results = {}
    if jobs <= 1:
        for c in cases:
            results[c] = run_case(c, record)
            _report(c, results[c])
    else:
        # 编译期 stderr 需要串行才看得清；并行时先收集，最后统一按用例名顺序打印
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [(c, ex.submit(run_case, c, record)) for c in cases]
            for c, fut in futs:
                results[c] = fut.result()
        for c in cases:
            _report(c, results[c])

    npass = sum(1 for s, _ in results.values() if s == PASS)
    nfail = sum(1 for s, _ in results.values() if s == FAIL)
    nskip = sum(1 for s, _ in results.values() if s == SKIP)
    print("-" * 64)
    tail = f"通过 {npass} / {len(cases)}"
    if nfail:
        tail += f"，失败 {nfail}"
    if nskip:
        tail += f"，跳过 {nskip}（环境依赖缺失）"
    if not nfail and not nskip:
        tail += "  —— 全部通过"
    elif not nfail:
        tail += "  —— 无失败"
    print(tail)
    if nfail:
        print("失败用例：" + " ".join(c for c in cases if results[c][0] == FAIL))
    if strict and nskip:
        return 1
    return 0 if nfail == 0 else 1


def _report(name, res):
    status, msg = res
    mark = {PASS: "✓", FAIL: "✗", SKIP: "○"}[status]
    if status == PASS:
        print(f"  {mark} {name}")
    elif status == SKIP:
        print(f"  {mark} {name}: 跳过 —— {msg}")
    else:
        print(f"  {mark} {name}: {msg}")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
