/* FA IDE 前端。
 *
 * 编辑器是「叠加式」的：底层 <pre id="hl"> 画高亮和波浪线，顶层透明
 * <textarea id="ed"> 负责输入和光标。两层字体/行高/内边距必须一致（见 style.css），
 * 滚动时把 textarea 的 scrollTop/Left 同步给底层。
 *
 * 所有语言智能都来自后端 /api/*，后端又是 lsp/fa_lang.py —— 和 LSP 同一套内核，
 * 所以网页里看到的诊断跟 VSCode 里的一模一样。
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- DOM
  var $ = function (id) { return document.getElementById(id); };
  var ed = $("ed"), hl = $("hl"), gutter = $("gutter");
  var popup = $("popup"), hoverbox = $("hoverbox"), sighelp = $("sighelp");
  var stFile = $("st-file"), stPos = $("st-pos"), stDiags = $("st-diags"), stTime = $("st-time");
  var filelabel = $("filelabel"), dirtyDot = $("dirty");
  var problemsEl = $("tab-problems"), outputEl = $("output"), cheatEl = $("tab-cheat");
  var outlineEl = $("outline"), treeEl = $("filetree");

  // ---------------------------------------------------------------- 状态
  var S = {
    path: "untitled.fa",          // 相对工作目录的路径；untitled.fa 表示还没落盘
    saved: "",                    // 上次保存/打开时的内容，用来判断脏
    diags: [],                    // 后端给的诊断
    symbols: [],
    info: null,
    items: [],                    // 当前补全项
    sel: 0,                       // 选中的补全项
    popCtx: "",
    popOpen: false,
    sigOpen: false,
    running: false,
    lastAnalyze: 0,
    args: "",                     // 运行参数
    timeout: 10,
    opt: 2,
    reqSeq: 0                     // 请求序号：晚发先至的响应要丢掉
  };

  function load(k, d) { try { var v = localStorage.getItem("faide." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem("faide." + k, JSON.stringify(v)); } catch (e) { } }

  // ---------------------------------------------------------------- API
  function api(path, body) {
    var opt = body === undefined
      ? { method: "GET" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    return fetch(path, opt).then(function (r) { return r.json(); });
  }

  // ---------------------------------------------------------------- 位置换算
  // 这两个是纯函数（收 text 参数），node 那边能直接测；见 test_frontend.js
  function posFromOffset(text, off) {
    var v = String(text).substring(0, off);
    var nl = v.lastIndexOf("\n");
    return { line: v.split("\n").length, col: off - nl };   // 都是 1 起
  }
  function offsetFromPos(text, line, col) {
    var ls = String(text).split("\n"), o = 0;
    // 上界是 ls.length - 1：目标行自己那一行的长度不该累加进去，
    // 否则行号越界（诊断是旧的、文件已经改短了）时偏移会跑到 EOF 之外
    for (var i = 0; i < line - 1 && i < ls.length - 1; i++) o += ls[i].length + 1;
    return o + Math.max(0, (col || 1) - 1);
  }
  function caretPos() { return posFromOffset(ed.value, ed.selectionStart); }

  // ---------------------------------------------------------------- 渲染
  function render() {
    var src = ed.value;
    var res = FaTokens.highlight(src, "");
    var byLine = {};
    S.diags.forEach(function (d) {
      var k = d.line;
      (byLine[k] = byLine[k] || []).push({
        a: Math.max(0, d.col - 1), b: Math.max(1, d.endCol - 1),
        cls: d.severity === 1 ? "d-err" : (d.severity === 2 ? "d-warn" : "d-info")
      });
    });
    var html = [], marks = [];
    for (var i = 0; i < res.lines.length; i++) {
      var pieces = applyDiags(res.lines[i], byLine[i + 1] || []);
      html.push('<div class="ln">' + (FaTokens.toHTML(pieces) || " ") + "</div>");
      marks.push(byLine[i + 1] ? (byLine[i + 1][0].cls === "d-err" ? "e" : "w") : "");
    }
    hl.innerHTML = html.join("");
    renderGutter(res.lines.length, marks);
    syncScroll();
  }

  /* 把诊断区间**切进**高亮片段里：一段文字可能一半是字符串、一半带波浪线。
   * ranges 是这一行上的 0 起字符区间；返回新的片段数组（不改原数组）。 */
  function applyDiags(pieces, ranges) {
    if (!ranges || !ranges.length) return pieces;
    var out = [], pos = 0;
    for (var i = 0; i < pieces.length; i++) {
      var p = pieces[i], end = pos + p.t.length;
      var cur = pos;
      while (cur < end) {
        var r = null;
        for (var j = 0; j < ranges.length; j++) {
          if (ranges[j].a < end && ranges[j].b > cur) { r = ranges[j]; break; }
        }
        if (!r) { out.push({ t: p.t.substring(cur - pos), c: p.c }); cur = end; }
        else {
          var s = Math.max(cur, r.a), e = Math.min(end, r.b);
          if (s > cur) out.push({ t: p.t.substring(cur - pos, s - pos), c: p.c });   // 命中前那一截
          out.push({ t: p.t.substring(s - pos, e - pos), c: (p.c ? p.c + " " : "") + r.cls });
          cur = e;
        }
      }
      pos = end;
    }
    return out;
  }

  function renderGutter(nlines, marks) {
    var cur = caretPos().line;
    var h = [];
    for (var i = 1; i <= nlines; i++) {
      var m = marks[i - 1];
      h.push('<div class="' + (i === cur ? "cur " : "") + (m === "e" ? "has-err" : m === "w" ? "has-warn" : "") +
        '"><span class="mk">' + (m === "e" ? "●" : m === "w" ? "◌" : "") + '</span><span class="no">' +
        i + "</span></div>");
    }
    gutter.innerHTML = h.join("");
  }

  function syncScroll() {
    hl.scrollTop = ed.scrollTop; hl.scrollLeft = ed.scrollLeft;
    gutter.scrollTop = ed.scrollTop;
  }

  function updateStatus() {
    var p = caretPos();
    stPos.textContent = "行 " + p.line + "，列 " + p.col;
    var dirty = ed.value !== S.saved;
    dirtyDot.classList.toggle("hidden", !dirty);
    document.title = (dirty ? "● " : "") + S.path + " — FA IDE";
    filelabel.textContent = S.path;
    stFile.textContent = S.path;
    var errs = S.diags.filter(function (d) { return d.severity === 1; }).length;
    var warns = S.diags.length - errs;
    stDiags.className = errs ? "bad" : (warns ? "warny" : "ok");
    stDiags.textContent = errs ? "✗ " + errs + " 个错误" + (warns ? "，" + warns + " 个警告" : "")
      : (warns ? "⚠ " + warns + " 个警告" : "✓ 没有问题");
    $("cnt-problems").textContent = String(S.diags.length);
  }

  // ---------------------------------------------------------------- 诊断
  var analyzeTimer = null;
  function scheduleAnalyze(delay) {
    if (analyzeTimer) clearTimeout(analyzeTimer);
    analyzeTimer = setTimeout(doAnalyze, delay === undefined ? 350 : delay);
  }

  function doAnalyze() {
    var seq = ++S.reqSeq;
    var src = ed.value, path = S.path;
    api("/api/analyze", { src: src, path: path }).then(function (r) {
      if (seq !== S.reqSeq && src !== ed.value) return;      // 已经又敲了字，丢掉这份
      S.diags = (r.diagnostics || []).map(function (d) {
        return { line: d.line, col: d.col, endCol: d.endCol, severity: d.severity, message: d.message, stage: d.stage || "" };
      });
      S.symbols = r.symbols || [];
      stTime.textContent = r.elapsedMs + " ms";
      render(); renderProblems(); renderOutline(); updateStatus();
    }).catch(function (e) { stTime.textContent = "后端没响应"; });
  }

  function renderProblems() {
    if (!S.diags.length) {
      problemsEl.innerHTML = '<div class="empty">✓ 没有问题。改一行试试：把某个 <code>fn main() -> i64:</code> 的冒号删掉，' +
        "或者在缩进里敲一个 Tab —— 这些编译器报得含糊的错，IDE 会直接点破。</div>";
      return;
    }
    var h = S.diags.map(function (d, i) {
      return '<div class="prob ' + (d.severity === 1 ? "e" : "w") + '" data-i="' + i + '">' +
        '<span class="sev">' + (d.severity === 1 ? "错误" : d.severity === 2 ? "警告" : "提示") + "</span>" +
        '<span class="msg">' + FaTokens.esc(d.message) + "</span>" +
        '<span class="stage">' + FaTokens.esc(d.stage) + "</span>" +
        '<span class="loc">' + d.line + ":" + d.col + "</span></div>";
    }).join("");
    problemsEl.innerHTML = h;
    Array.prototype.forEach.call(problemsEl.querySelectorAll(".prob"), function (el) {
      el.onclick = function () { gotoDiag(S.diags[+el.dataset.i]); };
    });
  }

  function gotoDiag(d) {
    if (!d) return;
    var off = offsetFromPos(ed.value, d.line, d.col);
    ed.focus();
    ed.setSelectionRange(off, off + Math.max(1, (d.endCol || d.col + 1) - d.col));
    scrollToCaret();
    updateStatus();
  }

  function scrollToCaret() {
    var p = caretPos();
    var lh = parseFloat(getComputedStyle(ed).lineHeight) || 21;
    var pad = parseFloat(getComputedStyle(ed).paddingTop) || 8;
    var y = pad + (p.line - 1) * lh;
    if (y < ed.scrollTop) ed.scrollTop = Math.max(0, y - lh * 3);
    else if (y + lh > ed.scrollTop + ed.clientHeight) ed.scrollTop = y - ed.clientHeight + lh * 3;
    syncScroll();
  }

  // ---------------------------------------------------------------- 大纲
  function renderOutline() {
    if (!S.symbols.length) { outlineEl.innerHTML = '<div class="out-row" style="color:var(--fg-faint)">（还没有声明）</div>'; return; }
    var ico = { "函数": "ƒ", "结构体": "▤", "枚举": "⊕", "方法组": "◎", "方法": "m", "常量": "C", "全局": "G", "导入": "⇢" };
    outlineEl.innerHTML = S.symbols.map(function (s) {
      return '<div class="out-row" data-line="' + s.line + '" title="' + FaTokens.esc(s.detail || "") + '">' +
        '<span class="ico">' + (ico[s.kind] || "·") + "</span>" +
        '<span class="nm">' + FaTokens.esc(s.name) + "</span>" +
        '<span class="ln">' + s.line + "</span></div>";
    }).join("");
    Array.prototype.forEach.call(outlineEl.querySelectorAll(".out-row[data-line]"), function (el) {
      el.onclick = function () { jump(+el.dataset.line, 1); };
    });
  }

  function jump(line, col) {
    var off = offsetFromPos(ed.value, line, col || 1);
    ed.focus(); ed.setSelectionRange(off, off);
    scrollToCaret(); updateStatus(); render();
  }

  // ---------------------------------------------------------------- 文件树
  function loadTree(path, into, depth) {
    api("/api/tree?path=" + encodeURIComponent(path || "")).then(function (r) {
      if (r.error) { into.innerHTML = '<div class="out-row">' + FaTokens.esc(r.error) + "</div>"; return; }
      (r.entries || []).forEach(function (e) {
        var row = document.createElement("div");
        row.className = "tree-row" + (e.dir ? " dir" : "") + (e.path === S.path ? " on" : "");
        row.style.paddingLeft = (6 + (depth || 0) * 12) + "px";
        row.innerHTML = '<span class="ico">' + (e.dir ? "▸" : "·") + '</span><span class="nm">' +
          FaTokens.esc(e.name) + "</span>" + (e.dir ? "" : '<span class="sz">' + fmtSize(e.size) + "</span>");
        into.appendChild(row);
        if (e.dir) {
          var kid = document.createElement("div");
          kid.className = "hidden";
          into.appendChild(kid);
          var opened = false;
          row.onclick = function () {
            kid.classList.toggle("hidden");
            row.querySelector(".ico").textContent = kid.classList.contains("hidden") ? "▸" : "▾";
            if (!opened) { opened = true; loadTree(e.path, kid, (depth || 0) + 1); }
          };
        } else {
          row.onclick = function () { openFile(e.path); };
        }
      });
    });
  }
  function fmtSize(n) { return n > 1024 * 1024 ? (n / 1048576).toFixed(1) + "M" : n > 1024 ? (n / 1024).toFixed(0) + "K" : n + "B"; }

  function refreshTree() { treeEl.innerHTML = ""; loadTree("", treeEl, 0); }

  function openFile(path) {
    if (ed.value !== S.saved) {
      if (!confirm("当前文件还没保存，确定要打开 " + path + " 吗？")) return;
    }
    api("/api/read", { path: path }).then(function (r) {
      if (r.error) { toast("打不开：" + r.error); return; }
      S.path = r.path || path; S.saved = r.src; ed.value = r.src;
      S.diags = []; closeAllFloats();
      render(); updateStatus(); refreshTree(); scheduleAnalyze(0);
    });
  }

  function saveFile() {
    var path = S.path;
    if (!path || path === "untitled.fa") {
      promptModal("保存为", "文件名（相对工作目录，会自动补 .fa）", "hello.fa", function (v) {
        if (v) { S.path = v; doSave(); }
      });
      return;
    }
    doSave();
  }
  function doSave() {
    api("/api/write", { path: S.path, src: ed.value }).then(function (r) {
      if (r.error) { toast("存不下：" + r.error); return; }
      S.saved = ed.value; S.path = r.path || S.path;
      updateStatus(); refreshTree(); toast("已保存 " + S.path);
    });
  }

  // ---------------------------------------------------------------- 补全
  function requestComplete(immediate) {
    var p = caretPos();
    var seq = ++S.reqSeq;
    var src = ed.value;
    var go = function () {
      api("/api/complete", { src: src, path: S.path, line: p.line, col: p.col }).then(function (r) {
        if (src !== ed.value) return;                       // 内容变了，这份作废
        S.items = r.items || [];
        S.allItems = S.items.slice();     // 客户端再过滤时要有全集可用
        S.popCtx = r.context || "";
        S.popWhy = r.why || "";
        S.sel = 0;
        if (S.items.length) showPopup(); else hidePopup();
      });
    };
    if (immediate) go();
    else { if (S.ctimer) clearTimeout(S.ctimer); S.ctimer = setTimeout(go, 130); }
  }

  var KIND_ICO = { 1: "·", 2: "m", 3: "ƒ", 5: "▤", 6: "m", 7: "·", 9: "⇢", 12: "v", 13: "v", 14: "C", 15: "⌘", 20: "⊕", 23: "▤" };
  var KIND_CLS = { 14: "kw", 5: "ty", 23: "ty", 3: "fn", 2: "fn", 6: "fn", 15: "sn", 13: "vr", 12: "vr", 20: "md", 9: "md" };

  function showPopup() {
    var h = "";
    var max = Math.min(S.items.length, 200);
    for (var i = 0; i < max; i++) {
      var it = S.items[i];
      h += '<div class="pi' + (i === S.sel ? " sel" : "") + '" data-i="' + i + '">' +
        '<span class="k ' + (KIND_CLS[it.kind] || "") + '">' + (KIND_ICO[it.kind] || "·") + "</span>" +
        '<span class="l">' + FaTokens.esc(it.label) + "</span>" +
        '<span class="d">' + FaTokens.esc((it.detail || "").replace(/\*\*/g, "")) + "</span></div>";
    }
    var ctx = { member: "成员补全", type: "类型名", use: "标准库模块", global: "全部" }[S.popCtx] || S.popCtx;
    h += '<div id="pop-ctx">' + FaTokens.esc(ctx) + "　" + S.items.length + " 项" +
      (S.popWhy ? "　· " + FaTokens.esc(S.popWhy) : "") + "　↑↓ 选 · Tab/Enter 插入 · Esc 关</div>";
    popup.innerHTML = h;
    popup.classList.remove("hidden");
    S.popOpen = true;
    placeAt(popup, caretXY());
    Array.prototype.forEach.call(popup.querySelectorAll(".pi"), function (el) {
      el.onmousedown = function (ev) { ev.preventDefault(); S.sel = +el.dataset.i; acceptComplete(); };
      el.onmouseenter = function () { S.sel = +el.dataset.i; markSel(); };
    });
    markSel();
  }
  function markSel() {
    var rows = popup.querySelectorAll(".pi");
    for (var i = 0; i < rows.length; i++) rows[i].classList.toggle("sel", i === S.sel);
    var r = rows[S.sel];
    if (r && r.scrollIntoView) r.scrollIntoView({ block: "nearest" });
  }
  function hidePopup() { popup.classList.add("hidden"); S.popOpen = false; }

  function placeAt(el, xy) {
    var wrap = $("editor-wrap").getBoundingClientRect();
    var x = xy.x - wrap.left, y = xy.y - wrap.top + xy.h + 2;
    el.style.left = "0px"; el.style.top = "0px"; el.style.visibility = "hidden";
    el.classList.remove("hidden");
    var w = el.offsetWidth, hh = el.offsetHeight;
    if (x + w > wrap.width - 8) x = Math.max(4, wrap.width - w - 8);
    if (y + hh > wrap.height - 8) y = Math.max(4, xy.y - wrap.top - hh - 2);
    el.style.left = x + "px"; el.style.top = y + "px"; el.style.visibility = "";
  }

  /* 光标的像素位置：拿一个跟 textarea 同字体的隐藏 div 复刻光标前的文本，
   * 末尾插一个 span 量它的偏移。这是叠加式编辑器唯一可靠的办法。 */
  function caretXY() {
    var ta = ed, pos = ta.selectionStart;
    var div = document.createElement("div");
    var cs = getComputedStyle(ta);
    ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight", "letterSpacing",
      "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "textIndent", "tabSize"
    ].forEach(function (p) { div.style[p] = cs[p]; });
    div.style.position = "absolute"; div.style.visibility = "hidden";
    div.style.whiteSpace = "pre"; div.style.top = "0"; div.style.left = "0";
    div.textContent = ta.value.substring(0, pos);
    var span = document.createElement("span");
    span.textContent = ta.value.substring(pos) || ".";
    div.appendChild(span);
    document.body.appendChild(div);
    var dr = div.getBoundingClientRect(), sr = span.getBoundingClientRect();
    var tr = ta.getBoundingClientRect();
    var x = tr.left + (sr.left - dr.left) - ta.scrollLeft;
    var y = tr.top + (sr.top - dr.top) - ta.scrollTop;
    var h = sr.height || parseFloat(cs.lineHeight) || 21;
    document.body.removeChild(div);
    return { x: x, y: y, h: h };
  }

  /* 鼠标像素 -> (行, 列)。走高亮层：每行一个 div，里面是 span，量 offsetLeft 就行。 */
  function pointToPos(clientX, clientY) {
    var rows = hl.querySelectorAll("div.ln");
    if (!rows.length) return null;
    var hr = hl.getBoundingClientRect();
    var cs = getComputedStyle(ed);
    var padTop = parseFloat(cs.paddingTop) || 8, padLeft = parseFloat(cs.paddingLeft) || 8;
    var lh = parseFloat(cs.lineHeight) || 21;
    var y = clientY - hr.top + hl.scrollTop - padTop;
    var line = Math.max(1, Math.min(rows.length, Math.floor(y / lh) + 1));
    var x = clientX - hr.left + hl.scrollLeft - padLeft;
    var row = rows[line - 1];
    var col = 1;
    if (row) {
      var kids = row.childNodes, acc = 0;
      for (var i = 0; i < kids.length; i++) {
        var k = kids[i];
        var w = (k.offsetWidth !== undefined && k.nodeName === "SPAN") ? k.offsetWidth : 0;
        if (k.nodeName === "SPAN") {
          if (x < acc + w) { col += Math.round((x - acc) / Math.max(1, w) * k.textContent.length); acc = x; break; }
          acc += w; col += k.textContent.length;
        } else {
          var t = k.textContent || "";
          col += t.length;                              // 文本节点没法量宽，按字符数估
        }
      }
    }
    return { line: line, col: Math.max(1, col) };
  }

  function acceptComplete() {
    var it = S.items[S.sel];
    if (!it) { hidePopup(); return; }
    var p = caretPos();
    var v = ed.value;
    // 把光标前已经敲的那截前缀替换掉
    var lineStart = offsetFromPos(ed.value, p.line, 1);
    var caret = ed.selectionStart;
    var before = v.substring(lineStart, caret);
    var m = before.match(/([A-Za-z_0-9]*)$/);
    var pre = m ? m[1] : "";
    var text = expandSnippet(it.insertText || it.label);
    var st = caret - pre.length;
    ed.value = v.substring(0, st) + text.body + v.substring(caret);
    var np = st + text.caret;
    ed.setSelectionRange(np, np);
    hidePopup();
    render(); updateStatus(); scheduleAnalyze(120);
  }

  /* 极简 snippet：${1:默认} 展开成默认值并把光标停在那儿，$0/$1 这种删掉。 */
  function expandSnippet(s) {
    var caret = s.length;
    var out = s.replace(/\$\{(\d+):([^}]*)\}/g, function (_m, _n, d) { return d; });
    var m = /\$\{(\d+):([^}]*)\}/.exec(s);
    if (m) caret = s.indexOf(m[0]) + m[2].length;
    out = out.replace(/\$\d+/g, "");
    if (!m) caret = out.length;
    return { body: out, caret: Math.min(caret, out.length) };
  }

  // ---------------------------------------------------------------- 悬停
  var hoverTimer = null;
  function scheduleHover(p) {
    if (hoverTimer) clearTimeout(hoverTimer);
    hoverTimer = setTimeout(function () {
      var src = ed.value;
      api("/api/hover", { src: src, path: S.path, line: p.line, col: p.col }).then(function (r) {
        if (!r.markdown || src !== ed.value) { hoverbox.classList.add("hidden"); return; }
        hoverbox.innerHTML = mdToHTML(r.markdown);
        hoverbox.classList.remove("hidden");
        placeAt(hoverbox, caretXY());
      });
    }, 260);
  }

  /* 后端给的 markdown 很简单（```fa 代码块、**粗体**、*斜体*、`行内码`、换行），
   * 不引第三方库，自己转就够了 —— 但必须先转义，别把源码里的 <> 当标签。 */
  function mdToHTML(md) {
    var parts = String(md).split(/```/);
    var h = "";
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        var body = parts[i].replace(/^[a-zA-Z]*\n/, "");
        h += "<pre>" + FaTokens.esc(body) + "</pre>";
      } else {
        var t = FaTokens.esc(parts[i]);
        t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
        t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
        t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
        h += t.split("\n").map(function (x) { return x.trim() ? "<div>" + x + "</div>" : ""; }).join("");
      }
    }
    return h;
  }

  // ---------------------------------------------------------------- 签名
  function requestSignature() {
    var p = caretPos(), src = ed.value;
    api("/api/signature", { src: src, path: S.path, line: p.line, col: p.col }).then(function (r) {
      if (!r || !r.label || src !== ed.value) { hideSig(); return; }
      var params = r.params || [];
      var lab = FaTokens.esc(r.label);
      // 把当前那个参数高亮出来（拿 params 里的名字去 label 里找）
      var act = r.active || 0;
      if (params.length) {
        var target = params[act];
        var re = new RegExp("(\\b" + target.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b)");
        lab = FaTokens.esc(r.label).replace(re, '<span class="p-on">$1</span>');
      }
      sighelp.innerHTML = "<div>" + lab + "</div>" +
        (r.doc ? '<div class="doc">' + FaTokens.esc(String(r.doc).replace(/\*\*/g, "")) + "</div>" : "");
      sighelp.classList.remove("hidden");
      S.sigOpen = true;
      placeAt(sighelp, caretXY());
    });
  }
  function hideSig() { sighelp.classList.add("hidden"); S.sigOpen = false; }
  function closeAllFloats() { hidePopup(); hideSig(); hoverbox.classList.add("hidden"); }

  // ---------------------------------------------------------------- 运行
  function runProgram() {
    if (S.running) return;
    if (S.info && !S.info.canRun) { toast(S.info.runBlockReason || "这台机器上跑不了，只能检查"); return; }
    S.running = true;
    $("btn-run").disabled = true;
    $("btn-run").textContent = "编译中…";
    showTab("output");
    outputEl.innerHTML = '<span class="o-dim">编译中…</span>';
    var args = S.args ? S.args.split(/\s+/).filter(Boolean) : [];
    var t0 = performance.now();
    api("/api/run", { src: ed.value, path: S.path, args: args, timeout: S.timeout, opt: S.opt, stdin: "" })
      .then(function (r) {
        var h = "";
        h += '<span class="o-dim">── ' + FaTokens.esc(S.path) + " ──" +
          (args.length ? " 参数: " + FaTokens.esc(args.join(" ")) : "") + "</span>\n";
        if (!r.compiled) {
          h += '<span class="o-err">编译没过：\n' + FaTokens.esc(r.stderr || "（没有更多信息）") + "</span>\n";
          if (r.diagnostics) {
            S.diags = r.diagnostics.map(function (d) {
              return { line: d.line, col: d.col, endCol: d.endCol, severity: d.severity, message: d.message, stage: d.stage || "" };
            });
            render(); renderProblems(); updateStatus();
          }
        } else {
          if (r.stdout) h += FaTokens.esc(r.stdout);
          if (r.stderr) h += '<span class="o-err">' + FaTokens.esc(r.stderr) + "</span>\n";
          if (r.buildLog) h += '<span class="o-dim">' + FaTokens.esc(r.buildLog) + "</span>\n";
          var wall = Math.round(performance.now() - t0);
          h += '<span class="' + (r.ok ? "o-ok" : "o-err") + '">── 退出码 ' +
            String(r.exitCode) + "，编译+运行共 " + r.elapsedMs + " ms（页面等了 " + wall + " ms）──</span>\n";
        }
        outputEl.innerHTML = h;
      })
      .catch(function (e) { outputEl.innerHTML = '<span class="o-err">请求失败：' + FaTokens.esc(String(e)) + "</span>"; })
      .then(function () {
        S.running = false; $("btn-run").disabled = false; $("btn-run").textContent = "▶ 运行";
      });
  }

  // ---------------------------------------------------------------- 面板/弹窗
  function showTab(name) {
    ["problems", "output", "cheat"].forEach(function (t) {
      $("tab-" + t).classList.toggle("hidden", t !== name);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#panel-tabs button[data-tab]"), function (b) {
      b.classList.toggle("on", b.dataset.tab === name);
    });
    $("panel").classList.remove("collapsed");
  }

  function toast(msg) {
    var t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText = "position:fixed;bottom:210px;left:50%;transform:translateX(-50%);" +
      "background:var(--bg3);border:1px solid var(--line);padding:6px 14px;border-radius:4px;" +
      "z-index:200;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.4)";
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 2200);
  }

  function promptModal(title, label, value, onOk) {
    $("modal-title").textContent = title;
    $("modal-body").innerHTML = "<label>" + FaTokens.esc(label) + '</label><input id="m-in" value="' +
      FaTokens.esc(value || "") + '">';
    $("modal-btns").innerHTML = '<button id="m-ok" class="primary">确定</button><button id="m-no">取消</button>';
    $("modal").classList.remove("hidden");
    var inp = $("m-in"); inp.focus(); inp.select();
    var done = function (ok) {
      $("modal").classList.add("hidden");
      if (ok) onOk(inp.value.trim());
    };
    $("m-ok").onclick = function () { done(true); };
    $("m-no").onclick = function () { done(false); };
    inp.onkeydown = function (e) {
      if (e.key === "Enter") done(true);
      if (e.key === "Escape") done(false);
    };
  }

  function renderCheat() {
    api("/api/cheat").then(function (r) {
      var h = "";
      function sect(title, obj, limit) {
        var ks = Object.keys(obj || {});
        if (limit) ks = ks.slice(0, limit);
        h += "<h4>" + FaTokens.esc(title) + "（" + ks.length + "）</h4><div class='row'>";
        ks.forEach(function (k) {
          h += '<span class="chip" title="' + FaTokens.esc(String(obj[k]).replace(/\*\*/g, "")) + '">' +
            FaTokens.esc(k) + "</span>";
        });
        h += "</div>";
      }
      sect("类型", arrToObj(r.types || []));
      sect("关键字", arrToObj(r.keywords || []));
      sect("内建函数", r.builtins || {});
      sect("str 的方法", r.str || {});
      sect("Vec 的方法", r.vec || {});
      sect("Map 的方法", r.map || {});
      sect("数字的方法", r.num || {});
      cheatEl.innerHTML = h || "（后端没给速查表）";
    });
  }
  function arrToObj(a) { var o = {}; (a || []).forEach(function (x) { o[x] = ""; }); return o; }

  // ---------------------------------------------------------------- 编辑动作
  function insertText(t) {
    var s = ed.selectionStart, e = ed.selectionEnd;
    ed.value = ed.value.substring(0, s) + t + ed.value.substring(e);
    ed.setSelectionRange(s + t.length, s + t.length);
    onEdit();
  }

  function onEdit() {
    render(); updateStatus(); scheduleAnalyze();
  }

  function indentSelection(delta) {
    var v = ed.value, s = ed.selectionStart, e = ed.selectionEnd;
    var ls = v.lastIndexOf("\n", s - 1) + 1;
    var le = v.indexOf("\n", e); if (le < 0) le = v.length;
    var block = v.substring(ls, le);
    var out = block.split("\n").map(function (ln) {
      if (delta > 0) return "    " + ln;
      return ln.replace(/^ {1,4}/, "");
    }).join("\n");
    ed.value = v.substring(0, ls) + out + v.substring(le);
    ed.setSelectionRange(ls, ls + out.length);
    onEdit();
  }

  function toggleComment() {
    var v = ed.value, s = ed.selectionStart, e = ed.selectionEnd;
    var ls = v.lastIndexOf("\n", s - 1) + 1;
    var le = v.indexOf("\n", e); if (le < 0) le = v.length;
    var lines = v.substring(ls, le).split("\n");
    var allOn = lines.every(function (l) { return !l.trim() || /^\s*#/.test(l); });
    var out = lines.map(function (l) {
      if (!l.trim()) return l;
      if (allOn) return l.replace(/^(\s*)#\s?/, "$1");
      return l.replace(/^(\s*)/, "$1# ");
    }).join("\n");
    ed.value = v.substring(0, ls) + out + v.substring(le);
    ed.setSelectionRange(ls, ls + out.length);
    onEdit();
  }

  // ---------------------------------------------------------------- 事件
  ed.addEventListener("input", function () {
    onEdit();
    var p = caretPos();
    var before = ed.value.substring(offsetFromPos(ed.value, p.line, 1), ed.selectionStart);
    if (/\.$/.test(before) || /\buse\s+(std\.)?$/.test(before) || /->\s*$/.test(before) || /:\s*$/.test(before)) {
      requestComplete(true);
    } else if (/[A-Za-z_0-9]$/.test(before)) {
      if (S.popOpen) { filterPopup(before.match(/([A-Za-z_0-9]*)$/)[1]); }
      else requestComplete(false);
    } else if (S.popOpen && !/[A-Za-z_0-9.]$/.test(before)) {
      hidePopup();
    }
    if (/\($/.test(before) || /,\s*$/.test(before) || /\{$/.test(before)) requestSignature();
    else if (/\)$/.test(before)) hideSig();
  });

  function filterPopup(pre) {
    var low = pre.toLowerCase();
    if (S.allItems === undefined) S.allItems = S.items;
    S.items = low ? S.allItems.filter(function (i) { return i.label.toLowerCase().indexOf(low) === 0; }) : S.allItems.slice();
    if (!S.items.length) { S.items = S.allItems.slice(); }
    S.sel = 0; showPopup();
  }

  ed.addEventListener("keydown", function (e) {
    var mod = e.ctrlKey || e.metaKey;
    // 补全弹窗开着时，方向键/Tab/Enter 归弹窗
    if (S.popOpen) {
      if (e.key === "ArrowDown") { e.preventDefault(); S.sel = (S.sel + 1) % S.items.length; markSel(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); S.sel = (S.sel - 1 + S.items.length) % S.items.length; markSel(); return; }
      if (e.key === "Tab" || e.key === "Enter") { e.preventDefault(); acceptComplete(); return; }
      if (e.key === "Escape") { e.preventDefault(); hidePopup(); return; }
      if (e.key === "PageDown") { e.preventDefault(); S.sel = Math.min(S.items.length - 1, S.sel + 8); markSel(); return; }
      if (e.key === "PageUp") { e.preventDefault(); S.sel = Math.max(0, S.sel - 8); markSel(); return; }
    }
    if (e.key === "Escape") { closeAllFloats(); return; }
    if (e.key === "Tab") {
      e.preventDefault();
      if (ed.selectionStart !== ed.selectionEnd) indentSelection(e.shiftKey ? -1 : 1);
      else insertText("    ");                        // 永远插空格：FA 对缩进敏感，Tab 会算错层级
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      // 自动缩进：抄上一行的；上一行以 : 或 { 收尾就再加一级
      var v = ed.value, s = ed.selectionStart;
      if (s !== ed.selectionEnd) return;               // 有选区就走默认替换
      var ls = v.lastIndexOf("\n", s - 1) + 1;
      var cur = v.substring(ls, s);
      var m = cur.match(/^([ ]*)/);
      var ind = m ? m[1] : "";
      var t = cur.trim();
      if (/[:{]$/.test(t)) ind += "    ";
      else if (!t) { e.preventDefault(); ed.setSelectionRange(ls, s); insertText("\n"); return; }
      e.preventDefault();
      insertText("\n" + ind);
      return;
    }
    if (mod && e.key === " ") { e.preventDefault(); S.allItems = undefined; requestComplete(true); return; }
    if (mod && (e.key === "s" || e.key === "S")) { e.preventDefault(); saveFile(); return; }
    if (e.key === "F5" || (mod && e.key === "Enter")) { e.preventDefault(); runProgram(); return; }
    if (mod && e.key === "/") { e.preventDefault(); toggleComment(); return; }
    if (e.key === "F12" || (mod && e.altKey)) { e.preventDefault(); gotoDef(); return; }
    if (mod && e.key === "g") { e.preventDefault(); doAnalyze(); return; }
    if (e.key === "F1" || (mod && e.shiftKey && (e.key === "?" || e.key === "/"))) {
      e.preventDefault(); showTab("cheat"); return;
    }
  });

  ed.addEventListener("keyup", function (e) {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].indexOf(e.key) >= 0) {
      hidePopup(); updateStatus(); render();
      if (S.sigOpen) requestSignature();
    }
  });
  ed.addEventListener("click", function () { hidePopup(); updateStatus(); render(); });
  ed.addEventListener("scroll", syncScroll);
  ed.addEventListener("blur", function () { setTimeout(function () { if (!popup.contains(document.activeElement)) hidePopup(); }, 120); });

  // Ctrl + 鼠标移动 -> 悬停文档；Ctrl + 点击 -> 跳定义
  ed.addEventListener("mousemove", function (e) {
    if (!(e.ctrlKey || e.metaKey)) { hoverbox.classList.add("hidden"); return; }
    var p = pointToPos(e.clientX, e.clientY);
    if (p) scheduleHover(p);
  });
  ed.addEventListener("mouseleave", function () { hoverbox.classList.add("hidden"); });
  $("editor-wrap").addEventListener("mousedown", function (e) {
    if (!(e.ctrlKey || e.metaKey)) return;
    var p = pointToPos(e.clientX, e.clientY);
    if (!p) return;
    e.preventDefault();
    var off = offsetFromPos(ed.value, p.line, p.col);
    ed.setSelectionRange(off, off);
    gotoDef();
  });

  function gotoDef() {
    var p = caretPos(), src = ed.value;
    api("/api/definition", { src: src, path: S.path, line: p.line, col: p.col }).then(function (r) {
      if (!r || (!r.line && !r.path)) { toast("这里没有可跳的定义"); return; }
      if (r.path && r.path !== S.path) {
        api("/api/read", { path: r.path }).then(function (rr) {
          if (rr.error) { toast("跳不过去：" + rr.error); return; }
          S.path = rr.path; S.saved = rr.src; ed.value = rr.src;
          render(); updateStatus(); refreshTree(); scheduleAnalyze(0);
          setTimeout(function () { jump(r.line, r.col || 1); }, 30);
        });
      } else {
        jump(r.line, r.col || 1);
      }
    });
  }

  // ---------------------------------------------------------------- 按钮
  $("btn-save").onclick = saveFile;
  $("btn-run").onclick = runProgram;
  $("btn-check").onclick = function () { showTab("problems"); doAnalyze(); };
  $("btn-refresh").onclick = refreshTree;
  $("btn-newfile").onclick = $("btn-new").onclick = function () {
    promptModal("新建文件", "文件名（相对工作目录，会自动补 .fa）", "hello.fa", function (v) {
      if (!v) return;
      api("/api/new", { path: v }).then(function (r) {
        if (r.error) { toast(r.error); return; }
        refreshTree(); openFile(r.path);
      });
    });
  };
  $("btn-open").onclick = function () {
    promptModal("打开文件", "路径（相对工作目录）", "", function (v) { if (v) openFile(v); });
  };
  $("btn-theme").onclick = function () {
    var dark = document.body.classList.toggle("dark");
    document.body.classList.toggle("light", !dark);
    save("theme", dark ? "dark" : "light");
  };
  $("btn-runargs").onclick = function () {
    $("modal-title").textContent = "运行设置";
    $("modal-body").innerHTML =
      "<label>程序参数（空格分开，传给 main 之前的 argv）</label><input id='r-args' value='" + FaTokens.esc(S.args) + "'>" +
      "<label>超时（秒，1–120；死循环会被杀掉）</label><input id='r-to' type='number' min='1' max='120' value='" + S.timeout + "'>" +
      "<label>优化级别 -O（0–3）</label><input id='r-opt' type='number' min='0' max='3' value='" + S.opt + "'>" +
      "<div class='tip'>运行走的是真编译器：先 <code>fa check</code> 那一套（parse + Sema + codegen），" +
      "过了才生成汇编、调 gcc 链接，然后跑出来的程序，stdout/stderr 都收回来显示在下面。</div>";
    $("modal-btns").innerHTML = '<button id="m-ok" class="primary">好</button><button id="m-no">取消</button>';
    $("modal").classList.remove("hidden");
    $("m-ok").onclick = function () {
      S.args = $("r-args").value; S.timeout = +$("r-to").value || 10; S.opt = +$("r-opt").value || 2;
      save("run", { args: S.args, timeout: S.timeout, opt: S.opt });
      $("modal").classList.add("hidden");
    };
    $("m-no").onclick = function () { $("modal").classList.add("hidden"); };
  };
  Array.prototype.forEach.call(document.querySelectorAll("#panel-tabs button[data-tab]"), function (b) {
    b.onclick = function () { showTab(b.dataset.tab); if (b.dataset.tab === "cheat" && !cheatEl.innerHTML) renderCheat(); };
  });
  $("panel-toggle").onclick = function () {
    var p = $("panel"); p.classList.toggle("collapsed");
    $("panel-toggle").textContent = p.classList.contains("collapsed") ? "▴" : "▾";
  };

  // 侧栏宽度拖动
  (function () {
    var sp = $("splitter"), sb = $("sidebar"), dragging = false;
    sp.addEventListener("mousedown", function (e) { dragging = true; sp.classList.add("drag"); e.preventDefault(); });
    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var w = Math.max(140, Math.min(520, e.clientX));
      sb.style.flexBasis = w + "px"; sb.style.width = w + "px";
    });
    window.addEventListener("mouseup", function () { if (dragging) { dragging = false; sp.classList.remove("drag"); } });
  })();

  window.addEventListener("resize", function () { if (S.popOpen) placeAt(popup, caretXY()); });
  window.addEventListener("beforeunload", function (e) {
    if (ed.value !== S.saved) { e.preventDefault(); e.returnValue = ""; }
  });

  // ---------------------------------------------------------------- 启动
  function boot() {
    var theme = load("theme", "dark");
    document.body.classList.toggle("dark", theme === "dark");
    document.body.classList.toggle("light", theme !== "dark");
    var rs = load("run", { args: "", timeout: 10, opt: 2 });
    S.args = rs.args; S.timeout = rs.timeout; S.opt = rs.opt;

    api("/api/info").then(function (info) {
      S.info = info;
      FaTokens.configure({ keywords: info.keywords, types: info.types, builtins: info.builtins });
      if (!info.canRun) {
        // 不能真跑就别假装能跑：按钮变灰，原因写在 tooltip 和输出面板里
        var why = info.runBlockReason || (info.canCompile ? "" : "没找到 C 编译器（gcc/clang）");
        $("btn-run").disabled = true;
        $("btn-run").title = why || "这台机器上跑不了";
        $("st-mode").textContent = "FA（只检查）";
        outputEl.innerHTML = '<span class="o-dim">' + FaTokens.esc(why || "") + "</span>";
      }
      var last = load("last", "");
      refreshTree();
      renderCheat();
      if (last) { openFile(last); } else {
        ed.value = WELCOME; S.saved = WELCOME; S.path = "untitled.fa";
        render(); updateStatus(); scheduleAnalyze(0);
      }
    }).catch(function () {
      outputEl.textContent = "后端没起来。这个页面必须由 fa ide / python3 ide/server.py 提供，直接双击打开 index.html 是不行的。";
      showTab("output");
    });
  }

  // 打开文件时记一下，下次接着用
  var _openFile = openFile;
  openFile = function (p) { save("last", p); _openFile(p); };

  ed.addEventListener("change", function () { S.allItems = undefined; });

  var WELCOME = [
    "# 欢迎用 FA IDE。这个文件还没存盘，Ctrl+S 会问你存成什么名字。",
    "# F5 编译并运行；Ctrl+Space 补全；Ctrl+鼠标移动看文档；F12 跳定义。",
    "# 下面的代码是可以直接跑的：",
    "",
    "struct Person:",
    "    name: str",
    "    age: i64 = 0",
    "",
    "fn is_adult(p: *Person) -> bool: return p.age >= 18",
    "fn double(x: *i64) -> i64: return (*x) * 2",
    "",
    "fn main() -> i64:",
    "    let ps = Vec<Person>[Person{name: \"甲\", age: 30}, Person{name: \"乙\", age: 12}]",
    "    for p in ps.filter(is_adult):",
    "        print(p.name, \" 成年了\")",
    "    let n = Vec<i64>[1, 2, 3, 4]",
    "    print(\"翻倍: \", n.map(double).to_str(), \"　求和: \", n.sum())",
    "    return 0",
    ""
  ].join("\n");

  // 纯函数挂出去：ide/static/test_frontend.js 用 node 直接跑这些断言，
  // 不用起浏览器也能守住「高亮切片 / snippet 展开 / markdown 渲染 / 位置换算」这几块
  (typeof window !== "undefined" ? window : globalThis).FaIDEPure = {
    applyDiags: applyDiags, expandSnippet: expandSnippet, mdToHTML: mdToHTML,
    posFromOffset: posFromOffset, offsetFromPos: offsetFromPos
  };

  boot();
})();
