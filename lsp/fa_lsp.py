#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FA 的 LSP 服务器（stdio / JSON-RPC 2.0）。

编辑器（VSCode、Neovim、Helix、Zed、Emacs…）只要会说 LSP，就能拿到：
    · 边打边报的错误和警告（含编译器看不出的那几类：少冒号、Tab 缩进、
      全角标点、&& / || / :=、match 里写 case、int/string 这类类型名）
    · 代码补全（成员按接收者类型给、类型位给类型名、use 后给模块名）
    · 悬停文档、签名提示、文档大纲、跳到定义

跑法：
    python3 lsp/fa_lsp.py            # 由编辑器拉起，别手工跑
    fa lsp                           # 同上，装好之后走 fa 的子命令
    python3 lsp/fa_lsp.py --selftest # 自己跟自己握手一遍，验证协议实现

语言分析全在 lsp/fa_lang.py 里，和网页版 IDE 共用同一套内核 ——
两边看到的诊断和补全必须一模一样，所以只准有一份实现。
"""

import json
import os
import sys
import threading
import time
import traceback
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (os.path.join(ROOT, "compiler"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fa_lang as F                                       # noqa: E402

LSP_VERSION = "1.0.0"

# ---------------------------------------------------------------- 协议常量
# FA 的三档 -> LSP 的 DiagnosticSeverity（1=Error 2=Warning 3=Info 4=Hint）
SEV_LSP = {F.SEV_ERROR: 1, F.SEV_WARN: 2, F.SEV_INFO: 3}

# DocumentSymbol / SymbolInformation 的 SymbolKind
SK_FILE = 1
SK_MODULE = 2
SK_CLASS = 5
SK_METHOD = 6
SK_PROPERTY = 7
SK_VARIABLE = 13
SK_CONSTANT = 14
SK_STRUCT = 23
SK_ENUM = 10

_KIND_MAP = {"函数": SK_METHOD, "结构体": SK_STRUCT, "枚举": SK_ENUM,
             "方法组": SK_CLASS, "方法": SK_METHOD, "常量": SK_CONSTANT,
             "全局": SK_VARIABLE, "导入": SK_MODULE}


def uri_to_path(uri):
    """file:///a/b.fa -> /a/b.fa；file:///C:/x.fa -> C:/x.fa。"""
    if not uri:
        return "<untitled>"
    p = urlparse(uri)
    if p.scheme != "file":
        return uri
    path = unquote(p.path)
    if os.name == "nt" and path.startswith("/"):
        path = path[1:]
    return path or "<untitled>"


def path_to_uri(path):
    if not path or path.startswith("<"):
        return "file:///" + pathname2url(os.path.abspath(path or "untitled.fa"))
    return "file://" + pathname2url(os.path.abspath(path))


# ---------------------------------------------------------------- 读写帧
class Reader:
    """按 LSP 的 Content-Length 分帧读 stdin。"""

    def __init__(self, stream):
        self.s = stream
        self._buf = b""

    def _readline(self):
        while b"\n" not in self._buf:
            chunk = self.s.read(4096)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.rstrip(b"\r")

    def read_message(self):
        length = None
        while True:
            line = self._readline()
            if line is None:
                return None                     # 对端关了
            if line == b"":
                break
            if line.lower().startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    length = None
        if length is None:
            return None
        while len(self._buf) < length:
            chunk = self.s.read(length - len(self._buf))
            if not chunk:
                return None
            self._buf += chunk
        body, self._buf = self._buf[:length], self._buf[length:]
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None


