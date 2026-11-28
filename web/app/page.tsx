"use client";
import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";
const API=process.env.NEXT_PUBLIC_API_URL||"http://127.0.0.1:8000";
const API_KEY=process.env.NEXT_PUBLIC_CDCI_API_KEY||"";
const STAGES=[
 ["1","Format detection","Raster / PDF inspection and image enhancement"],
 ["2","Title & revision","Drawing metadata and extraction-scope decision"],
 ["3","Revision clouds","Gemini localization with OpenCV refinement"],
 ["4","Notes intelligence","Notes, abbreviations and drawing rules"],
 ["5","SOW scope memory","Excel symbol register and scope rules"],
 ["6","Table extraction","Tag lists and engineering schedules"],
 ["7","Candidate extraction","SAHI patches, Gemini vision and OCR"],
 ["8","Geometric association","Leader lines, pipes and spatial relationships"],
 ["9","Hierarchy graph","Equipment tree, pipelines and control loops"],
 ["10","Validation & dedup","ISA rules, register checks and duplicate resolution"],
 ["11","CEDM normalization","Canonical tags and Annexure-4 mapping"],
 ["12","Confidence routing","Auto-accept, human review and deliverables"],
] as const;
type EventItem={event:string;step?:string;eval_id?:string;status?:string;timestamp?:string;event_id?:number;decision_id?:string;decision?:string};
type ReviewItem={queue_id:string;candidate_id?:string;tag?:string;priority?:number;reason?:string;confidence?:number;human_decision?:string;human_note?:string};
type Job={job_id:string;status:string;input_name:string;payload?:{message?:string;artifacts?:string[];pair_evals?:Array<{eval_id:string;status:string;model_calls:number}>;extraction_scope?:string};events?:EventItem[]};
const STEP_EVENTS:string[][]=[
 ["1_format_detection"],["2_title_revision"],["3_revision_clouds"],["4_notes_context"],
 ["5_scope_memory"],["6_table_extraction"],["7_full_sheet_candidates","7b_cloud_scope_candidates"],
 ["8_full_sheet_geometry","8b_cloud_scope_geometry"],["9_hierarchy_graph","9b_hierarchy_deliverables"],
 ["10_validation","10b_deduplication"],["11_cedm_normalization"],
 ["12_confidence_routing","12b_reporting","12c_comparison","12d_visualization"]
];
function stageStatus(i:number,events:EventItem[],status:string){
 const related=events.filter(e=>STEP_EVENTS[i].includes(e.step||""));
 if(related.some(e=>e.event==="logical_step.completed"))return "done";
 if(related.some(e=>e.event==="logical_step.started"))return status==="failed"?"failed":"running";
 return "pending";
}
async function apiFetch(path:string,init:RequestInit={}){
 const headers=new Headers(init.headers||{});
 if(API_KEY)headers.set("X-API-Key",API_KEY);
 return fetch(API+path,{...init,headers,cache:"no-store"});
}
export default function Home(){
 const [drawing,setDrawing]=useState<File|null>(null),[register,setRegister]=useState<File|null>(null),[sow,setSow]=useState<File|null>(null);
 const [workers,setWorkers]=useState("1"),[job,setJob]=useState<Job|null>(null);
 const [submitting,setSubmitting]=useState(false),[error,setError]=useState(""),[dragging,setDragging]=useState(false);
 const [reviewId,setReviewId]=useState(""),[reviewNote,setReviewNote]=useState(""),[reviewDecision,setReviewDecision]=useState("approve");
 const [reviewMessage,setReviewMessage]=useState("");
 const events=job?.events||[];
 const evaluations=useMemo(()=>{
  const seen=new Map<string,{eval_id:string;status:string;model_calls:number}>();
  for(const e of events)if(e.event==="eval.completed"&&e.eval_id)seen.set(e.eval_id,{eval_id:e.eval_id,status:e.status||"UNKNOWN",model_calls:0});
  for(const e of job?.payload?.pair_evals||[])seen.set(e.eval_id,e);
  return Array.from(seen.values());
 },[events,job?.payload?.pair_evals]);
 useEffect(()=>{
  if(!job?.job_id||["completed","failed","cancelled"].includes(job.status))return;
  let source:EventSource|undefined;
  let active=true;
  const load=async()=>{
   try{const r=await apiFetch("/api/jobs/"+job.job_id);if(r.ok&&active)setJob(await r.json());}catch{/* reconnect on next render */}
  };
  const connect=()=>{
   if(!active)return;
   const url=new URL(API+"/api/jobs/"+job.job_id+"/events");
   // EventSource cannot set headers; use queryless public local stream only with auth disabled.
   // For protected deployments we use the status endpoint fallback below.
   if(API_KEY){const timer=window.setInterval(load,2500);return ()=>window.clearInterval(timer);}
   source=new EventSource(url.toString());
   source.onmessage=(event)=>{
    try{const item=JSON.parse(event.data) as EventItem;setJob(current=>current?{...current,events:[...(current.events||[]),item]}:current);}catch{/* ignore malformed telemetry */}
   };
   source.addEventListener("eval.completed",(event)=>{try{const item=JSON.parse((event as MessageEvent).data) as EventItem;setJob(current=>current?{...current,events:[...(current.events||[]),item]}:current);}catch{/* ignore */}});
   source.addEventListener("logical_step.started",(event)=>{try{const item=JSON.parse((event as MessageEvent).data) as EventItem;setJob(current=>current?{...current,events:[...(current.events||[]),item]}:current);}catch{/* ignore */}});
   source.addEventListener("logical_step.completed",(event)=>{try{const item=JSON.parse((event as MessageEvent).data) as EventItem;setJob(current=>current?{...current,events:[...(current.events||[]),item]}:current);}catch{/* ignore */}});
   source.onerror=()=>{source?.close();void load();};
   return ()=>source?.close();
  };
  const cleanup=connect();
  return()=>{active=false;source?.close();if(typeof cleanup==="function")cleanup();};
 },[job?.job_id,job?.status]);
 async function submit(e:FormEvent){
  e.preventDefault();if(!drawing){setError("Choose a drawing file first.");return;}
  setSubmitting(true);setError("");setJob(null);
  const body=new FormData();body.set("drawing",drawing);body.set("workers",workers);
  if(register)body.set("register",register);if(sow)body.set("sow",sow);
  try{
   const r=await apiFetch("/api/jobs",{method:"POST",body,headers:{"Idempotency-Key":crypto.randomUUID()}});
   const data=await r.json();if(!r.ok)throw new Error(data.detail||"Could not enqueue workflow.");
   const initial=await apiFetch(data.status_url);if(!initial.ok)throw new Error("Could not load job status.");
   setJob(await initial.json());
  }catch(ex){setError(ex instanceof Error?ex.message:"Unexpected API error.");}
  finally{setSubmitting(false);}
 }
 function picker(setter:(file:File|null)=>void){return(e:ChangeEvent<HTMLInputElement>)=>setter(e.target.files?.[0]||null);}
 async function submitReview(e:FormEvent){
  e.preventDefault();if(!job||!reviewId.trim())return;
  setReviewMessage("");
  try{
   const r=await apiFetch("/api/jobs/"+job.job_id+"/review",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({decision_id:reviewId,decision:reviewDecision,reviewer:"recruiter-demo",note:reviewNote})});
   const data=await r.json();if(!r.ok)throw new Error(data.detail||"Review action failed.");
   setReviewMessage("Decision recorded for "+reviewId);setReviewId("");setReviewNote("");
   const refreshed=await apiFetch("/api/jobs/"+job.job_id);if(refreshed.ok)setJob(await refreshed.json());
  }catch(ex){setReviewMessage(ex instanceof Error?ex.message:"Review request failed.");}
 }
 const done=STAGES.filter((_,i)=>stageStatus(i,events,job?.status||"queued")==="done").length;
 const progress=Math.round(done/STAGES.length*100);
 const evalNames=["Format + title","Clouds + notes","SOW + tables","Extraction + geometry","Hierarchy + validation","Normalization + routing"];
 const evalIds=["eval_01_format_title","eval_02_cloud_notes","eval_03_scope_tables","eval_04_extraction_geometry","eval_05_hierarchy_validation","eval_06_normalization_routing"];
 const downloads=["final_tags.xlsx","final_hierarchy.xlsx","hierarchy_viewer.html","hierarchy_graph.html","eval_coverage_report.json","final_tags_vs_annexure4.xlsx","human_review_queue.json"];
 return <main className="shell">
  <header className="topbar"><a className="brand" href="#"><span className="brand-mark">CD</span><span>CDCI <small>DRAWING INTELLIGENCE</small></span></a><div className="top-status"><span className="status-dot"/> LOCAL WORKER QUEUE</div></header>
  <section className="hero"><div className="eyebrow">ENGINEERING DOCUMENT AUTOMATION <span>•</span> LANGGRAPH WORKFLOW</div><h1>From complex P&amp;ID drawings<br/><em>to structured engineering data.</em></h1><p>Submit a drawing to the durable queue, watch the 12-stage DAG, inspect paired quality gates and review the final deliverables.</p></section>
  <section className="workspace">
   <aside className="panel upload-panel"><div className="panel-heading"><div><span className="kicker">01 / INPUT</span><h2>Start a new extraction</h2></div><span className="tag">PDF · RASTER</span></div>
    <form onSubmit={submit}>
     <label className={"dropzone "+(dragging?"dragging":"")} onDragOver={e=>{e.preventDefault();setDragging(true);}} onDragLeave={()=>setDragging(false)} onDrop={e=>{e.preventDefault();setDragging(false);const f=e.dataTransfer.files?.[0];if(f)setDrawing(f);}}>
      <input type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff" onChange={picker(setDrawing)}/><span className="upload-icon">↑</span><strong>{drawing?drawing.name:"Drop your drawing here"}</strong><span>{drawing?(drawing.size/1024/1024).toFixed(2)+" MB selected":"or browse · PDF, PNG, JPG, TIFF"}</span>
     </label>
     <div className="file-row"><label>Annexure-4 register <small>Optional · repository sample by default</small><input type="file" accept=".xlsx,.xlsm" onChange={picker(setRegister)}/>{register&&<span className="file-selected">{register.name}</span>}</label>
      <label>Scope of Work workbook <small>Optional · repository sample by default</small><input type="file" accept=".xlsx,.xlsm" onChange={picker(setSow)}/>{sow&&<span className="file-selected">{sow.name}</span>}</label></div>
     <label className="worker-setting">SAHI patch workers<select value={workers} onChange={e=>setWorkers(e.target.value)}><option value="1">1 · Conservative / free-tier</option><option value="2">2 · Balanced</option><option value="4">4 · Faster</option><option value="8">8 · High concurrency</option></select></label>
     <button className="primary-button" type="submit" disabled={submitting||!drawing}>{submitting?"Submitting…":"Queue extraction workflow"} <span>↗</span></button>
     {error&&<div className="error-box">{error}</div>}<p className="fineprint">Gemini credentials remain on the worker. API key is sent as a request header and is not stored in the job payload.</p>
    </form></aside>
   <section className="panel pipeline-panel"><div className="panel-heading"><div><span className="kicker">02 / ORCHESTRATION</span><h2>12-stage processing pipeline</h2></div><span className="tag">6 PAIRED EVALS</span></div>
    <div className="pipeline-summary"><div><strong>{String(done).padStart(2,"0")}<small> / 12</small></strong><span>Stages complete</span></div><div className="progress-track"><span style={{width:progress+"%"}}/></div><span className="progress-value">{progress}%</span></div>
    <div className="stage-list">{STAGES.map((s,i)=>{const status=stageStatus(i,events,job?.status||"queued");return <div className={"stage "+status} key={s[0]}><div className="stage-number">{status==="done"?"✓":s[0]}</div><div className="stage-copy"><strong>{s[1]}</strong><span>{s[2]}</span></div><span className="stage-state">{status.toUpperCase()}</span></div>;})}</div>
   </section>
  </section>
  <section className="lower-grid">
   <div className="panel telemetry-panel"><div className="panel-heading"><div><span className="kicker">03 / QUALITY GATES</span><h2>Paired-step evaluations</h2></div><span className="tag">0 EXTRA LLM CALLS</span></div><p className="muted">Deterministic checks validate artefact contracts, candidate geometry, hierarchy and unique review identifiers.</p>
    <div className="eval-grid">{evalNames.map((name,i)=>{const r=evaluations.find(x=>x.eval_id===evalIds[i]);return <div className="eval-item" key={name}><span>EVAL 0{i+1}</span><strong>{name}</strong><small className={r?.status==="PASS"?"pass":""}>{r?.status||"PENDING"}</small></div>;})}</div>
   </div>
   <div className="panel job-panel"><div className="panel-heading"><div><span className="kicker">04 / RUN STATUS</span><h2>Execution summary</h2></div></div>
    {!job?<div className="empty-state">Your queued job and execution summary appear here.</div>:<>
     <div className="job-state"><span className={"state-pill "+job.status}>{job.status}</span><span>{job.payload?.extraction_scope||"Scope pending"}</span></div>
     <div className="job-id">RUN ID <code>{job.job_id}</code></div><div className="job-message">{job.payload?.message||"Waiting for worker."}</div>
     {job.status==="failed"&&<div className="error-box">{job.payload?.message}</div>}
     {job.status==="completed"&&<div className="download-list">{downloads.map(file=><a key={file} href={API+"/api/jobs/"+job.job_id+"/files/"+encodeURIComponent(file)+(API_KEY?"?":"")} target="_blank" rel="noreferrer" onClick={e=>{if(API_KEY){e.preventDefault();void apiFetch("/api/jobs/"+job.job_id+"/files/"+encodeURIComponent(file)).then(r=>r.blob()).then(blob=>{const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=file;a.click();URL.revokeObjectURL(url);});}}}>{file}<span>↓</span></a>)}</div>}
     {job.status==="completed"&&<div className="review-block"><h3>Human review decision</h3><form onSubmit={submitReview}><input value={reviewId} onChange={e=>setReviewId(e.target.value)} placeholder="Queue ID / candidate ID" required/><select value={reviewDecision} onChange={e=>setReviewDecision(e.target.value)}><option value="approve">Approve</option><option value="reject">Reject</option><option value="edit">Mark for edit / revalidation</option></select><textarea value={reviewNote} onChange={e=>setReviewNote(e.target.value)} placeholder="Reviewer note (optional)"/><button className="secondary-button" type="submit">Record review decision</button></form>{reviewMessage&&<p className="fineprint">{reviewMessage}</p>}</div>}
    </>}
   </div>
  </section>
  <footer><span>CDCI ENGINEERING INTELLIGENCE</span><span>DURABLE QUEUE · LANGGRAPH · SIX PAIRED EVALS · HUMAN REVIEW</span></footer>
 </main>;
}
