/* FA 的 VSCode 扩展。
 *
 * 刻意**零依赖**：不用 vscode-languageclient（那要 npm install，装完还得带
 * 一个 node_modules），自己把 LSP 的 Content-Length 分帧和 JSON-RPC 写了 ——
 * 一共百来行，换来的是 .vsix 只有几十 KB、离线能装、跨平台不会卡在依赖上。
 *
 * 语言智能全部来自 `lsp/fa_lsp.py`（和网页版 IDE 同一个内核 fa_lang.py），
 * 所以 VSCode 里看到的错误、补全、悬停跟网页 IDE 里一模一样。
 */
"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");

let client = null;              // LspClient
let diagCol = null;             // DiagnosticCollection
let outChan = null;             // 输出面板
let logChan = null;             // 服务器日志
let statusItem = null;
let faRoot = "";                // 找到的 FA 根目录（含 lsp/ 和 compiler/）

// ------------------------------------------------------------------ 激活
function activate(ctx) {
  outChan = vscode.window.createOutputChannel("FA");
  logChan = vscode.window.createOutputChannel("FA 语言服务器日志");
  diagCol = vscode.languages.createDiagnosticCollection("fa");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 5);
  statusItem.command = "fa.showServerLog";
  ctx.subscriptions.push(outChan, logChan, diagCol, statusItem);

  faRoot = findFaRoot(ctx);
  if (!faRoot) {
    statusItem.text = "$(warning) FA：没找到编译器";
    statusItem.tooltip = "配置 fa.compilerPath 指向 FA 的安装目录（里面有 lsp/fa_lsp.py）";
    statusItem.show();
    vscode.window.showErrorMessage(
      "FA 扩展没找到语言服务器（lsp/fa_lsp.py）。装 fa 的 deb/exe，或在设置里把 fa.compilerPath 指到 FA 目录。",
      "打开设置").then((v) => { if (v) vscode.commands.executeCommand("workbench.action.openSettings", "fa.compilerPath"); });
    return;
  }
  outChan.appendLine(`[FA] 扩展激活，FA 根目录：${faRoot}`);

  startClient(ctx);
  registerProviders(ctx);
  registerCommands(ctx);

  // 已经打开着的 .fa 文件也要立刻检查一遍
  vscode.window.visibleTextEditors.forEach((e) => onDocOpen(e.document));
  ctx.subscriptions.push(
    vscode.workspace.onDidOpenTextDocument(onDocOpen),
    vscode.workspace.onDidChangeTextDocument(scheduleCheck),
    vscode.workspace.onDidSaveTextDocument((d) => checkNow(d)),
    vscode.workspace.onDidCloseTextDocument((d) => diagCol.delete(d.uri)),
    vscode.window.onDidChangeActiveTextEditor(updateStatusForEditor)
  );
}

function deactivate() {
  if (client) { client.stop(); client = null; }
}

