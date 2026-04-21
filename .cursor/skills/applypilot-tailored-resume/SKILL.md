---
name: applypilot-tailored-resume
description: >-
  Produces ATS-oriented tailored resume text and PDFs via ApplyPilot's CLI
  (tailor-job). Covers prerequisites, UTF-8 resume handling, Playwright/Chromium
  for PDF, and output paths. Use when the user wants a tailored resume or PDF
  for a job description, mentions applypilot tailor-job, or resume tailoring
  with ApplyPilot.
---

# ApplyPilot tailored resume PDFs

## When this applies

Use the **`applypilot tailor-job`** command when the user (or you) need a **tailored plain-text resume** and optionally a **PDF**, given a **job description** (file, `--text`, or stdin). This is the same tailoring engine as the full pipeline (`applypilot run` → tailor stage).

## Prerequisites (check first)

1. **Install ApplyPilot** in the active environment (`pip install -e .` from the repo root, or `pip install applypilot`).
2. **Tier 2**: an LLM key in `~/.applypilot/.env` (e.g. `GEMINI_API_KEY`, or `OPENAI_API_KEY`, or `LLM_URL`). Run `applypilot doctor` if unsure.
3. **Profile + base resume**: `applypilot init` should have created `~/.applypilot/profile.json` and `~/.applypilot/resume.txt`.
4. **PDF output**: requires **Playwright Chromium**. If PDF fails with “Executable doesn't exist” under `ms-playwright`, run:
   ```bash
   python -m playwright install chromium
   ```

## Resume file encoding (common failure)

`tailor-job` reads the base resume as **UTF-8**. If `UnicodeDecodeError` mentions byte `0x96` or invalid UTF-8, the file is often **Windows-1252**. Fix **before** re-running:

- Prefer: re-save the resume as UTF-8 in the editor, **or**
- One-shot from a shell (adjust paths):
  ```bash
  python -c "from pathlib import Path; p=Path('resume.txt'); p.write_text(p.read_text(encoding='cp1252'), encoding='utf-8')"
  ```
- Or pass a UTF-8 copy: `--resume path/to/resume_utf8.txt`.

## Command: `tailor-job`

```bash
python -m applypilot tailor-job [JOB_FILE] [options]
```

**Job description (one of):**

- Positional **`JOB_FILE`** — path to a text file with the posting.
- **`--text "..."`** — short inline description (long posts: use a file).
- **Stdin** — omit `JOB_FILE` and pipe/redirect input (not from an interactive TTY without file/`--text`).

**Useful options:**

| Option | Role |
|--------|------|
| `--job-title` | Title shown to the model (match the role). |
| `--company` / `-c` | Company name. |
| `--location` / `-l` | Location string. |
| `--resume` | Base resume `.txt` (default: `~/.applypilot/resume.txt`). |
| `-o` / `--output` | Write tailored `.txt` and `{stem}_REPORT.json`. |
| `--pdf` | Generate PDF (needs a write path; see below). |
| `--pdf-out` | Explicit PDF path. |
| `--validation` | `strict` \| `normal` (default) \| `lenient` — same as `applypilot run`. |

**PDF + output paths:**

- **`-o out.txt --pdf`** → PDF defaults to **`out.pdf`** next to `out.txt`.
- **`--pdf` without `-o`** → saves under **`~/.applypilot/tailored_resumes/`** with an auto-generated name, then writes PDF beside that `.txt`.
- **`--pdf-out /path/to/resume.pdf`** → sets PDF destination; implies PDF generation.

## Reliable end-to-end recipe

1. Save the job description to a file (e.g. `jd.txt`).
2. Ensure resume is UTF-8 (see above).
3. Install Playwright browser once if PDF is required: `python -m playwright install chromium`.
4. Run from the repo (or any cwd; use absolute paths if needed):
   ```bash
   python -m applypilot tailor-job jd.txt \
     --job-title "ROLE TITLE" \
     -c "COMPANY" \
     -l "LOCATION" \
     -o tailored.txt \
     --pdf
   ```
5. Confirm outputs:
   - **Tailored text:** path passed to `-o`
   - **Report:** same directory, `{stem}_REPORT.json`
   - **PDF:** same stem as `-o` with `.pdf` unless `--pdf-out` was set

## Shell note (Windows PowerShell)

Older PowerShell may not support `&&`. Use `;` between commands or run commands separately.

## What not to duplicate

Do not re-implement tailoring in ad-hoc prompts; use **`tailor-job`** so validation, judge, header injection, and PDF HTML pipeline stay consistent with the rest of ApplyPilot.
