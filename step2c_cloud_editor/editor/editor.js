/* ============================================================================
   Revision Cloud Editor — Frontend Engine (Step 2C)
   ============================================================================
   Coordinate spaces:
     DISPLAY  = canvas world coords (the downscaled image, e.g. 2400px wide)
     IMAGE    = full image resolution (e.g. 8000x5650) — for mask export
     JSON     = detection resolution (e.g. 9934x7017) — for JSON export
   The editor renders & interacts in DISPLAY space; conversion happens on export.
   ========================================================================== */

const SCALLOP_ARC_RADIUS = 7;     // display px — size of each cloud bump
const SCALLOP_POINTS_PER_ARC = 8; // points per arc
const CONTROL_POINT_RADIUS = 4;   // screen px
const CONTROL_POINT_HIT = 11;     // screen px hit radius
const MAX_CONTROL_POINTS = 60;    // max draggable points shown in edit mode
const MIN_RECT_SIZE = 14;         // display px min to count as a draw
const UNDO_LIMIT = 60;

// Soft proportional edge editing ("pull the boundary" brush)
const EDIT_RADIUS_DEFAULT = 90;   // SCREEN px — how far along the boundary a pull reaches
const EDIT_RADIUS_MIN = 18;
const EDIT_RADIUS_MAX = 600;
const EDIT_RADIUS_STEP = 1.2;     // multiplicative step for [ and ]

// Region cleanup (Eraser / Cut) — raster subtract then re-trace the boundary
const ERASE_RADIUS_DEFAULT = 45;  // SCREEN px — eraser brush radius
const ERASE_RADIUS_MIN = 8;
const ERASE_RADIUS_MAX = 400;
const CUT_KERF = 3;               // SCREEN px — width of the cut slice
const MIN_FRAGMENT_FRAC = 0.04;   // drop components < 4% of total area (auto cleanup)
const MIN_FRAGMENT_PX = 64;       // …and always drop blobs below this raw pixel area
const CONTOUR_EPS = 1.6;          // display-px RDP epsilon for re-traced boundaries
const RASTER_MAX = 4_000_000;     // safety cap on raster pixels per op

const STATE = {
  clouds: [],
  imageB64: '', overlayB64: null,
  displayW: 0, displayH: 0, imageW: 0, imageH: 0, jsonW: 0, jsonH: 0,
  scaleJ2I: 1, displayScale: 1,

  zoom: 1, panX: 0, panY: 0,
  showOverlay: false,

  currentTool: 'pan',
  selectedIds: [],
  hoveredId: null,
  isDragging: false,
  spaceHeld: false,
  dragStart: null,

  drawPoints: [],
  drawRect: null,

  editVertexIdx: null,
  hoveredVertexIdx: null,
  hoveredSegmentIdx: null,

  editRadius: EDIT_RADIUS_DEFAULT, // screen px — soft-drag brush size
  softDrag: null,                  // active proportional-drag session
  mouseWorld: [0, 0],              // last cursor pos in world coords (for brush ring)

  eraseRadius: ERASE_RADIUS_DEFAULT, // screen px — eraser brush size
  cutLine: null,                   // active cut line { x1,y1,x2,y2 }
  regionTargetId: null,            // cloud being erased / cut

  extendTargetId: null,

  undoStack: [], redoStack: [],
  nextId: 100,
  addedCount: 0, deletedCount: 0, modifiedCount: 0, mergedCount: 0,
  originalCount: 0,
};

let canvas, ctx, wrap, bgImg, overlayImg = null;

/* ============================================================================
   INIT
   ========================================================================== */
async function init() {
  canvas = document.getElementById('canvas');
  ctx = canvas.getContext('2d');
  wrap = document.getElementById('canvasWrap');

  const resp = await fetch('/api/data');
  const data = await resp.json();

  STATE.imageB64 = data.image_b64;
  STATE.displayW = data.display_w;
  STATE.displayH = data.display_h;
  STATE.imageW = data.image_w;
  STATE.imageH = data.image_h;
  STATE.jsonW = data.json_w;
  STATE.jsonH = data.json_h;
  STATE.scaleJ2I = data.scale_j2i;
  STATE.displayScale = data.display_scale;

  STATE.clouds = data.clouds.map(c => ({
    id: c.id,
    source: c.source,
    confidence: c.confidence,
    scallopedness: c.scallopedness,
    areaJson: c.areaJson,
    pointCount: c.pointCount,
    status: 'approved',
    polygon: c.polygon.map(p => [p[0], p[1]]),
    bbox: c.bbox.slice(),
    polygonImg: c.polygonImg,
    polygonJson: c.polygonJson,
    isUserAdded: false,
    isModified: false,
    isMerged: false,
    visible: true,
  }));
  STATE.originalCount = STATE.clouds.length;

  // Load background image
  bgImg = new Image();
  bgImg.onload = () => { fitToView(); render(); };
  bgImg.src = 'data:image/jpeg;base64,' + STATE.imageB64;

  // Optional overlay
  fetch('/api/overlay').then(r => r.json()).then(o => {
    if (o.overlay_b64) {
      overlayImg = new Image();
      overlayImg.src = 'data:image/jpeg;base64,' + o.overlay_b64;
    }
  });

  resizeCanvas();
  window.addEventListener('resize', () => { resizeCanvas(); render(); });

  canvas.addEventListener('mousedown', onMouseDown);
  canvas.addEventListener('mousemove', onMouseMove);
  canvas.addEventListener('mouseup', onMouseUp);
  canvas.addEventListener('wheel', onWheel, { passive: false });
  canvas.addEventListener('dblclick', onDblClick);
  canvas.addEventListener('contextmenu', e => e.preventDefault());

  document.addEventListener('keydown', onKeyDown);
  document.addEventListener('keyup', onKeyUp);
  window.addEventListener('beforeunload', e => {
    if (STATE.addedCount + STATE.deletedCount + STATE.modifiedCount + STATE.mergedCount > 0) {
      e.preventDefault(); e.returnValue = '';
    }
  });

  setTool('select');
  setStatus(`Loaded ${STATE.clouds.length} auto-detected clouds. Review and correct.`);
  updateUI();
}

function resizeCanvas() {
  const r = wrap.getBoundingClientRect();
  canvas.width = r.width;
  canvas.height = r.height;
}

/* ============================================================================
   COORDINATE TRANSFORMS
   ========================================================================== */
function worldToScreen(wx, wy) { return [wx * STATE.zoom + STATE.panX, wy * STATE.zoom + STATE.panY]; }
function screenToWorld(sx, sy) { return [(sx - STATE.panX) / STATE.zoom, (sy - STATE.panY) / STATE.zoom]; }
function displayToImage(dx, dy) { const s = 1 / STATE.displayScale; return [Math.round(dx * s), Math.round(dy * s)]; }
function displayToJson(dx, dy) { const s = 1 / (STATE.displayScale * STATE.scaleJ2I); return [Math.round(dx * s), Math.round(dy * s)]; }

/* ============================================================================
   RENDERING
   ========================================================================== */
function render() {
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0c0d11';
  ctx.fillRect(0, 0, W, H);

  ctx.save();
  ctx.translate(STATE.panX, STATE.panY);
  ctx.scale(STATE.zoom, STATE.zoom);

  const img = (STATE.showOverlay && overlayImg && overlayImg.complete) ? overlayImg : bgImg;
  if (img && img.complete) ctx.drawImage(img, 0, 0, STATE.displayW, STATE.displayH);

  // Non-selected clouds
  for (const c of STATE.clouds) {
    if (c.status === 'deleted' || !c.visible) continue;
    if (STATE.selectedIds.includes(c.id)) continue;
    drawCloud(c, false);
  }
  // Selected clouds on top
  for (const id of STATE.selectedIds) {
    const c = findCloud(id);
    if (c && c.status !== 'deleted' && c.visible) {
      drawCloud(c, true);
      if (STATE.currentTool === 'edit') drawControlPoints(c);
    }
  }

  ctx.restore();

  drawToolOverlay();
  updateZoomLabel();
}

function cloudColors(c, sel) {
  if (sel) return { s: '#e94560', f: 'rgba(233,69,96,.15)' };
  if (STATE.selectedIds.includes(c.id) && STATE.currentTool === 'merge')
    return { s: '#9b59b6', f: 'rgba(155,89,182,.18)' };
  if (c.isMerged) return { s: '#9b59b6', f: 'rgba(155,89,182,.10)' };
  if (c.isUserAdded) return { s: '#2ecc71', f: 'rgba(46,204,113,.12)' };
  if (c.isModified) return { s: '#f39c12', f: 'rgba(243,156,18,.10)' };
  return { s: '#4fc3f7', f: 'rgba(79,195,247,.06)' };
}