// ------------------------------------------------------------------ 找 FA
function findFaRoot(ctx) {
  const cfg = vscode.workspace.getConfiguration("fa");
  const cands = [];
  const p = cfg.get("compilerPath", "");
  if (p) cands.push(p);
  cands.push(path.join(ctx.extensionPath, "fa"));            // 插件自带的（打包时塞进去）
  const wf = vscode.workspace.workspaceFolders;
  if (wf && wf.length) cands.push(path.join(wf[0].uri.fsPath, "FA"));
  if (wf && wf.length) cands.push(wf[0].uri.fsPath);          // 就在 FA 仓库里开发
  if (process.env.FA_HOME) cands.push(process.env.FA_HOME);
  for (const c of cands) {
    if (c && fs.existsSync(path.join(c, "lsp", "fa_lsp.py")) &&
        fs.existsSync(path.join(c, "compiler", "falang", "sema.py"))) {
      return c;
    }
  }
  // PATH 上的 fa：读它的真实位置再往上找
  const which = process.platform === "win32" ? "where fa" : "command -v fa";
  try {
    const out = cp.execSync(which, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim().split(/\r?\n/)[0];
    if (out) {
      const real = fs.realpathSync(out);
      for (let d = path.dirname(real); d && d !== path.dirname(d); d = path.dirname(d)) {
        if (fs.existsSync(path.join(d, "lsp", "fa_lsp.py"))) return d;
      }
    }
  } catch (e) { /* 找不到就算了 */ }
  return "";
}

function pythonExe() {
  const cfg = vscode.workspace.getConfiguration("fa").get("pythonPath", "");
  if (cfg) return cfg;
  if (process.env.FA_PYTHON) return process.env.FA_PYTHON;
  return process.platform === "win32" ? "python" : "python3";
}

// ------------------------------------------------------------------ LSP 分帧
/* Content-Length 分帧。收发都在这儿，LspClient 只管进程和请求表。
 * 拆出来是为了能单独测：一个响应被 TCP 切成三半、两条消息挤在一个 chunk 里、
 * 头和数据之间断在正好 4 个字节上 —— 这些情况手写 framing 最容易出错。 */
class FrameDecoder {
  constructor(onMessage) { this.onMessage = onMessage; this.buf = Buffer.alloc(0); }

  feed(chunk) {
    this.buf = Buffer.concat([this.buf, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)]);
    let out = 0;
    for (;;) {
      const i = this.buf.indexOf("\r\n\r\n");
      if (i < 0) return out;
      const head = this.buf.slice(0, i).toString("ascii");
      const m = /content-length:\s*(\d+)/i.exec(head);
      if (!m) { this.buf = this.buf.slice(i + 4); continue; }   // 头坏了，跳过这段
      const len = parseInt(m[1], 10);
      if (this.buf.length < i + 4 + len) return out;            // 正文还没收全
      const body = this.buf.slice(i + 4, i + 4 + len).toString("utf8");
      this.buf = this.buf.slice(i + 4 + len);
      out++;
      let msg;
      try { msg = JSON.parse(body); } catch (e) { continue; }   // 半截 JSON，丢掉
      this.onMessage(msg);
    }
  }

  static encode(obj) {
    const body = Buffer.from(JSON.stringify(obj), "utf8");
    return Buffer.concat([Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, "ascii"), body]);
  }
}

// ------------------------------------------------------------------ LSP 客户端
class LspClient {
  constructor(cmd, args, env) {
    this.seq = 0;
    this.pending = new Map();       // id -> {resolve, reject}
    this.handlers = new Map();      // method -> fn(params)
    this.alive = false;
    this.decoder = new FrameDecoder((m) => this._dispatch(m));
    this.proc = cp.spawn(cmd, args, { env: env, stdio: ["pipe", "pipe", "pipe"], shell: false });
    this.alive = true;
    this.proc.stdout.on("data", (d) => this._feed(d));
    this.proc.stderr.on("data", (d) => { logChan.append(d.toString()); });
    this.proc.on("exit", (code) => {
      this.alive = false;
      logChan.appendLine(`[fa-lsp] 退出，码 ${code}`);
      this.pending.forEach((p) => p.reject(new Error("语言服务器已退出")));
      this.pending.clear();
    });
    this.proc.on("error", (e) => {
      this.alive = false;
      logChan.appendLine(`[fa-lsp] 起不来：${e.message}`);
    });
  }

  _feed(chunk) { this.decoder.feed(chunk); }

