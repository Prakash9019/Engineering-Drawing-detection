#!/usr/bin/env python3
"""
Fast Test Runner Hook
Runs lightweight core unit tests (<1.0s) without invoking expensive vision APIs or full-sheet raster processing.
"""
import sys
import os
import subprocess


def main():
    test_file = os.path.join("tests", "test_core.py")
    if not os.path.exists(test_file):
        print("[SKIP] Fast Test Runner: No unit tests found.")
        sys.exit(0)

    print("[*] Fast Test Runner: Executing core unit tests...")
    result = subprocess.run([sys.executable, "-m", "pytest", test_file, "-q"], capture_output=True, text=True)
    if result.returncode == 0:
        print("[OK] Fast Test Runner: Core tests passed.")
        sys.exit(0)
    else:
        print("[!] Fast Test Runner: Core tests failed:")
        print(result.stdout)
        print(result.stderr)
        sys.exit(result.returncode)


if __name__ == '__main__':
    main()
