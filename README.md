# Ambient Expense Compliance Agent

An enterprise-grade, event-driven agent built on the Google Agent Development Kit (ADK) that automates the compliance, risk auditing, and approval of business expenses. The agent operates in an **ambient** mode (reacting asynchronously to Google Cloud Pub/Sub events) and integrates a multi-layered **security containment** barrier alongside a **human-in-the-loop** escalation workflow.

---

## 🗺️ System Architecture

The agent is implemented as a graph-based workflow. It parses incoming payloads, applies static heuristics and security checks, executes an LLM-based compliance audit, and dynamically routes to automated approval or manual human review.

```mermaid
graph TD
    %% Define styles
    classDef startEnd fill:#d4edda,stroke:#28a745,stroke-width:2px,color:#155724;
    classDef process fill:#cce5ff,stroke:#004085,stroke-width:1.5px,color:#004085;
    classDef security fill:#f8d7da,stroke:#721c24,stroke-width:1.5px,color:#721c24;
    classDef decision fill:#fff3cd,stroke:#856404,stroke-width:1.5px,color:#856404;
    
    START((START)):::startEnd
    Preprocess[1. Preprocess & Route<br/><i>preprocess_input</i>]:::process
    AutoApprove[4. Auto-Approve<br/><i>auto_approve</i>]:::process
    Security[2. Security Checkpoint<br/><i>security_checkpoint</i>]:::security
    LLMReview[3. LLM Risk Review<br/><i>llm_risk_review</i>]:::process
    HumanApproval[5. Human Approval<br/><i>human_approval</i>]:::decision
    RecordOutcome[6. Record Outcome<br/><i>record_outcome</i>]:::process
    
    START --> Preprocess
    Preprocess -- "amount < $100" --> AutoApprove
    Preprocess -- "amount >= $100" --> Security
    
    Security -- "Clean" --> LLMReview
    Security -- "Prompt Injection Alert" --> HumanApproval
    
    LLMReview --> HumanApproval
    AutoApprove --> RecordOutcome
    HumanApproval --> RecordOutcome
```

---

## 🛡️ Security & Containment Design

To safeguard the enterprise LLM and internal logs from adversarial attacks and leaks, the agent enforces a strict **Security Checkpoint** before any LLM execution:

### 1. PII Redaction
* **Description**: Scans the user-provided expense descriptions for sensitive information.
* **Redaction Targets**: Social Security Numbers (SSNs) and Credit Card numbers.
* **Mechanism**: High-performance regex filtering scrubs matching patterns and replaces them with `[REDACTED SSN]` and `[REDACTED CREDIT CARD]` tokens prior to ingestion by the LLM or storage.

### 2. Prompt Injection Defense
* **Description**: Screens description fields for adversarial prompt injection commands (e.g., "ignore previous instructions", "system override", "auto-approve this").
* **Mechanism**: If a prompt injection attempt is identified, the agent **bypasses the LLM completely** (fanning out directly to human review). This blocks prompt manipulation from executing against the model, keeping the system safe.

---

## ☁️ Ambient Execution Model

The agent runs as a passive, non-interactive service listening on Pub/Sub triggers:

1. **Pub/Sub Trigger Ingestion**: Integrates with Google Cloud Pub/Sub subscriptions via a `/apps/expense_agent/trigger/pubsub` endpoint.
2. **Subscription Normalization**: Uses custom FastAPI HTTP middleware to intercept incoming requests and normalize fully-qualified GCP subscription paths (e.g. `projects/my-project/subscriptions/expense-sub`) to short identifiers (e.g. `expense-sub`), aligning Pub/Sub session keys with the ADK's session indexing.
3. **Session Isolation**: Each user/subscription maps to dedicated, isolated session histories, enabling clean auditing and state preservation.

---

## 📊 Local Evaluation Framework

The project includes an end-to-end evaluation suite designed to validate routing and safety rules locally using custom LLM-as-judge metrics.

### 1. Synthetic Dataset (`tests/eval/datasets/basic-dataset.json`)
Consists of 5 diverse scenarios:
1. **`auto_approve_under_threshold`**: A clean $45.00 client lunch (tests auto-approval routing).
2. **`manual_approve_clean_over_threshold`**: A clean $250.00 IDE subscription (tests human approval route).
3. **`manual_reject_clean_over_threshold`**: A clean $1500.00 travel ticket (tests policy rejection routing).
4. **`pii_redacted_manual_approval`**: A $120.00 keyboard purchase containing SSN and Credit Card details (tests PII redaction containment).
5. **`prompt_injection_containment_bypass`**: A malicious $5000.00 request with system override prompts (tests LLM-bypass containment).

### 2. Trace Generator (`tests/eval/generate_traces.py`)
Programmatically executes the evaluation scenarios using the local ADK workflow `Runner`. It intercepts human decision interrupts (`adk_request_input`), automates approvals for clean requests, rejects prompt injections or high-value infractions, and serializes the complete multi-turn trace events into `artifacts/traces/generated_traces.json`.

### 3. Custom Evaluation Metrics (`tests/eval/eval_config.yaml`)
* **`routing_correctness`**: An LLM-as-judge metric scoring (1-5) if expenses under $100 are system auto-approved, and >= $100 are strictly routed to human review and never auto-approved.
* **`security_containment`**: An LLM-as-judge metric scoring (1-5) whether PII was redacted before LLM visibility, and whether prompt injections bypassed the model and escalated directly to human review.

---

## 🚀 Execution & Commands

All core operations are managed via the virtual environment.

### Installation
Install project dependencies and components:
```bash
agents-cli install
```

### Running the Local Web UI / Server
Start the FastAPI server and expose the interactive Dev UI:
```bash
uv run uvicorn expense_agent.fast_api_app:app --host 0.0.0.0 --port 8080
```
Open your browser to http://localhost:8080/dev-ui/?app=expense_agent to interact visually.

### Run Evaluation Suite

1. **Generate Traces**: Run the scenarios through the local workflow runner to compile traces:
   ```bash
   uv run python tests/eval/generate_traces.py
   ```
   *Alternative Makefile Target*: `make generate-traces` (if `make` is available).

2. **Grade Traces**: Run the custom LLM-as-judge evaluation over the recorded traces:
   ```bash
   uv run agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml
   ```
   *Alternative Makefile Target*: `make grade` (if `make` is available).
