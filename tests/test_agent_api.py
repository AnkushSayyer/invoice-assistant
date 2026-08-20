from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.database import get_db
from app.main import app
from app.schemas.agent import AgentDecision, AgentResult
from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import DEFAULT_VALIDATION_RULES


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def override_db():
    mock_db = MagicMock()

    def _override():
        yield mock_db

    app.dependency_overrides[get_db] = _override
    yield mock_db
    app.dependency_overrides.clear()


def _agent_result() -> AgentResult:
    return AgentResult(
        decision=AgentDecision.APPROVE,
        confidence=0.95,
        reasons=["math reconciles, no duplicate, within policy"],
        extraction=InvoiceExtraction(
            vendor="Merry Berry",
            invoice_number="INV-9001",
            date="2026-04-11",
            subtotal="150.00",
            tax="10.68",
            fees="10.00",
            total="170.68",
        ),
        validation_rules=DEFAULT_VALIDATION_RULES,
        calculated_total=Decimal("170.68"),
        claimed_amount=Decimal("170.68"),
        vendor_key="pan:AADCD4946L",
        source="llm",
    )


def test_process_endpoint_returns_agent_decision(
    client: TestClient, override_db: MagicMock
) -> None:
    with patch("app.api.agent.run_agent", return_value=_agent_result()):
        response = client.post(
            "/agent/process",
            files={"file": ("invoice.pdf", b"%PDF-1.4 sample", "application/pdf")},
            data={"claimed_amount": "170.68"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approve"
    assert body["confidence"] == 0.95
    assert body["vendor_key"] == "pan:AADCD4946L"


def test_process_endpoint_rejects_non_pdf(
    client: TestClient, override_db: MagicMock
) -> None:
    response = client.post(
        "/agent/process",
        files={"file": ("invoice.txt", b"not a pdf", "text/plain")},
        data={"claimed_amount": "10.00"},
    )
    assert response.status_code == 400


def test_process_endpoint_rejects_negative_amount(
    client: TestClient, override_db: MagicMock
) -> None:
    response = client.post(
        "/agent/process",
        files={"file": ("invoice.pdf", b"%PDF-1.4", "application/pdf")},
        data={"claimed_amount": "-5.00"},
    )
    assert response.status_code == 400


def test_resolve_endpoint_approves(
    client: TestClient, override_db: MagicMock
) -> None:
    result = _agent_result()
    with patch("app.api.agent.resolve_document", return_value=result) as mock_resolve:
        response = client.post(
            "/agent/resolve",
            json={
                "document_id": str(uuid4()),
                "decision": "approve",
                "approved_fields": result.extraction.model_dump(mode="json"),
            },
        )

    assert response.status_code == 200
    assert response.json()["decision"] == "approve"
    mock_resolve.assert_called_once()


def test_resolve_endpoint_requires_fields_to_approve(
    client: TestClient, override_db: MagicMock
) -> None:
    response = client.post(
        "/agent/resolve",
        json={"document_id": str(uuid4()), "decision": "approve"},
    )
    assert response.status_code == 422


def test_queue_endpoint_returns_items(
    client: TestClient, override_db: MagicMock
) -> None:
    with patch("app.api.agent.list_agent_queue", return_value=[]):
        response = client.get("/agent/queue")
    assert response.status_code == 200
    assert response.json() == []


def test_clarifications_endpoint_returns_items(
    client: TestClient, override_db: MagicMock
) -> None:
    with patch("app.api.agent.list_clarification_queue", return_value=[]):
        response = client.get("/agent/clarifications")
    assert response.status_code == 200
    assert response.json() == []


def test_clarify_endpoint_resolves_answer(
    client: TestClient, override_db: MagicMock
) -> None:
    result = _agent_result()
    with patch(
        "app.api.agent.resolve_clarification", return_value=result
    ) as mock_resolve:
        response = client.post(
            "/agent/clarify",
            json={
                "clarification_id": str(uuid4()),
                "answer_option_id": "fee",
                "answer_note": "Airport Surcharge",
            },
        )

    assert response.status_code == 200
    assert response.json()["decision"] == "approve"
    mock_resolve.assert_called_once()


def test_clarify_endpoint_conflict_when_already_answered(
    client: TestClient, override_db: MagicMock
) -> None:
    from app.services.document_processor import InvalidDocumentStateError

    with patch(
        "app.api.agent.resolve_clarification",
        side_effect=InvalidDocumentStateError("already answered"),
    ):
        response = client.post(
            "/agent/clarify",
            json={"clarification_id": str(uuid4()), "answer_option_id": "fee"},
        )
    assert response.status_code == 409
