from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from tracefang import api


class ReadinessTests(IsolatedAsyncioTestCase):
    async def test_local_readiness_does_not_query_upstream(self) -> None:
        state = SimpleNamespace(
            persistence=SimpleNamespace(health=lambda: SimpleNamespace(state="healthy")),
            acquisition=object(),
            frame_store=SimpleNamespace(is_connected=True),
        )
        with patch.object(api, "runtime", state), patch.object(api, "_manager", Mock()) as manager:
            self.assertEqual((await api.readiness())["status"], "ok")
            manager.assert_not_called()
            state.frame_store.is_connected = False
            self.assertEqual((await api.readiness())["status"], "degraded")
