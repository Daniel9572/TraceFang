from __future__ import annotations

from dataclasses import dataclass

from market_analysis.application.provider_frames import ProviderFrame
from market_analysis.infrastructure.jetstream.frames import FrameEnvelope
from market_analysis.infrastructure.jetstream.store import FrameStore


@dataclass(frozen=True, slots=True)
class JetStreamRawFrameSink:
    """Adapts the provider-facing capture port to the JetStream frame store."""

    store: FrameStore

    async def capture(self, frame: ProviderFrame) -> int:
        return await self.store.capture(
            FrameEnvelope(
                version=frame.version,
                channel=frame.channel,
                connection_id=frame.connection_id,
                sequence=frame.sequence,
                received_at=frame.received_at,
                encoding=frame.encoding,
                body=frame.body,
            )
        )