function drawCloud(c, sel) {
  const pts = c.polygon;
  if (pts.length < 3) return;
  const col = cloudColors(c, sel);
  const hovered = c.id === STATE.hoveredId;

  ctx.beginPath();
  catmullRomPath(ctx, pts);
  ctx.closePath();
  ctx.fillStyle = col.f;
  ctx.fill();
  ctx.strokeStyle = col.s;
  ctx.lineWidth = (sel ? 3 : (hovered ? 2.5 : 2)) / STATE.zoom;
  if (hovered && !sel) {
    ctx.shadowColor = col.s;
    ctx.shadowBlur = 10 / STATE.zoom;
  }
  ctx.stroke();
  ctx.shadowBlur = 0;

  // Label
  if (STATE.zoom > 0.25) {
    const cx = (c.bbox[0] + c.bbox[2]) / 2;
    const cy = (c.bbox[1] + c.bbox[3]) / 2;
    const fs = Math.max(9, 13 / STATE.zoom);
    ctx.font = `bold ${fs}px sans-serif`;
    ctx.fillStyle = col.s;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('C' + c.id, cx, cy);
  }
}

// Smooth closed curve through all points (Catmull-Rom → bezier)
function catmullRomPath(ctx, pts) {
  const n = pts.length;
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n];
    const p1 = pts[i];
    const p2 = pts[(i + 1) % n];
    const p3 = pts[(i + 2) % n];
    const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
    const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
    const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
    const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
    ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2[0], p2[1]);
  }
}

function drawControlPoints(c) {
  const pts = c.polygon;
  const step = pts.length > MAX_CONTROL_POINTS ? Math.ceil(pts.length / MAX_CONTROL_POINTS) : 1;
  const r = CONTROL_POINT_RADIUS / STATE.zoom;

  // segment highlight
  if (STATE.hoveredSegmentIdx !== null) {
    const i = STATE.hoveredSegmentIdx;
    const a = pts[i], b = pts[(i + 1) % pts.length];
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
    ctx.strokeStyle = '#ffd24a';
    ctx.lineWidth = 4 / STATE.zoom;
    ctx.stroke();
  }

  for (let i = 0; i < pts.length; i += step) {
    const [x, y] = pts[i];
    const isHover = i === STATE.hoveredVertexIdx;
    const isActive = i === STATE.editVertexIdx;
    ctx.beginPath();
    ctx.arc(x, y, (isHover || isActive ? r * 1.5 : r), 0, Math.PI * 2);
    ctx.fillStyle = isActive ? '#e94560' : (isHover ? '#ffd24a' : '#fff');
    ctx.fill();
    ctx.strokeStyle = '#e94560';
    ctx.lineWidth = 1.5 / STATE.zoom;
    ctx.stroke();
  }
}

function drawToolOverlay() {
  // Rectangle preview
  if (STATE.drawRect) {
    const [a, b] = [worldToScreen(STATE.drawRect.x1, STATE.drawRect.y1),
                    worldToScreen(STATE.drawRect.x2, STATE.drawRect.y2)];
    ctx.strokeStyle = STATE.currentTool === 'extend' ? '#f39c12' : '#2ecc71';
    ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
    ctx.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
    ctx.setLineDash([]);
  }
  // Lasso / polygon path (erase has its own preview below)
  if (STATE.drawPoints.length > 0 && STATE.currentTool !== 'erase') {
    const isRemove = STATE.currentTool === 'remove_poly';
    const dotColor = isRemove ? '#e94560' : '#4fc3f7';
    ctx.strokeStyle = STATE.currentTool === 'extend' ? '#f39c12'
      : (STATE.currentTool === 'add_poly' ? '#4fc3f7'
      : (isRemove ? '#e94560' : '#2ecc71'));
    ctx.lineWidth = 2; ctx.setLineDash([5, 3]);
    ctx.beginPath();
    const f = worldToScreen(STATE.drawPoints[0][0], STATE.drawPoints[0][1]);
    ctx.moveTo(f[0], f[1]);
    for (let i = 1; i < STATE.drawPoints.length; i++) {
      const p = worldToScreen(STATE.drawPoints[i][0], STATE.drawPoints[i][1]);
      ctx.lineTo(p[0], p[1]);
    }
    if (isRemove && STATE.drawPoints.length >= 2) ctx.lineTo(f[0], f[1]); // hint closure
    ctx.stroke();
    ctx.setLineDash([]);
    if (STATE.currentTool === 'add_poly' || isRemove) {
      for (const pt of STATE.drawPoints) {
        const sp = worldToScreen(pt[0], pt[1]);
        ctx.beginPath(); ctx.arc(sp[0], sp[1], 4, 0, Math.PI * 2);
        ctx.fillStyle = dotColor; ctx.fill();
      }
    }
  }
  // Soft-drag brush ring (edit tool): shows how far a pull will reach
  if (STATE.currentTool === 'edit' && STATE.selectedIds.length === 1 && !STATE.spaceHeld) {
    let center = STATE.mouseWorld;
    if (STATE.softDrag) center = STATE.softDrag.orig[STATE.softDrag.anchorIdx];
    const sp = worldToScreen(center[0], center[1]);
    const rigid = STATE.softDrag ? STATE.softDrag.rigid : false;
    ctx.beginPath();
    ctx.arc(sp[0], sp[1], STATE.editRadius, 0, Math.PI * 2);
    ctx.strokeStyle = rigid ? 'rgba(233,69,96,.35)' : 'rgba(255,210,74,.55)';
    ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.arc(sp[0], sp[1], 2.5, 0, Math.PI * 2);
    ctx.fillStyle = rigid ? '#e94560' : '#ffd24a';
    ctx.fill();
  }
  // Eraser: painted trail + brush ring at cursor
  if (STATE.currentTool === 'erase' && !STATE.spaceHeld) {
    if (STATE.drawPoints.length) {
      ctx.beginPath();
      const f = worldToScreen(STATE.drawPoints[0][0], STATE.drawPoints[0][1]);
      ctx.moveTo(f[0], f[1]);
      for (let i = 1; i < STATE.drawPoints.length; i++) {
        const p = worldToScreen(STATE.drawPoints[i][0], STATE.drawPoints[i][1]);
        ctx.lineTo(p[0], p[1]);
      }
      ctx.strokeStyle = 'rgba(233,69,96,.35)';
      ctx.lineWidth = STATE.eraseRadius * 2;
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      ctx.stroke();
    }
    const sp = worldToScreen(STATE.mouseWorld[0], STATE.mouseWorld[1]);
    ctx.beginPath();
    ctx.arc(sp[0], sp[1], STATE.eraseRadius, 0, Math.PI * 2);
    ctx.strokeStyle = '#e94560'; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
    ctx.stroke(); ctx.setLineDash([]);
  }
  // Cut: the slice line
  if (STATE.currentTool === 'cut' && STATE.cutLine) {
    const a = worldToScreen(STATE.cutLine.x1, STATE.cutLine.y1);
    const b = worldToScreen(STATE.cutLine.x2, STATE.cutLine.y2);
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
    ctx.strokeStyle = '#e94560'; ctx.lineWidth = 2; ctx.setLineDash([8, 5]);
    ctx.stroke(); ctx.setLineDash([]);
    for (const p of [a, b]) {
      ctx.beginPath(); ctx.arc(p[0], p[1], 4, 0, Math.PI * 2);
      ctx.fillStyle = '#e94560'; ctx.fill();
    }
  }
}

/* ============================================================================
   ZOOM / PAN
   ========================================================================== */
function fitToView() {
  const r = wrap.getBoundingClientRect();
  STATE.zoom = Math.min(r.width / STATE.displayW, r.height / STATE.displayH) * 0.96;
  STATE.panX = (r.width - STATE.displayW * STATE.zoom) / 2;
  STATE.panY = (r.height - STATE.displayH * STATE.zoom) / 2;
}
function zoomAt(sx, sy, f) {
  const [wx, wy] = screenToWorld(sx, sy);
  STATE.zoom = Math.max(0.1, Math.min(12, STATE.zoom * f));
  STATE.panX = sx - wx * STATE.zoom;
  STATE.panY = sy - wy * STATE.zoom;
  render();
}
function zoomIn() { zoomAt(canvas.width / 2, canvas.height / 2, 1.3); }
function zoomOut() { zoomAt(canvas.width / 2, canvas.height / 2, 1 / 1.3); }
function updateZoomLabel() { document.getElementById('zoomLvl').textContent = Math.round(STATE.zoom * 100) + '%'; }

/* ============================================================================
   MOUSE
   ========================================================================== */
