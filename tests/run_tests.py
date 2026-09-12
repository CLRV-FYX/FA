#!/usr/bin/env python3
"""FA 回归测试运行器

用法:
    python3 tests/run_tests.py            # 运行全部用例并比对 .expected
    python3 tests/run_tests.py --record   # 把当前实际输出记录为期望输出（改动需人工复核）
    python3 tests/run_tests.py 003 008    # 只跑指定用例（支持子串匹配）

约定：
    tests/cases/NNN_name.fa       测试用例
    tests/cases/NNN_name.expected 期望的标准输出
"""

from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "tests", "cases")
sys.path.insert(0, os.path.join(ROOT, "compiler"))

from falang.driver import build      # noqa: E402


def list_cases(filters=None):
    out = []
    for f in sorted(os.listdir(CASES)):
        if not f.endswith(".fa"):
            continue
        if filters and not any(k in f for k in filters):
            continue
        out.append(f)
    return out


def run_case(name: str, record: bool):
    src = os.path.join(CASES, name)
    base = os.path.splitext(name)[0]
    exp_path = os.path.join(CASES, base + ".expected")
    tmp = tempfile.mkdtemp(prefix="fa_test_")
    ok = True
    msg = ""
    try:
        exe = os.path.join(tmp, "prog")
        rc = build(src, exe, opt=2)
        if rc != 0:
            return False, "编译失败"
        env = dict(os.environ)
        env["FA_FLUSH"] = "1"          # 逐行刷新，保证崩溃时也能看到已产生的输出
        p = subprocess.run([exe], cwd=CASES, capture_output=True, text=True,
                           timeout=120, env=env)
        actual = p.stdout
        if p.returncode not in (0,):
            return False, f"运行退出码 {p.returncode}\n--- stdout ---\n{actual}\n--- stderr ---\n{p.stderr[-800:]}"
        if record:
            with open(exp_path, "w") as f:
                f.write(actual)
            return True, "已记录期望输出"
        if not os.path.exists(exp_path):
            return False, "缺少 .expected 文件（请先运行 --record 并人工复核）"
        with open(exp_path) as f:
            expected = f.read()
        if actual != expected:
            ok = False
            import difflib
            diff = "\n".join(difflib.unified_diff(
                expected.splitlines(), actual.splitlines(),
                fromfile="期望", tofile="实际", lineterm=""))
            msg = "输出不一致:\n" + diff
    except subprocess.TimeoutExpired:
        return False, "运行超时"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok, msg


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    record = "--record" in sys.argv
    cases = list_cases(args)
    if not cases:
        print("没有匹配的用例")
        return 1
    npass = nfail = 0
    print(f"FA 测试套件 —— 共 {len(cases)} 个用例{'(记录模式)' if record else ''}")
    print("-" * 60)
    for c in cases:
        ok, msg = run_case(c, record)
        if ok:
            npass += 1
            print(f"  ✓ {c}")
        else:
            nfail += 1
            print(f"  ✗ {c}: {msg}")
    print("-" * 60)
    print(f"通过 {npass} / {len(cases)}" + (f"，失败 {nfail}" if nfail else "  —— 全部通过"))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
