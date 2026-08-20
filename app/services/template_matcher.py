from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Template

SIMILARITY_THRESHOLD = 0.85
AUTO_APPROVE_THRESHOLD = 0.92
# When an exact vendor key matches, we trust it and accept a lower layout-similarity
# score to pick the right template variant for that vendor.
ANCHORED_SIMILARITY_THRESHOLD = 0.5


@dataclass(frozen=True)
class TemplateMatch:
    template: Template
    similarity_score: float


def find_matching_template(
    db: Session,
    signature: str,
    *,
    vendor_key: Optional[str] = None,
    threshold: float = SIMILARITY_THRESHOLD,
    anchored_threshold: float = ANCHORED_SIMILARITY_THRESHOLD,
) -> Optional[TemplateMatch]:
    """Return the best matching template using a two-tier strategy.

    Tier 1 (anchor): when a stable ``vendor_key`` is known, pick the highest
    layout-similarity template sharing that key, accepting a lower threshold since
    the vendor is already confirmed. Tier 2 (fuzzy): otherwise fall back to pure
    pg_trgm similarity over the masked fingerprint.
    """
    if not signature.strip():
        return None

    similarity = func.similarity(Template.vendor_fingerprint, signature)

    if vendor_key:
        anchored_stmt = (
            select(Template, similarity.label("similarity_score"))
            .where(Template.vendor_key == vendor_key)
            .order_by(similarity.desc())
            .limit(1)
        )
        anchored = db.execute(anchored_stmt).first()
        if anchored is not None:
            template, score = anchored
            if float(score) > anchored_threshold:
                return TemplateMatch(template=template, similarity_score=float(score))

    stmt = (
        select(Template, similarity.label("similarity_score"))
        .where(similarity > threshold)
        .order_by(similarity.desc())
        .limit(1)
    )

    result = db.execute(stmt).first()
    if result is None:
        return None

    template, score = result
    return TemplateMatch(template=template, similarity_score=float(score))
