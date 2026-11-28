# Orchestration and operations

The branched LangGraph DAG retains the 12 logical extraction stages and six paired deterministic quality gates.

## Reliability
1. API persists input files and job payload before responding.
2. A separate worker claims a SQLite queue record atomically with a lease.
3. LangGraph checkpoints workflow state by job ID.
4. Job-level retries are bounded; expired worker leases can be reclaimed.
5. Provider keys remain in worker environment, never graph state/queue payload.
6. Idempotency keys protect clients retrying upload requests.

## Evaluations
Six paired gates validate required files, JSON structures, candidate geometry, association structure, hierarchy and review-queue contracts. These are smoke/contract evals with zero LLM judge calls. Precision/recall and semantic tag accuracy require labelled drawings and ground truth.

## Observability
Durable DB events, JSONL logs and stage stdout/stderr; replayable SSE events; stage duration/retry tracking; Prometheus-compatible job and duration metrics. Token/cost metrics remain unknown until provider usage metadata is instrumented.

## Human review
Approve/reject/edit decisions record the decision, reviewer, note and timestamp in the actual step8 queue item and emit an audit event. Human decisions do not overwrite model-extracted values.

## Production boundary
This local SQLite queue is single-host/single-worker. Multi-host use requires PostgreSQL job/checkpoint storage, Redis/SQS, object storage, OIDC and tenant isolation, signed downloads, secret manager, distributed quota controls, OpenTelemetry, backup/recovery, data retention and dead-letter handling.
