from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    ReplayPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.client import JetStreamContext
from nats.js.errors import NotFoundError

from market_analysis.infrastructure.jetstream.frames import FrameEnvelope
from market_analysis.infrastructure.jetstream.settings import JetStreamSettings


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    """One immutable provider frame with its durable stream ordering key."""

    stream_sequence: int
    envelope: FrameEnvelope


FrameHandler = Callable[[RecordedFrame], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FrameStreamBounds:
    first_sequence: int | None
    last_sequence: int | None
    message_count: int
    first_received_at: datetime | None
    last_received_at: datetime | None


class ReplaySession:
    """Consumes a server-paced ephemeral push consumer over a finite stream range."""

    def __init__(
        self,
        *,
        subscription: Any,
        jetstream: JetStreamContext,
        stream_name: str,
        consumer_name: str,
        end_sequence: int,
        initial_pending: int,
        speed: float,
        on_frame: FrameHandler,
        on_closed: Callable[[ReplaySession], None],
    ) -> None:
        self._subscription = subscription
        self._jetstream = jetstream
        self._stream_name = stream_name
        self._consumer_name = consumer_name
        self._end_sequence = end_sequence
        self._initial_pending = initial_pending
        self._speed = speed
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._task: asyncio.Task[None] | None = None
        self._cleanup_lock = asyncio.Lock()
        self._cleaned = False
        self.last_stream_sequence: int | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("replay session has already started")
        self._task = asyncio.create_task(self._consume(), name="jetstream-frame-replay")

    async def wait(self) -> None:
        if self._task is None:
            raise RuntimeError("replay session has not started")
        await self._task

    async def cancel(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        # ``wait`` is the observation point for handler failures. Cleanup remains
        # idempotent and must not re-raise the same failure from a finally block.
        with suppress(asyncio.CancelledError, Exception):
            await self._task

    async def _consume(self) -> None:
        previous_received_at: datetime | None = None
        try:
            if self._initial_pending == 0:
                return
            async for message in self._subscription.messages:
                metadata = message.metadata
                stream_sequence = metadata.sequence.stream
                if stream_sequence > self._end_sequence:
                    break
                envelope = FrameEnvelope.from_message(message.data, message.headers)
                if previous_received_at is not None:
                    delay = (envelope.received_at - previous_received_at).total_seconds()
                    if delay > 0:
                        await asyncio.sleep(delay / self._speed)
                previous_received_at = envelope.received_at
                self.last_stream_sequence = stream_sequence
                await self._on_frame(RecordedFrame(stream_sequence, envelope))
                if stream_sequence >= self._end_sequence or metadata.num_pending == 0:
                    break
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        async with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            try:
                await self._subscription.unsubscribe()
                with suppress(NotFoundError):
                    await self._jetstream.delete_consumer(
                        self._stream_name,
                        self._consumer_name,
                    )
            finally:
                self._on_closed(self)


class FrameStore:
    """Persists complete provider frames and replays them using JetStream timing."""

    def __init__(self, settings: JetStreamSettings) -> None:
        self.settings = settings
        self._client: NatsClient | None = None
        self._jetstream: JetStreamContext | None = None
        self._sessions: set[ReplaySession] = set()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        if self.is_connected:
            return
        client = await nats.connect(
            self.settings.url,
            connect_timeout=self.settings.connect_timeout_seconds,
            max_reconnect_attempts=2,
            reconnect_time_wait=0.25,
        )
        jetstream = client.jetstream(timeout=self.settings.publish_timeout_seconds)
        try:
            await self._ensure_stream(jetstream)
        except BaseException:
            await client.close()
            raise
        self._client = client
        self._jetstream = jetstream

    async def close(self) -> None:
        sessions = tuple(self._sessions)
        for session in sessions:
            await session.cancel()
        client, self._client = self._client, None
        self._jetstream = None
        if client is not None:
            await client.drain()

    async def capture(self, envelope: FrameEnvelope) -> int:
        jetstream = self._require_jetstream()
        acknowledgement = await jetstream.publish(
            self.settings.channel_subject(envelope.channel),
            envelope.body,
            timeout=self.settings.publish_timeout_seconds,
            stream=self.settings.stream_name,
            headers=envelope.headers(),
        )
        if acknowledgement.stream != self.settings.stream_name:
            raise RuntimeError("JetStream acknowledged the frame from an unexpected stream")
        return acknowledgement.seq

    async def bounds(self) -> FrameStreamBounds:
        stream = await self._require_jetstream().stream_info(self.settings.stream_name)
        state = stream.state
        if state.messages == 0:
            return FrameStreamBounds(None, None, 0, None, None)
        return FrameStreamBounds(
            first_sequence=state.first_seq,
            last_sequence=state.last_seq,
            message_count=state.messages,
            # nats-py versions before StreamState timestamp exposure still
            # provide exact per-frame capture time through ``frame_at``.
            first_received_at=getattr(state, "first_ts", None),
            last_received_at=getattr(state, "last_ts", None),
        )

    async def frame_at(self, sequence: int) -> RecordedFrame:
        """Reads one exact retained frame for seek timecodes without replaying a range."""

        if sequence < 1:
            raise ValueError("frame sequence must be positive")
        jetstream = self._require_jetstream()
        stream = await jetstream.stream_info(self.settings.stream_name)
        if (
            stream.state.messages == 0
            or sequence < stream.state.first_seq
            or sequence > stream.state.last_seq
        ):
            raise ValueError("requested frame is outside retained stream sequences")
        try:
            message = await jetstream.get_msg(self.settings.stream_name, seq=sequence)
        except NotFoundError as error:
            raise ValueError("requested frame is not retained") from error
        headers = getattr(message, "headers", None) or getattr(message, "header", None)
        return RecordedFrame(
            stream_sequence=sequence,
            envelope=FrameEnvelope.from_message(message.data, headers),
        )

    async def replay(
        self,
        *,
        start_sequence: int,
        end_sequence: int,
        on_frame: FrameHandler,
        channel: str | None = None,
        speed: float = 1.0,
    ) -> ReplaySession:
        if start_sequence < 1:
            raise ValueError("replay start sequence must be positive")
        if end_sequence < start_sequence:
            raise ValueError("replay end sequence must be >= start sequence")
        if not 0.25 <= speed <= 64:
            raise ValueError("replay speed must be between 0.25 and 64")
        jetstream = self._require_jetstream()
        stream = await jetstream.stream_info(self.settings.stream_name)
        if stream.state.messages == 0:
            raise ValueError("recorded frame stream is empty")
        if start_sequence < stream.state.first_seq or end_sequence > stream.state.last_seq:
            raise ValueError("requested replay range is outside retained stream sequences")

        subject = (
            self.settings.capture_subject
            if channel is None
            else self.settings.channel_subject(channel)
        )
        subscription = await jetstream.subscribe(
            subject,
            stream=self.settings.stream_name,
            config=ConsumerConfig(
                deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
                opt_start_seq=start_sequence,
                ack_policy=AckPolicy.NONE,
                # Application pacing uses the immutable capture timestamps so the
                # same sequence can be replayed at 0.25x..64x without buffering it
                # in the browser. Delivery itself remains strictly ordered.
                replay_policy=ReplayPolicy.INSTANT,
                flow_control=True,
                idle_heartbeat=5.0,
            ),
        )
        consumer = await subscription.consumer_info()
        session = ReplaySession(
            subscription=subscription,
            jetstream=jetstream,
            stream_name=self.settings.stream_name,
            consumer_name=consumer.name,
            end_sequence=end_sequence,
            initial_pending=consumer.num_pending or 0,
            speed=speed,
            on_frame=on_frame,
            on_closed=self._sessions.discard,
        )
        self._sessions.add(session)
        session.start()
        return session

    async def _ensure_stream(self, jetstream: JetStreamContext) -> None:
        try:
            info = await jetstream.stream_info(self.settings.stream_name)
        except NotFoundError:
            await jetstream.add_stream(
                config=StreamConfig(
                    name=self.settings.stream_name,
                    subjects=[self.settings.capture_subject],
                    retention=RetentionPolicy.LIMITS,
                    max_bytes=self.settings.max_bytes,
                    discard=DiscardPolicy.NEW,
                    max_age=self.settings.max_age_seconds,
                    storage=StorageType.FILE,
                )
            )
            return
        subjects = info.config.subjects or []
        if self.settings.capture_subject not in subjects:
            raise RuntimeError("existing JetStream stream does not capture the configured subject")
        if info.config.storage != StorageType.FILE:
            raise RuntimeError("existing JetStream frame stream must use file storage")

    def _require_jetstream(self) -> JetStreamContext:
        if self._jetstream is None or not self.is_connected:
            raise RuntimeError("JetStream frame store is not connected")
        return self._jetstream