function getMouse(e) {
  const r = canvas.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

function onMouseDown(e) {
  const [sx, sy] = getMouse(e);
  const [wx, wy] = screenToWorld(sx, sy);

  if (STATE.currentTool === 'pan' || STATE.spaceHeld) {
    STATE.isDragging = true;
    STATE.dragStart = [sx - STATE.panX, sy - STATE.panY];
    canvas.style.cursor = 'grabbing';
    return;
  }

  switch (STATE.currentTool) {
    case 'select': {
      const hit = hitTestCloud(wx, wy);
      STATE.selectedIds = hit ? [hit.id] : [];
      updateUI(); render();
      break;
    }
    case 'delete': {
      const hit = hitTestCloud(wx, wy);
      if (hit) deleteCloud(hit.id);
      break;
    }
    case 'merge': {
      const hit = hitTestCloud(wx, wy);
      if (hit) {
        const i = STATE.selectedIds.indexOf(hit.id);
        if (i >= 0) STATE.selectedIds.splice(i, 1);
        else STATE.selectedIds.push(hit.id);
        updateUI(); render();
      }
      break;
    }
    case 'add_rect':
    case 'extend': {
      if (STATE.currentTool === 'extend' && STATE.selectedIds.length !== 1) {
        setStatus('Select exactly one cloud first, then draw to extend it.');
        break;
      }
      STATE.isDragging = true;
      STATE.drawRect = { x1: wx, y1: wy, x2: wx, y2: wy };
      break;
    }
    case 'add_lasso': {
      STATE.isDragging = true;
      STATE.drawPoints = [[wx, wy]];
      break;
    }
    case 'add_poly': {
      STATE.drawPoints.push([wx, wy]);
      render();
      break;
    }
    case 'remove_poly': {           // same interaction as add_poly, but subtracts
      STATE.drawPoints.push([wx, wy]);
      render();
      break;
    }
    case 'edit': {
      if (STATE.selectedIds.length !== 1) break;
      const c = findCloud(STATE.selectedIds[0]);
      if (!c) break;
      // shift+click = delete that point; right-click = delete; alt = rigid single-point move
      const vIdx = hitTestVertex(c, wx, wy);
      if (vIdx !== null) {
        if (e.shiftKey || e.button === 2) { deleteVertex(c, vIdx); render(); break; }
        // grab existing vertex and pull the surrounding boundary with it
        beginSoftDrag(c, vIdx, e.altKey, false);
      } else {
        const seg = hitTestSegment(c, wx, wy);
        if (seg !== null) {
          if (e.shiftKey || e.button === 2) { deleteSegment(c, seg); render(); break; }
          // grab the edge itself: insert an anchor at the click point, then pull
          const a = c.polygon[seg], b = c.polygon[(seg + 1) % c.polygon.length];
          const proj = projectPointToSegment(wx, wy, a, b);
          pushUndo();
          c.polygon.splice(seg + 1, 0, proj);
          beginSoftDrag(c, seg + 1, e.altKey, true);
        }
      }
      render();
      break;
    }
    case 'erase': {
      const target = pickRegionTarget(wx, wy);
      if (!target) { setStatus('Eraser: hover over a cloud to paint away part of it.'); break; }
      STATE.regionTargetId = target.id;
      STATE.selectedIds = [target.id];
      STATE.isDragging = true;
      STATE.drawPoints = [[wx, wy]];
      render();
      break;
    }
    case 'cut': {
      const target = pickRegionTarget(wx, wy);
      if (!target) { setStatus('Cut: start the line on the cloud you want to slice.'); break; }
      STATE.regionTargetId = target.id;
      STATE.selectedIds = [target.id];
      STATE.isDragging = true;
      STATE.cutLine = { x1: wx, y1: wy, x2: wx, y2: wy };
      render();
      break;
    }
  }
}

function onMouseMove(e) {
  const [sx, sy] = getMouse(e);
  const [wx, wy] = screenToWorld(sx, sy);
  STATE.mouseWorld = [wx, wy];

  const [jx, jy] = displayToJson(wx, wy);
  document.getElementById('cursorPos').textContent = `x: ${jx}, y: ${jy}`;

  if (STATE.isDragging && (STATE.currentTool === 'pan' || STATE.spaceHeld)) {
    STATE.panX = sx - STATE.dragStart[0];
    STATE.panY = sy - STATE.dragStart[1];
    render(); return;
  }
  if (STATE.isDragging && (STATE.currentTool === 'add_rect' || STATE.currentTool === 'extend')) {
    STATE.drawRect.x2 = wx; STATE.drawRect.y2 = wy; render(); return;
  }
  if (STATE.isDragging && STATE.currentTool === 'add_lasso') {
    const last = STATE.drawPoints[STATE.drawPoints.length - 1];
    if (Math.hypot(wx - last[0], wy - last[1]) > 2.5) STATE.drawPoints.push([wx, wy]);
    render(); return;
  }
  if (STATE.isDragging && STATE.currentTool === 'edit' && STATE.softDrag) {
    updateSoftDrag(wx, wy);
    return;
  }
  if (STATE.isDragging && STATE.currentTool === 'erase') {
    const last = STATE.drawPoints[STATE.drawPoints.length - 1];
    if (!last || Math.hypot(wx - last[0], wy - last[1]) > 2 / STATE.zoom)
      STATE.drawPoints.push([wx, wy]);
    render(); return;
  }
  if (STATE.isDragging && STATE.currentTool === 'cut' && STATE.cutLine) {
    STATE.cutLine.x2 = wx; STATE.cutLine.y2 = wy; render(); return;
  }

  // Hover states
  if (STATE.currentTool === 'select' || STATE.currentTool === 'delete' || STATE.currentTool === 'merge') {
    const hit = hitTestCloud(wx, wy);
    const newHover = hit ? hit.id : null;
    if (newHover !== STATE.hoveredId) { STATE.hoveredId = newHover; render(); }
    canvas.style.cursor = hit ? 'pointer' : 'crosshair';
  } else if (STATE.currentTool === 'edit' && STATE.selectedIds.length === 1) {
    const c = findCloud(STATE.selectedIds[0]);
    if (c) {
      const v = hitTestVertex(c, wx, wy);
      const seg = v === null ? hitTestSegment(c, wx, wy) : null;
      STATE.hoveredVertexIdx = v; STATE.hoveredSegmentIdx = seg;
      canvas.style.cursor = v !== null ? 'move' : (seg !== null ? 'grab' : 'crosshair');
      render(); // keep the brush ring under the cursor
    }
  } else if (STATE.currentTool === 'erase') {
    canvas.style.cursor = 'crosshair';
    render(); // keep the eraser ring under the cursor
  }
}

function onMouseUp(e) {
  if (STATE.isDragging) {
    if (STATE.currentTool === 'add_rect' && STATE.drawRect) {
      finishRect(STATE.drawRect);
      STATE.drawRect = null;
    } else if (STATE.currentTool === 'extend' && STATE.drawRect) {
      finishExtendRect(STATE.drawRect);
      STATE.drawRect = null;
    } else if (STATE.currentTool === 'add_lasso' && STATE.drawPoints.length > 5) {
      finishLasso(STATE.drawPoints);
      STATE.drawPoints = [];
    } else if (STATE.currentTool === 'edit' && STATE.softDrag) {
      const c = findCloud(STATE.softDrag.cloudId);
      if (c) markModified(c);   // pushUndo already taken at drag start
      STATE.softDrag = null;
      STATE.editVertexIdx = null;
    } else if (STATE.currentTool === 'erase' && STATE.drawPoints.length) {
      applyErase(STATE.regionTargetId, STATE.drawPoints);
      STATE.drawPoints = []; STATE.regionTargetId = null;
    } else if (STATE.currentTool === 'cut' && STATE.cutLine) {
      applyCut(STATE.regionTargetId, STATE.cutLine);
      STATE.cutLine = null; STATE.regionTargetId = null;
    }
  }
  STATE.isDragging = false;
  canvas.style.cursor = toolCursor();
  render();
}

function onWheel(e) {
  e.preventDefault();
  const [sx, sy] = getMouse(e);
  zoomAt(sx, sy, e.deltaY < 0 ? 1.12 : 1 / 1.12);
}

function onDblClick(e) {
  if (STATE.currentTool === 'add_poly' && STATE.drawPoints.length >= 3) {
    finishPolygon(STATE.drawPoints);
    STATE.drawPoints = [];
    render();
    return;
  }
  if (STATE.currentTool === 'remove_poly' && STATE.drawPoints.length >= 3) {
    finishRemoveRegion(STATE.drawPoints); // clears drawPoints itself
    return;
  }
  const [sx, sy] = getMouse(e);
  const [wx, wy] = screenToWorld(sx, sy);
  const hit = hitTestCloud(wx, wy);
  if (hit) zoomToCloud(hit);
}

/* ============================================================================
   KEYBOARD
   ========================================================================== */
function onKeyDown(e) {
  if (e.target.tagName === 'TEXTAREA') return;
  if (e.code === 'Space') { STATE.spaceHeld = true; canvas.style.cursor = 'grab'; e.preventDefault(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    if (e.shiftKey) redo(); else undo(); e.preventDefault(); return;
  }
  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (STATE.selectedIds.length) { STATE.selectedIds.forEach(deleteCloud); STATE.selectedIds = []; }
    e.preventDefault(); return;
  }
  if (e.key === 'Escape') {
    STATE.selectedIds = []; STATE.drawPoints = []; STATE.drawRect = null;
    STATE.cutLine = null; STATE.regionTargetId = null; STATE.isDragging = false;
    updateUI(); render(); return;
  }
  if (e.key === 'Enter' && STATE.currentTool === 'add_poly' && STATE.drawPoints.length >= 3) {
    finishPolygon(STATE.drawPoints); STATE.drawPoints = []; render(); return;
  }
  if (e.key === 'Enter' && STATE.currentTool === 'remove_poly' && STATE.drawPoints.length >= 3) {
    finishRemoveRegion(STATE.drawPoints); return;
  }
  if (e.key === 'Enter' && STATE.currentTool === 'merge' && STATE.selectedIds.length >= 2) {
    executeMerge(); return;
  }
  const map = { '1': 'pan', '2': 'select', '3': 'add_rect', '4': 'add_lasso',
                '5': 'add_poly', '6': 'edit', '7': 'merge', '8': 'extend', '9': 'delete' };
  if (map[e.key]) { setTool(map[e.key]); return; }
  if (e.key === 'e' || e.key === 'E') { setTool('erase'); return; }
  if (e.key === 'x' || e.key === 'X') { setTool('cut'); return; }
  if (e.key === 'r' || e.key === 'R') { setTool('remove_poly'); return; }
  if (e.key === 'f' || e.key === 'F') { fitToView(); render(); }
  if (e.key === 'o' || e.key === 'O') toggleOverlay();
  if (e.key === '+' || e.key === '=') zoomIn();
  if (e.key === '-') zoomOut();
  if (e.key === '[') { STATE.currentTool === 'erase' ? adjustEraseRadius(1 / EDIT_RADIUS_STEP) : adjustEditRadius(1 / EDIT_RADIUS_STEP); e.preventDefault(); }
  if (e.key === ']') { STATE.currentTool === 'erase' ? adjustEraseRadius(EDIT_RADIUS_STEP) : adjustEditRadius(EDIT_RADIUS_STEP); e.preventDefault(); }
}
function onKeyUp(e) {
  if (e.code === 'Space') { STATE.spaceHeld = false; canvas.style.cursor = toolCursor(); }
}

/* ============================================================================
   HIT TESTING
   ========================================================================== */
function hitTestCloud(wx, wy) {
  const active = STATE.clouds.filter(c => c.status !== 'deleted' && c.visible);
  // smallest area first (so nested/overlapping clouds select the inner one)
  active.sort((a, b) => bboxArea(a.bbox) - bboxArea(b.bbox));
  for (const c of active) {
    if (wx < c.bbox[0] || wx > c.bbox[2] || wy < c.bbox[1] || wy > c.bbox[3]) continue;
    if (pointInPolygon(wx, wy, c.polygon)) return c;
  }
  return null;
}
function bboxArea(b) { return (b[2] - b[0]) * (b[3] - b[1]); }
function pointInPolygon(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
    if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
  }
  return inside;
}
function hitTestVertex(c, wx, wy) {
  const hitR = CONTROL_POINT_HIT / STATE.zoom;
  const step = c.polygon.length > MAX_CONTROL_POINTS ? Math.ceil(c.polygon.length / MAX_CONTROL_POINTS) : 1;
  let best = null, bestD = hitR;
  for (let i = 0; i < c.polygon.length; i += step) {
    const d = Math.hypot(c.polygon[i][0] - wx, c.polygon[i][1] - wy);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}
function hitTestSegment(c, wx, wy) {
  const hitR = CONTROL_POINT_HIT / STATE.zoom;
  const pts = c.polygon;
  let best = null, bestD = hitR;
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    const d = pointToSegment(wx, wy, a[0], a[1], b[0], b[1]);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}
function pointToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(px - x1, py - y1);
  let t = ((px - x1) * dx + (py - y1) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}
// Closest point ON segment a→b to (px,py), clamped to the segment endpoints.
function projectPointToSegment(px, py, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return [a[0], a[1]];
  let t = ((px - a[0]) * dx + (py - a[1]) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return [a[0] + t * dx, a[1] + t * dy];
}

/* ============================================================================
   SCALLOP GENERATOR
   ========================================================================== */
function generateScallopedBoundary(polygon, arcRadius = SCALLOP_ARC_RADIUS, ppa = SCALLOP_POINTS_PER_ARC) {
  // ensure clockwise so normals point outward
  if (signedArea(polygon) > 0) polygon = polygon.slice().reverse();
  const out = [];
  const n = polygon.length;
  for (let i = 0; i < n; i++) {
    const p1 = polygon[i], p2 = polygon[(i + 1) % n];
    const dx = p2[0] - p1[0], dy = p2[1] - p1[1];
    const L = Math.hypot(dx, dy);
    if (L < arcRadius * 0.6) { out.push(p1); continue; }
    const nx = -dy / L, ny = dx / L;            // outward normal (CW)
    const numArcs = Math.max(1, Math.round(L / (arcRadius * 2)));
    for (let a = 0; a < numArcs; a++) {
      for (let j = 0; j < ppa; j++) {
        const angle = Math.PI * j / (ppa - 1);
        const t = (a + j / (ppa - 1)) / numArcs;
        const bx = p1[0] + dx * t, by = p1[1] + dy * t;
        const bump = Math.sin(angle) * arcRadius;
        out.push([bx + nx * bump, by + ny * bump]);
      }
    }
  }
  return out;
}
function signedArea(poly) {
  let s = 0;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++)
    s += (poly[j][0] - poly[i][0]) * (poly[j][1] + poly[i][1]);
  return s / 2;
}
function rdpSimplify(points, eps) {
  if (points.length <= 2) return points;
  let maxD = 0, idx = 0;
  const a = points[0], b = points[points.length - 1];
  for (let i = 1; i < points.length - 1; i++) {
    const d = pointToSegment(points[i][0], points[i][1], a[0], a[1], b[0], b[1]);
    if (d > maxD) { maxD = d; idx = i; }
  }
  if (maxD > eps) {
    const left = rdpSimplify(points.slice(0, idx + 1), eps);
    const right = rdpSimplify(points.slice(idx), eps);
    return left.slice(0, -1).concat(right);
  }
  return [a, b];
}

/* ============================================================================
   CONVEX HULL (Andrew's monotone chain) — for merge/extend
   ========================================================================== */
function convexHull(points) {
  const pts = points.slice().sort((p, q) => p[0] - q[0] || p[1] - q[1]);
  if (pts.length <= 2) return pts;
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  const upper = [];
  for (let i = pts.length - 1; i >= 0; i--) {
    const p = pts[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  lower.pop(); upper.pop();
  return lower.concat(upper);
}

/* ============================================================================
   CLOUD CREATION
   ========================================================================== */
function clampPoint(x, y) {
  return [Math.max(0, Math.min(STATE.displayW, x)), Math.max(0, Math.min(STATE.displayH, y))];
}
function makeCloud(polygon, source) {
  pushUndo();
  const clamped = polygon.map(p => clampPoint(p[0], p[1]));
  const xs = clamped.map(p => p[0]), ys = clamped.map(p => p[1]);
  const c = {
    id: STATE.nextId++,
    source,
    confidence: 1.0,
    scallopedness: 0,
    areaJson: 0,
    pointCount: clamped.length,
    status: 'added',
    polygon: clamped,
    bbox: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)],
    polygonImg: clamped.map(p => displayToImage(p[0], p[1])),
    polygonJson: null,
    isUserAdded: true, isModified: false, isMerged: false, visible: true,
  };
  STATE.clouds.push(c);
  STATE.addedCount++;
  STATE.selectedIds = [c.id];
  return c;
}

function finishRect(r) {
  const x1 = Math.min(r.x1, r.x2), y1 = Math.min(r.y1, r.y2);
  const x2 = Math.max(r.x1, r.x2), y2 = Math.max(r.y1, r.y2);
  if (x2 - x1 < MIN_RECT_SIZE || y2 - y1 < MIN_RECT_SIZE) { render(); return; }
  const rect = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
  const scalloped = generateScallopedBoundary(rect);
  makeCloud(scalloped, 'manual_rect');
  setStatus(`Added scalloped cloud C${STATE.nextId - 1}`);
  setTool('select'); updateUI(); render();
}

function finishLasso(raw) {
  const simplified = rdpSimplify(raw, 3);
  if (simplified.length < 3) { render(); return; }
  const scalloped = generateScallopedBoundary(simplified, 5);
  makeCloud(scalloped, 'manual_lasso');
  setStatus(`Added freehand cloud C${STATE.nextId - 1}`);
  setTool('select'); updateUI(); render();
}

function finishPolygon(pts) {
  if (pts.length < 3) return;
  const scalloped = generateScallopedBoundary(pts.slice(), 6);
  makeCloud(scalloped, 'manual_polygon');
  setStatus(`Added polygon cloud C${STATE.nextId - 1}`);
  setTool('select'); updateUI();
}

/* ============================================================================
   MERGE
   ========================================================================== */
function executeMerge() {
  if (STATE.selectedIds.length < 2) { setStatus('Select 2+ clouds to merge (Merge tool).'); return; }
  pushUndo();
  const targets = STATE.selectedIds.map(findCloud).filter(c => c && c.status !== 'deleted');
  let allPts = [];
  targets.forEach(c => { allPts = allPts.concat(c.polygon); });
  const hull = convexHull(allPts);
  const scalloped = generateScallopedBoundary(hull, 6);

  targets.forEach(c => { c.status = 'deleted'; });
  const xs = scalloped.map(p => p[0]), ys = scalloped.map(p => p[1]);
  const merged = {
    id: STATE.nextId++,
    source: 'manual_merge',
    confidence: 1.0, scallopedness: 0, areaJson: 0, pointCount: scalloped.length,
    status: 'merged',
    polygon: scalloped,
    bbox: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)],
    polygonImg: scalloped.map(p => displayToImage(p[0], p[1])),
    polygonJson: null,
    isUserAdded: true, isModified: false, isMerged: true, visible: true,
  };
  STATE.clouds.push(merged);
  STATE.mergedCount++;
  STATE.selectedIds = [merged.id];
  setTool('select');
  setStatus(`Merged ${targets.length} clouds into C${merged.id}`);
  updateUI(); render();
}

