#!/usr/bin/env node
/* VSCode 扩展的回归测试（不需要 VSCode，也不需要 npm install）。
 *
 * 跑法：node editors/vscode-fa/test/extension.test.js
 *
 * 用 Module._load 把 "vscode" 换成一个桩，就能把扩展 load 进来测真东西：
 *   · LSP 的 Content-Length 分帧（被 TCP 切碎、两条挤一起、头断在中间）
 *   · 找 FA 根目录的兜底顺序
 *   · 补全/符号的 kind 映射表，别映射到 VSCode 没有的枚举名上
 *   · package.json 里承诺的东西（命令、语法、片段、main）是不是真存在
 *   · 生成的语法文件是不是真覆盖了编译器里的关键字
 */
"use strict";

const path = require("path");
const fs = require("fs");
const cp = require("child_process");
const Module = require("module");

const EXT = path.join(__dirname, "..");
const ROOT = path.join(EXT, "..", "..");

// ---------------------------------------------------------------- vscode 桩
const COMPLETION_KINDS = ["Text", "Method", "Function", "Constructor", "Field", "Variable",
  "Class", "Interface", "Module", "Property", "Unit", "Value", "Enum", "Keyword", "Snippet",
  "Color", "Reference", "File", "Folder", "EnumMember", "Constant", "Struct", "Event",
  "Operator", "TypeParameter"];
const SYMBOL_KINDS = ["File", "Module", "Namespace", "Package", "Class", "Method", "Property",
  "Field", "Constructor", "Enum", "Interface", "Function", "Variable", "Constant", "String",
  "Number", "Boolean", "Array", "Object", "Key", "Null", "EnumMember", "Struct", "Event",
  "Operator", "TypeParameter"];

function mkRange(a, b, c, d) { return { start: { line: a, character: b }, end: { line: c, character: d } }; }
const channels = [];
const vscodeStub = {
  window: {
    createOutputChannel: (n) => { const c = { name: n, lines: [], appendLine(s) { this.lines.push(String(s)); }, append(s) { this.lines.push(String(s)); }, show() { }, clear() { this.lines = []; } }; channels.push(c); return c; },
    createStatusBarItem: () => ({ text: "", tooltip: "", command: "", show() { }, hide() { }, dispose() { } }),
    showErrorMessage: (m) => { vscodeStub._errors.push(m); return Promise.resolve(undefined); },
    showInformationMessage: (m) => { vscodeStub._infos.push(m); return Promise.resolve(undefined); },
    showWarningMessage: (m) => { vscodeStub._infos.push(m); return Promise.resolve(undefined); },
    visibleTextEditors: [],
    activeTextEditor: undefined,
    onDidChangeActiveTextEditor: () => ({ dispose() { } }),
    _errors: [], _infos: []
  },
  workspace: {
    getConfiguration: () => ({ get: (k, d) => (k === "compilerPath" ? "" : d) }),
    workspaceFolders: [{ uri: { fsPath: ROOT, toString: () => "file://" + ROOT } }],
    onDidOpenTextDocument: () => ({ dispose() { } }),
    onDidChangeTextDocument: () => ({ dispose() { } }),
    onDidSaveTextDocument: () => ({ dispose() { } }),
    onDidCloseTextDocument: () => ({ dispose() { } })
  },
  languages: {
    createDiagnosticCollection: () => {
      const m = new Map();
      return { set: (u, d) => m.set(String(u), d), get: (u) => m.get(String(u)), delete: (u) => m.delete(String(u)), clear: () => m.clear(), _map: m };
    },
    registerCompletionItemProvider: () => ({ dispose() { } }),
    registerHoverProvider: () => ({ dispose() { } }),
    registerSignatureHelpProvider: () => ({ dispose() { } }),
    registerDocumentSymbolProvider: () => ({ dispose() { } }),
    registerDefinitionProvider: () => ({ dispose() { } })
  },
  commands: { registerCommand: () => ({ dispose() { } }), executeCommand: () => Promise.resolve() },
  env: { openExternal: () => Promise.resolve(true) },
  Uri: { parse: (s) => ({ toString: () => s, fsPath: s }), file: (s) => ({ toString: () => "file://" + s, fsPath: s }) },
  Range: class { constructor(a, b, c, d) { this.start = { line: a, character: b }; this.end = { line: c, character: d }; } },
  Position: class { constructor(l, c) { this.line = l; this.character = c; } },
  Location: class { constructor(u, r) { this.uri = u; this.range = r; } },
  Diagnostic: class { constructor(r, m, s) { this.range = r; this.message = m; this.severity = s; } },
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
  CompletionItem: class { constructor(l, k) { this.label = l; this.kind = k; } },
  CompletionItemKind: COMPLETION_KINDS.reduce((o, k, i) => (o[k] = i, o), {}),
  SymbolKind: SYMBOL_KINDS.reduce((o, k, i) => (o[k] = i, o), {}),
  SymbolInformation: class { constructor(n, k, d, l) { this.name = n; this.kind = k; this.detail = d; this.location = l; } },
  SignatureHelp: class { constructor() { this.signatures = []; this.activeSignature = 0; this.activeParameter = 0; } },
  SignatureInformation: class { constructor(l, d) { this.label = l; this.documentation = d; this.parameters = []; } },
  ParameterInformation: class { constructor(l) { this.label = l; } },
  MarkdownString: class { constructor(v) { this.value = v; } },
  SnippetString: class { constructor(v) { this.value = v; } },
  Hover: class { constructor(c) { this.contents = c; } },
  StatusBarAlignment: { Left: 1, Right: 2 },
  _errors: [], _infos: []
};
vscodeStub.window._errors = vscodeStub._errors;
vscodeStub.window._infos = vscodeStub._infos;

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") return vscodeStub;
  return origLoad.apply(this, arguments);
};

