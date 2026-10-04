from __future__ import annotations

import json
import os
import stat
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_SESSION_TOKEN_LENGTH = 36
_MAX_STORAGE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Jin10SessionCredentials:
    """The reusable credential actually persisted by the Jin10 client."""

    session_token: str = field(repr=False)
    origin: str


class Jin10SessionResolver:
    """Resolve the supported desktop session store without consulting diagnostics.

    The wire login accepts ``user_id=0`` and resolves the account from the session
    token. Client logs are deliberately outside this resolver.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        home_directory: Path | None = None,
        platform_name: str | None = None,
        current_uid: int | None = None,
        explicit_credentials: Jin10SessionCredentials | None = None,
    ) -> None:
        self._env = os.environ if env is None else env
        self._home_directory = Path.home() if home_directory is None else home_directory
        self._platform_name = sys.platform if platform_name is None else platform_name
        self._current_uid = (
            os.getuid() if current_uid is None and hasattr(os, "getuid") else current_uid
        )
        self._explicit_credentials = explicit_credentials
        self._cached: Jin10SessionCredentials | None = None
        self._known_tokens: set[str] = set()
        self._lock = threading.Lock()

    @classmethod
    def fixed(cls, *, session_token: str) -> Jin10SessionResolver:
        credentials = cls._validated_credentials(
            session_token=session_token,
            origin="explicit",
        )
        return cls(explicit_credentials=credentials)

    def validate_configuration(self) -> None:
        """Validate explicit settings without requiring a logged-in client yet."""

        configured = self._env.get("JIN10_LOCAL_SESSION_TOKEN", "").strip()
        if configured:
            self._validate_token(configured)

    def resolve(self, *, refresh: bool = False) -> Jin10SessionCredentials:
        with self._lock:
            if self._cached is not None and not refresh:
                return self._cached
            credentials = self._resolve_uncached()
            self._known_tokens.add(credentials.session_token)
            self._cached = credentials
            return credentials

    def available(self) -> bool:
        try:
            self.resolve(refresh=True)
        except ValueError:
            return False
        return True

    def redact(self, message: str) -> str:
        result = message
        configured = self._env.get("JIN10_LOCAL_SESSION_TOKEN", "").strip()
        candidates = self._known_tokens | ({configured} if configured else set())
        for token in candidates:
            if token:
                result = result.replace(token, "<redacted>")
        return result

    def _resolve_uncached(self) -> Jin10SessionCredentials:
        if self._explicit_credentials is not None:
            return self._explicit_credentials

        configured_token = self._env.get("JIN10_LOCAL_SESSION_TOKEN", "").strip()
        if configured_token:
            return self._validated_credentials(
                session_token=configured_token,
                origin="environment",
            )
        return self._validated_credentials(
            session_token=self._read_desktop_token(),
            origin="desktop",
        )

    def _read_desktop_token(self) -> str:
        if not self._platform_name.startswith("darwin"):
            raise ValueError(
                "JIN10_LOCAL_SESSION_TOKEN is required when desktop token discovery is unavailable"
            )
        support_directory = self._macos_support_directory()
        storage = support_directory / "local_storage.json"
        self._validate_client_file(storage, support_directory)
        try:
            if storage.stat().st_size > _MAX_STORAGE_BYTES:
                raise ValueError("Jin10 desktop session storage is unexpectedly large")
            payload = json.loads(storage.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("cannot read the Jin10 desktop session storage") from error
        token = payload.get("ji10_token") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            raise ValueError("Jin10 desktop session token was not found; sign in to the client")
        return self._validate_token(token.strip())

    def _macos_support_directory(self) -> Path:
        return self._home_directory / "Library" / "Application Support" / "com.jin10.desktop"

    def _validate_client_file(self, path: Path, root: Path) -> None:
        if root.is_symlink() or path.is_symlink():
            raise ValueError("Jin10 session storage must be a regular client file")
        try:
            resolved_root = root.resolve(strict=True)
            resolved_path = path.resolve(strict=True)
            resolved_path.relative_to(resolved_root)
            info = path.stat(follow_symlinks=False)
        except (OSError, ValueError) as error:
            raise ValueError("Jin10 session storage must be a regular client file") from error
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Jin10 session storage must be a regular client file")
        if self._current_uid is not None and info.st_uid != self._current_uid:
            raise ValueError("Jin10 session storage must belong to the current user")

    @staticmethod
    def _validate_token(token: str) -> str:
        if len(token) != _SESSION_TOKEN_LENGTH:
            raise ValueError("Jin10 session token must contain the 36-character token")
        return token

    @classmethod
    def _validated_credentials(
        cls,
        *,
        session_token: str,
        origin: str,
    ) -> Jin10SessionCredentials:
        token = cls._validate_token(session_token.strip())
        return Jin10SessionCredentials(
            session_token=token,
            origin=origin,
        )
