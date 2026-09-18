/*
 * fa —— FA 编译器的启动器（单文件 C，Windows 上用 zig 交叉编成 fa.exe）
 *
 * 为什么是「启动器」而不是把 Python 塞进 exe：
 *   FA 的编译器、LSP、IDE 后端全是 Python 写的（约两万行），真正落地成机器码的
 *   是它生成的那些程序。把 CPython 整个打进 exe 要 30 MB 起步，还得为每个 Python
 *   小版本重打一次；而 Windows 上装 Python 是一条命令的事（winget install Python.
 *   Python.3.12）。所以这个 exe 只干一件事：**找到 FA 的安装目录、找到一个 Python，
 *   然后把参数原样交给 bin/fa_cli.py**，退出码也原样传回来。
 *
 * 查找顺序（FA 安装目录）：
 *   1. 环境变量 FA_HOME
 *   2. exe 同级目录（绿色版：fa.exe 和 lib/fa 放一起时是 exe\lib\fa）
 *   3. exe 上一级（安装版：bin\fa.exe + lib\fa）
 *   4. %LOCALAPPDATA%\Programs\fa\lib\fa
 *   5. C:\fa\lib\fa
 * 查找顺序（Python）：
 *   1. 环境变量 FA_PYTHON（可以指向 python-build-standalone 那种免安装版）
 *   2. py -3（Windows 官方的 Python launcher，多版本共存时最稳）
 *   3. python / python3（PATH 上）
 *
 * 这份文件同时能在 POSIX 上编（Linux/macOS 分支走 fork+exec）。这不是为了好看：
 * 它让 packaging/build_windows.py 可以**把同一套查找与转交逻辑在 Linux 上真跑一遍**，
 * 而不是「编出个 exe 就当它没问题」。Windows 分支只有 CreateProcess 那几行是独有的。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  define PATH_SEP '\\'
#  define PATH_SEPS "\\"
#  define IS_SEP(c) ((c) == '\\' || (c) == '/')
#else
#  include <unistd.h>
#  include <sys/wait.h>
#  include <libgen.h>
#  define PATH_SEP '/'
#  define PATH_SEPS "/"
#  define IS_SEP(c) ((c) == '/')
#endif

#define FA_MAX 4096

/* 判断 dir\lib\fa\bin\fa_cli.py 在不在。这是「找对地方了」的唯一凭据。 */
static int fa_root_ok(const char *root) {
    char p[FA_MAX];
    if (!root || !*root) return 0;
#ifdef _WIN32
    _snprintf(p, sizeof(p) - 1, "%s\\bin\\fa_cli.py", root);
#else
    snprintf(p, sizeof(p) - 1, "%s/bin/fa_cli.py", root);
#endif
    p[sizeof(p) - 1] = 0;
#ifdef _WIN32
    return GetFileAttributesA(p) != INVALID_FILE_ATTRIBUTES;
#else
    return access(p, R_OK) == 0;
#endif
}

/* 拼路径。截断了就返回 0 —— 调用方拿到的是「没找着」，不是半截路径。 */
static int join(char *out, size_t n, const char *a, const char *b) {
    int k;
#ifdef _WIN32
    k = _snprintf(out, n, "%s\\%s", a, b);
#else
    k = snprintf(out, n, "%s%c%s", a, PATH_SEP, b);
#endif
    if (k < 0 || (size_t)k >= n) { out[0] = 0; return 0; }
    return 1;
}

/* 取 exe 自身所在目录（解析掉符号链接，否则装成 /usr/bin/fa -> ../lib/... 就找歪了） */
static int exe_dir(char *out, size_t n) {
#ifdef _WIN32
    DWORD got = GetModuleFileNameA(NULL, out, (DWORD)n);
    if (got == 0 || got >= n) return 0;
    char *slash = strrchr(out, '\\');
    if (!slash) slash = strrchr(out, '/');
    if (!slash) return 0;
    *slash = 0;
    return 1;
#else
    ssize_t got = readlink("/proc/self/exe", out, n - 1);
    if (got <= 0) {
        /* 没有 /proc（少见）就退回 argv[0] 的目录 */
        return 0;
    }
    out[got] = 0;
    char *slash = strrchr(out, '/');
    if (!slash) return 0;
    *slash = 0;
    return 1;
#endif
}

