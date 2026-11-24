# Samples

Two synthetic materials so you can try every mode without exposing anyone's real data:

| File | Mode to pick | Why it is useful |
|---|---|---|
| `fr_demo_lecture.txt` | `translate_summary`, `--lang bilingual` | French oral transcript with numbers, constraints, a deadline and a penalty rule — the kind of material where unit and quantity mistakes show up |
| `en_demo_notes.md` | `organize` | Bullet-style meeting notes with decisions and open questions, to check the restructured output |

Both files are written for this repository. No real course, teacher, student or grade appears in them.

Try it:

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py mon_brain.py "samples\fr_demo_lecture.txt" --mode translate_summary --lang bilingual
& $py mon_brain.py "samples\en_demo_notes.md" --mode organize
```

Keep your own material in `sources/`; that folder is git-ignored so nothing personal leaks if
you publish the repository.