  _dispatch(msg) {
    trace(msg, "«");
    if (msg.id !== undefined && (msg.result !== undefined || msg.error)) {
      const p = this.pending.get(msg.id);
      if (p) {
        this.pending.delete(msg.id);
        if (msg.error) p.reject(new Error(msg.error.message || "LSP 错误"));
        else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method) {
      const h = this.handlers.get(msg.method);
      if (h) { try { h(msg.params || {}); } catch (e) { logChan.appendLine(`[fa-lsp] 处理 ${msg.method} 出错：${e.message}`); } }
      if (msg.id !== undefined) this._raw({ jsonrpc: "2.0", id: msg.id, result: null });
    }
  }

  _raw(obj) {
    if (!this.alive) return;
    trace(obj, "»");
    try {
      this.proc.stdin.write(FrameDecoder.encode(obj));      // 头和正文一次写完，别被切开
    } catch (e) { logChan.appendLine(`[fa-lsp] 写不进去：${e.message}`); }
  }

  request(method, params) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this._raw({ jsonrpc: "2.0", id, method, params });
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error(method + " 超时（5s）")); }
      }, 5000);
    });
  }

  notify(method, params) { this._raw({ jsonrpc: "2.0", method, params }); }
  on(method, fn) { this.handlers.set(method, fn); }
  stop() {
    if (!this.alive) return;
    try {
      this.notify("exit", {});
      setTimeout(() => { try { this.proc.kill(); } catch (e) { } }, 300);
    } catch (e) { try { this.proc.kill(); } catch (e2) { } }
    this.alive = false;
  }
}

function trace(msg, dir) {
  const level = vscode.workspace.getConfiguration("fa").get("trace.server", "off");
  if (level === "off") return;
  let s = JSON.stringify(msg);
  if (level !== "verbose" && s.length > 400) s = s.slice(0, 400) + "…";
  logChan.appendLine(`${dir} ${s}`);
}

function startClient(ctx) {
  const env = Object.assign({}, process.env);
  env.PYTHONPATH = path.join(faRoot, "compiler") + (env.PYTHONPATH ? path.delimiter + env.PYTHONPATH : "");
  env.FA_HOME = faRoot;
  env.PYTHONIOENCODING = "utf-8";
  const py = pythonExe();
  const entry = path.join(faRoot, "lsp", "fa_lsp.py");
  outChan.appendLine(`[FA] 启动语言服务器：${py} ${entry}`);
  try {
    client = new LspClient(py, [entry], env);
  } catch (e) {
    vscode.window.showErrorMessage(`FA 语言服务器起不来：${e.message}（检查 fa.pythonPath）`);
    return;
  }
  client.on("textDocument/publishDiagnostics", (p) => {
    const uri = vscode.Uri.parse(p.uri);
    diagCol.set(uri, (p.diagnostics || []).map(toVsDiag));
    updateStatusForEditor(vscode.window.activeTextEditor);
  });
  client.on("window/logMessage", (p) => logChan.appendLine(p.message));
  client.on("window/showMessage", (p) => logChan.appendLine("[服务器] " + p.message));

  const rootUri = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length
    ? vscode.workspace.workspaceFolders[0].uri.toString() : null;
  client.request("initialize", {
    processId: process.pid, rootUri,
    capabilities: {
      textDocument: {
        completion: { completionItem: { snippetSupport: true, documentationFormat: ["markdown", "plaintext"] } },
        hover: { contentFormat: ["markdown", "plaintext"] },
        signatureHelp: { signatureInformation: { documentationFormat: ["markdown", "plaintext"] } },
        publishDiagnostics: { relatedInformation: true }
      }
    },
    initializationOptions: { lint: vscode.workspace.getConfiguration("fa").get("lintEnabled", true) }
  }).then((r) => {
    client.notify("initialized", {});
    statusItem.text = "$(check) FA";
    statusItem.tooltip = "FA 语言服务器已连接（点我看日志）";
    statusItem.show();
    outChan.appendLine("[FA] initialize 完成，能力：" + Object.keys((r && r.capabilities) || {}).join(", "));
    updateStatusForEditor(vscode.window.activeTextEditor);
  }).catch((e) => {
    statusItem.text = "$(error) FA";
    statusItem.tooltip = String(e.message || e);
    statusItem.show();
    outChan.appendLine("[FA] initialize 失败：" + (e.message || e));
  });
}

