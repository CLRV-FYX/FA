#!/usr/bin/env node
/* 前端纯函数的回归测试（不需要浏览器）。
 *
 * 跑法：node ide/test_frontend.js
 *
 * app.js 是给浏览器写的，这里用一套最小 DOM 桩把它 load 进来，
 * 只测那几个不碰 DOM 的函数：高亮切片、诊断叠加、snippet 展开、
 * markdown 渲染、行列与偏移的互相换算。这几块错了，编辑器就会
 * 「波浪线画错地方」「补全插错位置」，而这种 bug 光看代码看不出来。
 */
"use strict";

const path = require("path");

// ---------------------------------------------------------------- DOM 桩
function fakeEl() {
  return {
    style: {}, dataset: {}, value: "", textContent: "", innerHTML: "", title: "",
    className: "", disabled: false, offsetWidth: 0, offsetHeight: 0,
    scrollTop: 0, scrollLeft: 0, clientHeight: 0, clientWidth: 0,
    selectionStart: 0, selectionEnd: 0, onclick: null, onmousedown: null,
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, appendChild() {}, removeChild() {}, remove() {},
    querySelectorAll() { return []; }, querySelector() { return fakeEl(); },
    contains() { return false; }, focus() {}, select() {}, scrollIntoView() {},
    setSelectionRange() {}, getBoundingClientRect() {
      return { left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600 };
    },
    childNodes: []
  };
}
const els = {};
global.document = {
  getElementById: (id) => (els[id] = els[id] || fakeEl()),
  createElement: () => fakeEl(),
  querySelectorAll: () => [],
  addEventListener() {},
  body: fakeEl()
};
global.window = global;
global.addEventListener = () => {};
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.fetch = () => Promise.reject(new Error("node 里没有后端"));
global.getComputedStyle = () => ({ lineHeight: "21px", paddingTop: "8px", paddingLeft: "8px" });
global.performance = { now: () => 0 };
global.confirm = () => true;
global.alert = () => {};

require(path.join(__dirname, "static", "tokens.js"));
require(path.join(__dirname, "static", "app.js"));

const T = global.FaTokens;
const P = global.FaIDEPure;

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

// ---------------------------------------------------------------- 1. 高亮
console.log("[1] 词法高亮");
let r = T.highlightLine('fn main() -> i64:', "");
let cls = r.pieces.map(p => p.c + ":" + p.t).join(" | ");
ck("fn 是关键字", /t-kw:fn/.test(cls), cls);
ck("main 是函数名", /t-fn:main/.test(cls), cls);
ck("i64 是类型", /t-ty:i64/.test(cls), cls);
ck("整行拼回去和原文一致",
  r.pieces.map(p => p.t).join("") === "fn main() -> i64:", cls);

r = T.highlightLine('    print("中文，标点。")   # 注释里有 : 和 "', "");
ck("字符串整段是一种颜色", r.pieces.some(p => p.c === "t-str" && p.t === '"中文，标点。"'),
  JSON.stringify(r.pieces));
ck("注释到行尾", r.pieces[r.pieces.length - 1].c === "t-com", JSON.stringify(r.pieces.slice(-1)));
ck("注释里的引号不会开启字符串", r.state === "", r.state);

r = T.highlightLine('    let n = 0xFF + 3.5e-2 + 1_000', "");
ck("十六进制/科学计数/下划线分隔都算数字",
  r.pieces.filter(p => p.c === "t-num").map(p => p.t).join(",") === "0xFF,3.5e-2,1_000",
  JSON.stringify(r.pieces));

r = T.highlightLine('    let s = """开头', "");
ck("三引号开启跨行状态", r.state === '"""', r.state);
r = T.highlightLine('中间一行', r.state);
ck("下一行整行还在字符串里", r.pieces.length === 1 && r.pieces[0].c === "t-str", JSON.stringify(r.pieces));
ck("状态延续", r.state === '"""', r.state);
r = T.highlightLine('结尾"""', r.state);
ck("三引号闭合后状态归零", r.state === "", r.state);