static void up_one(char *path) {
    char *slash = strrchr(path, PATH_SEP);
    if (slash && slash != path) *slash = 0;
}

/* 找到 FA 安装目录；找到就写进 out 并返回 1 */
static int find_fa_root(char *out, size_t n) {
    const char *env = getenv("FA_HOME");
    if (env && *env) {
        /* FA_HOME 既可以直接指到含 bin/fa_cli.py 的目录，也可以指它的上一级 */
        if (fa_root_ok(env)) { snprintf(out, n, "%s", env); return 1; }
        char sub[FA_MAX];
        if (join(sub, sizeof(sub), env, "lib/fa") && fa_root_ok(sub)) {
            snprintf(out, n, "%s", sub);
            return 1;
        }
    }

    char dir[FA_MAX];
    if (exe_dir(dir, sizeof(dir))) {
        char cand[FA_MAX];
        if (join(cand, sizeof(cand), dir, "lib/fa") && fa_root_ok(cand)) {
            snprintf(out, n, "%s", cand);                  /* 绿色版 */
            return 1;
        }
        if (fa_root_ok(dir)) { snprintf(out, n, "%s", dir); return 1; }
        char parent[FA_MAX];
        snprintf(parent, sizeof(parent), "%s", dir);
        up_one(parent);
        if (join(cand, sizeof(cand), parent, "lib/fa") && fa_root_ok(cand)) {
            snprintf(out, n, "%s", cand);                  /* 安装版 bin/fa.exe */
            return 1;
        }
    }

#ifdef _WIN32
    const char *local = getenv("LOCALAPPDATA");
    if (local && *local) {
        char cand[FA_MAX];
        snprintf(cand, sizeof(cand), "%s\\Programs\\fa\\lib\\fa", local);
        if (fa_root_ok(cand)) { snprintf(out, n, "%s", cand); return 1; }
    }
    if (fa_root_ok("C:\\fa\\lib\\fa")) { snprintf(out, n, "%s", "C:\\fa\\lib\\fa"); return 1; }
#else
    if (fa_root_ok("/usr/local/lib/fa")) { snprintf(out, n, "%s", "/usr/local/lib/fa"); return 1; }
    if (fa_root_ok("/usr/lib/fa"))       { snprintf(out, n, "%s", "/usr/lib/fa");       return 1; }
#endif
    return 0;
}

static void fail_no_root(void) {
    fprintf(stderr,
        "\n找不到 FA 的安装目录（要能看到 <目录>/bin/fa_cli.py）。\n\n"
        "三种解法，任选其一：\n"
        "  1) 设环境变量 FA_HOME 指向安装目录，例如\n"
        "       set FA_HOME=C:\\Program Files\\fa\\lib\\fa     (cmd)\n"
        "       $env:FA_HOME='C:\\Program Files\\fa\\lib\\fa'  (PowerShell)\n"
        "  2) 把 fa.exe 和 lib\\fa 放在同一目录下（解压版就是这个布局）。\n"
        "  3) 用 deb/源码安装：仓库里的 ./bin/fa 开箱即用。\n\n");
}

static void fail_no_python(void) {
    fprintf(stderr,
        "\n找到了 FA，但没找到能用的 Python 3（需要 3.10 以上）。\n\n"
        "  winget install Python.Python.3.12      # 或者去 python.org 下载安装\n"
        "装完重开一个终端再试。要指定某个 Python，可以设 FA_PYTHON：\n"
        "  set FA_PYTHON=C:\\Python312\\python.exe\n\n");
}

/* ---- 转交执行 ---- */

