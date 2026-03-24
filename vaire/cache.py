"""
In-memory read cache for the Vaire shared memory server.

Responsibility: fast recall on the warm path — no DB hit when warm.
  - vector_search() uses numpy brute-force dot-product (replaces sqlite-vec).
  - invalidate() keeps all three tiers consistent after every write.

SRP boundary: NO write logic lives here. The cache reads from StorageEngine
on warmup and accepts invalidation notifications from the write path only.

Three-tier layout
─────────────────
Tier 1 — always loaded at warmup, updated on every invalidation:
  _memories        dict[int, CachedMemory]     id → memory
  _content_index   dict[str, int]              sha256(content) → id (dedup)
  _project_index   dict[str, list[int]]        project_dir → [id, ...]
  _entities        dict[str, dict]             entity name → entity row
  _rules           list[dict]                  active rules, priority DESC

Tier 2 — lazily rebuilt whenever _dirty_tier2 is True:
  _embedding_matrix   np.ndarray | None        (N, D) float32, L2-normalised
  _memory_id_index    list[int]                row i → memory_id

Tier 3 — reserved for Phase 3+ (SR matrix, Hopfield patterns, HDC vectors).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .storage import StorageEngine

logger = logging.getLogger(__name__)

# Embedding dimension used by all-MiniLM-L6-v2 (default model).
_EMBEDDING_DIM = 384


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class CachedMemory:
    """Lightweight in-memory representation of a single memory row."""

    id: int
    content: str
    heat: float
    embedding: np.ndarray | None  # shape (D,) float32, or None if not yet embedded
    tags: list[str]
    project_dir: str
    created_at: str
    last_accessed: str


# ── Invalidation events ────────────────────────────────────────────────────────

class InvalidationEvent(Enum):
    """All event types that can mutate cache state."""

    MEMORY_UPSERT = auto()        # new memory stored or existing one updated
    MEMORY_DELETE = auto()        # memory permanently deleted
    HEAT_UPDATE = auto()          # heat-only update (no content/embedding change)
    ENTITY_UPSERT = auto()        # entity created or updated
    RELATIONSHIP_UPSERT = auto()  # relationship created or reinforced
    RULE_UPSERT = auto()          # memory rule created, updated, or deleted


# ── Cache ──────────────────────────────────────────────────────────────────────

class MemoryCache:
    """Shared in-process read cache for all connected Vaire clients.

    Thread/concurrency safety: all methods are called from a single asyncio
    event loop (the socket server's loop). No locks are needed.
    """

    def __init__(self, storage: StorageEngine) -> None:
        self._storage = storage

        # Tier 1 — always loaded
        self._memories: dict[int, CachedMemory] = {}
        self._content_index: dict[str, int] = {}      # sha256(content) → id
        self._project_index: dict[str, list[int]] = {} # project_dir → [ids]
        self._entities: dict[str, dict[str, Any]] = {} # entity name → row
        self._rules: list[dict[str, Any]] = []

        # Tier 2 — lazy, rebuilt on dirty flag
        self._embedding_matrix: np.ndarray | None = None  # (N, D) float32
        self._memory_id_index: list[int] = []             # row i → memory_id
        self._dirty_tier2: bool = True

    # ── Warmup ────────────────────────────────────────────────────────────────

    async def warmup(self) -> None:
        """Load all three Tier-1 data sets concurrently.

        Logs a warning if the total time exceeds 1 second (signals DB health
        issue or unexpectedly large dataset at startup).
        """
        t0 = time.monotonic()
        await asyncio.gather(
            asyncio.to_thread(self._load_memories),
            asyncio.to_thread(self._load_entities),
            asyncio.to_thread(self._load_rules),
        )
        elapsed = time.monotonic() - t0
        logger.info(
            "Cache warmed up in %.3fs: %d memories, %d entities, %d rules",
            elapsed,
            len(self._memories),
            len(self._entities),
            len(self._rules),
        )
        if elapsed >= 1.0:
            logger.warning(
                "Cache warmup took %.3fs — consider archiving cold memories "
                "or checking DB performance",
                elapsed,
            )

    def _load_memories(self) -> None:
        """Populate Tier-1 memory maps from storage. Sets _dirty_tier2."""
        rows = self._storage.get_all_memories_for_cache()
        self._memories.clear()
        self._content_index.clear()
        self._project_index.clear()

        for row in rows:
            mem_id: int = row["id"]
            content: str = row["content"]
            project_dir: str = row["directory_context"]
            tags_raw = row.get("tags", "[]") or "[]"

            # Decode tags (stored as JSON string)
            try:
                import json as _json
                tags: list[str] = _json.loads(tags_raw)
                if not isinstance(tags, list):
                    tags = []
            except Exception:
                tags = []

            # Decode embedding BLOB → float32 ndarray
            embedding: np.ndarray | None = None
            raw_blob = row.get("embedding")
            if raw_blob is not None:
                try:
                    embedding = np.frombuffer(raw_blob, dtype=np.float32).copy()
                except Exception:
                    embedding = None

            cm = CachedMemory(
                id=mem_id,
                content=content,
                heat=float(row.get("heat", 1.0)),
                embedding=embedding,
                tags=tags,
                project_dir=project_dir,
                created_at=row.get("created_at", ""),
                last_accessed=row.get("last_accessed", ""),
            )
            self._memories[mem_id] = cm
            self._content_index[_sha256(content)] = mem_id
            self._project_index.setdefault(project_dir, []).append(mem_id)

        self._dirty_tier2 = True

    def _load_entities(self) -> None:
        """Populate Tier-1 entity map from storage."""
        rows = self._storage.get_all_entities()
        self._entities = {row["name"]: row for row in rows}

    def _load_rules(self) -> None:
        """Populate Tier-1 rules list from storage (priority DESC)."""
        self._rules = self._storage.get_active_rules()

    # ── Tier-2 rebuild ────────────────────────────────────────────────────────

    def _build_embedding_matrix(self) -> None:
        """Stack all available embeddings into (N, D) float32 matrix.

        Memories without an embedding are skipped — they participate in
        Tier-1 lookups (content/project index) but not in vector search.
        After building, L2-normalises each row so dot-product == cosine sim.
        """
        ids: list[int] = []
        vecs: list[np.ndarray] = []

        for mem_id, cm in self._memories.items():
            if cm.embedding is not None and cm.embedding.size > 0:
                ids.append(mem_id)
                vecs.append(cm.embedding)

        if not vecs:
            self._embedding_matrix = None
            self._memory_id_index = []
            self._dirty_tier2 = False
            return

        matrix = np.stack(vecs, axis=0).astype(np.float32)  # (N, D)

        # L2-normalise rows so dot-product == cosine similarity
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)  # avoid zero-division
        matrix /= norms

        self._embedding_matrix = matrix
        self._memory_id_index = ids
        self._dirty_tier2 = False

        logger.debug(
            "Embedding matrix built: %d vectors, dim=%d",
            matrix.shape[0],
            matrix.shape[1],
        )

    # ── Vector search ─────────────────────────────────────────────────────────

    def vector_search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 10,
        project_dir: str | None = None,
    ) -> list[tuple[int, float]]:
        """Return top-k (memory_id, score) pairs via brute-force cosine sim.

        Steps:
          1. Rebuild embedding matrix if stale.
          2. If matrix is empty, return [].
          3. If project_dir given, restrict candidates via _project_index.
          4. L2-normalise query_embedding.
          5. Dot-product scores = matrix[candidates] @ query_normalised.
          6. argpartition(-actual_k) + argsort for ordered top-k.
          7. Return [(memory_id, score), ...] sorted score DESC.
        """
        # Step 1 — rebuild if stale
        if self._dirty_tier2:
            self._build_embedding_matrix()

        # Step 2 — empty matrix shortcut
        if self._embedding_matrix is None or len(self._memory_id_index) == 0:
            return []

        # Step 3 — project filter
        if project_dir is not None:
            allowed_ids = set(self._project_index.get(project_dir, []))
            if not allowed_ids:
                return []
            candidate_rows = [
                i for i, mid in enumerate(self._memory_id_index)
                if mid in allowed_ids
            ]
            if not candidate_rows:
                return []
            sub_matrix = self._embedding_matrix[candidate_rows]  # (M, D)
            sub_ids = [self._memory_id_index[i] for i in candidate_rows]
        else:
            sub_matrix = self._embedding_matrix
            sub_ids = self._memory_id_index

        # Step 4 — L2-normalise query
        q = query_embedding.astype(np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return []
        q = q / q_norm

        # Step 5 — dot-product scores
        scores = sub_matrix @ q  # (M,)

        # Step 6 — top-k selection
        actual_k = min(top_k, len(sub_ids))
        if actual_k == len(sub_ids):
            # Fewer candidates than k — sort all
            sorted_indices = np.argsort(-scores)
        else:
            top_indices = np.argpartition(-scores, actual_k)[:actual_k]
            sorted_indices = top_indices[np.argsort(-scores[top_indices])]

        # Step 7 — build result
        return [
            (sub_ids[i], float(scores[i]))
            for i in sorted_indices
        ]

    # ── Invalidation ──────────────────────────────────────────────────────────

    def invalidate(self, event: InvalidationEvent, **kwargs: Any) -> None:
        """Update cache state after a write-path mutation.

        All six InvalidationEvent branches are handled:

        MEMORY_UPSERT (id, content, heat, embedding, tags, project_dir, ...):
          - Upsert CachedMemory in _memories.
          - Update _content_index (remove old sha256 key if content changed).
          - Update _project_index (move between project buckets if needed).
          - Mark _dirty_tier2 = True (embedding matrix may have changed).

        MEMORY_DELETE (id):
          - Remove from _memories, _content_index, _project_index.
          - Mark _dirty_tier2 = True.

        HEAT_UPDATE (id, new_heat):
          - Update only CachedMemory.heat; no tier-2 rebuild needed.

        ENTITY_UPSERT (name, row):
          - Upsert _entities[name].

        RELATIONSHIP_UPSERT:
          - No-op in Phase 2; entity graph is Phase 3+.

        RULE_UPSERT (rules):
          - Replace _rules list entirely with the provided list.
        """
        if event is InvalidationEvent.MEMORY_UPSERT:
            self._invalidate_memory_upsert(**kwargs)

        elif event is InvalidationEvent.MEMORY_DELETE:
            self._invalidate_memory_delete(**kwargs)

        elif event is InvalidationEvent.HEAT_UPDATE:
            mem_id = kwargs.get("id")
            new_heat = kwargs.get("new_heat")
            if mem_id is not None and new_heat is not None and mem_id in self._memories:
                self._memories[mem_id].heat = float(new_heat)

        elif event is InvalidationEvent.ENTITY_UPSERT:
            name = kwargs.get("name")
            row = kwargs.get("row")
            if name is not None and row is not None:
                self._entities[name] = row

        elif event is InvalidationEvent.RELATIONSHIP_UPSERT:
            pass  # entity graph managed in Phase 3+

        elif event is InvalidationEvent.RULE_UPSERT:
            self._rules = list(kwargs.get("rules", []))

    def _invalidate_memory_upsert(
        self,
        id: int,
        content: str,
        heat: float,
        embedding: np.ndarray | None = None,
        tags: list[str] | None = None,
        project_dir: str = "",
        created_at: str = "",
        last_accessed: str = "",
        **_ignored: Any,
    ) -> None:
        existing = self._memories.get(id)

        # Remove stale content_index entry if content changed
        if existing is not None and existing.content != content:
            old_key = _sha256(existing.content)
            if self._content_index.get(old_key) == id:
                del self._content_index[old_key]

        # Remove from old project bucket if project_dir changed
        if existing is not None and existing.project_dir != project_dir:
            old_bucket = self._project_index.get(existing.project_dir, [])
            if id in old_bucket:
                old_bucket.remove(id)

        cm = CachedMemory(
            id=id,
            content=content,
            heat=float(heat),
            embedding=embedding,
            tags=tags or [],
            project_dir=project_dir,
            created_at=created_at,
            last_accessed=last_accessed,
        )
        self._memories[id] = cm
        self._content_index[_sha256(content)] = id
        self._project_index.setdefault(project_dir, [])
        if id not in self._project_index[project_dir]:
            self._project_index[project_dir].append(id)
        self._dirty_tier2 = True

    def _invalidate_memory_delete(self, id: int, **_ignored: Any) -> None:
        cm = self._memories.pop(id, None)
        if cm is not None:
            key = _sha256(cm.content)
            if self._content_index.get(key) == id:
                del self._content_index[key]
            bucket = self._project_index.get(cm.project_dir, [])
            if id in bucket:
                bucket.remove(id)
        self._dirty_tier2 = True

    # ── Read helpers ──────────────────────────────────────────────────────────

    def get_memory(self, memory_id: int) -> CachedMemory | None:
        """Return a cached memory by id, or None if not found."""
        return self._memories.get(memory_id)

    def get_memories_for_project(self, project_dir: str) -> list[CachedMemory]:
        """Return all cached memories for a given project directory."""
        ids = self._project_index.get(project_dir, [])
        return [self._memories[i] for i in ids if i in self._memories]

    def content_hash_exists(self, content: str) -> bool:
        """Return True if a memory with this exact content is already cached."""
        return _sha256(content) in self._content_index

    @property
    def memory_count(self) -> int:
        return len(self._memories)

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    @property
    def embedded_count(self) -> int:
        return sum(1 for cm in self._memories.values() if cm.embedding is not None)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
