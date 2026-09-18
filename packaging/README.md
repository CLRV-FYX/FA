# 打包：把 FA 变成能装的东西

一条命令打完三种交付物，打之前先把测试跑绿，打完每个包**自己验一遍**：

```bash
./packaging/build_all.sh
```

产物在 `dist/`（已 gitignore，不入库）：

| 文件 | 给谁 | 装法 |
|---|---|---|
| `fa_<版本>_amd64.deb` | Linux / Debian / Ubuntu / **WSL** | `sudo apt install ./fa_0.2.0_amd64.deb` |
| `fa-<版本>-windows-amd64.zip` | Windows | 解压即用（先 `winget install Python.Python.3.12`） |
| `fa-vscode-<版本>.vsix` | VSCode / VSCodium / Cursor | `code --install-extension dist/fa-vscode-0.2.0.vsix` |

版本号只有一个来源：`compiler/falang/__init__.py` 里的 `__version__`。
deb、zip、vsix、`fa version`、扩展的 `package.json` 全都从它派生
（`packaging/version.py` 负责这件事，扩展自测里有一条盯着它们不许漂移）。

---

## 装完能干什么

| 命令 | Linux | Windows |
|---|---|---|
| `fa check a.fa`　词法/语法/类型检查，逐行错误定位 | ✅ | ✅ |
| `fa ide`　网页 IDE：补全、波浪线、悬停、大纲、跳转、运行 | ✅ | ✅（运行按钮自动置灰） |
| `fa lsp`　给 VSCode / Neovim / Emacs 的语言服务 | ✅ | ✅ |
| `fa tokens` / `fa ast` / `fa bind`（读 C 头文件出 FA 声明） | ✅ | ✅ |
| `fa selftest`　四套编译器自测 | ✅ | ✅ |
| `fa build` / `fa run` / `fa asm`　出 x86-64 Linux ELF | ✅ | ❌ 见下 |

**Windows 上为什么不能 build/run**：代码生成后端目前只出 x86-64 Linux System V 的 ELF，
链接 gcc 和 Linux 系统调用。所以在 Windows 上这三条命令会直接说清原因并返回退出码 3
（`compiler/falang/cli.py` 里的 `_backend_platform_ok()`），而不是生成一个跑不起来的东西。
IDE 后端也会把这件事报给前端（`/api/info` 里的 `canRun` / `runBlockReason`），
前端据此把「运行」按钮置灰并把原因写在按钮上。

要在 Windows 上真跑出程序，用 WSL：`wsl --install -d Debian`，进去 `sudo apt install ./fa_*.deb`。
同一份 `.fa` 源码，Windows 侧写代码 + 检查 + IDE，WSL 侧编译运行。

---

## deb 里装了什么

```
/usr/bin/fa                 → 转交给 /usr/lib/fa/bin/fa_cli.py
/usr/bin/fa-ide             → fa ide（起网页 IDE）
/usr/bin/fa-lsp             → fa lsp（给编辑器当语言服务）
/usr/lib/fa/                编译器 / LSP / IDE / 标准库 / C 运行时 / 入口
/usr/share/fa/docs/         语言参考、标准库、完全教程
/usr/share/applications/    桌面菜单项
```

依赖只有 `python3 (>= 3.10)`；`gcc`、`g++` 是 Recommends（没有它们 check/ide/lsp 照样能用，
只是 build/run 不行）。

单独打：`python3 packaging/build_deb.py`（`--version` 可覆盖版本号，`--output` 换目录）。

## Windows 包里有什么

```
fa.exe                  启动器：找 FA 安装目录 → 找 Python → 原样转交参数与退出码
lib/fa/                 和 deb 里同一套载荷
fa.bat                  不想用 exe 时的等价批处理
README-Windows.md       装 Python、加 PATH、WSL 的说明
editors/fa-vscode-*.vsix  扩展（如果先打了 vsix 就会带进来）
```

`fa.exe` 用 zig 交叉编译（`pip install --user ziglang`），不需要 Windows 机器也不需要 MSVC。
它是**启动器**而不是把 CPython 塞进去的胖 exe：FA 的编译器/LSP/IDE 是 Python 写的，
打进 exe 要 30 MB 起步，还得为每个 Python 小版本重打一次。
查找顺序写在 `packaging/launcher/fa_launcher.c` 的文件头注释里
（FA 目录：`FA_HOME` → exe 同级 `lib/fa` → 上一级 `lib/fa` → `%LOCALAPPDATA%\Programs\fa` → `C:\fa`；
Python：`FA_PYTHON` → `py -3` → `python` → `python3`）。

单独打：`python3 packaging/build_windows.py`（`--no-zig` 只跑启动器自测，不交叉编译）。

## VSCode 扩展

零第三方依赖，语法词表和代码片段都是**生成**出来的，不是手写的：

```bash
python3 editors/vscode-fa/gen_grammar.py          # 重新生成
python3 editors/vscode-fa/gen_grammar.py --check  # CI 用：仓库里那份是不是旧的
```