static int hand_off(const char *fa_root, int argc, char **argv) {
    char cli[FA_MAX];
    if (!join(cli, sizeof(cli), fa_root, "bin" PATH_SEPS "fa_cli.py")) return 3;

#ifdef _WIN32
    /* 候选 Python：FA_PYTHON > py -3 > python > python3 */
    const char *cands[4];
    int ncand = 0;
    const char *envpy = getenv("FA_PYTHON");
    if (envpy && *envpy) cands[ncand++] = envpy;
    cands[ncand++] = "py";
    cands[ncand++] = "python";
    cands[ncand++] = "python3";

    /* 拼命令行。FA 的路径可能带空格，一律加引号。 */
    char cmdline[FA_MAX * 2];
    for (int i = 0; i < ncand; i++) {
        int off;
        if (strcmp(cands[i], "py") == 0)
            off = _snprintf(cmdline, sizeof(cmdline) - 1, "py -3 \"%s\"", cli);
        else
            off = _snprintf(cmdline, sizeof(cmdline) - 1, "\"%s\" \"%s\"", cands[i], cli);
        for (int a = 1; a < argc && off < (int)sizeof(cmdline) - 4; a++) {
            /* 简单可靠：一律加引号，内部引号按 Windows 规则转义 */
            off += _snprintf(cmdline + off, sizeof(cmdline) - off - 1, " \"");
            for (const char *p = argv[a]; *p && off < (int)sizeof(cmdline) - 8; p++) {
                if (*p == '"') { cmdline[off++] = '\\'; cmdline[off++] = '"'; }
                else cmdline[off++] = *p;
            }
            off += _snprintf(cmdline + off, sizeof(cmdline) - off - 1, "\"");
        }
        cmdline[off] = 0;

        STARTUPINFOA si;
        PROCESS_INFORMATION pi;
        ZeroMemory(&si, sizeof(si));
        si.cb = sizeof(si);
        ZeroMemory(&pi, sizeof(pi));
        /* 句柄继承打开，这样子进程的 stdout/stderr 直接是终端 */
        if (!CreateProcessA(NULL, cmdline, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
            DWORD e = GetLastError();
            if (e == ERROR_FILE_NOT_FOUND || e == ERROR_PATH_NOT_FOUND) continue;
            continue;   /* 换下一个候选 */
        }
        CloseHandle(pi.hThread);
        WaitForSingleObject(pi.hProcess, INFINITE);
        DWORD code = 1;
        GetExitCodeProcess(pi.hProcess, &code);
        CloseHandle(pi.hProcess);
        return (int)code;
    }
    fail_no_python();
    return 3;
#else
    const char *cands[3];
    int ncand = 0;
    const char *envpy = getenv("FA_PYTHON");
    if (envpy && *envpy) cands[ncand++] = envpy;
    cands[ncand++] = "python3";
    cands[ncand++] = "python";

    char **child = (char **)malloc(sizeof(char *) * (argc + 3));
    for (int i = 0; i < ncand; i++) {
        child[0] = (char *)cands[i];
        child[1] = cli;
        for (int a = 1; a < argc; a++) child[a + 1] = argv[a];
        child[argc + 1] = NULL;
        pid_t pid = fork();
        if (pid < 0) { perror("fork"); return 1; }
        if (pid == 0) {
            execvp(child[0], child);
            _exit(127);   /* 这个 Python 不存在，父进程换下一个 */
        }
        int st = 0;
        waitpid(pid, &st, 0);
        if (WIFEXITED(st) && WEXITSTATUS(st) != 127) return WEXITSTATUS(st);
        if (WIFSIGNALED(st)) return 128 + WTERMSIG(st);
    }
    free(child);
    fail_no_python();
    return 3;
#endif
}

int main(int argc, char **argv) {
    char root[FA_MAX];
    if (!find_fa_root(root, sizeof(root))) {
        fail_no_root();
        return 3;
    }
    return hand_off(root, argc, argv);
}
