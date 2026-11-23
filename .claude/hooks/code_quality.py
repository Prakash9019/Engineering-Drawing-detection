#!/usr/bin/env python3
"""
Code Quality Hook
Checks Python syntax with py_compile across touched or specified files.
Fast (<50ms), zero external dependencies.
"""
import sys
import os
import py_compile
import subprocess

EXCLUDED_DIRS = {'.git', '.claude_backup', '__pycache__', 'output', 'output2', '.venv'}


def is_excluded(filepath: str) -> bool:
    norm = os.path.normpath(filepath)
    parts = norm.split(os.sep)
    return any(p in EXCLUDED_DIRS for p in parts)


def get_changed_python_files() -> list:
    files = []
    try:
        res = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, check=False)
        for line in res.stdout.splitlines():
            if len(line) > 3:
                filepath = line[3:].strip().strip('"')
                if filepath.endswith('.py') and os.path.isfile(filepath):
                    files.append(filepath)
    except Exception:
        pass
    return files


def check_file(filepath: str) -> bool:
    if not os.path.isfile(filepath) or is_excluded(filepath):
        return True
    try:
        py_compile.compile(filepath, doraise=True)
        return True
    except py_compile.PyCompileError as err:
        print(f"[!] Syntax error in {filepath}:\n{err}")
        return False


def main():
    target_files = [f for f in sys.argv[1:] if f.endswith('.py')]
    if not target_files:
        target_files = get_changed_python_files()

    if not target_files:
        print("[OK] Code Quality: No Python files to check.")
        sys.exit(0)

    has_error = False
    for f in target_files:
        if not check_file(f):
            has_error = True

    if has_error:
        sys.exit(1)
    else:
        print(f"[OK] Code Quality: {len(target_files)} Python file(s) compiled successfully.")
        sys.exit(0)


if __name__ == '__main__':
    main()
