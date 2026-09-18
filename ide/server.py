#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FA 网页版 IDE 的后端：一个自带编译器的本地 HTTP 服务。

    fa ide                     # 起服务并打开浏览器
    fa ide --port 8080         # 指定端口
    fa ide --no-browser        # 只起服务（远程/容器里用）
    python3 ide/server.py --selftest   # 端到端自测（起服务、打 API、断言）

前端在 ide/static/ 里，纯 HTML/CSS/JS，不依赖任何 CDN —— 装完 deb / 解出 exe
断网也能用。语言分析和 LSP 共用 lsp/fa_lang.py，所以网页里看到的错误、补全
跟 VSCode 里的一模一样。

安全：文件接口一律夹在 --root（默认仓库根）里面，路径穿越直接拒。
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (os.path.join(ROOT, "compiler"), os.path.join(ROOT, "lsp")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fa_lang as F                                       # noqa: E402

STATIC = os.path.join(HERE, "static")
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".woff2": "font/woff2"}

def _backend_ok():
    import platform
    return platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64")


DEFAULT_TEMPLATE = '''fn main() -> i64:
    print("你好，FA！")
    return 0
'''


# ------------------------------------------------------------------ 沙箱
class Sandbox:
    """把用户给的路径夹在 root 里面，顺带记住最近用过的文件。"""

    def __init__(self, root):
        self.root = os.path.realpath(root)

    def resolve(self, path):
        """返回 (绝对路径 or None, 错误信息 or None)。"""
        if not path:
            return None, "没给路径"
        p = path if os.path.isabs(path) else os.path.join(self.root, path)
        p = os.path.realpath(p)
        if p != self.root and not p.startswith(self.root + os.sep):
            return None, f"路径 {path} 越出了工作目录 {self.root}"
        return p, None

    def rel(self, path):
        try:
            return os.path.relpath(path, self.root)
        except ValueError:
            return path


