#!/usr/bin/env bash
# FA 安装脚本：把 fa 命令装到 /usr/local/bin（或 $PREFIX）
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/usr/local}"
BIN_DIR="$PREFIX/bin"
LIB_DIR="$PREFIX/lib/fa"

echo "安装 FA 到 $BIN_DIR ..."
mkdir -p "$BIN_DIR" "$LIB_DIR"
cp -r "$HERE/compiler" "$HERE/runtime" "$LIB_DIR/"
mkdir -p "$LIB_DIR/build"
for f in "$HERE"/build/*.o; do
    [ -e "$f" ] && cp "$f" "$LIB_DIR/build/" 2>/dev/null || true
done

cat > "$BIN_DIR/fa" <<LAUNCHER
#!/usr/bin/env bash
# FA (FYX-all) 启动器
export PYTHONPATH="$LIB_DIR/compiler:\${PYTHONPATH}"
if command -v python3 >/dev/null 2>&1; then
    exec python3 -c "
import sys, os
sys.path.insert(0, '$LIB_DIR/compiler')
os.environ.setdefault('FA_HOME', '$LIB_DIR')
from falang.cli import main
sys.exit(main())
" "\$@"
else
    echo "错误：需要 python3 来运行 FA 编译器" >&2
    exit 1
fi
LAUNCHER
chmod +x "$BIN_DIR/fa"

# 本地开发用（不安装时也能跑）
chmod +x "$HERE/bin/fa" 2>/dev/null || true

echo "完成。试试："
echo "    fa version"
echo "    echo 'fn main() -> i64: print(\"你好\") return 0' > /tmp/hi.fa && fa run /tmp/hi.fa"
