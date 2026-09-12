# FA (FYX-all)

> **目标：功能最全 + 速度最快。**
> 一门能被零基础的人 30 分钟学会、又能直接调用 **C / C++ / Python / Java** 一切生态的系统级语言。

FA 编译器直接生成 **x86-64 原生机器码**（经 GNU as 汇编），不依赖 LLVM，不需要运行时虚拟机。
它把 CPython 与 JVM **嵌入**到你的程序里，所以 Python 的 numpy/pandas/torch、Java 的整个 Maven 生态，
都是你的一行 `use` 就能调用的函数库。

---

## 1. 五分钟上手

```bash
# 0) 安装（把 fa 命令放到 PATH）
chmod +x bin/fa
export PATH=$PWD/bin:$PATH          # 或运行 ./install.sh 安装到 /usr/local/bin

# 1) 写第一个程序
cat > hello.fa <<'EOF'
fn main() -> i64:
    print("你好, FA!")
    return 0
EOF

# 2) 运行
fa run hello.fa                     # 编译并运行
fa build hello.fa -o hello && ./hello   # 或编译成可执行文件
```

命令一览：

| 命令 | 作用 |
|---|---|
| `fa run x.fa` | 编译并立即运行 |
| `fa build x.fa -o out` | 编译为可执行文件 |
| `fa asm x.fa` | 只输出 x86-64 汇编：打到 stdout，并存一份 `x.s`（`-o` 可指定路径） |
| `fa check x.fa` | 只做语法/类型检查 |
| `fa tokens x.fa` / `fa ast x.fa` | 转储词法 / 语法树（自举比对用） |
| `fa version` | 版本 + Python/JVM 环境探测 |

常用选项：`-O 0..3` 优化级别（默认 2）、`--emit-asm` 额外导出汇编、`-v` 显示详细过程、`-k` 保留中间产物。

---

## 2. 语言一瞥

```fa
use py                                   # 想用 Python 生态？一行搞定

struct Point:
    x: f64
    y: f64

impl Point:
    fn norm(self) -> f64:                # 方法（self 可以裸写）
        return sqrt(self.x * self.x + self.y * self.y)

fn fib(n: i64) -> i64:                   # 递归，直接编译成循环级别的机器码
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

fn main() -> i64:
    let name = "FA"
    print("{name}: fib(20) = {fib(20)}") # 字符串插值

    let p = Point { x: 3.0, y: 4.0 }
    print(p.norm())                      # 5.0

    let v = Vec<i64>[1, 2, 3]            # 动态数组（自动引用计数）
    v.push(4)
    let s = 0
    for x in v:
        s = s + x
    print(s)                             # 10

    let np = py.import("numpy")          # 直接用 numpy
    py.exec("print(' numpy:', __import__('numpy').arange(5).sum())")
    return 0
```

---

## 3. 文档索引

| 文档 | 内容 |
|---|---|
| [01_教程.md](docs/01_教程.md) | **零基础教程**：从 `print` 到结构体、容器、错误处理，一步一步来 |
| [02_语言参考.md](docs/02_语言参考.md) | 完整语法与语义（类型、运算符、控制流、所有权规则） |
| [03_标准库.md](docs/03_标准库.md) | 内建函数、字符串 / Vec / Map 的全部方法 |
| [04_互操作.md](docs/04_互操作.md) | 调用 **C / C++ / Python / Java** 的全部写法与示例 |
| [05_迁移速查.md](docs/05_迁移速查.md) | 从 Python / C / Java / Go / Rust 过来的对照表 |
| [06_性能.md](docs/06_性能.md) | 与 C、Python 的实测基准对比 |
| [07_编译器架构.md](docs/07_编译器架构.md) | 编译器内部：lexer → parser → sema → IR → 寄存器分配 → 汇编 |

---

## 4. 目录结构

```
fa/
├── bin/fa                 命令行启动器
├── compiler/falang/       Python 引导实现的编译器前端 + x86-64 后端
│   ├── lexer.py           词法（缩进 / 大括号双模式）
│   ├── parser.py          语法（混合缩进与大括号）
│   ├── sema.py            语义 + 类型检查
│   ├── codegen.py         AST → 线性 IR
│   ├── regalloc.py        线性扫描寄存器分配
│   ├── asmgen.py          IR → x86-64 GNU as 汇编
│   └── driver.py / cli.py 编译流程与命令行
├── runtime/               运行时（C + 手写汇编的热路径）
│   ├── fa_runtime.c       内存、字符串、Vec/Map、引用计数、I/O
│   ├── fa_syscall.S       手写汇编的 syscall 封装
│   ├── fa_python.c        内嵌 CPython（可用一切 Python 扩展）
│   └── fa_jvm.c           内嵌 JVM（JNI）
├── examples/              示例程序（含四种互操作的完整例子）
├── tests/                 回归测试（python3 tests/run_tests.py）
├── bench/                 性能基准（FA vs C vs Python）
└── docs/                  文档
```

