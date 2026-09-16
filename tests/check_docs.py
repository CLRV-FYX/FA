#!/usr/bin/env python3
"""把文档里的 ```fa 代码块喂给 fa check —— 文档说的必须是编译器真能做的。

再加一条：如果某个含 `fn main` 的代码块后面紧跟一个 ```text 块，就把它当成
「这段程序的输出」，真的跑一遍逐字比对 —— 文档里印出来的输出必须是真的。
（含 args() / file_read / now() / random() / cmd() 的示例结果不确定，只编译不比对。）

用法：
    python3 tests/check_docs.py            # 编译检查 + 输出比对
    python3 tests/check_docs.py --no-run   # 只做编译检查
"""
import os, re, subprocess, sys, tempfile, textwrap

RUN_OUTPUTS = "--no-run" not in sys.argv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["README.md"] + sorted(
    os.path.join("docs", f) for f in os.listdir(os.path.join(ROOT, "docs"))
    if f.endswith(".md"))
BLOCK = re.compile(r"```fa\n(.*?)```", re.S)
PLACEHOLDER = re.compile(r"\.\.\.|名字|类型\b|字段\b|变体A|参数\b")
DECL = re.compile(r"^(fn|struct|enum|impl|const|extern|use)\b")
# 教学片段本来就是一段节选：引用了别处才定义的名字、依赖一个并不存在的
# 头文件/动态库，这些都不算文档写错了
LENIENT_IGNORE = ("未定义的标识符", "找不到要导入的 FA 模块", "未知类型", "未知结构体",
                  "没有字段", "未定义变量", "未知构造器", "没有变体", "重复定义",
                  "找不到", "No such file", "没有那个", "缺少")
MULTIFILE = re.compile(r"^//\s*(\S+\.fa)\s*$", re.M)
# 紧跟在 fa 块后面的 ```text 块 = 这段程序的真实输出
OUTBLOCK = re.compile(r"\A\s*```(?:text|out)\n(.*?)```", re.S)
# 结果不确定的示例（读参数/文件/环境/时钟/随机数/外部命令）：只编译，不比对输出
NONDET = re.compile(r"\b(args|file_read|file_write|now|random|cmd|env|sleep)\s*\(")


def indent(code, n=4):
    pad = " " * n
    return "".join((pad + l if l.strip() else l) + "\n"
                   for l in code.rstrip("\n").split("\n"))


def candidates(code):
    """一个片段可能有几种「补全成完整程序」的方式，任意一种通过就算通过。

    返回顺序 = 可信度顺序（第一种最接近作者的意图），报错时报第一种那次的错，
    免得把「包装方式不合适」当成「文档写错了」。
    """
    out = []
    has_main = re.search(r"^fn\s+main\b", code, re.M) is not None
    as_is = code if has_main else code.rstrip("\n") + "\n\nfn main() -> i64:\n    return 0\n"
    wrapped = "fn main() -> i64:\n" + indent(code) + "    return 0\n"
    if not re.search(r"^(fn|struct|enum|impl|const|extern|use)\b", code, re.M):
        # 整块都是语句：本来就该包在 main 里
        return [wrapped, as_is]
    out.append(as_is)
    # 声明留在顶层，其余语句搬进 main（教程里最常见的一种片段）
    lines = code.rstrip("\n").split("\n")
    decls, stmts, in_decl = [], [], False
    for l in lines:
        if not l.strip():
            (decls if in_decl else stmts).append(l)
            continue
        if not l.startswith((" ", "\t")) and DECL.match(l):
            in_decl = True
        elif not l.startswith((" ", "\t")):
            in_decl = False
        (decls if in_decl else stmts).append(l)
    if decls and stmts:
        out.append("\n".join(decls).strip("\n") + "\n\nfn main() -> i64:\n"
                   + indent("\n".join(stmts)) + "    return 0\n")
    out.append(wrapped)
    return out


def run_program(src, workdir):
    """编译并运行，返回 (退出码, stdout)。"""
    path = os.path.join(workdir, "_doc_run.fa")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    env = dict(os.environ, FA_FLUSH="1")
    r = subprocess.run(["bash", os.path.join(ROOT, "bin/fa"), "run", path],
                       capture_output=True, text=True, cwd=workdir, env=env)
    return r.returncode, r.stdout


