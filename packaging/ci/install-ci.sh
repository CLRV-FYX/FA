#!/usr/bin/env bash
# 把 CI 定义装到 .github/workflows/ 下。
#
#   ./packaging/ci/install-ci.sh
#
# 为什么要单独一步：流水线的定义放在 packaging/ci/ 里，而不是直接放
# .github/workflows/。GitHub App 令牌默认没有 workflows 权限，
# 往 .github/workflows/ 里写文件会被整个 push 拒掉：
#
#   ! [remote rejected] (refusing to allow a GitHub App to create or update
#     workflow `.github/workflows/release.yml` without `workflows` permission)
#
# 一个文件把整批提交都挡住，不值得。所以定义随仓库走，启用由有权限的人一条命令完成
# （给 App 装上 workflows 写权限，或者用个人令牌 push 这一次）。

set -euo pipefail

cd "$(dirname "$0")/../.." || exit 1
ROOT="$PWD"
SRC="packaging/ci/release.yml"
DST=".github/workflows/release.yml"

if [ ! -f "$SRC" ]; then
    echo "找不到 $SRC" >&2
    exit 1
fi

mkdir -p .github/workflows
cp "$SRC" "$DST"
echo "✓ 已装到 $DST"

# 装完自己验一遍：YAML 能不能解析、作业和步骤齐不齐。
# CI 跑一次要几分钟还得排队，语法错在这儿就该拦住。
if python3 -c "import yaml" 2>/dev/null; then
    python3 - "$DST" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
jobs = d.get("jobs", {})
assert jobs, "没有 jobs"
need = {"test", "package", "windows-smoke", "release"}
missing = need - set(jobs)
assert not missing, f"缺作业：{missing}"
assert jobs["windows-smoke"]["runs-on"] == "windows-latest", "Windows 作业跑错平台了"
assert jobs["package"].get("needs") == "test", "package 必须在 test 之后"
for name, j in jobs.items():
    assert j.get("steps"), f"{name} 没有步骤"
    print(f"  ✓ {name:16s} {j['runs-on']:16s} {len(j['steps'])} 步")
print("✓ 流水线定义合法")
PY
else
    echo "  ! 没有 pyyaml，跳过 YAML 校验（pip install --user pyyaml）"
fi

echo
echo "接下来：git add .github && git commit && git push"
echo "如果 push 被拒（workflows 权限），到仓库 Settings → GitHub Apps 里"
echo "给这个 App 勾上 Workflows 的写权限，或者用个人令牌 push 这一次。"