const ext = require(path.join(EXT, "extension.js"));

// ---------------------------------------------------------------- 断言
let pass = 0;
const fail = [];
function ck(name, cond, detail) {
  if (cond) pass++;
  else { fail.push(name); console.log("  ✗ " + name + (detail ? "　" + detail : "")); }
}
function eq(name, got, want) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  ck(name, a === b, `得到 ${a}，想要 ${b}`);
}

// ---------------------------------------------------------------- 1. 分帧
console.log("[1] LSP 的 Content-Length 分帧");
const FD = ext.FrameDecoder;
ck("FrameDecoder 导出了", typeof FD === "function");

let got = [];
let dec = new FD((m) => got.push(m));
const msg1 = { jsonrpc: "2.0", id: 1, result: { ok: true } };
const msg2 = { jsonrpc: "2.0", method: "textDocument/publishDiagnostics", params: { uri: "file:///a.fa", diagnostics: [] } };
const both = Buffer.concat([FD.encode(msg1), FD.encode(msg2)]);
eq("两条消息挤在一个 chunk 里也能都收到", (got = [], dec.feed(both), got.length), 2);
eq("第一条内容对", got[0], msg1);
eq("第二条内容对", got[1], msg2);

got = []; dec = new FD((m) => got.push(m));
const one = FD.encode({ jsonrpc: "2.0", id: 7, result: { items: [1, 2, 3] } });
for (const k of [1, 2, 3, 5, 7, one.length]) {          // 各种奇怪的切法
  got = []; dec = new FD((m) => got.push(m));
  for (let i = 0; i < one.length; i += k) dec.feed(one.slice(i, i + k));
  ck(`按 ${k} 字节切碎仍能完整解出`, got.length === 1 && got[0].id === 7, JSON.stringify(got));
}

got = []; dec = new FD((m) => got.push(m));
const s = FD.encode({ jsonrpc: "2.0", id: 1, result: "中文和 emoji ⚡ 也要按字节数算" });
dec.feed(s.slice(0, s.length - 3));
eq("正文没收全时先不解", got.length, 0);
dec.feed(s.slice(s.length - 3));
ck("补齐后解出来，中文没坏", got.length === 1 && got[0].result.indexOf("⚡") >= 0, JSON.stringify(got));

got = []; dec = new FD((m) => got.push(m));
const hdr = FD.encode(msg1);
dec.feed(hdr.slice(0, hdr.indexOf("\r\n\r\n") + 2));     // 断在头的 \r\n\r\n 中间
eq("头断在中间不会误判", got.length, 0);
dec.feed(hdr.slice(hdr.indexOf("\r\n\r\n") + 2));
ck("剩下的到了就解出来", got.length === 1, JSON.stringify(got));

got = []; dec = new FD((m) => got.push(m));
dec.feed(Buffer.from("Content-Length: notanumber\r\n\r\n{}"));
eq("头里的长度不是数字也不崩", got.length, 0);
dec.feed(FD.encode(msg1));
ck("坏帧之后还能继续收好帧", got.length === 1, JSON.stringify(got));