function toVsDiag(d) {
  const sev = d.severity === 1 ? vscode.DiagnosticSeverity.Error
    : d.severity === 2 ? vscode.DiagnosticSeverity.Warning
      : d.severity === 3 ? vscode.DiagnosticSeverity.Information
        : vscode.DiagnosticSeverity.Hint;
  const dg = new vscode.Diagnostic(toRange(d.range), d.message || "", sev);
  dg.source = d.source || "fa";
  if (d.code) dg.code = String(d.code);
  return dg;
}
function toRange(r) {
  const s = (r && r.start) || { line: 0, character: 0 };
  const e = (r && r.end) || { line: 0, character: 1 };
  return new vscode.Range(s.line, s.character, e.line, e.character);
}

// ------------------------------------------------------------------ 文档同步
const timers = new Map();
function onDocOpen(doc) {
  if (!isFa(doc) || !client || !client.alive) return;
  client.notify("textDocument/didOpen", {
    textDocument: { uri: doc.uri.toString(), languageId: "fa", version: doc.version, text: doc.getText() }
  });
  updateStatusForEditor(vscode.window.activeTextEditor);
}

function scheduleCheck(e) {
  const doc = e.document;
  if (!isFa(doc) || !client || !client.alive) return;
  const delay = vscode.workspace.getConfiguration("fa").get("analyzeDelayMs", 300);
  client.notify("textDocument/didChange", {
    textDocument: { uri: doc.uri.toString(), version: doc.version },
    contentChanges: [{ text: doc.getText() }]           // Full 同步：每次给全文
  });
  const old = timers.get(doc.uri.toString());
  if (old) clearTimeout(old);
  timers.set(doc.uri.toString(), setTimeout(() => updateStatusForEditor(vscode.window.activeTextEditor), delay));
}

function checkNow(doc) {
  if (!isFa(doc) || !client || !client.alive) return;
  client.notify("textDocument/didSave", {
    textDocument: { uri: doc.uri.toString() }, text: doc.getText()
  });
}

function isFa(doc) {
  return doc && (doc.languageId === "fa" || /\.fa$/i.test(doc.fileName || ""));
}

function updateStatusForEditor(ed) {
  if (!statusItem) return;
  if (!ed || !isFa(ed.document)) { statusItem.text = client && client.alive ? "$(check) FA" : "$(warning) FA"; statusItem.show(); return; }
  const ds = diagCol.get(ed.document.uri) || [];
  const errs = ds.filter((d) => d.severity === vscode.DiagnosticSeverity.Error).length;
  const warns = ds.length - errs;
  statusItem.text = errs ? `$(error) FA ${errs}` : warns ? `$(warning) FA ${warns}` : "$(check) FA";
  statusItem.tooltip = errs || warns ? `${errs} 个错误，${warns} 个警告（点我看服务器日志）` : "没有问题";
  statusItem.show();
}

