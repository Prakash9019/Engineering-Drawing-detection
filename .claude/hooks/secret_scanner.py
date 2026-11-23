#!/usr/bin/env python3
"""
Secret Scanner Hook
Fast scanner that prevents committing API keys, private keys, or tokens.
Runs on PostToolUse or pre-commit.
"""
import sys
import os
import re
import subprocess

SUSPICIOUS_PATTERNS = [
    (r'(?i)(AIza[0-9A-Za-z-_]{35})', "Google API Key"),
    (r'(?i)(sk-[a-zA-Z0-9]{20,})', "OpenAI / Anthropic Secret Key"),
    (r'(?i)(ghp_[a-zA-Z0-9]{36})', "GitHub Personal Access Token"),
    (r'(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----)', "Private Key"),
    (r'(?i)(api[_-]?key\s*[:=]\s*["\'][a-zA-Z0-9_\-]{16,}["\'])', "Generic API Key Assignment"),
    (r'(?i)(password\s*[:=]\s*["\'][^"\']{8,}["\'])', "Hardcoded Password"),
]

EXCLUDED_EXTENSIONS = {'.jpg', '.png', '.xlsx', '.docx', '.pdf', '.pyc', '.bak'}
EXCLUDED_DIRS = {'.git', '.claude_backup', '__pycache__', 'output', 'output2', 'node_modules', '.venv'}


def is_excluded(filepath: str) -> bool:
    norm = os.path.normpath(filepath)
    parts = norm.split(os.sep)
    if any(p in EXCLUDED_DIRS for p in parts):
        return True
    ext = os.path.splitext(filepath)[1].lower()
    return ext in EXCLUDED_EXTENSIONS


def scan_file(filepath: str) -> list:
    issues = []
    if not os.path.isfile(filepath) or is_excluded(filepath):
        return issues
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for idx, line in enumerate(f, 1):
                # Ignore comments in test/example files or docstrings
                if "SUSPICIOUS_PATTERNS" in line or "example" in line.lower():
                    continue
                for pat, label in SUSPICIOUS_PATTERNS:
                    if re.search(pat, line):
                        issues.append((filepath, idx, label))
    except Exception:
        pass
    return issues


def get_changed_files() -> list:
    files = []
    try:
        # Check git modified / untracked files
        res = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, check=False)
        for line in res.stdout.splitlines():
            if len(line) > 3:
                filepath = line[3:].strip().strip('"')
                files.append(filepath)
    except Exception:
        pass
    return files


def main():
    target_files = sys.argv[1:]
    if not target_files:
        target_files = get_changed_files()

    all_issues = []
    for f in target_files:
        all_issues.extend(scan_file(f))

    if all_issues:
        print("[!] Secret Scanner: Potential secret(s) detected:")
        for path, line_no, label in all_issues:
            print(f"    - {path}:{line_no} -> {label}")
        print("Please remove secrets or use environment variables.")
        sys.exit(1)
    else:
        print("[OK] Secret Scanner: No secrets detected.")
        sys.exit(0)


if __name__ == '__main__':
    main()
