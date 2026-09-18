/* FA 的词法（只为高亮服务，不参与编译）。
 *
 * 输出的是「一段一段带类名的文本」，而不是一整个 HTML 字符串 —— 这样上层才能
 * 把诊断的波浪线**插进**这些片段里（见 app.js 的 applyDiags）。
 *
 * 只有一种跨行状态：三引号字符串。注释在 FA 里都是单行的（# 或 //，
 * 见 compiler/falang/lexer.py:138）。
 */
(function (global) {
  "use strict";

  // 兜底用的表；app.js 会从 /api/info 拿编译器里真正的 KEYWORDS/TYPES/BUILTIN_FNS 覆盖
  var KEYWORDS = ("and as asm break catch const continue cxx defer dyn elif else enum extern " +
    "false fn for if impl in java let libc loop match mod move mut new nil not or pub py raise " +
    "ref return sizeof static struct trait true try typeof unsafe use where while").split(" ");
  // 这几个在 FA 里是「控制流/修饰」性质，单独一种颜色，跟类型关键字区分开
  var KW2 = ("and or not in as is".split(" "));
  var TYPES = ("i8 i16 i32 i64 isize u8 u16 u32 u64 usize f32 f64 bool char str void any " +
    "jobj pyobj Vec Map").split(" ");
  // 注意 .split 要写在括号外面：写在最后一个字符串字面量上，
  // 就变成 "…" + ["a","b"] —— 结果是**字符串**，下面 forEach 直接崩（浏览器里也一样崩）
  var BUILTINS = ("print println write len push pop str i64 f64 range input exit assert panic " +
    "sizeof typeof new free").split(" ");

  var KW_SET = {}, KW2_SET = {}, TY_SET = {}, BI_SET = {};
  KEYWORDS.forEach(function (k) { KW_SET[k] = 1; });
  KW2.forEach(function (k) { KW2_SET[k] = 1; });
  TYPES.forEach(function (k) { TY_SET[k] = 1; });
  BUILTINS.forEach(function (k) { BI_SET[k] = 1; });

  function configure(cfg) {
    if (!cfg) return;
    if (cfg.keywords && cfg.keywords.length) {
      KW_SET = {}; cfg.keywords.forEach(function (k) { KW_SET[k] = 1; });
      KW2.forEach(function (k) { KW2_SET[k] = 1; });   // 这几个是 FA 自己的，表里没有也补上
      ["and", "or", "not", "in", "as"].forEach(function (k) { KW2_SET[k] = 1; });
    }
    if (cfg.types && cfg.types.length) {
      TY_SET = {}; cfg.types.forEach(function (k) { TY_SET[k] = 1; });
      ["Vec", "Map"].forEach(function (k) { TY_SET[k] = 1; });
    }
    if (cfg.builtins && cfg.builtins.length) {
      BI_SET = {}; cfg.builtins.forEach(function (k) { BI_SET[k] = 1; });
    }
    if (cfg.userTypes && cfg.userTypes.length) {
      cfg.userTypes.forEach(function (k) { TY_SET[k] = 1; });
    }
  }

  function isWordCh(c) { return /[A-Za-z0-9_]/.test(c); }
  function isDigit(c) { return c >= "0" && c <= "9"; }

  /* 高亮整个源码。
   * 返回 {lines: [[{t:"文本", c:"类名"}, ...], ...], state: 末尾状态}
   * state = "" 或 '"""'（三引号还没关，下一行接着当字符串）
   */
  function highlight(src, state) {
    var lines = String(src).split("\n");
    var out = [];
    state = state || "";
    for (var i = 0; i < lines.length; i++) {
      var r = highlightLine(lines[i], state);
      out.push(r.pieces);
      state = r.state;
    }
    return { lines: out, state: state };
  }

  function push(pieces, text, cls) {
    if (!text) return;
    var last = pieces[pieces.length - 1];
    if (last && last.c === cls) last.t += text;      // 相邻同类合并，DOM 少一半
    else pieces.push({ t: text, c: cls });
  }

  function highlightLine(line, state) {
    var pieces = [];
    var i = 0, n = line.length;
    var quote = state || "";                          // '"""' 表示从上一行延续过来
    var start = 0;

    while (i < n) {
      // ---- 字符串里 ----
      if (quote) {
        if (line[i] === "\\") { i += 2; continue; }
        if (line.substr(i, quote.length) === quote) {
          push(pieces, line.substring(start, i + quote.length), "t-str");
          i += quote.length; quote = ""; start = i; continue;
        }
        i++; continue;
      }
      var c = line[i];
      // ---- 注释：# 或 // 到行尾 ----
      if (c === "#" || (c === "/" && line[i + 1] === "/")) {
        push(pieces, line.substring(start, i), "");
        push(pieces, line.substring(i), "t-com");
        return { pieces: pieces, state: "" };
      }
      // ---- 三引号 ----
      if (line.substr(i, 3) === '"""') {
        push(pieces, line.substring(start, i), "");
        start = i; quote = '"""'; i += 3; continue;
      }
      // ---- 单引号字符 / 双引号字符串 ----
      if (c === '"' || c === "'") {
        push(pieces, line.substring(start, i), "");
        start = i; quote = c; i++; continue;
      }
      // ---- 数字 ----
      if (isDigit(c) || (c === "." && isDigit(line[i + 1]) && !isWordCh(line[i - 1] || ""))) {
        push(pieces, line.substring(start, i), "");
        var j = i;
        if (c === "0" && (line[j + 1] === "x" || line[j + 1] === "X")) {
          j += 2; while (j < n && /[0-9a-fA-F_]/.test(line[j])) j++;
        } else if (c === "0" && (line[j + 1] === "b" || line[j + 1] === "B")) {
          j += 2; while (j < n && /[01_]/.test(line[j])) j++;
        } else {
          while (j < n && /[0-9_]/.test(line[j])) j++;
          if (line[j] === ".") { j++; while (j < n && /[0-9_]/.test(line[j])) j++; }
          if (line[j] === "e" || line[j] === "E") {
            j++; if (line[j] === "+" || line[j] === "-") j++;
            while (j < n && isDigit(line[j])) j++;
          }
        }
        push(pieces, line.substring(i, j), "t-num");
        i = j; start = i; continue;
      }
      // ---- 标识符 / 关键字 ----
      if (/[A-Za-z_]/.test(c)) {
        push(pieces, line.substring(start, i), "");
        var k = i;
        while (k < n && isWordCh(line[k])) k++;
        var word = line.substring(i, k);
        var cls = classify(word, line, k);
        push(pieces, word, cls);
        i = k; start = i; continue;
      }
      i++;
    }
    push(pieces, line.substring(start), quote ? "t-str" : "");
    return { pieces: pieces, state: quote };
  }

  function classify(word, line, after) {
    if (word === "true" || word === "false" || word === "nil") return "t-const";
    if (KW2_SET[word]) return "t-kw2";
    if (KW_SET[word]) return "t-kw";
    if (TY_SET[word]) return "t-ty";
    // 全大写带下划线的当常量（S_IFDIR / O_RDWR / MAX_SIZE）
    if (/^[A-Z][A-Z0-9_]*$/.test(word) && word.length > 1) return "t-const";
    // 后面紧跟 ( 的是调用；FA 里 fn 定义也是这个名字，都当函数着色
    var rest = line.substring(after);
    if (/^\s*\(/.test(rest)) return BI_SET[word] ? "t-fn" : "t-fn";
    if (BI_SET[word]) return "t-fn";
    // 首字母大写多半是自己定义的类型（struct Person / enum Shape）
    if (/^[A-Z]/.test(word)) return "t-ty";
    return "t-var";
  }

  /* 把片段数组渲染成 HTML（转义过）。diags 见 app.js。 */
  function toHTML(pieces) {
    var h = "";
    for (var i = 0; i < pieces.length; i++) {
      var p = pieces[i];
      var t = esc(p.t);
      h += p.c ? '<span class="' + p.c + '">' + t + "</span>" : t;
    }
    return h;
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  global.FaTokens = {
    highlight: highlight,
    highlightLine: highlightLine,
    toHTML: toHTML,
    esc: esc,
    configure: configure,
    defaults: { keywords: KEYWORDS, types: TYPES, builtins: BUILTINS }
  };
})(typeof window !== "undefined" ? window : globalThis);