// ------------------------------------------------------------------ 语言功能
function registerProviders(ctx) {
  const sel = { scheme: "file" };

  ctx.subscriptions.push(vscode.languages.registerCompletionItemProvider("fa", {
    provideCompletionItems(doc, pos) {
      if (!client || !client.alive) return null;
      return client.request("textDocument/completion", {
        textDocument: { uri: doc.uri.toString() }, position: { line: pos.line, character: pos.character }
      }).then((r) => {
        const items = (r.items || []).map(toVsCompletion);
        if (!items.length) return items;
        // 上下文说明放在第一项的 detail 里？不，VSCode 没这位置 —— 打到输出面板，
        // 排查「为什么给我这些」的时候有用
        if (r.faWhy) outChan.appendLine(`[FA] 补全上下文 ${r.faContext}：${r.faWhy}`);
        return items;
      }).catch((e) => { outChan.appendLine("[FA] 补全失败：" + e.message); return []; });
    }
  }, ".", ":", "<", '"', "("));

  ctx.subscriptions.push(vscode.languages.registerHoverProvider("fa", {
    provideHover(doc, pos) {
      if (!client || !client.alive) return null;
      return client.request("textDocument/hover", {
        textDocument: { uri: doc.uri.toString() }, position: { line: pos.line, character: pos.character }
      }).then((r) => {
        if (!r || !r.contents) return null;
        const md = typeof r.contents === "string" ? r.contents : (r.contents.value || "");
        return new vscode.Hover(new vscode.MarkdownString(md));
      }).catch(() => null);
    }
  }));

  ctx.subscriptions.push(vscode.languages.registerSignatureHelpProvider("fa", {
    provideSignatureHelp(doc, pos) {
      if (!client || !client.alive) return null;
      return client.request("textDocument/signatureHelp", {
        textDocument: { uri: doc.uri.toString() }, position: { line: pos.line, character: pos.character }
      }).then((r) => {
        if (!r || !r.signatures || !r.signatures.length) return null;
        const sh = new vscode.SignatureHelp();
        r.signatures.forEach((s) => {
          const si = new vscode.SignatureInformation(s.label,
            s.documentation ? new vscode.MarkdownString(s.documentation.value || s.documentation) : undefined);
          si.parameters = (s.parameters || []).map((p) => new vscode.ParameterInformation(typeof p === "string" ? p : p.label));
          sh.signatures.push(si);
        });
        sh.activeSignature = r.activeSignature || 0;
        sh.activeParameter = r.activeParameter || 0;
        return sh;
      }).catch(() => null);
    }
  }, "(", ",", "{"));

  ctx.subscriptions.push(vscode.languages.registerDocumentSymbolProvider("fa", {
    provideDocumentSymbols(doc) {
      if (!client || !client.alive) return [];
      return client.request("textDocument/documentSymbol", {
        textDocument: { uri: doc.uri.toString() }
      }).then((r) => (r || []).map((s) => {
        const si = new vscode.SymbolInformation(
          s.name, toVsSymbolKind(s.kind), s.detail || "",
          new vscode.Location(doc.uri, toRange(s.range)));
        return si;
      })).catch(() => []);
    }
  }));

  ctx.subscriptions.push(vscode.languages.registerDefinitionProvider("fa", {
    provideDefinition(doc, pos) {
      if (!client || !client.alive) return null;
      return client.request("textDocument/definition", {
        textDocument: { uri: doc.uri.toString() }, position: { line: pos.line, character: pos.character }
      }).then((r) => (r && r.uri) ? new vscode.Location(vscode.Uri.parse(r.uri), toRange(r.range)) : null)
        .catch(() => null);
    }
  }));
}

const COMP_KIND = {
  1: "Text", 2: "Method", 3: "Function", 5: "Field", 6: "Variable", 7: "Struct",
  9: "Module", 12: "Value", 14: "Keyword", 15: "Snippet", 20: "EnumMember"
};
const SYM_KIND = {
  1: "File", 2: "Module", 5: "Class", 6: "Method", 7: "Property", 10: "Enum",
  12: "Function", 13: "Variable", 14: "Constant", 23: "Struct"
};

function toVsCompletion(it) {
  const ci = new vscode.CompletionItem(it.label,
    vscode.CompletionItemKind[COMP_KIND[it.kind] || "Text"]);
  ci.detail = it.detail || "";
  ci.insertText = it.insertTextFormat === 2
    ? new vscode.SnippetString(it.insertText || it.label)
    : (it.insertText || it.label);
  ci.sortText = it.sortText;
  ci.filterText = it.filterText;
  return ci;
}
function toVsSymbolKind(k) { return vscode.SymbolKind[SYM_KIND[k] || "Variable"]; }

