from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class SessionSummary:
    """Metadata-only description of a session; no conversation content."""
    id: str
    title: str = ""
    slug: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    project_worktree: str | None = None
    directory: str | None = None
    subpath: str | None = None
    parent_id: str | None = None
    fork_of: str | None = None
    created: int | None = None
    updated: int | None = None
    archived_at: int | None = None
    agent: str | None = None
    provider: str | None = None
    model: str | None = None
    variant: str | None = None
    version: str | None = None
    message_count: int | None = None
    event_count: int | None = None
    cost: float | None = None
    tokens: dict = field(default_factory=dict)
    outcome: str | None = None          # last stored idle outcome (succeeded / failed / interrupted)
    storage: str = "unknown"            # v2 | legacy | files | export
    in_legacy_table: bool | None = None # visible to `opencode export` / `session list` machinery
    # filled by Source
    source_id: str = ""
    child_count: int = 0
    # filled from the search index once indexed (None = not indexed yet)
    error_count: int | None = None
    tool_count: int | None = None
    models: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source_id}~{self.id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d
