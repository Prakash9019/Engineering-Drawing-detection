# System Design Document: CDCI Tag Extraction Application

## 1. Overview
The CDCI P&ID Tag Extraction Application processes engineering diagrams to automatically extract tag information and hierarchies. Originally designed as a local sequence of Python scripts, this system design scales the application into a web service capable of handling concurrent, high-volume workloads (e.g., uploading 500 files at once).

## 2. Architecture Components

### Frontend
- **Framework**: React / Next.js
- **Responsibilities**:
  - File uploading interface (drag and drop, progress bars for multiple files).
  - Visualization of results (interactive hierarchies, bounding box overlays).
  - Human-in-the-loop review queues (tag review and cloud verification UI).

### Backend / API
- **Framework**: FastAPI (Python)
- **Responsibilities**:
  - Receive files and handle authentication.
  - Store files securely in a blob store (e.g., AWS S3).
  - Queue tasks into a message broker (e.g., Redis / RabbitMQ / AWS SQS).
  - Expose API endpoints for frontend polling (WebSockets/SSE) to update processing status.

### Processing Workers & Orchestration
- **Framework**: Celery (or LangGraph / Temporal for complex DAG workflows)
- **Responsibilities**:
  - Consume messages from the queue and run pipeline stages independently.
  - Perform API calls to Gemini and local Tesseract OCR operations.

### Data Storage
- **File Storage**: Object storage (AWS S3, Google Cloud Storage) for drawings, crops, and JSON context files.
- **Database**: PostgreSQL for storing metadata (file status, parsed tags, review queue items, final deliverables).

## 3. Handling 500 File Uploads (Scalability & Fault Tolerance)

### What happens if 500 files are uploaded at once?
1. **API Gateway & Backend**: The FastAPI backend intercepts the 500 files. Instead of processing them synchronously, it uploads the files directly to Object Storage and inserts a "Pending" record into the database.
2. **Message Queue**: For each uploaded file, a job is pushed onto a message queue (e.g., RabbitMQ, Celery Redis Queue).
3. **Worker Pool**: A pool of autoscaling worker nodes (e.g., Kubernetes pods running the Python pipeline) pulls jobs from the queue. If there are 10 workers, 10 files are processed in parallel. The rest wait in the queue.
4. **Rate Limiting (External APIs)**: The pipeline relies heavily on the Gemini API. A crucial failure point is hitting the API rate limit.
   - *Solution*: Workers must implement exponential backoff and retry logic. Alternatively, a centralized token bucket system should regulate calls to Gemini to ensure we never exceed quotas.
5. **State Management**: Using LangGraph or Temporal ensures that if a worker crashes halfway through a drawing (e.g., during Step 5A patches), the job can be restarted from the exact failure point without re-running Step 1-4.

### What breaks and how we mitigate it?
- **Memory/Disk Overload on Single Server**: Processing 500 high-res P&IDs locally will crash due to out-of-memory errors or disk space exhaustion. *Fix*: Decouple processing into stateless scalable workers, utilizing remote blob storage.
- **API Rate Limits (Gemini HTTP 429)**: Flooding Gemini with SAHI patches for 500 files simultaneously will immediately result in rate limit blocks. *Fix*: Implement strict concurrency limits for API calls across the worker cluster.
- **Tesseract Bottlenecks**: Heavy local OCR processing per patch will consume high CPU. *Fix*: Scale out the number of worker nodes dynamically based on queue depth.
- **Web Timeout**: Keeping HTTP connections open for the duration of a 10-minute extraction will result in timeouts. *Fix*: The upload API should return an immediate `202 Accepted` with a job ID, allowing the frontend to poll for progress or subscribe via WebSockets.

## 4. Pipeline Orchestration (LangGraph vs. Agno)
For converting the sequential Python scripts into a robust backend workflow:
- **LangGraph is highly recommended.** The pipeline is essentially a Directed Acyclic Graph (DAG) passing a shared state (`drawing_context.json`). LangGraph natively supports explicit state transitions, conditional branching (like deciding between Full Drawing vs. Cloud Scoped based on step2), and pausing for human-in-the-loop (e.g., Step 2c human cloud review).
- **Agno (Phidata)** is better suited for autonomous AI assistants making free-form tool choices. Our P&ID pipeline requires a rigid, deterministic, high-throughput data processing flow, not an open-ended assistant.