"""Persistent, deterministic repair learning records."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.models.local_llm_day import RepairEpisode


class SolutionCatalogEntry(BaseModel):
    catalog_id: str = Field(default_factory=lambda: uuid4().hex)
    scope: Literal["generic", "project"] = "project"
    project_id: str | None = None
    failure_class: str = Field(min_length=1)
    failure_fingerprint: str = Field(min_length=8, max_length=128)
    component: str = Field(default="", max_length=160)
    title: str = Field(min_length=1)
    symptoms: str = Field(min_length=1)
    root_cause: str = Field(min_length=1)
    diagnostic_steps: str = Field(min_length=1)
    resolution_strategy: str = Field(min_length=1)
    preconditions: list[str] = Field(default_factory=list)
    affected_files_or_scope: list[str] = Field(default_factory=list)
    verification: str = Field(min_length=1)
    source: Literal["CODEX_VERIFIED", "LOCAL_VERIFIED", "EXTERNAL_REVIEW_VERIFIED"]
    status: Literal["VERIFIED", "RETIRED"] = "VERIFIED"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    uses: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    last_used_at: str | None = None


class SolutionCatalogStore(Protocol):
    def load(self) -> list[SolutionCatalogEntry]: ...
    def save(self, entries: list[SolutionCatalogEntry]) -> None: ...


class JsonSolutionCatalogStore:
    """Small JSON persistence boundary; corrupt data fails closed to no matches."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def load(self) -> list[SolutionCatalogEntry]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return []
            values = raw.get("entries") if isinstance(raw, dict) else raw
            if not isinstance(values, list):
                return []
            entries: list[SolutionCatalogEntry] = []
            for value in values:
                try:
                    entries.append(SolutionCatalogEntry.model_validate(value))
                except (TypeError, ValueError):
                    continue
            return entries

    def save(self, entries: list[SolutionCatalogEntry]) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = {"version": 1, "entries": [entry.model_dump(mode="json") for entry in entries]}
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                # Keep the already verified in-memory entry usable; the caller
                # will expose a later restart as a missing persistent match.
                return


class SolutionCatalog:
    def __init__(self, store: SolutionCatalogStore | None = None) -> None:
        self.store = store
        self._entries = store.load() if store else []

    def find(self, *, project_id: str, failure_class: str, fingerprint: str, component: str) -> list[SolutionCatalogEntry]:
        ranked: list[tuple[int, SolutionCatalogEntry]] = []
        for entry in self._entries:
            if entry.status != "VERIFIED" or entry.failure_class != failure_class:
                continue
            if entry.scope == "project" and entry.project_id != project_id:
                continue
            score = 0
            if entry.failure_fingerprint == fingerprint:
                score += 100
            elif entry.failure_fingerprint[:16] == fingerprint[:16]:
                score += 40
            if entry.component and entry.component == component:
                score += 20
            if entry.scope == "project":
                score += 10
            if score:
                ranked.append((score, entry))
        ranked.sort(key=lambda item: (-item[0], item[1].catalog_id))
        matched = [entry for _, entry in ranked]
        if matched:
            now = datetime.now(timezone.utc).isoformat()
            self._entries = [
                entry.model_copy(update={"uses": entry.uses + 1, "last_used_at": now, "updated_at": now})
                if entry.catalog_id in {value.catalog_id for value in matched} else entry
                for entry in self._entries
            ]
            self._persist()
            return [entry.model_copy(update={"uses": entry.uses + 1, "last_used_at": now, "updated_at": now}) for entry in matched]
        return []

    def add_verified(self, entry: SolutionCatalogEntry) -> SolutionCatalogEntry:
        if entry.status != "VERIFIED":
            raise ValueError("only verified solutions may enter the catalog")
        self._entries = [value for value in self._entries if value.catalog_id != entry.catalog_id] + [entry]
        self._persist()
        return entry

    def entries(self) -> list[SolutionCatalogEntry]:
        return list(self._entries)

    def _persist(self) -> None:
        if self.store:
            self.store.save(self._entries)


class RepairEpisodeStore:
    """Independent episode persistence so restart cannot reset a deadline."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._episodes: dict[str, RepairEpisode] = {}
        if path:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                values = raw.get("episodes", []) if isinstance(raw, dict) else []
                self._episodes = {episode.episode_id: episode for value in values if (episode := RepairEpisode.model_validate(value))}
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                self._episodes = {}

    def save(self, episode: RepairEpisode) -> RepairEpisode:
        self._episodes[episode.episode_id] = episode
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                temporary.write_text(json.dumps({"version": 1, "episodes": [item.model_dump(mode="json") for item in self._episodes.values()]}, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                return episode
        return episode

    def get(self, episode_id: str) -> RepairEpisode | None:
        return self._episodes.get(episode_id)