/* ============================================================================
   EXTEND
   ========================================================================== */
function finishExtendRect(r) {
  const c = findCloud(STATE.selectedIds[0]);
  if (!c) { STATE.drawRect = null; render(); return; }
  const x1 = Math.min(r.x1, r.x2), y1 = Math.min(r.y1, r.y2);
  const x2 = Math.max(r.x1, r.x2), y2 = Math.max(r.y1, r.y2);
  if (x2 - x1 < MIN_RECT_SIZE || y2 - y1 < MIN_RECT_SIZE) { render(); return; }
  pushUndo();
  const newRegion = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
  const combined = c.polygon.concat(newRegion);
  const hull = convexHull(combined);
  c.polygon = generateScallopedBoundary(hull, 6);
  recomputeBbox(c);
  markModified(c);
  setStatus(`Extended cloud C${c.id}`);
  updateUI(); render();
}

/* ============================================================================
   VERTEX / SEGMENT EDIT
   ========================================================================== */
/* --- Soft proportional drag ("pull the boundary") -------------------------
   Grab the boundary anywhere (a vertex, or a point inserted on an edge) and
   drag. A run of neighbouring vertices follows with a smooth cosine falloff
   measured ALONG the perimeter, so a whole stretch of the edge deforms as one
   continuous curve — A ──── pulled bend ──── B — instead of poking single
   points. Hold Alt for a rigid single-point move (precision).
   `alreadyPushed` = caller already took an undo snapshot (edge-insert case). */
