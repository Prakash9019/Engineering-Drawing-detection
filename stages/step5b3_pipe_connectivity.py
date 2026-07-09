#!/usr/bin/env python3
"""
step5b3_pipe_connectivity.py — Gemini Pipeline Connectivity Agent
=================================================================
CDCI P&ID Pipeline — Step 5B3

Reads raw CV pipe segments from step5b and produces a Verified Pipeline
Graph (step5b3_verified_graph.json) that step5b2 can consume instead of
re-running CV detection and gemini_pipe_verify internally.

Three tasks:
  Task A — Gap bridging     : verify 100-300px gaps between pipeline ends
  Task C — Tee vs crossing  : verify degree-3+ junctions
  Task D — Pipeline tracing : trace complete physical pipelines via Gemini

Inputs:
  --segments     output/step5b_pipe_segments.json  (from step5b)
  --associations output/step5b_associations_full.json  (optional, for Task D)
  --image        input_drawing.jpg
  --api-key      $GEMINI_KEY

Output:
  output/step5b3_verified_graph.json

Dry run (cost estimate only, no API calls):
  python stages/step5b3_pipe_connectivity.py \\
      --segments output/step5b_pipe_segments.json \\
      --image input_drawing.jpg --api-key $GEMINI_KEY \\
      --out output/ --dry-run
"""

import argparse
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

import cv2

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5b_geometric_association import (
    bbox_center, bbox_area, dist_pt_to_segment,
)
import step5b2_hierarchy as _s5b2

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Task D tuning ─────────────────────────────────────────────────────────────
TRACE_CROP_PAD_PX           = 300   # px padding around pipeline bbox before crop
TRACE_SNAP_PX               = 150   # max gap to bridge via trace continuation
TRACE_ALIGN_TOL_PX          = 80    # perpendicular tolerance for "adjacent in direction"
GEMINI_TRACE_MODEL          = "gemini-3.1-pro-preview"
GEMINI_TRACE_TEMP           = 0.0
GEMINI_TRACE_EST_INPUT_TOK  = 1100
GEMINI_TRACE_EST_OUTPUT_TOK = 350

# ── Task D prompt ─────────────────────────────────────────────────────────────
_TRACE_PROMPT = (
    "This is a section of a P&ID engineering drawing.\n"
    "The thick red lines show one detected pipeline segment or pipeline group.\n"
    "The thin blue lines show adjacent detected segments.\n\n"
    "Starting from the red highlighted pipeline:\n"
    "1. Does this pipeline continue beyond the highlighted region? YES or NO\n"
    "2. If YES: which direction? (LEFT, RIGHT, UP, DOWN, or MULTIPLE)\n"
    "3. Does this pipeline pass THROUGH any valves while remaining the same\n"
    "   physical pipe? List visible valve symbols, or [] if none.\n"
    "4. Where does this pipeline TERMINATE? Equipment tag if visible, or UNKNOWN.\n\n"
    "Answer in JSON only:\n"
    '{"continues": true, "directions": ["LEFT"], '
    '"passes_through_valves": ["FV-208"], '
    '"terminates_at": "K-V-201", '
    '"confidence": "high", "reasoning": "one sentence"}'
)


# ═══════════════════════════════════════════════════════════════════════════════
# 0. Segment loader
# ═══════════════════════════════════════════════════════════════════════════════

def load_segments(path: str):
    """Load raw segments from step5b_pipe_segments.json.
    Returns (lines_list, meta_dict)."""
    with open(path) as f:
        data = json.load(f)
    meta = {
        "drawing_scale": data.get("drawing_scale", 1.0),
        "image_w": data.get("image_w", 10000),
        "image_h": data.get("image_h", 7000),
    }
    return data.get("segments", []), meta


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Candidate → pipeline spatial link (Task D target selection)
# ═══════════════════════════════════════════════════════════════════════════════

