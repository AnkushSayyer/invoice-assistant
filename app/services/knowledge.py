"""Per-vendor knowledge base: additive, scalable learning keyed on ``vendor_key``.

This replaces the brittle layout-fingerprint approach as the learning backbone.
Identity is a stable vendor key (email domain / URL host / PAN / GSTIN), and each
lesson is a typed, scoped :class:`~app.db.models.VendorRule` that accumulates
rather than overwrites. The extractor injects these rules (and worked few-shot
examples) before parsing a new invoice, and the deterministic reconcile step uses
``capture`` rules to recover a known missing charge without the LLM.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    RuleScope,
    RuleType,
    VendorFewShot,
    VendorProfile,
    VendorRule,
)
from app.schemas.validation_rules import TemplateValidationRules

# Cap how many worked examples we inject so the prompt stays bounded.
MAX_FEW_SHOTS = 3


@dataclass
class VendorKnowledge:
    """Everything learned for a vendor, resolved for one extraction run."""

    directives: List[str] = field(default_factory=list)
    few_shots: List[Any] = field(default_factory=list)
    validation_rules: Optional[TemplateValidationRules] = None
    capture_rules: List[VendorRule] = field(default_factory=list)
    policy_rules: List[VendorRule] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.directives
            or self.few_shots
            or self.validation_rules
            or self.capture_rules
            or self.policy_rules
        )


def get_or_create_profile(
    db: Session,
    vendor_key: Optional[str],
    *,
    display_name: Optional[str] = None,
    category: Optional[str] = None,
) -> Optional[VendorProfile]:
    if not vendor_key:
        return None
    profile = db.execute(
        select(VendorProfile).where(VendorProfile.vendor_key == vendor_key)
    ).scalar_one_or_none()
    if profile is None:
        profile = VendorProfile(
            vendor_key=vendor_key,
            display_name=display_name,
            category=category,
        )
        db.add(profile)
        db.flush()
    else:
        if display_name and not profile.display_name:
            profile.display_name = display_name
        if category and not profile.category:
            profile.category = category
    return profile


def record_event(
    db: Session,
    vendor_key: Optional[str],
    *,
    seen: bool = False,
    approved: bool = False,
    clarified: bool = False,
    display_name: Optional[str] = None,
    category: Optional[str] = None,
) -> Optional[VendorProfile]:
    """Bump rolling per-vendor counters used for stats and rule-decay heuristics."""
    profile = get_or_create_profile(
        db, vendor_key, display_name=display_name, category=category
    )
    if profile is None:
        return None
    if seen:
        profile.times_seen = (profile.times_seen or 0) + 1
    if approved:
        profile.times_approved = (profile.times_approved or 0) + 1
    if clarified:
        profile.times_clarified = (profile.times_clarified or 0) + 1
    return profile


def load_active_rules(
    db: Session,
    vendor_key: Optional[str],
    *,
    category: Optional[str] = None,
) -> List[VendorRule]:
    """Return active rules that apply here: this vendor + its category + global.

    Ordered vendor-first so a vendor-specific lesson overrides a broader one when
    both set the same structured field.
    """
    scope_keys: List[tuple[str, str]] = [(RuleScope.GLOBAL, RuleScope.GLOBAL_KEY)]
    if category:
        scope_keys.append((RuleScope.CATEGORY, category))
    if vendor_key:
        scope_keys.append((RuleScope.VENDOR, vendor_key))
    if not scope_keys:
        return []

    conditions = [
        (VendorRule.scope == scope) & (VendorRule.scope_key == key)
        for scope, key in scope_keys
    ]
    combined = conditions[0]
    for cond in conditions[1:]:
        combined = combined | cond

    rules = list(
        db.execute(
            select(VendorRule)
            .where(VendorRule.active.is_(True))
            .where(combined)
            .order_by(VendorRule.created_at.asc())
        ).scalars().all()
    )
    # Broadest first (global -> category -> vendor) so vendor-specific wins on merge.
    scope_rank = {RuleScope.GLOBAL: 0, RuleScope.CATEGORY: 1, RuleScope.VENDOR: 2}
    rules.sort(key=lambda r: scope_rank.get(r.scope, 0))
    return rules


def load_few_shots(
    db: Session, vendor_key: Optional[str], *, limit: int = MAX_FEW_SHOTS
) -> List[VendorFewShot]:
    if not vendor_key:
        return []
    return list(
        db.execute(
            select(VendorFewShot)
            .where(VendorFewShot.vendor_key == vendor_key)
            .order_by(VendorFewShot.created_at.desc())
            .limit(limit)
        ).scalars().all()
    )


def build_vendor_knowledge(
    db: Session,
    vendor_key: Optional[str],
    *,
    category: Optional[str] = None,
) -> VendorKnowledge:
    """Resolve all learned knowledge for a vendor into an injectable bundle."""
    knowledge = VendorKnowledge()
    if not vendor_key:
        return knowledge

    rules = load_active_rules(db, vendor_key, category=category)
    for rule in rules:
        if rule.rule_type == RuleType.VALIDATION and rule.payload:
            # Later (more specific) rules win because rules are scope-sorted.
            knowledge.validation_rules = TemplateValidationRules.model_validate(
                rule.payload
            )
        if rule.rule_type == RuleType.POLICY:
            knowledge.policy_rules.append(rule)
        if rule.capture_anchor and rule.target_field:
            knowledge.capture_rules.append(rule)
        if rule.directive:
            knowledge.directives.append(rule.directive)

    for example in load_few_shots(db, vendor_key):
        knowledge.few_shots.append(
            SimpleNamespace(
                raw_text=example.example_text,
                masked_text=example.example_text,
                expected_fields=example.example_fields,
            )
        )
    return knowledge


def add_few_shot(
    db: Session,
    vendor_key: Optional[str],
    *,
    example_text: Optional[str],
    example_fields: Optional[dict],
    source_document_id: Optional[Any] = None,
) -> Optional[VendorFewShot]:
    if not vendor_key or not example_text or not example_fields:
        return None
    example = VendorFewShot(
        vendor_key=vendor_key,
        source_document_id=source_document_id,
        example_text=example_text,
        example_fields=example_fields,
    )
    db.add(example)
    return example


def create_rule(
    db: Session,
    *,
    scope: str,
    scope_key: str,
    rule_type: str,
    directive: str,
    trigger: Optional[str] = None,
    payload: Optional[dict] = None,
    capture_anchor: Optional[str] = None,
    target_field: Optional[str] = None,
    source: str = "human",
    confidence: float = 1.0,
    document_id: Optional[Any] = None,
    clarification_id: Optional[Any] = None,
) -> VendorRule:
    """Persist a new learned rule. Always additive (never overwrites an existing one)."""
    rule = VendorRule(
        scope=scope,
        scope_key=scope_key,
        rule_type=rule_type,
        trigger=trigger,
        directive=directive,
        payload=payload or {},
        capture_anchor=capture_anchor,
        target_field=target_field,
        source=source,
        confidence=confidence,
        document_id=document_id,
        created_from_clarification_id=clarification_id,
    )
    db.add(rule)
    db.flush()
    return rule
