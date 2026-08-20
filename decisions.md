# Decisions Log — InvoiceOps AI

A running log of the real calls made while building this, and the tradeoffs I accepted.
It's written to be honest about what I chose, what I rejected, and what I deliberately
cut for a time-boxed build.

---

## 0. Framing the problem: "structured data" is not the goal — a trusted decision is

**Decision.** I picked Problem 3 (messy documents → structured, queryable data) but
scoped it around a concrete user and a concrete job: an **expense/AP reviewer** who
receives invoice and receipt PDFs and has to decide *whether to reimburse them and for
how much*. So the product isn't "an invoice-to-JSON API" — it's a system that turns a
messy PDF into a **verified reimbursement decision** (`approve | reject | escalate |
clarify`) plus the structured fields and an audit trail behind it.

**Alternatives considered.**
- A pure extraction service: PDF in, clean JSON out, plus a search endpoint. This is the
  literal reading of the prompt and the easiest to build.
- A generic "document" platform that handles any doc type.

**Reasoning.** Extraction alone is the part LLMs already do reasonably well, so it's the
part *least* likely to surprise anyone. The genuinely hard, valuable part of this problem
is trusting the output: an invoice that extracts perfectly but whose numbers don't add up
is worthless, and the failure is silent. Anchoring on a decision forced me to build the
verification, correction, and human-in-the-loop machinery that makes extracted data
actually usable. It also gives "queryable" a purpose — you query `documents` +
`agent_runs` to answer "what did we approve, why, and on whose authority."

**What I cut.** Multi-document-type ambition. I went deep on invoices/receipts rather
than shallow on contracts + resumes + invoices. Depth over breadth was a deliberate call
given the rubric.

---

## 1. Deterministic math validation is the confidence engine — not the LLM's self-reported confidence

**Decision.** The core of trust is `app/services/validation.py`: a deterministic
reconciliation that recomputes `subtotal + tax + fees + tip − discounts` and checks it
against the stated `total` and the user's `claimed_amount`, within a tolerance
(default `0.01`). An invoice is only auto-approvable if the arithmetic *actually
reconciles*.

**Alternatives considered.**
- Trust the LLM's confidence score / "looks right" judgment.
- Ask a second LLM to verify the first (LLM-as-judge).

**Reasoning.** LLM confidence is uncalibrated and correlates poorly with arithmetic
correctness — a model will confidently return numbers that don't sum. Money math is
exactly the thing you should *never* delegate to a probabilistic system when a
deterministic check is trivial and free. So the LLM does perception (turn pixels/text
into candidate fields) and deterministic code does the judgment. This split is the single
most important design decision in the repo.

**Tradeoff accepted.** Real invoices don't all follow one formula (tax-inclusive line
items, discounts applied pre- vs post-tax, rounding, bundled charges). A naive single
formula would reject a lot of legitimate invoices. That pushed me into decisions #2 and #3.

---

## 2. Handling the messy real world: remediation + missing-charge recovery instead of failing

**Decision.** When the primary formula doesn't reconcile, the agent doesn't give up. In
`app/services/agent_tools.py` it **remediates** — searches a small space of plausible
total formulas (with/without tax on line items, subtract discounts or not, include
fees/tip) — and can **recover a missing charge** by looking for an anchored amount in the
raw text (`recover_missing_charge` / `find_anchored_amount`) when the gap matches a known
line like a delivery fee or service charge. Only if nothing reconciles does it escalate.

**Alternatives considered.**
- One canonical formula; anything else → escalate to a human.
- Ask the LLM to "fix" the numbers.

**Reasoning.** This is the edge-case pile most people route around, and it's where the
real value is. A rigid formula turns the agent into a rejection machine and destroys trust
faster than being wrong. Searching a bounded set of *deterministic* formulas keeps the
correction explainable (we record which formula won in `agent_runs`) and avoids letting
the LLM hallucinate numbers into balance.