def link_candidates_to_pipelines(pipelines, segments, candidates, radius):
    """Return {pipeline_id: [candidate_id, ...]} — candidates (instruments/valves)
    whose symbol_bbox center is within ``radius`` px of any segment in a pipeline."""
    seg_by_pipe = defaultdict(list)
    for s in segments:
        if s.get("pipeline_id"):
            seg_by_pipe[s["pipeline_id"]].append(s)

    result = defaultdict(list)
    for c in candidates:
        bb = c.get("symbol_bbox") or {}
        if not bb:
            continue
        if c.get("symbol_category", "") not in ("instrument", "valve"):
            continue
        cx, cy = bbox_center(bb)
        for pid, segs in seg_by_pipe.items():
            for s in segs:
                d = dist_pt_to_segment(cx, cy,
                                       s["x0"], s["y0"], s["x1"], s["y1"])
                if d <= radius:
                    result[pid].append(c["candidate_id"])
                    break
    return dict(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Task A + C — gap bridge + tee/crossing (thin wrapper over step5b2 helpers)
# ═══════════════════════════════════════════════════════════════════════════════

def run_task_a_c(pipelines, junctions, segments, img, api_key, out_dir,
                 equip_bboxes, limit_gaps, n_workers, confirm,
                 skip_task_a=False, skip_task_c=False) -> dict:
    """Gap bridge (A) + tee-vs-crossing (C) using step5b2 helpers.
    Modifies pipelines/junctions/segments IN PLACE. Returns audit report."""
    seg_by_id = {s["segment_id"]: s for s in segments}
    gap_items, _elbow_items, junc_items, _ = _s5b2._pv_build_work(
        pipelines, junctions, segments)

    n_gap_total = len(gap_items)
    for g in gap_items:
        sa = seg_by_id.get(g["segA"]); sb = seg_by_id.get(g["segB"])
        same_orient = bool(sa and sb and sa["type"] == sb["type"])
        eq_between = _s5b2._pv_equip_between(g["ptA"], g["ptB"], equip_bboxes)
        g["same_orientation"] = same_orient
        g["equip_between"] = eq_between
        g["score"] = round(_s5b2.gap_bridge_score(
            sa, sb, g["dist"], same_orient, eq_between), 4)
    gap_items.sort(key=lambda g: g["score"], reverse=True)
    if limit_gaps and limit_gaps > 0:
        gap_items = gap_items[:limit_gaps]

    work_a = [] if skip_task_a else gap_items
    work_c = [] if skip_task_c else junc_items
    all_items = [(i, it) for i, it in enumerate(work_a + work_c)]

    report = {
        "task_a": {"candidates": len(work_a), "total": n_gap_total,
                   "bridged": 0, "kept_separate": 0},
        "task_c": {"candidates": len(work_c), "tee": 0, "crossing": 0},
        "gap_bridges": [],
        "tee_crossings": [],
    }
    if not confirm or not all_items:
        return report

    client, sdk = _s5b2._build_gemini_client(api_key)
    Hh, Ww = img.shape[:2]
    PAD = _s5b2.GEMINI_PIPE_VERIFY_PAD_PX
    MS  = _s5b2.GEMINI_PIPE_VERIFY_MAX_SIDE
    crop_dir = Path(out_dir) / "step5b3_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    cache_path = Path(out_dir) / "step5b3_ac_cache.json"
    cache = {}
    if cache_path.exists():
        try:
            cache = json.load(open(cache_path))
        except Exception:
            pass
    cache_lock = threading.Lock()

    def _cache_key(item):
        if item["task"] == "gap":
            sig = ("gap", round(item["ptA"][0]), round(item["ptA"][1]),
                   round(item["ptB"][0]), round(item["ptB"][1]))
        else:
            sig = ("jn", item["point"]["x"], item["point"]["y"], item["degree"])
        return hashlib.md5(json.dumps(sig).encode()).hexdigest()

    def _crop_window(item):
        if item["task"] == "gap":
            xs = [item["ptA"][0], item["ptB"][0]]
            ys = [item["ptA"][1], item["ptB"][1]]
        else:
            xs = [item["point"]["x"]]; ys = [item["point"]["y"]]
        x1 = max(0, int(min(xs)) - PAD); y1 = max(0, int(min(ys)) - PAD)
        x2 = min(Ww, int(max(xs)) + PAD); y2 = min(Hh, int(max(ys)) + PAD)
        return x1, y1, x2, y2

    def _call_item(idx_item):
        idx, item = idx_item
        time.sleep(idx * 0.05)
        key = _cache_key(item)
        with cache_lock:
            if key in cache:
                return idx, item, cache[key], True
        x1, y1, x2, y2 = _crop_window(item)
        crop = img[y1:y2, x1:x2].copy()
        ch, cw = crop.shape[:2]
        if ch == 0 or cw == 0:
            return idx, item, None, False
        sc = MS / max(ch, cw) if max(ch, cw) > MS else 1.0

        def _tx(px): return int((px - x1) * sc)
        def _ty(py): return int((py - y1) * sc)

        if item["task"] == "gap":
            for sid in (item["segA"], item["segB"]):
                s = seg_by_id.get(sid)
                if s:
                    cv2.line(crop, (_tx(s["x0"]), _ty(s["y0"])),
                             (_tx(s["x1"]), _ty(s["y1"])), (0, 0, 255), 4)
            prompt = _s5b2._PV_GAP_PROMPT.format(distance=int(item["dist"]))
        else:
            cv2.circle(crop,
                       (_tx(item["point"]["x"]), _ty(item["point"]["y"])),
                       22, (0, 0, 255), 4)
            prompt = _s5b2._PV_TEE_PROMPT

        if sc != 1.0:
            crop = cv2.resize(crop, (int(cw * sc), int(ch * sc)))
        ok, buf = cv2.imencode(".jpg", crop)
        if not ok:
            return idx, item, None, False
        cv2.imwrite(str(crop_dir / f"ac_{item['task']}_{idx}.jpg"), crop)
        ans = _s5b2._pv_gemini_json(client, sdk, buf.tobytes(), prompt,
                                    f"{item['task']}#{idx}")
        if ans is not None:
            with cache_lock:
                cache[key] = ans
        return idx, item, ans, False

    eff = max(1, min(n_workers, len(all_items)))
    results = [None] * len(all_items)
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=eff) as ex:
        futs = {ex.submit(_call_item, ii): ii[0] for ii in all_items}
        for fut in concurrent.futures.as_completed(futs):
            idx, item, ans, cached = fut.result()
            done += 1
            log.info("task-a/c %d/%d [%s] %s", done, len(all_items),
                     "CACHE" if cached else "LIVE", item["task"])
            results[idx] = (item, ans)
    try:
        json.dump(cache, open(cache_path, "w"), indent=2)
    except Exception:
        pass

    live_ids = {p["pipeline_id"] for p in pipelines}
    for entry in results:
        if entry is None:
            continue
        item, ans = entry
        if ans is None:
            continue
        if item["task"] == "gap":
            rec = {"pidA": item["pidA"], "pidB": item["pidB"],
                   "dist": item["dist"], "score": item.get("score"),
                   "reason": ans.get("reason", ""),
                   "connected": ans.get("connected", False)}
            report["gap_bridges"].append(rec)
            if ans.get("connected") is True and _s5b2._pv_conf_ok(ans):
                a, b = item["pidA"], item["pidB"]
                if a not in live_ids or b not in live_ids:
                    report["task_a"]["kept_separate"] += 1
                    continue
                pa = next((p for p in pipelines if p["pipeline_id"] == a), None)
                pb = next((p for p in pipelines if p["pipeline_id"] == b), None)
                keep = a if (
                    (pa or {}).get("segment_count", 0) >=
                    (pb or {}).get("segment_count", 0)
                ) else b
                drop = b if keep == a else a
                if _s5b2._pv_merge_pipelines(
                        pipelines, junctions, seg_by_id, keep, drop):
                    live_ids.discard(drop)
                    report["task_a"]["bridged"] += 1
                else:
                    report["task_a"]["kept_separate"] += 1
            else:
                report["task_a"]["kept_separate"] += 1
        else:
            jt = ans.get("junction_type", "").lower()
            report["tee_crossings"].append({
                "junction_id": item["junction_id"],
                "degree": item["degree"],
                "verdict": jt or "unknown",
                "reason": ans.get("reason", ""),
            })
            if jt == "crossing" and _s5b2._pv_conf_ok(ans):
                report["task_c"]["crossing"] += 1
            else:
                report["task_c"]["tee"] += 1

    crossing_ids = {r["junction_id"] for r in report["tee_crossings"]
                    if r["verdict"] == "crossing"}
    if crossing_ids:
        junctions[:] = [j for j in junctions
                        if j["junction_id"] not in crossing_ids]
        for p in pipelines:
            if p.get("intermediate_nodes"):
                p["intermediate_nodes"] = [
                    n for n in p["intermediate_nodes"]
                    if n not in crossing_ids]
    return report


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Task D — Pipeline tracing (new)
# ═══════════════════════════════════════════════════════════════════════════════