class Writer:
    def __init__(self, stream):
        self.s = stream
        self.lock = threading.Lock()

    def send(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        head = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self.lock:
            self.s.write(head + body)
            self.s.flush()


# ---------------------------------------------------------------- 服务器
class Server:
    def __init__(self, reader, writer, log=None):
        self.r = reader
        self.w = writer
        self.log = log
        self.docs = {}              # uri -> 源码
        self.paths = {}             # uri -> 文件路径
        self.cache = {}             # uri -> (源码, Analysis)
        self.shutdown = False
        self.root = None
        self.snippet_ok = True

    # ---- 出入口 ----
    def notify(self, method, params):
        self.w.send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid, result):
        self.w.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def error(self, rid, code, message):
        self.w.send({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": code, "message": message}})

    def _log(self, msg):
        if self.log:
            self.log(msg)
        self.notify("window/logMessage", {"type": 4, "message": f"[fa-lsp] {msg}"})

    # ---- 分析缓存：源码没变就不重跑 ----
    def analysis(self, uri, full=True):
        src = self.docs.get(uri, "")
        hit = self.cache.get(uri)
        if hit and hit[0] == src and hit[2] == full:
            return hit[1]
        t0 = time.time()
        a = F.analyze(src, self.paths.get(uri, uri_to_path(uri)), full)
        self.cache[uri] = (src, a, full)
        if self.log:
            self.log(f"analyze {os.path.basename(self.paths.get(uri, uri))} "
                     f"{'ok' if a.ok else a.stage} {(time.time()-t0)*1000:.0f}ms")
        return a

    def publish(self, uri):
        src = self.docs.get(uri, "")
        diags, a = F.check(src, self.paths.get(uri, uri_to_path(uri)))
        self.cache[uri] = (src, a, True)
        lines = src.split("\n")
        out = []
        for d in diags:
            ln = max(0, min(d.line - 1, len(lines) - 1))
            c0 = max(0, d.col - 1)
            c1 = max(c0 + 1, d.end_col - 1)
            out.append({
                "range": {"start": {"line": ln, "character": c0},
                          "end": {"line": ln, "character": c1}},
                "severity": SEV_LSP.get(d.severity, 1),
                "source": "fa",
                "code": d.stage or "fa",
                "message": d.message,
            })
        self.notify("textDocument/publishDiagnostics",
                    {"uri": uri, "diagnostics": out})

    # ---- 请求分派 ----
    def handle(self, msg):
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                return self.respond(rid, self.on_initialize(params))
            if method == "initialized":
                return None
            if method == "shutdown":
                self.shutdown = True
                return self.respond(rid, None)
            if method == "exit":
                return "EXIT"
            if method == "textDocument/didOpen":
                td = params.get("textDocument") or {}
                uri = td.get("uri", "")
                self.docs[uri] = td.get("text", "")
                self.paths[uri] = uri_to_path(uri)
                self.publish(uri)
                return None
            if method == "textDocument/didChange":
                uri = (params.get("textDocument") or {}).get("uri", "")
                # textDocumentSync=1（Full），客户端每次给全文
                for ch in (params.get("contentChanges") or []):
                    if "text" in ch:
                        self.docs[uri] = ch["text"]
                self.publish(uri)
                return None
            if method == "textDocument/didClose":
                uri = (params.get("textDocument") or {}).get("uri", "")
                self.docs.pop(uri, None)
                self.cache.pop(uri, None)
                self.notify("textDocument/publishDiagnostics",
                            {"uri": uri, "diagnostics": []})
                return None
            if method == "textDocument/didSave":
                uri = (params.get("textDocument") or {}).get("uri", "")
                if "text" in params:
                    self.docs[uri] = params["text"]
                self.publish(uri)
                return None
            if method == "textDocument/completion":
                return self.respond(rid, self.on_completion(params))
            if method == "completionItem/resolve":
                return self.respond(rid, params)
            if method == "textDocument/hover":
                return self.respond(rid, self.on_hover(params))
            if method == "textDocument/signatureHelp":
                return self.respond(rid, self.on_signature(params))
            if method == "textDocument/documentSymbol":
                return self.respond(rid, self.on_symbols(params))
            if method == "textDocument/definition":
                return self.respond(rid, self.on_definition(params))
            if method in ("$/cancelRequest", "workspace/didChangeConfiguration",
                          "workspace/didChangeWatchedFiles", "textDocument/didSave",
                          "setTrace", "$/setTrace"):
                return None
            if rid is not None:
                # 没实现的请求要回 MethodNotFound，不然编辑器会一直等
                return self.error(rid, -32601, f"FA 语言服务器不支持 {method}")
            return None
        except Exception as e:
            self._log(f"{method} 崩了：{type(e).__name__}: {e}\n"
                      f"{traceback.format_exc(limit=6)}")
            if rid is not None:
                return self.error(rid, -32603, f"{type(e).__name__}: {e}")
            return None

    # ---- initialize ----
    def on_initialize(self, params):
        caps = params.get("capabilities") or {}
        self.root = uri_to_path((params.get("rootUri") or "")) or params.get("rootPath")
        ci = ((caps.get("textDocument") or {}).get("completion") or {})
        self.snippet_ok = 2 in ((ci.get("completionItem") or {}).get("insertTextFormat") or [1])
        info = F.version_info() if hasattr(F, "version_info") else {}
        return {
            "capabilities": {
                "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": True}},
                "completionProvider": {
                    "triggerCharacters": [".", ":", "<", '"', "(", " "],
                    "resolveProvider": False,
                },
                "hoverProvider": True,
                "signatureHelpProvider": {"triggerCharacters": ["(", ",", "{", "["]},
                "documentSymbolProvider": True,
                "definitionProvider": True,
            },
            "serverInfo": {"name": "fa-lsp", "version": LSP_VERSION},
            "faInfo": info,
        }

    # ---- 位置换算：LSP 是 0 起的 (line, character)，fa_lang 是 1 起的 (行, 列) ----
    @staticmethod
    def _pos(params):
        td = params.get("textDocument") or {}
        p = params.get("position") or {}
        return td.get("uri", ""), int(p.get("line", 0)) + 1, int(p.get("character", 0)) + 1

    def on_completion(self, params):
        uri, line, col = self._pos(params)
        src = self.docs.get(uri, "")
        a = self.analysis(uri, full=False)
        r = F.complete(src, line, col, a=a)
        items = []
        for it in r["items"]:
            ins = it.get("insertText") or it["label"]
            snippet = "${" in ins or "$0" in ins
            entry = {
                "label": it["label"],
                "kind": it["kind"],
                "detail": (it.get("detail") or "").replace("**", ""),
                "insertText": ins,
                "insertTextFormat": 2 if (snippet and self.snippet_ok) else 1,
                "sortText": it.get("sortText") or it["label"],
                "filterText": it["label"],
            }
            if snippet and not self.snippet_ok:
                # 客户端不吃 snippet：把 ${1:xxx} 降级成 xxx
                import re as _re
                entry["insertText"] = _re.sub(r"\$\{\d+:([^}]*)\}", r"\1", ins)
                entry["insertText"] = _re.sub(r"\$\d+", "", entry["insertText"])
            items.append(entry)
        return {"isIncomplete": False, "items": items,
                "faContext": r.get("context", ""), "faWhy": r.get("why", "")}

    def on_hover(self, params):
        uri, line, col = self._pos(params)
        src = self.docs.get(uri, "")
        a = self.analysis(uri, full=False)
        md = F.hover(src, line, col, a=a)
        if not md:
            return None
        return {"contents": {"kind": "markdown", "value": md}}

    def on_signature(self, params):
        uri, line, col = self._pos(params)
        src = self.docs.get(uri, "")
        a = self.analysis(uri, full=False)
        s = F.signature(src, line, col, a=a)
        if not s:
            return None
        return {
            "signatures": [{
                "label": s["label"],
                "documentation": {"kind": "markdown", "value": s.get("doc", "")},
                "parameters": [{"label": p} for p in (s.get("params") or [])],
            }],
            "activeSignature": 0,
            "activeParameter": int(s.get("active") or 0),
        }

    def on_symbols(self, params):
        uri = (params.get("textDocument") or {}).get("uri", "")
        src = self.docs.get(uri, "")
        a = self.analysis(uri, full=False)
        lines = src.split("\n")
        out = []
        for s in F.symbols(src, a=a):
            ln = max(0, min(int(s.get("line") or 1) - 1, len(lines) - 1))
            end = len(lines[ln])
            rng = {"start": {"line": ln, "character": 0},
                   "end": {"line": ln, "character": end}}
            out.append({
                "name": s["name"],
                "detail": s.get("detail", ""),
                "kind": _KIND_MAP.get(s.get("kind", ""), SK_VARIABLE),
                "range": rng,
                "selectionRange": rng,
            })
        return out

    def on_definition(self, params):
        uri, line, col = self._pos(params)
        src = self.docs.get(uri, "")
        a = self.analysis(uri, full=False)
        loc = F.goto_definition(src, line, col, a=a)
        if not loc:
            return None
        path, tline, tcol = loc
        # 声明来自本文件时，goto_definition 给的 path 就是当前文件自己
        # （parse 打的 file 标记），这时别去读盘 —— 缓冲区里的版本才是最新的
        same = path and os.path.realpath(path) == os.path.realpath(self.paths.get(uri, ""))
        if path and not same:
            # 跳到 stdlib 或 `use "x.fa"` 带进来的模块：编辑器可能还没打开它，
            # 那就读一下盘上的内容，好算行宽（range 得落在真实文本上）
            turi = path_to_uri(path)
            if turi not in self.docs:
                try:
                    with open(path, encoding="utf-8") as fh:
                        self.docs[uri if False else turi] = fh.read()
                    self.paths[uri if False else turi] = path
                except OSError:
                    return None
            text = self.docs.get(turi, "")
        else:
            turi, text = uri, src
        lines = text.split("\n")
        if not lines:
            return None
        ln = max(0, min(int(tline or 1) - 1, len(lines) - 1))
        c0 = max(0, min(int(tcol or 1) - 1, len(lines[ln])))
        rng = {"start": {"line": ln, "character": c0},
               "end": {"line": ln, "character": len(lines[ln])}}
        return {"uri": turi, "range": rng}

    # ---- 主循环 ----
    def serve(self):
        while True:
            msg = self.r.read_message()
            if msg is None:
                return
            if self.handle(msg) == "EXIT":
                return


