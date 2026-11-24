"""Build a trilingual static docs page for GitHub Pages.

Reads README.md / README.zh.md / README.fr.md, renders each through the same
Markdown converter the local /docs route uses, and writes one self-contained
docs/index.html with client-side language tabs (no jump to another file when
you switch language).

Usage:  .\\.venv\\Scripts\\python.exe tools\\build_docs.py
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app_web  # noqa: E402

LANGS = (("en", "English", "README.md"), ("zh", "中文", "README.zh.md"), ("fr", "Français", "README.fr.md"))

REPO_BLOB = "https://github.com/NexPathArchitect/mon-brain/blob/main/"
# README links point at sibling .md files; those do not exist on the Pages site,
# so rewrite them to the GitHub blob URLs (the /docs route does the same).
LINK_FIX = {
    'href="README.md"': f'href="{REPO_BLOB}README.md"',
    'href="README.zh.md"': f'href="{REPO_BLOB}README.zh.md"',
    'href="README.fr.md"': f'href="{REPO_BLOB}README.fr.md"',
}


def fix_links(html: str) -> str:
    for old, new in LINK_FIX.items():
        html = html.replace(old, new)
    return html

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f1f5f9;color:#1e293b;font:15px/1.75 -apple-system,"Segoe UI","Noto Sans SC",sans-serif}
.bar{position:sticky;top:0;z-index:10;background:#0f172a;color:#fff;display:flex;gap:14px;align-items:center;
     justify-content:space-between;padding:12px 20px;flex-wrap:wrap}
.bar b{font-size:15px}
.bar .sub{opacity:.55;font-size:12px;margin-left:8px}
.tabs{display:flex;gap:6px}
.tab{background:rgba(255,255,255,.12);border:0;color:#fff;font:inherit;font-size:13px;padding:5px 12px;
     border-radius:7px;cursor:pointer}
.tab.on{background:#0e7490}
.bar a{color:#fff;font-size:13px;text-decoration:none;opacity:.9}
.wrap{max-width:920px;margin:0 auto;padding:0 16px 72px}
.doc{background:#fff;border-radius:14px;padding:10px 26px 36px;margin-top:18px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1,h2{color:#0f172a;border-bottom:1px solid #e2e8f0;padding-bottom:6px}
h3,h4{color:#0f172a;margin:26px 0 8px}
code{background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;padding:1px 4px;font-size:13px}
pre{background:#0f172a;color:#d7fbe8;padding:12px;border-radius:10px;overflow:auto;font-size:13px;line-height:1.6}
pre code{background:none;border:0;color:inherit;padding:0}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}
th,td{border:1px solid #e2e8f0;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f8fafc}
ul,ol{padding-left:20px} li{margin:3px 0}
li.sub{margin-left:22px;font-size:14px}
a{color:#0e7490}
[hidden]{display:none}
"""

JS = """
(function(){
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
  var docs = Array.prototype.slice.call(document.querySelectorAll('.doc'));
  function show(code){
    tabs.forEach(function(t){ t.classList.toggle('on', t.dataset.lang === code); });
    docs.forEach(function(d){ d.hidden = (d.dataset.lang !== code); });
    try { history.replaceState(null, '', '#' + code); } catch (e) {}
  }
  tabs.forEach(function(t){ t.addEventListener('click', function(){ show(t.dataset.lang); }); });
  var want = (location.hash || '').replace('#','');
  var valid = docs.some(function(d){ return d.dataset.lang === want; });
  show(valid ? want : 'en');
})();
"""


def build() -> int:
    sections, buttons = [], []
    for code, label, fname in LANGS:
        path = ROOT / fname
        if not path.exists():
            print(f"missing {fname}")
            return 1
        html = fix_links(app_web.md_to_html(path.read_text(encoding="utf-8")))
        sections.append(f'<div class="doc" data-lang="{code}" hidden>\n{html}\n</div>')
        buttons.append(f'<button class="tab" data-lang="{code}">{label}</button>')

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mon_brain · 使用说明 / Docs</title>
<meta name="description" content="Local, offline note brain on Ollama: organize / translate-summary / review. Data never leaves your machine.">
<style>{CSS}</style></head>
<body>
<div class="bar">
  <div><b>mon_brain</b><span class="sub">使用说明 · Docs · Documentation</span></div>
  <div class="tabs">{''.join(buttons)}</div>
  <a href="https://github.com/NexPathArchitect/mon-brain">← 仓库 / Repository</a>
</div>
<div class="wrap">
{''.join(sections)}
</div>
<script>{JS}</script>
</body></html>"""

    out_dir = ROOT / "docs"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote docs/index.html ({len(page)} chars, {page.count('<table>')} tables, "
          f"{page.count('<pre>')} code blocks across {len(LANGS)} languages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
