from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.template_matcher import find_matching_template


def _template(name: str = "Zomato") -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), name=name)


def test_returns_none_for_blank_signature() -> None:
    db = MagicMock()

    assert find_matching_template(db, "   ") is None
    db.execute.assert_not_called()


def test_anchored_match_accepts_low_similarity_for_known_vendor() -> None:
    db = MagicMock()
    template = _template()
    # Anchored query returns a template with modest similarity (0.60).
    db.execute.return_value.first.return_value = (template, 0.60)

    match = find_matching_template(db, "signature", vendor_key="zomato.com")

    assert match is not None
    assert match.template is template
    assert match.similarity_score == 0.60
    # Anchor hit resolves in a single query; no fuzzy fallback needed.
    assert db.execute.call_count == 1


def test_anchor_miss_falls_back_to_fuzzy_similarity() -> None:
    db = MagicMock()
    fuzzy_template = _template("Fuzzy")
    # First call: anchored query finds nothing. Second call: fuzzy match succeeds.
    db.execute.return_value.first.side_effect = [None, (fuzzy_template, 0.90)]

    match = find_matching_template(db, "signature", vendor_key="zomato.com")

    assert match is not None
    assert match.template is fuzzy_template
    assert match.similarity_score == 0.90
    assert db.execute.call_count == 2


def test_anchor_below_threshold_falls_back_to_fuzzy() -> None:
    db = MagicMock()
    anchored_template = _template("Anchored")
    fuzzy_template = _template("Fuzzy")
    # Anchored similarity 0.40 is below ANCHORED_SIMILARITY_THRESHOLD -> fall back.
    db.execute.return_value.first.side_effect = [
        (anchored_template, 0.40),
        (fuzzy_template, 0.88),
    ]

    match = find_matching_template(db, "signature", vendor_key="zomato.com")

    assert match is not None
    assert match.template is fuzzy_template
    assert db.execute.call_count == 2


def test_no_vendor_key_uses_fuzzy_only() -> None:
    db = MagicMock()
    template = _template()
    db.execute.return_value.first.return_value = (template, 0.95)

    match = find_matching_template(db, "signature")

    assert match is not None
    assert match.template is template
    # Only the fuzzy query runs when there is no vendor key.
    assert db.execute.call_count == 1


def test_returns_none_when_no_match_found() -> None:
    db = MagicMock()
    db.execute.return_value.first.return_value = None

    assert find_matching_template(db, "signature") is None
