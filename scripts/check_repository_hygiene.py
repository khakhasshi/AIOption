#!/usr/bin/env python3
"""Check the public tree for local state, oversized files, and high-confidence secrets."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 5 * 1024 * 1024
BLOCKED_PARTS = {".longbridge_accounts", ".local-test-data", "backups", "outputs", "reports", "node_modules"}
BLOCKED_SUFFIXES = {".sqlite3", ".sqlite3-shm", ".sqlite3-wal", ".pem", ".p12"}
TEXT_SUFFIXES = {
    "", ".css", ".env", ".html", ".ini", ".js", ".json", ".jsx", ".md",
    ".py", ".sh", ".toml", ".txt", ".yaml", ".yml",
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Longbridge token": re.compile(r"\bhk_[A-Za-z0-9._-]{80,}\b"),
    "macOS home path": re.compile(r"/" r"Users/[^/\s]+/"),
}


def repository_files() -> list[Path]:
    if (ROOT / ".git").is_dir():
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        )
        names = [name for name in result.stdout.decode().split("\0") if name]
        return [ROOT / name for name in names]
    return [path for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts]


def main() -> int:
    files = repository_files()
    findings: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT)
        if any(part in BLOCKED_PARTS for part in relative.parts):
            findings.append(f"blocked path: {relative}")
            continue
        if path.name == ".env" or any(str(relative).endswith(suffix) for suffix in BLOCKED_SUFFIXES):
            findings.append(f"sensitive file: {relative}")
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            findings.append(f"oversized file: {relative} ({path.stat().st_size} bytes)")
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {relative}")
    print(f"Repository files checked: {len(files)}; findings: {len(findings)}")
    for finding in sorted(set(findings)):
        print(f"  {finding}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