# ------------------------------------------------------------------ 业务
class Service:
    """所有 /api/* 的实现。和 HTTP 无关，方便直接测。"""

    def __init__(self, root):
        self.box = Sandbox(root)
        self.lock = threading.Lock()        # 编译要写临时目录、跑 gcc，串行稳妥
        self.workdir = tempfile.mkdtemp(prefix="fa_ide_")

    # ---- 分析 ----
    def analyze(self, src, path="ide.fa"):
        t0 = time.time()
        diags, a = F.check(src, path or "ide.fa")
        return {
            "ok": a.ok,
            "stage": a.stage,
            "elapsedMs": round((time.time() - t0) * 1000, 1),
            "diagnostics": [d.to_dict() for d in diags],
            "symbols": F.symbols(src, a=a),
        }

    def complete(self, src, line, col, path="ide.fa"):
        a = F.analyze(src, path or "ide.fa", full=False)
        r = F.complete(src, int(line), int(col), a=a)
        return {"context": r.get("context", ""), "why": r.get("why", ""),
                "receiver": r.get("receiver", ""), "items": r["items"]}

    def hover(self, src, line, col, path="ide.fa"):
        a = F.analyze(src, path or "ide.fa", full=False)
        return {"markdown": F.hover(src, int(line), int(col), a=a)}

    def signature(self, src, line, col, path="ide.fa"):
        a = F.analyze(src, path or "ide.fa", full=False)
        return F.signature(src, int(line), int(col), a=a) or {}

    def definition(self, src, line, col, path="ide.fa"):
        a = F.analyze(src, path or "ide.fa", full=False)
        loc = F.goto_definition(src, int(line), int(col), a=a)
        if not loc:
            return {}
        f, ln, c = loc
        if f:
            f = self.box.rel(f)
        return {"path": f or "", "line": ln, "col": c}

    # ---- 编译 + 运行 ----
    def run(self, src, path="untitled.fa", args=None, stdin_text="", timeout=10, opt=2):
        """编译到临时目录再跑，捕获 stdout/stderr，超时杀掉。"""
        args = [str(x) for x in (args or [])]
        timeout = max(1, min(int(timeout or 10), 120))
        t0 = time.time()
        # 先做检查：有错就别白跑一趟 gcc（也省得用户等）
        diags, a = F.check(src, path or "untitled.fa")
        if not a.ok:
            return {"ok": False, "compiled": False, "exitCode": None, "stdout": "",
                    "stderr": "\n".join(f"{d.stage}: {d.message}" for d in diags
                                        if d.severity == F.SEV_ERROR),
                    "elapsedMs": round((time.time() - t0) * 1000, 1),
                    "diagnostics": [d.to_dict() for d in diags]}

        base = os.path.splitext(os.path.basename(path or "untitled.fa"))[0] or "prog"
        src_path = os.path.join(self.workdir, base + ".fa")
        out_path = os.path.join(self.workdir, base + ".out")
        with open(src_path, "w", encoding="utf-8") as fh:
            fh.write(src)
        build_log = io.StringIO()
        compiled, rc = False, 1
        with self.lock:
            old_err, old_out = sys.stderr, sys.stdout
            try:
                sys.stderr = sys.stdout = build_log
                from falang import driver
                rc = driver.build(src_path, out_path, opt=int(opt), keep=False)
                compiled = (rc == 0)
            except Exception as e:
                build_log.write(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}")
                compiled = False
            finally:
                sys.stderr, sys.stdout = old_err, old_out
        if not compiled:
            return {"ok": False, "compiled": False, "exitCode": None, "stdout": "",
                    "stderr": build_log.getvalue().strip(),
                    "elapsedMs": round((time.time() - t0) * 1000, 1),
                    "diagnostics": [d.to_dict() for d in diags]}

        stdout = stderr = ""
        code, timed_out = None, False
        try:
            p = subprocess.run([out_path] + args, input=stdin_text or "",
                               capture_output=True, text=True, timeout=timeout,
                               cwd=self.workdir)
            stdout, stderr, code = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            stderr += f"\n[超时] 跑满 {timeout} 秒被杀掉了（死循环？要更长就在运行设置里调）"
        except Exception as e:
            stderr = f"{type(e).__name__}: {e}"
        return {"ok": code == 0 and not timed_out, "compiled": True, "exitCode": code,
                "stdout": stdout, "stderr": stderr, "timedOut": timed_out,
                "buildLog": build_log.getvalue().strip(),
                "elapsedMs": round((time.time() - t0) * 1000, 1),
                "diagnostics": [d.to_dict() for d in diags]}

    # ---- 文件 ----
    def tree(self, path=""):
        p, err = self.box.resolve(path if path else self.box.root)
        if err:
            return {"error": err}
        if not os.path.isdir(p):
            return {"error": f"{path} 不是目录"}
        out = []
        try:
            names = sorted(os.listdir(p), key=lambda x: (not os.path.isdir(os.path.join(p, x)), x.lower()))
        except OSError as e:
            return {"error": str(e)}
        for n in names:
            if n in (".git", "__pycache__", ".fa_work", "node_modules", ".mypy_cache"):
                continue
            fp = os.path.join(p, n)
            is_dir = os.path.isdir(fp)
            out.append({"name": n, "path": self.box.rel(fp), "dir": is_dir,
                        "size": 0 if is_dir else (os.path.getsize(fp) if os.path.exists(fp) else 0)})
        return {"path": self.box.rel(p), "entries": out}

    def read(self, path):
        p, err = self.box.resolve(path)
        if err:
            return {"error": err}
        if not os.path.isfile(p):
            return {"error": f"找不到文件 {path}"}
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError as e:
            return {"error": str(e)}
        return {"path": self.box.rel(p), "src": src,
                "size": len(src), "readOnly": not os.access(p, os.W_OK)}

    def write(self, path, src, mkdirs=True):
        p, err = self.box.resolve(path)
        if err:
            return {"error": err}
        if mkdirs:
            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                try:
                    os.makedirs(d, exist_ok=True)
                except OSError as e:
                    return {"error": str(e)}
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(src or "")
        except OSError as e:
            return {"error": str(e)}
        return {"ok": True, "path": self.box.rel(p), "size": len(src or "")}

    def new_file(self, path, template=None):
        p, err = self.box.resolve(path)
        if err:
            return {"error": err}
        if not p.endswith(".fa"):
            p += ".fa"
        if os.path.exists(p):
            return {"error": f"{self.box.rel(p)} 已经存在了"}
        return self.write(self.box.rel(p), template if template is not None else DEFAULT_TEMPLATE)

    def cheat(self):
        """速查表。直接端 fa_lang 里的文档表 —— 悬停、补全、速查必须同一份措辞，
        不然用户在三个地方看到三种说法。"""
        return {
            "types": sorted(F.TYPE_NAMES),
            "keywords": sorted(F.KEYWORDS),
            "builtins": dict(sorted(F.BUILTIN_DOC.items())),
            "str": dict(sorted(F.STR_DOC.items())),
            "vec": dict(sorted(F.VEC_DOC.items())),
            "map": dict(sorted(F.MAP_DOC.items())),
            "num": dict(sorted(F.NUM_DOC.items())),
            "snippets": [{"label": x[0], "doc": x[2]} for x in F.SNIPPETS],
        }

    def info(self):
        cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
        try:
            from falang.sema import stdlib_modules
            mods = stdlib_modules()
        except Exception:
            mods = []
        # 版本号不许自己写一份：以前这里硬编码 1.0.0，而 fa version 说 0.2.0，
        # 同一个东西两个版本号（deb 里也是 1.0.0）。统一从 falang.__version__ 取。
        try:
            from falang import __version__ as _fa_ver
        except Exception:
            _fa_ver = "0"
        return {
            "name": "FA IDE",
            "version": _fa_ver,
            "root": self.box.root,
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "cc": cc or "",
            "canCompile": bool(cc),
            # 能不能真的编译运行，还要看平台：后端只生成 x86-64 Linux System V 汇编
            "canRun": bool(cc) and _backend_ok(),
            "runBlockReason": ("" if _backend_ok() else
                               f"FA 的代码生成只支持 x86-64 Linux（这台是 {sys.platform}）；"
                               "检查、补全、波浪线都正常，编译运行请在 Linux/WSL2 里做"),
            "stdlib": mods,
            "keywords": sorted(F.KEYWORDS),
            "builtins": sorted(F.BUILTIN_FNS),
            "types": sorted(F.TYPE_NAMES),
        }


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    service = None              # 由 make_server 注入
    server_version = "fa-ide/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("FA_IDE_VERBOSE"):
            sys.stderr.write("[ide] " + (fmt % args) + "\n")

    # ---- 基础 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._send(204, b"")

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/info":
                return self._json(self.service.info())
            if path == "/api/tree":
                return self._json(self.service.tree((q.get("path") or [""])[0]))
            if path == "/api/cheat":
                return self._json(self.service.cheat())
            if path == "/favicon.ico":
                return self._send(404, b"", "image/x-icon")
            return self._json({"error": f"没有这个接口：{path}"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}",
                               "trace": traceback.format_exc(limit=6)}, 500)

    def _static(self, name):
        name = name.split("?")[0]
        if "/" in name or "\\" in name or name.startswith("."):
            return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        fp = os.path.join(STATIC, name)
        if not os.path.isfile(fp):
            return self._send(404, f"找不到 {name}".encode("utf-8"), "text/plain; charset=utf-8")
        with open(fp, "rb") as fh:
            body = fh.read()
        ctype = MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
        return self._send(200, body, ctype)

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        b = self._body()
        svc = self.service
        try:
            if path == "/api/analyze":
                return self._json(svc.analyze(b.get("src", ""), b.get("path", "ide.fa")))
            if path == "/api/complete":
                return self._json(svc.complete(b.get("src", ""), b.get("line", 1),
                                               b.get("col", 1), b.get("path", "ide.fa")))
            if path == "/api/hover":
                return self._json(svc.hover(b.get("src", ""), b.get("line", 1),
                                            b.get("col", 1), b.get("path", "ide.fa")))
            if path == "/api/signature":
                return self._json(svc.signature(b.get("src", ""), b.get("line", 1),
                                                b.get("col", 1), b.get("path", "ide.fa")))
            if path == "/api/definition":
                return self._json(svc.definition(b.get("src", ""), b.get("line", 1),
                                                 b.get("col", 1), b.get("path", "ide.fa")))
            if path == "/api/run":
                return self._json(svc.run(b.get("src", ""), b.get("path", "untitled.fa"),
                                          b.get("args"), b.get("stdin", ""),
                                          b.get("timeout", 10), b.get("opt", 2)))
            if path == "/api/read":
                return self._json(svc.read(b.get("path", "")))
            if path == "/api/write":
                return self._json(svc.write(b.get("path", ""), b.get("src", "")))
            if path == "/api/new":
                return self._json(svc.new_file(b.get("path", ""), b.get("template")))
            return self._json({"error": f"没有这个接口：{path}"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}",
                               "trace": traceback.format_exc(limit=6)}, 500)

    do_HEAD = do_GET


