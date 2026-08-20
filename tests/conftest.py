"""Shared pytest fixtures.

The agent service now consults the vendor knowledge base (``build_vendor_knowledge``)
and bumps per-vendor stats (``record_event``) on every run. Those touch the DB, but
the legacy agent tests drive ``run_agent`` with a ``MagicMock`` session. This autouse
fixture neutralizes those calls so existing tests keep exercising the decision logic
without a real database. Tests that want real knowledge behaviour simply patch
``app.services.agent.build_vendor_knowledge`` themselves inside the test body, which
overrides this fixture's patch for that scope.
"""

from unittest.mock import patch

import pytest

from app.services.knowledge import VendorKnowledge


@pytest.fixture(autouse=True)
def _neutralize_agent_knowledge():
    with (
        patch(
            "app.services.agent.build_vendor_knowledge",
            return_value=VendorKnowledge(),
        ),
        patch("app.services.agent.record_event", return_value=None),
    ):
        yield
