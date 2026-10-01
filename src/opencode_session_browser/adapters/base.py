from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import SessionSummary


class UnsupportedStorage(Exception):
    """The location exists but is not a storage layout this adapter understands."""


class StorageBackend(ABC):
    """One storage generation/format. Adapters never write and never assume other adapters' schemas."""

    format_name = "?"

    @abstractmethod
    def fingerprint(self) -> tuple:
        """Cheap value that changes whenever the underlying storage may have changed."""

    @abstractmethod
    def describe(self) -> dict:
        """Diagnostics: detected format/schema details (no secrets)."""

    @abstractmethod
    def list_sessions(self) -> list[SessionSummary]:
        """Metadata for every session in this storage (no message bodies)."""

    @abstractmethod
    def load_messages(self, session_id: str) -> list[dict]:
        """Full normalised transcript (see normalize.py) in chronological order."""

    def prepare(self) -> bool:
        """Optionally refresh derived state (e.g. snapshot copy). True if something changed."""
        return False

    def message_fingerprint(self, session_id: str) -> tuple | None:
        return None
