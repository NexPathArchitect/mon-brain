# mon_brain — local Ollama note organizer / translator-summary / review tool

**Languages / Langues / 语言**：[English](README.md) · [中文](README.zh.md) · [Français](README.fr.md) · [📖 在线文档 / Docs / Documentation](https://nexpatharchitect.github.io/mon-brain/)

**← [中文版本 / Chinese version](README.zh.md)**

A command-line tool running on your local Ollama: read source material → auto-split long text and merge summaries → apply the per-mode prompt → call the local model → export Markdown to `sortie/`.
Data never leaves this machine; it does not depend on the teacher's workshop project, only reusing the same Ollama calling approach.

## Directory layout

```
mon_brain\
├── mon_brain.py        # CLI main script (comments in English for reviewers)
├── app_web.py          # local web UI (standard-library server, same kernel reused)
├── corrections.txt     # static transcription correction table (misspelling -> correct word, editable)
├── prompts\            # per-mode prompts (change requirements here, not in code)
│   ├── organize.md
│   ├── translate_summary.md
│   └── recap.md
├── sources\            # staging folder; _upload\ holds temporary web uploads (auto-generated)
├── sessions\           # recap multi-turn session history (JSON, auto-generated)
├── sortie\             # generated Markdown output (new file each run; originals are never overwritten)
└── .venv\              # isolated virtual environment (requests, pypdf installed)
```

## Environment (already set up)

- Interpreter: `.venv\Scripts\python.exe` (isolated; does not touch system Python or the teacher's project)
- Dependencies: `requests`, `pypdf` (installed)
- Prerequisite: Ollama is running and the required models are pulled

## Modes and default models

| `--mode` | Purpose | Default model | Thinking chain |
|---|---|---|---|
| `organize` | Objective extraction & structured organizing | `qwen2.5:latest` | stripped |
| `translate_summary` | Summary + organizing (mainly FR/EN transcripts) | `gemma3:12b` | stripped |
| `recap` | Deep review + question evaluation + self-check | `qwen3:14b` | **kept** |

The model can be overridden with `--model`.

## GPU / compute configuration (measured on this laptop, 2026-11-23)

Ollama is configured for an RTX 5070 Ti Laptop GPU (12 GB VRAM) via user environment variables; re-apply with `configure_ollama.ps1` after a fresh install.

| Variable | Value | Effect |
|---|---|---|
| `OLLAMA_FLASH_ATTENTION` | `1` | Flash Attention: faster warmup and lighter KV cache |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | halves KV-cache memory with negligible quality loss |
| `OLLAMA_NUM_PARALLEL` | `1` | serial long-document runs, avoids VRAM blow-up |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | one 12B model at a time |
| `OLLAMA_KEEP_ALIVE` | `30m` | no 15–20 s reload before every run |

Verified state: `ollama ps` shows `100% GPU`; the server log reports `offloaded 49/49 layers to GPU`, `flash_attn = enabled`, `K (q8_0) / V (q8_0)`.

Context window defaults to `8192` (override with `MB_NUM_CTX`). Warm measurements for one `gemma3:12b` answer: `8192` → 27.6 s / 8.7 GB; `12288` → 34.3 s / 8.9 GB. **8192 is the sweet spot**; 24% slower otherwise.

Hybrid pipeline (`--map-model`): chunk summaries by a fast model, final note by the strong one. On a 28 694-char French transcript: pure `gemma3:12b` 344 s vs hybrid (`qwen2.5:latest` + `gemma3:12b`) **106 s** (~3.2x). Two safeguards matter:
- the map stage has a hard token cap (`MAP_PREDICT=420`), because small models ignore "be brief";
- re-compression always uses the strong model, since a weak one echoes its input and stalls the loop.

Web UI presets: **Fast** (single `qwen2.5:latest`), **Balanced** (chunk `qwen2.5` + final `gemma3:12b`), **Quality** (pure `gemma3:12b`). The output YAML header records `map_model`.

## How it works: why a long source gets *faster*

A 28 694-char French lecture transcript takes 344 s with `gemma3:12b` alone and 106 s with the hybrid pipeline. The gain does not come from running agents in parallel: it comes from shrinking the work done at every step and paying for the expensive model only once.

```text
source (>5000 chars)
   │  split_text: 3500 chars/chunk, 200-char overlap (no cut sentences, no lost context)
   ▼
[map] one call per chunk -> rough bullets, num_predict=420 (~800 chars cap)
      sequential on purpose: Ollama runs NUM_PARALLEL=1, parallel chunks would contend for VRAM
   ▼
merged summary
   │  only if still over budget -> reduce_to_budget: hierarchical re-compression
   │  with the strong model, max 3 rounds, stops when compression stalls
   ▼
[final] system + task prompt + language rule + your intent + merged summary -> the note
```

Budget: `NUM_CTX=8192` minus the output reserve minus ~700 instruction tokens, estimated at `CHAR_PER_TOKEN=2` → roughly 8 700 safe characters per single run.

Three reasons it is faster: (1) the 12B model writes only the final note, roughly 2.6x slower per token than the 7B one; (2) generation time scales with output length, and every chunk is capped at 420 tokens instead of free-running for thousands; (3) it avoids the expensive route where a 28k source blows the context and `gemma3` has to re-compress round after round, each round a full generation.

How this differs from "multiple sub-agents": chunking is a fixed map-reduce pipeline — same prompt, stateless, no cross-talk, no decisions — it just turns one oversized request into several short ones. Sub-agents each carry their own goal, tools and planning. The speedup here comes from *less total work*, not from concurrency.

## Output language policy

**The body is always written in Chinese.** `--lang` only controls the parallel language for key terms/paragraphs; it never produces a whole foreign-language summary. The ending glossary is always trilingual: **中文 / English / Français**.

| `--lang` | Meaning |
|---|---|
| `zh` | Chinese body only, no paragraph parallel text (default) |
| `en` | Chinese body + English parallel for key terms/paragraphs |
| `fr` | Chinese body + French parallel for key terms/paragraphs |
| `bilingual` | Chinese body + English and French parallel |
| `fr-zh` | legacy alias for `bilingual`, kept for compatibility |

## Web UI

Besides the CLI, a local web dashboard is available (127.0.0.1; data still stays on this machine):

```powershell
cd C:\path\to\mon_brain
.\.venv\Scripts\python.exe app_web.py
# opens http://127.0.0.1:5000 automatically
```

- The UI defaults to **English**; switch to **Chinese / French** in the top-right corner (handy for a reviewer who reads English).
- **Self-documenting UI**: the collapsible *Quick guide* covers the three modes, the measured cost of each preset (with the exact model names), what the chunk-summary model does, what debug details show, how to fill the intent box, and how to keep asking questions in recap. Every control has a `?` badge with a tooltip in the current language, and mode names read `translate_summary · 翻译总结 · Résumé`.
- **Fully offline**: Tailwind is vendored to `static\tailwind.js` and served from `/static/tailwind.js`, so the page no longer calls a CDN. On a fresh machine run `tools\fetch_vendor.ps1` once.
- **`/docs` page**: «说明 / Docs» in the header opens `http://127.0.0.1:5000/docs` in a new tab and renders this README as a web page (zh / en / fr switchable) — principles, architecture, parameter tables, measurements. Handy when you want to re-read instead of re-learn. A back link returns to the workbench.
- Two output modes:
  - `Chinese summary + parallel terms` (default): Chinese body + selected parallel language + trilingual glossary;
  - `Follow source language (whole answer)`: the whole answer is written in the source language (French source → French, English source → English), no Chinese switch.
- Supports: multi-file upload, mode / parallel language / model selection, merge toggle, fresh-start toggle, recap follow-up chat, history list with view and download.
- Recap has an optional "intent / your question" box (CLI `--intent`); leave it empty to review the source only.
- If local Ollama cannot be reached, the result area shows a red alert telling you to start Ollama.
- Uploaded files are staged temporarily in `sources\_upload\`; folders older than 24h are auto-cleaned on the next upload. Final notes still land in `sortie\`.

## Examples

Open PowerShell in this folder:

```powershell
# Use the venv Python directly (no activation needed)
$py = ".\.venv\Scripts\python.exe"

# 1) Single file: summary + organizing (default mode, Chinese body)
& $py mon_brain.py "C:\path\to\notes\lecture.txt" --lang bilingual

# 2) Process every file in a folder into its own note
& $py mon_brain.py "C:\path\to\notes" --mode organize

# 3) Merge all files in the folder into one note (shared context across files)
& $py mon_brain.py "C:\path\to\notes" --mode organize --merge

# 4) Recap mode (keeps thinking chain, evaluates the question + self-check), then enter a follow-up loop
& $py mon_brain.py sources\test_recap.txt --mode recap --interactive

# 5) Continue the previous recap session (multi-turn follow-up)
& $py mon_brain.py sources\test_recap.txt --mode recap --continue

# 6) Override the model; --fresh ignores the previous draft and starts over
& $py mon_brain.py sources\xxx.docx --mode recap --model deepseek-r1:8b --fresh

# 7) List the available prompt files
& $py mon_brain.py --list-prompts
```

## CLI arguments

| Argument | Effect |
|---|---|
| `--mode` | `organize` / `translate_summary` / `recap`; default `translate_summary` |
| `--model` | override the default model for the mode |
| `--lang` | `zh` / `en` / `fr` / `bilingual` (see table above) |
| `--merge` | for a folder input, combine all files into one note instead of one note per file |
| `--fresh` | disable incremental update: do not read the previous draft, start from scratch |
| `--interactive` | recap: after the first draft, enter a follow-up Q&A loop |
| `--intent` | recap only: your question / what you want this round, used to review question quality; leave empty to review the source only |
| `--continue` | load the previous recap session from `sessions\` and continue the multi-turn chat |
| `--list-prompts` | list prompt files under `prompts\` and exit |

## Web UI checks (run after every front-end edit)

The UI ships as one inline `<script>`; a single missing bracket stops the whole script, so language buttons, compute presets and upload all look "dead". Run this first:

```powershell
.\.venv\Scripts\python.exe tools\check_page.py
```

It validates JS syntax (`node --check`), i18n key coverage for en/zh/fr, and that every `getElementById` target exists in the HTML. Exit code 0 means clean.

## Static correction table (corrections.txt)

Common speech-to-text misspellings (e.g. `Olama → Ollama`, `Lama → Llama`, `LocalRoss → localhost`) are listed in `corrections.txt`. The script loads it at startup and injects it into the prompt so the model restores the correct words.

- Format: `wrong => correct  # note`; lines starting with `#` are comments.
- The `[uncertain]` section lists items to confirm; they are marked uncertain in the output and never stated as fact.
- Add your own frequent misspellings at any time.

## Incremental update

When the same source (same mode, same file name) is processed again, the script automatically finds the latest previous draft in `sortie\` and injects it as the "previous draft", so it revises and supplements it instead of starting over. Pass `--fresh` to disable this. Old drafts are never overwritten.

## Merge mode and session memory

- **Merge mode `--merge`**: each file is summarized first; earlier files' conclusions are carried forward as background context for later files, then everything is fused into one note. The output header `source` lists all input files.
- **Recap sessions**: the first recap exchange is stored in `sessions\<source>.json`; `--interactive` lets you keep asking after the first draft, and `--continue` resumes the last history next time. Follow-up turns are appended to the same Markdown note.

## Output rules

- File name: `date_mode_source.md`; an existing name gets an incremented number — **originals are never overwritten**.
- YAML header: source file, mode, model, language, date; recap notes also reference the session file.
- **Long material**: over ~5000 characters it is auto-split into chunks, each summarized, then merged into one note (no hard truncation).
- If the merged source is still too long, a warning suggests processing it in separate runs.
- **Recap mode**: two calls — material review first, then question evaluation + self-check; the thinking chain is kept in its own section; stray outer Markdown fences are stripped automatically.

## Current limitations (later batches)

1. **Repeated recap sections (fixed)**: the second call often re-emits all three sections. `strip_repeated_sections` now truncates at the first repeated heading (4 836 -> 2 501 chars in a real run), and prompts/recap.md states "write each section once".
2. **Paragraph parallel text is unreliable**: local 12B models do not consistently follow "append a foreign parallel to key paragraphs"; most of the time only the trilingual glossary is produced.
3. **Generation length cap**: about 2048–2560 tokens per call; very long answers may still end incompletely.
4. **Scanned PDFs cannot be read**: image PDFs need OCR (llava / easyocr), not yet integrated.
5. **Model ceiling**: local 7B–12B models are weaker than cloud models in French idiomaticness, long-text logic, and English term rendering; review the output yourself and do not treat uncertain items as fact.
6. To do: source-code project parsing, scanned-PDF OCR, segmented continuation of long answers; the web result area currently shows Markdown as plain text, rich rendering is left for later.

## Troubleshooting

- **Cannot reach Ollama / Connection refused**: make sure Ollama is running (taskbar icon, or run `ollama serve` in another terminal).
- **model not found**: the model name does not match `ollama list`; use `--model` with the exact name from the list.
- **PDF yields no text**: most likely a scanned image PDF; OCR is not supported in this version.
- **Garbled Chinese/French text**: the script forces UTF-8; if the console still garbles, make sure you are using the `.venv` Python.


## Copyright and use (not an open-source licence)

Copyright © 2026 NexPathArchitect. **All rights reserved.**

- You may: read the code, learn from the approach, run your own local copy.
- You may not: copy, modify, redistribute or use it commercially without written permission, or present it as your own work.
- If you quote this documentation, credit the author and this repository.

Contact the repository owner for permission. This repository is **not** open source and ships no open-source licence.
