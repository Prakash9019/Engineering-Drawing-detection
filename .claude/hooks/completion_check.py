#!/usr/bin/env python3
"""
Completion Verification Hook
Verifies that no secrets or unintended .env files are staged before finishing a turn.
"""
import sys
import os
import subprocess


def main():
    try:
        res = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, check=False)
        staged_lines = [l for l in res.stdout.splitlines() if l.startswith(('A ', 'M ', 'R '))]
        forbidden = ['.env', 'secrets', '.pem', '.key']
        violations = []
        for line in staged_lines:
            path = line[2:].strip().strip('"').lower()
            if any(f in path for f in forbidden):
                violations.append(path)

        if violations:
            print(f"[!] Completion Check WARNING: Staged sensitive file detected: {violations}")
            sys.exit(1)
        else:
            print("[OK] Completion Check: Repository safety verified.")
            sys.exit(0)
    except Exception as e:
        # Don't block completion if git isn't available
        sys.exit(0)


if __name__ == '__main__':
    main()
