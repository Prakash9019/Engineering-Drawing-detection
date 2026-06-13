#!/usr/bin/env python3
"""
stage_visualizer.py — Per-stage annotated images + hand-off JSON
================================================================
CDCI P&ID Pipeline — visual artefacts for the review UI.

For each stage we emit TWO images plus the JSON the next step consumes:

  Stage 5A  detection       → 5a_detection.jpg          (every detected tag)
  Stage SOW detect          → sow_detected.jpg          (colour by SOW status)
            filter applied   → sow_filtered.jpg          (DO-NOT-USE removed)
  Stage DUP detect          → dup_detected.jpg          (duplicates flagged,
                                                          NOT removed — links
                                                          each dup to its primary)
            filter applied   → dup_filtered.jpg          (primaries only)

The "detected" image always shows everything, colour-coded. The "filtered"
image is what the user sees after clicking the filter button. JSON artefacts
mirror each image so the next stage / UI can drive off data, not pixels.

Usage
-----
  python stages/stage_visualizer.py \
      --candidates output/step5a_candidates.json \
      --deduped    output/step5d_deduped.json \
      --sow        output/sow_symbol_memory.json \
      --image      input_drawing.jpg \
      --out        output/stages/
"""
import argparse
import json
import re
from pathlib import Path

import cv2

# ── Colours (BGR) ──────────────────────────────────────────────────────────────
C_PLAIN     = (0, 150, 0)        # generic detection — green
C_IN_SCOPE  = (0, 170, 0)        # SOW IN_SCOPE      — green
C_OUT_SCOPE = (0, 0, 230)        # SOW OUT_OF_SCOPE  — red
C_UNSPEC    = (150, 150, 150)    # SOW UNSPECIFIED   — grey
C_PRIMARY   = (0, 170, 0)        # dedup PRIMARY     — green
C_DUP       = (200, 0, 200)      # dedup DUPLICATE   — magenta
C_LINK      = (255, 180, 0)      # link dup → primary

OVERVIEW_W = 2600


# ═══════════════════════════════════════════════════════════════════════════════
# SOW classification (mirrors step5a_candidate_extraction.apply_sow_filter)
# ═══════════════════════════════════════════════════════════════════════════════

def apply_sow_filter(symbol_name: str, sow_memory: dict) -> str:
    """Return IN_SCOPE | OUT_OF_SCOPE | UNSPECIFIED for a symbol name."""
    if not sow_memory:
        return "UNSPECIFIED"
    blocked = {n.upper() for n in sow_memory.get("blocked_names", [])}
    allowed = {n.upper() for n in sow_memory.get("allowed_names", [])}
    sym = re.sub(r'\s+', ' ', (symbol_name or "").strip().upper())
    if not sym:
        return "UNSPECIFIED"
    if sym in blocked:
        return "OUT_OF_SCOPE"
    if sym in allowed:
        return "IN_SCOPE"
    q = set(sym.split())
    for names, status in [(blocked, "OUT_OF_SCOPE"), (allowed, "IN_SCOPE")]:
        for name in names:
            nw = set(name.split())
            if nw and len(q & nw) / max(len(q), len(nw), 1) >= 0.6:
                return status
    return "UNSPECIFIED"


# ═══════════════════════════════════════════════════════════════════════════════
# Drawing helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _box(c: dict) -> dict:
    return c.get("tag_bbox") or c.get("symbol_bbox") or {}


def _center(b: dict):
    return (int((b.get("x1", 0) + b.get("x2", 0)) / 2),
            int((b.get("y1", 0) + b.get("y2", 0)) / 2))


