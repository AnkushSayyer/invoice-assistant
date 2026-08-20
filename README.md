# InvoiceOps AI

**Turn messy invoice & receipt PDFs into verified reimbursement decisions — with the structured data and audit trail behind every call.**

> Problem 3 (messy documents → structured, queryable data), scoped around a real user:
> an expense/AP reviewer who needs to decide *whether to reimburse a PDF and for how much*.
> Extraction alone isn't the hard part — **trusting the numbers** is. See
> [`decisions.md`](./decisions.md) for the full reasoning.

**Live demo:** `<LIVE_URL>/ui/`  ·  **API docs:** `<LIVE_URL>/docs`
_(The deployment is gated by HTTP Basic Auth — credentials are provided with the submission.)_

---

## What it does

Upload a PDF invoice/receipt and a claimed amount. The system:

1. **Perceives** the document — Google Document AI Invoice Parser if configured, otherwise
   PyMuPDF layout text → LLM (via Instructor) into a typed `InvoiceExtraction`.
2. **Reconciles the math deterministically** — recomputes
   `subtotal + tax + fees + tip − discounts` and checks it against the stated total *and*
   the claimed amount. This — not the LLM's confidence — is the trust engine.
3. **Self-corrects** when the math doesn't add up — searches plausible total formulas and
   recovers missing anchored charges before giving up.
4. **Applies guardrails** — duplicate detection, expense policy, confidence, auto-approve limit.
5. **Decides**: `approve · reject · escalate · clarify`, and **persists an audit trail**.
6. **Learns** from human corrections — keyed on vendor identity (GSTIN/PAN/email domain),
   so the next invoice from that vendor extracts and reconciles automatically.

There are two pipelines over one database and one math core:

- **Autonomous agent** (`/agent/*`) — the primary path, with a human-in-the-loop review
  and clarification queue.
- **Manual template pipeline** (`/upload`, `/review`, `/approve`) — a high-precision,
  human-curated fingerprint/template flow using Postgres `pg_trgm`.

Architecture diagrams and the decision loop: [`docs/architecture.md`](./docs/architecture.md).

---

## Quickstart (one command)

**Prereqs:** Docker + Docker Compose, and an LLM API key (Gemini by default; OpenAI or
Anthropic also work).

```bash
git clone <YOUR_REPO_URL> invoice-assistant
cd invoice-assistant

cp .env.example .env
# edit .env and set at least ONE provider key, e.g. GEMINI_API_KEY=...

docker compose up --build
```

Then open:

- **UI:** http://localhost:8000/ui/
- **API docs:** http://localhost:8000/docs
- **Health:** http://localhost:8000/health

Compose starts Postgres 16 (with `pg_trgm` enabled via `docker/postgres/init.sql`) and the
API. The API container listens on `8080`; Compose publishes it on host **8000**.

### Run without Docker

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Point DATABASE_URL at a running Postgres with the pg_trgm extension, then:
uvicorn app.main:app --reload
# -> http://localhost:8000
```

---

## Try it in 30 seconds

1. Open the UI and go to **Process invoice**.
2. Drop an invoice PDF, enter the **claimed amount**, optionally a category, and submit.
3. Watch the decision (`approve/reject/escalate/clarify`) and the reasoning.
4. If it **escalates** or asks a **clarification**, answer it in the queue — the system
   learns a vendor rule and re-runs, so the same question isn't asked twice.
5. Check **Audit trail** to see every decision, the winning formula, and the full step trace.

Or via the API:

```bash
curl -u "$USER:$PASS" -F "file=@invoice.pdf" -F "claimed_amount=1234.50" \
  "$BASE_URL/agent/process"