r = T.highlightLine('    let v = Vec<i64>[1,2].map(double)', "");
ck("Vec 是类型", r.pieces.some(p => p.c === "t-ty" && p.t === "Vec"), JSON.stringify(r.pieces));
ck("map 后面跟 ( 算函数", r.pieces.some(p => p.c === "t-fn" && p.t === "map"), JSON.stringify(r.pieces));
ck("整行拼回去一致", r.pieces.map(p => p.t).join("") === "    let v = Vec<i64>[1,2].map(double)");

r = T.highlightLine('    let x = S_IFDIR and true', "");
ck("全大写常量着色", r.pieces.some(p => p.c === "t-const" && p.t === "S_IFDIR"), JSON.stringify(r.pieces));
ck("and 是控制流关键字", r.pieces.some(p => p.c === "t-kw2" && p.t === "and"), JSON.stringify(r.pieces));
ck("true 是常量", r.pieces.some(p => p.c === "t-const" && p.t === "true"), JSON.stringify(r.pieces));

// 整个文件的高亮：每行拼回去必须等于原文（少一个字符，光标就会错位）
const SRC = [
  'use std.time', '', 'struct P:', '    name: str', '',
  'fn main() -> i64:', '    let t = Time.now()   // 时间戳',
  '    print("多行字符串：""" + t.to_str())', '    return 0', ''
].join("\n");
const all = T.highlight(SRC, "");
eq("文件行数对得上", all.lines.length, SRC.split("\n").length);
ck("每行拼回去都等于原文", all.lines.every((pieces, i) =>
  pieces.map(p => p.t).join("") === SRC.split("\n")[i]),
  all.lines.map((ps, i) => [ps.map(p => p.t).join(""), SRC.split("\n")[i]]).filter(x => x[0] !== x[1])[0]);

// configure：后端给的真表要能覆盖兜底表
T.configure({ keywords: ["fn", "let", "zzz"], types: ["i64", "MyType"], builtins: ["print"] });
r = T.highlightLine("zzz MyType", "");
ck("configure 生效（新关键字/新类型）",
  r.pieces[0].c === "t-kw" && r.pieces.some(p => p.c === "t-ty" && p.t === "MyType"), JSON.stringify(r.pieces));
T.configure({ keywords: T.defaults.keywords, types: T.defaults.types, builtins: T.defaults.builtins });

// ---------------------------------------------------------------- 2. 诊断叠加
console.log("[2] 诊断波浪线叠加");
const pieces = [{ t: "    print(", c: "" }, { t: "x：1", c: "t-str" }];
const ranges = [{ a: 9, b: 12, cls: "d-warn" }];       // 落在 "x：1" 的第 1..3 个字符上
const out = P.applyDiags(pieces, ranges);
ck("叠加后文本不变", out.map(p => p.t).join("") === pieces.map(p => p.t).join(""), JSON.stringify(out));
ck("命中区间跨了片段也全覆盖（拼起来正好是 (x：）",
  out.filter(p => /d-warn/.test(p.c)).map(p => p.t).join("") === "(x：", JSON.stringify(out));
ck("没命中的段保持原类", out.some(p => p.c === "" && p.t === "    print"), JSON.stringify(out));
ck("字符串段的类名保留（t-str 还在）", out.some(p => p.c === "t-str" && p.t === "1"), JSON.stringify(out));
ck("原数组没被改（不可变）", JSON.stringify(pieces) ===
  JSON.stringify([{ t: "    print(", c: "" }, { t: "x：1", c: "t-str" }]), JSON.stringify(pieces));
eq("没有诊断时原样返回", P.applyDiags(pieces, []).length, 2);
// 一段文字被两条诊断切开
const p2 = [{ t: "abcdefgh", c: "t-var" }];
const o2 = P.applyDiags(p2, [{ a: 2, b: 4, cls: "d-err" }, { a: 6, b: 8, cls: "d-warn" }]);
ck("两条诊断把一段切成四块", o2.length === 4, JSON.stringify(o2));
ck("切完文本还是 abcdefgh", o2.map(p => p.t).join("") === "abcdefgh", JSON.stringify(o2));
ck("两块各自带对类名", o2[1].c === "t-var d-err" && o2[3].c === "t-var d-warn", JSON.stringify(o2));

