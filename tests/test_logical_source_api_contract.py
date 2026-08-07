from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from market_analysis import api
from market_analysis.application.sources import (
    QuoteServiceTier,
    SourceAccessModel,
    SourceHealth,
    SourceRoutingRole,
)


class LogicalSourceApiContractTests(unittest.TestCase):
    def test_realtime_endpoints_do_not_accept_client_selected_source(self) -> None:
        self.assertEqual(tuple(inspect.signature(api.quote).parameters), ("code",))
        self.assertEqual(
            tuple(inspect.signature(api.quote_stream).parameters),
            ("websocket", "code"),
        )

    def test_public_source_payload_hides_physical_topology(self) -> None:
        descriptor = SimpleNamespace(
            source_id="logical",
            display_name="Logical",
            description="one aggregate result",
            capabilities=("quote",),
            enabled=True,
            frozen=False,
            delayed=False,
            requires_running_app=False,
            structured=True,
            quote_poll_interval_seconds=0,
            quote_streaming=True,
            quote_service_tier=QuoteServiceTier.ENHANCED,
            access_model=SourceAccessModel.UNMETERED,
            access_note=None,
            manual_connection_required=False,
            connection_active=True,
            quotas=(),
            health=SourceHealth.HEALTHY,
            state="ready",
            error=None,
            checked_at=None,
            last_success_at=None,
            routing_role=SourceRoutingRole.LOGICAL,
            component_source_ids=("physical_a", "physical_b"),
            field_ownership=("internal",),
        )

        payload = api._public_source(descriptor)

        self.assertTrue(payload["selectable"])
        self.assertNotIn("routing_role", payload)
        self.assertNotIn("component_source_ids", payload)
        self.assertNotIn("field_ownership", payload)
        self.assertNotIn("enabled", payload)

    def test_public_health_hides_physical_acquisition_channels(self) -> None:
        payload = api._public_acquisition_status(
            {
                "routes": {"XAU/USD": "jin10_client"},
                "active_channels": {"physical_a": ("XAU/USD",)},
                "poll_tasks": ("physical_b:XAU/USD",),
            }
        )

        self.assertEqual(
            payload,
            {
                "state": "running",
                "routes": {"XAU/USD": "jin10_client"},
            },
        )


if __name__ == "__main__":
    unittest.main()
