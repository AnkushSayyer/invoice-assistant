"""Autonomous agent API: process an invoice end-to-end and expose its audit trail.

This router is additive; the manual upload/approve endpoints in ``documents.py``
are untouched. ``POST /agent/process`` runs the full perceive -> reconcile ->
remediate -> decide -> act loop and commits an autonomous decision.
"""

from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db.database import get_db
from app.db.models import AgentRun
from app.schemas.agent import (
    AgentQueueItem,
    AgentResolveRequest,
    AgentResult,
    AgentRunSummary,
    AgentTrainingExample,
    ResolvePreviewRequest,
    ResolvePreviewResponse,
)
from app.schemas.clarification import (
    ClarificationAnswer,
    ClarificationQueueItem,
    ClarificationResponse,
)
from app.schemas.documents import build_document_response
from app.services.agent import (
    list_agent_queue,
    list_clarification_queue,
    list_training_examples,
    preview_resolution,
    resolve_clarification,
    resolve_document,
    run_agent,
)
from app.services.document_ai import DocumentAIError
from app.services.document_processor import (
    DocumentNotFoundError,
    InvalidDocumentStateError,
)
from app.services.extractor import ExtractionTimeoutError, PDFReadError
from app.services.llm import (
    ExtractionLLMError,
    MissingLLMApiKeyError,
    format_llm_error_detail,
    llm_http_status_for_error,
)

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/process", response_model=AgentResult)
async def process_invoice(
    file: UploadFile = File(...),
    claimed_amount: Decimal = Form(
        ..., description="Amount the user is claiming for reimbursement"
    ),
    category: Optional[str] = Form(
        default=None, description="Expense category (checked against policy)"
    ),
    db: Session = Depends(get_db),
) -> AgentResult:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Uploaded file must be a PDF")
    if claimed_amount < 0:
        raise HTTPException(status_code=400, detail="Claimed amount must be non-negative")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        return await run_in_threadpool(
            run_agent,
            db,
            pdf_bytes,
            file.filename,
            claimed_amount,
            category=category,
        )
    except PDFReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MissingLLMApiKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ExtractionTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except DocumentAIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ExtractionLLMError as exc:
        raise HTTPException(
            status_code=llm_http_status_for_error(exc),
            detail=format_llm_error_detail(exc),
        ) from exc
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc


@router.post("/resolve", response_model=AgentResult)
def resolve_escalated_document(
    payload: AgentResolveRequest,
    db: Session = Depends(get_db),
) -> AgentResult:
    """A human reviewer corrects and resolves an escalated or rejected document."""
    if payload.decision == "approve" and payload.approved_fields is None:
        raise HTTPException(
            status_code=422,
            detail="approved_fields is required to approve a document",
        )
    try:
        return resolve_document(
            db,
            payload.document_id,
            decision=payload.decision,
            approved_fields=payload.approved_fields,
            validation_rules=payload.validation_rules,
            approved_amount=payload.approved_amount,
            category=payload.category,
            note=payload.note,
            force=payload.force,
            learn_vendor=payload.learn_vendor,
            learn_scope=payload.learn_scope,
            learn_scope_key=payload.learn_scope_key,
            capture_anchor=payload.capture_anchor,
            capture_target_field=payload.capture_target_field,
            directive=payload.directive,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDocumentStateError as exc:
        # Corrected fields still do not reconcile (and force=False), or the
        # document is already approved.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc


@router.post("/resolve/preview", response_model=ResolvePreviewResponse)
def preview_escalated_document(
    payload: ResolvePreviewRequest,
    db: Session = Depends(get_db),
) -> ResolvePreviewResponse:
    """Read-only dry-run: test the reviewer's edited fields + a candidate rule
    against the current document without persisting anything."""
    try:
        return preview_resolution(
            db,
            payload.document_id,
            approved_fields=payload.approved_fields,
            validation_rules=payload.validation_rules,
            capture_anchor=payload.capture_anchor,
            capture_target_field=payload.capture_target_field,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDocumentStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc


@router.get("/clarifications", response_model=List[ClarificationQueueItem])
def clarification_queue(
    db: Session = Depends(get_db),
) -> List[ClarificationQueueItem]:
    """Documents the agent parked with a targeted, answerable question."""
    try:
        items = list_clarification_queue(db)
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc
    return [
        ClarificationQueueItem(
            document=build_document_response(document),
            clarifications=[
                ClarificationResponse.model_validate(clarification)
                for clarification in clarifications
            ],
        )
        for document, clarifications in items
    ]


@router.post("/clarify", response_model=AgentResult)
def answer_clarification(
    payload: ClarificationAnswer,
    db: Session = Depends(get_db),
) -> AgentResult:
    """Answer a clarification: learn the rule and re-run the document with it applied."""
    try:
        return resolve_clarification(
            db,
            payload.clarification_id,
            answer_option_id=payload.answer_option_id,
            answer_note=payload.answer_note,
            confirmed_scope=payload.confirmed_scope,
            confirmed_scope_key=payload.confirmed_scope_key,
            learn=payload.learn,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDocumentStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PDFReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MissingLLMApiKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ExtractionTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except DocumentAIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ExtractionLLMError as exc:
        raise HTTPException(
            status_code=llm_http_status_for_error(exc),
            detail=format_llm_error_detail(exc),
        ) from exc
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc


@router.get("/queue", response_model=List[AgentQueueItem])
def agent_review_queue(db: Session = Depends(get_db)) -> List[AgentQueueItem]:
    """List documents the agent escalated or rejected, awaiting human resolution."""
    try:
        items = list_agent_queue(db)
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc
    return [
        AgentQueueItem(
            document=build_document_response(document),
            latest_run=AgentRunSummary.model_validate(run) if run is not None else None,
        )
        for document, run in items
    ]


@router.get("/training-data", response_model=List[AgentTrainingExample])
def agent_training_data(
    limit: int = 200,
    db: Session = Depends(get_db),
) -> List[AgentTrainingExample]:
    """Export human-verified (PDF, labelled-fields) pairs for uptraining a parser."""
    try:
        documents = list_training_examples(db, limit=limit)
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc
    return [
        AgentTrainingExample(
            document_id=str(document.id),
            filename=document.filename,
            vendor_key=document.vendor_key,
            fields=document.extracted_fields or {},
        )
        for document in documents
    ]


@router.get("/runs", response_model=List[AgentRunSummary])
def list_agent_runs(
    limit: int = 50,
    db: Session = Depends(get_db),
) -> List[AgentRun]:
    try:
        stmt = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit)
        return list(db.execute(stmt).scalars().all())
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc


@router.get("/runs/{document_id}", response_model=List[AgentRunSummary])
def get_agent_runs_for_document(
    document_id: UUID,
    db: Session = Depends(get_db),
) -> List[AgentRun]:
    try:
        stmt = (
            select(AgentRun)
            .where(AgentRun.document_id == document_id)
            .order_by(AgentRun.created_at.desc())
        )
        return list(db.execute(stmt).scalars().all())
    except OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="Database connection failed"
        ) from exc
