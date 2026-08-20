from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.database import get_db
from app.db.models import Document
from app.main import app
from app.schemas.documents import DocumentStatus
from app.schemas.validation_rules import B2B_VALIDATION_RULES
from app.services.validation import ValidationResult


def _document(**overrides: object) -> Document:
    now = datetime.now(timezone.utc)
    defaults = {
        "id": uuid4(),
        "filename": "invoice.pdf",
        "fingerprint": "sig",
        "status": DocumentStatus.PENDING,
        "claimed_amount": Decimal("110.00"),
        "calculated_total": Decimal("110.00"),
        "has_pdf": True,
        "pdf_data": b"%PDF-1.4 sample",
        "auto_approved": False,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return Document(**defaults)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def mock_db() -> MagicMock:
    return MagicMock()


@pytest.fixture
def override_db(mock_db: MagicMock):
    def _override() -> MagicMock:
        yield mock_db

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.clear()


def test_review_returns_pending_documents(
    client: TestClient,
    override_db: None,
    mock_db: MagicMock,
) -> None:
    pending = _document(status=DocumentStatus.PENDING)
    mock_db.execute.return_value.scalars.return_value.all.return_value = [pending]

    response = client.get("/review")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["filename"] == "invoice.pdf"
    assert body[0]["status"] == DocumentStatus.PENDING
    assert body[0]["claimed_amount"] == "110.00"
    assert body[0]["has_pdf"] is True
    assert body[0]["pdf_url"].endswith("/pdf")


@patch("app.api.documents.process_upload")
def test_upload_returns_created_document(
    mock_process_upload: MagicMock,
    client: TestClient,
    override_db: None,
) -> None:
    document = _document(
        extracted_fields={"vendor": "Acme Corp"},
    )
    validation = ValidationResult(
        is_valid=True,
        errors=[],
        calculated_total=Decimal("110.00"),
    )
    mock_process_upload.return_value = (
        document,
        validation,
        None,
        B2B_VALIDATION_RULES,
        False,
    )

    response = client.post(
        "/upload",
        data={"claimed_amount": "110.00"},
        files={"file": ("invoice.pdf", b"%PDF-1.4 test", "application/pdf")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["math_valid"] is True
    assert body["claimed_amount"] == "110.00"
    assert body["calculated_total"] == "110.00"
    assert body["document"]["filename"] == "invoice.pdf"


@patch("app.api.documents.approve_document")
def test_approve_returns_processed_document(
    mock_approve_document: MagicMock,
    client: TestClient,
    override_db: None,
) -> None:
    document_id = uuid4()
    template_id = uuid4()
    example_id = uuid4()
    document = _document(
        id=document_id,
        status=DocumentStatus.PROCESSED,
    )
    template = MagicMock(id=template_id)
    template_example = MagicMock(id=example_id)
    mock_approve_document.return_value = (
        document,
        template,
        template_example,
        B2B_VALIDATION_RULES,
    )

    response = client.post(
        "/approve",
        json={
            "document_id": str(document_id),
            "template_name": "Acme Standard",
            "validation_rules": {
                "validate_line_items": False,
                "total_components": ["subtotal", "tax", "fees"],
            },
            "approved_fields": {
                "vendor": "Acme Corp",
                "invoice_number": "INV-1",
                "date": "2024-01-15",
                "line_items": [],
                "discounts": [],
                "subtotal": "100.00",
                "tax": "8.00",
                "fees": "2.00",
                "tip": "0.00",
                "total": "110.00",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["document"]["status"] == DocumentStatus.PROCESSED
    assert body["template_id"] == str(template_id)
    assert body["validation_rules"]["total_components"] == ["subtotal", "tax", "fees"]


def test_upload_rejects_non_pdf(client: TestClient, override_db: None) -> None:
    response = client.post(
        "/upload",
        data={"claimed_amount": "10.00"},
        files={"file": ("invoice.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file must be a PDF"


def test_upload_requires_claimed_amount(client: TestClient, override_db: None) -> None:
    response = client.post(
        "/upload",
        files={"file": ("invoice.pdf", b"%PDF-1.4 test", "application/pdf")},
    )

    assert response.status_code == 422


@patch("app.api.documents.get_document_pdf")
def test_get_document_pdf_returns_inline_pdf(
    mock_get_document_pdf: MagicMock,
    client: TestClient,
    override_db: None,
) -> None:
    document_id = uuid4()
    mock_get_document_pdf.return_value = (
        _document(id=document_id),
        b"%PDF-1.4 stored",
    )

    response = client.get(f"/documents/{document_id}/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == b"%PDF-1.4 stored"
    assert 'inline; filename="invoice.pdf"' in response.headers["content-disposition"]


@patch("app.api.documents.get_document_pdf")
def test_get_document_pdf_returns_404_when_missing(
    mock_get_document_pdf: MagicMock,
    client: TestClient,
    override_db: None,
) -> None:
    from app.services.document_processor import DocumentNotFoundError

    mock_get_document_pdf.side_effect = DocumentNotFoundError("missing")

    response = client.get(f"/documents/{uuid4()}/pdf")

    assert response.status_code == 404