ck("encode 出来的头格式对", /^Content-Length: \d+\r\n\r\n/.test(FD.encode({ a: 1 }).toString("binary")));
ck("encode 的长度是**字节**数不是字符数",
  FD.encode({ s: "中文" }).toString("binary").startsWith("Content-Length: " + Buffer.byteLength(JSON.stringify({ s: "中文" }))));

// ---------------------------------------------------------------- 2. 找 FA
console.log("[2] 找 FA 根目录");
const fakeCtx = { extensionPath: EXT, subscriptions: [] };
const found = ext.findFaRoot(fakeCtx);
ck("能从工作区找到 FA 根", found === ROOT || fs.existsSync(path.join(found || "", "lsp", "fa_lsp.py")),
  String(found));
ck("找到的目录里有 lsp/fa_lsp.py", fs.existsSync(path.join(found, "lsp", "fa_lsp.py")));
ck("找到的目录里有 compiler/falang/sema.py", fs.existsSync(path.join(found, "compiler", "falang", "sema.py")));
ck("找到的目录里有 ide/server.py", fs.existsSync(path.join(found, "ide", "server.py")));
ck("找到的目录里有 bin/fa_cli.py", fs.existsSync(path.join(found, "bin", "fa_cli.py")));

// ---------------------------------------------------------------- 3. kind 映射
console.log("[3] LSP kind -> VSCode kind 的映射");
Object.keys(ext.COMP_KIND).forEach((k) => {
  const name = ext.COMP_KIND[k];
  ck(`补全 kind ${k} -> ${name} 在 VSCode 枚举里`,
    COMPLETION_KINDS.indexOf(name) >= 0, name);
});
Object.keys(ext.SYM_KIND).forEach((k) => {
  const name = ext.SYM_KIND[k];
  ck(`符号 kind ${k} -> ${name} 在 VSCode 枚举里`,
    SYMBOL_KINDS.indexOf(name) >= 0, name);
});
ck("服务端会发的 7（类型/结构体）有映射", !!ext.COMP_KIND[7]);
ck("服务端会发的 15（snippet）有映射", !!ext.COMP_KIND[15]);

// ---------------------------------------------------------------- 4. package.json
console.log("[4] package.json 承诺的东西都在");
const pkg = JSON.parse(fs.readFileSync(path.join(EXT, "package.json"), "utf8"));
ck("main 指向的文件存在", fs.existsSync(path.join(EXT, pkg.main)), pkg.main);
ck("语法文件存在", fs.existsSync(path.join(EXT, pkg.contributes.grammars[0].path)));
ck("语言配置文件存在", fs.existsSync(path.join(EXT, pkg.contributes.languages[0].configuration)));
ck("片段文件存在", fs.existsSync(path.join(EXT, pkg.contributes.snippets[0].path)));
ck("语言 id 是 fa，认 .fa", pkg.contributes.languages[0].id === "fa" &&
  pkg.contributes.languages[0].extensions.indexOf(".fa") >= 0);

// 版本号只许有一个来源。以前 package.json 写 1.0.0、编译器写 0.2.0，
// 打出来的 vsix 和 deb 各说各话，用户报 bug 都说不清装的是哪版。
{
  const initPy = fs.readFileSync(path.join(ROOT, "compiler", "falang", "__init__.py"), "utf8");
  const m = /__version__\s*=\s*"([^"]+)"/.exec(initPy);
  ck("编译器里有 __version__", !!m, initPy.slice(0, 80));
  ck("扩展版本号和编译器一致（不许各写各的）",
    m && pkg.version === m[1],
    `扩展 ${pkg.version} 编译器 ${m ? m[1] : "?"}`);
}
const cmds = pkg.contributes.commands.map((c) => c.command);
ck("命令都注册了", cmds.length >= 6, String(cmds));
const menuCmds = [];
Object.values(pkg.contributes.menus).forEach((arr) => arr.forEach((m) => menuCmds.push(m.command)));
pkg.contributes.keybindings.forEach((k) => menuCmds.push(k.command));
ck("菜单/快捷键引用的命令都在 commands 里",
  menuCmds.every((c) => cmds.indexOf(c) >= 0),
  String(menuCmds.filter((c) => cmds.indexOf(c) < 0)));
ck("每个命令在 extension.js 里都 reg 过了",
  cmds.every((c) => fs.readFileSync(path.join(EXT, "extension.js"), "utf8").indexOf('"' + c + '"') >= 0),
  String(cmds.filter((c) => fs.readFileSync(path.join(EXT, "extension.js"), "utf8").indexOf('"' + c + '"') < 0)));
