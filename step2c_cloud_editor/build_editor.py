#!/usr/bin/env python3
"""
build_editor.py — inline editor.js into index_split.html → editor/index.html

Run this after editing editor/editor.js to regenerate the single-file
editor/index.html that the server actually serves.
"""
from pathlib import Path

here = Path(__file__).resolve().parent / "editor"
html = (here / "index_split.html").read_text()
js = (here / "editor.js").read_text()
html = html.replace('<script src="editor.js"></script>', f"<script>\n{js}\n</script>")
(here / "index.html").write_text(html)
print(f"Built {here / 'index.html'} ({len(html)} chars)")
