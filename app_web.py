# -*- coding: utf-8 -*-
"""
app_web.py — local web UI for mon_brain.

Serves a single-page dashboard on 127.0.0.1 and reuses the mon_brain.py kernel;
no web framework, only the Python standard library plus requests.

Run:  .venv\\Scripts\\python.exe app_web.py  ->  http://127.0.0.1:5000
"""

import sys
import re
import json
import random
import shutil
import base64
import pathlib
import datetime
import threading
import webbrowser
from urllib.parse import urlparse, parse_qs, quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Force UTF-8 on the Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
import mon_brain as mb

PORT = 5000
DIR_UPLOAD = mb.ROOT / "sources" / "_upload"

# Output instruction for the "follow source language" mode: the whole answer
# is written in the source language instead of the default Chinese body.
FOLLOW_INPUT_INSTR = (
    "Language: write the ENTIRE answer in the same language as the source material "
    "(French if the source is French, English if English). Do not switch to Chinese. "
    "Keep technical terms in their original form; do not invent."
)


def lang_instr_for(opts: dict) -> str:
    """Pick the injected language instruction from the selected output mode."""
    if opts.get("output_mode") == "follow_input":
        return FOLLOW_INPUT_INSTR
    lang = opts.get("lang", "zh")
    return mb.LANG_PROMPT.get(lang, mb.LANG_PROMPT["zh"])


def list_models() -> list[str]:
    """Pull the local model list from Ollama; fall back to defaults on failure."""
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=5).json()
        return [m.get("name", "") for m in r.get("models", [])]
    except Exception:
        return list(mb.DEFAULT_MODELS.values())


def list_history() -> list[dict]:
    """Scan sortie/ for generated notes and read their YAML header."""
    out = []
    if not mb.DIR_SORTIE.exists():
        return out
    for fp in sorted(mb.DIR_SORTIE.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        head = {}
        try:
            for line in fp.read_text(encoding="utf-8").splitlines()[:12]:
                m = re.match(r"^(\w+):\s*(.*)$", line)
                if m:
                    head[m.group(1)] = m.group(2).strip()
        except Exception:
            pass
        out.append({
            "file": fp.name,
            "source": head.get("source", ""),
            "mode": head.get("mode", ""),
            "lang": head.get("lang", ""),
            "date": head.get("date", ""),
        })
    return out


def cleanup_uploads(max_age_hours: int = 24) -> None:
    """Delete per-job upload folders older than max_age_hours (privacy hygiene)."""
    if not DIR_UPLOAD.exists():
        return
    cutoff = datetime.datetime.now().timestamp() - max_age_hours * 3600
    for child in DIR_UPLOAD.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def save_uploads(files: list[dict]) -> list[pathlib.Path]:
    """Decode base64 uploads into a per-job temp folder under sources/_upload/."""
    cleanup_uploads()
    job = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    udir = DIR_UPLOAD / job
    udir.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files:
        name = f.get("name", "sans_nom")
        b64 = f.get("b64", "")
        safe = re.sub(r"\W+", "_", pathlib.Path(name).stem)[:50] or "file"
        ext = pathlib.Path(name).suffix.lower()
        fp = udir / (safe + ext)
        fp.write_bytes(base64.b64decode(b64))
        paths.append(fp)
    return paths


def summarize_paths(paths: list[pathlib.Path], model: str, system: str,
                    lang_instr: str, corrections: str, merge: bool,
                    notify=None, map_model: str | None = None) -> tuple[str, int]:
    """Map stage: read each file, split if long, summarize; carry earlier context forward when merging.

    Returns (merged_material, number_of_chunks_summarized).
    """
    map_m = map_model or model
    resumes, contexte, chunks = [], [], 0
    for p in paths:
        texte = mb.read_file(p).strip()
        if notify:
            notify(f"Reading {p.name}: {len(texte)} chars")
        ctx = "\n".join(contexte) if merge else ""
        if len(texte) > mb.SPLIT_THRESHOLD:
            pieces = mb.split_text(texte)
            if notify:
                notify(f"{p.name}: splitting into {len(pieces)} fragment(s)")
            for i, piece in enumerate(pieces, 1):
                if notify:
                    notify(f"{p.name}: summarizing fragment {i}/{len(pieces)}")
                resumes.append(mb.summarize_chunk(piece, map_m, system, lang_instr, corrections, ctx))
                chunks += 1
        else:
            if notify:
                notify(f"{p.name}: single fragment, summarizing")
            resumes.append(mb.summarize_chunk(texte, map_m, system, lang_instr, corrections, ctx))
            chunks += 1
        if merge:
            contexte.append(f"## {p.name}\n" + (resumes[-1] if resumes else ""))
    return "\n\n---\n\n".join(s for s in resumes if s), chunks


def run_job(files: list[dict], opts: dict, notify=None) -> dict:
    """Core web job: read uploads, run the kernel, export a note, return the result."""
    def log(txt: str) -> None:
        if notify:
            notify(txt)

    mode = opts.get("mode", "translate_summary")
    model = opts.get("model") or mb.DEFAULT_MODELS[mode]
    map_model = (opts.get("map_model") or "").strip() or None
    merge = bool(opts.get("merge"))
    fresh = bool(opts.get("fresh"))
    lang = opts.get("lang", "zh")
    intent = (opts.get("intent") or "").strip()

    log(f"Job started: mode={mode}, model={model}, parallel={lang}, merge={merge}, fresh={fresh}")
    if map_model and map_model != model:
        log(f"Hybrid pipeline: chunk summaries by {map_model}, final note by {model}")
    paths = save_uploads(files)
    if not paths:
        return {"ok": False, "error": "no file received"}
    log(f"Received {len(paths)} file(s): " + ", ".join(p.name for p in paths))

    raw_text = "\n".join(mb.read_file(p) for p in paths)
    corrections = mb.build_corrections_block(raw_text)
    lang_instr = lang_instr_for(opts)
    system = mb.SYSTEM[mode]
    prompt = mb.load_prompt(mode)
    log(f"Correction table entries injected: {corrections.count(chr(10) + '- ')}")

    base = paths[0].stem if len(paths) == 1 else (paths[0].parent.name or "merged")
    previous = ""
    if not fresh:
        prev = mb.find_previous(base, mode)
        if prev:
            previous = prev.read_text(encoding="utf-8")
            log(f"Incremental update: reusing previous draft {prev.name} ({len(previous)} chars)")

    material, chunks = summarize_paths(paths, model, system, lang_instr, corrections, merge,
                                     notify=notify, map_model=map_model)
    if not material.strip():
        return {"ok": False, "error": "empty material after reading"}
    log(f"Map stage done: {chunks} chunk(s) summarized, merged material {len(material)} chars")

    num_predict = mb.RECAP_PREDICT if mode == "recap" else mb.NUM_PREDICT
    budget = mb.source_budget_chars(system, prompt, lang_instr, corrections, previous, num_predict)
    log(f"Context budget: {budget} chars allowed for source (num_ctx={mb.NUM_CTX}, num_predict={num_predict})")
    # Re-compression deliberately uses the final (strong) model: a weak map model
    # often echoes the input instead of compressing it, stalling the loop.
    if len(material) > budget:
        material = mb.reduce_to_budget(material, model, system, lang_instr, corrections,
                                       budget, lang, notify=notify)
        log(f"Re-compression finished: material now {len(material)} chars")
    if len(material) > budget:
        return {"ok": False, "error": f"Merged material is still about {len(material)} chars, over the single-run budget (~{budget} chars) even after re-compression. Split the source into parts and retry."}

    log(f"Generating the final note with {model} (num_predict={num_predict})…")
    try:
        if mode == "recap":
            think, conclusion = mb.generate_recap(model, system, prompt, lang_instr, corrections,
                                                  material, previous, intent=intent)
        else:
            think, conclusion = mb.generate_final(
                mode, model, system, prompt, lang_instr, corrections, material,
                fused=len(paths) > 1 or len(material) > mb.SPLIT_THRESHOLD, previous=previous)
    except requests.exceptions.ConnectionError:
        return {"ok": False, "kind": "ollama_down", "error": "Cannot reach Ollama. Make sure Ollama is running (taskbar icon, or run `ollama serve`)."}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "kind": "request_error", "error": f"Ollama request error: {e}"}

    conclusion = mb.clean_fence(conclusion)

    # Number-fidelity self-check: warn when source quantities did not survive.
    fid = mb.check_fidelity(raw_text, conclusion)
    if fid["missing"]:
        log(f"Number check: {len(fid['missing'])} of {fid['checked']} quantities missing "
            f"({', '.join(fid['missing'][:12])})")
    else:
        log(f"Number check passed: all {fid['checked']} quantities present")

    meta = {"mode": mode, "model": model, "lang": lang, "base": base,
            "map_model": map_model or model, "fidelity_missing": len(fid["missing"])}

    sess_path = mb.session_path(base) if mode == "recap" else None
    if sess_path:
        intent_note = f"\n\n## User's intent / question\n\n{intent}" if intent else ""
        session = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt + "\n\n" + lang_instr + intent_note},
                {"role": "assistant", "content": conclusion},
            ],
            "meta": {"source": ", ".join(p.name for p in paths), "mode": mode, "model": model,
                     "lang": lang, "intent": intent},
        }
        mb.save_session(sess_path, session)

    fp = mb.export_note(think, conclusion, meta, [p.name for p in paths],
                        session_ref=sess_path.name if sess_path else None)
    log(f"Saved: {fp.name}")
    return {
        "ok": True, "conclusion": conclusion, "think": think, "meta": meta,
        "file": fp.name, "session": sess_path.name if sess_path else None, "base": base,
        "fidelity": fid,
        "debug": {
            "model": model, "map_model": map_model or model,
            "num_ctx": mb.NUM_CTX, "num_predict": num_predict,
            "budget_chars": budget, "material_chars": len(material), "chunks": chunks,
            "source_chars": len(raw_text), "corrections_len": len(corrections),
            "previous_chars": len(previous),
            "system_prompt": system,
            "task_prompt": prompt,
            "lang_instr": lang_instr,
            "intent": intent,
        },
    }


