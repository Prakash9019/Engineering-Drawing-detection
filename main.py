"""
CDCI Tag Extraction Engine — Main Orchestrator
================================================
Runs the complete extraction pipeline:

  STAGE 1 — Green Cloud Detection         (OpenCV, deterministic)
  STAGE 2 — Title Block Extraction        (Gemini OCR)
  STAGE 3 — Notes Intelligence            (Gemini OCR + reasoning)
  STAGE 4 — Symbol+Tag Detection          (Gemini, cloud-scoped)
  STAGE 5 — Tag Association & Enrichment  (Gemini + ISA decode)
  STAGE 6 — Validation Engine             (7-stage rules)
  STAGE 7 — Excel Output                  (openpyxl)

Usage:
    export GOOGLE_API_KEY="your-key"
    python main.py <input_image> [output.xlsx]

The input image must have revision-scope clouds manually marked in GREEN.
If no green clouds are detected, the system processes the full drawing.
"""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure this script's directory is in Python path (so core/ and pipeline/ are found)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2

from settings import DEBUG_DIR, GEMINI_MODEL, GEMINI_DELAY_SEC
from core.gemini_client import GeminiClient
from pipeline.stage1_cloud import detect_clouds
from pipeline.stage2_title import extract_title_block
from pipeline.stage3_notes import extract_notes
from pipeline.stage4_detect import detect_symbols
from pipeline.stage5_associate import associate_tags
from pipeline.stage6_validate import validate_records
from pipeline.stage7_excel import generate_excel


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run_pipeline(input_path: Path, output_path: Path, model: str = GEMINI_MODEL):
    log = logging.getLogger("CDCI")
    t0 = time.time()

    # ── Load image ──
    log.info("═" * 70)
    log.info(f" CDCI Tag Extraction Engine v5.0")
    log.info("═" * 70)
    log.info(f" Input:  {input_path}")
    log.info(f" Output: {output_path}")
    log.info(f" Model:  {model}")
    log.info("═" * 70)

    image = cv2.imread(str(input_path))
    if image is None:
        raise FileNotFoundError(f"Cannot read image: {input_path}")
    H, W = image.shape[:2]
    log.info(f" Image:  {W}x{H} ({W*H/1e6:.1f} megapixels)")

    # Setup debug directory
    debug_dir = output_path.parent / DEBUG_DIR
    debug_dir.mkdir(parents=True, exist_ok=True)

    # Initialize Gemini client
    gemini = GeminiClient(model=model)

    # ── STAGE 1: Green Cloud Detection (PRIORITY) ──
    log.info("\n┌─ STAGE 1: Revision Cloud Detection " + "─" * 31)
    cloud_result = detect_clouds(image, debug_path=debug_dir, gemini=gemini)
    scope_mask = cloud_result.mask if not cloud_result.is_full_scope else None
    if cloud_result.is_full_scope:
        log.info(f"└─ Result: FULL SCOPE (no green clouds detected)")
    else:
        log.info(f"└─ Result: {len(cloud_result)} clouds, "
                 f"{cloud_result.coverage_pct:.1f}% coverage")

    # Save cloud info JSON
    cloud_json = cloud_result.to_json()
    with open(str(output_path.with_suffix('')) + "_clouds.json", 'w') as f:
        json.dump(cloud_json, f, indent=2)

    # ── STAGE 2: Title Block ──
    log.info("\n┌─ STAGE 2: Title Block Extraction " + "─" * 33)
    title_block = extract_title_block(image, gemini)
    log.info("└─ Done")
    time.sleep(GEMINI_DELAY_SEC)

    # ── STAGE 3: Notes ──
    log.info("\n┌─ STAGE 3: Notes Intelligence " + "─" * 37)
    notes_data = extract_notes(image, gemini)
    log.info("└─ Done")
    time.sleep(GEMINI_DELAY_SEC)

    # ── STAGE 4: Symbol+Tag Detection (cloud-scoped) ──
    log.info("\n┌─ STAGE 4: Engineering Symbol Detection " + "─" * 26)
    detections = detect_symbols(image, gemini, scope_mask=scope_mask)
    log.info(f"└─ Detected: {len(detections)} symbol+tag objects")

    # Save raw detections JSON
    with open(str(output_path.with_suffix('.json')), 'w') as f:
        json.dump(detections, f, indent=2)

    # Save annotated image (blue boxes for tags, red for clouds)
    annotated = image.copy()
    if cloud_result.polygons:
        for poly in cloud_result.polygons:
            cv2.polylines(annotated, [poly.astype(int)],
                          isClosed=True, color=(0, 0, 255), thickness=6)
    for d in detections:
        b = d['box']
        cv2.rectangle(annotated, (b[0], b[1]), (b[2], b[3]), (255, 0, 0), 4)
        cv2.putText(annotated, d['label'], (b[0], b[1] - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
    annot_path = output_path.with_suffix('.jpg')
    cv2.imwrite(str(annot_path), annotated, [cv2.IMWRITE_JPEG_QUALITY, 92])

    if not detections:
        log.warning("No detections found — generating empty register")

    # ── STAGE 5: Tag Association & Enrichment ──
    log.info("\n┌─ STAGE 5: Tag Association & Enrichment " + "─" * 26)
    records = associate_tags(detections, image, gemini, title_block)
    log.info(f"└─ Enriched: {len(records)} records")

    # ── STAGE 6: Validation ──
    log.info("\n┌─ STAGE 6: Validation Engine (7-stage) " + "─" * 27)
    validated = validate_records(records, title_block)
    log.info(f"└─ Validated: {len(validated)} records")

    # ── STAGE 7: Excel Output ──
    log.info("\n┌─ STAGE 7: Excel Output Generation " + "─" * 31)
    generate_excel(validated, title_block, notes_data, cloud_json, output_path)
    log.info(f"└─ Done")

    # ── Summary ──
    elapsed = time.time() - t0
    auto = sum(1 for r in validated if r.get('route') == 'AUTO_ACCEPT')
    rev = sum(1 for r in validated if r.get('route') == 'REVIEW_REQUIRED')
    rej = sum(1 for r in validated if r.get('route') == 'AUTO_REJECT')

    print("\n" + "═" * 70)
    print(" ✓ CDCI EXTRACTION COMPLETE")
    print("═" * 70)
    print(f"   Tags extracted:    {len(validated)}")
    print(f"   Auto-accept:       {auto}")
    print(f"   Review required:   {rev}")
    print(f"   Auto-reject:       {rej}")
    if cloud_result.is_full_scope:
        print(f"   Cloud scope:       FULL DRAWING (no green clouds)")
    else:
        print(f"   Cloud scope:       {len(cloud_result)} clouds, "
              f"{cloud_result.coverage_pct:.1f}% coverage")
    print(f"   Time elapsed:      {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"\n   Excel:    {output_path}")
    print(f"   JSON:     {output_path.with_suffix('.json')}")
    print(f"   Cloud:    {str(output_path.with_suffix('')) + '_clouds.json'}")
    print(f"   Image:    {annot_path}")
    print(f"   Debug:    {debug_dir}/")
    print("═" * 70)

    if validated:
        print("\n TAG REGISTER PREVIEW (first 50 entries):")
        for r in sorted(validated, key=lambda r: r.get('tag_number', ''))[:50]:
            print(f"   {r.get('tag_number','?'):25s} | "
                  f"{r.get('discipline',''):15s} | "
                  f"{r.get('duplicate','NO'):3s} | "
                  f"c={r.get('c_final',0):.2f} | "
                  f"{r.get('tag_description','')[:48]}")
        if len(validated) > 50:
            print(f"   ... and {len(validated) - 50} more (see Excel for full list)")

    return validated


def main():
    parser = argparse.ArgumentParser(
        description="CDCI Tag Extraction Engine — P&ID tag register generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py drawing.jpg
  python main.py drawing.jpg output.xlsx
  python main.py drawing.jpg output.xlsx --model gemini-2.5-pro
  python main.py drawing.jpg output.xlsx --verbose

Cloud Scoping:
  Mark revision-scope cloud boundaries in GREEN on the input drawing.
  The system will detect them and extract only tags within the cloud scope.
  If no green clouds are detected, the full drawing is processed.

Environment:
  GOOGLE_API_KEY=your-api-key   (required — get at aistudio.google.com/apikey)
""",
    )
    parser.add_argument("input", help="Path to input P&ID drawing image")
    parser.add_argument("output", nargs="?",
                        help="Output .xlsx path (default: <input>_TAG_REGISTER.xlsx)")
    parser.add_argument("--model", default=GEMINI_MODEL,
                        help=f"Gemini model name (default: {GEMINI_MODEL})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose logging")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else \
        input_path.parent / f"{input_path.stem}_TAG_REGISTER.xlsx"

    try:
        run_pipeline(input_path, output_path, model=args.model)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        logging.getLogger("CDCI").exception(f"Pipeline failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
