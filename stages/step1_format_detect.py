#!/usr/bin/env python3
"""
step1_format_detect.py — Format Detection & Native Parsing Agent
================================================================
Step 1 of the CDCI P&ID multi-agent pipeline.

What this does
--------------
1. Inspects the input file (PDF or raster image) to determine its type.
2. For PDFs:
   - Checks for native vector/text layers (pdffonts + pdftotext)
   - If vector data exists → extracts text strings and line metadata natively
     (NO vision model needed for text content)
   - If purely raster → rasterizes the page to a high-res PNG
3. For raster images (JPG/PNG/TIFF):
   - Runs adaptive binarization (CLAHE + adaptive threshold) to enhance faint lines
4. If the document type is ambiguous, calls Gemini 2.5 Flash-Lite (cheapest/fastest)
   to classify it from a thumbnail.
5. Writes a `drawing_context.json` with:
   - document_type: "vector_pdf" | "raster_pdf" | "raster_image"
   - native_text: extracted text block (if vector)
   - native_lines: approximate line geometry (if vector)
   - raster_path: path to the enhanced raster PNG ready for downstream agents
   - page_size_mm, resolution_dpi (if determinable)
   - gemini_classification: raw Gemini response (if used)

Usage
-----
    python step1_format_detect.py drawing.pdf --out output/ --api-key YOUR_KEY
    python step1_format_detect.py drawing.jpg --out output/
    python step1_format_detect.py drawing.pdf --out output/ --force-raster
    python step1_format_detect.py drawing.tiff --out output/ --dpi 300
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Binarization parameters (shared with cloud_detector_v2) ──────────────────
CLAHE_CLIP      = 3.0
CLAHE_TILE      = 8
ADAPTIVE_BLOCK  = 51    # must be odd
ADAPTIVE_C      = 10
RASTER_DPI      = 300   # DPI to rasterize PDFs at

# ── Gemini model for cheap classification ────────────────────────────────────
GEMINI_CLASSIFY_MODEL = "gemini-2.5-flash-lite"   # cheapest/fastest
GEMINI_THUMB_SIZE     = 1024                        # px for classification call

# Minimum number of characters from pdftotext to call a PDF "vector"
VECTOR_TEXT_THRESHOLD = 50


# ═══════════════════════════════════════════════════════════════════════════════
# PDF inspection helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout: {cmd}"


def inspect_pdf_fonts(pdf_path: str) -> dict:
    """
    Run pdffonts to determine if the PDF has an embedded text layer.
    Returns {has_fonts: bool, font_count: int, raw: str}
    """
    rc, stdout, stderr = _run(["pdffonts", pdf_path])
    if rc != 0:
        log.warning("pdffonts failed: %s", stderr)
        return {"has_fonts": None, "font_count": 0, "raw": stderr}
    # pdffonts output: header line + divider + one row per font
    lines = [l for l in stdout.strip().splitlines() if l.strip()]
    # First 2 lines are header + divider
    font_rows = lines[2:] if len(lines) > 2 else []
    return {
        "has_fonts": len(font_rows) > 0,
        "font_count": len(font_rows),
        "raw": stdout[:500],
    }


def extract_pdf_text(pdf_path: str, max_chars: int = 50_000) -> str:
    """
    Extract text from a vector PDF using pdftotext.
    Returns empty string if nothing was found.
    """
    rc, stdout, stderr = _run(["pdftotext", "-layout", pdf_path, "-"])
    if rc != 0:
        log.warning("pdftotext failed: %s", stderr)
        return ""
    text = stdout.strip()
    if len(text) > max_chars:
        log.info("pdftotext: truncating %d chars to %d", len(text), max_chars)
        text = text[:max_chars] + "\n[...truncated]"
    return text


def get_pdf_page_info(pdf_path: str) -> dict:
    """
    Use pdfinfo to get page size and page count.
    Returns {pages, width_mm, height_mm, dpi_hint}
    """
    rc, stdout, _ = _run(["pdfinfo", pdf_path])
    info = {"pages": 1, "width_mm": None, "height_mm": None}
    if rc != 0:
        return info
    for line in stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                info["pages"] = int(line.split(":")[1].strip())
            except ValueError:
                pass
        elif line.startswith("Page size:"):
            # "Page size:      841.89 x 595.28 pts (A4)"
            try:
                parts = line.split(":")[1].strip().split()
                pts_w = float(parts[0])
                pts_h = float(parts[2])
                info["width_mm"]  = round(pts_w * 0.352778, 1)
                info["height_mm"] = round(pts_h * 0.352778, 1)
            except (IndexError, ValueError):
                pass
    return info


def rasterize_pdf(pdf_path: str, out_png: str, dpi: int = RASTER_DPI,
                  page: int = 1) -> bool:
    """
    Rasterize a PDF page to PNG using pdftoppm (part of poppler).
    Falls back to Pillow if pdftoppm is unavailable.
    Returns True on success.
    """
    # Try pdftoppm first (highest quality, handles large engineering drawings)
    rc, _, stderr = _run([
        "pdftoppm", "-r", str(dpi), "-f", str(page), "-l", str(page),
        "-png", "-singlefile", pdf_path,
        str(Path(out_png).with_suffix(""))   # pdftoppm appends .png itself
    ])
    if rc == 0 and Path(out_png).exists():
        log.info("Rasterized PDF via pdftoppm at %d dpi → %s", dpi, out_png)
        return True

    # Fallback: pypdf + Pillow (lower quality but no extra binaries)
    try:
        from pypdf import PdfReader
        from PIL import Image as PILImage
        import io
        reader = PdfReader(pdf_path)
        pg = reader.pages[page - 1]
        # Extract any embedded image from the page
        for img_obj in pg.images:
            img = PILImage.open(io.BytesIO(img_obj.data))
            img.save(out_png)
            log.info("Rasterized PDF via pypdf image extraction → %s", out_png)
            return True
        log.warning("pypdf fallback: no embedded images found on page %d", page)
        return False
    except Exception as e:
        log.error("PDF rasterization failed: %s", e)
        return False


def extract_pdf_lines(pdf_path: str) -> list[dict]:
    """
    Extract line/path geometry from a vector PDF using pdfminer.
    Returns list of {x0, y0, x1, y1, linewidth} dicts.
    Only meaningful for vector PDFs.
    """
    lines = []
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTLine, LTRect, LTCurve, LTLayoutContainer
        for page_layout in extract_pages(pdf_path):
            for element in page_layout:
                if isinstance(element, (LTLine, LTRect, LTCurve)):
                    lines.append({
                        "x0": round(element.x0, 2),
                        "y0": round(element.y0, 2),
                        "x1": round(element.x1, 2),
                        "y1": round(element.y1, 2),
                        "linewidth": round(getattr(element, "linewidth", 0), 2),
                    })
        log.info("Extracted %d line/path objects from PDF", len(lines))
    except Exception as e:
        log.warning("pdfminer line extraction failed: %s", e)
    return lines[:5000]   # cap to avoid context explosion


# ═══════════════════════════════════════════════════════════════════════════════
# Raster image processing
# ═══════════════════════════════════════════════════════════════════════════════

def adaptive_binarize(gray: np.ndarray) -> np.ndarray:
    """
    CLAHE + adaptive Gaussian threshold → ink-enhanced binary.
    Preserves faint arcs that global Otsu drops.
    Same parameters as cloud_detector_v2 (shared constant).
    """
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP,
                              tileGridSize=(CLAHE_TILE, CLAHE_TILE))
    enhanced = clahe.apply(gray)
    binary = cv2.adaptiveThreshold(
        enhanced, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        ADAPTIVE_BLOCK, ADAPTIVE_C
    )
    # Tiny bridge: reconnect 1–2px JPEG artefact breaks
    bridge = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, bridge)
    return binary


def enhance_raster(img_path: str, out_path: str) -> dict:
    """
    Load a raster image, apply CLAHE + adaptive binarization, save enhanced PNG.
    Returns metadata dict.
    """
    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {img_path}")
    H, W = img.shape[:2]

    gray   = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    binary = adaptive_binarize(gray)

    # Save both the original color image (for Gemini) and the binary (for OpenCV)
    base    = Path(out_path)
    color_p = str(base.parent / (base.stem + "_color.jpg"))
    binary_p= str(base.parent / (base.stem + "_binary.png"))
    cv2.imwrite(color_p, img,    [cv2.IMWRITE_JPEG_QUALITY, 95])
    cv2.imwrite(binary_p, binary)

    log.info("Enhanced raster: %dx%d → %s + %s", W, H, color_p, binary_p)
    return {
        "width_px":  W,
        "height_px": H,
        "color_path":  color_p,
        "binary_path": binary_p,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini classification (only called when type is ambiguous)
# ═══════════════════════════════════════════════════════════════════════════════

_CLASSIFY_PROMPT = """You are inspecting an engineering drawing image.

