"""Six paired deterministic workflow contract/quality eval gates."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

PAIRED_EVALS={
 "eval_01_format_title":["drawing_context.json","title_block_context.json"],
 "eval_02_cloud_notes":["outer_clouds_v2.json","notes_context.json"],
 "eval_03_scope_tables":["sow_symbol_memory.json","tables_context.json"],
 "eval_04_extraction_geometry":["step5a_candidates_full.json","step5b_associations_full.json"],
 "eval_05_hierarchy_validation":["step5b2_hierarchy_full.json","step5c_validated.json","step5d_deduped.json"],
 "eval_06_normalization_routing":["step7_cedm_output.json","final_tags.xlsx","human_review_queue.json"]
}
def _json(path:Path)->Any:
 return json.loads(path.read_text(encoding="utf-8"))
def _rows(data:Any,*keys):
 if isinstance(data,list):return data
 if isinstance(data,dict):
  for key in keys:
   if isinstance(data.get(key),list):return data[key]
 return []
def _valid_box(box:Any)->bool:
 try:
  if isinstance(box,dict): x1,y1,x2,y2=(float(box[k]) for k in ("x1","y1","x2","y2"))
  elif isinstance(box,(list,tuple)) and len(box)==4:x1,y1,x2,y2=map(float,box)
  else:return False
  return x2>x1 and y2>y1
 except (KeyError,TypeError,ValueError):return False
def evaluate_pair(eval_id:str,output_dir:Path)->dict[str,Any]:
 if eval_id not in PAIRED_EVALS:raise ValueError(f"Unknown eval gate: {eval_id}")
 expected=PAIRED_EVALS[eval_id]
 missing=[name for name in expected if not (output_dir/name).is_file()]
 invalid=[]; metrics={}
 parsed={}
 for name in expected:
  p=output_dir/name
  if name in missing or p.suffix.lower()!=".json":continue
  try:
   data=_json(p); parsed[name]=data
   if not isinstance(data,(dict,list)):invalid.append(f"{name}: expected object or array")
  except (OSError,json.JSONDecodeError) as exc:invalid.append(f"{name}: {exc}")
 if eval_id=="eval_01_format_title" and "drawing_context.json" in parsed:
  ctx=parsed["drawing_context.json"]
  required=("drawing_number","revision_code")
  absent=[key for key in required if not ctx.get(key)]
  metrics["context_required_fields"]={"missing":absent}
  if absent:invalid.append("drawing_context.json missing drawing identity/revision fields: "+",".join(absent))
 if eval_id=="eval_02_cloud_notes":
  clouds=parsed.get("outer_clouds_v2.json",{})
  regions=_rows(clouds,"outer_clouds","clouds","regions")
  metrics["cloud_regions"]={"count":len(regions)}
 if eval_id=="eval_03_scope_tables":
  scope=parsed.get("sow_symbol_memory.json",{})
  metrics["scope_memory"]={"keys":len(scope) if isinstance(scope,dict) else 0}
 if eval_id=="eval_04_extraction_geometry":
  cand=parsed.get("step5a_candidates_full.json",{})
  rows=_rows(cand,"candidates","results","records")
  ids=[str(x.get("candidate_id","")) for x in rows if isinstance(x,dict)]
  missing_ids=sum(not x for x in ids)
  duplicate_ids=len(ids)-len(set(ids))
  boxes=[x.get("symbol_bbox") or x.get("bbox") for x in rows if isinstance(x,dict)]
  invalid_boxes=sum(not _valid_box(b) for b in boxes if b is not None)
  metrics["candidate_quality"]={"count":len(rows),"missing_ids":missing_ids,
                                "duplicate_ids":duplicate_ids,"invalid_bounding_boxes":invalid_boxes}
  if missing_ids or duplicate_ids or invalid_boxes:
   invalid.append("candidate ID/bbox integrity check failed")
 if eval_id=="eval_05_hierarchy_validation":
  hierarchy=parsed.get("step5b2_hierarchy_full.json",{})
  associations=_rows(hierarchy,"associations","nodes","records")
  metrics["hierarchy"]={"keys":len(hierarchy) if isinstance(hierarchy,dict) else 0,
                        "association_count":len(associations)}
  validated=parsed.get("step5c_validated.json",{})
  dedup=parsed.get("step5d_deduped.json",{})
  metrics["validation_dedup"]={"validated_count":validated.get("total_validated"),
                               "primary_count":dedup.get("primary_count"),
                               "discarded_count":dedup.get("discarded_count")}
 if eval_id=="eval_06_normalization_routing":
  review=parsed.get("human_review_queue.json",{})
  items=_rows(review,"items")
  ids=[str(x.get("queue_id","")) for x in items if isinstance(x,dict)]
  duplicate_ids=len(ids)-len(set(ids))
  missing_ids=sum(not x for x in ids)
  metrics["review_queue"]={"count":len(items),"missing_queue_ids":missing_ids,
                           "duplicate_queue_ids":duplicate_ids}
  if missing_ids or duplicate_ids:invalid.append("review queue IDs must be present and unique")
 status="PASS" if not missing and not invalid else "FAIL"
 return {"eval_id":eval_id,"status":status,"expected_artifacts":expected,
         "missing_artifacts":missing,"invalid_artifacts":invalid,
         "metrics":metrics,"model_calls":0}