// ---------------------------------------------------------------- 3. 位置换算
console.log("[3] 行列 <-> 偏移");
const t3 = "ab\ncdef\ngh";
eq("(1,1) -> 偏移 0", P.offsetFromPos(t3, 1, 1), 0);
eq("(2,3) -> 偏移 5", P.offsetFromPos(t3, 2, 3), 5);
eq("(3,2) -> 偏移 9", P.offsetFromPos(t3, 3, 2), 9);
eq("偏移 5 -> (2,3)", P.posFromOffset(t3, 5), { line: 2, col: 3 });
eq("偏移 0 -> (1,1)", P.posFromOffset(t3, 0), { line: 1, col: 1 });
eq("偏移 8 -> (3,1)", P.posFromOffset(t3, 8), { line: 3, col: 1 });
eq("偏移 9 -> (3,2)", P.posFromOffset(t3, 9), { line: 3, col: 2 });
ck("往返一致", [0, 3, 5, 8, 9].every(o =>
  P.offsetFromPos(t3, P.posFromOffset(t3, o).line, P.posFromOffset(t3, o).col) === o));
eq("越界的行夹到最后一行的行首", P.offsetFromPos(t3, 99, 1), 8);
eq("越界的列夹到 0 偏移", P.offsetFromPos(t3, 1, 99), 98);

// ---------------------------------------------------------------- 4. snippet
console.log("[4] snippet 展开");
let sn = P.expandSnippet("for ${1:x} in ${2:xs}:");
eq("占位符展开成默认值", sn.body, "for x in xs:");
// "for x in xs:" 里第一个占位符展开成 "x"，占 body[4..5)，光标停在 5
ck("光标停在第一个占位符末尾", sn.caret === 5, String(sn.caret));
sn = P.expandSnippet("Vec<i64>[${1:1, 2, 3}]");
eq("中括号里的占位符", sn.body, "Vec<i64>[1, 2, 3]");
sn = P.expandSnippet("plain");
eq("没有占位符就原样", sn.body, "plain");
ck("光标在末尾", sn.caret === 5, String(sn.caret));
sn = P.expandSnippet('"${1:mod.fa}"');
eq("带引号的占位符", sn.body, '"mod.fa"');

// ---------------------------------------------------------------- 5. markdown
console.log("[5] 悬停的 markdown 渲染");
let h = P.mdToHTML("**map()**\n\nmap(f) -> Vec<R>　新表\n\n*Vec<i64>* 的方法");
ck("粗体转成 strong", /<strong>map\(\)<\/strong>/.test(h), h);
ck("斜体转成 em", /<em>Vec&lt;i64&gt;<\/em>/.test(h), h);
ck("< > 被转义（不会变成标签）", h.indexOf("<i64>") === -1 && h.indexOf("&lt;i64&gt;") >= 0, h);
h = P.mdToHTML("```fa\nfn f() -> i64:\n    return 1\n```");
ck("代码块转成 pre 且内容原样转义", /<pre>fn f\(\) -&gt; i64:/.test(h), h);
h = P.mdToHTML("用 `Vec<i64>` 装数字");
ck("行内码转成 code", /<code>Vec&lt;i64&gt;<\/code>/.test(h), h);
ck("script 标签会被转义（防注入）", P.mdToHTML("<script>alert(1)</script>").indexOf("<script>") === -1);

// ---------------------------------------------------------------- 结果
console.log();
console.log(`通过 ${pass} 条，失败 ${fail.length} 条`);
if (fail.length) { fail.forEach(f => console.log("  ✗ " + f)); process.exit(1); }
console.log("✓ 前端纯函数全部通过");