Classify this document by answering ONLY with a JSON object (no markdown, no explanation):
{
  "document_type": "vector_pdf" | "raster_scan" | "hybrid",
  "has_revision_clouds": true | false,
  "estimated_drawing_type": "PID" | "PFD" | "isometric" | "mechanical" | "civil" | "unknown",
  "image_quality": "high" | "medium" | "low",
  "confidence": 0.0-1.0,
  "notes": "one sentence describing what you see"
}

Definitions:
- vector_pdf: clean crisp lines, uniform text, no scan artifacts, likely exported from CAD
- raster_scan: visible scan grain/noise, slightly skewed text, paper texture, halftone dots
- hybrid: mix of both (e.g. a PDF that was printed, annotated, and re-scanned)
- revision_clouds: scalloped/bumpy closed boundaries drawn around changed areas"""


def _build_gemini_client(api_key: str):
    """Returns (client, sdk_type). Tries new SDK then legacy."""
    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        return client, "new"
    except Exception:
        pass
    try:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        return genai_legacy, "legacy"
    except Exception as e:
        raise RuntimeError(f"No working Gemini SDK found: {e}")


def classify_with_gemini(img_path: str, api_key: str) -> dict:
    """
    Send a thumbnail to Gemini Flash-Lite for cheap document classification.
    Returns parsed classification dict.
    """
    # Downscale to thumbnail for cheap call
    img = cv2.imread(img_path)
    if img is None:
        return {"error": f"cannot read {img_path}"}
    H, W = img.shape[:2]
    scale = GEMINI_THUMB_SIZE / max(H, W)
    if scale < 1.0:
        thumb = cv2.resize(img, (int(W * scale), int(H * scale)),
                           interpolation=cv2.INTER_AREA)
    else:
        thumb = img

    ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        return {"error": "encode failed"}
    img_bytes = buf.tobytes()

    client, sdk = _build_gemini_client(api_key)
    log.info("Calling Gemini %s (%s SDK) for classification...", GEMINI_CLASSIFY_MODEL, sdk)

    try:
        if sdk == "new":
            from google.genai import types as gtypes
            response = client.models.generate_content(
                model=GEMINI_CLASSIFY_MODEL,
                contents=[
                    gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                    gtypes.Part.from_text(text=_CLASSIFY_PROMPT),
                ],
            )
            raw = response.text.strip()
        else:
            import google.generativeai as genai_legacy
            model = genai_legacy.GenerativeModel(GEMINI_CLASSIFY_MODEL)
            import PIL.Image as PILImage
            import io
            pil_img = PILImage.open(io.BytesIO(img_bytes))
            response = model.generate_content([_CLASSIFY_PROMPT, pil_img])
            raw = response.text.strip()

        # Parse JSON
        raw_clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw_clean)
        result["raw_response"] = raw
        log.info("Gemini classification: %s (confidence=%.2f)",
                 result.get("document_type"), result.get("confidence", 0))
        return result

    except json.JSONDecodeError as e:
        log.warning("Gemini response was not valid JSON: %s", e)
        return {"document_type": "unknown", "raw_response": raw, "parse_error": str(e)}
    except Exception as e:
        log.error("Gemini classify call failed: %s", e)
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# Main routing logic
# ═══════════════════════════════════════════════════════════════════════════════

def detect_format_and_parse(
    input_path: str,
    out_dir: str = "output",
    api_key: Optional[str] = None,
    force_raster: bool = False,
    raster_dpi: int = RASTER_DPI,
    page: int = 1,
) -> dict:
    """
    Main entry point. Returns the drawing_context dict and writes
    drawing_context.json to out_dir.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    inp = Path(input_path)
    if not inp.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    ext = inp.suffix.lower()
    ctx: dict = {
        "input_file": str(inp.resolve()),
        "document_type": None,
        "native_text": None,
        "native_lines": None,
        "native_line_count": 0,
        "raster_path": None,
        "binary_path": None,
        "width_px": None,
        "height_px": None,
        "width_mm": None,
        "height_mm": None,
        "pages": 1,
        "resolution_dpi": raster_dpi,
        "gemini_classification": None,
        "routing_method": None,
    }

    # ── ROUTE 1: PDF ──────────────────────────────────────────────────────────
    if ext == ".pdf":
        log.info("Input is PDF — inspecting for vector layers...")
        page_info = get_pdf_page_info(input_path)
        ctx.update({
            "pages":      page_info["pages"],
            "width_mm":   page_info["width_mm"],
            "height_mm":  page_info["height_mm"],
        })

        font_info = inspect_pdf_fonts(input_path)

        if force_raster:
            log.info("--force-raster: skipping vector extraction")
            is_vector = False
        elif font_info["has_fonts"] is None:
            # pdffonts unavailable — use Gemini if key present
            is_vector = False
            log.warning("pdffonts unavailable; defaulting to raster path")
        elif font_info["has_fonts"]:
            # Confirm there's actually extractable text (fonts ≠ text always)
            text = extract_pdf_text(input_path)
            is_vector = len(text.strip()) >= VECTOR_TEXT_THRESHOLD
            if not is_vector:
                log.info("Fonts present but extracted text < %d chars → treating as raster",
                         VECTOR_TEXT_THRESHOLD)
        else:
            is_vector = False
            log.info("No fonts found in PDF → raster path")

        if is_vector:
            log.info("✓ Vector PDF — extracting native text & lines")
            ctx["document_type"]   = "vector_pdf"
            ctx["routing_method"]  = "native_extraction"
            ctx["native_text"]     = extract_pdf_text(input_path)
            lines = extract_pdf_lines(input_path)
            ctx["native_lines"]    = lines
            ctx["native_line_count"] = len(lines)
            # Still rasterize for vision agents that need the image
            raster_png = str(out / f"{inp.stem}_page{page}.png")
            if rasterize_pdf(input_path, raster_png, dpi=raster_dpi, page=page):
                ctx["raster_path"] = raster_png
                meta = enhance_raster(raster_png, str(out / f"{inp.stem}_enhanced"))
                ctx.update({
                    "binary_path": meta["binary_path"],
                    "width_px":    meta["width_px"],
                    "height_px":   meta["height_px"],
                })
        else:
            log.info("Raster PDF — rasterizing page %d at %d dpi...", page, raster_dpi)
            ctx["document_type"]  = "raster_pdf"
            ctx["routing_method"] = "raster_pipeline"
            raster_png = str(out / f"{inp.stem}_page{page}.png")
            if rasterize_pdf(input_path, raster_png, dpi=raster_dpi, page=page):
                ctx["raster_path"] = raster_png
                meta = enhance_raster(raster_png, str(out / f"{inp.stem}_enhanced"))
                ctx.update({
                    "binary_path": meta["binary_path"],
                    "width_px":    meta["width_px"],
                    "height_px":   meta["height_px"],
                })
            else:
                log.error("Failed to rasterize PDF")

        # Optional Gemini classification for metadata enrichment
        if api_key and ctx.get("raster_path"):
            ctx["gemini_classification"] = classify_with_gemini(
                ctx["raster_path"], api_key
            )

    # ── ROUTE 2: Raster image (JPG / PNG / TIFF / BMP) ───────────────────────
    elif ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}:
        log.info("Input is raster image — applying adaptive binarization...")
        ctx["document_type"]  = "raster_image"
        ctx["routing_method"] = "raster_pipeline"

        meta = enhance_raster(input_path, str(out / f"{inp.stem}_enhanced"))
        ctx.update({
            "raster_path":  meta["color_path"],
            "binary_path":  meta["binary_path"],
            "width_px":     meta["width_px"],
            "height_px":    meta["height_px"],
            "resolution_dpi": raster_dpi,
        })

        # Gemini classification (optional enrichment — tells us if revision clouds present)
        if api_key:
            ctx["gemini_classification"] = classify_with_gemini(
                meta["color_path"], api_key
            )

    else:
        raise ValueError(f"Unsupported input format: {ext}. "
                         "Supported: .pdf .jpg .jpeg .png .tif .tiff .bmp")

    # ── Write drawing_context.json ────────────────────────────────────────────
    # Truncate native_lines for JSON output (keep full list in memory)
    json_ctx = {k: v for k, v in ctx.items() if k != "native_lines"}
    if ctx.get("native_lines"):
        json_ctx["native_lines_sample"] = ctx["native_lines"][:20]
        json_ctx["native_line_count"]   = len(ctx["native_lines"])

    ctx_path = str(out / "drawing_context.json")
    with open(ctx_path, "w") as f:
        json.dump(json_ctx, f, indent=2)

    log.info("✓ drawing_context.json written → %s", ctx_path)
    log.info("  document_type : %s", ctx["document_type"])
    log.info("  routing_method: %s", ctx["routing_method"])
    log.info("  raster_path   : %s", ctx.get("raster_path"))
    log.info("  binary_path   : %s", ctx.get("binary_path"))
    log.info("  native_text   : %d chars", len(ctx.get("native_text") or ""))
    log.info("  native_lines  : %d objects", ctx.get("native_line_count", 0))

    return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 1: Detect document format and extract native data or rasterize.")
    parser.add_argument("input", help="PDF, JPG, PNG, or TIFF drawing")
    parser.add_argument("--out", default="output", help="Output directory")
    parser.add_argument("--api-key", help="Gemini API key (optional: for classification call)")
    parser.add_argument("--force-raster", action="store_true",
                        help="Skip vector extraction even if fonts present")
    parser.add_argument("--dpi", type=int, default=RASTER_DPI,
                        help=f"DPI for PDF rasterization (default: {RASTER_DPI})")
    parser.add_argument("--page", type=int, default=1,
                        help="PDF page number to process (default: 1)")
    args = parser.parse_args()

    api_key = (args.api_key
               or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))

    if not api_key:
        log.info("No Gemini API key — skipping classification call (deterministic only)")

    ctx = detect_format_and_parse(
        input_path=args.input,
        out_dir=args.out,
        api_key=api_key,
        force_raster=args.force_raster,
        raster_dpi=args.dpi,
        page=args.page,
    )

    print("\n=== Step 1 Complete ===")
    print(f"  Document type : {ctx['document_type']}")
    print(f"  Routing       : {ctx['routing_method']}")
    print(f"  Raster path   : {ctx.get('raster_path') or '—'}")
    print(f"  Binary path   : {ctx.get('binary_path') or '—'}")
    print(f"  Native text   : {len(ctx.get('native_text') or '')} chars")
    print(f"  Native lines  : {ctx.get('native_line_count', 0)} objects")
    if ctx.get("gemini_classification"):
        g = ctx["gemini_classification"]
        print(f"  Gemini says   : {g.get('document_type')} | "
              f"revision_clouds={g.get('has_revision_clouds')} | "
              f"confidence={g.get('confidence')}")
    print(f"  Output dir    : {args.out}/drawing_context.json")


if __name__ == "__main__":
    main()