def _find_adjacent_pipelines(pipeline, segments, direction, snap_px, align_tol):
    """Find pipeline IDs adjacent to this pipeline in the given cardinal direction.
    Returns list of pipeline_ids within snap_px whose endpoint aligns within align_tol."""
    bb = pipeline["bbox"]
    pid = pipeline["pipeline_id"]
    found = {}  # pid -> segment

    for seg in segments:
        if "pipe" not in seg.get("type", ""):
            continue
        seg_pid = seg.get("pipeline_id")
        if not seg_pid or seg_pid == pid:
            continue
        for ex, ey in [(seg["x0"], seg["y0"]), (seg["x1"], seg["y1"])]:
            match = False
            if direction == "LEFT":
                match = (bb["x1"] - snap_px <= ex <= bb["x1"] and
                         bb["y1"] - align_tol <= ey <= bb["y2"] + align_tol)
            elif direction == "RIGHT":
                match = (bb["x2"] <= ex <= bb["x2"] + snap_px and
                         bb["y1"] - align_tol <= ey <= bb["y2"] + align_tol)
            elif direction == "UP":
                match = (bb["y1"] - snap_px <= ey <= bb["y1"] and
                         bb["x1"] - align_tol <= ex <= bb["x2"] + align_tol)
            elif direction == "DOWN":
                match = (bb["y2"] <= ey <= bb["y2"] + snap_px and
                         bb["x1"] - align_tol <= ex <= bb["x2"] + align_tol)
            if match:
                found[seg_pid] = seg
                break
    return list(found.items())