function beginSoftDrag(c, anchorIdx, rigid, alreadyPushed) {
  if (!alreadyPushed) pushUndo();
  STATE.editVertexIdx = anchorIdx;
  STATE.isDragging = true;
  STATE.softDrag = {
    cloudId: c.id,
    anchorIdx,
    rigid: !!rigid,
    orig: c.polygon.map(p => [p[0], p[1]]),     // frozen start positions
    radiusWorld: STATE.editRadius / STATE.zoom, // brush reach in world units
    dist: cumulativePerimeterDist(c.polygon, anchorIdx),
  };
}

function updateSoftDrag(wx, wy) {
  const sd = STATE.softDrag;
  const c = findCloud(sd.cloudId);
  if (!c) return;
  const a = sd.orig[sd.anchorIdx];
  const dx = wx - a[0], dy = wy - a[1];

  if (sd.rigid) {
    c.polygon[sd.anchorIdx] = [wx, wy];
  } else {
    const R = sd.radiusWorld;
    for (let i = 0; i < c.polygon.length; i++) {
      const d = sd.dist[i];
      if (d >= R) { c.polygon[i] = [sd.orig[i][0], sd.orig[i][1]]; continue; }
      const w = 0.5 * (1 + Math.cos(Math.PI * d / R)); // 1 at anchor → 0 at radius
      c.polygon[i] = [sd.orig[i][0] + dx * w, sd.orig[i][1] + dy * w];
    }
  }
  recomputeBbox(c);
  render();
}

// Shortest distance from each vertex to `anchorIdx` walking along the closed
// boundary (min of the two directions). Used as the falloff metric.
function cumulativePerimeterDist(poly, anchorIdx) {
  const n = poly.length;
  const dist = new Array(n).fill(Infinity);
  dist[anchorIdx] = 0;
  let acc = 0;
  for (let k = 1; k < n; k++) {            // walk forward
    const i = (anchorIdx + k) % n, prev = (anchorIdx + k - 1) % n;
    acc += Math.hypot(poly[i][0] - poly[prev][0], poly[i][1] - poly[prev][1]);
    dist[i] = acc;
  }
  acc = 0;
  for (let k = 1; k < n; k++) {            // walk backward, keep the smaller
    const i = (anchorIdx - k + n) % n, next = (anchorIdx - k + 1 + n) % n;
    acc += Math.hypot(poly[i][0] - poly[next][0], poly[i][1] - poly[next][1]);
    if (acc < dist[i]) dist[i] = acc;
  }
  return dist;
}

function adjustEditRadius(factor) {
  STATE.editRadius = Math.max(EDIT_RADIUS_MIN,
    Math.min(EDIT_RADIUS_MAX, STATE.editRadius * factor));
  if (STATE.currentTool === 'edit') {
    setStatus(`Pull radius: ${Math.round(STATE.editRadius)} px  ([ / ] to change)`);
    render();
  }
}
function adjustEraseRadius(factor) {
  STATE.eraseRadius = Math.max(ERASE_RADIUS_MIN,
    Math.min(ERASE_RADIUS_MAX, STATE.eraseRadius * factor));
  setStatus(`Eraser size: ${Math.round(STATE.eraseRadius * 2)} px  ([ / ] to change)`);
  render();
}

/* ============================================================================
   REGION CLEANUP ENGINE (Eraser / Cut)
   ----------------------------------------------------------------------------
   These work on the cloud as a filled REGION, not as individual vertices:
     1. rasterize the cloud polygon to an off-screen mask (display resolution)
     2. subtract the eraser stroke / cut slice with destination-out
     3. re-trace the surviving boundary back to a polygon
     4. auto-cleanup: drop tiny fragments, simplify, recompute
   This is robust to self-intersections and long false-positive tails without
   any manual vertex work — the production "cloud-region editor" behaviour.
   ========================================================================== */

// Which cloud does an erase/cut act on? Prefer the current single selection
// (so you can paint slightly outside its edge), else the cloud under the cursor.
function pickRegionTarget(wx, wy) {
  if (STATE.selectedIds.length === 1) {
    const c = findCloud(STATE.selectedIds[0]);
    if (c && c.status !== 'deleted' && c.visible) return c;
  }
  return hitTestCloud(wx, wy);
}

// Fill the cloud polygon into an off-screen 1:1 (display-px) mask canvas.
function rasterizeCloudFill(c, pad) {
  const minx = Math.floor(c.bbox[0] - pad), miny = Math.floor(c.bbox[1] - pad);
  const maxx = Math.ceil(c.bbox[2] + pad), maxy = Math.ceil(c.bbox[3] + pad);
  const w = Math.max(1, maxx - minx), h = Math.max(1, maxy - miny);
  if (w * h > RASTER_MAX) return null;     // too large — bail safely
  const cv = document.createElement('canvas');
  cv.width = w; cv.height = h;
  const g = cv.getContext('2d');
  g.translate(-minx, -miny);
  g.beginPath();
  const p = c.polygon;
  g.moveTo(p[0][0], p[0][1]);
  for (let i = 1; i < p.length; i++) g.lineTo(p[i][0], p[i][1]);
  g.closePath();
  g.fillStyle = '#fff';
  g.fill();
  return { cv, g, w, h, ox: minx, oy: miny };
}

