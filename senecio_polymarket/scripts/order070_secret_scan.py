#!/usr/bin/env python3
"""Tracked-file secret scan, explicitly including Markdown."""
from __future__ import annotations
import re, subprocess, sys
from pathlib import Path
PATTERNS = {
    "OPENAI_KEY": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    "GITHUB_TOKEN": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS_ACCESS_KEY": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "PRIVATE_KEY": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
root = Path(__file__).resolve().parents[2]
files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
scanned=md=0; findings=[]
for raw in files:
    if not raw: continue
    rel=raw.decode("utf-8", "surrogateescape"); p=root/rel
    if not p.is_file(): continue
    try: text=p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError): continue
    scanned += 1; md += int(p.suffix.lower()==".md")
    for label, rx in PATTERNS.items():
        for m in rx.finditer(text): findings.append((rel,label,m.start()))
print(f"SECRET_SCAN_FILES={scanned}")
print(f"SECRET_SCAN_MARKDOWN_FILES={md}")
if findings:
    for rel,label,pos in findings: print(f"SECRET_FINDING={rel}:{label}:{pos}", file=sys.stderr)
    raise SystemExit(1)
print("SECRET_SCAN=PASS")
