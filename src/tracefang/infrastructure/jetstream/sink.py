from __future__ import annotations

from dataclasses import dataclass

from tracefang.application.provider_frames import ProviderFrame
from tracefang.infrastructure.jetstream.frames import FrameEnvelope
from tracefang.infrastructure.jetstream.store import FrameStore


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
