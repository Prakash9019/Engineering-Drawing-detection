"use client";

import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
const STAGES = [
  ["1", "Format detection", "Raster / PDF inspection and image enhancement"],
  ["2", "Title & revision", "Drawing metadata and extraction-scope decision"],
  ["3", "Revision clouds", "Gemini localization with OpenCV boundary refinement"],
  ["4", "Notes intelligence", "Notes, abbreviations, and drawing-specific rules"],
  ["5", "SOW scope memory", "Excel symbol register: allowed and excluded symbols"],
  ["6", "Table extraction", "Embedded tag lists and engineering schedules"],
  ["7", "Candidate extraction", "SAHI patches, Gemini vision, and Tesseract OCR"],
  ["8", "Geometric association", "Leader lines, pipes, and spatial relationships"],
  ["9", "Hierarchy graph", "Equipment tree, pipelines, junctions, and control loops"],
  ["10", "Validation & dedup", "ISA rules, register checks, and duplicate resolution"],
  ["11", "CEDM normalization", "Canonical tags and Annexure-4 field mapping"],
  ["12", "Confidence routing", "Auto-accept, human review, audit, and deliverables"],
] as const;

type EventItem = { event: string; step?: string; eval_id?: string; status?: string; timestamp?: string };
type Job = {
  job_id: string;
  status: string;
  input_name: string;
  payload?: { message?: string; artifacts?: string[]; pair_evals?: Array<{ eval_id: string; status: string; model_calls: number }>; extraction_scope?: string };
  events?: EventItem[];
};

const STEP_EVENTS: string[][] = [
  ["1_format_detection"], ["2_title_revision"], ["3_revision_clouds"], ["4_notes_context"],
  ["5_scope_memory"], ["6_table_extraction"], ["7_full_sheet_candidates", "7b_cloud_scope_candidates"],
  ["8_full_sheet_geometry", "8b_cloud_scope_geometry"], ["9_hierarchy_graph", "9b_hierarchy_deliverables"],
  ["10_validation", "10b_deduplication"], ["11_cedm_normalization"],
  ["12_confidence_routing", "12b_reporting", "12c_comparison", "12d_visualization"],
];

function getStageStatus(index: number, events: EventItem[], jobStatus: string) {
  const related = events.filter((item) => STEP_EVENTS[index].includes(item.step || ""));
  if (related.some((item) => item.event === "logical_step.completed")) return "done";
  if (related.some((item) => item.event === "logical_step.started")) {
    return jobStatus === "failed" ? "failed" : "running";
  }
  if (jobStatus === "failed" && events.length === 0) return "failed";
  return "pending";
}

