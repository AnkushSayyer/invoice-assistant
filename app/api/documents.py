from decimal import Decimal
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db.database import get_db
from app.schemas.documents import (
    ApproveRequest,
    ApproveResponse,
    DocumentResponse,
    UploadResponse,
    build_document_response,
)
from app.services.document_processor import (
    DocumentNotFoundError,
    DuplicateTemplateNameError,
    InvalidDocumentStateError,
    approve_document,
    get_document_pdf,
    list_pending_documents,
    process_upload,
)
from app.services.extractor import ExtractionTimeoutError, PDFReadError
from app.services.llm import (
    ExtractionLLMError,
    MissingLLMApiKeyError,
    format_llm_error_detail,
    llm_http_status_for_error,
)

router = APIRouter(tags=["documents"])


@router.post("/upload", response_model=UploadResponse, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    claimed_amount: Decimal = Form(..., description="Amount the user is claiming for reimbursement"),
    db: Session = Depends(get_db),
) -> UploadResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Uploaded file must be a PDF")

    if claimed_amount < 0:
        raise HTTPException(status_code=400, detail="Claimed amount must be non-negative")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        document, validation, template_match, validation_rules, auto_approved = (
            await run_in_threadpool(
                process_upload,
                db,
                pdf_bytes,
                file.filename,
                claimed_amount,
            )
        )
    except PDFReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MissingLLMApiKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ExtractionTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ExtractionLLMError as exc:
        raise HTTPException(
            status_code=llm_http_status_for_error(exc),
            detail=format_llm_error_detail(exc),
        ) from exc
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database connection failed",
        ) from exc

    return UploadResponse(
        document=build_document_response(document),
        math_valid=validation.is_valid,
        validation_errors=validation.errors,
        calculated_total=validation.calculated_total,
        claimed_amount=claimed_amount,
        matched_template_id=(
            template_match.template.id if template_match is not None else None
        ),
        similarity_score=(
            template_match.similarity_score if template_match is not None else None
        ),
        validation_rules=validation_rules,
        auto_approved=auto_approved,
    )


@router.get("/review", response_model=List[DocumentResponse])
def review_pending_documents(db: Session = Depends(get_db)) -> List[DocumentResponse]:
    try:
        documents = list_pending_documents(db)
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database connection failed",
        ) from exc

    return [build_document_response(document) for document in documents]


@router.get("/documents/{document_id}/pdf")
def get_document_pdf_file(
    document_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    try:
        document, pdf_bytes = get_document_pdf(db, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database connection failed",
        ) from exc

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{document.filename}"'},
    )


@router.post("/approve", response_model=ApproveResponse)
def approve_pending_document(
    payload: ApproveRequest,
    db: Session = Depends(get_db),
) -> ApproveResponse:
    try:
        document, template, template_example, validation_rules = approve_document(
            db,
            payload.document_id,
            payload.template_name,
            payload.approved_fields,
            payload.validation_rules,
            payload.description,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDocumentStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DuplicateTemplateNameError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database connection failed",
        ) from exc

    return ApproveResponse(
        document=build_document_response(document),
        template_id=template.id,
        template_example_id=template_example.id,
        validation_rules=validation_rules,
    )
