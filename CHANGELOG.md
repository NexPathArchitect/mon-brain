# Changelog

All notable changes to this project. Dates are local (America/Toronto).

## 2026-11-23

### Core (`mon_brain.py`)

- Three modes: `organize`, `translate_summary`, `recap`; prompts live in `prompts/*.md` and can be edited without touching code.
- Chunked map-reduce for long sources (`SPLIT_THRESHOLD=5000`, `CHUNK_SIZE=3500`, 200-char overlap for the map stage).
- Two-level budget guard: estimate the single-run character budget from `NUM_CTX` minus output and instruction reserves, split first, then `reduce_to_budget` (strong model only, max 3 rounds, stops when compression stalls).
- Hybrid pipeline `--map-model`: fast model for chunk summaries, strong model for the final note. Measured 28 694-char source: **344.3 s → 106 s** (≈3.2x).
- Token hard cap on the map stage (`MAP_PREDICT=420`, ~800 chars/chunk): prompt-only length limits did not work, the small model still wrote 2 000+ chars per chunk.
- Number fidelity check: extracts number+unit pairs and flags missing ones; writes `fidelity_missing: N` into the note header and shows an amber bar in the UI.
- Recap deduplication: repeated sections truncated by normalized heading (4 836 → 2 501 chars measurable).
- Incremental update (reuse a previous draft), multi-file merge, multi-turn recap sessions persisted in `sessions/`.
- `MB_NUM_CTX` lets you override the context window without editing code.

### Compute

- Wrote and applied five user-level Ollama variables: `FLASH_ATTENTION=1`, `KV_CACHE_TYPE=q8_0`, `NUM_PARALLEL=1`, `MAX_LOADED_MODELS=1`, `KEEP_ALIVE=30m`; `configure_ollama.ps1` reproduces them after a reinstall.
- Verified in logs: `offloaded 49/49 layers to GPU`, `flash_attn = enabled`, `K (q8_0) / V (q8_0)`.
- Context measured on the reference machine: 8192 → 27.6 s / 8.7 GB; 12288 → 34.3 s / 8.9 GB; **8192 kept as default**.

### Web UI (`app_web.py`)

- Single page, standard library only; task queue + 700 ms progress polling so the page is never "frozen".
- Three compute presets (Fast / Balanced / Quality) that fill both model dropdowns, with the exact model names printed under the buttons.
- Debug panel (model, `num_ctx`, chunk count, char budget, full prompt), multi-session switching, prompt template editor.
- Trilingual UI (en / zh / fr) with mode names shown as `translate_summary · 翻译总结 · Résumé`.
- Inline help: a collapsible quick guide plus a `?` badge with a hover tooltip on every control.
- `/docs` route renders the three READMEs as a web page (Markdown → HTML, no extra dependency).
- Fixed: Chinese filenames broke downloads (HTTP headers are Latin-1) → RFC 5987 encoding.
- Fixed: the whole inline script silently stopped running because one `);` was missing, which looked like "language switch and preset buttons do nothing".
- Offline: Tailwind vendored to `static/tailwind.js` and served locally; no CDN call remains.
- Fixed: stale `app_web.py` process holding port 5000 (and later 5001) served an old page, so edits seemed to do nothing.

### Tooling

- `tools/check_page.py`: seven deterministic checks — inline JS syntax (`node --check`), i18n key coverage for en/zh/fr, tooltip coverage, guide presence, `/docs` rendering, offline asset presence, `getElementById` targets. Exit code 0 = clean. Reverse-tested by deleting one bracket on purpose.
- `tools/fetch_vendor.ps1`: re-fetch the vendored Tailwind build on a fresh machine.
- `tools/build_docs.py`: renders the three READMEs into one static `docs/index.html` with in-place language tabs, served by GitHub Pages (no jump to another file when switching language).
- `configure_ollama.ps1`: one-shot GPU configuration.

### Prompt rules added (`prompts/translate_summary.md`)

1. Keep teacher corrections (`not X, but Y`) as a standalone item, never flatten them into a parallel list.
2. Numbers keep their original unit (`points` ≠ `minutes`); ambiguous units are marked, not guessed.
3. Exactly one glossary, at the end, only words that actually occur in the source.
4. No duplicated content (no second listing of the same points inside a code block).
5. An "uncertainties" section for unclear passages, kept out of the to-do list.

Measured effect: glossary tables 3 → 1, uncertainties section appeared, unit error ("10 minutes") disappeared;
percentage attribution and the "six vs three" nuance were still wrong, and `40 %` was still missing —
see `BACKLOG.md` for why and what comes next.

### Docs

- READMEs in zh / en / fr: GPU configuration, model table, principles and architecture diagram, budget formula,
  page-check instructions, `/docs` and offline notes, limitations.
- `BACKLOG.md`: prioritised next steps with evidence.
- `samples/`: two synthetic materials (French lecture, English meeting notes) so every mode can be tried safely.

## Earlier work (same repository, before this changelog existed)

- First working CLI and venv, end-to-end smoke tests, initial README.
- Output language policy settled: Chinese body always, `--lang` only controls term glosses, trilingual glossary at the end.
- Static correction table with hit-based injection (an unconditional table poisoned outputs with unrelated vocabulary).
- Multi-file merge, incremental update, recap session resume, split warning when the source does not fit.
- 2026-11-23: 许可证定为「保留所有权利」（非开源，仓库不含 LICENSE 文件，三份 README 增版权章节）；英/法 README 顶部加「← 返回中文」；docs 页重建。

