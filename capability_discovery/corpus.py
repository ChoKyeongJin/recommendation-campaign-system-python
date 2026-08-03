"""Bounded, provenance-preserving evidence retrieval for capability GraphRAG.

Only files already named and hashed by a :class:`GraphSnapshot` are eligible.
This module deliberately has no repository scan and treats every source path as
untrusted input, even when it originated in the projection code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .domain import Evidence, GraphEdge, GraphNode, GraphSnapshot

_LINE_POINTER = re.compile(r"#L(?P<start>[1-9]\d*)(?:-L(?P<end>[1-9]\d*))?")
_SYMBOL_POINTER = re.compile(r"(?:symbol:)?([A-Za-z_][A-Za-z0-9_]*)")
_TEXT_SUFFIXES = {
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_SECRET_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_SECRET_NAMES = {
    "client_secret.json",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "service_account.json",
    "secrets",
    "secrets.json",
}
_SECRET_STEMS = {"credential", "credentials", "secret", "secrets"}
_DENIED_PARTS = {".git", ".ssh", "credentials", "secret", "secrets"}


def _bounded(text: str, maximum: int) -> tuple[str, bool]:
    if len(text) <= maximum:
        return text, False
    marker = "\n…[truncated]"
    if maximum <= len(marker):
        return marker[:maximum], True
    return text[: max(0, maximum - len(marker))] + marker, True


def _decode_json_pointer(pointer: str) -> tuple[str, ...] | None:
    if not pointer.startswith("/"):
        return None
    return tuple(
        part.replace("~1", "/").replace("~0", "~")
        for part in pointer[1:].split("/")
    )


def _json_pointer_context(payload: Any, pointer: str) -> Any:
    parts = _decode_json_pointer(pointer)
    if parts is None:
        raise ValueError("not a JSON pointer")
    current = payload
    parent: Any = None
    selected_key: str | int | None = None
    for raw_part in parts:
        parent = current
        if isinstance(current, dict):
            if raw_part not in current:
                raise KeyError(raw_part)
            selected_key = raw_part
            current = current[raw_part]
        elif isinstance(current, list):
            if not raw_part.isdigit():
                raise KeyError(raw_part)
            selected_key = int(raw_part)
            current = current[selected_key]
        else:
            raise KeyError(raw_part)

    context: dict[str, Any] = {"pointer": pointer, "value": current}
    if isinstance(parent, dict) and isinstance(selected_key, str):
        keys = list(parent)
        index = keys.index(selected_key)
        nearby = keys[max(0, index - 1) : index + 2]
        context["surrounding_object"] = {key: parent[key] for key in nearby}
    elif isinstance(parent, list) and isinstance(selected_key, int):
        start = max(0, selected_key - 1)
        context["surrounding_items"] = parent[start : selected_key + 2]
    return context


@dataclass(frozen=True)
class EvidenceChunk:
    """One bounded excerpt tied to immutable projection provenance."""

    chunk_id: str
    source_path: str
    source_pointer: str
    source_type: str
    trust_state: str
    content_hash: str
    excerpt_kind: str
    text: str
    owner_ids: tuple[str, ...]
    line_start: int | None = None
    line_end: int | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "source_pointer": self.source_pointer,
            "source_type": self.source_type,
            "trust_state": self.trust_state,
            "content_hash": self.content_hash,
            "excerpt_kind": self.excerpt_kind,
            "text": self.text,
            "owner_ids": list(self.owner_ids),
            "line_start": self.line_start,
            "line_end": self.line_end,
            "truncated": self.truncated,
        }

    def to_runtime_ref(self) -> dict[str, Any]:
        """Return provenance only; runtime annotations must not contain text."""

        return {
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "source_pointer": self.source_pointer,
            "source_type": self.source_type,
            "trust_state": self.trust_state,
            "content_hash": self.content_hash,
            "excerpt_kind": self.excerpt_kind,
            "owner_ids": list(self.owner_ids),
            "line_start": self.line_start,
            "line_end": self.line_end,
            "truncated": self.truncated,
        }


class EvidenceCorpus:
    """Read bounded excerpts from a snapshot's explicitly approved file set."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        max_file_bytes: int = 1_000_000,
        max_chunk_chars: int = 1_600,
        max_total_chars: int = 16_000,
        max_chunks: int = 24,
        line_radius: int = 4,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_total_chars = max(1, int(max_total_chars))
        self.max_chunk_chars = max(
            1, min(int(max_chunk_chars), self.max_total_chars)
        )
        self.max_chunks = max(1, int(max_chunks))
        self.line_radius = max(0, int(line_radius))

    def retrieve(
        self,
        snapshot: GraphSnapshot,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge] = (),
    ) -> tuple[EvidenceChunk, ...]:
        references: dict[
            tuple[str, str, str, str, str], tuple[Evidence, set[str]]
        ] = {}
        for owner in nodes:
            self._collect(references, owner.evidence, owner.id)
        for owner in edges:
            self._collect(references, owner.evidence, owner.id)

        chunks: list[EvidenceChunk] = []
        total_chars = 0
        # ``nodes`` arrive in deterministic relevance order, so insertion
        # order keeps the tight chunk budget focused on the best candidates.
        for key in references:
            if len(chunks) >= self.max_chunks or total_chars >= self.max_total_chars:
                break
            evidence, owners = references[key]
            remaining = min(self.max_chunk_chars, self.max_total_chars - total_chars)
            chunk = self._read(snapshot, evidence, tuple(sorted(owners)), remaining)
            if chunk is None:
                continue
            chunks.append(chunk)
            total_chars += len(chunk.text)
        return tuple(chunks)

    @staticmethod
    def _collect(
        references: dict[
            tuple[str, str, str, str, str], tuple[Evidence, set[str]]
        ],
        evidence_items: Iterable[Evidence],
        owner_id: str,
    ) -> None:
        for evidence in evidence_items:
            key = (
                evidence.source_path.replace("\\", "/"),
                evidence.source_pointer,
                evidence.source_type,
                evidence.content_hash or "",
                evidence.trust_state,
            )
            if key in references:
                references[key][1].add(owner_id)
            else:
                references[key] = (evidence, {owner_id})

    def _read(
        self,
        snapshot: GraphSnapshot,
        evidence: Evidence,
        owner_ids: tuple[str, ...],
        maximum: int,
    ) -> EvidenceChunk | None:
        source_path = evidence.source_path.replace("\\", "/")
        path = self._safe_path(source_path)
        if path is None or maximum < 1:
            return None
        expected_revision_hash = snapshot.revision.source_hashes.get(source_path)
        if expected_revision_hash is None:
            expected_revision_hash = snapshot.revision.source_hashes.get(
                evidence.source_path
            )
        if (
            expected_revision_hash is None
            or evidence.content_hash is None
            or expected_revision_hash != evidence.content_hash
        ):
            return None
        try:
            stat = path.stat()
            if stat.st_size > self.max_file_bytes:
                return None
            raw = path.read_bytes()
        except OSError:
            return None
        if len(raw) > self.max_file_bytes or b"\x00" in raw:
            return None
        actual_hash = hashlib.sha256(raw).hexdigest()
        if (
            actual_hash != evidence.content_hash
            or actual_hash != expected_revision_hash
        ):
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

        excerpt_kind = "text"
        line_start: int | None = None
        line_end: int | None = None
        truncated = False
        pointer = evidence.source_pointer
        if path.suffix.casefold() == ".json" and pointer.startswith("/"):
            try:
                payload = json.loads(text)
                selected = _json_pointer_context(payload, pointer)
                excerpt = json.dumps(
                    selected, ensure_ascii=False, sort_keys=True, indent=2
                )
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                return None
            excerpt_kind = "json_pointer"
            excerpt, truncated = _bounded(excerpt, maximum)
        else:
            lines = text.splitlines()
            match = _LINE_POINTER.search(pointer)
            if match:
                requested_start = int(match.group("start"))
                requested_end = int(match.group("end") or requested_start)
                if requested_start > len(lines) or requested_end < requested_start:
                    return None
                line_start = max(1, requested_start - self.line_radius)
                line_end = min(len(lines), requested_end + self.line_radius)
                excerpt_kind = "line_window"
            else:
                located = self._locate_pointer(lines, pointer)
                if located is None:
                    line_start = 1
                else:
                    line_start = max(1, located - self.line_radius)
                line_end = min(len(lines), line_start + self.line_radius * 2)
                excerpt_kind = "symbol_window" if located is not None else "text_window"
            excerpt = "\n".join(
                f"{number}: {lines[number - 1]}"
                for number in range(line_start, line_end + 1)
            )
            excerpt, truncated = _bounded(excerpt, maximum)

        digest_input = "\x1f".join(
            (
                source_path,
                pointer,
                evidence.source_type,
                evidence.trust_state,
                evidence.content_hash,
                excerpt_kind,
            )
        )
        chunk_id = "chunk:" + hashlib.sha256(
            digest_input.encode("utf-8")
        ).hexdigest()[:24]
        return EvidenceChunk(
            chunk_id=chunk_id,
            source_path=source_path,
            source_pointer=pointer,
            source_type=evidence.source_type,
            trust_state=evidence.trust_state,
            content_hash=evidence.content_hash,
            excerpt_kind=excerpt_kind,
            text=excerpt,
            owner_ids=owner_ids,
            line_start=line_start,
            line_end=line_end,
            truncated=truncated,
        )

    def _safe_path(self, source_path: str) -> Path | None:
        pure = PurePosixPath(source_path)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            return None
        lowered_parts = tuple(part.casefold() for part in pure.parts)
        name = lowered_parts[-1]
        pure_name = PurePosixPath(name)
        suffix = pure_name.suffix
        if (
            any(part in _DENIED_PARTS for part in lowered_parts)
            or any(
                part == ".env" or part.startswith(".env.")
                for part in lowered_parts
            )
            or name in _SECRET_NAMES
            or pure_name.stem in _SECRET_STEMS
            or suffix in _SECRET_SUFFIXES
            or suffix not in _TEXT_SUFFIXES
        ):
            return None
        try:
            candidate = (self.repository_root / Path(*pure.parts)).resolve(strict=True)
            candidate.relative_to(self.repository_root)
        except (OSError, RuntimeError, ValueError):
            return None
        return candidate if candidate.is_file() else None

    @staticmethod
    def _locate_pointer(lines: list[str], pointer: str) -> int | None:
        match = _SYMBOL_POINTER.search(pointer)
        if not match:
            return None
        symbol = match.group(1)
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        for index, line in enumerate(lines, start=1):
            if pattern.search(line):
                return index
        return None


__all__ = ["EvidenceChunk", "EvidenceCorpus"]
