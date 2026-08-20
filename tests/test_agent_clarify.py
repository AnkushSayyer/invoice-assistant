"""End-to-end ask-and-learn flow: the agent asks a targeted question on a missing
charge, and once answered it learns a rule so the re-run reconciles on its own."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.db.models import ClarificationStatus, Document, RuleScope
from app.schemas.agent import AgentDecision
from app.schemas.documents import DocumentStatus
from app.schemas.extraction import InvoiceExtraction
from app.services.agent import resolve_clarification, run_agent
from app.services.agent_tools import Perception
from app.services.clarification import AMB_MISSING_CHARGE
from app.services.knowledge import VendorKnowledge

_RAW_TEXT = (
    "Trip fare\t500.00\n"
    "GST\t25.00\n"
    "Airport Surcharge\t40.00\n"
    "Total\t565.00"
)


def _missing_charge_extraction() -> InvoiceExtraction:
    # Airport surcharge (40) is on the total but not captured into fees, so the
    # stated total (565) exceeds subtotal + tax + fees (525).
    return InvoiceExtraction(
        vendor="Uber",
        invoice_number="TRIP-1",
        date="2026-04-11",
        line_items=[{"description": "Trip fare", "amount": "500.00"}],
        subtotal="500.00",
        tax="25.00",
        fees="0.00",
        total="565.00",
    )


def _perception() -> Perception:
    return Perception(
        extraction=_missing_charge_extraction(),
        confidence=0.75,
        source="llm",
        segments=1,
        vendor_key="uber.com",
        raw_text=_RAW_TEXT,
    )


def test_agent_asks_clarification_on_missing_charge() -> None:
    db = MagicMock()
    with (
        patch("app.services.agent.perceive", return_value=_perception()),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy") as mock_learn,
    ):
        result = run_agent(db, b"%PDF-1.4", "trip.pdf", Decimal("565.00"))

    assert result.decision == AgentDecision.CLARIFY
    assert len(result.clarifications) == 1
    clarification = result.clarifications[0]
    assert clarification.ambiguity_type == AMB_MISSING_CHARGE
    assert clarification.proposed_scope == RuleScope.VENDOR
    # A clarification must never auto-approve or learn a (wrong) rule yet.
    mock_learn.assert_not_called()
    db.commit.assert_called_once()


def _document() -> Document:
    return Document(
        id=uuid4(),
        filename="trip.pdf",
        fingerprint="uber.com",
        vendor_key="uber.com",
        claimed_amount=Decimal("565.00"),
        calculated_total=Decimal("525.00"),
        validation_rules={},
        pdf_data=b"%PDF-1.4",
        has_pdf=True,
        status=DocumentStatus.NEEDS_INPUT,
    )


def _open_clarification(document_id) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        document_id=document_id,
        ambiguity_type=AMB_MISSING_CHARGE,
        evidence={},
        proposed_scope=RuleScope.VENDOR,
        proposed_scope_key="uber.com",
        round=1,
        status=ClarificationStatus.OPEN,
    )


def test_answering_clarification_learns_rule_and_rerun_recovers() -> None:
    """The guarantee: after the human names the missing line, the re-run captures it
    deterministically (Tier A) and auto-approves — no second question."""
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    clarification = _open_clarification(document.id)

    # After the answer, the vendor's knowledge now includes the capture rule.
    learned_knowledge = VendorKnowledge(
        capture_rules=[
            SimpleNamespace(capture_anchor="Airport Surcharge", target_field="fees")
        ]
    )

    with (
        patch("app.services.agent.get_clarification", return_value=clarification),
        patch("app.services.agent.perceive", return_value=_perception()),
        patch(
            "app.services.agent.build_vendor_knowledge",
            return_value=learned_knowledge,
        ),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy"),
    ):
        result = resolve_clarification(
            db,
            clarification.id,
            answer_option_id="fee",
            answer_note="Airport Surcharge",
        )

    assert result.decision == AgentDecision.APPROVE
    assert result.approved_amount == Decimal("565.00")
    assert any("recovered missing charge" in r for r in result.remediations)
    assert document.status == DocumentStatus.PROCESSED
    # The question was answered and a rule was persisted.
    assert clarification.status == ClarificationStatus.ANSWERED
    assert clarification.answer_option_id == "fee"


def test_answering_missing_charge_persists_capture_rule() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    clarification = _open_clarification(document.id)
    added: list = []
    db.add.side_effect = lambda obj: added.append(obj)

    with (
        patch("app.services.agent.get_clarification", return_value=clarification),
        patch("app.services.agent.perceive", return_value=_perception()),
        patch(
            "app.services.agent.build_vendor_knowledge",
            return_value=VendorKnowledge(
                capture_rules=[
                    SimpleNamespace(
                        capture_anchor="Airport Surcharge", target_field="fees"
                    )
                ]
            ),
        ),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy"),
    ):
        resolve_clarification(
            db,
            clarification.id,
            answer_option_id="fee",
            answer_note="Airport Surcharge",
        )

    from app.db.models import VendorRule

    capture_rules = [
        obj
        for obj in added
        if isinstance(obj, VendorRule) and obj.capture_anchor == "Airport Surcharge"
    ]
    assert capture_rules, "a deterministic capture rule should have been learned"
    assert capture_rules[0].target_field == "fees"
    assert capture_rules[0].scope_key == "uber.com"
