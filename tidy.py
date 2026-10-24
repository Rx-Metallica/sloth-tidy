#!/usr/bin/env python3
"""
sloth-tidy - sorts, renames and archives your messy folders. 100% local (Ollama + gemma3:1b).

    python tidy.py "C:/Users/you/Downloads"            dry run: shows the plan, moves nothing
    python tidy.py "C:/Users/you/Downloads" --apply    actually do it
    python tidy.py "C:/Users/you/Downloads" --undo     put everything back

What it does:
  * sorts files into Sorted/<Category>/ by type (Images, Documents, Installers, ...)
  * documents and .eml emails are read locally and labelled by the model (Invoices, Resumes, Work, ...)
  * messy names like "scan0001.pdf" or "Screenshot (12).png" get a clean name
  * files untouched for a long time go to Sorted/Archive/<year>/
Nothing is ever deleted or overwritten, and every run can be undone. Stdlib only.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
if not HOST.startswith("http"):
    HOST = "http://" + HOST
MODEL = os.environ.get("SLOTH_MODEL", "gemma3:1b")

LABELS = ["Invoices", "Receipts", "Certificates", "Resumes", "Study", "Work", "Legal",
          "Finance", "Travel", "Personal", "Newsletters", "Misc"]

CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".svg"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt"},
    "Spreadsheets": {".xls", ".xlsx", ".csv", ".ods"},
    "Slides": {".ppt", ".pptx", ".odp"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".webm"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Installers": {".exe", ".msi", ".apk", ".iso", ".dmg"},
    "Code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".html", ".css", ".json", ".ipynb", ".sh", ".bat", ".ps1"},
    "Emails": {".eml"},
}
TEXT_EXTS = {".txt", ".md", ".rtf"}
SKIP_EXTS = {".crdownload", ".part", ".tmp", ".download", ".partial"}
SKIP_NAMES = {"desktop.ini", "thumbs.db"}
AI_CATEGORIES = {"Documents", "Emails"}
SETTLE_SECONDS = 600  # ignore files touched in the last 10 min (probably still downloading)


def die(msg):
    print(f"tidy: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------- file helpers ----------

def category_of(ext):
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


def should_skip(p, now):
    if p.name.startswith(".") or p.name.lower() in SKIP_NAMES or p.suffix.lower() in SKIP_EXTS:
        return True
    st = p.stat()
    attrs = getattr(st, "st_file_attributes", 0)  # Windows only: hidden (2) / system (4)
    if attrs & 0x2 or attrs & 0x4:
        return True
    return now - st.st_mtime < SETTLE_SECONDS


def sanitize(name, limit=80):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].strip(" .")


MESSY_PREFIX = re.compile(
    r"(img|dsc|dscn|pxl|scan|screenshot|screen shot|image|document|untitled|download|whatsapp (image|video|audio))[\W_]*\d",
    re.I)


def is_messy(stem):
    return bool(
        re.fullmatch(r"[\W_\d]*", stem)                 # only digits/symbols
        or re.fullmatch(r"[0-9a-f\-_]{16,}", stem, re.I)  # hash-like
        or MESSY_PREFIX.match(stem)
    )


def unique(dest, taken):
    n, cand = 1, dest
    while cand.exists() or cand in taken:
        n += 1
        cand = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
    return cand


# ---------- reading content ----------

def snippet_of(p):
    ext, text = p.suffix.lower(), ""
    try:
        if ext in TEXT_EXTS:
            text = p.read_text(encoding="utf-8", errors="replace")[:4000]
        elif ext == ".docx":
            with zipfile.ZipFile(p) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
            text = re.sub(r"<[^>]+>", " ", xml)
        elif ext == ".pdf":
            from pypdf import PdfReader  # optional: pip install pypdf
            text = PdfReader(str(p)).pages[0].extract_text() or ""
    except Exception:
        text = ""
    return re.sub(r"\s+", " ", text).strip()[:1200]


def read_eml(p):
    """Returns (subject, sender, date, body_snippet)."""
    try:
        with open(p, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
        subject = str(msg["subject"] or "")
        sender = str(msg["from"] or "")
        try:
            date = parsedate_to_datetime(str(msg["date"])).replace(tzinfo=None)
        except Exception:
            date = datetime.fromtimestamp(p.stat().st_mtime)
        part = msg.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part else ""
        body = re.sub(r"<[^>]+>", " ", body)
        return subject, sender, date, re.sub(r"\s+", " ", body).strip()[:800]
    except Exception:
        return "", "", datetime.fromtimestamp(p.stat().st_mtime), ""


# ---------- the model ----------

SYSTEM = (
    "You sort files. Reply with JSON only, in this exact shape: "
    '{"label": "<one of ' + ", ".join(LABELS) + '>", "title": "<3 to 6 word descriptive title>"}\n'
    "Pick Misc if unsure. Never invent details that are not in the text.\n"
    'Example input: File: scan0001.pdf\nText: Electricity bill October 2024 total due 82.50\n'
    'Example output: {"label": "Invoices", "title": "Electricity bill October 2024"}'
)


def ollama_status():
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=5) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return "down"
    return "ok" if MODEL in names or f"{MODEL}:latest" in names else "nomodel"


def ask_ai(name, text):
    payload = {
        "model": MODEL, "stream": False, "format": "json",
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"File: {name}\nText: {text or '(no readable text)'}"}],
        "options": {"temperature": 0.1, "num_ctx": 2048},
    }
    req = urllib.request.Request(f"{HOST}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(json.load(r)["message"]["content"])
    except Exception:
        return None, ""
    label = data.get("label") if data.get("label") in LABELS else None
    title = sanitize(" ".join(str(data.get("title", "")).split()[:8]), 60)
    return label, title


KEYWORDS = {
    "Invoices": ["invoice", "bill", "amount due", "total due", "balance due"],
    "Receipts": ["receipt", "payment received", "order confirmation", "thank you for your purchase"],
    "Certificates": ["certificate", "certification", "certified", "completion", "awarded", "participation"],
    "Resumes": ["resume", "curriculum vitae", "cv", "work experience"],
    "Legal": ["agreement", "contract", "terms and conditions", "nda", "hereby"],
    "Finance": ["bank statement", "account statement", "tax", "payslip", "salary slip"],
    "Travel": ["boarding pass", "itinerary", "flight", "hotel booking", "reservation"],
    "Study": ["lecture", "assignment", "syllabus", "exam", "chapter"],
    "Newsletters": ["unsubscribe", "newsletter", "view in browser"],
}


def keyword_label(text):
    """Fallback for when the 1B model says 'Misc' or gives an invalid label."""
    low = text.lower()
    for label, words in KEYWORDS.items():
        if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in words):
            return label
    return None


# ---------- planning ----------

def stamp_name(p, cat, mtime):
    low = p.stem.lower()
    base = "Screenshot" if low.startswith(("screenshot", "screen shot")) else {
        "Images": "Image", "Videos": "Video", "Audio": "Audio"}.get(cat)
    return f"{base} {mtime:%Y-%m-%d %H%M}" if base else p.stem


def build_plan(folder, args, use_ai):
    root = folder / "Sorted"
    files = [p for p in sorted(folder.iterdir()) if p.is_file()]
    now, moves, taken = time.time(), [], set()
    todo = [p for p in files if not should_skip(p, now)]
    if args.limit:
        todo = todo[: args.limit]
    for i, p in enumerate(todo, 1):
        ext = p.suffix.lower()
        cat = category_of(ext)
        mtime = datetime.fromtimestamp(p.stat().st_mtime)
        label, new_stem = None, p.stem
        want_rename = args.rename == "all" or (args.rename == "messy" and is_messy(p.stem))

        if cat == "Emails":
            subject, sender, date, body = read_eml(p)
            if use_ai:
                print(f"[{i}/{len(todo)}] reading {p.name}", file=sys.stderr)
                label, _ = ask_ai(p.name, f"From: {sender}. Subject: {subject}. {body}")
                label = label if label not in (None, "Misc") else keyword_label(f"{subject} {sender} {body}")
            if args.rename != "off" and subject and (want_rename or args.rename == "messy"):
                new_stem = f"{date:%Y-%m-%d} {sanitize(subject, 70)}"
        elif cat in AI_CATEGORIES and use_ai:
            print(f"[{i}/{len(todo)}] reading {p.name}", file=sys.stderr)
            text = snippet_of(p)
            label, title = ask_ai(p.name, text)
            label = label if label not in (None, "Misc") else keyword_label(f"{p.name} {text}")
            if want_rename and title and text:
                new_stem = f"{mtime:%Y-%m-%d} {title}"
        elif args.rename != "off" and is_messy(p.stem):
            new_stem = stamp_name(p, cat, mtime)

        parts = [cat]
        if (now - p.stat().st_mtime) > args.archive_days * 86400:
            parts = ["Archive", str(mtime.year), cat]
        if label and label != "Misc":
            parts.append(label)
        dest = unique(root.joinpath(*parts) / (sanitize(new_stem) + ext), taken)
        if dest == p:
            continue
        taken.add(dest)
        moves.append((p, dest))
    return moves


# ---------- apply / undo ----------

def log_path(folder):
    return folder / "Sorted" / ".tidy-log.json"


def load_log(folder):
    try:
        return json.loads(log_path(folder).read_text(encoding="utf-8"))
    except Exception:
        return []


def apply_plan(folder, moves):
    done = []
    for src, dst in moves:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            done.append([str(src), str(dst)])
        except Exception as e:
            print(f"  skipped {src.name}: {e}", file=sys.stderr)
    runs = load_log(folder)
    runs.append({"time": datetime.now().isoformat(timespec="seconds"), "moves": done})
    log_path(folder).write_text(json.dumps(runs, indent=1), encoding="utf-8")
    print(f"\nMoved {len(done)} files. Undo any time with: python tidy.py \"{folder}\" --undo")


def undo(folder):
    runs = load_log(folder)
    if not runs:
        die("nothing to undo.")
    run, restored = runs.pop(), 0
    for src, dst in reversed(run["moves"]):
        s, d = Path(src), Path(dst)
        if d.exists() and not s.exists():
            shutil.move(str(d), str(s))
            restored += 1
    log_path(folder).write_text(json.dumps(runs, indent=1), encoding="utf-8")
    root = folder / "Sorted"  # tidy up empty folders
    for dirpath, _, _ in os.walk(root, topdown=False):
        if dirpath != str(root) and not os.listdir(dirpath):
            os.rmdir(dirpath)
    print(f"Restored {restored} files from the run at {run['time']}.")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Sort, rename and archive a messy folder with a local model.")
    ap.add_argument("folder", nargs="?", default=str(Path.home() / "Downloads"))
    ap.add_argument("--apply", action="store_true", help="actually move files (default is a dry run)")
    ap.add_argument("--undo", action="store_true", help="revert the last --apply run")
    ap.add_argument("--rename", choices=["messy", "all", "off"], default="messy",
                    help="rename only messy names (default), everything, or nothing")
    ap.add_argument("--archive-days", type=int, default=180, help="archive files older than this (default 180)")
    ap.add_argument("--no-ai", action="store_true", help="rules only, don't call the model")
    ap.add_argument("--limit", type=int, help="only process the first N files (good for a first try)")
    args = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        die(f"'{folder}' is not a folder.")
    if folder == Path.home() or folder.parent == folder:
        die("refusing to tidy your home folder or a drive root. Point me at a specific folder.")
    if args.undo:
        return undo(folder)

    use_ai = not args.no_ai
    if use_ai:
        status = ollama_status()
        if status == "down":
            die(f"can't reach Ollama at {HOST}. Start it, or use --no-ai for rules only.")
        if status == "nomodel":
            die(f"model not found. Run: ollama pull {MODEL}")
        try:
            import pypdf  # noqa: F401
        except ImportError:
            print("note: pypdf isn't installed, so PDFs are sorted by filename only (pip install pypdf).",
                  file=sys.stderr)

    moves = build_plan(folder, args, use_ai)
    if not moves:
        print("Nothing to tidy. Enjoy the nap.")
        return
    print()
    for src, dst in moves:
        print(f"{src.name}\n    -> {os.path.relpath(dst, folder)}")
    print(f"\n{len(moves)} files planned.")
    if args.apply:
        apply_plan(folder, moves)
    else:
        print("Dry run: nothing was moved. Add --apply to do it.")


if __name__ == "__main__":
    main()