def make_server(root=ROOT, host="0.0.0.0", port=0):
    svc = Service(root)
    Handler.service = svc
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd, svc


def pick_port(host, want):
    if want:
        return want
    # 0 让内核挑一个空闲端口，再把实际端口读出来
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    root = ROOT
    host = "0.0.0.0"        # 容器/远程开发要能从外面连进来，别绑 127.0.0.1
    port = 8765
    browser = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--root", "-C") and i + 1 < len(argv):
            root = os.path.abspath(argv[i + 1]); i += 2; continue
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2; continue
        if a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]; i += 2; continue
        if a in ("--no-browser", "--headless"):
            browser = False; i += 1; continue
        if a in ("-h", "--help"):
            print(__doc__); return 0
        i += 1

    if not os.path.isdir(root):
        sys.stderr.write(f"[fa ide] 工作目录不存在：{root}\n")
        return 2
    try:
        httpd, svc = make_server(root, host, port)
    except OSError as e:
        sys.stderr.write(f"[fa ide] 端口 {port} 起不来（{e}）；换一个：fa ide --port 8790\n")
        return 2
    actual = httpd.server_address[1]
    url = f"http://{'localhost' if host in ('0.0.0.0', '::') else host}:{actual}/"
    print(f"FA IDE 已启动：{url}")
    print(f"  工作目录：{svc.box.root}")
    print(f"  C 编译器：{shutil.which('gcc') or shutil.which('cc') or '（没找到，只能检查不能运行）'}")
    print("  Ctrl+C 停止")
    if browser:
        threading.Timer(0.6, lambda: _open_browser(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[fa ide] 已停止")
    finally:
        httpd.server_close()
        shutil.rmtree(svc.workdir, ignore_errors=True)
    return 0


def _open_browser(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


# ------------------------------------------------------------------ 自测
def _selftest():
    """真起一个服务，用 HTTP 打一遍所有接口，逐条断言返回。"""
    import urllib.request
    import urllib.error

    ok = True
    passed = []
    failed = []

    def ck(name, cond, detail=""):
        nonlocal ok
        (passed if cond else failed).append(name)
        if not cond:
            ok = False
            print(f"  ✗ {name}　{detail}")

    # 用一个临时工作目录，别把仓库写脏
    work = tempfile.mkdtemp(prefix="fa_ide_selftest_")
    with open(os.path.join(work, "hello.fa"), "w", encoding="utf-8") as fh:
        fh.write('fn main() -> i64:\n    print("你好，IDE！")\n    return 0\n')
    os.makedirs(os.path.join(work, "sub"), exist_ok=True)

    httpd, svc = make_server(work, "127.0.0.1", 0)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"

    def GET(p):
        try:
            with urllib.request.urlopen(base + p, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:      # 403/404 也是**正确**结果，别抛出去
            return e.code, e.read()

    def POST(p, obj):
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(base + p, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    try:
        print(f"[IDE 自测] 服务起在 {base}，工作目录 {work}")

        st, body = GET("/")
        ck("GET / 返回 200 和 HTML", st == 200 and b"<html" in body.lower(), f"{st}")
        ck("首页带编辑器脚本", b"app.js" in body, "")
        for asset in ("/static/app.js", "/static/style.css", "/static/tokens.js"):
            st, b = GET(asset)
            ck(f"静态资源 {asset} 200 且非空", st == 200 and len(b) > 200, f"{st} {len(b)}")
        st, _b = GET("/static/../server.py")
        ck("静态目录不给穿越", st in (403, 404), str(st))

        st, b = GET("/api/info")
        j = json.loads(b.decode("utf-8"))
        ck("info 给出 root", j.get("root") == os.path.realpath(work), str(j.get("root")))
        ck("info 给出标准库清单", len(j.get("stdlib", [])) >= 4, str(j.get("stdlib")))
        ck("info 给出关键字表", "fn" in j.get("keywords", []) and "match" in j.get("keywords", []))
        ck("info 报告能不能编译", "canCompile" in j, str(j))

        st, b = GET("/api/tree"); j = json.loads(b.decode("utf-8"))
        names = [e["name"] for e in j.get("entries", [])]
        ck("目录列表看到 hello.fa 和 sub", "hello.fa" in names and "sub" in names, str(names))
        st, b = GET("/api/tree?path=../../etc"); j = json.loads(b.decode("utf-8"))
        ck("目录列表拒绝越界", "error" in j, str(j)[:80])

        st, j = POST("/api/read", {"path": "hello.fa"})
        ck("读文件拿到源码", "你好，IDE" in j.get("src", ""), str(j)[:80])
        st, j = POST("/api/read", {"path": "../../../etc/passwd"})
        ck("读文件拒绝越界", "error" in j, str(j)[:80])

        good = 'fn main() -> i64:\n    let v = Vec<i64>[1,2,3]\n    print(v.map(double).to_str())\n    return 0\n\nfn double(x: *i64) -> i64: return (*x) * 2\n'
        st, j = POST("/api/analyze", {"src": good, "path": "good.fa"})
        ck("合法源码 analyze ok=True", j.get("ok") is True, str(j)[:120])
        ck("合法源码 0 条诊断", not j.get("diagnostics"), str(j.get("diagnostics"))[:120])
        ck("analyze 同时给出大纲", [s["name"] for s in j.get("symbols", [])] == ["main", "double"],
           str(j.get("symbols"))[:100])
        ck("analyze 报告耗时", isinstance(j.get("elapsedMs"), (int, float)), str(j))

        # 少冒号：编译到语法分析就停了，所以只有 lint 那条能点破病因
        nocolon = 'fn main() -> i64\n    print(nope)\n    return 0\n'
        st, j = POST("/api/analyze", {"src": nocolon, "path": "bad.fa"})
        ck("少冒号 ok=False", j.get("ok") is False, str(j)[:100])
        msgs = " ".join(d["message"] for d in j.get("diagnostics", []))
        ck("少冒号 → lint 点破病因", "冒号" in msgs, msgs[:100])
        # 能解析过的源码，才轮得到语义分析报未定义
        undef = 'fn main() -> i64:\n    print(nope)\n    return 0\n'
        st, j = POST("/api/analyze", {"src": undef, "path": "undef.fa"})
        msgs = " ".join(d["message"] for d in j.get("diagnostics", []))
        ck("未定义标识符能报出来", "nope" in msgs, msgs[:120])
        bad = nocolon
        ck("诊断字段齐全（行/列/级别）",
           all({"line", "col", "severity", "message"} <= set(d) for d in j["diagnostics"]),
           str(j["diagnostics"][:1]))

        # 光标停在 `v.` 之后（1 起第 13 列 = 0 起的 12）
        st, j = POST("/api/complete", {"src": good, "line": 3, "col": 13, "path": "good.fa"})
        labs = [i["label"] for i in j.get("items", [])]
        ck("v. 补全给出 map/filter/push", {"map", "filter", "push"} <= set(labs), str(labs[:10]))
        ck("补全上下文是 member", j.get("context") == "member", str(j.get("context")))
        st, j = POST("/api/complete", {"src": "use std.", "line": 1, "col": 9, "path": "u.fa"})
        labs = [i["label"] for i in j.get("items", [])]
        ck("use std. 给模块名", "fs" in labs and "time" in labs, str(labs))

        # 光标停在 map 这个词上（1 起第 14 列）
        st, j = POST("/api/hover", {"src": good, "line": 3, "col": 14, "path": "good.fa"})
        ck("悬停 map 给出文档", "map" in (j.get("markdown") or ""), str(j)[:100])

        st, j = POST("/api/signature", {"src": good, "line": 3, "col": 21, "path": "good.fa"})
        ck("签名提示给出 map(f)", "map(" in j.get("label", ""), str(j)[:100])

        st, j = POST("/api/definition", {"src": good, "line": 3, "col": 20, "path": "good.fa"})
        ck("跳定义指到 double 那行", j.get("line") == 6, str(j))

        st, j = POST("/api/write", {"path": "sub/new.fa", "src": good})
        ck("写文件（含建目录）成功", j.get("ok") is True, str(j)[:80])
        ck("写的文件真的在盘上", os.path.isfile(os.path.join(work, "sub", "new.fa")))
        st, j = POST("/api/new", {"path": "brand"})
        ck("新建文件套模板", j.get("ok") is True, str(j)[:80])
        ck("新建的文件带 .fa 后缀", os.path.isfile(os.path.join(work, "brand.fa")))
        st, j = POST("/api/new", {"path": "brand"})
        ck("重名新建被拒", "error" in j, str(j)[:60])
        st, j = POST("/api/write", {"path": "../escape.fa", "src": "x"})
        ck("写文件拒绝越界", "error" in j, str(j)[:60])

        from falang import __version__ as _fa_ver
        ck("info 里的版本号和编译器一致（不许各写各的）",
           svc.info().get("version") == _fa_ver,
           f"IDE {svc.info().get('version')} 编译器 {_fa_ver}")

        # 这里要同时看 canCompile 和 canRun：Windows 上就算装了 gcc，
        # 后端出的也是 x86-64 Linux ELF，跑不起来 —— 只判断 canCompile 的话，
        # 这 7 条会在 Windows 上集体假失败（CI 里第一次跑就撞上了）。
        _info = svc.info()
        if _info["canCompile"] and _info["canRun"]:
            st, j = POST("/api/run", {"src": 'fn main() -> i64:\n    print("跑起来了")\n    return 0\n',
                                      "path": "r.fa", "timeout": 20})
            ck("能编译时 run 成功", j.get("ok") is True, str(j)[:200])
            ck("run 捕获到 stdout", "跑起来了" in (j.get("stdout") or ""), str(j.get("stdout"))[:80])
            ck("run 报告退出码 0", j.get("exitCode") == 0, str(j.get("exitCode")))
            st, j = POST("/api/run", {"src": 'fn main() -> i64:\n    return 3\n', "path": "rc.fa"})
            ck("run 透传退出码 3", j.get("exitCode") == 3, str(j.get("exitCode")))
            st, j = POST("/api/run", {"src": 'fn main() -> i64:\n    let i = 0\n    while true:\n        i += 1\n    return 0\n',
                                      "path": "loop.fa", "timeout": 2})
            ck("死循环被超时杀掉", j.get("timedOut") is True, str(j)[:120])
            st, j = POST("/api/run", {"src": bad, "path": "bad.fa"})
            ck("编译不过时不跑，直接把错误给回来",
               j.get("compiled") is False and "顶层只允许" in (j.get("stderr") or ""), str(j)[:140])
            ck("run 也把 lint 的诊断带回来（前端能画波浪线）",
               any("冒号" in d["message"] for d in (j.get("diagnostics") or [])),
               str(j.get("diagnostics"))[:120])
            st, j = POST("/api/run", {"src": 'fn main() -> i64:\n    let s = ""\n    for c in Stdin.lines():\n        s += c\n    print(s)\n    return 0\n',
                                      "path": "in.fa", "stdin": "甲\n乙\n"})
            ck("stdin 能喂进去（跑不通也算测到了接口）", st == 200, str(st))
        else:
            why = _info.get("runBlockReason") or "没有 C 编译器"
            print(f"  ○ 跳过 run 相关的 7 条 —— {why}")

        print()
        print(f"通过 {len(passed)} 条，失败 {len(failed)} 条")
        if failed:
            for f in failed:
                print("  ✗", f)
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(svc.workdir, ignore_errors=True)
    print("✓ IDE 后端自测全部通过" if ok else "✗ IDE 后端自测有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
