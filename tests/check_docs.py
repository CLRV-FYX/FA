#!/usr/bin/env python3
"""把文档里的 ```fa 代码块喂给 fa check —— 文档说的必须是编译器真能做的。"""
import os, re, subprocess, sys, tempfile, textwrap

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


def check(src, workdir):
    path = os.path.join(workdir, "_doc_check.fa")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    r = subprocess.run(["bash", os.path.join(ROOT, "bin/fa"), "check", path],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, (r.stdout + r.stderr).strip()


stats = {"strict": 0, "lenient": 0, "skip": 0}
bad = []
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
        if "❌" in code or "expect-compile-error" in code or PLACEHOLDER.search(code):
            stats["skip"] += 1                     # 反面教材 / 带占位符的模板
            continue
        if any(l.count("fn ") > 1 for l in code.split("\n")):
            stats["skip"] += 1                     # 左右并排对照两种语法，不是一个文件
            continue
        strict = re.search(r"^fn\s+main\b", code, re.M) is not None
        with tempfile.TemporaryDirectory() as work:
            # 一个块里写了多个文件（`// math.fa` + `// main.fa`）：拆开各写各的
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
                continue
            if not strict and any(k in err for k in LENIENT_IGNORE):
                stats["lenient"] += 1              # 片段本来就是节选，缺名字很正常
                continue
        bad.append((rel, lineno, "完整程序" if strict else "教学片段", err))

total = sum(stats.values())
print(f"文档代码块 {total} 个：完整程序 {stats['strict']}，教学片段 {stats['lenient']}，"
      f"跳过 {stats['skip']}（反面教材/模板/引用块）；不通过 {len(bad)}")
for rel, lineno, mode, msg in bad:
    print(f"  [{mode}] {rel}:{lineno}  {msg}")
sys.exit(1 if bad else 0)
