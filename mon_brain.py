# -*- coding: utf-8 -*-
"""
mon_brain — local Ollama personal note organizer / translator-summary / review tool.

Pipeline: read source (single file or folder) -> auto split long material into
chunks, summarize each, merge back (map-reduce) -> apply the prompt in prompts/
by mode -> call local Ollama -> handle the thinking chain -> export Markdown to
sortie/ (a new file each run; originals are never overwritten).

Language model (--lang) controls the *parallel language*, NOT the main body:
    zh        Chinese body only; no paragraph parallel text (default)
    en        Chinese body + English parallel for key terms/paragraphs
    fr        Chinese body + French parallel for key terms/paragraphs
    bilingual Chinese body + English & French parallel
    fr-zh     alias for bilingual (backward compatible)
The term glossary at the end is always trilingual: Chinese / English / French.

Modes:
    organize          objective extraction & structuring             default qwen2.5:latest
    translate_summary translate + summary + organize                   default gemma3:12b
    recap             deep review + question evaluation + self-check   default qwen3:14b
"""

import sys
import os
import re
import json
import time
import pathlib
import argparse
import datetime
import zipfile

# Force UTF-8 on the Windows console to avoid garbled Chinese/French text.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

# ---------- Paths ----------
ROOT = pathlib.Path(__file__).resolve().parent
DIR_PROMPTS = ROOT / "prompts"
DIR_SOURCES = ROOT / "sources"
DIR_SORTIE = ROOT / "sortie"
DIR_SESSIONS = ROOT / "sessions"
FILE_CORRECTIONS = ROOT / "corrections.txt"
FILE_CORRECTIONS_EXAMPLE = ROOT / "corrections.example.txt"

URL_OLLAMA = "http://127.0.0.1:11434/api/chat"

# Mode -> default model (overridable via --model).
DEFAULT_MODELS = {
    "organize": "qwen2.5:latest",
    "translate_summary": "gemma3:12b",
    "recap": "qwen3:14b",
}

# Modes that keep the model's <think> reasoning chain; others strip it.
KEEP_THINK = {"recap"}

# ---------- Map-reduce splitting parameters ----------
SPLIT_THRESHOLD = 5000     # material longer than this is split into chunks
CHUNK_SIZE = 3500          # target characters per chunk
CHUNK_OVERLAP = 200        # overlap between chunks to avoid losing boundary info

# ---------- Generation parameters ----------
# Context window. Override with MB_NUM_CTX (e.g. 12288) without editing code;
# stays a constant per run so Ollama reuses one KV cache allocation instead of
# reallocating on every request (bucket principle).
NUM_CTX = int(os.environ.get("MB_NUM_CTX", "8192"))
NUM_PREDICT = 2048         # max tokens per generation, keeps long answers from filling the context
RECAP_PREDICT = 2560
# Map stage hard output cap. Small models ignore "keep it short" instructions,
# so the length is also enforced by the token budget (~800 chars at 2 chars/token).
MAP_PREDICT = 420
WARN_CHARS = 6000          # merged input above this warns the user to split the source

# Retry policy for transient local-model hiccups (load spike, timeout, 5xx).
RETRY_TIMES = 3
RETRY_BACKOFF = 5          # seconds between retries

# How many times merged summaries may be re-compressed before refusing.
MAX_REDUCE_ROUNDS = 3

# Context budget estimate (conservative). Mixed CJK/Latin text averages roughly
# 2 characters per token; we reserve room for the output plus fixed instructions.
CHAR_PER_TOKEN = 2
RESERVE_INSTRUCTION_TOKENS = 700

SUPPORTED_EXT = {".txt", ".md", ".docx", ".pdf"}

# ---------- Number-fidelity check ----------
# Quantities are the most fragile part of a summary: small models silently merge
# "10 points" with "30%" or drop "6 references". After generating a note we pull
# every quantity out of the source and look for it (number + unit pair) in the note.
QTY_UNITS = (r"%|％|分|部|篇|个|项|分钟|小时|天|周|页|条|份|倍|次"
             r"|minutes?|points?|œuvres?|références?|references?|heures?|jours?|mois")
QTY_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(" + QTY_UNITS + r")", re.I)
BARE_NUM_RE = re.compile(r"(?<![\d.])(\d{2,})(?![\d.%])")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")

# The note is written in Chinese even when the source is French/English, so the same
# quantity can legitimately change unit spelling ("10 points" -> "10 分").
UNIT_SYNONYMS = {
    "points": ["分", "point", "pts"],
    "point": ["分", "points", "pts"],
    "分": ["points", "point", "pts"],
    "minutes": ["分钟", "minute", "min"],
    "minute": ["分钟", "minutes", "min"],
    "分钟": ["minutes", "minute", "min"],
    "œuvres": ["部", "篇", "作品", "œuvre", "oeuvres"],
    "œuvre": ["部", "篇", "作品", "œuvres"],
    "références": ["篇", "个", "条", "reference", "référence"],
    "référence": ["篇", "个", "条", "références"],
    "references": ["篇", "个", "条", "références"],
    "heures": ["小时", "heure", "h"],
    "heure": ["小时", "heures", "h"],
    "jours": ["天", "jour"],
    "jour": ["天", "jours"],
    "mois": ["月"],
    "%": ["%", "％", "百分之"],
    "％": ["%", "％", "百分之"],
}


def _norm_digits(text: str) -> str:
    """Normalize full-width digits and decimal commas so comparisons are stable."""
    return text.translate(FULLWIDTH).replace("，", ",")