```

---

## Configuration

Copy `.env.example` → `.env`. Key variables:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres DSN (Compose sets this automatically for the container) |
| `LLM_PROVIDER` | `openai` \| `anthropic` \| `gemini` (default `gemini`) |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Provider key — set at least one |
| `BASIC_AUTH_USERNAME` / `BASIC_AUTH_PASSWORD` | When **both** set, gate the whole app; unset = open (local/tests) |
| `DOCAI_PROJECT_ID` / `DOCAI_LOCATION` / `DOCAI_PROCESSOR_ID` + `GOOGLE_APPLICATION_CREDENTIALS` | Optional Google Document AI; falls back to PyMuPDF+LLM if unset or on failure |
| `EXPENSE_MAX_AMOUNT`, `EXPENSE_AUTO_APPROVE_LIMIT`, `EXPENSE_ALLOWED_CATEGORIES`, `EXPENSE_MAX_TIP_RATIO`, `EXPENSE_REQUIRE_RECONCILIATION` | Agent expense policy (all optional) |

No real secrets are committed — `.env` and service-account JSONs are git-ignored.

---

## API surface

**Agent (`/agent`)**

| Method | Path | Description |
|---|---|---|
| `POST` | `/agent/process` | Run the full agent loop on a PDF + claimed amount |
| `GET`  | `/agent/queue` | Escalated / rejected review queue |
| `POST` | `/agent/resolve` | Human approve/reject + field corrections (learns) |
| `POST` | `/agent/resolve/preview` | Dry-run a resolution without persisting |
| `GET`  | `/agent/clarifications` | Documents awaiting a targeted answer |
| `POST` | `/agent/clarify` | Answer a clarification → learn rule → re-run |
| `GET`  | `/agent/runs` · `/agent/runs/{document_id}` | Decision audit trail |
| `GET`  | `/agent/training-data` | Export verified (PDF, fields) pairs |

**Manual template pipeline**

| Method | Path | Description |
|---|---|---|
| `POST` | `/upload` | Upload a PDF (+ claimed amount) through the template flow |
| `GET`  | `/review` | List pending documents |
| `POST` | `/approve` | Approve → create/update template + example |
| `GET`  | `/documents/{document_id}/pdf` | Fetch stored PDF |

Full interactive docs at `/docs`.

---

## Testing

```bash
pytest          # from the repo root
```

Tests cover the parts most likely to break in the real world, not token coverage:

- **Math validation** edge cases — discounts, GST, tax-inclusive lines, multi-invoice tolerance (`test_validation.py`)
- **Fingerprint/masking** stability and vendor-key extraction (`test_fingerprint.py`)
- **Template matching** — anchored vs fuzzy `pg_trgm` (`test_template_matcher.py`)
- **Extraction** — PDF layout, bundled-invoice split/combine, LLM timeouts (`test_extractor.py`, `test_llm.py`)
- **Agent** decisions, resolve/learn, clarify E2E, missing-charge recovery
  (`test_agent*.py`, `test_clarification.py`, `test_missing_charge_recovery.py`)
- **APIs** for both pipelines (`test_agent_api.py`, `test_documents_api.py`)

---

## Deployment

Deployed on **Google Cloud Run + Cloud SQL (Postgres 16)** via
[`deploy/gcp-cloudrun.sh`](./deploy/gcp-cloudrun.sh), which provisions the SQL instance,
wires `DATABASE_URL`/keys as secrets, and deploys with `gcloud run deploy --source .`.

> Note: the agent path does not require `pg_trgm`. If you use the manual template pipeline
> on managed Postgres, run `CREATE EXTENSION IF NOT EXISTS pg_trgm;` once (Compose does this
> automatically locally).

---

## Tech stack

Python 3.11 · FastAPI · SQLAlchemy 2.0 / Pydantic v2 · PostgreSQL (`pg_trgm`) · PyMuPDF ·
Instructor + Gemini/OpenAI/Anthropic · optional Google Document AI · Docker Compose ·
vanilla static SPA.

---

## Project layout

```
app/
  api/          # FastAPI routes only (documents.py, agent.py)
  services/     # business logic: extraction, validation, agent loop, learning, fingerprinting
  schemas/      # Pydantic models (API + Instructor extraction + validation rules)
  db/           # SQLAlchemy models, engine/session
  static/       # dark SPA served at /ui
  main.py       # app wiring, auth, static mount
tests/          # pytest suite (one per service + API)
docs/           # architecture.md (diagrams)
deploy/         # gcp-cloudrun.sh
docker/         # postgres init.sql (enables pg_trgm)
decisions.md    # the decisions + tradeoffs log
```