// ------------------------------------------------------------------ 命令
function registerCommands(ctx) {
  const reg = (id, fn) => ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));

  reg("fa.restartServer", () => {
    if (client) client.stop();
    diagCol.clear();
    startClient(ctx);
    vscode.window.visibleTextEditors.forEach((e) => onDocOpen(e.document));
    outChan.appendLine("[FA] 语言服务器已重启");
  });
  reg("fa.showServerLog", () => logChan.show(true));

  reg("fa.runFile", () => runFa(["run"], true));
  reg("fa.checkFile", () => runFa(["check"], false));
  reg("fa.buildFile", () => runFa(["build"], false));
  reg("fa.showAsm", () => runFa(["asm"], false));
  reg("fa.openWebIDE", () => {
    const port = 8765;
    outChan.appendLine(`[FA] 起网页版 IDE：http://localhost:${port}/`);
    const env = Object.assign({}, process.env);
    env.PYTHONPATH = path.join(faRoot, "compiler");
    env.FA_HOME = faRoot;
    const p = cp.spawn(pythonExe(), [path.join(faRoot, "ide", "server.py"),
      "--port", String(port), "--root", workspaceDir()],
      { env, detached: true, stdio: "ignore" });
    p.on("error", (e) => vscode.window.showErrorMessage("起网页 IDE 失败：" + e.message));
    p.unref();
    setTimeout(() => vscode.env.openExternal(vscode.Uri.parse(`http://localhost:${port}/`)), 1200);
    vscode.window.showInformationMessage(`FA 网页版 IDE：http://localhost:${port}/（关掉终端前它一直在）`);
  });
}

function workspaceDir() {
  const wf = vscode.workspace.workspaceFolders;
  return wf && wf.length ? wf[0].uri.fsPath : (faRoot || os.homedir());
}

/* 调 fa 的命令行。走的是真编译器（不是扩展里另写一套），
 * 所以命令行里看到什么，编辑器里就是什么。 */
function runFa(subCmd, isRun) {
  const ed = vscode.window.activeTextEditor;
  if (!ed) { vscode.window.showErrorMessage("先打开一个 .fa 文件"); return; }
  const doc = ed.document;
  if (!isFa(doc)) { vscode.window.showErrorMessage("当前文件不是 .fa"); return; }
  if (doc.isDirty) doc.save();

  outChan.show(true);
  const args = subCmd.concat([doc.fileName]);
  outChan.appendLine(`\n$ fa ${args.join(" ")}`);
  const env = Object.assign({}, process.env);
  env.PYTHONPATH = path.join(faRoot, "compiler");
  env.FA_HOME = faRoot;
  env.PYTHONIOENCODING = "utf-8";

  const timeout = vscode.workspace.getConfiguration("fa").get("runTimeoutSec", 20) * 1000;
  const p = cp.spawn(pythonExe(), [path.join(faRoot, "bin", "fa_cli.py")].concat(args), {
    env, cwd: path.dirname(doc.fileName), shell: false
  });
  // bin/fa 是 bash 脚本，Windows 上跑不了；所以直接调 cli.py。
  let killed = false;
  const timer = setTimeout(() => {
    killed = true;
    try { p.kill("SIGKILL"); } catch (e) { }
    outChan.appendLine(`[FA] 跑满 ${timeout / 1000} 秒，杀掉了（死循环？）`);
  }, timeout);
  p.stdout.on("data", (d) => outChan.append(d.toString()));
  p.stderr.on("data", (d) => outChan.append(d.toString()));
  p.on("error", (e) => {
    clearTimeout(timer);
    outChan.appendLine(`[FA] 起不来：${e.message}`);
    outChan.appendLine(`[FA] 提示：这个扩展需要 FA 仓库/安装目录里有 bin/fa_cli.py（faRoot=${faRoot}）`);
  });
  p.on("close", (code) => {
    clearTimeout(timer);
    if (killed) return;
    outChan.appendLine(`[FA] 退出码 ${code}`);
    if (isRun && code === 0) statusItem.text = "$(check) FA";
  });
}

module.exports = {
  activate, deactivate, findFaRoot, FrameDecoder, LspClient,
  COMP_KIND, SYM_KIND, toVsDiag: null,     // toVsDiag 要 vscode 类型，测试里只验映射表
  _test: { pythonExe, isFa, workspaceDir, setFaRoot: (r) => { faRoot = r; } }
};