---

## 5. 自动化测试

```bash
python3 tests/run_tests.py            # 跑全部用例（55 个）
python3 tests/run_tests.py --opt 0    # 换个优化级别再跑一遍（差分测试）
python3 tests/run_tests.py 010        # 只跑名字里含 010 的用例
python3 tests/run_tests.py --record   # 把当前输出记录为期望输出（改动需人工复核）
```

用例有两种断言方式：`tests/cases/<名字>.expected` 存期望输出；或者在源码开头写指令
（`# expect-compile-error: 关键字`、`# expect-exit: 3`、`# expect-stderr: 越界`），
用来测编译期诊断和运行时错误。缺少 Python 开发头文件 / JDK 时，
`021_python`、`022_java` 会自动 SKIP 而不是失败。

---

## 6. 当前状态

- ✅ 端到端可用：**55 个回归用例，53 通过 + 2 跳过（环境依赖），0 失败**；`-O0/-O1/-O2/-O3` 四个优化级别结果完全一致
- ✅ 语言核心：函数 / 递归 / 结构体 / 方法（带参数、返回聚合值）/ 枚举（带载荷）/
  模式匹配（缩进、单行、花括号三种写法）/ 数组 / 指针（`p.field` 自动解引用）/
  递归类型（链表、树）/ Vec / Map / 字符串 / 插值（可含 `m["k"]`）/ 多行字符串 /
  函数指针 / `defer` / 多文件模块（`use "other.fa"`）/ 内嵌汇编
- ✅ 下标统一：`v[i]`、`m[k]`、`s[i]`、`a[i]` 都能读写，越界会报运行时错误
- ✅ 自动内存管理：编译期插入引用计数，作用域结束自动释放
  （实测 200 万次「建 Vec + 建 Map + 拼字符串」的循环，RSS 恒定在 ~0.9 MB）
- ✅ 四条互操作链路：C、C++（自动生成 shim）、Python（含 numpy）、Java（JDK 类库）
- ✅ 运行时安全网：段错误 / 除零 / 下标越界 / 空 Vec pop / assert 失败都有中文报错和非零退出码
- ✅ 性能（2 核 Xeon 2.60 GHz 虚拟机，gcc 12.2，实测见 [06_性能.md](docs/06_性能.md)）：

  | 基准 | FA | C -O2 | C -O2 -fno-inline | CPython | FA/C | FA/C 同算法 |
  |---|---|---|---|---|---|---|
  | fib(38) 递归 | 291 ms | 70 ms | 111 ms | 5256 ms | 4.16× | 2.62× |
  | 1 亿次算术循环 | 94 ms | 86 ms | 86 ms | 15668 ms | **1.09×** | 1.09× |
  | 200 万内素数筛 | 19 ms | 8.2 ms | 8.6 ms | 121 ms | 2.27× | 2.17× |

  比 CPython 快 **6.5~167 倍**。fib 的差距主要来自 gcc 把递归内联进自己并消掉公共子表达式；
  按「同算法」比（`-fno-inline`）是 2.62 倍。

---

## 7. 已知限制（还没做到的）

写代码前值得知道这些，省得踩坑：

| 还不支持 | 现在的替代写法 |
|---|---|
| 闭包 / 捕获环境的 lambda | 顶层函数 + 函数指针 `fn(i64) -> i64` |
| 函数体里再定义 `fn` | 提到顶层 |
| trait / interface / 泛型函数 | 每种类型各写一个 `impl`；容器用 `Vec<T>` / `Map<K,V>`（类型参数已支持嵌套） |
| `if` 当表达式用（三元） | `let y = 0` + `if ...: y = 1` |
| 顶层 `let` 全局可变变量 | `const`（不可变），或放进结构体 |
| `T?` 可选类型 | 只用于指针；其它类型请用枚举 `enum Opt: None / Some(v: T)` |
| 结构体字面量省略字段 | 所有字段都要写（缺字段会在语义阶段报错） |
| `m[k]` 之外的 Map 语法糖 | `m.get/set/has/del` 全都有；`m[k]` / `m[k] = v` 现在也支持了 |
| 字符串按字符（而非字节）索引 | `s[i]` 取的是**字节**（char = u8）；要按码点处理先用 `s.split("")` 之类自行解码 |
| Python / Java 互操作 | 需要 python3-dev 头文件 / JDK；没有时这两个测试会 SKIP |

- 🚧 待办：自举（用 FA 重写编译器）、包管理器、公共子表达式消除、自动向量化、泛型函数、闭包