**Tradeoff accepted.** The formula search is heuristic and bounded — it won't reconcile
truly exotic invoices, and it could in principle pick a formula that coincidentally
balances. I mitigate with tight tolerance and by logging the chosen formula so a human can
audit it. Encoding this as typed, per-vendor rules (#4) is the durable fix.

---

## 3. Per-invoice-type validation presets instead of hardcoding one region's tax model

**Decision.** `app/schemas/validation_rules.py` defines a small `TemplateValidationRules`
model and presets: default, B2B, food-delivery (with/without discounts), Uber-style ride,
and GST tax invoices. Rules can also be **derived** from an approved example and stored per
template/vendor.

**Alternatives considered.** Hardcode one tax model (e.g. US sales tax) into `validation.py`.

**Reasoning.** "Line amount includes tax," "subtract discounts before total," and "validate
line items at all" genuinely differ across vendor types — a food-delivery receipt and a GST
B2B invoice reconcile differently. Making the rules data (presets + learned per-vendor
rules) rather than code lets the system adapt without a redeploy and lets the learning loop
(#5) improve accuracy over time.

**What I cut.** A full rules DSL / UI for editing rules. Presets + derived rules cover the
common cases; a rule editor is future work.

---

## 4. Learning is keyed on vendor identity (GSTIN/PAN/email domain), not on the document fingerprint

**Decision.** The agent's memory (`app/services/knowledge.py`, `VendorProfile` /
`VendorRule` / `VendorFewShot`) is keyed on a stable **vendor key** extracted from the
document — email domain → URL host → `PAN` → `GSTIN` (`fingerprint.extract_vendor_key`).
When a human corrects an escalated invoice, we learn a typed rule + a worked few-shot for
*that vendor*, so the next invoice from them extracts and reconciles automatically.

**Alternatives considered.**
- Key learning on the layout fingerprint (what the legacy template pipeline does).
- Global few-shots shared across all vendors.

**Reasoning.** Layout fingerprints drift — a vendor tweaks their invoice template and the
fingerprint changes, orphaning everything you learned. Vendor identity is far more stable
across template revisions. Per-vendor scoping also keeps few-shots relevant (a food app's
quirks shouldn't leak into a SaaS invoice) and keeps the prompt small.

**Tradeoff accepted.** Vendor-key extraction is heuristic and can miss (no domain, no
GSTIN). When it can't key a vendor, we fall back to generic extraction and don't persist a
lesson — correctness over false learning.

---

## 5. Human-in-the-loop with a clarification protocol, not a dead-letter queue

**Decision.** Escalations aren't a dead end. There are three human touchpoints:
`/agent/queue` (review escalated/rejected), `/agent/resolve` (approve/reject + correct
fields, which *learns*), and a **clarification** flow (`/agent/clarifications`,
`/agent/clarify`) where the agent asks a targeted question (e.g. "is this line
tax-inclusive?"), and the answer becomes a durable vendor rule that re-runs the document.
There's also `/agent/resolve/preview` for a dry-run before committing.

**Alternatives considered.** Escalate to a human and stop; make the human re-key
everything by hand every time.

**Reasoning.** A reviewer's time is the scarce resource. The valuable move is to make each
human correction *teach the system* so the same question is never asked twice for that
vendor. `detect_ambiguity` narrows the question to the specific unknown rather than
dumping the whole document on the human. Preview exists because I wouldn't trust a system
that mutates state on a guess without letting me see the outcome first.

---

## 6. Postgres + `pg_trgm` over a vector database

**Decision.** One datastore: Postgres. Structured fields live in a JSONB column; template
matching uses the `pg_trgm` extension via `func.similarity` on a masked layout fingerprint
(`app/services/template_matcher.py`).

**Alternatives considered.** A vector DB (pgvector, Pinecone, etc.) with embedding-based
similarity for both templates and search.

**Reasoning.** The query patterns here are relational and exact — "documents pending
review," "runs for this document," "rules for this vendor," "did we already see this
invoice." That's SQL, not nearest-neighbor. Template similarity is a *structural* match
(does this layout look like one we've approved), which trigram similarity on a
PII-masked fingerprint captures well and cheaply. Running a second datastore for a 5-day
build — with its own ops, consistency, and failure modes — wasn't justified by the query
patterns. If semantic free-text search over invoice contents became a real requirement,
`pgvector` inside the same Postgres would be the first move, not a separate service.

**Tradeoff accepted.** No semantic search over document *content* yet. Trigram matching is
lexical, so heavily reworded layouts match worse — acceptable because vendor-key anchoring
(#4) carries most of the load in the agent path.

---

## 7. Masking is for fingerprints, and I was honest about its scope

**Decision.** `app/services/fingerprint.py` masks emails, phones, dates, amounts, names,
addresses, line items, GSTIN/PAN/CIN, invoice numbers, etc. into tokens (`<EMAIL>`,
`<AMOUNT>`, …). This produces a **stable layout signature** that's robust to per-invoice
values, and it's what we store as `masked_text`.

**Reasoning.** Two invoices from the same vendor differ only in the variable fields;
masking those out yields a signature that trigram-matches reliably. This is what makes
template matching work.

**Explicit caveat (stated so it isn't mistaken for more than it is).** This masking is a
*fingerprinting* mechanism, not a privacy guarantee. The LLM extraction path still sees
raw layout text, because you need the real values to extract them. If PII-minimized LLM
calls were a hard requirement, that would be a separate redaction layer — I scoped it out
and I'm flagging it rather than pretending the fingerprint masking covers it.

---

## 8. Optional Google Document AI with a graceful LLM fallback

**Decision.** If `DOCAI_PROJECT_ID` / `DOCAI_LOCATION` / `DOCAI_PROCESSOR_ID` are set, the
agent perceives via Document AI's Invoice Parser (with real per-field confidence and
bundled-invoice splitting). If not configured — or if it fails — it falls back to PyMuPDF
layout text → LLM. Configuration is checked at runtime (`is_document_ai_configured`).

**Alternatives considered.** Require Document AI (better accuracy, hard dependency) or
never use it (simpler, weaker OCR).

**Reasoning.** Document AI is meaningfully better on scanned/photographed invoices and
gives calibrated confidence, but I didn't want the whole system to be unrunnable for a
reviewer who just has an LLM key. Making it optional means the app degrades gracefully:
best-effort perception when the good backend is available, still-functional when it isn't.

**Tradeoff accepted.** Two perception code paths to test. Covered in `tests/`
(`test_document_ai.py`, `test_agent_tools.py`) including the fallback.

---

## 9. Structured output via Instructor + a multi-provider abstraction

**Decision.** Extraction uses **Instructor** with `response_model=InvoiceExtraction`
(a Pydantic v2 model) so the LLM returns validated, typed data — no hand-rolled JSON
parsing. `app/services/llm.py` abstracts the provider (`openai | anthropic | gemini`) so
the key you have determines the backend.

**Alternatives considered.** Prompt-and-parse raw JSON with try/except; lock to a single
provider.

**Reasoning.** Instructor gives schema validation and automatic retries on malformed
output, which removes a whole class of brittle parsing bugs and is exactly the reliability
you want at the extraction boundary. The provider abstraction is cheap insurance against
rate limits/outages and makes the reviewer's setup easier (bring whatever key you have).

**Tradeoff accepted.** A thin abstraction over three SDKs to maintain. Error mapping
(timeouts → proper HTTP status) is centralized and tested (`test_llm.py`).

---

## 10. Kept the legacy template pipeline instead of deleting it

**Decision.** The original fingerprint/template flow (`/upload`, `/review`, `/approve`)
lives alongside the agent. The agent is the primary UI path; the template pipeline is
intact and shares the same math core.

**Alternatives considered.** Rip out the older pipeline once the agent existed.

**Reasoning.** They solve slightly different jobs — the template flow is a
human-curated, high-precision path (approve → template + example), while the agent is
autonomous-first. Keeping both was additive and low-risk, and it demonstrates two valid
approaches to the same problem rather than throwing away working, tested code.

**Tradeoff accepted.** More surface area and a small conceptual overlap. Justified by the
fact that both are covered by tests and share `validation.py`.

---

## 11. Shared HTTP Basic Auth for the public deployment

**Decision.** When `BASIC_AUTH_USERNAME` **and** `BASIC_AUTH_PASSWORD` are set, the whole
app (UI + API) is gated by Basic Auth with a timing-safe compare; `/health` is exempt.
Unset → open, for local dev and tests.

**Alternatives considered.** Full user accounts + sessions/JWT + RBAC; no auth at all on
the public URL.

**Reasoning.** The deployed URL is a demo, not a multi-tenant product. A shared credential
is enough to keep it from being a public open endpoint, costs almost nothing to build, and
doesn't get in the way of local development. Real auth would be significant scope for zero
added insight into the actual problem.

**What I cut.** Multi-tenant identity, roles, per-user audit attribution. `agent_runs`
records the decision trail but not *which* human resolved it — a clear next step, cut for time.

---

## 12. Schema via `create_all`, not migrations

**Decision.** Tables are created at startup with `Base.metadata.create_all`.

**Alternatives considered.** Alembic migrations from day one.

**Reasoning.** For a 5-day build with an evolving schema and no production data to
preserve, migrations are ceremony that slows iteration. `create_all` gets a stranger
running in one command.

**Tradeoff accepted — and called out honestly.** This does **not** handle schema
*changes* on an existing database (no `ALTER`), so it's not production-safe. Alembic is the
first thing I'd add before this held real data. Also noted: the manual template flow needs
`CREATE EXTENSION pg_trgm`, which Compose runs via `init.sql` but a managed Postgres
(Cloud SQL) needs run once manually — documented in the README and deploy script.

---

## 13. Vanilla static SPA instead of a frontend framework

**Decision.** The UI (`app/static/`) is hand-written HTML/CSS/JS served by FastAPI at
`/ui/`, with views for Process, Needs input, Review queue, Audit trail, and Training data.

**Alternatives considered.** React/Next.js with a separate build + deploy.

**Reasoning.** The UI's job is to make the agent's *decisions and the human-in-the-loop*
legible — a dropzone, a review queue, and an audit view. That doesn't need a framework or a
second deployable. Shipping it as static files inside the same container keeps the setup
one-command and the whole thing one URL. I spent the UI budget on the flow (empty states,
the "needs input" queue, showing *why* a decision was made) rather than on tooling.

**What I cut.** Client-side routing niceties, a component library, visual polish beyond a
clean dark theme — the rubric explicitly isn't grading visual polish.

---

## Summary of deliberate cuts

- Multi-document-type support (went deep on invoices/receipts instead).
- Semantic/vector search over document content (relational queries didn't need it).
- Real multi-user auth + RBAC + per-user audit attribution.
- Alembic migrations (used `create_all`).
- A PII-redaction layer for LLM input (fingerprint masking is not that, and I said so).
- A rules editor UI / full rules DSL (presets + learned rules instead).
- Async workers / a job queue (synchronous request path; fine at demo scale).
