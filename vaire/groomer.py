"""Data grooming engine for Vaire.

Provides audit, mutation, and automated workflow tools for a dedicated
groomer agent — a Claude Code instance that connects with role='groomer'
and gets access to these tools in addition to the normal agent tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from vaire.config import Settings
from vaire.storage import StorageEngine

logger = logging.getLogger(__name__)


class GroomerEngine:
    """Orchestrates all grooming audit and mutation operations."""

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: Any,
        cache: Any,          # MemoryCache — avoid circular import at module level
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._cache = cache
        self._settings = settings

    # ── Audit / browse tools ───────────────────────────────────────────────────

    def audit(
        self,
        directory: str | None = None,
        min_age_days: int | None = None,
        max_heat: float | None = None,
        tags: list[str] | None = None,
        store_type: str | None = None,
        provenance_agent: str | None = None,
        content_length_min: int | None = None,
        content_length_max: int | None = None,
        tags_empty: bool | None = None,
        id_range: list[int] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Browse the corpus with optional filters, ordered oldest/coldest first."""
        # Use groomer_search for the extended filters, fall back to get_memories_by_filter for basics
        if any(v is not None for v in (provenance_agent, content_length_min, content_length_max, tags_empty, id_range)):
            memories = self._storage.groomer_search(
                provenance_agent=provenance_agent,
                content_length_min=content_length_min,
                content_length_max=content_length_max,
                tags_empty=tags_empty,
                id_range=id_range,
                directory=directory,
                limit=limit,
            )
            return [self._summarise(m) for m in memories]
        memories = self._storage.get_memories_by_filter(
            directory=directory,
            min_age_days=min_age_days,
            max_heat=max_heat,
            tags=tags,
            store_type=store_type,
            limit=limit,
        )
        return [self._summarise(m) for m in memories]

    def inspect(self, memory_id: int) -> dict:
        """Return the full memory record including archive history."""
        mem = self._storage.get_memory_full(memory_id)
        if mem is None:
            return {"error": f"Memory {memory_id} not found"}
        # Strip all binary fields (not human-readable / not JSON-serializable)
        _BINARY_FIELDS = ("embedding", "hdc_vector", "implicit_embedding")
        for field in _BINARY_FIELDS:
            mem.pop(field, None)
        for archive in mem.get("archive_history", []):
            for field in _BINARY_FIELDS:
                archive.pop(field, None)
        return mem

    def find_duplicates(
        self,
        similarity_threshold: float = 0.85,
        directory: str | None = None,
        limit: int = 50,
    ) -> list[list[dict]]:
        """Return candidate duplicate groups."""
        return self._storage.get_duplicate_groups(
            threshold=similarity_threshold,
            directory=directory,
            limit=limit,
        )

    def find_contradictions(
        self,
        directory: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Scan for memories with negation mismatches or action divergence."""
        import re

        _NEGATION_RE = re.compile(
            r"\b(not|don't|doesn't|didn't|won't|can't|cannot|isn't|aren't|"
            r"no longer|instead of|rather than|replaced|switched from|stopped)\b",
            re.IGNORECASE,
        )
        _ACTION_RE = re.compile(r"\b(use|using|implement|choose|prefer|adopt)\b", re.IGNORECASE)
        # Stopwords excluded from topical overlap check
        _STOPWORDS = frozenset(
            "a an the is are was were be been being have has had do does did "
            "will would shall should may might can could not no and or but if "
            "then else when where how what which who whom this that these those "
            "it its i me my we our you your he him his she her they them their "
            "to of in for on with at by from as into through during before after "
            "above below between out off over under again further once here there "
            "all each every both few more most other some such than too very "
            "just also now so get got use using".split()
        )
        _MIN_CONTENT_LEN = 50
        _MIN_WORD_OVERLAP = 2

        def _content_words(text: str) -> set[str]:
            return {w for w in re.findall(r"\b[a-z][a-z0-9_.-]+\b", text.lower()) if w not in _STOPWORDS}

        memories = self._storage.get_memories_by_filter(directory=directory, limit=limit * 4)
        pairs: list[dict] = []

        # Pre-compute per-memory data
        mem_data: list[tuple[str, bool, set[str], set[str]]] = []
        for m in memories:
            c = m.get("content", "")
            mem_data.append((
                c,
                bool(_NEGATION_RE.search(c)),
                set(a.lower() for a in _ACTION_RE.findall(c)),
                _content_words(c),
            ))

        for i, m1 in enumerate(memories):
            c1, neg1, acts1, words1 = mem_data[i]
            if len(c1) < _MIN_CONTENT_LEN:
                continue

            for j in range(i + 1, len(memories)):
                c2, neg2, acts2, words2 = mem_data[j]
                if len(c2) < _MIN_CONTENT_LEN:
                    continue

                # Require topical overlap before checking heuristics
                shared_words = words1 & words2
                if len(shared_words) < _MIN_WORD_OVERLAP:
                    continue

                reason = None
                if neg1 != neg2:
                    reason = "negation_mismatch"
                elif acts1 and acts2 and acts1 != acts2:
                    shared = acts1 & acts2
                    if len(shared) < len(acts1 | acts2) * 0.5:
                        reason = "action_divergence"

                if reason:
                    pairs.append(
                        {
                            "memory_id_a": m1["id"],
                            "content_a": c1[:200],
                            "memory_id_b": memories[j]["id"],
                            "content_b": c2[:200],
                            "reason": reason,
                        }
                    )
                    if len(pairs) >= limit:
                        return pairs
        return pairs

    def find_orphans(
        self,
        directory: str | None = None,
        max_heat: float = 0.15,
        include_tagged: bool = False,
        min_content_length: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return low-connectivity memories (cold, optionally untagged)."""
        memories = self._storage.get_orphan_memories(
            directory=directory,
            max_heat=max_heat,
            include_tagged=include_tagged,
            min_content_length=min_content_length,
            limit=limit,
        )
        return [self._summarise(m) for m in memories]

    def find_stale(
        self,
        directory: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return memories flagged as stale (source file changed or deleted)."""
        stale = self._storage.get_stale_memories()
        if directory:
            stale = [m for m in stale if m.get("directory_context") == directory]
        return [self._summarise(m) for m in stale[:limit]]

    def get_stats(self, directory: str | None = None) -> dict:
        """Return grooming-specific corpus statistics."""
        all_mems = self._storage.get_memories_by_filter(directory=directory, limit=99999)
        total = len(all_mems)

        heat_buckets = {"0-0.2": 0, "0.2-0.5": 0, "0.5-0.8": 0, "0.8-1.0": 0}
        for m in all_mems:
            h = m.get("heat", 0)
            if h < 0.2:
                heat_buckets["0-0.2"] += 1
            elif h < 0.5:
                heat_buckets["0.2-0.5"] += 1
            elif h < 0.8:
                heat_buckets["0.5-0.8"] += 1
            else:
                heat_buckets["0.8-1.0"] += 1

        stale_count = len(self._storage.get_stale_memories())
        orphan_count = len(
            self._storage.get_orphan_memories(directory=directory, limit=99999)
        )
        duplicate_groups = self._storage.get_duplicate_groups(
            threshold=0.92, directory=directory, limit=200
        )
        duplicate_count = sum(len(g) - 1 for g in duplicate_groups)

        # Provenance breakdown (top 5 agents)
        agent_counts: dict[str, int] = {}
        for m in all_mems:
            agent = m.get("provenance_agent") or "unknown"
            agent_counts[agent] = agent_counts.get(agent, 0) + 1
        top_agents = sorted(agent_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "total_memories": total,
            "stale_count": stale_count,
            "orphan_count": orphan_count,
            "duplicate_count": duplicate_count,
            "duplicate_groups": len(duplicate_groups),
            "heat_distribution": heat_buckets,
            "top_provenance_agents": [{"agent": a, "count": c} for a, c in top_agents],
        }

    # ── Mutation tools ─────────────────────────────────────────────────────────

    def merge(
        self,
        memory_ids: list[int],
        merged_content: str,
        merged_tags: list[str],
        agent_id: str = "groomer",
    ) -> dict:
        """Merge N memories into one; archive the originals."""
        if len(memory_ids) < 2:
            return {"error": "merge requires at least 2 memory_ids"}

        # Determine best attributes from inputs
        inputs = [self._storage.get_memory(mid) for mid in memory_ids]
        inputs = [m for m in inputs if m is not None]
        if not inputs:
            return {"error": "No valid memories found for provided memory_ids"}

        best_heat = max(m.get("heat", 0) for m in inputs)
        all_tags = list(
            set(merged_tags)
            | {t for m in inputs for t in (m.get("tags") or [])}
        )

        embedding = self._embeddings.encode_document(merged_content)
        try:
            new_id = self._storage.insert_memory(
                {
                    "content": merged_content,
                    "embedding": embedding,
                    "tags": all_tags,
                    "directory_context": inputs[0].get("directory_context", ""),
                    "heat": best_heat,
                    "is_stale": False,
                    "provenance_agent": agent_id,
                }
            )
        except Exception as exc:
            logger.exception("merge: failed to insert merged memory")
            return {"error": f"merge failed during insert: {exc}"}

        archived = []
        for mid in memory_ids:
            try:
                self._storage.archive_memory(mid, replacement_id=new_id, reason="merged")
                self._storage.delete_memory(mid)
                self._invalidate_delete(mid)
                archived.append(mid)
            except Exception as exc:
                logger.exception("merge: failed to archive/delete memory %d; rolling back", mid)
                # Compensate: remove the newly created merged memory to avoid duplicates
                try:
                    self._storage.delete_memory(new_id)
                except Exception:
                    pass
                return {
                    "error": f"merge failed while archiving memory {mid}: {exc}; merged memory removed"
                }

        self._invalidate_upsert(new_id, merged_content, best_heat, all_tags, inputs[0].get("directory_context", ""))

        return {
            "new_memory_id": new_id,
            "archived_ids": archived,
            "merged_content_length": len(merged_content),
        }

    def split(
        self,
        memory_id: int,
        splits: list[dict],
        agent_id: str = "groomer",
    ) -> dict:
        """Split one memory into N; archive the original.

        Each split: {"content": str, "tags": list[str]}
        """
        if not splits:
            return {"error": "splits list is empty"}

        original = self._storage.get_memory(memory_id)
        if original is None:
            return {"error": f"Memory {memory_id} not found"}

        heat_each = original.get("heat", 1.0) / len(splits)
        directory = original.get("directory_context", "")

        new_ids: list[int] = []
        for piece in splits:
            content = piece.get("content", "")
            tags = piece.get("tags", [])
            embedding = self._embeddings.encode_document(content)
            try:
                nid = self._storage.insert_memory(
                    {
                        "content": content,
                        "embedding": embedding,
                        "tags": tags,
                        "directory_context": directory,
                        "heat": heat_each,
                        "is_stale": False,
                        "provenance_agent": agent_id,
                    }
                )
            except Exception as exc:
                logger.exception("split: failed to insert split piece; rolling back")
                for rollback_id in new_ids:
                    try:
                        self._storage.delete_memory(rollback_id)
                    except Exception:
                        pass
                return {"error": f"split failed during insert: {exc}; partial pieces removed"}
            new_ids.append(nid)
            self._invalidate_upsert(nid, content, heat_each, tags, directory)

        try:
            self._storage.archive_memory(memory_id, replacement_id=None, reason="split")
            self._storage.delete_memory(memory_id)
            self._invalidate_delete(memory_id)
        except Exception as exc:
            logger.exception("split: failed to archive/delete original memory %d", memory_id)
            # New pieces were created successfully; leave them and report the partial state
            return {
                "error": f"split created new pieces but failed to archive original {memory_id}: {exc}",
                "new_memory_ids": new_ids,
            }

        return {
            "archived_id": memory_id,
            "new_memory_ids": new_ids,
            "heat_each": round(heat_each, 3),
        }

    def retag(self, memory_id: int, new_tags: list[str]) -> dict:
        """Replace a memory's tags."""
        mem = self._storage.get_memory(memory_id)
        if mem is None:
            return {"error": f"Memory {memory_id} not found"}
        old_tags = mem.get("tags", [])
        self._storage.update_memory_full(memory_id, tags=new_tags)
        self._invalidate_upsert(
            memory_id,
            mem["content"],
            mem.get("heat", 1.0),
            new_tags,
            mem.get("directory_context", ""),
        )
        return {"memory_id": memory_id, "old_tags": old_tags, "new_tags": new_tags}

    def reclassify(self, memory_id: int, new_directory: str) -> dict:
        """Move a memory to a different directory context."""
        mem = self._storage.get_memory(memory_id)
        if mem is None:
            return {"error": f"Memory {memory_id} not found"}
        old_dir = mem.get("directory_context", "")
        self._storage.update_memory_full(memory_id, directory_context=new_directory)
        self._invalidate_upsert(
            memory_id,
            mem["content"],
            mem.get("heat", 1.0),
            mem.get("tags", []),
            new_directory,
        )
        return {
            "memory_id": memory_id,
            "old_directory": old_dir,
            "new_directory": new_directory,
        }

    def update_content(self, memory_id: int, new_content: str) -> dict:
        """Rewrite a memory's content; re-embeds automatically."""
        mem = self._storage.get_memory(memory_id)
        if mem is None:
            return {"error": f"Memory {memory_id} not found"}
        old_len = len(mem.get("content", ""))
        embedding = self._embeddings.encode_document(new_content)
        self._storage.update_memory_full(
            memory_id, content=new_content, embedding=embedding
        )
        self._invalidate_upsert(
            memory_id,
            new_content,
            mem.get("heat", 1.0),
            mem.get("tags", []),
            mem.get("directory_context", ""),
        )
        return {
            "memory_id": memory_id,
            "old_content_length": old_len,
            "new_content_length": len(new_content),
        }

    def promote(self, memory_id: int) -> dict:
        """Boost a memory: heat=1.0, protected, _anchor tag."""
        self._storage.promote_memory(memory_id)
        mem = self._storage.get_memory(memory_id)
        if mem:
            from vaire.cache import InvalidationEvent
            if self._cache is not None:
                self._cache.invalidate(
                    InvalidationEvent.HEAT_UPDATE,
                    id=memory_id,
                    new_heat=1.0,
                )
        return {"memory_id": memory_id, "status": "promoted"}

    def demote(self, memory_id: int) -> dict:
        """Demote a memory: heat=0.01, unprotected."""
        self._storage.demote_memory(memory_id)
        if self._cache is not None:
            from vaire.cache import InvalidationEvent
            self._cache.invalidate(
                InvalidationEvent.HEAT_UPDATE,
                id=memory_id,
                new_heat=0.01,
            )
        return {"memory_id": memory_id, "status": "demoted"}

    _VALID_FILTER_KEYS = frozenset(
        {"directory", "min_age_days", "max_heat", "tags", "store_type", "compression_level", "limit"}
    )

    def bulk_delete(self, filter: dict) -> dict:
        """Delete memories matching filter dict (at least one criterion required)."""
        clean = {k: v for k, v in filter.items() if k in self._VALID_FILTER_KEYS}
        try:
            count = self._storage.bulk_delete_by_filter(clean)
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}
        return {"deleted_count": count}

    # ── Automated workflow ─────────────────────────────────────────────────────

    def auto_groom(
        self,
        directory: str | None = None,
        depth: str = "light",
    ) -> dict:
        """Run a structured grooming pass.

        depth='light'  — find duplicates, stale, orphans; auto-delete exact dupes
        depth='medium' — light + contradictions + retag suggestions
        depth='deep'   — medium + merge candidates
        """
        report: dict[str, Any] = {
            "depth": depth,
            "directory": directory,
            "found_duplicates": 0,
            "found_stale": 0,
            "found_orphans": 0,
            "found_contradictions": 0,
            "auto_executed": 0,
            "recommendations": [],
        }

        # ── duplicates ──────────────────────────────────────────────────────
        dup_groups = self._storage.get_duplicate_groups(
            threshold=0.85, directory=directory, limit=100
        )
        report["found_duplicates"] = sum(len(g) - 1 for g in dup_groups)

        # Auto-execute: delete exact duplicates (sim >= 0.98)
        for group in dup_groups:
            exact = [g for g in group if g["similarity_to_anchor"] >= 0.98]
            for item in exact[1:]:  # keep anchor, delete rest
                mid = item["memory_id"]
                self._storage.archive_memory(mid, reason="auto_dedup")
                self._storage.delete_memory(mid)
                self._invalidate_delete(mid)
                report["auto_executed"] += 1

        # ── stale ───────────────────────────────────────────────────────────
        stale = self.find_stale(directory=directory, limit=50)
        report["found_stale"] = len(stale)

        # Auto-execute: remove confirmed stale (skip protected memories)
        for mem_summary in stale:
            mid = mem_summary["id"]
            full = self._storage.get_memory(mid)
            if full and full.get("is_protected"):
                continue
            self._storage.archive_memory(mid, reason="auto_stale")
            self._storage.delete_memory(mid)
            self._invalidate_delete(mid)
            report["auto_executed"] += 1

        # ── orphans ─────────────────────────────────────────────────────────
        orphans = self.find_orphans(directory=directory, limit=50)
        report["found_orphans"] = len(orphans)
        if orphans:
            report["recommendations"].append(
                f"Review {len(orphans)} orphan memories (cold, untagged)"
            )

        if depth in ("medium", "deep"):
            # ── contradictions ───────────────────────────────────────────────
            contras = self.find_contradictions(directory=directory, limit=30)
            report["found_contradictions"] = len(contras)
            if contras:
                report["recommendations"].append(
                    f"Resolve {len(contras)} potential contradictions"
                )

            # ── near-duplicate merge candidates ──────────────────────────────
            near_dups = [
                g for g in dup_groups
                if any(0.85 <= item["similarity_to_anchor"] < 0.98 for item in g[1:])
            ]
            if near_dups:
                report["recommendations"].append(
                    f"{len(near_dups)} near-duplicate groups warrant manual merge review"
                )

        if depth == "deep":
            report["recommendations"].append(
                "Deep mode: review orphan memories for content quality improvements"
            )

        return report

    # ── New groomer tools (Vale feature requests, 2026-03-26) ───────────────────

    def forget(self, memory_ids: list[int], reason: str = "groomer") -> dict:
        """Delete memories by ID list, archiving each to grooming_archives first."""
        if not memory_ids:
            return {"error": "memory_ids list is empty"}
        archived = []
        not_found = []
        for mid in memory_ids:
            mem = self._storage.get_memory(mid)
            if mem is None:
                not_found.append(mid)
                continue
            self._storage.archive_memory(mid, replacement_id=None, reason=reason)
            self._storage.delete_memory(mid)
            self._invalidate_delete(mid)
            archived.append(mid)
        return {"archived": archived, "not_found": not_found, "reason": reason}

    def search(
        self,
        provenance_agent: str | None = None,
        content_like: str | None = None,
        content_length_min: int | None = None,
        content_length_max: int | None = None,
        tags_empty: bool | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        id_range: list[int] | None = None,
        directory: str | None = None,
        limit: int = 50,
        fields: list[str] | None = None,
    ) -> list[dict]:
        """Raw filtered query — no embedding search, no reranking."""
        return self._storage.groomer_search(
            provenance_agent=provenance_agent,
            content_like=content_like,
            content_length_min=content_length_min,
            content_length_max=content_length_max,
            tags_empty=tags_empty,
            created_before=created_before,
            created_after=created_after,
            id_range=id_range,
            directory=directory,
            limit=limit,
            fields=fields,
        )

    def bulk_retag(
        self,
        filter: dict,
        new_tags: list[str],
        mode: str = "replace",
    ) -> dict:
        """Retag multiple memories by filter. mode: replace|append|remove."""
        if mode not in ("replace", "append", "remove"):
            return {"error": f"Invalid mode: {mode!r}. Must be replace, append, or remove."}
        memories = self._storage.get_memories_by_filter(**{
            k: v for k, v in filter.items()
            if k in self._VALID_FILTER_KEYS
        })
        updated = 0
        for mem in memories:
            old_tags = mem.get("tags") or []
            if mode == "replace":
                final_tags = list(new_tags)
            elif mode == "append":
                final_tags = list(set(old_tags) | set(new_tags))
            else:  # remove
                final_tags = [t for t in old_tags if t not in new_tags]
            self._storage.update_memory_full(mem["id"], tags=final_tags)
            self._invalidate_upsert(
                mem["id"], mem["content"], mem.get("heat", 0),
                final_tags, mem.get("directory_context", ""),
            )
            updated += 1
        return {"updated": updated, "mode": mode, "new_tags": new_tags}

    def content_scan(
        self,
        pattern: str,
        replacement: str | None = None,
        dry_run: bool = True,
    ) -> dict:
        """Scan all memories for regex pattern. Optionally replace (re-embeds)."""
        import re as _re
        try:
            regex = _re.compile(pattern)
        except _re.error as exc:
            return {"error": f"Invalid regex: {exc}"}

        all_mems = self._storage.get_memories_by_filter(limit=99999)
        matches: list[dict] = []
        replaced = 0

        for mem in all_mems:
            content = mem.get("content", "")
            found = list(regex.finditer(content))
            if not found:
                continue
            match_info = {
                "memory_id": mem["id"],
                "matches": [{"text": m.group(), "start": m.start(), "end": m.end()} for m in found[:5]],
                "match_count": len(found),
            }
            matches.append(match_info)

            if replacement is not None and not dry_run:
                new_content = regex.sub(replacement, content)
                embedding = self._embeddings.encode_document(new_content)
                self._storage.update_memory_full(
                    mem["id"], content=new_content, embedding=embedding,
                )
                self._invalidate_upsert(
                    mem["id"], new_content, mem.get("heat", 0),
                    mem.get("tags", []), mem.get("directory_context", ""),
                )
                replaced += 1

        result: dict = {
            "pattern": pattern,
            "total_matches": len(matches),
            "dry_run": dry_run,
            "matches": matches[:100],
        }
        if replacement is not None:
            result["replacement"] = replacement
            result["replaced"] = replaced
        return result

    def bulk_update_content(
        self,
        memory_ids: list[int],
        find: str,
        replace: str,
    ) -> dict:
        """Find/replace across specified memory IDs. Re-embeds all affected memories."""
        if not memory_ids:
            return {"error": "memory_ids list is empty"}
        updated = []
        not_found = []
        for mid in memory_ids:
            mem = self._storage.get_memory(mid)
            if mem is None:
                not_found.append(mid)
                continue
            content = mem.get("content", "")
            if find not in content:
                continue
            new_content = content.replace(find, replace)
            embedding = self._embeddings.encode_document(new_content)
            self._storage.update_memory_full(mid, content=new_content, embedding=embedding)
            self._invalidate_upsert(
                mid, new_content, mem.get("heat", 0),
                mem.get("tags", []), mem.get("directory_context", ""),
            )
            updated.append(mid)
        return {"updated": updated, "not_found": not_found}

    def provenance(self, directory: str | None = None) -> list[dict]:
        """List distinct provenance_agent values with counts, date ranges, top tags."""
        return self._storage.groomer_provenance(directory=directory)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _summarise(self, m: dict) -> dict:
        """Trim a memory row for groomer audit output."""
        return {
            "id": m.get("id"),
            "content_preview": (m.get("content") or "")[:200],
            "heat": m.get("heat"),
            "created_at": m.get("created_at"),
            "directory": m.get("directory_context"),
            "tags": m.get("tags", []),
            "store_type": m.get("store_type"),
            "compression_level": m.get("compression_level"),
        }

    def _invalidate_upsert(
        self,
        memory_id: int,
        content: str,
        heat: float,
        tags: list[str],
        project_dir: str,
    ) -> None:
        if self._cache is None:
            return
        try:
            from vaire.cache import InvalidationEvent
            import numpy as np
            mem = self._storage.get_memory(memory_id)
            embedding = None
            if mem and mem.get("embedding"):
                embedding = np.frombuffer(bytes(mem["embedding"]), dtype=np.float32)
            self._cache.invalidate(
                InvalidationEvent.MEMORY_UPSERT,
                id=memory_id,
                content=content,
                heat=heat,
                embedding=embedding,
                tags=tags,
                project_dir=project_dir,
                created_at=mem.get("created_at", "") if mem else "",
                last_accessed=mem.get("last_accessed", "") if mem else "",
            )
        except Exception:
            logger.debug("Cache invalidation skipped (cache not warmed up)")

    def _invalidate_delete(self, memory_id: int) -> None:
        if self._cache is None:
            return
        try:
            from vaire.cache import InvalidationEvent
            self._cache.invalidate(InvalidationEvent.MEMORY_DELETE, id=memory_id)
        except Exception:
            logger.debug("Cache delete invalidation skipped")