// Read the mask back, keep significant blobs, return their boundary polygons in
// world coords (largest first). keepLargestOnly drops everything but the biggest.
function retraceMask(mask, keepLargestOnly) {
  const { cv, w, h, ox, oy } = mask;
  const px = cv.getContext('2d').getImageData(0, 0, w, h).data;
  const bin = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) bin[i] = px[i * 4 + 3] > 128 ? 1 : 0;

  // connected components (4-connectivity), area + a guaranteed-boundary start px
  const labels = new Int32Array(w * h);
  const comps = [];
  const stack = [];
  let label = 0;
  for (let i = 0; i < w * h; i++) {
    if (!bin[i] || labels[i]) continue;
    label++;
    let area = 0, first = i;
    stack.length = 0; stack.push(i); labels[i] = label;
    while (stack.length) {
      const idx = stack.pop(); area++;
      if (idx < first) first = idx;
      const x = idx % w, y = (idx / w) | 0;
      if (x > 0     && bin[idx - 1] && !labels[idx - 1]) { labels[idx - 1] = label; stack.push(idx - 1); }
      if (x < w - 1 && bin[idx + 1] && !labels[idx + 1]) { labels[idx + 1] = label; stack.push(idx + 1); }
      if (y > 0     && bin[idx - w] && !labels[idx - w]) { labels[idx - w] = label; stack.push(idx - w); }
      if (y < h - 1 && bin[idx + w] && !labels[idx + w]) { labels[idx + w] = label; stack.push(idx + w); }
    }
    comps.push({ label, area, first });
  }
  if (!comps.length) return [];
  comps.sort((a, b) => b.area - a.area);

  let kept;
  if (keepLargestOnly) {
    kept = [comps[0]];
  } else {
    const total = comps.reduce((s, c) => s + c.area, 0);
    const minA = Math.max(MIN_FRAGMENT_PX, MIN_FRAGMENT_FRAC * total);
    kept = comps.filter(c => c.area >= minA);
    if (!kept.length) kept = [comps[0]];
  }

  const polys = [];
  for (const comp of kept) {
    const contour = traceBoundary(labels, w, h, comp.label, comp.first);
    if (contour.length < 3) continue;
    const world = contour.map(p => [p[0] + ox, p[1] + oy]);
    const simplified = rdpSimplify(world, CONTOUR_EPS);
    if (simplified.length >= 3) polys.push(simplified);
  }
  return polys;
}

// Moore-neighbour boundary tracing for one labelled component.
function traceBoundary(labels, w, h, label, startIdx) {
  const fg = (x, y) => x >= 0 && x < w && y >= 0 && y < h && labels[y * w + x] === label;
  // 8 neighbours in consistent clockwise order: W, NW, N, NE, E, SE, S, SW
  const nb = [[-1, 0], [-1, -1], [0, -1], [1, -1], [1, 0], [1, 1], [0, 1], [-1, 1]];
  const start = [startIdx % w, (startIdx / w) | 0];
  const contour = [start.slice()];
  let p = start.slice();
  let b = [start[0] - 1, start[1]];        // came from the (background) west pixel
  let guard = w * h * 8;
  while (guard-- > 0) {
    let bi = 0;
    for (let k = 0; k < 8; k++)
      if (p[0] + nb[k][0] === b[0] && p[1] + nb[k][1] === b[1]) { bi = k; break; }
    let found = null, prevBg = b;
    for (let s = 1; s <= 8; s++) {
      const k = (bi + s) % 8;
      const nx = p[0] + nb[k][0], ny = p[1] + nb[k][1];
      if (fg(nx, ny)) { found = [nx, ny]; break; }
      prevBg = [nx, ny];
    }
    if (!found) break;                       // isolated pixel
    b = prevBg; p = found;
    if (p[0] === start[0] && p[1] === start[1] && contour.length > 2) break;
    contour.push(p.slice());
  }
  return contour;
}

function shoelaceArea(poly) {
  let s = 0;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++)
    s += (poly[j][0] + poly[i][0]) * (poly[j][1] - poly[i][1]);
  return Math.abs(s) / 2;
}

function setCloudPolygon(c, poly) {
  c.polygon = poly.map(p => clampPoint(p[0], p[1]));
  recomputeBbox(c);
  markModified(c);
}

function makeCloudFromContour(poly, source) {
  const clamped = poly.map(p => clampPoint(p[0], p[1]));
  const xs = clamped.map(p => p[0]), ys = clamped.map(p => p[1]);
  const c = {
    id: STATE.nextId++,
    source, confidence: 1.0, scallopedness: 0, areaJson: 0,
    pointCount: clamped.length, status: 'added',
    polygon: clamped,
    bbox: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)],
    polygonImg: clamped.map(p => displayToImage(p[0], p[1])),
    polygonJson: null,
    isUserAdded: true, isModified: false, isMerged: false, visible: true,
  };
  STATE.clouds.push(c);
  STATE.addedCount++;
  return c;
}

// ERASER: subtract a painted stroke from the cloud; surviving big pieces are
// kept (a split becomes two clouds), tiny fragments are dropped.
function applyErase(cloudId, strokeWorld) {
  const c = findCloud(cloudId);
  if (!c || c.polygon.length < 3) return;
  const brush = STATE.eraseRadius / STATE.zoom;        // world-space radius
  const mask = rasterizeCloudFill(c, brush + 4);
  if (!mask) { setStatus('Region too large to erase at this zoom — zoom in first.'); return; }

  mask.g.globalCompositeOperation = 'destination-out';
  mask.g.strokeStyle = '#000';
  mask.g.lineWidth = brush * 2;
  mask.g.lineCap = 'round'; mask.g.lineJoin = 'round';
  mask.g.beginPath();
  mask.g.moveTo(strokeWorld[0][0], strokeWorld[0][1]);
  for (let i = 1; i < strokeWorld.length; i++) mask.g.lineTo(strokeWorld[i][0], strokeWorld[i][1]);
  if (strokeWorld.length === 1) mask.g.lineTo(strokeWorld[0][0] + 0.01, strokeWorld[0][1]); // dot
  mask.g.stroke();
  mask.g.globalCompositeOperation = 'source-over';

  const polys = retraceMask(mask, false);
  if (!polys.length) { setStatus('Eraser removed the whole cloud — delete it instead, or undo.'); return; }

  pushUndo();
  polys.sort((a, b) => shoelaceArea(b) - shoelaceArea(a));
  setCloudPolygon(c, polys[0]);
  let extra = 0;
  for (let i = 1; i < polys.length; i++) { makeCloudFromContour(polys[i], 'erase_split'); extra++; }
  STATE.selectedIds = [c.id];
  setStatus(extra
    ? `Erased — cloud split into ${extra + 1} regions (tiny fragments removed).`
    : `Erased region from C${c.id}.`);
  updateUI(); render();
}

// CUT: slice along a line and keep only the largest side (drops the tail).
function applyCut(cloudId, line) {
  const c = findCloud(cloudId);
  if (!c || c.polygon.length < 3) return;
  if (Math.hypot(line.x2 - line.x1, line.y2 - line.y1) < 3 / STATE.zoom) {
    setStatus('Cut: drag a line across the part you want to slice off.'); return;
  }
  const kerf = Math.max(CUT_KERF / STATE.zoom, 1.5);
  const mask = rasterizeCloudFill(c, kerf + 4);
  if (!mask) { setStatus('Region too large to cut at this zoom — zoom in first.'); return; }

  // extend the slice a little past both ends so it fully separates the shape
  let dx = line.x2 - line.x1, dy = line.y2 - line.y1;
  const L = Math.hypot(dx, dy) || 1;
  dx /= L; dy /= L;
  const ext = Math.max(mask.w, mask.h); // span the whole raster
  mask.g.globalCompositeOperation = 'destination-out';
  mask.g.strokeStyle = '#000';
  mask.g.lineWidth = kerf;
  mask.g.lineCap = 'round';
  mask.g.beginPath();
  mask.g.moveTo(line.x1 - dx * ext, line.y1 - dy * ext);
  mask.g.lineTo(line.x2 + dx * ext, line.y2 + dy * ext);
  mask.g.stroke();
  mask.g.globalCompositeOperation = 'source-over';

  const polys = retraceMask(mask, true);  // keep largest piece only
  if (!polys.length) { setStatus('Cut produced nothing — undo and try again.'); return; }
  pushUndo();
  setCloudPolygon(c, polys[0]);
  STATE.selectedIds = [c.id];
  setStatus(`Cut C${c.id} — kept the larger region, removed the slice.`);
  updateUI(); render();
}