def _gemini_trace_json(client, sdk, img_bytes, label):
    """One Gemini vision call for pipeline tracing → parsed JSON (None on failure)."""
    for attempt in range(3):
        try:
            if sdk == "new":
                from google.genai import types as gt
                resp = client.models.generate_content(
                    model=GEMINI_TRACE_MODEL,
                    contents=[
                        gt.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                        gt.Part.from_text(text=_TRACE_PROMPT),
                    ],
                    config=gt.GenerateContentConfig(temperature=GEMINI_TRACE_TEMP),
                )
                raw = resp.text.strip()
            else:
                import google.generativeai as gl
                import PIL.Image as PILImage
                import io
                pil = PILImage.open(io.BytesIO(img_bytes))
                cfg = gl.GenerationConfig(temperature=GEMINI_TRACE_TEMP)
                resp = gl.GenerativeModel(GEMINI_TRACE_MODEL).generate_content(
                    [_TRACE_PROMPT, pil], generation_config=cfg)
                raw = resp.text.strip()
            clean = raw.replace("```json", "").replace("```", "").strip()
            m = re.search(r"\{.*\}", clean, re.DOTALL)
            return json.loads(m.group(0) if m else clean)
        except Exception as e:
            err = str(e)
            if any(x in err for x in ("429", "503", "RESOURCE_EXHAUSTED", "overloaded")):
                wait = 2 ** attempt
                log.warning("trace %s rate-limited, retry in %ds", label, wait)
                time.sleep(wait)
            else:
                log.warning("trace %s failed: %s", label, err)
                return None
    return None