def extract_quantities(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ((number, unit) pairs, standalone multi-digit numbers) found in text."""
    text = _norm_digits(text)
    pairs, seen = [], set()
    for num, unit in QTY_RE.findall(text):
        key = (num.replace(",", "."), unit.lower())
        if key not in seen:
            seen.add(key)
            pairs.append((num, unit))
    bare, bare_seen = [], set()
    counts = {}
    for num in BARE_NUM_RE.findall(text):
        counts[num] = counts.get(num, 0) + 1
    for num in counts:
        # Only keep standalone numbers that carry real meaning: 3+ digits (course
        # codes, room numbers) or numbers repeated in the source. One-off small
        # numbers are usually transcription noise and would only create false alarms.
        if YEAR_RE.match(num) or (len(num) < 3 and counts[num] < 3):
            continue
        bare.append(num)
    return pairs, bare


def _number_near_unit(note: str, num: str, unit: str, window: int = 12) -> bool:
    """True if the number occurs somewhere with the unit (or a translated synonym) nearby."""
    accept = [unit.lower()] + [s.lower() for s in UNIT_SYNONYMS.get(unit.lower(), [])]
    pattern = re.compile(r"(?<!\d)" + re.escape(num) + r"(?!\d)")
    for m in pattern.finditer(note):
        chunk = note[max(0, m.start() - window): m.end() + window].lower()
        if any(a in chunk for a in accept):
            return True
    return False


def check_fidelity(source: str, note: str) -> dict:
    """Compare quantities in the source against the generated note.

    Returns {"checked": N, "missing": ["40%", "10分", ...], "passed": bool}.
    This is a warning heuristic, not a proof: it catches dropped or garbled numbers.
    """
    pairs, bare = extract_quantities(source)
    note_n = _norm_digits(note)
    missing = []
    for num, unit in pairs:
        if not _number_near_unit(note_n, num, unit):
            label = f"{num} {unit}" if unit[:1].isascii() else f"{num}{unit}"
            missing.append(label)
    for num in bare:
        if not re.search(r"(?<!\d)" + re.escape(num) + r"(?!\d)", note_n):
            missing.append(num)
    return {"checked": len(pairs) + len(bare), "missing": missing, "passed": not missing}


# System role per mode (internal instruction, not shown to the reader).
SYSTEM = {
    "organize": "You are a note-organizing assistant. Be objective and structured; rely only on the source material; do not invent; mark uncertain points explicitly. Never mention any tool, model, command or term that does not appear in the source. Write the body in Chinese: even if the source is wholly in another language, translate it into Chinese before organizing; do not mix languages inside a single paragraph.",
    "translate_summary": "You are a bilingual note assistant. Summarize accurately in Chinese, keep technical terms with their original form, provide parallel terms in the requested language, and do not invent. Never mention any tool, model, command or term that does not appear in the source. Write the body in Chinese: even if the source is wholly in another language, translate it into Chinese before summarizing; do not mix languages inside a single paragraph.",
    "recap": "You are a personal review assistant. Do a double reflection: review the material itself, and evaluate the quality of the user's question while self-checking your own answer for risks. Write the body in Chinese; do not mix languages inside a single paragraph.",
}

# Language instruction injected into the user prompt. The body is always Chinese;
# only the parallel language changes.
LANG_PROMPT = {
    "zh": "Language requirement: write the whole body in Chinese. Do NOT output whole paragraphs in another language. At the end, add a glossary with trilingual terms: 中文 / English / Français.",
    "en": "Language requirement: write the body in Chinese. For key terms and key paragraphs, append an English parallel after the Chinese. Do NOT output a whole English summary. At the end, add a glossary with trilingual terms: 中文 / English / Français.",
    "fr": "Language requirement: write the body in Chinese. For key terms and key paragraphs, append a French parallel after the Chinese. Do NOT output a whole French summary. At the end, add a glossary with trilingual terms: 中文 / English / Français.",
    "bilingual": "Language requirement: write the body in Chinese. For key terms and key paragraphs, append both an English and a French parallel after the Chinese. Do NOT output whole foreign-language summaries. At the end, add a glossary with trilingual terms: 中文 / English / Français.",
}
# Backward-compatible aliases.
LANG_ALIAS = {"fr-zh": "bilingual"}


# ---------- Console messages (switched by language) ----------
MSG = {
    "zh": {
        "processing": "=== 处理：{name}",
        "split": "ℹ 素材 {n} 字符，分为 {k} 片，逐片摘要…",
        "piece": "  片 {i}/{k} …",
        "piece_fail": "  ✗ 片 {i} 摘要失败：{e}",
        "merging": "  合并生成最终笔记…",
        "map_model": "ℹ 混合流水线：分片摘要用 {m}，终稿用 {f}。",
        "fidelity": "⚠ 数字保真自检：{n} 项数字/单位中缺 {k} 项（{items}），请人工核对补齐。",
        "fidelity_ok": "✓ 数字保真自检通过：{n} 项数字/单位都在稿中。",
        "reduce": "  ℹ 合并稿 {n} 字符仍超预算，第 {r} 轮再压缩（{k} 片）…",
        "all_fail": "✗ 所有分片摘要均失败，跳过。",
        "think_stripped": "ℹ 已剥离模型思考链（复盘模式才保留）。",
        "saved": "✓ 已保存：{fp}",
        "conn_fail": "✗ 连不上 Ollama。请先确认 Ollama 已启动（任务栏有图标，或运行 ollama serve）。",
        "req_fail": "✗ 请求 Ollama 出错：{e}",
        "usage": "用法：python mon_brain.py <文件或文件夹> [--mode organize|translate_summary|recap] [--model 模型] [--lang zh|en|fr|bilingual] [--merge] [--fresh] [--interactive] [--continue]",
        "example": "示例：python mon_brain.py \"{src}\" --mode recap --interactive",
        "no_path": "✗ 路径不存在：{p}",
        "no_files": "✗ 该路径下没有可读取的 txt / md / docx / pdf 文件。",
        "header": "模式：{mode} ｜ 模型：{model} ｜ 语言：{lang} ｜ 待处理 {n} 个文件",
        "prompt_list": "prompts/ 目录下可用的提示词文件：",
        "err_file": "✗ 处理 {name} 时出错：{e}",
        "corrections_loaded": "ℹ 已加载静态对照表 {n} 条。",
        "prev_found": "ℹ 找到上一版草稿：{fp}，将在其基础上增量修订。",
        "merge_header": "=== 合并模式：共 {n} 个文件，汇总成一篇",
        "continue_session": "=== 续接会话：{fp}",
        "no_session": "✗ 没有可续接的历史会话。",
        "interactive_hint": "ℹ 进入追问模式（输入空行 / exit 退出）：",
        "followup_saved": "✓ 追问已追加到笔记与会话。",
        "warn_split": "⚠ 合并后素材约 {n} 字符，可能超出上下文；建议拆成多次处理。",
        "budget_overflow": "✗ 合并后素材约 {n} 字符，仍超出单次上下文预算（约 {b} 字符）。请把素材拆成几份分别处理，未生成稿件。",
    },
    "fr": {
        "processing": "=== Traitement : {name}",
        "split": "ℹ Source de {n} caractères, découpée en {k} fragments, résumé séparé…",
        "piece": "  Fragment {i}/{k} …",
        "piece_fail": "  ✗ Échec du fragment {i} : {e}",
        "merging": "  Fusion en la note finale…",
        "map_model": "ℹ Pipeline hybride : résumés par {m}, note finale par {f}.",
        "fidelity": "⚠ Contrôle des chiffres : {k} élément(s) sur {n} absent(s) de la note ({items}) ; à vérifier manuellement.",
        "fidelity_ok": "✓ Contrôle des chiffres : les {n} éléments sont présents dans la note.",
        "reduce": "  ℹ Brouillon fusionné de {n} caractères encore trop long, recompression tour {r} ({k} fragments)…",
        "all_fail": "✗ Tous les fragments ont échoué, élément ignoré.",
        "think_stripped": "ℹ Chaîne de réflexion retirée (conservée uniquement en mode recap).",
        "saved": "✓ Enregistré : {fp}",
        "conn_fail": "✗ Impossible de joindre Ollama. Vérifiez qu'Ollama est démarré (icône dans la barre des tâches, ou lancez ollama serve).",
        "req_fail": "✗ Erreur de requête Ollama : {e}",
        "usage": "Usage : python mon_brain.py <fichier ou dossier> [--mode organize|translate_summary|recap] [--model modèle] [--lang zh|en|fr|bilingual] [--merge] [--fresh] [--interactive] [--continue]",
        "example": "Exemple : python mon_brain.py \"{src}\" --mode recap --interactive",
        "no_path": "✗ Chemin introuvable : {p}",
        "no_files": "✗ Aucun fichier txt / md / docx / pdf lisible à cet emplacement.",
        "header": "Mode : {mode} ｜ Modèle : {model} ｜ Langue : {lang} ｜ {n} fichier(s) à traiter",
        "prompt_list": "Fichiers de prompts disponibles dans prompts/ :",
        "err_file": "✗ Erreur lors du traitement de {name} : {e}",
        "corrections_loaded": "ℹ Table de corrections statique chargée : {n} entrée(s).",
        "prev_found": "ℹ Brouillon précédent trouvé : {fp}, révision incrémentale par-dessus.",
        "merge_header": "=== Mode fusion : {n} fichier(s) combiné(s) en une seule note",
        "continue_session": "=== Reprise de session : {fp}",
        "no_session": "✗ Aucune session historique à reprendre.",
        "interactive_hint": "ℹ Mode questions (ligne vide / exit pour quitter) :",
        "followup_saved": "✓ Suivi ajouté à la note et à la session.",
        "warn_split": "⚠ Source fusionnée d'environ {n} caractères, le contexte risque d'être dépassé ; préférez un traitement séparé.",
        "budget_overflow": "✗ Matériau fusionné d'environ {n} caractères, encore au-delà du budget (~{b}). Divisez la source en parties ; aucune note n'a été générée.",
    },
    "en": {
        "processing": "=== Processing: {name}",
        "split": "ℹ Source of {n} characters, split into {k} chunks, summarizing each…",
        "piece": "  Chunk {i}/{k} …",
        "piece_fail": "  ✗ Chunk {i} failed: {e}",
        "merging": "  Merging into the final note…",
        "map_model": "ℹ Hybrid pipeline: chunk summaries by {m}, final note by {f}.",
        "fidelity": "⚠ Number check: {k} of {n} quantities are missing from the note ({items}); review manually.",
        "fidelity_ok": "✓ Number check passed: all {n} quantities appear in the note.",
        "reduce": "  ℹ Merged draft still {n} chars, over budget; recompressing pass {r} ({k} fragments)…",
        "all_fail": "✗ All chunks failed, item skipped.",
        "think_stripped": "ℹ Thinking chain removed (kept only in recap mode).",
        "saved": "✓ Saved: {fp}",
        "conn_fail": "✗ Cannot reach Ollama. Make sure Ollama is running (taskbar icon, or run ollama serve).",
        "req_fail": "✗ Ollama request error: {e}",
        "usage": "Usage: python mon_brain.py <file or folder> [--mode organize|translate_summary|recap] [--model model] [--lang zh|en|fr|bilingual] [--merge] [--fresh] [--interactive] [--continue]",
        "example": "Example: python mon_brain.py \"{src}\" --mode recap --interactive",
        "no_path": "✗ Path not found: {p}",
        "no_files": "✗ No readable txt / md / docx / pdf files at this location.",
        "header": "Mode: {mode} ｜ Model: {model} ｜ Lang: {lang} ｜ {n} file(s) to process",
        "prompt_list": "Prompt files available in prompts/:",
        "err_file": "✗ Error processing {name}: {e}",
        "corrections_loaded": "ℹ Static correction table loaded: {n} entr(y/ies).",
        "prev_found": "ℹ Previous draft found: {fp}; incremental revision on top of it.",
        "merge_header": "=== Merge mode: {n} file(s) combined into one note",
        "continue_session": "=== Continuing session: {fp}",
        "no_session": "✗ No saved session to continue.",
        "interactive_hint": "ℹ Follow-up mode (empty line / exit to quit):",
        "followup_saved": "✓ Follow-up appended to the note and session.",
        "warn_split": "⚠ Merged source is about {n} characters, context may be exceeded; consider processing separately.",
        "budget_overflow": "✗ Merged material is about {n} chars, still over the single-run budget (~{b} chars). Split the source into parts; no note was produced.",
    },
}


def msg(key: str, lang: str, **kw) -> str:
    langue = MSG.get(lang, MSG["zh"])
    texte = langue.get(key, MSG["zh"].get(key, key))
    kw["lang"] = lang
    return texte.format(**kw) if kw else texte


# ---------- Static correction table ----------
def load_corrections():
    """Parse corrections.txt -> (confirmed[(wrong, correct, note)], uncertain[(...)]).

    Falls back to corrections.example.txt when the private table is absent, so a
    fresh clone still resolves the common tooling misspellings out of the box.
    """
    confirmed, uncertain = [], []
    path = FILE_CORRECTIONS if FILE_CORRECTIONS.exists() else FILE_CORRECTIONS_EXAMPLE
    if not path.exists():
        return confirmed, uncertain
    section = confirmed
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower() == "[uncertain]":
            section = uncertain
            continue
        if "=>" not in line:
            continue
        wrong, rest = line.split("=>", 1)
        note = ""
        if "#" in rest:
            rest, note = rest.split("#", 1)
        section.append((wrong.strip(), rest.strip(), note.strip()))
    return confirmed, uncertain


def corrections_block(confirmed, uncertain) -> str:
    """Build the prompt block that lists known transcription errors."""
    if not confirmed and not uncertain:
        return ""
    lines = ["## Known transcription errors (static table — reference only)",
             "Restore the left-hand misspelling to the right-hand correct word when it actually appears in the source. "
             "Do NOT copy this table into your answer, and do NOT list any entry whose misspelling is absent from the material. "
             "Items under 'To confirm' are not certain; mark them as uncertain instead of stating them as fact.", ""]
    if confirmed:
        lines.append("### Confirmed")
        for w, c, n in confirmed:
            lines.append(f"- {w} → {c}" + (f"（{n}）" if n else ""))
        lines.append("")
    if uncertain:
        lines.append("### To confirm (uncertain — do not treat as fact)")
        for w, c, n in uncertain:
            lines.append(f"- {w} → {c}" + (f"（{n}）" if n else ""))
        lines.append("")
    return "\n".join(lines)


def _normalize(text: str) -> str:
    """Lowercase, turn punctuation into spaces, collapse whitespace (for loose matching)."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def match_corrections(confirmed, uncertain, text: str):
    """Keep entries whose misspelling appears in the source.

    Matching runs on normalized text (punctuation stripped) so a transcription
    like "Olama," or "Olama." still hits; plain words still use word boundaries
    so a short misspelling inside another word does not match by accident.
    """
    norm = _normalize(text)

    def keep(entry) -> bool:
        w = _normalize(entry[0])
        if not w:
            return False
        if any(ch.isdigit() for ch in w) or " " in w:
            return w in norm
        return re.search(r"\b" + re.escape(w) + r"\b", norm) is not None

    return [e for e in confirmed if keep(e)], [e for e in uncertain if keep(e)]


def build_corrections_block(text: str) -> str:
    """Load the table, keep only entries present in the source, and build the prompt block."""
    confirmed, uncertain = load_corrections()
    confirmed, uncertain = match_corrections(confirmed, uncertain, text)
    return corrections_block(confirmed, uncertain)


# ---------- Source reading ----------
def read_text(path: pathlib.Path) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def read_docx(path: pathlib.Path) -> str:
    """Without python-docx: unzip word/document.xml and strip the tags."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p[^>]*>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def read_pdf(path: pathlib.Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[PDF unavailable: pypdf missing; run pip install pypdf in the venv]"
    try:
        pages = [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
        texte = "\n".join(pages)
        if len(texte.strip()) < 200:
            return "[PDF likely scanned (image): OCR is not supported in this version.]"
        return texte
    except Exception as e:
        return f"[PDF read error: {e}]"


def read_file(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        return read_text(path)
    if ext == ".docx":
        return read_docx(path)
    if ext == ".pdf":
        return read_pdf(path)
    return f"[Unsupported format: {ext}]"


def collect_sources(chemin: pathlib.Path) -> list[pathlib.Path]:
    """A file returns itself; a folder is walked for all supported files (sorted)."""
    if chemin.is_file():
        return [chemin]
    return [p for p in sorted(chemin.rglob("*")) if p.suffix.lower() in SUPPORTED_EXT]


# ---------- Prompts ----------
def load_prompt(mode: str) -> str:
    """Load prompts/<mode>.md; fall back to <mode>.zh.md, then a built-in default."""
    for name in (f"{mode}.md", f"{mode}.zh.md"):
        fp = DIR_PROMPTS / name
        if fp.exists():
            return fp.read_text(encoding="utf-8")
    return f"# {mode}\nOrganize the source material into a structured Markdown note; rely only on the source, do not invent.\n\n## Source\n"


def list_prompts(lang: str) -> None:
    print(msg("prompt_list", lang))
    for p in sorted(DIR_PROMPTS.glob("*.md")):
        print("  -", p.name)


# ---------- Context budget ----------
def input_budget_chars(num_predict: int) -> int:
    """Source characters that still fit: context window minus output + instruction headroom."""
    tokens = max(500, NUM_CTX - num_predict - RESERVE_INSTRUCTION_TOKENS)
    return tokens * CHAR_PER_TOKEN


def source_budget_chars(system: str, prompt: str, lang_instr: str,
                        corrections: str, previous: str, num_predict: int) -> int:
    """Budget left for the source after the fixed prompt-side overhead."""
    fixed = len(system) + len(prompt) + len(lang_instr) + len(corrections) + len(previous)
    return input_budget_chars(num_predict) - fixed


# ---------- Splitting ----------
def split_text(texte: str) -> list[str]:
    """Split by paragraph; a too-long paragraph is hard-split by sentence; chunks overlap."""
    paragraphes = [p.strip() for p in re.split(r"\n\s*\n", texte) if p.strip()]
    pieces, courant = [], ""

    def push():
        nonlocal courant
        if courant:
            pieces.append(courant)
            courant = courant[-CHUNK_OVERLAP:] if CHUNK_OVERLAP else ""

    for p in paragraphes:
        if len(p) > CHUNK_SIZE:
            if courant:
                push()
            phrases = re.split(r"(?<=[.!?。！？])\s+", p)
            buf = ""
            for ph in phrases:
                if len(buf) + len(ph) <= CHUNK_SIZE:
                    buf = (buf + " " + ph).strip()
                else:
                    if buf:
                        pieces.append(buf)
                    buf = ph
            if buf:
                pieces.append(buf)
            continue
        if len(courant) + len(p) <= CHUNK_SIZE:
            courant = (courant + "\n\n" + p) if courant else p
        else:
            push()
            courant = p
    if courant:
        pieces.append(courant)
    return pieces


# ---------- Ollama call ----------
def call_ollama(model: str, system: str, messages, num_predict: int = NUM_PREDICT) -> str:
    """messages: either a plain user string, or a list of {role, content} (multi-turn).

    Retries transient failures (connection drop, timeout, 429, 5xx) up to
    RETRY_TIMES with a backoff; client 4xx errors fail fast without retrying.
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    payload = [{"role": "system", "content": system}] + messages
    body = {
        "model": model,
        "messages": payload,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": NUM_CTX, "num_predict": num_predict},
    }
    last_exc: requests.exceptions.RequestException | None = None
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            reponse = requests.post(URL_OLLAMA, json=body, timeout=300)
            reponse.raise_for_status()
            return reponse.json()["message"]["content"] or ""
        except requests.exceptions.RequestException as e:
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            transient = status is None or status == 429 or status >= 500
            if not transient or attempt == RETRY_TIMES:
                raise
            time.sleep(RETRY_BACKOFF * attempt)
    raise last_exc


def clean_fence(texte: str) -> str:
    """Strip an outermost ``` fence the model wraps around the whole answer.

    Only the leading ```lang and trailing ``` pair are removed; fences inside
    the body are kept intact so code blocks do not end up half-open.
    """
    lignes = texte.split("\n")
    while lignes and not lignes[0].strip():
        lignes.pop(0)
    while lignes and not lignes[-1].strip():
        lignes.pop()
    if lignes and re.match(r"^\s*```\w*\s*$", lignes[0]):
        lignes.pop(0)
        if lignes and re.match(r"^\s*```\s*$", lignes[-1]):
            lignes.pop()
    return "\n".join(lignes).strip()


def strip_repeated_sections(texte: str) -> str:
    """Cut a repeated block: models often re-emit the sections they were asked to continue.

    Headings are compared by title with numbering and punctuation removed, so
    "## 二、对你的提问与输入的评估" and "### 2. 评估" collide only when the title
    text really is the same. The text is truncated at the first repeated heading.
    """
    seen = set()
    lignes = texte.split("\n")
    garde = []
    for ligne in lignes:
        m = re.match(r"^#{1,4}\s*(.+)$", ligne.strip())
        if m:
            titre = re.sub(r"[\s\d.、，,:：()（）\-]+", "", m.group(1)).lower()
            if titre and titre in seen:
                break
            if titre:
                seen.add(titre)
        garde.append(ligne)
    return "\n".join(garde).strip()


def split_think(texte: str) -> tuple[str, str]:
    """Separate the <think>...</think> chain from the final answer. Returns (think, conclusion)."""
    m = re.search(r"<think>(.*?)</think>", texte, re.S)
    if not m:
        return "", texte.strip()
    think = m.group(1).strip()
    concl = re.sub(r"<think>.*?</think>", "", texte, flags=re.S).strip()
    return think, concl


# ---------- Incremental update: locate the previous draft ----------
def find_previous(base: str, mode: str):
    if not DIR_SORTIE.exists():
        return None
    hits = list(DIR_SORTIE.glob(f"*_{mode}_{base}*.md"))
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


# ---------- Recap session (multi-turn history) ----------
def session_path(base: str) -> pathlib.Path:
    safe = re.sub(r"\W+", "_", base)[:40] or "session"
    return DIR_SESSIONS / f"{safe}.json"


def load_session(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"messages": [], "meta": {}}
    return {"messages": [], "meta": {}}


def save_session(path: pathlib.Path, data: dict) -> None:
    DIR_SESSIONS.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Generation stages ----------
def summarize_chunk(chunk: str, model: str, system: str, lang_instr: str,
                    corrections: str, context: str = "", strict: bool = False,
                    num_predict: int | None = None) -> str:
    """Map stage: compress one excerpt into a bullet list, with optional earlier context.

    strict=True is used when re-compressing an over-long merged summary: terse bullets only.
    num_predict caps the generated length at the token level, which small models obey even
    when they ignore the "be brief" instruction in the prompt.
    """
    ctx = f"\n\n## Earlier context\n\n{context}" if context else ""
    if strict:
        prompt = ("Compress the following excerpt into a terse bullet list: at most 8 bullets, "
                  "each no longer than 40 characters; keep only key facts, names, numbers and terms; "
                  "drop examples and repetition; do not add anything not in the source.")
    else:
        prompt = ("Compress the following excerpt into a bullet list: keep key facts, terms, "
                  "numbers, commands and names; do not elaborate, do not add what is not in the source. "
                  "Preserve every quantity exactly as stated: counts, points, percentages, deadlines "
                  "and enumerated requirement lists (e.g. '3 works', '2 non-fiction', '10 points', '30%'). "
                  "Never round, merge or paraphrase a number, and keep each condition attached to its item. "
                  "Format: at most 12 bullets, no sub-bullets, each bullet no longer than 60 characters; "
                  "drop examples, anecdotes and repetition.")
    user = f"{prompt}\n\n{lang_instr}\n\n{corrections}{ctx}\n\n## Excerpt\n\n{chunk}"
    return call_ollama(model, system, user, num_predict=num_predict or MAP_PREDICT).strip()


def reduce_to_budget(materiel: str, model: str, system: str, lang_instr: str,
                     corrections: str, budget: int, lang: str, notify=None) -> str:
    """Hierarchically re-compress merged summaries until they fit the single-run budget."""
    rounds = 0
    while len(materiel) > budget and rounds < MAX_REDUCE_ROUNDS:
        rounds += 1
        pieces = split_text(materiel)
        print(msg("reduce", lang, r=rounds, k=len(pieces), n=len(materiel)))
        if notify:
            notify(f"Re-compressing pass {rounds}: {len(materiel)} chars -> {len(pieces)} fragment(s)")
        out = []
        for piece in pieces:
            try:
                out.append(summarize_chunk(piece, model, system, lang_instr, corrections, strict=True))
            except requests.exceptions.RequestException as e:
                print(msg("piece_fail", lang, i=len(out) + 1, e=e))
        nouveau = "\n\n---\n\n".join(x for x in out if x)
        # Stop if compression stalled, so we do not loop on no progress.
        if not nouveau or len(nouveau) >= len(materiel):
            break
        materiel = nouveau
    return materiel


def generate_final(mode: str, model: str, system: str, prompt: str, lang_instr: str,
                   corrections: str, material: str, fused: bool = False, previous: str = "") -> tuple[str, str]:
    """Single final note. Returns (think, conclusion). fused=True: input is chunk summaries to merge."""
    fusion = ("\n\nNote: the material below is merged from chunk summaries; "
              "fuse it into one coherent note, do not list the chunks one by one.") if fused else ""
    prev = f"\n\n## Previous draft (revise on top of it; keep what still holds, fix errors, add what is new)\n\n{previous}" if previous else ""
    user = f"{prompt}\n\n{lang_instr}\n\n{corrections}{fusion}{prev}\n\n## Source\n\n{material}"
    brut = call_ollama(model, system, user)
    return split_think(brut)


def generate_recap(model: str, system: str, prompt: str, lang_instr: str, corrections: str,
                   material: str, previous: str = "", intent: str = "") -> tuple[str, str]:
    """Recap in two calls: (1) material review; (2) question/intent evaluation + self-check.

    The second call receives both the original source (to check claims against) and the
    user's stated intent/question this round (so the question can actually be evaluated).
    """
    prev = f"\n\n## Previous draft\n\n{previous}" if previous else ""
    user1 = (f"{prompt}\n\n{lang_instr}\n\n{corrections}{prev}\n\n"
             "## Task 1: produce only section 1 (material review: facts, insights, todos).\n\n"
             f"## Source\n\n{material}")
    t1, c1 = split_think(call_ollama(model, system, user1))
    c1 = strip_repeated_sections(c1)

    intent_block = (f"## User's intent / question this round\n\n{intent.strip()}"
                    if intent.strip() else
                    "## User's intent / question this round\n\n(No explicit intent was provided; "
                    "evaluate the input file itself and how it was handed over.)")
    user2 = (f"{prompt}\n\n{lang_instr}\n\n"
             "## Task 2: produce only sections 2 (question/input evaluation) and 3 (self-check). "
             "Do not repeat section 1; do not wrap the answer in a Markdown code fence.\n\n"
             f"{intent_block}\n\n"
             f"## Original source (refer back to it for the self-check)\n\n{material}\n\n"
             f"## Review from step 1\n\n{c1}")
    t2, c2 = split_think(call_ollama(model, system, user2, num_predict=RECAP_PREDICT))
    c2 = strip_repeated_sections(c2)

    think = "\n\n".join(x for x in (t1, t2) if x)
    conclusion = c1 + "\n\n---\n\n" + c2
    # Models often re-emit all three sections in the second call. Dedupe once more
    # on the combined text so the final note keeps each section exactly once.
    conclusion = strip_repeated_sections(conclusion)
    return think, conclusion


# ---------- Markdown export ----------
def export_note(think: str, conclusion: str, meta: dict, source_names: list[str],
                session_ref: str | None = None) -> pathlib.Path:
    DIR_SORTIE.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    base = re.sub(r"\W+", "_", meta["base"])[:40] or "sans_nom"

    nom = f"{date}_{meta['mode']}_{base}.md"
    fp = DIR_SORTIE / nom
    i = 2
    while fp.exists():
        fp = DIR_SORTIE / f"{date}_{meta['mode']}_{base}_{i}.md"
        i += 1

    lignes = [
        "---",
        f"source: {', '.join(source_names)}",
        f"mode: {meta['mode']}",
        f"model: {meta['model']}",
        f"lang: {meta['lang']}",
        f"date: {date}",
    ]
    if meta.get("map_model") and meta["map_model"] != meta["model"]:
        lignes.append(f"map_model: {meta['map_model']}")
    if meta.get("fidelity_missing"):
        lignes.append(f"fidelity_missing: {meta['fidelity_missing']}")
    if session_ref:
        lignes.append(f"session: {session_ref}")
    lignes += ["---", ""]
    if meta["mode"] in KEEP_THINK and think:
        lignes += ["## Thinking chain (model reasoning)", "", think, ""]
    lignes += [conclusion, ""]

    fp.write_text("\n".join(lignes), encoding="utf-8")
    return fp


# ---------- Follow-up interactive loop (recap multi-turn) ----------
def interactive_loop(sess_path: pathlib.Path, note_fp: pathlib.Path | None,
                     model: str, system: str, lang: str) -> None:
    session = load_session(sess_path)
    print(msg("interactive_hint", lang))
    while True:
        try:
            user_input = input("> ").strip()
        except EOFError:
            break
        if user_input in ("", "exit", "quit", "q"):
            break
        session["messages"].append({"role": "user", "content": user_input})
        try:
            raw = call_ollama(model, system, session["messages"][1:], num_predict=RECAP_PREDICT)
        except requests.exceptions.RequestException as e:
            print(msg("req_fail", lang, e=e))
            continue
        _, answer = split_think(raw)
        answer = clean_fence(answer)
        session["messages"].append({"role": "assistant", "content": answer})
        save_session(sess_path, session)
        if note_fp is not None:
            with note_fp.open("a", encoding="utf-8") as f:
                f.write(f"\n\n## Follow-up ({datetime.datetime.now().strftime('%H:%M')})\n\n"
                        f"**You:** {user_input}\n\n**AI:** {answer}\n")
        print(msg("followup_saved", lang))


# ---------- Single source processing ----------
def process_single(path: pathlib.Path, mode: str, model: str, lang: str, lang_instr: str,
                   fresh: bool, interactive: bool, intent: str = "",
                   map_model: str | None = None) -> None:
    """Process one source file.

    map_model: optional cheaper model used for the map (chunk summary) stage and
    hierarchical re-compression; the final note is always written by `model`.
    """
    print(msg("processing", lang, name=path.name))
    texte = read_file(path).strip()
    prompt = load_prompt(mode)
    system = SYSTEM[mode]
    corrections = build_corrections_block(texte)

    # Incremental: reuse the latest previous draft for the same source + mode.
    base = path.stem
    previous = ""
    if not fresh:
        prev_path = find_previous(base, mode)
        if prev_path:
            previous = prev_path.read_text(encoding="utf-8")
            print(msg("prev_found", lang, fp=prev_path.name))

    num_predict = RECAP_PREDICT if mode == "recap" else NUM_PREDICT
    budget = source_budget_chars(system, prompt, lang_instr, corrections, previous, num_predict)
    map_m = map_model or model
    if map_m != model:
        print(msg("map_model", lang, m=map_m, f=model))

    # Level 1: split when long by threshold OR when the source would exceed the budget.
    est_decoupe = len(texte) > SPLIT_THRESHOLD or len(texte) > budget
    if est_decoupe:
        pieces = split_text(texte)
        print(msg("split", lang, n=len(texte), k=len(pieces)))
        resume_pieces, contexte = [], ""
        for i, piece in enumerate(pieces, 1):
            print(msg("piece", lang, i=i, k=len(pieces)))
            try:
                s = summarize_chunk(piece, map_m, system, lang_instr, corrections, contexte)
                resume_pieces.append(s)
                contexte = s  # carry the previous chunk summary forward as context
            except requests.exceptions.RequestException as e:
                print(msg("piece_fail", lang, i=i, e=e))
        materiel = "\n\n---\n\n".join(p for p in resume_pieces if p)
        if not materiel:
            print(msg("all_fail", lang))
            return
        print(msg("merging", lang))
    else:
        materiel = texte

    # Level 2: if merged material still exceeds budget, re-compress it hierarchically;
    # only refuse if even that cannot bring it under the limit.
    # Re-compression uses the strong model on purpose: a weak model tends to echo the
    # input back, which stalls the loop and wrongly aborts the whole run.
    if len(materiel) > budget:
        materiel = reduce_to_budget(materiel, model, system, lang_instr, corrections, budget, lang)
    if len(materiel) > budget:
        print(msg("budget_overflow", lang, n=len(materiel), b=budget))
        return

    if len(materiel) > WARN_CHARS:
        print(msg("warn_split", lang, n=len(materiel)))

    try:
        if mode == "recap":
            think, conclusion = generate_recap(model, system, prompt, lang_instr, corrections,
                                              materiel, previous, intent=intent)
        else:
            think, conclusion = generate_final(mode, model, system, prompt, lang_instr, corrections,
                                                materiel, fused=est_decoupe, previous=previous)
    except requests.exceptions.ConnectionError:
        print(msg("conn_fail", lang))
        return
    except requests.exceptions.RequestException as e:
        print(msg("req_fail", lang, e=e))
        return

    if mode not in KEEP_THINK and think:
        print(msg("think_stripped", lang))
    conclusion = clean_fence(conclusion)

    # Number-fidelity self-check: the source quantities must survive into the note.
    fid = check_fidelity(texte, conclusion)
    if fid["missing"]:
        print(msg("fidelity", lang, n=fid["checked"], k=len(fid["missing"]),
                  items=", ".join(fid["missing"][:12])))
    else:
        print(msg("fidelity_ok", lang, n=fid["checked"]))

    meta = {"mode": mode, "model": model, "lang": lang, "base": base, "map_model": map_m,
            "fidelity_missing": len(fid["missing"])}
    sess_path = session_path(base) if mode == "recap" else None
    if sess_path:
        intent_note = f"\n\n## User's intent / question\n\n{intent.strip()}" if intent.strip() else ""
        session = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt + "\n\n" + lang_instr + intent_note},
                {"role": "assistant", "content": conclusion},
            ],
            "meta": {"source": path.name, "mode": mode, "model": model, "lang": lang, "intent": intent.strip()},
        }
        save_session(sess_path, session)

    fp = export_note(think, conclusion, meta, [path.name],
                     session_ref=sess_path.name if sess_path else None)
    print(msg("saved", lang, fp=fp))

    if mode == "recap" and interactive:
        interactive_loop(sess_path, fp, model, system, lang)