// REMOVE REGION: the inverse of Add Polygon. The user draws a closed polygon
// around an unwanted area; it is SUBTRACTED from the selected cloud
// (Cloud − Polygon) as a true region boolean. Surviving big pieces are kept
// (a split → separate clouds); tiny fragments are auto-removed.
function finishRemoveRegion(polyPts) {
  if (polyPts.length < 3) { STATE.drawPoints = []; render(); return; }

  // Target = the selected cloud, else the cloud under the drawn polygon's centroid.
  let c = (STATE.selectedIds.length === 1) ? findCloud(STATE.selectedIds[0]) : null;
  if (!c || c.status === 'deleted') {
    let cx = 0, cy = 0;
    for (const p of polyPts) { cx += p[0]; cy += p[1]; }
    c = hitTestCloud(cx / polyPts.length, cy / polyPts.length);
  }
  if (!c || c.status === 'deleted' || c.polygon.length < 3) {
    setStatus('Remove Region: select a cloud first, then draw the area to subtract.');
    STATE.drawPoints = []; render(); return;
  }

  // A polygon fully inside the cloud would carve an interior hole — which the
  // single-ring cloud model can't represent. Guide the user to cross the edge.
  if (polyPts.every(p => pointInPolygon(p[0], p[1], c.polygon))) {
    setStatus('Remove Region: extend the area past the cloud edge — fully-enclosed holes are not supported.');
    STATE.drawPoints = []; render(); return;
  }

  const mask = rasterizeCloudFill(c, 4);
  if (!mask) {
    setStatus('Region too large to subtract at this zoom — zoom in first.');
    STATE.drawPoints = []; render(); return;
  }

  // Cloud − Polygon: punch the drawn polygon out of the cloud's filled mask.
  mask.g.globalCompositeOperation = 'destination-out';
  mask.g.fillStyle = '#000';
  mask.g.beginPath();
  mask.g.moveTo(polyPts[0][0], polyPts[0][1]);
  for (let i = 1; i < polyPts.length; i++) mask.g.lineTo(polyPts[i][0], polyPts[i][1]);
  mask.g.closePath();
  mask.g.fill();
  mask.g.globalCompositeOperation = 'source-over';

  const polys = retraceMask(mask, false);
  pushUndo();
  if (!polys.length) {
    // the drawn region covered the whole cloud
    c._prevStatus = c.status;
    c.status = 'deleted';
    STATE.deletedCount++;
    STATE.selectedIds = STATE.selectedIds.filter(x => x !== c.id);
    setStatus(`Removed region covered all of C${c.id} — cloud deleted.`);
  } else {
    polys.sort((a, b) => shoelaceArea(b) - shoelaceArea(a));
    setCloudPolygon(c, polys[0]);
    let extra = 0;
    for (let i = 1; i < polys.length; i++) { makeCloudFromContour(polys[i], 'region_subtract'); extra++; }
    STATE.selectedIds = [c.id];
    setStatus(extra
      ? `Subtracted region — C${c.id} split into ${extra + 1} parts (tiny fragments removed).`
      : `Subtracted region from C${c.id}.`);
  }
  STATE.drawPoints = [];
  setTool('select');
  updateUI(); render();
}
function deleteVertex(c, idx) {
  if (c.polygon.length <= 4) { setStatus('Cannot delete — minimum 3 points.'); return; }
  pushUndo();
  c.polygon.splice(idx, 1);
  recomputeBbox(c); markModified(c);
}
function deleteSegment(c, segIdx) {
  // remove the points strictly between segIdx and segIdx+1 isn't meaningful for
  // adjacent indices; instead remove the start vertex of the segment (opens boundary)
  if (c.polygon.length <= 4) { setStatus('Cannot delete — minimum 3 points.'); return; }
  pushUndo();
  c.polygon.splice(segIdx, 1);
  recomputeBbox(c); markModified(c);
  setStatus(`Removed segment on C${c.id}`);
}
function recomputeBbox(c) {
  const xs = c.polygon.map(p => p[0]), ys = c.polygon.map(p => p[1]);
  c.bbox = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}
function markModified(c) {
  if (c.status !== 'added' && c.status !== 'merged') {
    if (!c.isModified) STATE.modifiedCount++;
    c.status = 'modified';
    c.isModified = true;
  }
  c.polygonImg = c.polygon.map(p => displayToImage(p[0], p[1]));
  c.polygonJson = null; // force recompute on export
}

/* ============================================================================
   DELETE / RESTORE
   ========================================================================== */
function deleteCloud(id) {
  const c = findCloud(id);
  if (!c || c.status === 'deleted') return;
  pushUndo();
  c._prevStatus = c.status;
  c.status = 'deleted';
  STATE.deletedCount++;
  STATE.selectedIds = STATE.selectedIds.filter(x => x !== id);
  setStatus(`Deleted cloud C${id}`);
  updateUI(); render();
}
function restoreCloud(id) {
  const c = findCloud(id);
  if (!c || c.status !== 'deleted') return;
  pushUndo();
  c.status = c._prevStatus || 'approved';
  STATE.deletedCount = Math.max(0, STATE.deletedCount - 1);
  setStatus(`Restored cloud C${id}`);
  updateUI(); render();
}

/* ============================================================================
   UNDO / REDO
   ========================================================================== */
function snapshot() {
  return {
    clouds: STATE.clouds.map(c => ({
      ...c, polygon: c.polygon.map(p => [p[0], p[1]]), bbox: c.bbox.slice(),
    })),
    counts: { a: STATE.addedCount, d: STATE.deletedCount, m: STATE.modifiedCount, g: STATE.mergedCount },
    nextId: STATE.nextId, selectedIds: STATE.selectedIds.slice(),
  };
}
function pushUndo() {
  STATE.undoStack.push(snapshot());
  if (STATE.undoStack.length > UNDO_LIMIT) STATE.undoStack.shift();
  STATE.redoStack = [];
  updateUndoButtons();
}
function applySnapshot(s) {
  STATE.clouds = s.clouds.map(c => ({ ...c, polygon: c.polygon.map(p => [p[0], p[1]]), bbox: c.bbox.slice() }));
  STATE.addedCount = s.counts.a; STATE.deletedCount = s.counts.d;
  STATE.modifiedCount = s.counts.m; STATE.mergedCount = s.counts.g;
  STATE.nextId = s.nextId; STATE.selectedIds = s.selectedIds.slice();
}
function undo() {
  if (!STATE.undoStack.length) return;
  STATE.redoStack.push(snapshot());
  applySnapshot(STATE.undoStack.pop());
  updateUndoButtons(); updateUI(); render();
}
function redo() {
  if (!STATE.redoStack.length) return;
  STATE.undoStack.push(snapshot());
  applySnapshot(STATE.redoStack.pop());
  updateUndoButtons(); updateUI(); render();
}
function updateUndoButtons() {
  document.getElementById('btn-undo').disabled = !STATE.undoStack.length;
  document.getElementById('btn-redo').disabled = !STATE.redoStack.length;
}

/* ============================================================================
   TOOLS / UI
   ========================================================================== */
function setTool(t) {
  STATE.currentTool = t;
  STATE.drawPoints = []; STATE.drawRect = null;
  STATE.cutLine = null; STATE.regionTargetId = null;
  if (t !== 'merge') {
    if (STATE.selectedIds.length > 1) STATE.selectedIds = STATE.selectedIds.slice(0, 1);
  }
  document.querySelectorAll('.tool').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('tool-' + t);
  if (btn) btn.classList.add('active');
  canvas.style.cursor = toolCursor();
  const hints = {
    pan: '<b>Pan:</b> drag to move · scroll to zoom',
    select: '<b>Select:</b> click a cloud · double-click to zoom · Del to remove',
    add_rect: '<b>Add Rectangle:</b> drag a box → becomes a scalloped cloud',
    add_lasso: '<b>Add Lasso:</b> draw freehand → smoothed cloud',
    add_poly: '<b>Add Polygon:</b> click points · Enter / double-click to finish',
    remove_poly: '<b>Remove Region:</b> select a cloud, click points around the unwanted area · Enter / double-click to subtract it',
    edit: '<b>Edit:</b> drag the boundary to pull it · <b>[ ]</b> brush size · <b>Alt</b> single point · <b>Shift</b>-click delete',
    erase: '<b>Eraser:</b> paint over unwanted cloud area to remove it · <b>[ ]</b> brush size',
    cut: '<b>Cut:</b> drag a line across a false tail — the smaller side is sliced off',
    merge: '<b>Merge:</b> click 2+ clouds · Enter or button to merge',
    extend: '<b>Extend:</b> select a cloud, then drag a box to grow it',
    delete: '<b>Delete:</b> click a cloud to remove it',
  };
  document.getElementById('hud').innerHTML = hints[t] || '';
  document.getElementById('mergePanel').style.display = t === 'merge' ? 'block' : 'none';
  updateUI(); render();
}
function toolCursor() {
  if (STATE.currentTool === 'pan') return 'grab';
  return 'crosshair';
}
function toggleOverlay() {
  if (!overlayImg) { setStatus('No overlay image available.'); return; }
  STATE.showOverlay = !STATE.showOverlay;
  document.getElementById('tool-overlay').classList.toggle('active', STATE.showOverlay);
  render();
}
function toggleAllVisible() {
  const anyVisible = STATE.clouds.some(c => c.status !== 'deleted' && c.visible);
  STATE.clouds.forEach(c => { if (c.status !== 'deleted') c.visible = !anyVisible; });
  updateUI(); render();
}
function zoomToCloud(c) {
  const r = wrap.getBoundingClientRect();
  const bw = c.bbox[2] - c.bbox[0], bh = c.bbox[3] - c.bbox[1];
  STATE.zoom = Math.min(6, Math.min(r.width / (bw * 1.6), r.height / (bh * 1.6)));
  const cx = (c.bbox[0] + c.bbox[2]) / 2, cy = (c.bbox[1] + c.bbox[3]) / 2;
  STATE.panX = r.width / 2 - cx * STATE.zoom;
  STATE.panY = r.height / 2 - cy * STATE.zoom;
  STATE.selectedIds = [c.id];
  updateUI(); render();
}
function reset() {
  if (!confirm('Reset all changes and reload the original auto-detected clouds?')) return;
  location.reload();
}
function findCloud(id) { return STATE.clouds.find(c => c.id === id); }
function setStatus(t) { document.getElementById('statusText').textContent = t; }

