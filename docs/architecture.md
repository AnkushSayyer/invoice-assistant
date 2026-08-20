# InvoiceOps AI — Architecture

InvoiceOps AI turns invoice/receipt PDFs into **verified reimbursement decisions**.
It ships two pipelines over one Postgres database and one validation core:

1. **Autonomous agent** (`/agent/*`) — perceive → reconcile → remediate → decide → act, with a human-in-the-loop review queue and a learning loop.
2. **Manual template pipeline** (`/upload`, `/review`, `/approve`) — the original fingerprint + template flow, kept intact.

---

## System overview

```mermaid
flowchart TB
    subgraph Client["Browser UI (static SPA)"]
        UI["Process · Review Queue · Audit · Training Data"]
    end

    subgraph API["FastAPI app (app/main.py)"]
        AR["/agent router\napp/api/agent.py"]
        DR["documents router\napp/api/documents.py"]
        ST["Static UI mount /ui"]
    end

    subgraph Services["Service layer (app/services)"]
        AG["agent.py\ndecision loop"]
        AT["agent_tools.py\nperceive · reconcile · remediate · duplicate · policy · learn"]
        DP["document_processor.py\nupload · approve"]
        EX["extractor.py\nlayout text · split · combine · LLM"]
        DAI["document_ai.py\nInvoice Parser mapper"]
        VAL["validation.py\ndeterministic math"]
        FP["fingerprint.py + template_matcher.py\npg_trgm matching"]
    end

    subgraph Data["Postgres"]
        DOC[("documents")]
        TPL[("templates / template_examples")]
        VP[("vendor_policies\nagent memory + few-shot")]
        RUN[("agent_runs\naudit trail")]
    end

    subgraph External["External"]
        GCP["Google Document AI\nInvoice Parser"]
        LLM["LLM provider\nGemini / OpenAI (Instructor)"]
    end

    UI -->|multipart / JSON| AR
    UI --> DR
    ST --- UI

    AR --> AG
    DR --> DP

    AG --> AT
    AT --> DAI
    AT --> EX
    AT --> VAL
    DP --> EX
    DP --> VAL
    DP --> FP

    DAI -->|OCR + parse| GCP
    EX -->|structured extract| LLM

    AG --> DOC
    AG --> VP
    AG --> RUN
    DP --> DOC
    DP --> TPL
    FP --> TPL
    AT --> VP
```

---

## Agent decision loop (`/agent/process`)

```mermaid
flowchart TD
    START([PDF + claimed_amount]) --> P{Document AI configured?}

    P -->|yes| SPLIT["Split bundled invoices\n(split_invoice_page_groups + subset_pdf_pages)"]
    SPLIT --> DAI["Parse each invoice via Document AI\ncombine_invoices · min-confidence gate"]
    P -->|no / fails| LLM["PyMuPDF layout text → LLM\napply learned few-shot per vendor"]

    DAI --> RULES
    LLM --> RULES["Resolve rules\nvendor memory else derive"]

    RULES --> REC{"Math reconciles?\nsubtotal+tax+fees ±disc = total\nand claimed = total"}
    REC -->|no| REM["Remediate:\nsearch alternative formulas"]
    REM --> REC2{Reconciles now?}
    REC -->|yes| GUARD
    REC2 -->|yes| GUARD
    REC2 -->|no| ESC1[["ESCALATE\nmath unresolved"]]

    GUARD{"Duplicate?\nPolicy violation?"}
    GUARD -->|duplicate| REJ1[["REJECT"]]
    GUARD -->|violation| REJ2[["REJECT"]]
    GUARD -->|clean| CONF{"Low confidence\nAND math failed?"}

    CONF -->|yes| ESC2[["ESCALATE"]]
    CONF -->|no| LIMIT{"Above auto-approve limit?"}
    LIMIT -->|yes| ESC3[["ESCALATE"]]
    LIMIT -->|no| APP[["APPROVE\nlearn vendor rules"]]

    APP --> ACT["Persist Document + AgentRun"]
    REJ1 --> ACT
    REJ2 --> ACT
    ESC1 --> ACT
    ESC2 --> ACT
    ESC3 --> ACT
```

---

## Human-in-the-loop learning

```mermaid
sequenceDiagram
    actor R as Reviewer
    participant UI
    participant API as /agent/resolve
    participant AG as agent.resolve_document
    participant VP as vendor_policies
    participant LLM as LLM path (next time)

    Note over AG: A doc was ESCALATED / REJECTED
    R->>UI: correct fields, set approved_amount, note
    UI->>API: POST approve/reject
    API->>AG: resolve_document(...)
    AG->>AG: re-run deterministic math (or force / manual amount)
    AG->>VP: learn rules + few-shot (example_text + example_fields)
    AG-->>UI: AgentResult (source = human)

    Note over LLM,VP: Future invoice from same vendor
    LLM->>VP: lookup_vendor_policy(vendor_key)
    VP-->>LLM: learned rules + worked example
    LLM->>LLM: extract with few-shot → reconciles → auto-approve
```

---

## Key building blocks

| Concern | Module | Notes |
| --- | --- | --- |
| Perception | `services/agent_tools.py`, `services/document_ai.py`, `services/extractor.py` | Document AI first (splits bundles), LLM fallback with learned few-shot |
| Verification | `services/validation.py` | Deterministic math is the confidence engine |
| Self-correction | `services/agent_tools.remediate` | Searches plausible total formulas |
| Memory / learning | `db/models.VendorPolicy` | Per-vendor rules + few-shot, keyed on GSTIN/PAN/domain |
| Audit | `db/models.AgentRun` | Every decision + full step trace |
| Template pipeline | `services/document_processor.py`, `fingerprint.py`, `template_matcher.py` | Original `pg_trgm` flow, unchanged |

**Stack:** Python 3.11 · FastAPI · SQLAlchemy 2.0 / Pydantic v2 · Postgres (`pg_trgm`) · PyMuPDF · Instructor + Gemini/OpenAI · optional Google Document AI · Docker Compose.
