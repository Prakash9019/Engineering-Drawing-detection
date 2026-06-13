#!/usr/bin/env python3
"""
eval_coverage.py — Compare extracted tags vs Annexure-4 ground truth + annotate.

Usage:
  python stages/eval_coverage.py \
      --candidates output/step5a_candidates.json \
      --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
      --image input_drawing.jpg \
      --out output/
"""
import argparse, json, re
from pathlib import Path
import cv2
import openpyxl


def norm(t: str) -> str:
    """Normalize a tag for matching: uppercase, unify unicode dashes/quotes,
    strip spaces/dashes/quotes, and collapse the inch marker so that
    `10"` and `10IN` compare equal."""
    s = (t or '').upper()
    for d in ('—', '–', '―', '−'):
        s = s.replace(d, '-')
    s = re.sub(r'[\s\-"“”‘’`\']+', '', s)
    s = s.replace('IN', '')          # 10IN / 10"  -> 10
    return s


def load_ground_truth(xlsx: str):
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb.worksheets[0]
    tags = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and row[2]:
            tags.append(str(row[2]).strip())
    return tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--register", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", default="output")
    ap.add_argument("--no-annotate", action="store_true")
    args = ap.parse_args()

    gt = load_ground_truth(args.register)
    gt_norm = {norm(t): t for t in gt}

    data = json.load(open(args.candidates))
    cands = data.get("candidates", [])
    cand_norm = {}
    for c in cands:
        cand_norm.setdefault(norm(c.get("tag_text", "")), []).append(c)

    # Match: exact normalized, then substring (gt contained in a candidate or v.v.)
    found, missing = {}, {}
    for gn, gorig in gt_norm.items():
        hit = None
        if gn in cand_norm:
            hit = cand_norm[gn][0]
        else:
            for cn, clist in cand_norm.items():
                if gn and (gn in cn or cn in gn) and abs(len(gn) - len(cn)) <= 2:
                    hit = clist[0]
                    break
        if hit:
            found[gorig] = hit.get("tag_text")
        else:
            missing[gorig] = None

    print(f"\n{'='*60}")
    print(f"GROUND TRUTH COVERAGE  (Annexure-4 = {len(gt)} tags)")
    print(f"{'='*60}")
    print(f"  FOUND   : {len(found)}/{len(gt)}  ({100*len(found)/len(gt):.0f}%)")
    print(f"  MISSING : {len(missing)}/{len(gt)}")
    print(f"  Total extracted candidates: {len(cands)}")
    if found:
        print(f"\n  ✓ Found:")
        for g, c in sorted(found.items()):
            tag_disp = f"(as '{c}')" if norm(c) != norm(g) else ""
            print(f"      {g:<26} {tag_disp}")
    if missing:
        print(f"\n  ✗ MISSING (not extracted):")
        for g in sorted(missing):
            print(f"      {g}")

    # Extra tags found beyond ground truth
    extra = [c.get("tag_text") for c in cands
             if norm(c.get("tag_text", "")) not in gt_norm
             and not any(norm(c.get("tag_text", "")) in g or g in norm(c.get("tag_text", ""))
                         for g in gt_norm)]
    print(f"\n  + Extra tags beyond Annexure-4: {len(set(extra))}")

    # ── Annotation ────────────────────────────────────────────────
    if not args.no_annotate:
        img = cv2.imread(args.image)
        H, W = img.shape[:2]
        gt_norm_set = set(gt_norm)
        for c in cands:
            box = c.get("tag_bbox") or c.get("symbol_bbox") or {}
            x1, y1, x2, y2 = box.get("x1", 0), box.get("y1", 0), box.get("x2", 0), box.get("y2", 0)
            if x2 <= x1 or y2 <= y1:
                continue
            is_gt = norm(c.get("tag_text", "")) in gt_norm_set or any(
                norm(c.get("tag_text", "")) in g or g in norm(c.get("tag_text", "")) for g in gt_norm_set)
            color = (0, 180, 0) if is_gt else (0, 140, 255)  # green=in register, orange=extra
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 4)
            cv2.putText(img, c.get("tag_text", "")[:16], (x1, max(y1 - 8, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 3)
        full = str(Path(args.out) / "step5a_eval_annotated_fullres.jpg")
        cv2.imwrite(full, img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        scale = 2600 / W
        ov = cv2.resize(img, (int(W * scale), int(H * scale)))
        overview = str(Path(args.out) / "step5a_eval_annotated.jpg")
        cv2.imwrite(overview, ov, [cv2.IMWRITE_JPEG_QUALITY, 90])
        print(f"\n  Annotated full-res → {full}")
        print(f"  Annotated overview → {overview}")

    # Save report
    rep = str(Path(args.out) / "eval_coverage_report.json")
    json.dump({"ground_truth_count": len(gt), "found": found, "missing": list(missing),
               "extra_count": len(set(extra)), "extra": sorted(set(extra)),
               "total_candidates": len(cands)}, open(rep, "w"), indent=2)
    print(f"  Report → {rep}\n")


if __name__ == "__main__":
    main()
