#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 editors/vscode-fa 打成 .vsix（VSCode 扩展安装包），并当场验证。

    python3 packaging/build_vsix.py

.vsix 其实就是个 zip，里面必须有 [Content_Types].xml 和 extension.vsixmanifest，
外加把扩展文件放在 extension/ 前缀下。这样 `code --install-extension x.vsix` 才认。
这里不依赖 vsce（那要装 node 包、要联网、还要一堆元数据文件），直接按规范拼。

验证做的事：
  1. 先跑扩展自带的测试（88 条）和语言内核的测试（87 + 15 条）；
  2. 解包出来，逐个 JSON 用 json.loads 过一遍（tmLanguage、snippets、
     language-configuration、package.json 都是 VSCode 直接读的，坏一个就不高亮）；
  3. 拿 package.json 里声明的每个 contributes 路径，去包里找对应文件，缺了就报；
  4. 把生成的 tmLanguage 拿到编译器词法分析器上对拍：关键字表必须和编译器一致
     （不一致就是 gen_grammar.py 忘了重跑，语法高亮会漏词）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.path.join(ROOT, "editors", "vscode-fa")
DIST = os.path.join(ROOT, "dist")
sys.path.insert(0, os.path.join(ROOT, "compiler"))
sys.path.insert(0, os.path.join(ROOT, "lsp"))
sys.path.insert(0, HERE)
from version import VERSION as FA_VERSION, extension_version, align_extension_version  # noqa: E402
from falang.lexer import KEYWORDS                  # noqa: E402
from falang.types import TYPES                     # noqa: E402

# 扩展目录里这些不进包
SKIP_DIRS = {".git", "__pycache__", "node_modules", "test", ".vscode-test"}
SKIP_FILES = {".DS_Store"}

FAILS = []
N = [0]


def ck(label, cond, msg=""):
    N[0] += 1
    if cond:
        print(f"  ✓ {label}")
        return True
    FAILS.append(f"{label}　{msg}")
    print(f"  ✗ {label}　{msg}")
    return False


def read_manifest():
    with open(os.path.join(EXT, "package.json"), encoding="utf-8") as fh:
        return json.load(fh)


CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="md" ContentType="text/markdown"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="txt" ContentType="text/plain"/>
  <Default Extension="fa" ContentType="text/plain"/>