def check(src, workdir):
    path = os.path.join(workdir, "_doc_check.fa")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    r = subprocess.run(["bash", os.path.join(ROOT, "bin/fa"), "check", path],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, (r.stdout + r.stderr).strip()


stats = {"strict": 0, "lenient": 0, "skip": 0}
bad = []
problems = 0
for rel in FILES:
    text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    for m in BLOCK.finditer(text):
        raw = m.group(1)
        lineno = text[:m.start()].count("\n") + 1
        if all(l.startswith(">") or not l.strip() for l in raw.split("\n")):
            stats["skip"] += 1                     # 引用块里的演示，不是可编译源码
            continue
        code = textwrap.dedent("\n".join(
            l[2:] if l.startswith("> ") else l for l in raw.split("\n")))
        # 变参声明里的 `...`（`fn printf(fmt: *u8, ...) -> i32`、绑 C 的
        # `fn open(file: str, oflag: i32, ...) -> i32`）是**真语法**，不是省略号占位符。
        # 先摘掉再判，不然教程 §30 那种「绑一个 C 变参函数」的完整例子会被整块跳过，
        # 文档里印的输出就没人复核了。
        probe = re.sub(r",\s*\.\.\.\s*\)", ")", code)
        if "❌" in probe or "expect-compile-error" in probe or PLACEHOLDER.search(probe):
            stats["skip"] += 1                     # 反面教材 / 带占位符的模板
            continue
        if any(l.count("fn ") > 1 for l in code.split("\n")):
            stats["skip"] += 1                     # 左右并排对照两种语法，不是一个文件
            continue
        strict = re.search(r"^fn\s+main\b", code, re.M) is not None
        with tempfile.TemporaryDirectory() as work:
            # 一个块里写了多个文件（`// math.fa` 接着 `// main.fa`）：
            # 按标记拆开，各写各的文件，再查最后一个（主文件）
            parts = MULTIFILE.split(code)
            multifile = len(parts) > 1
            if multifile:
                for i in range(1, len(parts) - 1, 2):
                    with open(os.path.join(work, parts[i]), "w", encoding="utf-8") as f:
                        f.write(parts[i + 1])
                code = parts[-1]
                strict = True
            err = None
            for i, cand in enumerate(candidates(code)):
                if multifile:
                    # 导入路径改成刚写出来的那个文件的绝对路径
                    cand = re.sub(r'use\s+"([^"]+\.fa)"',
                                  lambda mm: 'use "%s"' % os.path.join(work, mm.group(1)),
                                  cand)
                rc, out = check(cand, work)
                if rc == 0:
                    err = None
                    break
                if i == 0:
                    # 报「原样」那次的错：后面几种是包过 main 的猜测版本，
                    # 它们的语法错往往只是包装方式不合适，不是文档写错了
                    err = out.split("\n")[0]
            if err is None:
                stats["strict" if strict else "lenient"] += 1
                # 输出比对：只对「完整程序 + 后面跟着 ```text 块」的做
                tail = OUTBLOCK.match(text[m.end():])
                if RUN_OUTPUTS and strict and tail and not NONDET.search(code) \
                        and len(parts) == 1:
                    want = tail.group(1).rstrip("\n")
                    rc, got = run_program(candidates(code)[0], work)
                    got = got.rstrip("\n")
                    if rc != 0 or got != want:
                        why = f"退出码 {rc}" if rc != 0 else "输出对不上"
                        problems += 1
                        bad.append((rel, lineno, "输出",
                                    f"{why}（文档写的 vs 实际跑的，见下）"))
                        bad.append(("", 0, "  文档", want.replace("\n", "\n         ")))
                        bad.append(("", 0, "  实际", got.replace("\n", "\n         ")))
                continue
            if not strict and any(k in err for k in LENIENT_IGNORE):
                stats["lenient"] += 1              # 片段本来就是节选，缺名字很正常
                continue
        problems += 1
        bad.append((rel, lineno, "完整程序" if strict else "教学片段", err))

total = sum(stats.values())
print(f"文档代码块 {total} 个：完整程序 {stats['strict']}，教学片段 {stats['lenient']}，"
      f"跳过 {stats['skip']}（反面教材/模板/引用块）；不通过 {problems} 个块")
for rel, lineno, mode, msg in bad:
    where = f"{rel}:{lineno}" if rel else "        "
    print(f"  [{mode}] {where}  {msg}")
sys.exit(1 if bad else 0)
