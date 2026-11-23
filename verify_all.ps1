Write-Host "=== CDCI Pipeline Visual Verification ==="
Write-Host ""

# Step 1
Write-Host "[1/5] Cloud detection overlay"
if (Test-Path "output/overlay_v2.jpg") {
    Write-Host "  ✓ output/overlay_v2.jpg exists"
} else {
    Write-Host "  ✗ Missing — run step2b first"
}

# Step 2
Write-Host "[2/5] Pipe detection overlay"
$errorActionPreference = "SilentlyContinue"
python stages/step5b_geometric_association.py --image input_drawing.jpg --out output/ --debug-only
if (-not $?) {
    python -c "
import json, cv2, numpy as np
img = cv2.imread('input_drawing.jpg')
h, w = img.shape[:2]
with open('output/step5b_associations_full.json') as f:
    d = json.load(f)
print('  Pipe debug: use existing output/step5b_pipe_debug.jpg')
"
}
$errorActionPreference = "Continue"

if (Test-Path "output/step5b_pipe_debug.jpg") {
    Write-Host "  ✓ output/step5b_pipe_debug.jpg"
} else {
    Write-Host "  ✗ Missing — run step5b with pipe debug"
}

# Step 3
Write-Host "[3/5] Hierarchy debug overlay"
if (Test-Path "output/step5b2_debug_overlay.jpg") {
    Write-Host "  ✓ output/step5b2_debug_overlay.jpg"
} else {
    Write-Host "  ✗ Missing — run step5b2 with --debug-annotate"
}

# Step 4
Write-Host "[4/5] Hierarchy verification overlay"
python stages/visualize_hierarchy.py --hierarchy output/step5b2_hierarchy_full.json --image input_drawing.jpg --out output/hierarchy_verification.jpg
Write-Host "  ✓ output/hierarchy_verification.jpg"

# Step 5
Write-Host ""
Write-Host "[5/5] Metrics summary"
python -c "
import json
with open('output/step5b2_hierarchy_full.json') as f:
    h = json.load(f)
hier = h.get('hierarchy', [])
nodes = h.get('graph',{}).get('nodes',[])
edges = h.get('graph',{}).get('edges',[])
segs = h.get('line_segments', [])
pipes = h.get('pipelines', [])

total = len(hier)
equip_parent = sum(1 for r in hier if r.get('equipment_parent'))
inst_valve = [r for r in hier if r.get('kind') in ('instrument','valve')]
iv_parent = sum(1 for r in inst_valve if r.get('equipment_parent'))
isolated = sum(1 for r in hier if r.get('is_isolated'))
no_parent = sum(1 for r in inst_valve if not r.get('equipment_parent') and not r.get('is_isolated'))
equip = [n for n in nodes if n.get('kind')=='equipment']
pipe_segs = sum(1 for s in segs if s.get('type') in ('horizontal_pipe','vertical_pipe') and (s.get('length',0))>150)

ev_counts = {'gemini':0, 'pipeline':0, 'mounted':0, 'other':0}
for r in hier:
    ev = (r.get('equipment_parent_evidence') or '').lower()
    if not r.get('equipment_parent'): continue
    if 'gemini' in ev: ev_counts['gemini'] += 1
    elif 'pipeline' in ev: ev_counts['pipeline'] += 1
    elif 'mounted' in ev or 'from equipment' in ev: ev_counts['mounted'] += 1
    else: ev_counts['other'] += 1

gemini_edges = sum(1 for e in edges if e.get('rel')=='GEMINI_ATTACHED')
mounted_edges = sum(1 for e in edges if e.get('rel')=='MOUNTED_ON')

print('━' * 55)
print('  CDCI HIERARCHY — CURRENT STATE')
print('━' * 55)
print(f'  Hierarchy nodes      : {total}')
print(f'  Equipment nodes      : {len(equip)}')
for e in equip:
    tag = e.get('tag_text','')
    rec = next((r for r in hier if r.get('node_id')==e['node_id']),None)
    ch = len(rec.get('children',[])) if rec else 0
    lo = rec.get('is_label_only') if rec else None
    lo_str = ' (label_only)' if lo else ''
    print(f'    {tag:20s} {ch:3d} children{lo_str}')
print(f'  Equipment parent     : {equip_parent}/{total} ({100*equip_parent/total:.0f}%)')
print(f'    inst/valve only    : {iv_parent}/{len(inst_valve)} ({100*iv_parent/len(inst_valve):.0f}%)')
print(f'    by evidence:')
print(f'      gemini_vision    : {ev_counts[\"gemini\"]}')
print(f'      pipeline_traversal: {ev_counts[\"pipeline\"]}')
print(f'      mounted_on       : {ev_counts[\"mounted\"]}')
print(f'  Isolated             : {isolated}')
print(f'  NO PARENT (not iso)  : {no_parent}')
print(f'  Pipe segments (>150) : {pipe_segs}')
print(f'  Pipeline entities    : {len(pipes)}')
print(f'  Graph edges          : {len(edges)}')
print(f'    MOUNTED_ON         : {mounted_edges}')
print(f'    GEMINI_ATTACHED    : {gemini_edges}')
print('━' * 55)
print()
print('  Visual outputs:')
print('    output/overlay_v2.jpg              — cloud detection')
print('    output/step5b_pipe_debug.jpg       — pipe detection')
print('    output/step5b2_debug_overlay.jpg   — hierarchy debug')
print('    output/hierarchy_verification.jpg  — FULL verification')
print('    output/hierarchy_viewer.html       — interactive tree')
print('    output/hierarchy_graph.html        — force-directed graph')
print('━' * 55)
"

Write-Host ""
Write-Host "=== Done. Open these files to verify: ==="
Write-Host "  1. output/hierarchy_verification.jpg  <- main verification image"
Write-Host "  2. output/step5b_pipe_debug.jpg       <- pipe detection only"
Write-Host "  3. output/step5b2_debug_overlay.jpg   <- symbols + pipes"
Write-Host "  4. output/hierarchy_viewer.html       <- interactive tree browser"