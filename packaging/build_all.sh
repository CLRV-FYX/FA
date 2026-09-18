#!/usr/bin/env bash
# 一次把 FA 的三种交付物全打出来，打之前先把所有测试跑绿。
#
#   ./packaging/build_all.sh              # 测试 + deb + vsix + windows zip
#   ./packaging/build_all.sh --skip-tests # 只打包（自己确信测试是绿的时候）
#   ./packaging/build_all.sh --only deb   # 只打其中一种
#
# 产物落在 dist/：
#   fa_<版本>_amd64.deb              Linux/Debian/Ubuntu/WSL：sudo apt install ./xxx.deb
#   fa-<版本>-windows-amd64.zip      Windows：解压即用（要装 Python，见包内 README）
#   fa-vscode-<版本>.vsix            VSCode 扩展：code --install-extension xxx.vsix
#
# 每个打包脚本打完都会**自己验一遍**（解包到临时目录、真跑 fa version/check/run、
# 跑 lsp 和 ide 的自检、和仓库里直接跑的输出逐字节比对）。所以这个脚本的退出码
# 就是「这批交付物能不能用」的答案，不是「命令有没有跑完」。

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
PY="${PYTHON:-python3}"
SKIP_TESTS=0
ONLY=""

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-tests) SKIP_TESTS=1 ;;
        --only)       shift; ONLY="${1:-}" ;;
        -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
        *)            echo "不认识的参数：$1（-h 看用法）" >&2; exit 2 ;;
    esac
    shift
done

FAILED=()
STEP_START=$(date +%s)

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED+=("$1"); }

want() {  # want deb → 该不该打 deb
    [ -z "$ONLY" ] && return 0
    [ "$ONLY" = "$1" ]
}

# ---------------------------------------------------------------- 测试
if [ "$SKIP_TESTS" = 0 ] && [ -z "$ONLY" ]; then
    step "跑测试（五个套件，共约 290 条断言）"

    # fa selftest 自己会把词法/语法/语义/代码生成四套跑一遍
    if ./bin/fa selftest >/tmp/fa_st.log 2>&1; then
        ok "编译器自测　$(grep -oE '[0-9]+ / [0-9]+ 套自测通过' /tmp/fa_st.log | tail -1)"
    else
        bad "编译器自测（详见 /tmp/fa_st.log）"
        tail -5 /tmp/fa_st.log
    fi

    if "$PY" lsp/test_fa_lang.py >/tmp/fa_lang.log 2>&1; then
        ok "语言内核　　$(grep -oE '通过 [0-9]+ 条' /tmp/fa_lang.log | tail -1)"
    else
        bad "语言内核（详见 /tmp/fa_lang.log）"; tail -5 /tmp/fa_lang.log
    fi

    if "$PY" lsp/fa_lsp.py --selftest >/tmp/fa_lsp.log 2>&1; then
        ok "LSP 协议　　通过 $(grep -c '✓' /tmp/fa_lsp.log) 条"
    else
        bad "LSP 协议（详见 /tmp/fa_lsp.log）"; tail -5 /tmp/fa_lsp.log
    fi

    if "$PY" ide/server.py --selftest >/tmp/fa_ide.log 2>&1; then
        ok "IDE 后端　　$(grep -oE '通过 [0-9]+ 条' /tmp/fa_ide.log | tail -1)"
    else
        bad "IDE 后端（详见 /tmp/fa_ide.log）"; tail -5 /tmp/fa_ide.log
    fi

    if command -v node >/dev/null 2>&1; then
        if node ide/test_frontend.js >/tmp/fa_fe.log 2>&1; then
            ok "IDE 前端　　$(grep -oE '通过 [0-9]+ 条' /tmp/fa_fe.log | tail -1)"
        else
            bad "IDE 前端（详见 /tmp/fa_fe.log）"; tail -5 /tmp/fa_fe.log
        fi
        if node editors/vscode-fa/test/extension.test.js >/tmp/fa_ext.log 2>&1; then
            ok "VSCode 扩展　$(grep -oE '通过 [0-9]+ 条' /tmp/fa_ext.log | tail -1)"
        else
            bad "VSCode 扩展（详见 /tmp/fa_ext.log）"; tail -5 /tmp/fa_ext.log
        fi
    else
        echo "  ! 没有 node，跳过前端与扩展的自测"
    fi

    # 语言测试用例（每个 .fa 配一个 .expected，逐个真跑真比）
    if [ -d tests/cases ]; then
        n=$(ls tests/cases/*.fa 2>/dev/null | wc -l)
        if "$PY" tests/run_tests.py >/tmp/fa_cases.log 2>&1; then
            # 最后一行是完整结论，跳过几个、为什么跳过都在里面
            ok "语言用例　　$(grep -E '^通过 ' /tmp/fa_cases.log | tail -1)"
            grep -E '^  ○ ' /tmp/fa_cases.log | sed 's/^/      /' 
        else
            bad "语言用例（详见 /tmp/fa_cases.log）"; tail -8 /tmp/fa_cases.log
        fi
    fi

    if [ "${#FAILED[@]}" -gt 0 ]; then
        printf '\n测试没过，不打包（--skip-tests 可以强行打，但别这么干）\n'
        exit 1
    fi
fi

# ---------------------------------------------------------------- 打包
mkdir -p dist

if want vsix; then
    step "打 VSCode 扩展 (.vsix)"
    if "$PY" packaging/build_vsix.py; then ok "vsix"; else bad "vsix"; fi
fi

if want deb; then
    step "打 Debian 包 (.deb)"
    if command -v dpkg-deb >/dev/null 2>&1; then
        if "$PY" packaging/build_deb.py; then ok "deb"; else bad "deb"; fi
    else
        bad "deb（没有 dpkg-deb，装不了就打不出来）"
    fi
fi

if want windows; then
    step "打 Windows 包 (.exe + .zip)"
    if "$PY" -c "import ziglang" 2>/dev/null || command -v zig >/dev/null 2>&1; then
        if "$PY" packaging/build_windows.py; then ok "windows"; else bad "windows"; fi
    else
        echo "  ! 没有 zig：pip install --user ziglang（Windows exe 靠它交叉编译）"
        echo "    先只跑启动器逻辑的自测"
        if "$PY" packaging/build_windows.py --no-zig; then ok "windows 启动器自测"; else bad "windows 启动器自测"; fi
    fi
fi

# ---------------------------------------------------------------- 汇总
step "dist/ 里的交付物"
if [ -d dist ] && [ -n "$(ls -A dist 2>/dev/null)" ]; then
    for f in dist/*; do
        [ -f "$f" ] || continue
        b=$(wc -c <"$f")
        if [ "$b" -ge 1048576 ]; then sz="$(( b / 1048576 )).$(( b % 1048576 * 10 / 1048576 ))M"
        else sz="$(( b / 1024 ))K"; fi
        sum=$(sha256sum "$f" | cut -c1-16)
        printf '  %-42s %6s  sha256:%s…\n' "$(basename "$f")" "$sz" "$sum"
    done
else
    echo "  （空的）"
fi

ELAPSED=$(( $(date +%s) - STEP_START ))
printf '\n'
if [ "${#FAILED[@]}" -eq 0 ]; then
    printf '\033[32m全部打完并各自验过，用时 %ds\033[0m\n' "$ELAPSED"
    exit 0
fi
printf '\033[31m有 %d 步没过：\033[0m\n' "${#FAILED[@]}"
for f in "${FAILED[@]}"; do printf '  ✗ %s\n' "$f"; done
exit 1
