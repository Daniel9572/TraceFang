from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from nats.js.api import (
    AckPolicy,
    DeliverPolicy,
    DiscardPolicy,
    ReplayPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError

from tracefang.infrastructure.jetstream import (
    FrameEnvelope,
    FrameStore,
    JetStreamSettings,
    RecordedFrame,
)


def envelope(sequence: int = 1) -> FrameEnvelope:
    return FrameEnvelope(
        version=1,
        channel="jin10_web",
        connection_id="connection-1",
        sequence=sequence,
        received_at=datetime(2026, 8, 10, 12, 30, 45, 123456, tzinfo=UTC),
        encoding="wire",
        body=f"frame-{sequence}".encode(),
    )


class ConnectedClient:
    is_connected = True


class FakeMessage:
    def __init__(self, stream_sequence: int, value: FrameEnvelope, pending: int) -> None:
        self.data = value.body
        self.headers = value.headers()
        self.metadata = SimpleNamespace(
            sequence=SimpleNamespace(stream=stream_sequence),
            num_pending=pending,
        )


class FakeSubscription:
    def __init__(self, messages, *, pending: int | None = None) -> None:
        self._messages = tuple(messages)
        self._pending = len(self._messages) if pending is None else pending
        self.unsubscribed = False

    @property
    def messages(self):
        async def iterate():
            for message in self._messages:
                yield message

        return iterate()

    async def consumer_info(self):
        return SimpleNamespace(name="ephemeral-replay", num_pending=self._pending)

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class BlockingSubscription(FakeSubscription):
    def __init__(self) -> None:
        super().__init__((), pending=1)
        self.waiting = asyncio.Event()

    @property
    def messages(self):
        async def iterate():
            self.waiting.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover

        return iterate()


class FakeJetStream:
    def __init__(self, subscription: FakeSubscription | None = None) -> None:
        self.subscription = subscription or FakeSubscription(())
        self.publish_started = asyncio.Event()
        self.release_publish = asyncio.Event()
        self.publish_error: Exception | None = None
        self.publish_call = None
        self.subscribe_call = None
        self.deleted = []
        self.created_config = None
        self.updated_config = None
        self.stream_state = SimpleNamespace(messages=5, first_seq=1, last_seq=5)
        self.stored_messages = {}

    async def publish(self, subject, payload, **options):
        self.publish_call = (subject, payload, options)
        self.publish_started.set()
        await self.release_publish.wait()
        if self.publish_error is not None:
            raise self.publish_error
        return SimpleNamespace(stream="MARKET_RAW_FRAMES", seq=17)

    async def stream_info(self, name):
        return SimpleNamespace(
            state=self.stream_state,
            config=SimpleNamespace(subjects=["market.raw.>"], storage=StorageType.FILE),
        )

    async def subscribe(self, subject, **options):
        self.subscribe_call = (subject, options)
        return self.subscription

    async def delete_consumer(self, stream, consumer) -> bool:
        self.deleted.append((stream, consumer))
        return True

    async def get_msg(self, stream, *, seq):
        try:
            value = self.stored_messages[seq]
        except KeyError as error:
            raise NotFoundError from error
        return SimpleNamespace(data=value.body, header=value.headers())

    async def add_stream(self, *, config) -> None:
        self.created_config = config

    async def update_stream(self, *, config) -> None:
        self.updated_config = config


class MissingStreamJetStream(FakeJetStream):
    async def stream_info(self, name):
        raise NotFoundError


class LegacyDiscardNewJetStream(FakeJetStream):
    async def stream_info(self, name):
        return SimpleNamespace(
            state=self.stream_state,
            config=SimpleNamespace(
                subjects=["market.raw.>"],
                storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS,
                discard=DiscardPolicy.NEW,
            ),
        )


class EmptyLegacyDiscardNewJetStream(FakeJetStream):
    def __init__(self) -> None:
        super().__init__()
        self.stream_state = SimpleNamespace(messages=0, first_seq=1, last_seq=0)

    async def stream_info(self, name):
        return SimpleNamespace(
            state=self.stream_state,
            config=StreamConfig(
                name="MARKET_RAW_FRAMES",
                subjects=["market.raw.>"],
                retention=RetentionPolicy.LIMITS,
                max_bytes=10 * 1024 * 1024 * 1024,
                discard=DiscardPolicy.NEW,
                max_age=7 * 24 * 60 * 60,
                storage=StorageType.FILE,
            ),
        )


class LimitedMessageJetStream(FakeJetStream):
    async def stream_info(self, name):
        return SimpleNamespace(
            state=self.stream_state,
            config=SimpleNamespace(
                subjects=["market.raw.>"],
                storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS,
                discard=DiscardPolicy.OLD,
                max_msg_size=1024,
            ),
        )


def connected_store(jetstream: FakeJetStream) -> FrameStore:
    store = FrameStore(JetStreamSettings("nats://127.0.0.1:14222"))
    store._client = ConnectedClient()
    store._jetstream = jetstream
    return store


class JetStreamSettingsTests(unittest.TestCase):
    def test_empty_url_keeps_jetstream_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(JetStreamSettings.from_env())

    def test_loads_explicit_configuration(self) -> None:
        values = {
            "TRACEFANG_NATS_URL": "nats://127.0.0.1:14222",
            "TRACEFANG_NATS_STREAM": "RAW_TEST",
            "TRACEFANG_NATS_SUBJECT_PREFIX": "test.raw",
            "TRACEFANG_NATS_MAX_AGE_SECONDS": "60",
            "TRACEFANG_NATS_MAX_BYTES": "4096",
            "TRACEFANG_NATS_MAX_FRAME_BYTES": "2048",
        }
        with patch.dict(os.environ, values, clear=True):
            settings = JetStreamSettings.from_env()
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(settings.stream_name, "RAW_TEST")
        self.assertEqual(settings.capture_subject, "test.raw.>")
        self.assertEqual(settings.channel_subject("jin10_web"), "test.raw.jin10_web")
        self.assertEqual(settings.max_age_seconds, 60)
        self.assertEqual(settings.max_bytes, 4096)
        self.assertEqual(settings.max_frame_bytes, 2048)

    def test_rejects_frame_limit_larger_than_stream_capacity(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            JetStreamSettings(
                "nats://127.0.0.1:14222",
                max_bytes=1024,
                max_frame_bytes=2048,
            )


class FrameEnvelopeTests(unittest.TestCase):
    def test_round_trips_binary_body_and_capture_metadata(self) -> None:
        original = envelope()
        restored = FrameEnvelope.from_message(original.body, original.headers())
        self.assertEqual(restored, original)
        self.assertEqual(
            original.headers()["Nats-Msg-Id"],
            "jin10_web:connection-1:1",
        )

    def test_rejects_naive_capture_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            FrameEnvelope(
                version=1,
                channel="jin10_web",
                connection_id="connection-1",
                sequence=1,
                received_at=datetime(2026, 8, 10),
                encoding="wire",
                body=b"frame",
            )


class FrameStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_waits_for_and_returns_publish_ack(self) -> None:
        jetstream = FakeJetStream()
        store = connected_store(jetstream)
        task = asyncio.create_task(store.capture(envelope()))
        await asyncio.wait_for(jetstream.publish_started.wait(), timeout=1)
        self.assertFalse(task.done())
        jetstream.release_publish.set()
        self.assertEqual(await asyncio.wait_for(task, timeout=1), 17)
        subject, payload, options = jetstream.publish_call
        self.assertEqual(subject, "market.raw.jin10_web")
        self.assertEqual(payload, b"frame-1")
        self.assertEqual(options["stream"], "MARKET_RAW_FRAMES")

    async def test_capture_propagates_publish_ack_failure(self) -> None:
        jetstream = FakeJetStream()
        jetstream.publish_error = RuntimeError("publish acknowledgement failed")
        jetstream.release_publish.set()
        store = connected_store(jetstream)
        with self.assertRaisesRegex(RuntimeError, "acknowledgement failed"):
            await store.capture(envelope())

    async def test_capture_rejects_encoded_headers_plus_body_before_publish(self) -> None:
        value = envelope()
        jetstream = FakeJetStream()
        jetstream.release_publish.set()
        store = FrameStore(
            JetStreamSettings(
                "nats://127.0.0.1:14222",
                max_frame_bytes=len(value.body),
            )
        )
        store._client = ConnectedClient()
        store._jetstream = jetstream

        with self.assertRaisesRegex(ValueError, "encoded provider frame"):
            await store.capture(value)

        self.assertIsNone(jetstream.publish_call)

    async def test_rejects_server_payload_smaller_than_frame_limit(self) -> None:
        store = FrameStore(JetStreamSettings("nats://127.0.0.1:14222"))

        with self.assertRaisesRegex(RuntimeError, "nats-server.conf"):
            store._validate_server_capacity(SimpleNamespace(max_payload=1024))

    async def test_accepts_server_payload_equal_to_frame_limit(self) -> None:
        settings = JetStreamSettings("nats://127.0.0.1:14222")
        store = FrameStore(settings)

        store._validate_server_capacity(
            SimpleNamespace(max_payload=settings.max_frame_bytes)
        )

    async def test_creates_rolling_file_stream_that_retains_latest_frames(self) -> None:
        jetstream = MissingStreamJetStream()
        store = connected_store(jetstream)
        await store._ensure_stream(jetstream)
        config = jetstream.created_config
        self.assertEqual(config.storage, StorageType.FILE)
        self.assertEqual(config.discard, DiscardPolicy.OLD)
        self.assertEqual(config.subjects, ["market.raw.>"])
        self.assertEqual(config.max_msg_size, store.settings.max_frame_bytes)

    async def test_rejects_legacy_stream_instead_of_silently_losing_live_edge(self) -> None:
        jetstream = LegacyDiscardNewJetStream()
        store = connected_store(jetstream)

        with self.assertRaisesRegex(RuntimeError, "legacy retention policy"):
            await store._ensure_stream(jetstream)

    async def test_safely_migrates_empty_legacy_stream_to_rolling_retention(self) -> None:
        jetstream = EmptyLegacyDiscardNewJetStream()
        store = connected_store(jetstream)

        await store._ensure_stream(jetstream)

        self.assertIsNotNone(jetstream.updated_config)
        self.assertEqual(jetstream.updated_config.discard, DiscardPolicy.OLD)
        self.assertEqual(
            jetstream.updated_config.max_msg_size,
            store.settings.max_frame_bytes,
        )

    async def test_rejects_nonempty_stream_with_smaller_message_limit(self) -> None:
        jetstream = LimitedMessageJetStream()
        store = connected_store(jetstream)

        with self.assertRaisesRegex(RuntimeError, "select a new"):
            await store._ensure_stream(jetstream)

        self.assertIsNone(jetstream.updated_config)

    async def test_replay_uses_capture_timing_and_finite_sequence_range(self) -> None:
        messages = (
            FakeMessage(2, envelope(2), pending=2),
            FakeMessage(3, envelope(3), pending=1),
            FakeMessage(4, envelope(4), pending=0),
        )
        subscription = FakeSubscription(messages)
        jetstream = FakeJetStream(subscription)
        store = connected_store(jetstream)
        received = []

        async def accept(value: RecordedFrame) -> None:
            received.append(value)

        session = await store.replay(
            start_sequence=2,
            end_sequence=3,
            channel="jin10_web",
            on_frame=accept,
        )
        await asyncio.wait_for(session.wait(), timeout=1)

        self.assertEqual([value.stream_sequence for value in received], [2, 3])
        self.assertEqual([value.envelope.sequence for value in received], [2, 3])
        self.assertEqual(session.last_stream_sequence, 3)
        subject, options = jetstream.subscribe_call
        self.assertEqual(subject, "market.raw.jin10_web")
        config = options["config"]
        self.assertEqual(config.deliver_policy, DeliverPolicy.BY_START_SEQUENCE)
        self.assertEqual(config.opt_start_seq, 2)
        self.assertEqual(config.ack_policy, AckPolicy.NONE)
        self.assertEqual(config.replay_policy, ReplayPolicy.ORIGINAL)
        self.assertTrue(subscription.unsubscribed)
        self.assertEqual(
            jetstream.deleted,
            [("MARKET_RAW_FRAMES", "ephemeral-replay")],
        )

    async def test_cancel_removes_ephemeral_push_consumer(self) -> None:
        subscription = BlockingSubscription()
        jetstream = FakeJetStream(subscription)
        store = connected_store(jetstream)

        async def accept(value: RecordedFrame) -> None:
            self.fail(f"unexpected replay frame: {value}")

        session = await store.replay(
            start_sequence=1,
            end_sequence=5,
            on_frame=accept,
        )
        await asyncio.wait_for(subscription.waiting.wait(), timeout=1)
        await asyncio.wait_for(session.cancel(), timeout=1)
        self.assertTrue(subscription.unsubscribed)
        self.assertEqual(
            jetstream.deleted,
            [("MARKET_RAW_FRAMES", "ephemeral-replay")],
        )

    async def test_reads_exact_frame_for_seek_timecode(self) -> None:
        jetstream = FakeJetStream()
        jetstream.stored_messages[3] = envelope(3)
        store = connected_store(jetstream)

        recorded = await store.frame_at(3)

        self.assertEqual(recorded.stream_sequence, 3)
        self.assertEqual(recorded.envelope, envelope(3))


if __name__ == "__main__":
    unittest.main()
