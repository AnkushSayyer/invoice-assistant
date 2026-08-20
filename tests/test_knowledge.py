from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.db.models import RuleScope, RuleType
from app.schemas.validation_rules import TemplateValidationRules
from app.services import knowledge
from app.services.knowledge import (
    VendorKnowledge,
    add_few_shot,
    build_vendor_knowledge,
    create_rule,
)


def _rule(**overrides) -> SimpleNamespace:
    defaults = dict(
        rule_type=RuleType.HINT,
        directive="check the footer for a surcharge",
        payload={},
        capture_anchor=None,
        target_field=None,
        scope=RuleScope.VENDOR,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_build_vendor_knowledge_empty_without_vendor_key() -> None:
    result = build_vendor_knowledge(MagicMock(), None)
    assert result.is_empty


def test_build_vendor_knowledge_collects_directives_and_capture_rules() -> None:
    rules = [
        _rule(directive="lesson A"),
        _rule(
            rule_type=RuleType.FIELD_MAPPING,
            directive="capture Airport Surcharge into fees",
            capture_anchor="Airport Surcharge",
            target_field="fees",
        ),
        _rule(
            rule_type=RuleType.VALIDATION,
            directive="lines include tax",
            payload=TemplateValidationRules(
                validate_line_items=True, line_amount_includes_tax=True
            ).model_dump(mode="json"),
        ),
    ]
    db = MagicMock()
    with (
        patch.object(knowledge, "load_active_rules", return_value=rules),
        patch.object(knowledge, "load_few_shots", return_value=[]),
    ):
        result = build_vendor_knowledge(db, "uber.com")

    assert "lesson A" in result.directives
    assert len(result.capture_rules) == 1
    assert result.capture_rules[0].target_field == "fees"
    assert result.validation_rules is not None
    assert result.validation_rules.line_amount_includes_tax is True
    assert not result.is_empty


def test_build_vendor_knowledge_builds_few_shots() -> None:
    example = SimpleNamespace(
        example_text="Uber invoice body",
        example_fields={"vendor": "Uber", "total": "565.00"},
    )
    db = MagicMock()
    with (
        patch.object(knowledge, "load_active_rules", return_value=[]),
        patch.object(knowledge, "load_few_shots", return_value=[example]),
    ):
        result = build_vendor_knowledge(db, "uber.com")

    assert len(result.few_shots) == 1
    assert result.few_shots[0].raw_text == "Uber invoice body"
    assert result.few_shots[0].expected_fields == {"vendor": "Uber", "total": "565.00"}


def test_create_rule_is_additive() -> None:
    db = MagicMock()
    rule = create_rule(
        db,
        scope=RuleScope.VENDOR,
        scope_key="uber.com",
        rule_type=RuleType.FIELD_MAPPING,
        directive="capture Airport Surcharge into fees",
        capture_anchor="Airport Surcharge",
        target_field="fees",
    )

    db.add.assert_called_once_with(rule)
    assert rule.capture_anchor == "Airport Surcharge"
    assert rule.target_field == "fees"
    assert rule.scope_key == "uber.com"


def test_global_validation_rule_applies_to_any_vendor() -> None:
    rules = [
        _rule(
            rule_type=RuleType.VALIDATION,
            scope=RuleScope.GLOBAL,
            directive="do not subtract discounts",
            payload=TemplateValidationRules(subtract_discounts=False).model_dump(
                mode="json"
            ),
        ),
    ]
    db = MagicMock()
    with (
        patch.object(knowledge, "load_active_rules", return_value=rules),
        patch.object(knowledge, "load_few_shots", return_value=[]),
    ):
        result = build_vendor_knowledge(db, "some-unrelated-vendor.com")

    assert result.validation_rules is not None
    assert result.validation_rules.subtract_discounts is False


def test_vendor_validation_rule_overrides_global() -> None:
    # load_active_rules yields broadest-first (global then vendor); the vendor rule
    # is applied last on merge and therefore wins.
    rules = [
        _rule(
            rule_type=RuleType.VALIDATION,
            scope=RuleScope.GLOBAL,
            directive="global: subtract discounts",
            payload=TemplateValidationRules(subtract_discounts=True).model_dump(
                mode="json"
            ),
        ),
        _rule(
            rule_type=RuleType.VALIDATION,
            scope=RuleScope.VENDOR,
            directive="vendor: do not subtract discounts",
            payload=TemplateValidationRules(subtract_discounts=False).model_dump(
                mode="json"
            ),
        ),
    ]
    db = MagicMock()
    with (
        patch.object(knowledge, "load_active_rules", return_value=rules),
        patch.object(knowledge, "load_few_shots", return_value=[]),
    ):
        result = build_vendor_knowledge(db, "vendor.com")

    assert result.validation_rules.subtract_discounts is False


def test_add_few_shot_requires_text_and_fields() -> None:
    db = MagicMock()
    assert add_few_shot(db, "uber.com", example_text=None, example_fields={"a": 1}) is None
    assert add_few_shot(db, "uber.com", example_text="text", example_fields=None) is None
    db.add.assert_not_called()

    example = add_few_shot(
        db, "uber.com", example_text="text", example_fields={"vendor": "Uber"}
    )
    assert example is not None
    db.add.assert_called_once()
