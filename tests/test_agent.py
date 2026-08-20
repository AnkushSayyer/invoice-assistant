from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.schemas.agent import AgentDecision, ExpensePolicy
from app.schemas.extraction import InvoiceExtraction
from app.services.agent import run_agent
from app.services.agent_tools import DuplicateHit, Perception


def _extraction(**overrides) -> InvoiceExtraction:
    defaults = dict(
        vendor="Merry Berry",
        invoice_number="INV-9001",
        date="2026-04-11",
        line_items=[{"description": "Mini Alphonso Mango", "amount": "150.00"}],
        subtotal="150.00",
        tax="10.68",
        fees="10.00",
        total="170.68",
    )
    defaults.update(overrides)
    return InvoiceExtraction(**defaults)


def _perception(extraction=None, confidence=0.75, vendor_key="pan:AADCD4946L") -> Perception:
    return Perception(
        extraction=extraction or _extraction(),
        confidence=confidence,
        source="llm",
        segments=1,
        vendor_key=vendor_key,
    )


def test_agent_approves_when_math_reconciles() -> None:
    db = MagicMock()
    with (
        patch("app.services.agent.perceive", return_value=_perception()),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy") as mock_learn,
    ):
        result = run_agent(
            db, b"%PDF-1.4", "invoice.pdf", Decimal("170.68"),
            expense_policy=ExpensePolicy(),
        )

    assert result.decision == AgentDecision.APPROVE
    assert result.calculated_total == Decimal("170.68")
    assert result.approved_amount == Decimal("170.68")  # full claim on clean approval
    assert result.confidence > 0.75  # reconciliation bonus applied
    mock_learn.assert_called_once()  # vendor memory updated on approval
    db.add.assert_called()  # document + agent run persisted
    db.commit.assert_called_once()


def test_agent_remediates_when_vendor_rules_are_wrong() -> None:
    db = MagicMock()
    bad_policy = SimpleNamespace(
        validation_rules={
            "validate_line_items": False,
            "total_components": ["subtotal"],
            "subtract_discounts": False,
            "line_amount_includes_tax": False,
        },
        tolerance=None,
        times_seen=3,
    )
    with (
        patch("app.services.agent.perceive", return_value=_perception()),
        patch("app.services.agent.lookup_vendor_policy", return_value=bad_policy),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy"),
    ):
        result = run_agent(
            db, b"%PDF-1.4", "invoice.pdf", Decimal("170.68"),
            expense_policy=ExpensePolicy(),
        )

    assert result.decision == AgentDecision.APPROVE
    assert result.remediations  # self-correction recorded
    assert "subtotal" in result.validation_rules.total_components
    assert "tax" in result.validation_rules.total_components


def test_agent_rejects_duplicate() -> None:
    db = MagicMock()
    dup = DuplicateHit(document_id=uuid4(), invoice_number="INV-9001")
    with (
        patch("app.services.agent.perceive", return_value=_perception()),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=dup),
        patch("app.services.agent.learn_vendor_policy") as mock_learn,
    ):
        result = run_agent(
            db, b"%PDF-1.4", "invoice.pdf", Decimal("170.68"),
            expense_policy=ExpensePolicy(),
        )

    assert result.decision == AgentDecision.REJECT
    assert result.duplicate_of == str(dup.document_id)
    mock_learn.assert_not_called()  # never learn from a rejected claim


def test_agent_rejects_when_over_max_amount() -> None:
    db = MagicMock()
    with (
        patch("app.services.agent.perceive", return_value=_perception()),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy"),
    ):
        result = run_agent(
            db, b"%PDF-1.4", "invoice.pdf", Decimal("170.68"),
            expense_policy=ExpensePolicy(max_amount=Decimal("100")),
        )

    assert result.decision == AgentDecision.REJECT
    assert any("exceeds maximum" in reason for reason in result.reasons)


def test_agent_escalates_over_auto_approve_limit() -> None:
    db = MagicMock()
    with (
        patch("app.services.agent.perceive", return_value=_perception()),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy"),
    ):
        result = run_agent(
            db, b"%PDF-1.4", "invoice.pdf", Decimal("170.68"),
            expense_policy=ExpensePolicy(auto_approve_limit=Decimal("100")),
        )

    assert result.decision == AgentDecision.ESCALATE
    assert any("auto-approve limit" in reason for reason in result.reasons)


def test_agent_escalates_on_low_confidence_when_math_fails() -> None:
    db = MagicMock()
    with (
        patch(
            "app.services.agent.perceive",
            return_value=_perception(confidence=0.4),
        ),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy"),
    ):
        # Math does not reconcile and reconciliation is not required, so the low
        # confidence score is what forces the escalation.
        result = run_agent(
            db, b"%PDF-1.4", "invoice.pdf", Decimal("999.00"),
            expense_policy=ExpensePolicy(require_math_reconciliation=False),
        )

    assert result.decision == AgentDecision.ESCALATE
    assert any("confidence" in reason for reason in result.reasons)


def test_agent_trusts_reconciled_math_over_low_confidence() -> None:
    db = MagicMock()
    with (
        patch(
            "app.services.agent.perceive",
            return_value=_perception(confidence=0.33),
        ),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy"),
    ):
        # Low OCR confidence but the math reconciles exactly -> auto-approve, no human.
        result = run_agent(
            db, b"%PDF-1.4", "invoice.pdf", Decimal("170.68"),
            expense_policy=ExpensePolicy(),
        )

    assert result.decision == AgentDecision.APPROVE
    assert result.confidence >= 0.6  # low OCR score floored by clean reconciliation
    assert any("math reconciles" in reason for reason in result.reasons)


def test_agent_escalates_when_math_cannot_reconcile() -> None:
    db = MagicMock()
    with (
        patch("app.services.agent.perceive", return_value=_perception()),
        patch("app.services.agent.lookup_vendor_policy", return_value=None),
        patch("app.services.agent.check_duplicate", return_value=None),
        patch("app.services.agent.learn_vendor_policy"),
    ):
        # Claimed amount matches no plausible formula for this invoice.
        result = run_agent(
            db, b"%PDF-1.4", "invoice.pdf", Decimal("999.00"),
            expense_policy=ExpensePolicy(),
        )

    assert result.decision == AgentDecision.ESCALATE
    assert any("reconcile" in reason for reason in result.reasons)