`--check` 会从编译器的 `KEYWORDS`/`TYPES`/`BUILTIN_FNS` 和 `lsp/fa_lang.py` 的 `SNIPPETS`
重新生成一遍再和仓库里的比。加了语言特性却忘了重跑生成器，语法高亮会悄悄漏词，
肉眼看不出来 —— 所以让 CI 盯着。

单独打：`python3 packaging/build_vsix.py`。

---

## 每个包是怎么验的

**这里最容易骗自己**：「打出来了」不等于「能用」。exe 在 Linux 上根本跑不了，
deb 装不进沙箱，vsix 要 VSCode 才认。所以每个脚本都在能力范围内做到真验证：

**deb（36 条）** — `dpkg-deb -x` 解到临时前缀，用干净环境（去掉 `FA_HOME`/`PYTHONPATH`）跑：
`fa version`、`fa --help`、`fa check`、`fa run`（含标准库与高阶函数）、`fa asm`、
`fa lsp --selftest`、`fa ide --selftest`，并和仓库里直接跑的输出**逐字节比对**；
另外检查 control 文件、目录权限、没把 `.git`/`__pycache__` 打进去、
`runtime/fa_syscall.S` 这类手写源码没被当成中间产物筛掉。

**Windows（39 条）** — 先用本机 C 编译器把**同一份 `fa_launcher.c`** 编出来真跑一遍：
三种安装布局（绿色版 exe 同级 `lib/fa`、安装版 `bin/fa` + `../lib/fa`、`FA_HOME` 指定）、
经启动器真编译真运行一个程序、类型错误照样报、程序自己的退出码原样透传（`return 7` → rc=7）、
找不到目录时给人话且 rc=3。然后 zig 交叉编译出 `fa.exe`，逐字段校验 PE 头
（MZ、`PE\0\0`、machine=0x8664、PE32+ magic=0x20B、子系统=3 控制台）。
最后解开 zip，用**同布局的启动器**跑里面那份载荷，确认 zip 里的东西真能用。

**vsix（25 条）** — 解包检查 `[Content_Types].xml` 与 `extension.vsixmanifest`、
manifest 版本号和 `package.json` 一致、`contributes` 里声明的每个文件都在包里、
每个 JSON 都能解析、`test/` 没被打进去、tmLanguage 的词表覆盖编译器全部关键字与类型名。

真正的 Windows 实跑交给 CI：`packaging/ci/release.yml` 里的 `windows-smoke` 作业
把 `fa.exe` 放到真的 `windows-latest` 上跑 `version` / `check` / `lsp --selftest` /
`ide --selftest`，并断言 `run` 被后端守卫拦下（rc=3 且报错里提到 Linux/WSL）。

### CI 定义放在 packaging/ci/，不在 .github/workflows/

GitHub App 令牌默认没有 `workflows` 权限，往 `.github/workflows/` 写文件会让**整个 push 被拒**：

    ! [remote rejected] (refusing to allow a GitHub App to create or update
      workflow `.github/workflows/release.yml` without `workflows` permission)

一个文件挡住整批提交不值得，所以流水线定义随仓库走，启用是一条命令：

```bash
./packaging/ci/install-ci.sh      # 拷到 .github/workflows/ 并当场校验 YAML 与作业结构
git add .github && git commit -m "启用 CI" && git push
```

push 若仍被拒，就在仓库 Settings → GitHub Apps 里给这个 App 勾上 Workflows 写权限，
或者用个人令牌 push 这一次。

---

## 踩过的坑（都变成了断言）

- **deb 包装好了跑不起来，stderr 还是空的**：包装脚本写的是 `BIN/../../lib/fa`，
  可从 `/usr/bin` 到 `/usr/lib/fa` 只差一级。`sh` 直接 127 退出，一个字都不吐，
  排查了半天才发现是路径算错。现在 deb 自测里有一条专门跑 `fa version`。
- **`runtime/fa_syscall.S` 被当成中间产物筛掉了**：筛选规则按扩展名一刀切跳过了 `.s`，
  可那是手写的汇编**源文件**。装好的包一 build 就报「运行时编译失败」。
  现在 `runtime/` 只按目录名筛，并且自测里点名检查这几个源文件在不在包里。
- **断言的期望值是凭印象写的**：样本里 `Vec<i64>[1,2,3,4]` 过滤「成年人」当然返回空，
  我却断言 `[3, 4]`。挂的是断言不是包。现在期望值一律先把源码真跑一遍抄下来。
- **版本号有四个来源**：`__init__.py` 0.2.0、`cli.py` 里又硬编码一遍、
  deb 默认 1.0.0、扩展 package.json 1.0.0。`fa version` 说 0.2.0，装出来的包叫 1.0.0，
  用户根本没法报 bug。现在只有 `packaging/version.py` 一个来源。
- **IDE 自测在 Windows 上会集体假失败**：只判断了 `canCompile`，可 Windows 上就算有 gcc，
  后端出的也是 Linux ELF。现在同时判断 `canRun`，跳过时把原因打出来。
