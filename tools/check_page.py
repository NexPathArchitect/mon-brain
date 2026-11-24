"""Static checks for the single-page Web UI.

Run before every commit that touches app_web.py:

    .\\.venv\\Scripts\\python.exe tools\\check_page.py

Three checks, all cheap and deterministic:
  1. the inline <script> must be syntactically valid JS (needs `node`);
  2. every `data-i18n` / `data-i18n-ph` key must exist in the en / zh / fr dicts,
     otherwise switching language leaves raw key names on screen;
  3. every `document.getElementById("x")` must match an `id="x"` in the HTML,
     otherwise the listener silently binds to null.

Exit code 0 = all good, 1 = at least one problem (printed with the offending line).
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app_web  # noqa: E402  (import after sys.path tweak; module is side-effect free)

PAGE = app_web.PAGE


def inline_script() -> str:
    """Extract the main inline script (the one that defines setLang)."""
    anchor = PAGE.index("function setLang")
    start = PAGE.rindex("<script", 0, anchor)
    end = PAGE.index("</script>", anchor)
    tag_end = PAGE.index(">", start)
    return PAGE[tag_end + 1:end]


def check_js_syntax(js: str) -> list[str]:
    node = shutil.which("node")
    if not node:
        print("SKIP  JS syntax: node not found, cannot validate")
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        path = f.name
    try:
        proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
        if proc.returncode == 0:
            print("OK    JS syntax")
            return []
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        return ["JS syntax error: " + " / ".join(msg[:4])]
    finally:
        Path(path).unlink(missing_ok=True)


def check_i18n(js: str) -> list[str]:
    keys = set(re.findall(r'data-i18n="([^"]+)"', PAGE)) | set(
        re.findall(r'data-i18n-ph="([^"]+)"', PAGE)
    )
    i, j = js.index("const I18N = {"), js.index("let UI =")
    block = js[i:j]
    problems = []
    for lang in ("en", "zh", "fr"):
        m = re.search(lang + r":\s*\{(.*?)\}(?:,|\n\s*\})", block, re.S)
        defined = set(re.findall(r'([A-Za-z_]\w*)\s*:\s*"', m.group(1))) if m else set()
        missing = sorted(keys - defined)
        if missing:
            problems.append(f"{lang} dictionary missing keys: {', '.join(missing)}")
        else:
            print(f"OK    i18n {lang}: {len(keys)} keys covered")
    return problems


def check_tips(js: str) -> list[str]:
    """Every `data-tip` badge must have a TIPS entry with all three languages."""
    used = set(re.findall(r'data-tip="([^"]+)"', PAGE))
    m = re.search(r"const TIPS = \{(.*?)\n\};", js, re.S)
    block = m.group(1) if m else ""
    entries = {k: v for k, v in re.findall(r"([A-Za-z]\w*):\s*\{(.*?)\}", block, re.S)}
    problems = []
    for key in sorted(used):
        if key not in entries:
            problems.append(f"data-tip '{key}' has no TIPS entry")
            continue
        missing = [lg for lg in ("en", "zh", "fr") if not re.search(lg + r':\s*"', entries[key])]
        if missing:
            problems.append(f"TIPS['{key}'] missing language(s): {', '.join(missing)}")
    if not problems:
        print(f"OK    tooltips: {len(used)} badges, all trilingual")
    return problems


def check_guide(js: str) -> list[str]:
    """The collapsible guide must exist in all three languages and be substantial."""
    m = re.search(r"const GUIDE = \{(.*?)\n\};", js, re.S)
    if not m:
        return ["GUIDE object not found"]
    problems = []
    for lang in ("en", "zh", "fr"):
        body = re.search(lang + r":\s*'(.*?)'(?=,\n|\n|$)", m.group(1), re.S)
        if not body:
            problems.append(f"GUIDE.{lang} missing")
        elif len(body.group(1)) < 300:
            problems.append(f"GUIDE.{lang} suspiciously short ({len(body.group(1))} chars)")
    if not problems:
        print("OK    guide: en/zh/fr present")
    return problems


def check_docs() -> list[str]:
    """The /docs page renders the READMEs; make sure Markdown survives conversion."""
    problems = []
    for name, label in (("README.md", "zh"), ("README.en.md", "en"), ("README.fr.md", "fr")):
        path = ROOT / name
        if not path.exists():
            problems.append(f"{name} missing (docs page will fall back to placeholder)")
            continue
        html = app_web.md_to_html(path.read_text(encoding="utf-8"))
        outside_code = re.sub(r"<pre>.*?</pre>", "", html, flags=re.S)
        if outside_code.count("**"):
            problems.append(f"{label}: {outside_code.count('**')} unrendered '**' outside code blocks")
        if outside_code.count("<table>") < 1:
            problems.append(f"{label}: no table rendered (pipe tables broken?)")
        if html.count("<pre>") < 1:
            problems.append(f"{label}: no code block rendered")
        if not problems:
            print(f"OK    docs {label}: {len(html)} chars, "
                  f"{html.count('<table>')} tables, {html.count('<pre>')} code blocks")
    return problems


def check_offline_assets() -> list[str]:
    """The page must not depend on the network: Tailwind is vendored locally."""
    problems = []
    if "cdn.tailwindcss.com" in PAGE or "https://" in PAGE:
        problems.append("PAGE still references a remote URL (page would lose styling offline)")
    asset = ROOT / "static" / "tailwind.js"
    if not asset.exists():
        problems.append("static/tailwind.js missing (run tools/fetch_vendor.ps1)")
    elif asset.stat().st_size < 100_000:
        problems.append(f"static/tailwind.js looks truncated ({asset.stat().st_size} bytes)")
    if not problems:
        print(f"OK    offline assets: vendored tailwind ({asset.stat().st_size // 1024} KB)")
    return problems


def check_pages_doc() -> list[str]:
    """docs/index.html is what GitHub Pages serves; it must be regenerated and consistent."""
    problems = []
    index = ROOT / "docs" / "index.html"
    if not index.exists():
        return ["docs/index.html missing (run tools/build_docs.py)"]
    html = index.read_text(encoding="utf-8")
    for code in ("zh", "en", "fr"):
        if f'data-lang="{code}"' not in html:
            problems.append(f"docs/index.html has no {code} section")
    if html.count('<table>') < 3:
        problems.append(f"docs/index.html rendered only {html.count('<table>')} tables (Markdown conversion broken?)")
    # stale check: the page embeds the READMEs, so a size mismatch means it was not rebuilt
    expected = sum(len(app_web.md_to_html((ROOT / f).read_text(encoding="utf-8")))
                   for f in ("README.md", "README.en.md", "README.fr.md"))
    if len(html) < expected:
        problems.append("docs/index.html looks stale (rebuild with tools/build_docs.py)")
    if not problems:
        print(f"OK    pages doc: {len(html) // 1024} KB, 3 languages, {html.count('<table>')} tables")
    return problems


def check_element_ids(js: str) -> list[str]:
    ids = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    # ids that the script itself creates at runtime, not present in static HTML
    runtime_created = {"created"}
    missing = sorted(x for x in ids - runtime_created if f'id="{x}"' not in PAGE)
    if missing:
        return [f"getElementById target not in HTML: {', '.join(missing)}"]
    print(f"OK    element ids: {len(ids)} referenced, all present")
    return []


def main() -> int:
    js = inline_script()
    problems = []
    problems += check_js_syntax(js)
    problems += check_i18n(js)
    problems += check_tips(js)
    problems += check_guide(js)
    problems += check_docs()
    problems += check_offline_assets()
    problems += check_pages_doc()
    problems += check_element_ids(js)
    if problems:
        print("\nFAILED")
        for p in problems:
            print("  - " + p)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