# ---------- Merge mode: several sources into one note, shared context ----------
def process_merge(files: list[pathlib.Path], mode: str, model: str, lang: str,
                  lang_instr: str, fresh: bool, map_model: str | None = None) -> None:
    print(msg("merge_header", lang, n=len(files)))
    map_m = map_model or model
    if map_m != model:
        print(msg("map_model", lang, m=map_m, f=model))
    prompt = load_prompt(mode)
    system = SYSTEM[mode]
    folder_base = files[0].parent.name or "merged"
    all_text = "\n".join(read_file(p) for p in files)
    corrections = build_corrections_block(all_text)

    previous = ""
    if not fresh:
        prev_path = find_previous(folder_base, mode)
        if prev_path:
            previous = prev_path.read_text(encoding="utf-8")
            print(msg("prev_found", lang, fp=prev_path.name))

    resumes, contexte = [], []
    for path in files:
        print(msg("processing", lang, name=path.name))
        texte = read_file(path).strip()
        ctx = "\n".join(contexte)
        try:
            if len(texte) > SPLIT_THRESHOLD:
                pieces = split_text(texte)
                print(msg("split", lang, n=len(texte), k=len(pieces)))
                for i, piece in enumerate(pieces, 1):
                    print(msg("piece", lang, i=i, k=len(pieces)))
                    resumes.append(summarize_chunk(piece, map_m, system, lang_instr, corrections, ctx))
            else:
                resumes.append(summarize_chunk(texte, map_m, system, lang_instr, corrections, ctx))
            contexte.append(f"## {path.name}\n" + (resumes[-1] if resumes else ""))
        except requests.exceptions.RequestException as e:
            print(msg("err_file", lang, name=path.name, e=e))

    materiel = "\n\n---\n\n".join(s for s in resumes if s)
    if not materiel:
        print(msg("all_fail", lang))
        return
    budget = source_budget_chars(system, prompt, lang_instr, corrections, previous, NUM_PREDICT)
    if len(materiel) > budget:
        materiel = reduce_to_budget(materiel, model, system, lang_instr, corrections, budget, lang)
    if len(materiel) > budget:
        print(msg("budget_overflow", lang, n=len(materiel), b=budget))
        return
    if len(materiel) > WARN_CHARS:
        print(msg("warn_split", lang, n=len(materiel)))
    print(msg("merging", lang))

    try:
        think, conclusion = generate_final(mode, model, system, prompt, lang_instr, corrections,
                                            materiel, fused=True, previous=previous)
    except requests.exceptions.ConnectionError:
        print(msg("conn_fail", lang))
        return
    except requests.exceptions.RequestException as e:
        print(msg("req_fail", lang, e=e))
        return

    conclusion = clean_fence(conclusion)
    fid = check_fidelity(all_text, conclusion)
    if fid["missing"]:
        print(msg("fidelity", lang, n=fid["checked"], k=len(fid["missing"]),
                  items=", ".join(fid["missing"][:12])))
    else:
        print(msg("fidelity_ok", lang, n=fid["checked"]))
    meta = {"mode": mode, "model": model, "lang": lang, "base": folder_base, "map_model": map_m,
            "fidelity_missing": len(fid["missing"])}
    fp = export_note(think, conclusion, meta, [p.name for p in files])
    print(msg("saved", lang, fp=fp))