function updateUI() {
  const active = STATE.clouds.filter(c => c.status !== 'deleted');
  const deleted = STATE.clouds.filter(c => c.status === 'deleted');
  document.getElementById('st-count').textContent = active.length;
  document.getElementById('st-added').textContent = STATE.addedCount;
  document.getElementById('st-mod').textContent = STATE.modifiedCount;
  document.getElementById('st-del').textContent = STATE.deletedCount;
  document.getElementById('sb-count').textContent = active.length;
  document.getElementById('mergeCount').textContent = STATE.selectedIds.length;

  // Cloud list
  const list = document.getElementById('cloudList');
  list.innerHTML = '';
  for (const c of active) {
    list.appendChild(cloudListItem(c));
  }
  if (deleted.length) {
    const sec = document.createElement('div');
    sec.className = 'sb-section';
    sec.innerHTML = `<span>Deleted (${deleted.length})</span>`;
    list.appendChild(sec);
    for (const c of deleted) list.appendChild(cloudListItem(c, true));
  }

  // Info panel
  const info = document.getElementById('infoPanel');
  if (STATE.selectedIds.length === 1) {
    const c = findCloud(STATE.selectedIds[0]);
    if (c) {
      info.innerHTML = `
        <div class="row"><span>Cloud</span><b>C${c.id}</b></div>
        <div class="row"><span>Source</span><span>${c.source}</span></div>
        <div class="row"><span>Status</span><span>${c.status}</span></div>
        <div class="row"><span>Confidence</span><span>${c.confidence.toFixed(2)}</span></div>
        <div class="row"><span>Points</span><span>${c.polygon.length}</span></div>
        <div class="actions">
          <button class="btn" onclick="EDITOR.zoomToCloud(EDITOR.findCloud(${c.id}))">Zoom</button>
          <button class="btn" onclick="EDITOR.setTool('edit')">Edit</button>
          <button class="btn" onclick="EDITOR.deleteCloud(${c.id})">Delete</button>
        </div>`;
    }
  } else if (STATE.selectedIds.length > 1) {
    info.innerHTML = `<div class="row"><span>Selected for merge</span><b>${STATE.selectedIds.map(i => 'C' + i).join(', ')}</b></div>`;
  } else {
    info.innerHTML = '<div class="info-empty">No cloud selected</div>';
  }
  updateUndoButtons();
}

function cloudListItem(c, deleted = false) {
  const div = document.createElement('div');
  let cls = 'ci';
  if (STATE.selectedIds.includes(c.id)) cls += STATE.currentTool === 'merge' ? ' mergesel' : ' sel';
  if (deleted) cls += ' deleted';
  div.className = cls;
  const dotCls = c.isMerged ? 'merged' : (c.isUserAdded ? 'added' : (c.isModified ? 'mod' : 'auto'));
  div.innerHTML = `
    <div class="ci-left">
      <span class="dot ${dotCls}"></span>
      <span class="ci-id">C${c.id}</span>
      <span class="ci-src">${c.source}</span>
    </div>
    ${deleted
      ? `<button class="ci-del" title="Restore" style="opacity:1;color:var(--green)" onclick="event.stopPropagation();EDITOR.restoreCloud(${c.id})">↩</button>`
      : `<button class="ci-del" title="Delete" onclick="event.stopPropagation();EDITOR.deleteCloud(${c.id})">✕</button>`}`;
  if (!deleted) {
    div.onclick = () => {
      if (STATE.currentTool === 'merge') {
        const i = STATE.selectedIds.indexOf(c.id);
        if (i >= 0) STATE.selectedIds.splice(i, 1); else STATE.selectedIds.push(c.id);
      } else {
        STATE.selectedIds = [c.id];
      }
      updateUI(); render();
    };
  }
  return div;
}

/* ============================================================================
   DONE / SAVE
   ========================================================================== */
function validateBeforeSave() {
  const errors = [], warnings = [];
  const active = STATE.clouds.filter(c => c.status !== 'deleted');
  if (active.length === 0) warnings.push('No clouds in the approved set — outputs will be empty.');
  for (const c of active) {
    if (c.polygon.length < 3) errors.push(`Cloud C${c.id} has fewer than 3 points.`);
    const bw = c.bbox[2] - c.bbox[0], bh = c.bbox[3] - c.bbox[1];
    if (bw < 8 || bh < 8) warnings.push(`Cloud C${c.id} is very small.`);
    if (bw * bh > STATE.displayW * STATE.displayH * 0.9) warnings.push(`Cloud C${c.id} covers most of the drawing.`);
  }
  return { errors, warnings };
}

function buildPayload() {
  const active = STATE.clouds.filter(c => c.status !== 'deleted');
  return {
    clouds: active.map(c => ({
      id: c.id,
      source: c.source,
      status: c.status,
      confidence: c.confidence,
      polygon_display: c.polygon,
      polygon_img: c.polygonImg || null,
      polygon_json: c.polygonJson || null,
    })),
    deleted_ids: STATE.clouds.filter(c => c.status === 'deleted').map(c => c.id),
    edit_summary: {
      original_count: STATE.originalCount,
      deleted: STATE.deletedCount,
      added: STATE.addedCount,
      modified: STATE.modifiedCount,
      merged: STATE.mergedCount,
    },
  };
}

function showDone() {
  const { errors, warnings } = validateBeforeSave();
  const active = STATE.clouds.filter(c => c.status !== 'deleted');

  document.getElementById('modalWarnings').innerHTML =
    errors.map(e => `<div class="err">⚠ ${e}</div>`).join('') +
    warnings.map(w => `<div class="warn">⚠ ${w}</div>`).join('');

  const m = {
    'Approved (unchanged)': active.filter(c => c.status === 'approved').length,
    'Added': STATE.addedCount,
    'Modified': STATE.modifiedCount,
    'Merged': STATE.mergedCount,
    'Deleted': STATE.deletedCount,
    'Total approved': active.length,
  };
  document.getElementById('modalSummary').innerHTML =
    Object.entries(m).map(([k, v]) => `<div class="row"><span>${k}</span><b>${v}</b></div>`).join('');

  const preview = { clouds: active.slice(0, 2).map(c => ({
    id: c.id, source: c.source, status: c.status, points: c.polygon.length })),
    note: `… ${active.length} clouds total` };
  document.getElementById('jsonPreview').value = JSON.stringify(preview, null, 2);

  document.getElementById('saveBtn').disabled = errors.length > 0;
  document.getElementById('doneModal').style.display = 'flex';
}
function closeDone() { document.getElementById('doneModal').style.display = 'none'; }
function copyJson() {
  navigator.clipboard.writeText(JSON.stringify(buildPayload(), null, 2));
  setStatus('Payload copied to clipboard.');
}
async function save() {
  const btn = document.getElementById('saveBtn');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const resp = await fetch('/api/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildPayload()),
    });
    const result = await resp.json();
    if (result.status === 'ok') {
      document.querySelector('.modal').innerHTML = `
        <h2 style="color:var(--green)">✓ Saved</h2>
        <p>${result.result.cloud_count} clouds approved and written.</p>
        <div class="summary">
          <div class="row"><span>JSON</span><span>approved_clouds.json</span></div>
          <div class="row"><span>Mask</span><span>cloud_mask_approved.png</span></div>
          <div class="row"><span>Overlay</span><span>overlay_approved.jpg</span></div>
        </div>
        <p style="color:var(--muted)">You can close this tab. The editor has shut down.</p>`;
      window.removeEventListener('beforeunload', () => {});
    } else {
      throw new Error(result.message || 'Unknown error');
    }
  } catch (err) {
    btn.disabled = false; btn.textContent = '✓ Save & Exit';
    document.getElementById('modalWarnings').innerHTML =
      `<div class="err">Save failed: ${err.message}. Retry.</div>`;
  }
}

/* ============================================================================
   PUBLIC API + BOOT
   ========================================================================== */
const EDITOR = {
  setTool, zoomIn, zoomOut, fitToView, toggleOverlay, toggleAllVisible,
  undo, redo, reset, deleteCloud, restoreCloud, zoomToCloud, findCloud,
  executeMerge, showDone, closeDone, copyJson, save,
};
window.EDITOR = EDITOR;
init();