def run_task_d(pipelines, junctions, segments, cand_links, all_candidates,
               img, api_key, out_dir, n_workers, limit, confirm,
               snap_px=TRACE_SNAP_PX, align_tol=TRACE_ALIGN_TOL_PX) -> dict:
    """Trace each pipeline with connected candidates via Gemini.
    cand_links = {pipeline_id: [candidate_id, ...]} (from link_candidates_to_pipelines).
    Modifies pipelines/segments IN PLACE. Returns trace audit report."""
    Hh, Ww = img.shape[:2]

    seg_by_pipe = defaultdict(list)
    for s in segments:
        if s.get("pipeline_id"):
            seg_by_pipe[s["pipeline_id"]].append(s)

    # Select targets
    if cand_links:
        targets = [p for p in pipelines if p["pipeline_id"] in cand_links]
    else:
        targets = [p for p in pipelines if p.get("segment_count", 0) > 1]
    if limit and limit > 0:
        targets = targets[:limit]

    report = {
        "targets_total": len(targets),
        "targets_sent": len(targets),
        "merged": 0,
        "pipeline_traces": [],
    }
    if not targets or not confirm:
        return report

    client, sdk = _s5b2._build_gemini_client(api_key)
    crop_dir = Path(out_dir) / "step5b3_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    cache_path = Path(out_dir) / "step5b3_d_cache.json"
    cache = {}
    if cache_path.exists():
        try:
            cache = json.load(open(cache_path))
        except Exception:
            pass
    cache_lock = threading.Lock()
    cand_by_id = {c["candidate_id"]: c for c in (all_candidates or [])}

    def _cache_key(pl):
        bb = pl["bbox"]
        sig = (pl["pipeline_id"], bb["x1"], bb["y1"], bb["x2"], bb["y2"])
        return hashlib.md5(json.dumps(sig).encode()).hexdigest()

    def _call_trace(idx_pl):
        idx, pl = idx_pl
        time.sleep(idx * 0.05)
        pid = pl["pipeline_id"]
        key = _cache_key(pl)
        with cache_lock:
            if key in cache:
                return idx, pl, cache[key], True

        bb = pl["bbox"]
        x1c = max(0, bb["x1"] - TRACE_CROP_PAD_PX)
        y1c = max(0, bb["y1"] - TRACE_CROP_PAD_PX)
        x2c = min(Ww, bb["x2"] + TRACE_CROP_PAD_PX)
        y2c = min(Hh, bb["y2"] + TRACE_CROP_PAD_PX)
        crop = img[y1c:y2c, x1c:x2c].copy()
        ch, cw = crop.shape[:2]
        if ch == 0 or cw == 0:
            return idx, pl, None, False

        sc = 1024 / max(ch, cw) if max(ch, cw) > 1024 else 1.0

        def _tx(px): return max(0, min(int((px - x1c) * sc), int(cw * sc) - 1))
        def _ty(py): return max(0, min(int((py - y1c) * sc), int(ch * sc) - 1))

        # Adjacent pipeline segments — thin blue background
        for s in segments:
            if "pipe" not in s.get("type", ""):
                continue
            spid = s.get("pipeline_id")
            if not spid or spid == pid:
                continue
            if not (x1c <= s["x0"] <= x2c and y1c <= s["y0"] <= y2c):
                continue
            cv2.line(crop, (_tx(s["x0"]), _ty(s["y0"])),
                     (_tx(s["x1"]), _ty(s["y1"])), (255, 100, 0), 2)

        # This pipeline — thick red foreground + segment numbers
        for seg_num, s in enumerate(seg_by_pipe.get(pid, [])):
            cv2.line(crop, (_tx(s["x0"]), _ty(s["y0"])),
                     (_tx(s["x1"]), _ty(s["y1"])), (0, 0, 255), 5)
            mx = (_tx(s["x0"]) + _tx(s["x1"])) // 2
            my = (_ty(s["y0"]) + _ty(s["y1"])) // 2
            cv2.putText(crop, str(seg_num), (mx, my),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 180), 1,
                        cv2.LINE_AA)

        # Connected candidates — gray boxes
        for cid in (cand_links.get(pid) or []):
            c = cand_by_id.get(cid)
            if not c:
                continue
            cb = c.get("symbol_bbox") or {}
            if cb:
                cv2.rectangle(crop,
                              (_tx(cb["x1"]), _ty(cb["y1"])),
                              (_tx(cb["x2"]), _ty(cb["y2"])),
                              (128, 128, 128), 1)

        if sc != 1.0:
            crop = cv2.resize(crop, (int(cw * sc), int(ch * sc)))
        ok, buf = cv2.imencode(".jpg", crop)
        if not ok:
            return idx, pl, None, False

        crop_file = str(crop_dir / f"trace_{pid.replace('-', '')}.jpg")
        cv2.imwrite(crop_file, crop)

        ans = _gemini_trace_json(client, sdk, buf.tobytes(), f"trace#{idx}")
        if ans is not None:
            ans["_crop_file"] = crop_file
            with cache_lock:
                cache[key] = ans
        return idx, pl, ans, False

    eff = max(1, min(n_workers, len(targets)))
    results = [None] * len(targets)
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=eff) as ex:
        futs = {ex.submit(_call_trace, (i, pl)): i
                for i, pl in enumerate(targets)}
        for fut in concurrent.futures.as_completed(futs):
            idx, pl, ans, cached = fut.result()
            done += 1
            log.info("trace %d/%d [%s] %s", done, len(targets),
                     "CACHE" if cached else "LIVE", pl["pipeline_id"])
            results[idx] = (pl, ans)
    try:
        json.dump(cache, open(cache_path, "w"), indent=2)
    except Exception:
        pass

    seg_by_id = {s["segment_id"]: s for s in segments}
    live_ids = {p["pipeline_id"] for p in pipelines}

    for entry in results:
        if entry is None:
            continue
        pl, ans = entry
        if ans is None:
            continue
        pid = pl["pipeline_id"]
        conf_ok = (ans.get("confidence", "").lower() in ("high", "medium"))
        action = "no_action"
        merged_with = []

        if ans.get("continues") and conf_ok:
            for direction in (ans.get("directions") or []):
                direction = (direction or "").upper()
                adj = _find_adjacent_pipelines(pl, segments, direction,
                                               snap_px, align_tol)
                for adj_pid, _adj_seg in adj:
                    if adj_pid not in live_ids or adj_pid == pid:
                        continue
                    pa = next((p for p in pipelines
                               if p["pipeline_id"] == pid), None)
                    pb = next((p for p in pipelines
                               if p["pipeline_id"] == adj_pid), None)
                    keep = pid if (
                        (pa or {}).get("segment_count", 0) >=
                        (pb or {}).get("segment_count", 0)
                    ) else adj_pid
                    drop = adj_pid if keep == pid else pid
                    if _s5b2._pv_merge_pipelines(
                            pipelines, junctions, seg_by_id, keep, drop):
                        live_ids.discard(drop)
                        merged_with.append(adj_pid)
                        report["merged"] += 1
                        action = f"merged {drop} into {keep} via {direction}"
                        if drop == pid:
                            pl = next((p for p in pipelines
                                       if p["pipeline_id"] == keep), pl)
                            pid = keep

        # Mark valves as in_pipeline
        for vtag in (ans.get("passes_through_valves") or []):
            vtag_u = (vtag or "").strip().upper()
            for c in (all_candidates or []):
                if (c.get("tag_text") or "").strip().upper() == vtag_u:
                    c["in_pipeline"] = True
                    c["in_pipeline_id"] = pid

        report["pipeline_traces"].append({
            "pipeline_id": pid,
            "seed_segments": [s["segment_id"]
                               for s in seg_by_pipe.get(
                                   pl["pipeline_id"], [])],
            "decision": {k: v for k, v in ans.items()
                         if not k.startswith("_")},
            "action": action,
            "merged_with": merged_with,
            "crop_file": ans.get("_crop_file", ""),
        })

    return report


