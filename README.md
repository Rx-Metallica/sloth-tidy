# sloth-tidy

A lazy dev's file and inbox tidier. It sorts, renames and archives a messy folder using a
**local** model (`gemma3:1b` through [Ollama](https://ollama.com)). Your files never leave your machine.

Single Python file, standard library only.

## What it does

- **Sorts** files into `Sorted/<Category>/` by type: Images, Documents, Installers, Archives, Code, ...
- **Labels** documents and `.eml` emails by reading a snippet locally: Invoices, Receipts, Certificates,
  Resumes, Study, Work, Legal, Finance, Travel, Personal, Newsletters. A keyword fallback covers the
  cases where a 1B model answers "Misc".
- **Renames** only messy names (`scan0001.pdf`, `Screenshot (12).png`), e.g. to
  `2026-10-21 Electricity bill October 2024.pdf`. Descriptive names are left alone.
- **Archives** files older than N days into `Sorted/Archive/<year>/`.
- **Stays safe:** dry run by default, never deletes or overwrites, skips files modified in the last
  10 minutes (likely still downloading), and every run can be undone.

## Setup

```
ollama pull gemma3:1b
pip install pypdf        # optional: lets it read PDFs. Without it, PDFs are sorted by filename only.
```

## Usage

```
python tidy.py "C:/path/to/folder"              # dry run: shows the plan, moves nothing
python tidy.py "C:/path/to/folder" --apply      # do it
python tidy.py "C:/path/to/folder" --undo       # revert the last run
```

Options: `--rename {messy,all,off}`, `--archive-days N` (default 180), `--no-ai` (rules only),
`--limit N` (first N files). Environment: `SLOTH_MODEL` (default `gemma3:1b`), `OLLAMA_HOST`.

Weekly on Windows:

```
schtasks /Create /SC WEEKLY /D SUN /ST 20:00 /TN "SlothTidy" /TR "python C:\path\to\tidy.py C:\Users\you\Downloads --apply"
```

## Limitations

- Works on the files directly inside the folder; subfolders are not scanned.
- `gemma3:1b` is text-only, so images are sorted by type and never read.
- Scanned PDFs with no text layer can't be labelled (no OCR).
- A 1B model makes mistakes. That is why the dry run and `--undo` exist. Try `SLOTH_MODEL=gemma3:4b` for better labels.
- Tested on Windows with Ollama and `gemma3:1b`.

## Why open models

Invoices, certificates and private email are exactly the files you don't want on someone else's server.
A local open-weight model keeps them on your disk, works offline and costs nothing per file.