</Types>
"""


def vsixmanifest(mf, publisher, ext_id):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dn = mf.get("displayName") or mf["name"]
    desc = (mf.get("description") or "").replace("&", "&amp;").replace("<", "&lt;")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0"
  xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"
  xmlns:d="http://schemas.microsoft.com/developer/vsx-schema/2011/design">
  <Metadata>
    <Identity Language="en-US" Id="{ext_id}" Version="{mf['version']}" Publisher="{publisher}"/>
    <DisplayName>{dn}</DisplayName>
    <Description xml:space="preserve">{desc}</Description>
    <Tags>{",".join(mf.get("keywords", [])[:8]) or "fa,language,lsp"}</Tags>
    <Categories>{",".join(mf.get("categories", [])[:3]) or "Programming Languages"}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Services.Links.Source" Value="{mf.get('repository', {}).get('url', '')}"/>
      <Property Id="Microsoft.VisualStudio.Services.Content.Details" Value="{desc}"/>
    </Properties>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" Version="[1.75.0,)"/>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def collect_files():
    out = []
    for root, dirs, files in os.walk(EXT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if f in SKIP_FILES:
                continue
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, EXT).replace(os.sep, "/")
            out.append((fp, rel))
    return out


def run_tests():
    print("先把测试跑绿，再打包\n")
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, os.path.join(EXT, "test", "extension.test.js")],
                           capture_output=True, text=True, timeout=300, cwd=ROOT)
        ok = r.returncode == 0
        m = re.search(r"通过 (\d+) 条", r.stdout)
        ck(f"VSCode 扩展自测（{m.group(1) if m else '?'} 条）全绿", ok,
           (r.stdout + r.stderr)[-300:])
    else:
        print("  ! 没有 node，跳过扩展自测")

    py = sys.executable
    r = subprocess.run([py, os.path.join(ROOT, "lsp", "test_fa_lang.py")],
                       capture_output=True, text=True, timeout=900, cwd=ROOT)
    m = re.search(r"通过 (\d+) 条", r.stdout)
    ck(f"语言内核自测（{m.group(1) if m else '?'} 条）全绿", r.returncode == 0,
       (r.stdout + r.stderr)[-300:])

    r = subprocess.run([py, os.path.join(ROOT, "lsp", "fa_lsp.py"), "--selftest"],
                       capture_output=True, text=True, timeout=900, cwd=ROOT)
    ck("LSP 协议自检全绿",
       r.returncode == 0 and "LSP 自检全部通过" in r.stdout,
       (r.stdout + r.stderr)[-300:])


def build(vsix_path):
    mf = read_manifest()
    publisher = mf.get("publisher") or "fa-lang"
    ext_id = mf["name"]
    files = collect_files()

    if os.path.exists(vsix_path):
        os.remove(vsix_path)
    with zipfile.ZipFile(vsix_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("extension.vsixmanifest", vsixmanifest(mf, publisher, ext_id))
        for fp, rel in files:
            zf.write(fp, "extension/" + rel)
    return mf, files


def verify(vsix_path, mf, files):
    print(f"\n验证 {os.path.basename(vsix_path)}")
    with zipfile.ZipFile(vsix_path) as zf:
        names = zf.namelist()
        bad = zf.testzip()
        ck("zip 没有坏条目", bad is None, bad or "")
        ck("有 [Content_Types].xml", "[Content_Types].xml" in names, "")
        ck("有 extension.vsixmanifest", "extension.vsixmanifest" in names, "")
        ck("扩展文件都在 extension/ 前缀下",
           all(n.startswith("extension/") for n in names
               if n not in ("[Content_Types].xml", "extension.vsixmanifest")), "")

        # manifest 自洽
        mani = zf.read("extension.vsixmanifest").decode("utf-8")
        ck("manifest 里的版本号和 package.json 一致",
           f'Version="{mf["version"]}"' in mani, mf["version"])
        ck("manifest 指向了 extension/package.json",
           'Path="extension/package.json"' in mani, "")
        pk = json.loads(zf.read("extension/package.json").decode("utf-8"))
        ck("包里的 package.json 能解析", pk.get("name") == mf["name"], "")

        # contributes 里声明的每个文件都得真在包里
        contrib = pk.get("contributes", {})
        declared = []
        for lang in contrib.get("languages", []):
            declared.append(("language.configuration", lang.get("configuration")))
        for g in contrib.get("grammars", []):
            declared.append(("grammar.path", g.get("path")))
        for key, p in declared:
            if not p:
                ck(f"contributes 里有 {key}", False, "空值")
                continue
            rel = p[2:] if p.startswith("./") else p
            ck(f"contributes 声明的 {rel} 在包里", "extension/" + rel in names, p)

        # 每个 JSON 都要能解析
        for n in names:
            if n.endswith(".json") and n.startswith("extension/"):
                try:
                    json.loads(zf.read(n).decode("utf-8"))
                    ck(f"JSON 合法：{n[len('extension/'):]}", True)
                except Exception as exc:
                    ck(f"JSON 合法：{n[len('extension/'):]}", False, str(exc))

        # 主入口在包里
        ck("主入口 extension.js 在包里", "extension/extension.js" in names, "")
        # 测试目录不该进包
        ck("test/ 没被打进包", not any("/test/" in n for n in names), "")

        # 语法高亮的词表必须和编译器一致（漏了就是 gen_grammar.py 没重跑）
        tm = json.loads(zf.read("extension/syntaxes/fa.tmLanguage.json").decode("utf-8"))
        blob = json.dumps(tm, ensure_ascii=False)
        missing = [k for k in sorted(KEYWORDS) if not re.search(rf"\b{k}\b", blob)]
        ck("tmLanguage 覆盖了编译器全部关键字", not missing, f"漏了 {missing}")
        want_types = sorted(set(TYPES) | {"Vec", "Map"})
        missing_t = [t for t in want_types if not re.search(rf"\b{re.escape(t)}\b", blob)]
        ck("tmLanguage 覆盖了编译器全部内建类型名", not missing_t, f"漏了 {missing_t}")
        ck("tmLanguage 的 scopeName 是 source.fa",
           tm.get("scopeName") == "source.fa", tm.get("scopeName"))

        # 代码片段
        sn = json.loads(zf.read("extension/snippets/fa.json").decode("utf-8"))
        ck("snippets 数量对得上（>=14）", len(sn) >= 14, len(sn))
        ck("每个 snippet 都有 prefix 和 body",
           all(v.get("prefix") and v.get("body") for v in sn.values()), "")

    ck("打出来的文件数和源目录一致",
       len([n for n in names if n.startswith("extension/")]) == len(files),
       f"包里 {len([n for n in names if n.startswith('extension/')])} 源目录 {len(files)}")


def main():
    os.makedirs(DIST, exist_ok=True)
    version = FA_VERSION
    mf_version = read_manifest()["version"]
    ck("package.json 的版本和编译器版本一致", mf_version == version,
       f"扩展 {mf_version} 编译器 {version}")
    if mf_version != version:
        print("  ! 版本号不一致，用编译器的版本号打包（并提示改 package.json）")

    print(f"FA VSCode 扩展打包　版本 {version}\n")
    run_tests()

    vsix = os.path.join(DIST, f"fa-vscode-{version}.vsix")
    # 用编译器的版本号覆盖后再打（保持和 fa version 对得上），打完还原
    tmp_pkg = os.path.join(EXT, "package.json")
    with open(tmp_pkg, encoding="utf-8") as fh:
        orig = fh.read()
    if mf_version != version:
        with open(tmp_pkg, "w", encoding="utf-8") as fh:
            fh.write(orig.replace(f'"version": "{mf_version}"',
                                  f'"version": "{version}"', 1))
    try:
        mf2, files = build(vsix)
        size = os.path.getsize(vsix)
        print(f"  ✓ 打出 {vsix}（{size // 1024} KB，{len(files)} 个文件）")
        verify(vsix, mf2, files)
    finally:
        if mf_version != version:
            with open(tmp_pkg, "w", encoding="utf-8") as fh:
                fh.write(orig)

    print("\n通过 {} 条，失败 {} 条".format(N[0] - len(FAILS), len(FAILS)))
    for f in FAILS:
        print("  ✗", f)
    print(f"\n装法：code --install-extension {os.path.relpath(vsix, ROOT)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