ck("配置项有默认值", Object.values(pkg.contributes.configuration.properties).every((p) => p.default !== undefined));
ck("声明了 activationEvents", pkg.activationEvents.some((a) => a.indexOf("onLanguage:fa") === 0));

// ---------------------------------------------------------------- 5. 语法覆盖
console.log("[5] 语法文件覆盖编译器里的关键字");
const gram = JSON.parse(fs.readFileSync(path.join(EXT, "syntaxes", "fa.tmLanguage.json"), "utf8"));
eq("scopeName", gram.scopeName, "source.fa");
const text = JSON.stringify(gram);
const py = (code) => {
  try {
    return cp.execSync(`python3 -c "${code.replace(/"/g, '\\"')}"`,
      { cwd: ROOT, encoding: "utf8", env: Object.assign({}, process.env, { PYTHONPATH: path.join(ROOT, "compiler") }) }).trim();
  } catch (e) { return "ERR:" + e.message; }
};
const kws = py("from falang.lexer import KEYWORDS;print(' '.join(sorted(KEYWORDS)))").split(" ");
ck("从编译器读到了关键字表", kws.length > 30, String(kws.length));
const missing = kws.filter((k) => text.indexOf(k) < 0);
eq("关键字一个都不缺", missing, []);
const tys = py("from falang.types import TYPES;print(' '.join(sorted(TYPES)))").split(" ");
eq("类型一个都不缺", tys.filter((t) => text.indexOf(t) < 0), []);
ck("Vec / Map 也在类型里", text.indexOf("Vec") >= 0 && text.indexOf("Map") >= 0);
ck("注释认 # 和 // 两种", text.indexOf("comment.line.number-sign.fa") >= 0 &&
  text.indexOf("comment.line.double-slash.fa") >= 0);
ck("字符串插值有专门的 scope", text.indexOf("meta.interpolation.fa") >= 0);
ck("三引号字符串有专门的 scope", text.indexOf("string.quoted.triple.fa") >= 0);

// 语言配置：块头以冒号收尾要自动缩进（FA 最常见的写法）
const langCfg = JSON.parse(fs.readFileSync(path.join(EXT, "language-configuration.json"), "utf8"));
ck("increaseIndentPattern 认「以 : 或 { 收尾」",
  new RegExp(langCfg.indentationRules.increaseIndentPattern).test("fn main() -> i64:"),
  langCfg.indentationRules.increaseIndentPattern);
ck("同一个 pattern 也认大括号风格",
  new RegExp(langCfg.indentationRules.increaseIndentPattern).test("fn main() -> i64 {"));
ck("不该缩进的行不匹配",
  !new RegExp(langCfg.indentationRules.increaseIndentPattern).test("    return 0"));
eq("行注释是 #", langCfg.comments.lineComment, "#");

// ---------------------------------------------------------------- 6. 片段
console.log("[6] 代码片段");
const snips = JSON.parse(fs.readFileSync(path.join(EXT, "snippets", "fa.json"), "utf8"));
ck("片段数量 >= 14", Object.keys(snips).length >= 14, String(Object.keys(snips).length));
Object.keys(snips).forEach((k) => {
  const s = snips[k];
  ck(`片段 ${k} 结构完整`, s.prefix && Array.isArray(s.body) && s.body.length > 0 && s.description);
});
ck("main 片段是 FA 的写法（带冒号）",
  snips.main.body[0] === "fn main() -> i64:", JSON.stringify(snips.main.body));
ck("片段里的占位符语法合法",
  Object.values(snips).every((s) => s.body.join("\n").split("${").every((part, i) =>
    i === 0 || /^\d+[:|}]/.test(part) || /^\d+\}/.test(part))));

// 和网页 IDE 用的是同一份（改一边不会漏另一边）
const pySnip = py("import sys;sys.path.insert(0,'lsp');import fa_lang as F;print(' '.join(x[0] for x in F.SNIPPETS))").split(" ");
eq("片段名和语言内核里那份一致", pySnip.filter((x) => x && !snips[x]), []);

// ---------------------------------------------------------------- 结果
console.log();
console.log(`通过 ${pass} 条，失败 ${fail.length} 条`);
if (fail.length) { fail.forEach((f) => console.log("  ✗ " + f)); process.exit(1); }
console.log("✓ VSCode 扩展全部通过");