# ---------------------------------------------------------------- 自检
def _selftest():
    """自己跟自己走一遍协议：initialize -> didOpen -> 补全/悬停/大纲/签名 -> exit。

    不靠编辑器，直接拿管道喂 JSON-RPC，验证分帧、参数换算、返回结构都对。
    """
    import io

    demo = ('fn double(x: *i64) -> i64: return (*x) * 2\n'
            '\n'
            'fn main() -> i64:\n'
            '    let n = Vec<i64>[1, 2, 3]\n'
            '    let d = n.map(double)\n'
            '    print(d.to_str())\n'
            '    return 0\n')
    uri = path_to_uri(os.path.join(ROOT, "selftest.fa"))

    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"processId": os.getpid(), "rootUri": path_to_uri(ROOT),
                    "capabilities": {"textDocument": {"completion": {"completionItem":
                        {"insertTextFormat": [1, 2]}}}}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen",
         "params": {"textDocument": {"uri": uri, "languageId": "fa",
                                     "version": 1, "text": demo}}},
        # `n.` 之后（0 起：行 4，字符 14 = 紧跟在点号后面那一格）
        {"jsonrpc": "2.0", "id": 2, "method": "textDocument/completion",
         "params": {"textDocument": {"uri": uri},
                    "position": {"line": 4, "character": 14}}},
        {"jsonrpc": "2.0", "id": 3, "method": "textDocument/hover",
         "params": {"textDocument": {"uri": uri},
                    "position": {"line": 4, "character": 14}}},
        # 跳到定义：第 5 行的 print 上（内建，跳不了）和第 4 行的 double 上（能跳）
        {"jsonrpc": "2.0", "id": 8, "method": "textDocument/definition",
         "params": {"textDocument": {"uri": uri},
                    "position": {"line": 4, "character": 22}}},
        {"jsonrpc": "2.0", "id": 4, "method": "textDocument/documentSymbol",
         "params": {"textDocument": {"uri": uri}}},
        {"jsonrpc": "2.0", "id": 5, "method": "textDocument/signatureHelp",
         "params": {"textDocument": {"uri": uri},
                    "position": {"line": 4, "character": 20}}},
        # 改成一个有错的版本：少冒号 + 未定义变量
        {"jsonrpc": "2.0", "method": "textDocument/didChange",
         "params": {"textDocument": {"uri": uri, "version": 2},
                    "contentChanges": [{"text":
                        'fn main() -> i64\n    print(nope)\n    return 0\n'}]}},
        {"jsonrpc": "2.0", "id": 6, "method": "textDocument/completion",
         "params": {"textDocument": {"uri": uri},
                    "position": {"line": 0, "character": 5}}},
        {"jsonrpc": "2.0", "id": 7, "method": "shutdown", "params": {}},
        {"jsonrpc": "2.0", "method": "exit", "params": {}},
    ]
    raw = b""
    for m in msgs:
        b = json.dumps(m, ensure_ascii=False).encode("utf-8")
        raw += f"Content-Length: {len(b)}\r\n\r\n".encode("ascii") + b

    out = io.BytesIO()
    srv = Server(Reader(io.BytesIO(raw)), Writer(out))
    srv.serve()
    out.seek(0)
    got = []
    rd = Reader(out)
    while True:
        m = rd.read_message()
        if m is None:
            break
        got.append(m)

    by_id = {m.get("id"): m for m in got if "id" in m}
    diags = [m for m in got if m.get("method") == "textDocument/publishDiagnostics"]
    ok = True

    def ck(name, cond, detail=""):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + name + (f"　{detail}" if not cond and detail else ""))
        if not cond:
            ok = False

    print("[LSP 自检] 收到", len(got), "条消息")
    ck("initialize 回了能力", "result" in by_id.get(1, {})
       and "completionProvider" in by_id[1]["result"].get("capabilities", {}))
    ck("didOpen 推了诊断（合法文件 0 条）",
       len(diags) >= 1 and diags[0]["params"]["diagnostics"] == [],
       str(diags[0]["params"]["diagnostics"])[:120] if diags else "没收到")
    comp = by_id.get(2, {}).get("result") or {}
    labs = [i["label"] for i in comp.get("items", [])]
    ck("n. 补全给出 map/filter", "map" in labs and "filter" in labs, str(labs[:8]))
    ck("补全项带 kind 和 insertText",
       all("kind" in i and "insertText" in i for i in comp.get("items", [])))
    ck("上下文标成 member", comp.get("faContext") == "member", str(comp.get("faContext")))
    ck("成员项数是全量（不是前缀过滤后的两项）", len(labs) > 15, f"{len(labs)} 项")
    d8 = by_id.get(8, {}).get("result")
    ck("double 能跳到定义（第 1 行）",
       bool(d8) and d8["range"]["start"]["line"] == 0, str(d8))
    hov = by_id.get(3, {}).get("result")
    ck("悬停返回 markdown", bool(hov) and hov["contents"]["kind"] == "markdown",
       str(hov)[:80])
    syms = by_id.get(4, {}).get("result") or []
    ck("大纲给出 double 和 main",
       sorted(s["name"] for s in syms) == ["double", "main"], str(syms))
    ck("大纲项有 range 和 kind",
       all("range" in s and "kind" in s for s in syms))
    sig = by_id.get(5, {}).get("result")
    ck("签名提示给出 map(f)", bool(sig) and "map(" in sig["signatures"][0]["label"],
       str(sig)[:100])
    last = diags[-1]["params"]["diagnostics"] if diags else []
    ck("改坏的版本推了新诊断（>=2 条）", len(last) >= 2, str([d["message"][:30] for d in last]))
    ck("诊断里有「少了冒号」这条 lint",
       any("冒号" in d["message"] for d in last), str([d["message"][:24] for d in last]))
    ck("诊断的行列是 0 起且合法",
       all(d["range"]["start"]["line"] >= 0 and d["range"]["start"]["character"] >= 0
           for d in last))
    ck("shutdown 回了 null", by_id.get(7, {}).get("result", "x") is None)
    print("✓ LSP 自检全部通过" if ok else "✗ LSP 自检有失败项")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv or "--self-test" in argv:
        return _selftest()
    logf = None
    if "--log" in argv:
        path = argv[argv.index("--log") + 1] if len(argv) > argv.index("--log") + 1 \
            else os.path.join(ROOT, "fa-lsp.log")
        logf = open(path, "a", encoding="utf-8")

    def log(msg):
        if logf:
            logf.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            logf.flush()

    srv = Server(Reader(sys.stdin.buffer), Writer(sys.stdout.buffer), log)
    log(f"启动 pid={os.getpid()} argv={argv}")
    try:
        srv.serve()
    except KeyboardInterrupt:
        pass
    log("退出")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