def _legend(img, items, W):
    """Draw a legend panel top-left. items = [(label, color), ...]."""
    pad, lh, sw = 18, 46, 60
    panel_w = 760
    panel_h = pad * 2 + lh * len(items)
    cv2.rectangle(img, (0, 0), (panel_w, panel_h), (255, 255, 255), -1)
    cv2.rectangle(img, (0, 0), (panel_w, panel_h), (0, 0, 0), 2)
    y = pad
    for label, color in items:
        cv2.rectangle(img, (pad, y + 6), (pad + sw, y + lh - 10), color, -1)
        cv2.putText(img, label, (pad + sw + 16, y + lh - 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
        y += lh


def render(image_path, candidates, color_fn, out_stub, legend_items,
           links=None, title=""):
    """Draw boxes (colour from color_fn) + optional dup→primary links, save
    full-res and a downscaled overview. Returns (fullres_path, overview_path)."""
    img = cv2.imread(image_path)
    H, W = img.shape[:2]

    if links:
        for (ca, cb) in links:
            cv2.line(img, _center(_box(ca)), _center(_box(cb)), C_LINK, 2)

    drawn = 0
    for c in candidates:
        b = _box(c)
        x1, y1, x2, y2 = b.get("x1", 0), b.get("y1", 0), b.get("x2", 0), b.get("y2", 0)
        if x2 <= x1 or y2 <= y1:
            continue
        col = color_fn(c)
        if col is None:
            continue
        cv2.rectangle(img, (x1, y1), (x2, y2), col, 4)
        cv2.putText(img, str(c.get("tag_text", ""))[:16], (x1, max(y1 - 8, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, col, 3)
        drawn += 1

    _legend(img, legend_items + [(f"{title}: {drawn} shown", (0, 0, 0))], W)

    full = f"{out_stub}.jpg"
    cv2.imwrite(full, img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    s = OVERVIEW_W / W
    ov = cv2.resize(img, (OVERVIEW_W, int(H * s)))
    overview = f"{out_stub}_overview.jpg"
    cv2.imwrite(overview, ov, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return full, overview, drawn


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Per-stage annotated images + JSON")
    ap.add_argument("--candidates", default="output/step5a_candidates.json")
    ap.add_argument("--deduped",    default="output/step5d_deduped.json")
    ap.add_argument("--sow",        default="output/sow_symbol_memory.json")
    ap.add_argument("--image",      default="input_drawing.jpg")
    ap.add_argument("--out",        default="output/stages")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    candidates = json.load(open(args.candidates)).get("candidates", [])
    sow_mem = json.load(open(args.sow)) if Path(args.sow).exists() else {}
    manifest = {"stages": []}

    # ── Stage 5A — detection ──────────────────────────────────────────────────
    f, o, n = render(args.image, candidates, lambda c: C_PLAIN,
                     str(out / "5a_detection"),
                     [("Detected tag", C_PLAIN)], title="5A detection")
    manifest["stages"].append({"stage": "5A_detection", "image": f,
                               "overview": o, "count": n,
                               "json": args.candidates})
    print(f"[5A]  detection           {n:4d} tags  → {f}")

    # ── Stage SOW — classify, detect, filter ──────────────────────────────────
    sow_counts = {"IN_SCOPE": 0, "OUT_OF_SCOPE": 0, "UNSPECIFIED": 0}
    for c in candidates:
        st = apply_sow_filter(c.get("symbol_name", ""), sow_mem)
        c["sow_status"] = st
        sow_counts[st] += 1

    sow_json = str(out / "sow_candidates.json")
    json.dump({"sow_summary": sow_counts, "candidates": candidates},
              open(sow_json, "w"), indent=2)

    def sow_color(c):
        return {"IN_SCOPE": C_IN_SCOPE, "OUT_OF_SCOPE": C_OUT_SCOPE,
                "UNSPECIFIED": C_UNSPEC}[c["sow_status"]]

    f, o, n = render(args.image, candidates, sow_color,
                     str(out / "sow_detected"),
                     [("In scope (USE)", C_IN_SCOPE),
                      ("Out of scope (DO NOT USE)", C_OUT_SCOPE),
                      ("Unspecified", C_UNSPEC)], title="SOW detected")
    manifest["stages"].append({"stage": "SOW_detected", "image": f, "overview": o,
                               "count": n, "json": sow_json,
                               "summary": sow_counts})
    print(f"[SOW] detected            {n:4d} tags  "
          f"(IN={sow_counts['IN_SCOPE']} OUT={sow_counts['OUT_OF_SCOPE']} "
          f"UNSPEC={sow_counts['UNSPECIFIED']})  → {f}")

    # filter = drop OUT_OF_SCOPE (the DO-NOT-USE symbols); keep the rest
    kept = [c for c in candidates if c["sow_status"] != "OUT_OF_SCOPE"]
    sow_filt_json = str(out / "sow_filtered_candidates.json")
    json.dump({"removed_out_of_scope": sow_counts["OUT_OF_SCOPE"],
               "kept": len(kept), "candidates": kept},
              open(sow_filt_json, "w"), indent=2)
    f, o, n = render(args.image, kept, sow_color, str(out / "sow_filtered"),
                     [("In scope (USE)", C_IN_SCOPE), ("Unspecified", C_UNSPEC)],
                     title="SOW filtered")
    manifest["stages"].append({"stage": "SOW_filtered", "image": f, "overview": o,
                               "count": n, "json": sow_filt_json})
    print(f"[SOW] filtered            {n:4d} tags  "
          f"(removed {sow_counts['OUT_OF_SCOPE']} DO-NOT-USE)  → {f}")

    # ── Stage DUP — detect (don't remove) then filter ─────────────────────────
    records = json.load(open(args.deduped)).get("all_records", [])
    by_id = {r.get("candidate_id"): r for r in records}
    n_primary = sum(1 for r in records if r.get("duplicate_status") == "PRIMARY")
    n_dup = sum(1 for r in records if r.get("duplicate_status") == "DISCARDED")

    # links from each duplicate to the primary it belongs to
    links = []
    for r in records:
        if r.get("duplicate_status") == "DISCARDED" and r.get("merged_into") in by_id:
            links.append((r, by_id[r["merged_into"]]))

    def dup_color(c):
        return C_DUP if c.get("duplicate_status") == "DISCARDED" else C_PRIMARY

    f, o, n = render(args.image, records, dup_color, str(out / "dup_detected"),
                     [("Primary tag", C_PRIMARY),
                      ("Duplicate (kept, flagged)", C_DUP),
                      ("dup -> primary link", C_LINK)],
                     links=links, title="DUP detected")
    manifest["stages"].append({"stage": "DUP_detected", "image": f, "overview": o,
                               "count": n, "json": args.deduped,
                               "primary": n_primary, "duplicates": n_dup})
    print(f"[DUP] detected            {n:4d} tags  "
          f"(PRIMARY={n_primary} DUPLICATE={n_dup})  → {f}")

    primaries = [r for r in records if r.get("duplicate_status") == "PRIMARY"]
    f, o, n = render(args.image, primaries, lambda c: C_PRIMARY,
                     str(out / "dup_filtered"),
                     [("Primary tag (deduplicated)", C_PRIMARY)],
                     title="DUP filtered")
    manifest["stages"].append({"stage": "DUP_filtered", "image": f, "overview": o,
                               "count": n,
                               "json": "output/step5_final_output.json"})
    print(f"[DUP] filtered            {n:4d} tags  (duplicates hidden)  → {f}")

    json.dump(manifest, open(out / "manifest.json", "w"), indent=2)
    print(f"\nManifest → {out / 'manifest.json'}")


if __name__ == "__main__":
    main()
