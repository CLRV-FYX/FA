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
| [08_完全教程.md](docs/08_完全教程.md) | **完全教程**：从装机到内存模型，逐行讲透，每个示例都附真实输出（推荐从这里开始） |
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
python3 tests/run_tests.py            # 跑全部用例（103 个）
python3 tests/run_tests.py --opt 0    # 换个优化级别再跑一遍（差分测试）
python3 tests/run_tests.py 010        # 只跑名字里含 010 的用例
python3 tests/run_tests.py --record   # 把当前输出记录为期望输出（改动需人工复核）
python3 tests/check_docs.py           # 把文档里的 ```fa 代码块逐个喂给 fa check
python3 tests/run_asan.py             # 整套用例在 AddressSanitizer 底下重跑（内存安全）
python3 tools/fill_doc_outputs.py     # 教程里的 @@OUT@@ 占位符换成程序的真实输出
python3 tools/check_doc_errors.py     # 反面教材（❌ 块）印的报错也要和真的一致
```

写文档时的两个小工具，都是 `check_docs.py` 覆盖不到的角落：
`fill_doc_outputs.py` 先写 ` ```text ` 里一个 `@@OUT@@` 占位符，它把上面那个
` ```fa ` 块真的跑一遍、把 stdout 逐字填进去（所以「输出」不可能是手打的美好愿望）；
`check_doc_errors.py` 盯的是带 ❌ 的块 —— `check_docs.py` 会跳过它们，于是那些
「报错长这样」的文本没人校验，改了措辞、动了行列号就悄悄对不上。它把每个 ❌ 块
真的喂给编译器（编得过的就真跑一遍看它 panic 什么），逐行比对文档印的文本，
`--fix` 直接按真实输出改回文档。

`check_docs.py` 是「文档不许说谎」的自动化守卫：它扫 `README.md` 与 `docs/*.md`
里的每个 ` ```fa ` 代码块，按可信度顺序补全成完整程序（原样 / 整块包进 `main` /
声明留顶层而语句搬进 `main`），任意一种通过 `fa check` 就算通过。
含 `fn main` 的**完整程序必须严格通过**（读者会直接复制去跑）；教学片段放宽
「引用了别处才定义的名字、依赖不存在的头文件/动态库」这类节选现象；
反面教材（带 ❌ 或 `expect-compile-error`）、`名字`/`类型` 这类模板占位、
左右并排对照两种语法的排版、以及引用块里的演示，都跳过。
一个块里写了多个文件（`// math.fa` 接 `// main.fa`）会按标记拆开各写各的文件再查。
含 `fn main` 的完整程序如果后面紧跟 ` ```text ` 输出块，还会**真的编译运行**，
把 stdout 与文档写的逐字节比对（含 `args` / `now` / `random` / 文件读写的示例
输出不可复现，跳过比对；`--no-run` 可整个关掉）。
出错时打印 `文件:行号` 和编译器自己的报错，退出码非 0。

`run_asan.py` 是「内存不许出错」的守卫：用 `-fsanitize=address` 重编一份运行时，
把每个用例链接上去跑（含泄漏检测）。这一步很有必要 —— 引用计数的 bug 大多
**不改变输出**：多减一次、少加一次、堆上拷贝不给字段加引用，都要等到内存被复用
或 glibc 巡查到堆元数据时才炸，小字符串往往一声不吭。它已经抓到并修掉了
字符串比较的 use-after-free、`new` 结构体不给字段加引用、`fa_print_bool` /
`chr()` / `split()` / 循环里的 `defer` / `print(C 的 char*)` 六处泄漏。
用例可以声明设计上的泄漏（`# expect-asan-leak: 16`，字节数对不上仍算失败）。

用例有两种断言方式：`tests/cases/<名字>.expected` 存期望输出；或者在源码开头写指令
（`# expect-compile-error: 关键字`、`# expect-exit: 3`、`# expect-stderr: 越界`），
用来测编译期诊断和运行时错误。缺少 Python 开发头文件 / JDK 时，
`021_python`、`022_java` 会自动 SKIP 而不是失败。

---

## 6. 当前状态

- ✅ 端到端可用：**75 个回归用例，73 通过 + 2 跳过（环境依赖），0 失败**；`-O0/-O1/-O2/-O3` 四个优化级别结果完全一致；
  整套用例在 **AddressSanitizer（含泄漏检测）下无内存问题**（`python3 tests/run_asan.py --examples`：63 干净 / 17 按规则跳过）
- ✅ 语言核心：函数 / 递归 / 结构体 / 方法（带参数、返回聚合值）/ 枚举（带载荷）/
  模式匹配（缩进、单行、花括号三种写法）/ 数组 / 指针（`p.field` 自动解引用）/
  递归类型（链表、树）/ Vec / Map / 字符串 / 插值（可含 `m["k"]`）/ 多行字符串 /
  函数指针 / `defer` / 多文件模块（`use "other.fa"`）/ 内嵌汇编 /
  **顶层 `let` 全局可变变量** / **函数体里再定义 `fn`**（提升实现，不能捕获）/
  **结构体字段默认值**（字面量里可省略）/ **裸块 `{ ... }` 当作用域用** /
  `char` 参与算术（C 风格整型提升，`chr(s[i] - 'A' + 'a')`）/
  带标注时 `let v: Vec<i64> = [1, 2]` 直接当 Vec 字面量
- ✅ `if` / `match` 可以当表达式用：`let y = if c { 1 } else { 2 }`
  （分支类型自动统一，`return` / `panic` 结尾的分支算发散，结构体/枚举/数组结果也支持）
- ✅ 下标统一：`v[i]`、`m[k]`、`s[i]`、`a[i]` 都能读写，越界会报运行时错误
- ✅ 字符串既能按**字节**也能按**码点**处理：`s.char_len()` / `s.char_at(i)` /
  `s.codepoints()` / `s.slice_chars(a, b)`（中文不再需要自己解码）
- ✅ 容器字面量：`Vec<i64>[1, 2]`、`Map<str, i64>["a": 1]`、有类型标注时的空 `[]`
- ✅ 自动内存管理：编译期插入引用计数，作用域结束自动释放
  （实测 200 万次「建 Vec + 建 Map + 拼字符串」的循环，RSS 恒定在 ~0.9 MB）
- ✅ `new` / `free` 成对：`free(p)` 先把指向对象自己拥有的引用逐个还掉
  （结构体字段里的 `str` / `Vec` / 数组元素），再把内存还给 malloc；
  对 C 那边 `malloc` 出来的 `*u8` 同样可用
- ✅ `x.to_str()` 对任何类型都成立（结构体、枚举、数组、`bool`、`char`、指针都行），与 `str(x)` 等价
- ✅ 编译期诊断而不是运行时崩溃：内建方法的**参数个数**要查（`s.slice(1)`、`v.get(0,1,2)` 都是编译错误）；
  range 不是一等值（`print(0..3)` 会说明该怎么写）；`"{}"` 空插值、`'\u4F60'` 装不进 `char`、
  未知转义 `\q`、`s[0] = "x"` 改不可变字符串，都给人话报错（不再甩 Python traceback）
- ✅ 四条互操作链路：C、C++（自动生成 shim）、Python（含 numpy）、Java（JDK 类库）；
  `str` 与 `char*` 在 extern 边界上双向自动转换，按值收发结构体这类对不上的 ABI 会编译期报错
- ✅ 运行时安全网：段错误 / 栈溢出 / 除零 / 下标越界 / 空 Vec pop / assert 失败都有中文报错和非零退出码
- ✅ 性能（2 核 Xeon 2.60 GHz 虚拟机，gcc 12.2，实测见 [06_性能.md](docs/06_性能.md)）：

  | 基准 | FA | C -O2 | C -O2 -fno-inline | CPython | FA/C | FA/C 同算法 |
  |---|---|---|---|---|---|---|
  | fib(38) 递归 | 267 ms | 70 ms | 111 ms | 5439 ms | 3.82× | 2.40× |
  | 1 亿次算术循环 | 96 ms | 86 ms | 86 ms | 15463 ms | **1.11×** | 1.12× |
  | 200 万内素数筛 | 20 ms | 9.3 ms | 9.0 ms | 129 ms | 2.14× | 2.21× |

  比 CPython 快 **6.4~162 倍**。fib 的差距主要来自 gcc 把递归内联进自己并消掉公共子表达式；
  按「同算法」比（`-fno-inline`）是 2.40 倍。

---

## 7. 已知限制（还没做到的）

写代码前值得知道这些，省得踩坑：

| 还不支持 | 现在的替代写法 |
|---|---|
| 闭包 / 捕获环境的 lambda | 顶层函数 + 函数指针 `fn(i64) -> i64`；嵌套 `fn` 已支持但不捕获 |
| 嵌套函数捕获外层局部变量 | 把值当参数传进去，或用全局 / 结构体带状态（写了会给明确的编译期错误） |
| trait / interface / 泛型函数 | 每种类型各写一个 `impl`；容器用 `Vec<T>` / `Map<K,V>`（类型参数已支持嵌套） |
| 顶层 `let` 是结构体 / 枚举 | 标量 / `str` / `Vec` / `Map` / 数组都可以；值类型改用 `Vec<P>` / 指针 |
| `T?` 可选类型 | 只用于指针；其它类型请用枚举 `enum Opt: None / Some(v: T)` |
| 省略**没有默认值**的字段 | 给字段写默认值（`prio: i64 = 3`）就能省 |
| `let m = {}`（空 Map 字面量） | `{}` 已经是「空块」，没法两义；写 `Map<K, V>()` 或 `Map<K, V>[]` |
| 按码点索引的 `s[i]` | `s[i]` 永远是**字节**（char = u8）；按字符用 `s.char_at(i)` / `s.char_len()` |
| `s.bytes()` 当字节数组用 | 它是 `len()` 的历史别名（返回 `i64`）；要字节序列用 `s.chars()`（元素是 `char`），要码点用 `s.codepoints()` |
| range 当值用（存进变量、当参数传、`print(0..10)`） | 只在 `for` 的遍历位置有效；要一个整数序列用 `Vec<i64>`，或直接 `for i in 起..止` |
| range 的步进（`0..10..2`） | `for i in 0..10 { let j = i * 2 }`，或 `while` 自己加步长 |
| Python / Java 互操作 | 需要 python3-dev 头文件 / JDK；没有时这两个测试会 SKIP |

- 🚧 待办：自举（用 FA 重写编译器）、包管理器、公共子表达式消除、自动向量化、泛型函数、闭包