# ---------- Job queue (async processing with live progress) ----------
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _new_job() -> str:
    jid = "%08x" % random.getrandbits(32)
    with JOBS_LOCK:
        JOBS[jid] = {"log": [], "done": False, "result": None}
    return jid


def _push(jid: str, txt: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(jid)
        if not job:
            return
        job["log"].append(f"{datetime.datetime.now().strftime('%H:%M:%S')}  {txt}")
        if len(job["log"]) > 300:
            del job["log"][0]


def start_job(files: list[dict], opts: dict) -> str:
    """Run the job in a background thread so the UI can poll progress."""
    jid = _new_job()

    def work() -> None:
        try:
            result = run_job(files, opts, notify=lambda t: _push(jid, t))
        except Exception as e:  # never leave the job hanging
            import traceback
            traceback.print_exc()
            result = {"ok": False, "error": f"internal error: {e}"}
        with JOBS_LOCK:
            job = JOBS.get(jid)
            if job:
                job["result"] = result
                job["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return jid


# ---------- Sessions (multi-conversation switch) ----------
def list_sessions() -> list[dict]:
    out = []
    if not mb.DIR_SESSIONS.exists():
        return out
    for fp in sorted(mb.DIR_SESSIONS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("meta", {})
        turns = sum(1 for m in data.get("messages", []) if m.get("role") == "user")
        out.append({
            "session": fp.stem,
            "source": meta.get("source", ""),
            "mode": meta.get("mode", ""),
            "model": meta.get("model", ""),
            "intent": meta.get("intent", ""),
            "turns": turns,
            "mtime": datetime.datetime.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return out


def load_session_view(name: str) -> dict:
    """Return the user/assistant turns of a session for display."""
    safe = pathlib.Path(name).stem
    fp = mb.DIR_SESSIONS / f"{safe}.json"
    if not fp.exists():
        return {"ok": False, "error": "session not found"}
    data = mb.load_session(fp)
    meta = data.get("meta", {})
    turns = []
    for m in data.get("messages", []):
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        # The stored first user turn is the whole task template; show the intent instead.
        if m.get("role") == "user" and len(content) > 600:
            content = ("[intent] " + meta["intent"]) if meta.get("intent") else "[task template]"
        turns.append({"role": m["role"], "content": content})
    return {"ok": True, "session": safe, "meta": meta, "turns": turns}


# ---------- Prompt templates (web editor) ----------
def list_prompts() -> list[dict]:
    out = []
    if not mb.DIR_PROMPTS.exists():
        return out
    for fp in sorted(mb.DIR_PROMPTS.glob("*.md")):
        out.append({"name": fp.name, "size": fp.stat().st_size,
                    "mtime": datetime.datetime.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


def read_prompt(name: str) -> dict:
    safe = pathlib.Path(name).name
    fp = mb.DIR_PROMPTS / safe
    if not fp.exists() or fp.suffix.lower() != ".md":
        return {"ok": False, "error": "prompt not found"}
    return {"ok": True, "name": safe, "content": fp.read_text(encoding="utf-8")}


def write_prompt(name: str, content: str) -> dict:
    safe = pathlib.Path(name).name
    if not re.fullmatch(r"[\w\-.]{1,40}\.md", safe):
        return {"ok": False, "error": "invalid prompt file name"}
    fp = mb.DIR_PROMPTS / safe
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return {"ok": True, "name": safe, "size": len(content)}


def recap_chat(session_base: str, message: str, model: str) -> dict:
    """Follow-up turn for a recap session: append to history, call the model, persist."""
    sp = mb.session_path(session_base)
    sess = mb.load_session(sp)
    if not sess.get("messages"):
        return {"ok": False, "error": "no saved session to continue"}
    mode = sess["meta"].get("mode", "recap")
    system = mb.SYSTEM[mode]
    if not model:
        model = sess["meta"].get("model") or mb.DEFAULT_MODELS[mode]

    sess["messages"].append({"role": "user", "content": message})
    try:
        raw = mb.call_ollama(model, system, sess["messages"][1:], num_predict=mb.RECAP_PREDICT)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e)}
    _, answer = mb.split_think(raw)
    answer = mb.clean_fence(answer)
    sess["messages"].append({"role": "assistant", "content": answer})
    mb.save_session(sp, sess)

    note = mb.find_previous(session_base, mode)
    if note:
        with note.open("a", encoding="utf-8") as f:
            f.write(f"\n\n## Follow-up ({datetime.datetime.now().strftime('%H:%M')})\n\n"
                    f"**You:** {message}\n\n**AI:** {answer}\n")
    return {"ok": True, "answer": answer}


def _esc(s: str) -> str:
    """Minimal HTML escaping for the docs renderer."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(s: str) -> str:
    """Render inline Markdown (code, bold, links) on already-escaped text."""
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def md_to_html(md: str) -> str:
    """Tiny Markdown -> HTML converter for the README docs page.

    Covers only what the READMEs actually use: headings, fenced code, tables,
    bullet / numbered lists (one nesting level shown by indent), paragraphs.
    Deliberately dependency-free: no markdown package needed on a fresh machine.
    """
    lines = md.splitlines()
    html: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        s = raw.strip()

        if s.startswith("```"):
            j, body = i + 1, []
            while j < n and not lines[j].strip().startswith("```"):
                body.append(lines[j])
                j += 1
            html.append("<pre><code>" + _esc("\n".join(body)) + "</code></pre>")
            i = j + 1
            continue

        if not s:
            i += 1
            continue

        m = re.match(r"(#{1,4})\s+(.*)", s)
        if m:
            lvl = min(len(m.group(1)), 6)
            html.append(f"<h{lvl}>{_inline(_esc(m.group(2)))}</h{lvl}>")
            i += 1
            continue

        if s.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            cells = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in r)]
            html.append("<table>")
            for k, r in enumerate(cells):
                tag = "th" if k == 0 else "td"
                html.append("<tr>" + "".join(f"<{tag}>{_inline(_esc(c))}</{tag}>" for c in r) + "</tr>")
            html.append("</table>")
            continue

        if re.match(r"\s*[-*]\s+", raw):
            items = []
            while i < n and re.match(r"\s*[-*]\s+", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                text = re.sub(r"^\s*[-*]\s+", "", lines[i])
                items.append((indent, text))
                i += 1
            html.append("<ul>")
            for indent, text in items:
                cls = ' class="sub"' if indent >= 2 else ""
                html.append(f"<li{cls}>{_inline(_esc(text))}</li>")
            html.append("</ul>")
            continue

        if re.match(r"\s*\d+\.\s+", raw):
            html.append("<ol>")
            while i < n and re.match(r"\s*\d+\.\s+", lines[i]):
                text = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                html.append(f"<li>{_inline(_esc(text))}</li>")
                i += 1
            html.append("</ol>")
            continue

        para = []
        while i < n and lines[i].strip() and not re.match(
                r"(\s*[-*]\s+|\s*\d+\.\s+|#{1,4}\s+|\||```)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        html.append("<p>" + _inline(_esc(" ".join(para))) + "</p>")
    return "\n".join(html)


DOCS_TITLES = {"zh": "使用说明 · 原理与架构", "en": "Docs · How it works", "fr": "Docs · Fonctionnement"}


def docs_page(body: str, lang: str) -> str:
    """Wrap the rendered README in a readable, self-contained page."""
    tabs = "".join(
        f'<a class="tab{" on" if code == lang else ""}" href="/docs?lang={code}">{label}</a>'
        for code, label in (("zh", "中文"), ("en", "English"), ("fr", "Français"))
    )
    return f"""<!DOCTYPE html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{DOCS_TITLES.get(lang, "docs")}</title>
<style>
body{{margin:0;background:#f1f5f9;color:#1e293b;font:15px/1.7 -apple-system,"Segoe UI","Noto Sans SC",sans-serif}}
.wrap{{max-width:900px;margin:0 auto;padding:0 16px 64px}}
.bar{{position:sticky;top:0;background:#0f172a;color:#fff;display:flex;gap:12px;align-items:center;justify-content:space-between;padding:12px 16px;flex-wrap:wrap;z-index:5}}
.bar a{{color:#fff;text-decoration:none;font-size:13px}}
.tabs{{display:flex;gap:6px}}
.tab{{padding:4px 10px;border-radius:6px;background:rgba(255,255,255,.12)}}
.tab.on{{background:#0e7490}}
.doc{{background:#fff;border-radius:14px;padding:8px 24px 32px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
h2,h3,h4{{color:#0f172a;margin:28px 0 8px}}
h1,h2{{border-bottom:1px solid #e2e8f0;padding-bottom:6px}}
code{{background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;padding:1px 4px;font-size:13px}}
pre{{background:#0f172a;color:#d7fbe8;padding:12px;border-radius:10px;overflow:auto;font-size:13px;line-height:1.6}}
pre code{{background:none;border:0;color:inherit;padding:0}}
table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}}
th,td{{border:1px solid #e2e8f0;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f8fafc}}
ul,ol{{padding-left:20px}} li{{margin:3px 0}}
li.sub{{margin-left:22px;font-size:14px}}
a{{color:#0e7490}}
</style></head>
<body>
<div class="bar">
  <div><b>{DOCS_TITLES.get(lang, "docs")}</b> <span style="opacity:.6;font-size:12px">mon_brain</span></div>
  <div class="tabs">{tabs}</div>
  <div><a href="/">← 返回工作台 / Back to workbench</a></div>
</div>
<div class="wrap"><div class="doc">
{body}
</div></div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj: dict, status: int = 200) -> None:
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if n == 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/index"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/docs"):
            q = parse_qs(urlparse(self.path).query)
            lang = q.get("lang", ["zh"])[0]
            name = {"zh": "README.zh.md", "en": "README.md", "fr": "README.fr.md"}.get(lang, "README.zh.md")
            path = mb.ROOT / name
            md = path.read_text(encoding="utf-8") if path.exists() else f"# {name} not found"
            body = docs_page(md_to_html(md), lang).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/static/"):
            name = self.path.split("/static/", 1)[1].split("?")[0]
            name = name.replace("\\", "/").split("/")[-1]  # block path traversal
            p = mb.ROOT / "static" / name
            if not p.is_file():
                self.send_error(404, f"static asset not found: {name}")
                return
            body = p.read_bytes()
            ctype = ("text/javascript; charset=utf-8" if name.endswith(".js") else
                     "text/css; charset=utf-8" if name.endswith(".css") else
                     "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._send_json({
                "models": list_models(),
                "modes": list(mb.DEFAULT_MODELS),
                "langs": ["zh", "en", "fr", "bilingual"],
                "defaults": mb.DEFAULT_MODELS,
                "history": list_history(),
            })
        elif self.path == "/api/history":
            self._send_json({"history": list_history()})
        elif self.path.startswith("/api/progress"):
            q = parse_qs(urlparse(self.path).query)
            jid = q.get("task", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if not job:
                    self._send_json({"ok": False, "error": "unknown task"}, 404)
                    return
                snapshot = {"ok": True, "done": job["done"], "log": list(job["log"]), "result": job["result"]}
            self._send_json(snapshot)
        elif self.path == "/api/sessions":
            self._send_json({"sessions": list_sessions()})
        elif self.path.startswith("/api/session"):
            q = parse_qs(urlparse(self.path).query)
            self._send_json(load_session_view(q.get("name", [""])[0]))
        elif self.path == "/api/prompts":
            self._send_json({"prompts": list_prompts()})
        elif self.path.startswith("/api/prompt"):
            q = parse_qs(urlparse(self.path).query)
            self._send_json(read_prompt(q.get("name", [""])[0]))
        elif self.path.startswith("/api/download"):
            try:
                q = parse_qs(urlparse(self.path).query)
                fname = pathlib.Path(q.get("file", [""])[0]).name
                fp = mb.DIR_SORTIE / fname
                resolved = fp.resolve()
                if not fp.exists() or mb.DIR_SORTIE.resolve() not in resolved.parents:
                    self._send_json({"ok": False, "error": "file not found"}, 404)
                    return
                data = resolved.read_bytes()
                # RFC 5987: non-ASCII (e.g. Chinese) filenames must go in filename*;
                # a plain ASCII fallback keeps old clients happy.
                ascii_fallback = re.sub(r"[^\x20-\x7e]", "_", fname)
                disposition = f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(fname)}"
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", disposition)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                import traceback
                traceback.print_exc()
                try:
                    self._send_json({"ok": False, "error": "download failed"}, 500)
                except Exception:
                    pass
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            data = self._read_json()
        except Exception as e:
            self._send_json({"ok": False, "error": f"bad json: {e}"}, 400)
            return
        if self.path == "/api/process":
            jid = start_job(data.get("files", []), data)
            self._send_json({"ok": True, "task": jid})
        elif self.path == "/api/recap/chat":
            self._send_json(recap_chat(data.get("base", ""), data.get("message", ""), data.get("model", "")))
        elif self.path == "/api/prompt":
            self._send_json(write_prompt(data.get("name", ""), data.get("content", "")))
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def log_message(self, *_) -> None:
        pass


# ---------- Single-page dashboard (embedded HTML + Tailwind CDN) ----------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="/static/tailwind.js"></script>
<title>mon_brain</title>
<style>
.help{display:inline-flex;align-items:center;justify-content:center;width:1rem;height:1rem;margin-left:.3rem;border-radius:9999px;background:#0f172a;color:#fff;font-size:10px;font-weight:700;cursor:help;vertical-align:middle;user-select:none;flex:0 0 auto}
.help:hover{background:#0e7490}
#tipbox{position:fixed;z-index:60;max-width:21rem;background:#0f172a;color:#fff;font-size:12px;line-height:1.6;padding:.55rem .7rem;border-radius:.55rem;box-shadow:0 10px 28px rgba(0,0,0,.28);white-space:pre-line;pointer-events:none;display:none}
#guideBody ul{list-style:disc;padding-left:1.25rem;margin:.25rem 0}
#guideBody li{margin:.15rem 0}
</style>
</head>
<body class="bg-slate-100 min-h-screen text-slate-800">
<div class="max-w-6xl mx-auto p-4">

  <header class="bg-slate-900 text-white rounded-2xl p-5 mb-4 flex flex-wrap items-center justify-between gap-3">
    <div>
      <h1 class="text-xl font-bold" data-i18n="title">mon_brain</h1>
      <p class="text-sm text-slate-300" data-i18n="subtitle">Local Ollama note organizer · translator-summary · review</p>
    </div>
    <div class="flex gap-2">
      <a href="/docs" target="_blank" class="px-3 py-1 rounded bg-teal-700 hover:bg-teal-600 text-sm" style="text-decoration:none">说明 / Docs</a>
      <button id="lang-en" class="px-3 py-1 rounded bg-white/10">EN</button>
      <button id="lang-zh" class="px-3 py-1 rounded bg-white/10">中文</button>
      <button id="lang-fr" class="px-3 py-1 rounded bg-white/10">FR</button>
    </div>
  </header>

  <details class="bg-white rounded-2xl shadow mb-4">
    <summary class="cursor-pointer px-4 py-3 text-sm font-semibold text-slate-700 select-none" data-i18n="guideTitle">Quick guide</summary>
    <div id="guideBody" class="px-4 pb-4 pt-1 text-sm text-slate-600 leading-relaxed"></div>
  </details>

  <div class="grid md:grid-cols-[360px_1fr] gap-4">

    <aside class="bg-white rounded-2xl p-4 shadow space-y-4">
      <div>
        <label class="text-sm font-semibold" data-i18n="files">Files</label>
        <input id="file" type="file" multiple class="w-full mt-1 text-sm">
        <div id="filelist" class="text-xs text-slate-500 mt-1"></div>
      </div>

      <div>
        <label class="text-sm font-semibold" data-i18n="mode">Mode</label><span class="help" data-tip="tipMode">?</span>
        <select id="mode" class="w-full border rounded p-2 text-sm mt-1">
          <option value="organize">organize · 整理 · Organisation</option>
          <option value="translate_summary" selected>translate_summary · 翻译总结 · Résumé</option>
          <option value="recap">recap · 复盘 · Révision</option>
        </select>
        <p id="modeHint" class="text-xs text-cyan-800 mt-1 leading-snug"></p>
      </div>

      <div>
        <label class="text-sm font-semibold" data-i18n="output">Output</label><span class="help" data-tip="tipOutput">?</span>
        <label class="block mt-1 text-sm"><input type="radio" name="outmode" value="zh_parallel" checked> <span data-i18n="zhParallel">Chinese summary + parallel terms</span></label>
        <label class="block text-sm"><input type="radio" name="outmode" value="follow_input"> <span data-i18n="followInput">Follow source language (whole answer)</span></label>
      </div>

      <div>
        <label class="text-sm font-semibold" data-i18n="parallelLang">Parallel language</label><span class="help" data-tip="tipLang">?</span>
        <select id="lang" class="w-full border rounded p-2 text-sm mt-1">
          <option value="zh">zh</option>
          <option value="en">en</option>
          <option value="fr">fr</option>
          <option value="bilingual" selected>bilingual</option>
        </select>
      </div>

      <div>
        <label class="text-sm font-semibold" data-i18n="preset">Preset</label><span class="help" data-tip="tipPreset">?</span>
        <div class="flex gap-1 mt-1">
          <button type="button" class="preset-btn flex-1 border rounded px-2 py-1 text-xs" data-preset="fast" data-i18n="presetFast">Fast</button>
          <button type="button" class="preset-btn flex-1 border rounded px-2 py-1 text-xs" data-preset="balanced" data-i18n="presetBalanced">Balanced</button>
          <button type="button" class="preset-btn flex-1 border rounded px-2 py-1 text-xs" data-preset="quality" data-i18n="presetQuality">Quality</button>
        </div>
        <p id="presetHint" class="text-xs text-slate-500 mt-1 leading-snug"></p>
      </div>

      <div>
        <label class="text-sm font-semibold" data-i18n="model">Model</label><span class="help" data-tip="tipModel">?</span>
        <select id="model" class="w-full border rounded p-2 text-sm mt-1"></select>
      </div>

      <div>
        <label class="text-sm font-semibold" data-i18n="mapModel">Chunk-summary model (hybrid, optional)</label><span class="help" data-tip="tipMap">?</span>
        <select id="mapModel" class="w-full border rounded p-2 text-sm mt-1">
          <option value="" data-i18n="mapModelSame">Same as main model</option>
        </select>
        <p class="text-xs text-gray-500 mt-1" data-i18n="mapModelHint">Use a fast model for chunk summaries and a strong model for the final note.</p>
      </div>

      <label class="block text-sm"><input type="checkbox" id="merge"> <span data-i18n="merge">Merge multiple files into one note</span><span class="help" data-tip="tipMerge">?</span></label>
      <label class="block text-sm"><input type="checkbox" id="fresh"> <span data-i18n="fresh">Fresh start (ignore previous draft)</span><span class="help" data-tip="tipFresh">?</span></label>
      <label class="block text-sm"><input type="checkbox" id="debug"> <span data-i18n="debug">Show debug details (prompt, budget, chunks)</span><span class="help" data-tip="tipDebug">?</span></label>

      <div>
        <label class="text-sm font-semibold" data-i18n="intent">Intent / your question (recap)</label><span class="help" data-tip="tipIntent">?</span>
        <textarea id="intent" rows="2" class="w-full border rounded p-2 text-sm mt-1" data-i18n-ph="intentPh"></textarea>
        <p class="text-xs text-slate-500 mt-1" data-i18n="intentHint">Write one concrete sentence, e.g. "explain the three theories and make them memorisable".</p>
      </div>

      <button id="start" class="w-full bg-slate-900 text-white px-4 py-2 rounded" data-i18n="start">Process</button>
    </aside>

    <main class="bg-white rounded-2xl p-4 shadow">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm font-semibold" data-i18n="result">Result</span>
        <a id="download" class="hidden text-sm text-teal-700 underline" data-i18n="download">Download Markdown</a>
      </div>
      <pre id="result" class="bg-slate-900 text-green-100 p-3 rounded-xl whitespace-pre-wrap text-sm min-h-[200px]" data-i18n="empty">Pick files and choose a mode, then press Process.</pre>

      <div id="progress" class="mt-3">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-slate-500" data-i18n="progress">Progress</span>
          <span id="stage" class="text-xs text-teal-700"></span>
        </div>
        <div id="log" class="mt-1 bg-slate-50 border rounded-xl p-2 text-xs text-slate-600 whitespace-pre-wrap max-h-40 overflow-auto font-mono"></div>
      </div>

      <div id="fidelitybox" class="hidden mt-3 bg-amber-100 border border-amber-400 text-amber-900 rounded-xl p-3 text-sm whitespace-pre-wrap"></div>

      <div id="debugbox" class="hidden mt-3">
        <span class="text-xs font-semibold text-slate-500" data-i18n="debugTitle">Debug</span>
        <pre id="debugtext" class="mt-1 bg-slate-50 border rounded-xl p-2 text-xs whitespace-pre-wrap max-h-60 overflow-auto font-mono"></pre>
      </div>

      <div id="chat" class="hidden mt-4">
        <div id="bubbles" class="space-y-2"></div>
        <div class="flex gap-2 mt-2">
          <input id="chatinput" class="flex-1 border rounded p-2 text-sm" data-i18n-ph="followup">
          <button id="send" class="bg-teal-600 text-white px-3 py-2 rounded" data-i18n="send">Send</button>
        </div>
      </div>
    </main>
  </div>

  <section class="bg-white rounded-2xl p-4 shadow mt-4">
    <h2 class="text-sm font-semibold mb-2" data-i18n="history">History</h2>
    <div id="history" class="space-y-1 text-sm"></div>
  </section>

  <section class="bg-white rounded-2xl p-4 shadow mt-4">
    <h2 class="text-sm font-semibold mb-2" data-i18n="sessions">Recap sessions</h2>
    <div id="sessions" class="space-y-1 text-sm"></div>
  </section>

  <section class="bg-white rounded-2xl p-4 shadow mt-4">
    <div class="flex items-center justify-between mb-2">
      <h2 class="text-sm font-semibold" data-i18n="promptsTitle">Prompt templates</h2>
      <div class="flex gap-2 items-center">
        <select id="promptsel" class="border rounded p-1 text-sm"></select>
        <button id="promptsave" class="bg-teal-600 text-white px-3 py-1 rounded text-sm" data-i18n="save">Save</button>
      </div>
    </div>
    <textarea id="promptedit" rows="10" class="w-full border rounded-xl p-2 text-xs font-mono" data-i18n-ph="promptPh"></textarea>
    <div id="promptmsg" class="text-xs text-teal-700 mt-1"></div>
  </section>
</div>
<div id="tipbox"></div>

<script>
const I18N = {
  en: {title:"mon_brain", subtitle:"Local Ollama note organizer · translator-summary · review", files:"Files", mode:"Mode", output:"Output", zhParallel:"Chinese summary + parallel terms", followInput:"Follow source language (whole answer)", parallelLang:"Parallel language", model:"Model", merge:"Merge multiple files into one note", fresh:"Fresh start (ignore previous draft)", debug:"Show debug details (prompt, budget, chunks)", start:"Process", result:"Result", download:"Download Markdown", history:"History", sessions:"Recap sessions", promptsTitle:"Prompt templates", save:"Save", followup:"Ask a follow-up…", send:"Send", processing:"Processing…", empty:"Pick files and choose a mode, then press Process.", intent:"Intent / your question (recap)", intentPh:"What do you want this round? Recap uses it to review your question.", ollamaDown:"🔴 Cannot reach local Ollama. Check the taskbar icon, or run `ollama serve`.", progress:"Progress", debugTitle:"Debug", promptPh:"Pick a template to edit…", noSessions:"No saved recap session yet.", running:"Running…", done:"Done.", mapModel:"Chunk-summary model (hybrid, optional)", mapModelSame:"Same as main model", mapModelHint:"Use a fast model for chunk summaries and a strong model for the final note.", preset:"Compute preset", presetFast:"Fast", presetBalanced:"Balanced", presetQuality:"Quality", guideTitle:"Quick guide", intentHint:"One concrete sentence: what should this note do for you?", fidelityWarn:"⚠ Number check: {k} of {n} quantities in the source are missing from this note: {items}. Add them manually before printing."},
  zh: {title:"mon_brain", subtitle:"本地 Ollama 整理 · 翻译总结 · 复盘", files:"素材文件", mode:"模式", output:"输出", zhParallel:"中文总结 + 对照（默认）", followInput:"跟随素材语言整篇输出", parallelLang:"对照语言", model:"模型", merge:"多文件合并成一篇", fresh:"全新开始（不沿用旧稿）", debug:"显示调试信息（提示词 / 预算 / 分片）", start:"开始处理", result:"结果", download:"下载 Markdown", history:"历史稿件", sessions:"复盘会话", promptsTitle:"提示词模板", save:"保存", followup:"继续追问…", send:"发送", processing:"处理中…", empty:"选择文件和模式后，点开始处理。", intent:"意图 / 你的提问（复盘）", intentPh:"本轮你想得到什么？复盘模式据此评估你的提问。", ollamaDown:"🔴 连不上本地 Ollama，请检查任务栏图标，或运行 ollama serve。", progress:"处理进度", debugTitle:"调试信息", promptPh:"选一个模板开始编辑…", noSessions:"还没有保存的复盘会话。", running:"处理中…", done:"已完成。", mapModel:"分片摘要模型（混合流水线，可选）", mapModelSame:"与主模型相同", mapModelHint:"分片用快模型粗提炼，终稿用强模型精修。", preset:"算力档位", presetFast:"极速", presetBalanced:"均衡", presetQuality:"高质", guideTitle:"使用须知（点开看）", intentHint:"一句话写清本轮目标，例如「弄懂三大理论并做成能背的提纲」。", fidelityWarn:"⚠ 数字保真自检：素材中 {n} 项数字里缺 {k} 项（{items}），打印前请人工补上。"},
  fr: {title:"mon_brain", subtitle:"Organisation locale de notes via Ollama · résumé-traduction · révision", files:"Fichiers sources", mode:"Mode", output:"Sortie", zhParallel:"Résumé chinois + termes parallèles (défaut)", followInput:"Suivre la langue de la source (réponse entière)", parallelLang:"Langue parallèle", model:"Modèle", merge:"Fusionner plusieurs fichiers en une note", fresh:"Repartir de zéro (ignorer le brouillon précédent)", debug:"Afficher les détails de débogage (prompt, budget, fragments)", start:"Traiter", result:"Résultat", download:"Télécharger le Markdown", history:"Notes générées", sessions:"Sessions de révision", promptsTitle:"Modèles de prompt", save:"Enregistrer", followup:"Poser une question de suivi…", send:"Envoyer", processing:"Traitement en cours…", empty:"Choisissez des fichiers et un mode, puis cliquez sur Traiter.", intent:"Intention / votre question (révision)", intentPh:"Que voulez-vous obtenir ce tour ? La révision évalue votre question.", ollamaDown:"🔴 Ollama local injoignable. Vérifiez l'icône dans la barre des tâches, ou lancez `ollama serve`.", progress:"Progression", debugTitle:"Débogage", promptPh:"Choisissez un modèle à éditer…", noSessions:"Aucune session de révision enregistrée.", running:"En cours…", done:"Terminé.", mapModel:"Modèle pour les résumés de fragments (hybride, optionnel)", mapModelSame:"Identique au modèle principal", mapModelHint:"Un modèle rapide pour les résumés de fragments, un modèle fort pour la note finale.", preset:"Profil de calcul", presetFast:"Rapide", presetBalanced:"Équilibré", presetQuality:"Qualité", guideTitle:"Guide d'utilisation (cliquer)", intentHint:"Une phrase concrète : que doit produire cette note ?", fidelityWarn:"⚠ Contrôle des chiffres : {k} élément(s) sur {n} absent(s) de cette note ({items}). À compléter avant impression."}
};
let UI = "en";
let files = [];
let busy = false;
let pollTimer = null;
let currentBase = null, currentModel = null;

function t(k){ return I18N[UI][k] || I18N.en[k] || k; }
function applyI18n(){
  document.querySelectorAll("[data-i18n]").forEach(e => e.textContent = t(e.dataset.i18n));
  document.querySelectorAll("[data-i18n-ph]").forEach(e => e.placeholder = t(e.dataset.i18nPh));
  updateModeHint();
  updatePresetHint();
  applyGuide();
}

// ---------- inline help: per-control hints, hover tips, collapsible guide ----------
// The mode dropdown shows the English name plus a short zh/fr gloss, so the user
// can tell what each mode does without reading the README first.
const MODE_HINT = {
  organize: {en:"Re-chapters scattered notes into a structured Chinese note. No translation, no grading.",
             zh:"把零散的笔记/转写按逻辑重排成一份结构化中文笔记；不翻译、也不打分评价。",
             fr:"Réorganise des notes éparses en une note chinoise structurée. Sans traduction ni évaluation."},
  translate_summary: {en:"Foreign transcript to Chinese key points + trilingual glossary. Default mode.",
                      zh:"外语素材（如法语课转写）压成中文要点 + 关键术语对照 + 文末三语术语表。默认模式。",
                      fr:"Transcription étrangère vers points clés chinois + glossaire trilingue. Mode par défaut."},
  recap: {en:"Two layers: review the material, then judge how good your own question was.",
          zh:"双层复盘：先复盘素材本身（要点/你的理解偏差），再评估你写的意图与提问是否具体可验证。",
          fr:"Deux niveaux : révision du matériau, puis évaluation de la qualité de votre question."}
};
// Which model each preset really uses. Printed under the buttons, so the names
// on the buttons are never a mystery: the preset only swaps models, it does not
// change the GPU settings (those are global: flash-attn, q8_0 KV cache, ctx 8192).
const PRESET_ROWS = {
  fast:     {en:"Fast · every step on qwen2.5:latest (7B / 4.7 GB) · ≈1.7 min",
             zh:"极速 · 全程 qwen2.5:latest（7B / 4.7GB）· ≈1.7 分钟",
             fr:"Rapide · qwen2.5:latest (7B / 4,7 Go) partout · ~1,7 min"},
  balanced: {en:"Balanced · chunks on qwen2.5:latest (7B) + final on gemma3:12b · ≈1.8 min",
             zh:"均衡 · 分片 qwen2.5:latest（7B）+ 终稿 gemma3:12b（12B）· ≈1.8 分钟",
             fr:"Équilibré · fragments qwen2.5:latest (7B) + note finale gemma3:12b · ~1,8 min"},
  quality:  {en:"Quality · every step on gemma3:12b (12B / 8.2 GB) · ≈5.7 min",
             zh:"高质 · 全程 gemma3:12b（12B / 8.2GB）· ≈5.7 分钟",
             fr:"Qualité · gemma3:12b (12B / 8,2 Go) partout · ~5,7 min"}
};

const TIPS = {
  tipMode: {en:"organize = restructure notes. translate_summary = foreign transcript to Chinese key points + glossary. recap = review material AND your questioning. See Quick guide above.",
            zh:"整理=把零散笔记重排成结构稿；翻译总结=外语转写压成中文要点+术语表；复盘=既复盘素材也评估你的提问质量。三点说明详见顶部「使用须知」。",
            fr:"organize = restructurer. translate_summary = points clés chinois + glossaire. recap = réviser le matériau et vos questions. Voir le guide."},
  tipOutput: {en:"zh_parallel: body stays Chinese, only key terms are glossed in your parallel language. follow_input: the whole answer follows the source language.",
              zh:"中文要点+对照：正文始终中文，只把关键术语/段落翻成对照语言；跟素材语言：整篇按素材的语言输出。",
              fr:"zh_parallel : corps en chinois, seuls les termes clés sont glosés. follow_input : toute la réponse dans la langue source."},
  tipLang: {en:"Which language the glosses use. bilingual gives zh+en+fr side by side.",
            zh:"对照用的语言。选 bilingual 就中英法并排给，术语表本来就是三语。",
            fr:"Langue des gloses. bilingual = zh+en+fr côte à côte."},
  tipPreset: {en:"Measured here on a 28k-char source. Fast = qwen2.5:latest (7B) everywhere ≈1.7 min; Balanced = chunks qwen2.5 + final gemma3:12b ≈1.8 min; Quality = gemma3:12b (12B) everywhere ≈5.7 min. Clicking one fills both model dropdowns; GPU settings are shared.",
              zh:"本机 28k 字符实测：极速=全程 qwen2.5:latest（7B）≈1.7 分钟；均衡=分片 qwen2.5 + 终稿 gemma3:12b（12B）≈1.8 分钟；高质=全程 gemma3:12b ≈5.7 分钟。点一下会自动填好主模型与分片模型；GPU 参数三档共享，档位只换模型。",
              fr:"Mesuré ici sur 28k caractères. Rapide = qwen2.5:latest (7B) partout ~1,7 min ; Équilibré = fragments qwen2.5 + finale gemma3:12b ~1,8 min ; Qualité = gemma3:12b (12B) partout ~5,7 min."},
  tipModel: {en:"The model that writes the final note. Switching mode jumps to that mode's default model.",
             zh:"写终稿的主模型。切换模式时，会自动跳到该模式的默认模型（复盘默认 qwen3:14b）。",
             fr:"Modèle qui rédige la note finale. Changer de mode sélectionne son modèle par défaut."},
  tipMap: {en:"Only used when the source is longer than ~5000 chars and gets split into chunks: this picks who does the rough per-chunk pass. Empty = same as main model.",
           zh:"只有素材超过约 5000 字、被自动切段时才用得上：它决定「每段先由谁粗读」。留空=和主模型同一个；选 qwen2.5=快模型粗读、终稿仍由主模型写（就是均衡档的做法）。短素材用不到。",
           fr:"Utilisé seulement si la source dépasse ~5000 caractères et est découpée : choisit qui fait le résumé de chaque fragment. Vide = modèle principal."},
  tipMerge: {en:"Several files are concatenated into one material and processed as a single note.",
             zh:"多个文件拼成一份素材，一起处理成一篇稿子，共享上下文。",
             fr:"Plusieurs fichiers sont concaténés en un seul matériau traité comme une note."},
  tipFresh: {en:"Ignore the previous draft of the same source. Recommended for long sources, an old draft eats the context budget.",
             zh:"不沿用同一素材上次的旧稿。长素材建议勾上——旧稿会占掉一半上下文预算。",
             fr:"Ignore le brouillon précédent de la même source. Conseillé pour les sources longues."},
  tipDebug: {en:"Adds a panel under the result: model used, num_ctx, chunk count, char budget, and the full prompt sent to the model. For troubleshooting only.",
             zh:"勾上后结果下方多一块：实际用的模型、上下文长度、切了几片、预算多少字符、送给模型的完整提示词。排查「这次为什么答偏了」时用，正常交付不用勾。",
             fr:"Ajoute un panneau sous le résultat : modèle, num_ctx, nombre de fragments, budget, prompt complet. Pour le dépannage."},
  tipIntent: {en:"One sentence about what you want this round. Recap uses it as the yardstick to judge whether your question was specific enough.",
              zh:"用一句话写清这轮想拿素材干什么。复盘会拿它当标尺，判断你的提问是否具体、有没有可验证的目标。",
              fr:"Une phrase sur votre objectif. La révision s'en sert pour juger la précision de votre question."}
};

const GUIDE = {
en: '<p class="font-semibold text-slate-700 mb-1">1. Three modes</p><ul><li><b>organize</b> — restructure scattered notes into a clean Chinese note. No translation, no grading.</li><li><b>translate_summary</b> — foreign transcript (e.g. a French lecture) into Chinese key points, with parallel terms and a trilingual glossary at the end. Default.</li><li><b>recap</b> — two layers: first it reviews the material, then it judges how good your own question was.</li></ul><p class="font-semibold text-slate-700 mt-3 mb-1">2. The "compute presets" are actually model combinations (measured here, 28k-char source)</p><ul><li><b>Fast</b> ~1.7 min: <code>qwen2.5:latest</code> (7B / 4.7 GB) everywhere. Quickest, but looser structure and it drops numbers (missing 4 of 6 quantities on a short sample).</li><li><b>Balanced</b> ~1.8 min: chunks on <code>qwen2.5:latest</code> (7B), final note on <code>gemma3:12b</code> (12B / 8.2 GB). Best value, recommended.</li><li><b>Quality</b> ~5.7 min: <code>gemma3:12b</code> end to end. Most faithful, fullest glossary.</li></ul><p>Presets only swap models; GPU settings (flash-attn, q8_0 KV cache, ctx 8192, one model resident) are global and shared by all three. The speed comes from the smaller model and the short output cap, not from tuning hardware.</p><p class="font-semibold text-slate-700 mt-3 mb-1">3. What the chunk-summary model means</p><p>Sources over ~5000 chars are split into chunks, each roughly summarised, then merged. That dropdown decides who does the rough pass. Empty = the main model does everything (simplest, slower). Picking qwen2.5 = fast model roughs out, main model still writes the final note (this is exactly the Balanced preset). Short sources never get split, so it does nothing there.</p><p class="font-semibold text-slate-700 mt-3 mb-1">4. Show debug details</p><p>Adds a panel under the result with the model actually used, context size, chunk count, char budget and the full prompt. Use it when a run looks wrong; not needed for normal delivery.</p><p class="font-semibold text-slate-700 mt-3 mb-1">5. Intent / your question</p><p>One sentence in your own words about what this round should produce, e.g. "understand the three narrative theories and turn them into a memorisable outline". Recap uses it as the yardstick.</p><p class="font-semibold text-slate-700 mt-3 mb-1">6. How to talk to recap</p><p>Round one: just give the material plus your intent. Then keep asking in the box at the bottom right — same session, context is kept:</p><ul><li>"In part 2, what in the source supports your claim about X?"</li><li>"Rewrite my question into one more specific sentence."</li><li>"Compress it into 5 points for a 3-minute class recap."</li></ul><p class="text-xs text-slate-400 mt-2">Click open under Recap sessions to resume an earlier round.</p>',
zh: '<p class="font-semibold text-slate-700 mb-1">① 三种模式</p><ul><li><b>organize 整理</b> — 把零散的笔记/转写重排成一份结构化中文笔记，不翻译也不评价。</li><li><b>translate_summary 翻译总结</b> — 外语素材（比如法语课转写）压成中文要点，关键术语给对照，文末附三语术语表。默认模式。</li><li><b>recap 复盘</b> — 两层：先复盘素材本身（要点、你可能理解偏的地方），再评估你写的「意图/提问」是否具体、有没有可验证的目标。</li></ul><p class="font-semibold text-slate-700 mt-3 mb-1">② 三档「算力」其实是模型组合（本机实测，28k 字符素材）</p><ul><li><b>极速</b> 约 1.7 分钟 — 全程 <code>qwen2.5:latest</code>（7B / 4.7GB）。最省时间，但结构松散，数字容易漏（短样本上 6 项数字错了 4 项）。</li><li><b>均衡</b> 约 1.8 分钟 — 分片用 <code>qwen2.5:latest</code>（7B）粗读，终稿用 <code>gemma3:12b</code>（12B / 8.2GB）精修。质量和速度最平衡，<b>推荐默认用这档</b>。</li><li><b>高质</b> 约 5.7 分钟 — 全程 <code>gemma3:12b</code>（12B）。最忠实原文、术语最全，正式交作业或汇报用这档。</li></ul><p>三档只换模型组合，不改 GPU 参数：flash-attn、q8_0 KV cache、ctx 8192、单模型驻留是全局固定的，三档共享同一套。所以「快」来自小模型和短输出上限，不来自调硬件。</p><p class="font-semibold text-slate-700 mt-3 mb-1">③ 「分片摘要模型」到底是什么</p><p>素材超过约 5000 字会自动切成几段：先逐段粗提炼，再合成终稿。这个下拉决定「粗提炼那一步由谁做」。</p><ul><li>留空（与主模型相同）：全部交给主模型，最省心，但慢一些。</li><li>选 qwen2.5：快模型粗读 + 主模型写终稿 —— 这就是「均衡」档在做的事。</li><li>短素材（几千字以内）根本不会分片，这个选项用不上。</li></ul><p class="font-semibold text-slate-700 mt-3 mb-1">④ 显示调试信息</p><p>勾上后结果下方多一块，列出：实际用的模型、上下文长度、切了几片、预算是多少字符、送给模型的完整提示词。排查「这次为什么答偏了」时用，正常交付不用勾。</p><p class="font-semibold text-slate-700 mt-3 mb-1">⑤ 意图 / 你的提问</p><p>用一句话写清这轮想拿素材干什么，例如「弄懂老师讲的三大叙事理论，整理成能背的提纲」。复盘模式会拿它当标尺，评估你的提问够不够具体。</p><p class="font-semibold text-slate-700 mt-3 mb-1">⑥ 复盘该怎么问</p><p>第一轮只给素材 + 意图就行；之后在右下输入框继续问，同一个会话、上下文不丢：</p><ul><li>「第 2 部分你凭什么说老师强调 X？素材原话是哪句？」</li><li>「把我那条提问改写成一句更具体的。」</li><li>「3 分钟课堂复述，帮我压成 5 条要点。」</li></ul><p class="text-xs text-slate-400 mt-2">左侧「复盘会话」里点 open，可回到上次那轮的进度接着问。</p>',
fr: '<p class="font-semibold text-slate-700 mb-1">1. Trois modes</p><ul><li><b>organize</b> — restructure des notes éparses en une note chinoise propre. Sans traduction ni évaluation.</li><li><b>translate_summary</b> — transcription étrangère vers points clés chinois, termes parallèles et glossaire trilingue. Par défaut.</li><li><b>recap</b> — deux niveaux : révision du matériau, puis jugement sur la qualité de votre question.</li></ul><p class="font-semibold text-slate-700 mt-3 mb-1">2. Les « profils de calcul » sont en fait des combinaisons de modèles (mesuré ici, source de 28k caractères)</p><ul><li><b>Rapide</b> ~1,7 min : <code>qwen2.5:latest</code> (7B / 4,7 Go) partout. Le plus court, mais structure plus faible et chiffres omis.</li><li><b>Équilibré</b> ~1,8 min : fragments sur <code>qwen2.5:latest</code> (7B), note finale sur <code>gemma3:12b</code> (12B / 8,2 Go). Recommandé.</li><li><b>Qualité</b> ~5,7 min : <code>gemma3:12b</code> de bout en bout. Le plus fidèle, glossaire complet.</li></ul><p>Les profils ne changent que les modèles ; les réglages GPU (flash-attn, cache KV q8_0, ctx 8192, un seul modèle résident) sont globaux et partagés par les trois.</p><p class="font-semibold text-slate-700 mt-3 mb-1">3. Modèle de résumé de fragments</p><p>Au-delà de ~5000 caractères, la source est découpée : chaque fragment est résumé puis fusionné. Ce menu choisit qui fait ce passage grossier. Vide = modèle principal (simple, plus lent). qwen2.5 = modèle rapide pour les fragments, le principal rédige la note finale (c est le profil Équilibré). Inutile pour les sources courtes.</p><p class="font-semibold text-slate-700 mt-3 mb-1">4. Afficher les détails de débogage</p><p>Ajoute un panneau : modèle utilisé, taille de contexte, nombre de fragments, budget de caractères, prompt complet. Pour diagnostiquer, pas pour livrer.</p><p class="font-semibold text-slate-700 mt-3 mb-1">5. Intention / votre question</p><p>Une phrase : ce que ce tour doit produire, par ex. « comprendre les trois théories et en faire un plan mémorisable ».</p><p class="font-semibold text-slate-700 mt-3 mb-1">6. Comment interroger la révision</p><p>Premier tour : le matériau + votre intention. Ensuite, continuez dans le champ en bas à droite, le contexte est conservé :</p><ul><li>« Dans la partie 2, quelle phrase du texte appuie ton affirmation sur X ? »</li><li>« Reformule ma question en une phrase plus précise. »</li><li>« Condense en 5 points pour une reprise de 3 minutes. »</li></ul>'
};

let activePreset = "balanced";
function updateModeHint(){
  const m = document.getElementById("mode").value;
  const h = MODE_HINT[m];
  document.getElementById("modeHint").textContent = h ? (h[UI] || h.en) : "";
}
function updatePresetHint(){
  const box = document.getElementById("presetHint");
  if (!box) return;
  box.innerHTML = ["fast", "balanced", "quality"].map(k => {
    const row = PRESET_ROWS[k], txt = row[UI] || row.en;
    const on = k === activePreset;
    return `<div class="${on ? "text-slate-800 font-semibold" : "text-slate-400"}">${on ? "▸ " : "· "}${txt}</div>`;
  }).join("");
}
function applyGuide(){
  const g = document.getElementById("guideBody");
  if (g) g.innerHTML = GUIDE[UI] || GUIDE.en;
}
function showTip(el){
  const key = el.dataset.tip, item = TIPS[key];
  const box = document.getElementById("tipbox");
  if (!item || !box) return;
  box.textContent = item[UI] || item.en || "";
  box.style.display = "block";
  const r = el.getBoundingClientRect();
  let x = r.left, y = r.bottom + 6;
  if (x + box.offsetWidth > window.innerWidth - 8) x = window.innerWidth - box.offsetWidth - 8;
  if (x < 8) x = 8;
  if (y + box.offsetHeight > window.innerHeight - 8) y = r.top - box.offsetHeight - 6;
  if (y < 8) y = 8;
  box.style.left = x + "px";
  box.style.top = y + "px";
}
function hideTip(){
  const box = document.getElementById("tipbox");
  if (box) box.style.display = "none";
}
document.addEventListener("mouseover", e => {
  const el = e.target.closest ? e.target.closest("[data-tip]") : null;
  if (el) showTip(el); else hideTip();
});
document.addEventListener("click", e => {
  const el = e.target.closest ? e.target.closest("[data-tip]") : null;
  if (el) showTip(el); else hideTip();
});
window.addEventListener("scroll", hideTip, true);

function setLang(l){
  UI = l;
  ["en","zh","fr"].forEach(x => document.getElementById("lang-" + x).classList.toggle("bg-white/30", l === x));
  applyI18n();
}

async function getState(){
  const s = await (await fetch("/api/state")).json();
  const sel = document.getElementById("model");
  const keep = sel.value;
  sel.innerHTML = s.models.map(m => `<option value="${m}">${m}</option>`).join("");
  if (keep) sel.value = keep;
  const msel = document.getElementById("mapModel");
  const mkeep = msel.value;
  msel.innerHTML = `<option value="" data-i18n="mapModelSame">${t("mapModelSame")}</option>` +
    s.models.map(m => `<option value="${m}">${m}</option>`).join("");
  msel.value = mkeep;
  MODE_DEFAULTS = s.defaults || {};
  renderHistory(s.history);
  loadSessions();
}
let MODE_DEFAULTS = {};
// Compute presets: measured on this machine (RTX 5070 Ti 12GB, 28k-char source).
// fast     -> one small model, ~1.7 min, accurate numbers but weaker structuring
// balanced -> small model for chunks + strong model for the final note, ~1.8 min
// quality  -> strong model end to end, ~5.7 min, most faithful
const PRESETS = {
  fast:     { model: "qwen2.5:latest", map: "" },
  balanced: { model: "gemma3:12b",     map: "qwen2.5:latest" },
  quality:  { model: "gemma3:12b",     map: "" },
};
function applyPreset(name){
  const p = PRESETS[name];
  if (!p) return;
  activePreset = name;
  updatePresetHint();
  const sel = document.getElementById("model"), msel = document.getElementById("mapModel");
  if ([...sel.options].some(o => o.value === p.model)) sel.value = p.model;
  msel.value = p.map;
  document.querySelectorAll(".preset-btn").forEach(b => {
    b.classList.toggle("bg-red-900", b.dataset.preset === name);
    b.classList.toggle("text-white", b.dataset.preset === name);
  });
}
document.querySelectorAll(".preset-btn").forEach(b =>
  b.addEventListener("click", () => applyPreset(b.dataset.preset)));
document.getElementById("mode").addEventListener("change", () => {
  updateModeHint();
  const want = MODE_DEFAULTS[document.getElementById("mode").value];
  if (!want) return;
  const sel = document.getElementById("model");
  if ([...sel.options].some(o => o.value === want)) sel.value = want;
});
async function loadSessions(){
  const s = await (await fetch("/api/sessions")).json();
  const box = document.getElementById("sessions");
  if (!s.sessions.length){ box.innerHTML = `<div class="text-slate-400">${t("noSessions")}</div>`; return; }
  box.innerHTML = s.sessions.map(x => `<div class="flex items-center justify-between border-b py-1">
    <span>${x.session} <span class="text-slate-400">· ${x.mode} · ${x.turns} turn(s) · ${x.mtime}</span></span>
    <button data-session="${x.session}" class="opensess text-teal-700 underline">open</button></div>`).join("");
}
document.getElementById("sessions").addEventListener("click", async e => {
  const name = e.target.dataset.session;
  if (!name) return;
  const j = await (await fetch("/api/session?name=" + encodeURIComponent(name))).json();
  if (!j.ok){ alert(j.error || "error"); return; }
  currentBase = j.session; currentModel = j.meta.model || "";
  const chat = document.getElementById("chat");
  chat.classList.remove("hidden");
  const b = document.getElementById("bubbles");
  b.innerHTML = "";
  j.turns.forEach(m => addBubble(m.role === "user" ? "you" : "ai", m.content));
});
function renderHistory(h){
  const box = document.getElementById("history");
  box.innerHTML = h.map(x => `<div class="flex items-center justify-between border-b py-1">
    <span>${x.file} <span class="text-slate-400">· ${x.mode} · ${(x.source||"").slice(0,40)}</span></span>
    <span class="flex gap-3">
      <button data-file="${x.file}" class="view text-teal-700 underline">view</button>
      <a class="text-slate-500 underline" href="/api/download?file=${encodeURIComponent(x.file)}">download</a>
    </span></div>`).join("");
}
document.getElementById("history").addEventListener("click", e => {
  const f = e.target.dataset.file;
  if (f) viewHistory(f);
});
async function viewHistory(f){
  const r = await fetch("/api/download?file=" + encodeURIComponent(f));
  document.getElementById("result").textContent = await r.text();
  const dl = document.getElementById("download");
  dl.href = "/api/download?file=" + encodeURIComponent(f);
  dl.classList.remove("hidden");
}

function readB64(file){
  return new Promise(res => {
    const r = new FileReader();
    r.onload = () => res({name: file.name, b64: r.result.split(",")[1]});
    r.readAsDataURL(file);
  });
}
document.getElementById("file").addEventListener("change", async e => {
  files = await Promise.all([...e.target.files].map(readB64));
  document.getElementById("filelist").textContent = files.map(f => f.name).join(", ");
});

function opts(){
  return {
    mode: document.getElementById("mode").value,
    output_mode: document.querySelector("input[name=outmode]:checked").value,
    lang: document.getElementById("lang").value,
    model: document.getElementById("model").value,
    map_model: document.getElementById("mapModel").value,
    merge: document.getElementById("merge").checked,
    fresh: document.getElementById("fresh").checked,
    intent: document.getElementById("intent").value,
    debug: document.getElementById("debug").checked,
  };
}
document.querySelectorAll("input[name=outmode]").forEach(r => r.addEventListener("change", () => {
  document.getElementById("lang").disabled = document.querySelector("input[name=outmode]:checked").value === "follow_input";
}));

function renderDebug(d){
  const box = document.getElementById("debugbox");
  if (!d || !document.getElementById("debug").checked){ box.classList.add("hidden"); return; }
  const lines = [
    `model            : ${d.model}`,
    `map model        : ${d.map_model}`,
    `num_ctx          : ${d.num_ctx}`,
    `num_predict      : ${d.num_predict}`,
    `source chars     : ${d.source_chars}`,
    `chunks           : ${d.chunks}`,
    `budget chars     : ${d.budget_chars}`,
    `material chars   : ${d.material_chars}`,
    `corrections len  : ${d.corrections_len}`,
    `previous draft   : ${d.previous_chars} chars`,
    "",
    "--- SYSTEM PROMPT ---",
    d.system_prompt,
    "",
    "--- TASK PROMPT ---",
    d.task_prompt,
    "",
    "--- LANGUAGE INSTRUCTION ---",
    d.lang_instr,
  ];
  if (d.intent) lines.push("", "--- USER INTENT ---", d.intent);
  document.getElementById("debugtext").textContent = lines.join("\n");
  box.classList.remove("hidden");
}

function showError(res, j){
  const down = j.kind === "ollama_down";
  res.style.color = "#fecaca";
  res.style.background = "#7f1d1d";
  res.textContent = down ? t("ollamaDown") : ("✗ " + (j.error || "error"));
}

function poll(task){
  const logEl = document.getElementById("log");
  const stageEl = document.getElementById("stage");
  pollTimer = setInterval(async () => {
    const j = await (await fetch("/api/progress?task=" + task)).json();
    if (!j.ok) { clearInterval(pollTimer); return; }
    logEl.textContent = j.log.join("\n");
    logEl.scrollTop = logEl.scrollHeight;
    stageEl.textContent = j.done ? t("done") : t("running");
    if (j.done){
      clearInterval(pollTimer); pollTimer = null;
      finishJob(j.result);
    }
  }, 700);
}

function finishJob(r){
  const res = document.getElementById("result");
  const btn = document.getElementById("start");
  const fileInput = document.getElementById("file");
  busy = false;
  btn.disabled = false;
  fileInput.disabled = false;
  btn.textContent = t("start");
  if (!r){ showError(res, {error: "no result"}); return; }
  if (!r.ok){ showError(res, r); return; }
  res.style.color = "";
  res.style.background = "";
  res.textContent = r.conclusion;
  currentBase = r.base; currentModel = r.meta.model;
  const dl = document.getElementById("download");
  dl.href = "/api/download?file=" + encodeURIComponent(r.file);
  dl.classList.remove("hidden");
  const chat = document.getElementById("chat");
  if (r.meta.mode === "recap"){ chat.classList.remove("hidden"); document.getElementById("bubbles").innerHTML = ""; }
  else { chat.classList.add("hidden"); }
  renderFidelity(r.fidelity);
  renderDebug(r.debug);
  getState();
}

function renderFidelity(f){
  const box = document.getElementById("fidelitybox");
  if (!f || f.passed || !f.missing || !f.missing.length){ box.classList.add("hidden"); box.textContent = ""; return; }
  box.textContent = t("fidelityWarn")
    .replace("{k}", f.missing.length)
    .replace("{n}", f.checked)
    .replace("{items}", f.missing.join("、"));
  box.classList.remove("hidden");
}

document.getElementById("start").addEventListener("click", async () => {
  if (busy) return;
  if (!files.length){ alert(t("empty")); return; }
  const btn = document.getElementById("start");
  const fileInput = document.getElementById("file");
  const res = document.getElementById("result");
  const logEl = document.getElementById("log");
  const o = opts();
  busy = true;
  btn.disabled = true;
  fileInput.disabled = true;
  btn.textContent = t("processing");
  res.style.color = "";
  res.style.background = "";
  res.textContent = t("processing");
  document.getElementById("fidelitybox").classList.add("hidden");
  logEl.textContent = "";
  document.getElementById("stage").textContent = t("running");
  try {
    const j = await (await fetch("/api/process", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({files: files, ...o})
    })).json();
    if (!j.ok){ finishJob({ok: false, error: j.error || "error", kind: j.kind}); return; }
    poll(j.task);
  } catch (err) {
    finishJob({ok: false, kind: "ollama_down", error: err && err.message});
  }
});

document.getElementById("send").addEventListener("click", async () => {
  const inp = document.getElementById("chatinput");
  const msg = inp.value.trim();
  if (!msg) return;
  addBubble("you", msg); inp.value = "";
  const j = await (await fetch("/api/recap/chat", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({base: currentBase, message: msg, model: currentModel})
  })).json();
  if (j.ok) addBubble("ai", j.answer); else addBubble("ai", "✗ " + (j.error || "error"));
});
function addBubble(who, text){
  const b = document.getElementById("bubbles");
  const div = document.createElement("div");
  div.className = who === "you" ? "text-right" : "";
  const safe = text.replace(/</g, "&lt;");
  div.innerHTML = `<div class="inline-block max-w-[80%] rounded-xl px-3 py-2 whitespace-pre-wrap text-sm ${who === "you" ? "bg-teal-100" : "bg-slate-100"}">${safe}</div>`;
  b.appendChild(div);
}

async function loadPrompts(){
  const p = await (await fetch("/api/prompts")).json();
  const sel = document.getElementById("promptsel");
  sel.innerHTML = p.prompts.map(x => `<option value="${x.name}">${x.name}</option>`).join("");
  if (p.prompts.length) loadPrompt(p.prompts[0].name);
}
async function loadPrompt(name){
  const j = await (await fetch("/api/prompt?name=" + encodeURIComponent(name))).json();
  const ed = document.getElementById("promptedit");
  if (!j.ok){ ed.value = ""; document.getElementById("promptmsg").textContent = j.error || "error"; return; }
  ed.value = j.content;
  document.getElementById("promptmsg").textContent = name;
}
document.getElementById("promptsel").addEventListener("change", e => loadPrompt(e.target.value));
document.getElementById("promptsave").addEventListener("click", async () => {
  const name = document.getElementById("promptsel").value;
  const content = document.getElementById("promptedit").value;
  const j = await (await fetch("/api/prompt", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: name, content: content})
  })).json();
  document.getElementById("promptmsg").textContent = j.ok ? `${name} saved (${j.size} chars)` : ("✗ " + (j.error || "error"));
});

document.getElementById("lang-en").onclick = () => setLang("en");
document.getElementById("lang-zh").onclick = () => setLang("zh");
document.getElementById("lang-fr").onclick = () => setLang("fr");
setLang("en");
getState();
loadPrompts();
</script>
</body></html>"""


def main() -> None:
    print(f"mon_brain web UI -> http://127.0.0.1:{PORT}")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
