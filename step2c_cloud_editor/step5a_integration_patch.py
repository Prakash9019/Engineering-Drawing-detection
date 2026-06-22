# ════════════════════════════════════════════════════════════════════
# STEP5A INTEGRATION PATCH
# ════════════════════════════════════════════════════════════════════
#
# Apply this change to step5a_candidate_extraction.py so it prefers the
# human-approved clouds over the raw auto-detected ones.
#
# WHERE: In the main pipeline, find the section where the cloud JSON and
# mask are loaded. It currently looks something like:
#
#     clouds_path = os.path.join(out_dir, "outer_clouds_v2.json")
#     mask_path   = os.path.join(out_dir, "cloud_mask_v2.png")
#     cloud_regions = load_cloud_regions_from_step2b(clouds_path, img_w, img_h)
#     ...
#     mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
#     set_cloud_mask(mask)
#
# REPLACE the path-resolution lines with the block below.
# ════════════════════════════════════════════════════════════════════


def resolve_cloud_inputs(out_dir):
    """
    Prefer human-approved clouds (step2c) over raw auto-detection (step2b).

    Returns (clouds_json_path, mask_png_path, source_label).
    Drop this helper near the top of step5a, or inline the logic.
    """
    import os

    approved_json = os.path.join(out_dir, "approved_clouds.json")
    approved_mask = os.path.join(out_dir, "cloud_mask_approved.png")
    raw_json      = os.path.join(out_dir, "outer_clouds_v2.json")
    raw_mask      = os.path.join(out_dir, "cloud_mask_v2.png")

    if os.path.exists(approved_json):
        clouds_path = approved_json
        mask_path   = approved_mask if os.path.exists(approved_mask) else raw_mask
        return clouds_path, mask_path, "approved (human-verified, step2c)"

    return raw_json, raw_mask, "auto-detected (step2b, no human review)"


# ── In the main pipeline, replace the path lines with: ──────────────
#
#     clouds_path, mask_path, src_label = resolve_cloud_inputs(out_dir)
#     log.info("Cloud source: %s", src_label)
#     log.info("  clouds: %s", clouds_path)
#     log.info("  mask:   %s", mask_path)
#
#     cloud_regions = load_cloud_regions_from_step2b(clouds_path, img_w, img_h)
#     mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
#     set_cloud_mask(mask)
#
# ════════════════════════════════════════════════════════════════════
# NOTE FOR DOWNSTREAM STEPS (5B, 5C, tag extraction):
#
# Steps 5B and 5C consume the cloud regions THROUGH step5a's output, so
# they inherit the approved clouds automatically — no change needed.
#
# If any step loads cloud JSON independently (not via step5a's output),
# apply the SAME resolve_cloud_inputs() helper there.
# ════════════════════════════════════════════════════════════════════