export default function Home() {
  const [drawing, setDrawing] = useState<File | null>(null);
  const [register, setRegister] = useState<File | null>(null);
  const [sow, setSow] = useState<File | null>(null);
  const [workers, setWorkers] = useState("1");
  const [job, setJob] = useState<Job | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const events = job?.events || [];

  const evaluations = useMemo(() => {
    const seen = new Map<string, { eval_id: string; status: string; model_calls: number }>();
    for (const item of events) {
      if (item.event === "eval.completed" && item.eval_id) {
        seen.set(item.eval_id, { eval_id: item.eval_id, status: item.status || "UNKNOWN", model_calls: 0 });
      }
    }
    for (const item of job?.payload?.pair_evals || []) seen.set(item.eval_id, item);
    return Array.from(seen.values());
  }, [events, job?.payload?.pair_evals]);

  useEffect(() => {
    if (!job?.job_id || ["completed", "failed"].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch(API + "/api/jobs/" + job.job_id, { cache: "no-store" });
        if (response.ok) setJob(await response.json());
      } catch {
        // Retain the last known run state during brief polling failures.
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.job_id, job?.status]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!drawing) {
      setError("Choose a drawing file before starting the workflow.");
      return;
    }
    setSubmitting(true);
    setError("");
    setJob(null);
    const body = new FormData();
    body.set("drawing", drawing);
    body.set("workers", workers);
    if (register) body.set("register", register);
    if (sow) body.set("sow", sow);
    try {
      const response = await fetch(API + "/api/jobs", { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not start the workflow.");
      const initial = await fetch(API + data.status_url);
      setJob(await initial.json());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unexpected API error.");
    } finally {
      setSubmitting(false);
    }
  }

  function picker(setter: (file: File | null) => void) {
    return (event: ChangeEvent<HTMLInputElement>) => setter(event.target.files?.[0] || null);
  }

  const completed = STAGES.filter((_, index) => getStageStatus(index, events, job?.status || "queued") === "done").length;
  const progress = Math.round((completed / STAGES.length) * 100);
  const evalNames = [
    "Format + title", "Clouds + notes", "SOW + tables",
    "Extraction + geometry", "Hierarchy + validation", "Normalization + routing",
  ];
  const evalIds = [
    "eval_01_format_title", "eval_02_cloud_notes", "eval_03_scope_tables",
    "eval_04_extraction_geometry", "eval_05_hierarchy_validation", "eval_06_normalization_routing",
  ];
  const downloads = [
    "final_tags.xlsx", "final_hierarchy.xlsx", "hierarchy_viewer.html",
    "hierarchy_graph.html", "eval_coverage_report.json", "final_tags_vs_annexure4.xlsx",
  ];

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#">
          <span className="brand-mark">CD</span>
          <span>CDCI <small>DRAWING INTELLIGENCE</small></span>
        </a>
        <div className="top-status"><span className="status-dot" /> LOCAL DEMO ENVIRONMENT</div>
      </header>

      <section className="hero">
        <div className="eyebrow">ENGINEERING DOCUMENT AUTOMATION <span>•</span> LANGGRAPH WORKFLOW</div>
        <h1>From complex P&amp;ID drawings<br /><em>to structured engineering data.</em></h1>
        <p>Upload a drawing to run the 12-stage extraction pipeline, inspect its orchestration, and review the generated Annexure-4 deliverables.</p>
      </section>

      <section className="workspace">
        <aside className="panel upload-panel">
          <div className="panel-heading">
            <div><span className="kicker">01 / INPUT</span><h2>Start a new extraction</h2></div>
            <span className="tag">PDF · RASTER</span>
          </div>
          <form onSubmit={submit}>
            <label
              className={"dropzone " + (dragging ? "dragging" : "")}
              onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                const file = event.dataTransfer.files?.[0];
                if (file) setDrawing(file);
              }}
            >
              <input type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff" onChange={picker(setDrawing)} />
              <span className="upload-icon">↑</span>
              <strong>{drawing ? drawing.name : "Drop your drawing here"}</strong>
              <span>{drawing ? (drawing.size / 1024 / 1024).toFixed(2) + " MB selected" : "or browse files · PDF, PNG, JPG, TIFF"}</span>
            </label>
            <div className="file-row">
              <label>Annexure-4 register <small>Optional · defaults to repository sample</small>
                <input type="file" accept=".xlsx,.xlsm" onChange={picker(setRegister)} />
                {register && <span className="file-selected">{register.name}</span>}
              </label>
              <label>Scope of Work workbook <small>Optional · defaults to repository sample</small>
                <input type="file" accept=".xlsx,.xlsm" onChange={picker(setSow)} />
                {sow && <span className="file-selected">{sow.name}</span>}
              </label>
            </div>
            <label className="worker-setting">Patch workers
              <select value={workers} onChange={(event) => setWorkers(event.target.value)}>
                <option value="1">1 · Conservative / free-tier</option>
                <option value="2">2 · Balanced</option>
                <option value="4">4 · Faster processing</option>
                <option value="8">8 · High concurrency</option>
              </select>
            </label>
            <button className="primary-button" type="submit" disabled={submitting || !drawing}>
              {submitting ? "Starting workflow…" : "Run extraction workflow"} <span>↗</span>
            </button>
            {error && <div className="error-box">{error}</div>}
            <p className="fineprint">API key stays on the backend. Files are stored in the local runtime folder for this demo.</p>
          </form>
        </aside>

        <section className="panel pipeline-panel">
          <div className="panel-heading">
            <div><span className="kicker">02 / ORCHESTRATION</span><h2>12-stage processing pipeline</h2></div>
            <span className="tag">6 PAIRED EVALS</span>
          </div>
          <div className="pipeline-summary">
            <div><strong>{String(completed).padStart(2, "0")}<small> / 12</small></strong><span>Stages complete</span></div>
            <div className="progress-track"><span style={{ width: progress + "%" }} /></div>
            <span className="progress-value">{progress}%</span>
          </div>
          <div className="stage-list">
            {STAGES.map((stage, index) => {
              const status = getStageStatus(index, events, job?.status || "queued");
              return (
                <div className={"stage " + status} key={stage[0]}>
                  <div className="stage-number">{status === "done" ? "✓" : stage[0]}</div>
                  <div className="stage-copy"><strong>{stage[1]}</strong><span>{stage[2]}</span></div>
                  <span className="stage-state">{status === "done" ? "DONE" : status === "running" ? "RUNNING" : status === "failed" ? "FAILED" : "WAITING"}</span>
                </div>
              );
            })}
          </div>
        </section>
      </section>

      <section className="lower-grid">
        <div className="panel telemetry-panel">
          <div className="panel-heading"><div><span className="kicker">03 / QUALITY GATES</span><h2>Paired-step evaluations</h2></div><span className="tag">0 EXTRA LLM CALLS</span></div>
          <p className="muted">Each gate validates artifacts and structural invariants across two logical stages. No separate LLM judge is called.</p>
          <div className="eval-grid">
            {evalNames.map((name, index) => {
              const result = evaluations.find((item) => item.eval_id === evalIds[index]);
              return <div className="eval-item" key={name}><span>EVAL 0{index + 1}</span><strong>{name}</strong><small className={result?.status === "PASS" ? "pass" : ""}>{result?.status || "PENDING"}</small></div>;
            })}
          </div>
        </div>

        <div className="panel job-panel">
          <div className="panel-heading"><div><span className="kicker">04 / RUN STATUS</span><h2>Execution summary</h2></div></div>
          {!job ? <div className="empty-state">Your workflow run will appear here once you upload a drawing.</div> : (
            <>
              <div className="job-state"><span className={"state-pill " + job.status}>{job.status}</span><span>{job.payload?.extraction_scope || "Scope pending"}</span></div>
              <div className="job-id">RUN ID <code>{job.job_id}</code></div>
              <div className="job-message">{job.payload?.message || "Workflow queued."}</div>
              {job.status === "failed" && <div className="error-box">{job.payload?.message}</div>}
              {job.status === "completed" && <div className="download-list">
                {downloads.map((file) => (
                  <a key={file} href={API + "/api/jobs/" + job.job_id + "/files/" + encodeURIComponent(file)} target="_blank" rel="noreferrer">{file} <span>↓</span></a>
                ))}
              </div>}
            </>
          )}
        </div>
      </section>

      <footer><span>CDCI ENGINEERING INTELLIGENCE</span><span>DETERMINISTIC PIPELINE · MODEL-ASSISTED EXTRACTION · HUMAN REVIEW</span></footer>
    </main>
  );
}