# ═══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

def run(segments_path, img_path, api_key, out_dir,
        workers=8, limit_gaps=20, limit_traces=20,
        dry_run=False,
        skip_task_a=False, skip_task_c=False, skip_task_d=False,
        associations_path=None):

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ── Load image (needed for both segment fallback and drawing scale) ───────
    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {img_path}")
    Hh, Ww = img.shape[:2]
    log.info("Image: %dx%d", Ww, Hh)

    # ── Load raw segments (or auto-generate if step5b hasn't been re-run) ────
    if Path(segments_path).exists():
        raw_lines, meta = load_segments(segments_path)
        drawing_scale = meta["drawing_scale"]
        log.info("Loaded %d raw segments (drawing_scale=%.3f)",
                 len(raw_lines), drawing_scale)
    else:
        log.warning("step5b_pipe_segments.json not found at %s", segments_path)
        log.warning("Auto-generating segments via CV detection on image "
                    "(re-run step5b to persist this file for future runs)")
        from step5b_geometric_association import detect_pipes_and_lines as _cv_detect
        REFERENCE_WIDTH = 10000
        drawing_scale = Ww / REFERENCE_WIDTH
        _cv_lines = _cv_detect(img, drawing_scale)
        raw_lines = []
        for i, l in enumerate(_cv_lines):
            raw_lines.append({
                "segment_id": f"SEG-{i}",
                "x0": l["x0"], "y0": l["y0"],
                "x1": l["x1"], "y1": l["y1"],
                "cx": l.get("cx", (l["x0"] + l["x1"]) // 2),
                "cy": l.get("cy", (l["y0"] + l["y1"]) // 2),
                "length": l["length"],
                "angle": l["angle"],
                "type": l["type"],
                "pipeline_id": None,
            })
        meta = {"drawing_scale": drawing_scale,
                "image_w": Ww, "image_h": Hh}
        log.info("Auto-generated %d segments (drawing_scale=%.3f)",
                 len(raw_lines), drawing_scale)

    # ── Scale step5b2 thresholds for this drawing ─────────────────────────────
    _s5b2.SNAP_TOL_PX        = max(int(25  * drawing_scale), 5)
    _s5b2.MIN_PIPE_LEN       = max(int(60  * drawing_scale), 10)
    _s5b2.GAP_BRIDGE_PX      = max(int(100 * drawing_scale), 15)
    _s5b2.EQUIP_PIPE_RADIUS  = max(int(90  * drawing_scale), 15)
    _s5b2.SYMBOL_PIPE_RADIUS = max(int(60  * drawing_scale), 10)
    symbol_pipe_radius = _s5b2.SYMBOL_PIPE_RADIUS
    snap_px = max(int(TRACE_SNAP_PX * drawing_scale), 20)
    align_tol = max(int(TRACE_ALIGN_TOL_PX * drawing_scale), 20)

    # ── Build pipeline graph ──────────────────────────────────────────────────
    log.info("=== Building pipeline graph from raw segments ===")
    normalized_segs = _s5b2.build_line_segments(raw_lines)

    candidates, equip_bboxes = [], []
    if associations_path and Path(associations_path).exists():
        with open(associations_path) as f:
            assoc_data = json.load(f)
        candidates = assoc_data.get("enriched_candidates", [])
        equip_bboxes = [
            c.get("symbol_bbox", {}) for c in candidates
            if c.get("symbol_category") == "equipment" and c.get("symbol_bbox")
        ]
        log.info("Loaded %d candidates from associations", len(candidates))

    pipelines, junctions = _s5b2.build_pipelines_and_junctions(
        normalized_segs, equip_bboxes)
    log.info("Pipeline graph: %d pipelines, %d junctions, %d segments",
             len(pipelines), len(junctions), len(normalized_segs))

    cand_links = {}
    if candidates:
        cand_links = link_candidates_to_pipelines(
            pipelines, normalized_segs, candidates, symbol_pipe_radius)
        log.info("Task D links: %d pipelines have connected candidates",
                 len(cand_links))

    # ── Pre-flight cost gate ──────────────────────────────────────────────────
    gap_items_pf, _, junc_items_pf, _ = _s5b2._pv_build_work(
        pipelines, junctions, normalized_segs)

    n_gap_pf = (min(len(gap_items_pf), limit_gaps)
                if limit_gaps and limit_gaps > 0 else len(gap_items_pf))
    n_junc_pf = len(junc_items_pf)

    if cand_links:
        trace_pool = [p for p in pipelines if p["pipeline_id"] in cand_links]
    else:
        trace_pool = [p for p in pipelines if p.get("segment_count", 0) > 1]
    n_trace_pf = (min(len(trace_pool), limit_traces)
                  if limit_traces and limit_traces > 0 else len(trace_pool))

    n_a  = 0 if skip_task_a else n_gap_pf
    n_c  = 0 if skip_task_c else n_junc_pf
    n_d  = 0 if skip_task_d else n_trace_pf
    n_total = n_a + n_c + n_d

    est_in  = ((n_a + n_c) * _s5b2.GEMINI_PIPE_VERIFY_EST_INPUT_TOK
               + n_d * GEMINI_TRACE_EST_INPUT_TOK)
    est_out = ((n_a + n_c) * _s5b2.GEMINI_PIPE_VERIFY_EST_OUTPUT_TOK
               + n_d * GEMINI_TRACE_EST_OUTPUT_TOK)
    est_usd = (est_in  / 1e6 * _s5b2.GEMINI_PRO_USD_PER_MTOK_IN +
               est_out / 1e6 * _s5b2.GEMINI_PRO_USD_PER_MTOK_OUT)

    # Count cached entries
    n_cache = 0
    for cp in ("step5b3_ac_cache.json", "step5b3_d_cache.json"):
        cp_path = out / cp
        if cp_path.exists():
            try:
                n_cache += len(json.load(open(cp_path)))
            except Exception:
                pass

    print("\n=== Step 5B3 PRE-FLIGHT ===")
    print(f"  Task A (gap bridges)    : {n_a} candidates"
          + (f" (top {limit_gaps} of {len(gap_items_pf)})"
             if limit_gaps and limit_gaps > 0
             and len(gap_items_pf) > limit_gaps else ""))
    print(f"  Task C (tee/crossing)   : {n_c} junctions")
    print(f"  Task D (pipe traces)    : {n_d} pipelines")
    print(f"  Total Gemini calls      : {n_total}")
    print(f"  Est. cost               : ~${est_usd:.4f}"
          f"  ({_s5b2.GEMINI_PIPE_VERIFY_MODEL})")
    print(f"  Cache hits expected     : ~{n_cache}"
          f"  (from existing cache files)")
    print(f"  Drawing scale           : {drawing_scale:.3f} ({Ww}px wide)")

    if dry_run:
        print("  --> DRY RUN: no API calls. Writing skeleton JSON.")
        skel = {
            "version": "v1",
            "drawing_scale": drawing_scale,
            "dry_run": True,
            "pipelines": [], "junctions": [], "segments": [],
            "pipe_verify_report": {
                "gap_bridges": [],
                "tee_crossings": [],
                "pipeline_traces": [],
            },
        }
        out_path = str(out / "step5b3_verified_graph.json")
        with open(out_path, "w") as f:
            json.dump(skel, f, indent=2)
        print(f"  --> {out_path}")
        return skel

    # ── Run tasks ─────────────────────────────────────────────────────────────
    report_ac = {"task_a": {"bridged": 0, "kept_separate": 0},
                 "task_c": {"tee": 0, "crossing": 0},
                 "gap_bridges": [], "tee_crossings": []}

    if not (skip_task_a and skip_task_c):
        log.info("=== Tasks A + C (gap bridge + tee/crossing) ===")
        report_ac = run_task_a_c(
            pipelines, junctions, normalized_segs, img, api_key, out_dir,
            equip_bboxes, limit_gaps, workers, confirm=True,
            skip_task_a=skip_task_a, skip_task_c=skip_task_c)
        log.info("Task A: %d bridged, %d kept separate",
                 report_ac["task_a"].get("bridged", 0),
                 report_ac["task_a"].get("kept_separate", 0))
        log.info("Task C: %d tee, %d crossing removed",
                 report_ac["task_c"].get("tee", 0),
                 report_ac["task_c"].get("crossing", 0))

    report_d = {"merged": 0, "pipeline_traces": []}
    if not skip_task_d:
        log.info("=== Task D (pipeline tracing) ===")
        report_d = run_task_d(
            pipelines, junctions, normalized_segs, cand_links, candidates,
            img, api_key, out_dir, workers, limit_traces, confirm=True,
            snap_px=snap_px, align_tol=align_tol)
        log.info("Task D: %d traces, %d pipeline merges",
                 len(report_d["pipeline_traces"]), report_d["merged"])

    # ── Write verified graph ──────────────────────────────────────────────────
    out_path = str(out / "step5b3_verified_graph.json")
    result = {
        "version": "v1",
        "drawing_scale": drawing_scale,
        "dry_run": False,
        "pipelines": pipelines,
        "junctions": junctions,
        "segments": normalized_segs,
        "pipe_verify_report": {
            "gap_bridges":      report_ac.get("gap_bridges", []),
            "tee_crossings":    report_ac.get("tee_crossings", []),
            "pipeline_traces":  report_d.get("pipeline_traces", []),
        },
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    log.info("✓ step5b3_verified_graph.json → %s", out_path)
    log.info("  pipelines=%d  junctions=%d  segments=%d",
             len(pipelines), len(junctions), len(normalized_segs))
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="Step 5B3: Gemini Pipeline Connectivity Agent")
    ap.add_argument("--segments",
                    default="output/step5b_pipe_segments.json",
                    help="Step 5B pipe segments JSON (step5b_pipe_segments.json)")
    ap.add_argument("--associations",
                    help="Step 5B associations JSON (optional; provides candidates "
                         "for Task D target selection)")
    ap.add_argument("--image", help="Drawing image path")
    ap.add_argument("--context",
                    help="drawing_context.json (fallback source for --image)")
    ap.add_argument("--api-key",
                    help="Gemini API key (or GEMINI_API_KEY / GEMINI_KEY env)")
    ap.add_argument("--out", default="output", help="Output directory")
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel Gemini workers (default 8)")
    ap.add_argument("--limit-gaps", type=int, default=20,
                    help="Max Task A gap candidates (default 20; -1 = all)")
    ap.add_argument("--limit-traces", type=int, default=20,
                    help="Max Task D pipeline traces (default 20; -1 = all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print cost estimate only; write skeleton JSON; no API calls")
    ap.add_argument("--skip-task-a", action="store_true",
                    help="Skip gap bridging")
    ap.add_argument("--skip-task-c", action="store_true",
                    help="Skip tee-vs-crossing verification")
    ap.add_argument("--skip-task-d", action="store_true",
                    help="Skip pipeline tracing")
    args = ap.parse_args()

    img_path = args.image
    if not img_path and args.context:
        with open(args.context) as f:
            ctx = json.load(f)
        img_path = ctx.get("raster_path") or ctx.get("input_file")
    if not img_path:
        img_path = "input_drawing.jpg"

    api_key = (args.api_key
               or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GEMINI_KEY")
               or "")
    if not args.dry_run and not api_key:
        raise SystemExit(
            "ERROR: --api-key or GEMINI_API_KEY env required for non-dry-run mode")

    run(
        segments_path=args.segments,
        img_path=img_path,
        api_key=api_key,
        out_dir=args.out,
        workers=args.workers,
        limit_gaps=args.limit_gaps,
        limit_traces=args.limit_traces,
        dry_run=args.dry_run,
        skip_task_a=args.skip_task_a,
        skip_task_c=args.skip_task_c,
        skip_task_d=args.skip_task_d,
        associations_path=args.associations,
    )


if __name__ == "__main__":
    main()