# ---------- Continue a saved recap session ----------
def continue_session(base: str, mode: str, model: str, lang: str) -> None:
    sess_path = session_path(base)
    session = load_session(sess_path)
    if not session.get("messages"):
        print(msg("no_session", lang))
        return
    print(msg("continue_session", lang, fp=sess_path.name))
    system = SYSTEM[mode]
    note_fp = find_previous(base, mode)
    interactive_loop(sess_path, note_fp, model, system, lang)


# ---------- Entry point ----------
def main() -> None:
    parser = argparse.ArgumentParser(description="Local Ollama organize / translate-summary / review tool")
    parser.add_argument("source", nargs="?", help="source file or folder path")
    parser.add_argument("--mode", choices=list(DEFAULT_MODELS), default="translate_summary",
                        help="processing mode (default translate_summary)")
    parser.add_argument("--model", help="override the default model")
    parser.add_argument("--map-model", default=None,
                        help="hybrid pipeline: cheaper model for chunk summaries / re-compression, "
                             "final note still written by --model")
    parser.add_argument("--lang", default="zh",
                        help="parallel language: zh / en / fr / bilingual (fr-zh alias)")
    parser.add_argument("--merge", action="store_true",
                        help="merge all files in the folder into one note")
    parser.add_argument("--fresh", action="store_true",
                        help="do not reuse the previous draft (disable incremental update)")
    parser.add_argument("--continue", dest="continue_session", action="store_true",
                        help="continue a saved recap session")
    parser.add_argument("--interactive", action="store_true",
                        help="recap: enter a follow-up Q&A loop after the first draft")
    parser.add_argument("--intent", default="",
                        help="recap only: your question/intent this round, used for the question-quality review")
    parser.add_argument("--list-prompts", action="store_true", help="list prompts/ files and exit")
    args = parser.parse_args()

    lang = LANG_ALIAS.get(args.lang, args.lang)
    if lang not in LANG_PROMPT:
        lang = "zh"
    lang_instr = LANG_PROMPT[lang]

    if args.list_prompts:
        list_prompts(lang)
        return

    if args.continue_session:
        base = pathlib.Path(args.source).stem if args.source else "session"
        model = args.model or DEFAULT_MODELS[args.mode]
        continue_session(base, args.mode, model, lang)
        return

    if not args.source:
        print(msg("usage", lang))
        print(msg("example", lang, src=DIR_SOURCES))
        return

    chemin = pathlib.Path(args.source)
    if not chemin.exists():
        print(msg("no_path", lang, p=chemin))
        return

    model = args.model or DEFAULT_MODELS[args.mode]
    fichiers = collect_sources(chemin)
    if not fichiers:
        print(msg("no_files", lang))
        return

    print(msg("header", lang, mode=args.mode, model=model, n=len(fichiers)))

    if args.merge and len(fichiers) > 1:
        process_merge(fichiers, args.mode, model, lang, lang_instr, args.fresh,
                      map_model=args.map_model)
    else:
        for f in fichiers:
            try:
                process_single(f, args.mode, model, lang, lang_instr,
                               fresh=args.fresh, interactive=args.interactive, intent=args.intent,
                               map_model=args.map_model)
            except Exception as e:
                print(msg("err_file", lang, name=f.name, e=e))


if __name__ == "__main__":
    main()
