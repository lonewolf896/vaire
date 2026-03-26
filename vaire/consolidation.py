"""Astrocyte consolidation engine — background daemon that processes memories during idle time."""

import logging
import re
import threading
import time
from datetime import datetime, timezone
from itertools import combinations

from vaire.cls_store import DualStoreCLS
from vaire.compression import MemoryCompressor
from vaire.config import Settings
from vaire.curation import MemoryCurator
from vaire.embeddings import EmbeddingEngine
from vaire.knowledge_graph import KnowledgeGraph
from vaire.sleep_compute import SleepComputeEngine
from vaire.storage import StorageEngine
from vaire.thermodynamics import MemoryThermodynamics

# Lazy imports to avoid circular dependencies
_AstrocytePool = None
_CausalDiscovery = None


def _get_pool_class():
    global _AstrocytePool
    if _AstrocytePool is None:
        from vaire.astrocyte_pool import AstrocytePool
        _AstrocytePool = AstrocytePool
    return _AstrocytePool


def _get_causal_discovery_class():
    global _CausalDiscovery
    if _CausalDiscovery is None:
        from vaire.causal_discovery import CausalDiscovery
        _CausalDiscovery = CausalDiscovery
    return _CausalDiscovery

logger = logging.getLogger(__name__)

# Regex patterns for entity extraction
_FILE_PATH_RE = re.compile(
    r"(?:\.{0,2}/)?(?:[\w@.-]+/)+[\w@.-]+\.\w+"
)
_PYTHON_DEF_RE = re.compile(r"\b(def|class)\s+(\w+)")
_JS_FUNCTION_RE = re.compile(r"\bfunction\s+(\w+)")
_ERROR_RE = re.compile(r"\b(\w*(?:Error|Exception))\b")
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
_IMPORT_RE = re.compile(r"(?:^|\n)\s*import\s+([\w.]+)")
_FROM_IMPORT_RE = re.compile(r"(?:^|\n)\s*from\s+([\w.]+)\s+import")
_REQUIRE_RE = re.compile(r"require\(['\"]([^'\"]+)['\"]\)")
_DECISION_RE = re.compile(
    r"(?:decided|chose|choosing|using|switched to|migrated to|replaced with)"
    r"\s+(\w+(?:\s+\w+){0,3})",
    re.IGNORECASE,
)

_CODE_EXTENSIONS = frozenset((
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".rb", ".toml", ".yaml", ".yml", ".json", ".md", ".txt", ".cfg",
    ".ini", ".sh", ".css", ".html", ".sql", ".proto",
))


class AstrocyteEngine:
    """Background consolidation daemon inspired by astrocyte glial cells.

    Wakes up after a period of user inactivity to:
    - Apply thermodynamic decay to memory/entity heat values
    - Extract entities from new episodes and build the knowledge graph
    - Merge near-duplicate memories
    """

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        settings: Settings,
        write_gate=None,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._settings = settings
        self._write_gate = write_gate
        self._thermo = MemoryThermodynamics(storage, embeddings, settings)
        self._graph = KnowledgeGraph(storage, settings)
        self._curator = MemoryCurator(storage, embeddings, self._thermo, settings)
        self._sleep_engine = SleepComputeEngine(
            storage, embeddings, self._graph, self._curator, self._thermo, settings
        )
        self._cls = DualStoreCLS(storage, embeddings, settings)
        self._compressor = MemoryCompressor(storage, embeddings, settings)
        self._last_sleep_cycle: datetime | None = self._load_last_sleep_cycle()
        self._last_light_cycle: datetime | None = None
        self._last_medium_cycle: datetime | None = None
        self._last_full_activity: datetime | None = None  # activity stamp when full cycle last ran

        self.last_activity: datetime = datetime.now(timezone.utc)
        self.is_running: bool = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = paused
        self._last_consolidated_episode_id: int = 0

        # Initialize causal discovery engine
        self._causal_discovery = None
        self._events_since_last_discovery = 0
        try:
            CausalDiscoveryCls = _get_causal_discovery_class()
            self._causal_discovery = CausalDiscoveryCls(
                storage, self._graph, settings
            )
        except Exception:
            logger.exception("Failed to initialize CausalDiscovery")

        # Initialize astrocyte pool for domain-aware consolidation
        self._pool = None
        try:
            PoolCls = _get_pool_class()
            self._pool = PoolCls(
                storage, embeddings, self._graph, self._thermo, settings
            )
            self._pool.init_processes()
        except Exception:
            logger.exception("Failed to initialize AstrocytePool")

    # -- Public API --

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._last_consolidated_episode_id = self._storage.get_max_episode_id()
        self._thread = threading.Thread(target=self._daemon_loop, daemon=True)
        self.is_running = True
        self._thread.start()
        logger.info("Astrocyte daemon started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.is_running = False
        self._thread = None
        logger.info("Astrocyte daemon stopped")

    def record_activity(self) -> None:
        self.last_activity = datetime.now(timezone.utc)

    def _load_last_sleep_cycle(self) -> datetime | None:
        """Load last sleep cycle timestamp from DB to survive restarts."""
        try:
            row = self._storage._conn.execute(
                "SELECT value FROM metadata WHERE key = 'last_sleep_cycle'"
            ).fetchone()
            if row:
                dt = datetime.fromisoformat(row[0])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
        except Exception:
            pass
        return None

    def _save_last_sleep_cycle(self, ts: datetime) -> None:
        """Persist last sleep cycle timestamp to DB."""
        try:
            self._storage._conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                ("last_sleep_cycle", ts.isoformat()),
            )
            self._storage._conn.commit()
        except Exception:
            logger.debug("Could not persist last_sleep_cycle (metadata table may not exist)")

    def pause(self) -> None:
        """Pause the consolidation daemon (e.g. during bulk ingestion)."""
        self._pause_event.set()
        logger.debug("Astrocyte daemon paused")

    def resume(self) -> None:
        """Resume the consolidation daemon after a pause."""
        self._pause_event.clear()
        logger.debug("Astrocyte daemon resumed")

    def force_consolidate(self) -> dict:
        """Run a consolidation cycle immediately. Returns the cycle stats."""
        return self._consolidation_cycle()

    # -- Daemon loop --

    def _daemon_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._settings.DAEMON_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            if self._pause_event.is_set():
                continue

            now = datetime.now(timezone.utc)
            elapsed = (now - self.last_activity).total_seconds()

            # Light cycle (every 60s): decay + action log processing
            light_interval = self._settings.ACTION_LOG_INTERVAL
            if self._last_light_cycle is None or (
                (now - self._last_light_cycle).total_seconds() >= light_interval
            ):
                try:
                    self._light_cycle()
                    self._last_light_cycle = now
                except Exception:
                    logger.exception("Light cycle failed")

            # Medium cycle (every 15min): + entity extraction + merge
            medium_interval = self._settings.MEDIUM_CYCLE_INTERVAL
            if self._last_medium_cycle is None or (
                (now - self._last_medium_cycle).total_seconds() >= medium_interval
            ):
                try:
                    self._medium_cycle()
                    self._last_medium_cycle = now
                except Exception:
                    logger.exception("Medium cycle failed")

            # Full cycle: + causal discovery + memify + CLS + compression
            # Only on idle, and only if there's been new activity since last run
            if elapsed > self._settings.IDLE_THRESHOLD_SECONDS:
                if self._last_full_activity != self.last_activity:
                    try:
                        self._consolidation_cycle()
                        self._last_full_activity = self.last_activity
                    except Exception:
                        logger.exception("Full consolidation cycle failed")
                # Extended idle: trigger sleep cycle (also gated by min gap)
                if elapsed > 2 * self._settings.IDLE_THRESHOLD_SECONDS:
                    self._maybe_sleep_cycle()

    def _maybe_sleep_cycle(self) -> None:
        """Run a full sleep cycle if minimum gap has elapsed."""
        now = datetime.now(timezone.utc)
        min_gap = self._settings.SLEEP_CYCLE_MIN_GAP_HOURS
        if self._last_sleep_cycle is not None:
            hours_since = (now - self._last_sleep_cycle).total_seconds() / 3600.0
            if hours_since < min_gap:
                return
        try:
            stats = self._sleep_engine.run_sleep_cycle()
            self._last_sleep_cycle = now
            self._save_last_sleep_cycle(now)
            logger.info("Sleep cycle complete: %s", stats)
        except Exception:
            logger.exception("Sleep cycle failed")

    # -- Light cycle (runs during activity) --

    def _light_cycle(self) -> dict:
        """Lightweight cycle: action log processing + decay only.

        Safe to run while agents are active. Skips the heavier phases
        (entity extraction, merge, dedup, memify, sleep).
        """
        start = time.monotonic()
        stats = {
            "memories_updated": 0,
            "memories_archived": 0,
            "action_log": {},
        }
        self._apply_decay(stats)
        stats["action_log"] = self._process_action_log()
        elapsed_ms = (time.monotonic() - start) * 1000
        if stats["action_log"].get("memories_created", 0) > 0 or stats["memories_archived"] > 0:
            logger.info(
                "Light cycle: %d action outcomes stored, %d archived (%.0fms)",
                stats["action_log"].get("memories_created", 0),
                stats["memories_archived"],
                elapsed_ms,
            )
        return stats

    # -- Medium cycle (runs during activity, every 15min) --

    def _medium_cycle(self) -> dict:
        """Medium-weight cycle: entity extraction + merge.

        Keeps the knowledge graph fresh while agents are active.
        Skips the heaviest phases (causal discovery, memify, CLS,
        compression, sleep) which only run on idle.
        """
        start = time.monotonic()
        stats = {
            "memories_added": 0,
            "memories_updated": 0,
            "memories_archived": 0,
            "memories_deleted": 0,
        }
        self._process_new_episodes(stats)
        self._merge_duplicates(stats)
        elapsed_ms = (time.monotonic() - start) * 1000
        if stats["memories_added"] > 0 or stats["memories_deleted"] > 0:
            logger.info(
                "Medium cycle: %d entities extracted, %d duplicates merged (%.0fms)",
                stats["memories_added"],
                stats["memories_deleted"],
                elapsed_ms,
            )
        return stats

    # -- Core consolidation --

    def _consolidation_cycle(self) -> dict:
        start = time.monotonic()
        stats = {
            "memories_added": 0,
            "memories_updated": 0,
            "memories_archived": 0,
            "memories_deleted": 0,
        }

        self._apply_decay(stats)
        self._process_new_episodes(stats)
        self._merge_duplicates(stats)

        try:
            self._graph.detect_causality()
        except Exception:
            logger.exception("Causal detection failed")

        # Run formal causal discovery (PC algorithm) periodically
        if self._causal_discovery is not None:
            self._events_since_last_discovery += stats.get("memories_added", 0)
            if self._events_since_last_discovery >= 50:
                try:
                    dag = self._causal_discovery.discover_dag()
                    stats["causal_dag_edges"] = dag.get("metadata", {}).get(
                        "directed_count", 0
                    )
                    self._events_since_last_discovery = 0
                except Exception:
                    logger.exception("Causal discovery failed")

        # Run domain-specific consolidation via astrocyte pool
        if self._pool is not None:
            try:
                domain_stats = self._run_domain_consolidation()
                stats["domain_consolidation"] = domain_stats
            except Exception:
                logger.exception("Domain consolidation failed")

        # Run memify self-improvement cycle
        try:
            memify_stats = self._curator.memify_cycle()
            stats["memify_pruned"] = memify_stats.get("pruned", 0)
            stats["memify_strengthened"] = memify_stats.get("strengthened", 0)
            stats["memify_reweighted"] = memify_stats.get("reweighted", 0)
            stats["memify_derived"] = memify_stats.get("derived", 0)
        except Exception:
            logger.exception("Memify cycle failed")

        # Run CLS dual-store consolidation (Go-CLS: episodic → semantic)
        try:
            cls_stats = self._cls.consolidation_cycle()
            stats["cls_patterns_found"] = cls_stats.get("patterns_found", 0)
            stats["cls_promoted"] = cls_stats.get("promoted", 0)
            stats["cls_skipped_inconsistent"] = cls_stats.get("skipped_inconsistent", 0)
        except Exception:
            logger.exception("CLS consolidation cycle failed")

        # Run rate-distortion compression as the LAST step
        try:
            comp_stats = self._compressor.compression_cycle()
            stats["compression_to_gist"] = comp_stats.get("compressed_to_gist", 0)
            stats["compression_to_tag"] = comp_stats.get("compressed_to_tag", 0)
        except Exception:
            logger.exception("Compression cycle failed")

        # Process action_log entries into real memories
        try:
            action_stats = self._process_action_log()
            stats["actions_processed"] = action_stats.get("processed", 0)
            stats["action_memories_created"] = action_stats.get("memories_created", 0)
        except Exception:
            logger.exception("Action log processing failed")

        duration_ms = int((time.monotonic() - start) * 1000)
        self._storage.insert_consolidation_log({
            **stats,
            "duration_ms": duration_ms,
        })
        logger.info(
            "Consolidation complete in %dms: %s", duration_ms, stats
        )
        return stats

    @property
    def pool(self):
        """Access the AstrocytePool for domain-aware operations."""
        return self._pool

    @property
    def causal_discovery(self):
        """Access the CausalDiscovery engine."""
        return self._causal_discovery

    @property
    def cls(self):
        """Access the DualStoreCLS for episodic/semantic classification."""
        return self._cls

    def _run_domain_consolidation(self) -> list[dict]:
        """Run consolidation for each active astrocyte process domain."""
        results = []
        for proc_stat in self._pool.get_process_stats():
            name = proc_stat["name"]
            try:
                domain_result = self._pool.consolidate_domain(name)
                results.append(domain_result)
            except Exception:
                logger.exception("Domain consolidation failed for %s", name)
        return results

    # -- Thermodynamic decay --

    def _apply_decay(self, stats: dict) -> None:
        now = datetime.now(timezone.utc)
        decay = self._settings.DECAY_FACTOR
        cold = self._settings.COLD_THRESHOLD

        for mem in self._storage.get_all_memories_for_decay():
            if mem.get("is_protected"):
                continue
            try:
                last = datetime.fromisoformat(mem["last_accessed"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            hours = (now - last).total_seconds() / 3600.0
            new_heat = self._thermo.compute_decay(mem, hours)
            if new_heat < cold:
                new_heat = 0.0
                stats["memories_archived"] += 1
            if abs(new_heat - mem["heat"]) > 1e-9:
                self._storage.update_memory_heat(mem["id"], new_heat)
                stats["memories_updated"] += 1

        for ent in self._storage.get_all_entities_for_decay():
            try:
                last = datetime.fromisoformat(ent["last_accessed"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            hours = (now - last).total_seconds() / 3600.0
            new_heat = ent["heat"] * (decay ** hours)
            if new_heat < cold:
                new_heat = 0.0
                self._storage.archive_entity(ent["id"])
            if abs(new_heat - ent["heat"]) > 1e-9:
                self._storage.update_entity_heat(ent["id"], new_heat)

    # -- Entity extraction and graph building --

    def _process_new_episodes(self, stats: dict) -> None:
        episodes = self._storage.get_episodes_since(
            self._last_consolidated_episode_id
        )
        for ep in episodes:
            # Use typed extraction for richer relationships
            typed_entities = self._graph.extract_entities_typed(
                ep["raw_content"], ep.get("directory", "")
            )
            # Fall back to legacy extraction for broad coverage
            legacy_entities = self._extract_entities(ep["raw_content"])

            # Merge: typed triples -> (name, type) pairs + relationship context
            entity_map: dict[str, str] = {}  # name -> type
            rel_contexts: dict[str, str] = {}  # name -> relationship context
            for name, etype, ctx in typed_entities:
                entity_map[name] = etype
                if ctx:
                    rel_contexts[name] = ctx
            for name, etype in legacy_entities:
                if name not in entity_map:
                    entity_map[name] = etype

            entity_ids = []
            entity_names = []
            for name, etype in entity_map.items():
                existing = self._storage.get_entity_by_name(name)
                if existing:
                    self._storage.reinforce_entity(existing["id"])
                    entity_ids.append(existing["id"])
                else:
                    eid = self._storage.insert_entity(
                        {"name": name, "type": etype}
                    )
                    entity_ids.append(eid)
                entity_names.append(name)

            # Build co-occurrence relationships
            for id_a, id_b in combinations(entity_ids, 2):
                rel = self._storage.get_relationship_between(id_a, id_b)
                if rel:
                    self._storage.reinforce_relationship(rel["id"])
                else:
                    self._storage.insert_relationship({
                        "source_entity_id": id_a,
                        "target_entity_id": id_b,
                        "relationship_type": "co_occurrence",
                    })

            # Build typed relationships from extraction context
            for name, ctx in rel_contexts.items():
                if ctx == "imports":
                    # Find the module this was imported from (nearest dependency)
                    for other_name, other_type in entity_map.items():
                        if other_type == "dependency" and other_name != name:
                            self._graph.add_relationship(
                                name, other_name, "imports"
                            )
                            break
                elif ctx == "calls":
                    pass  # calls are implicit from co_occurrence for now
                elif ctx == "resolved_by":
                    for other_name, other_type in entity_map.items():
                        if other_type == "solution" and other_name != name:
                            self._graph.add_relationship(
                                other_name, name, "resolved_by"
                            )
                            break
                elif ctx == "decided_to_use":
                    pass  # decision pairs handled by extract_entities_typed

            # Synaptic boost: if any associated memory has high importance,
            # boost nearby memories in the time window
            if ep.get("source_episode_id") is not None:
                source_mem = self._storage.get_memory(ep["source_episode_id"])
                if source_mem and source_mem.get("importance", 0.5) > 0.7:
                    self._thermo.synaptic_boost(
                        source_mem["id"], source_mem["heat"]
                    )

            self._last_consolidated_episode_id = max(
                self._last_consolidated_episode_id, ep["id"]
            )

    @staticmethod
    def _extract_entities(content: str) -> list[tuple[str, str]]:
        """Extract (name, type) pairs from raw episode content."""
        entities: list[tuple[str, str]] = []

        # File paths
        for m in _FILE_PATH_RE.finditer(content):
            path = m.group(0)
            if any(path.endswith(ext) for ext in _CODE_EXTENSIONS):
                entities.append((path, "file"))

        # Python def/class
        for m in _PYTHON_DEF_RE.finditer(content):
            entities.append((m.group(2), "function"))

        # JS function keyword
        for m in _JS_FUNCTION_RE.finditer(content):
            entities.append((m.group(1), "function"))

        # Error/Exception types
        for m in _ERROR_RE.finditer(content):
            entities.append((m.group(1), "error"))

        # Traceback header
        if _TRACEBACK_RE.search(content):
            entities.append(("Traceback", "error"))

        # Python imports
        for m in _IMPORT_RE.finditer(content):
            entities.append((m.group(1), "dependency"))
        for m in _FROM_IMPORT_RE.finditer(content):
            entities.append((m.group(1), "dependency"))

        # JS require
        for m in _REQUIRE_RE.finditer(content):
            entities.append((m.group(1), "dependency"))

        # Decisions
        for m in _DECISION_RE.finditer(content):
            entities.append((m.group(0).strip(), "decision"))

        # Deduplicate preserving order
        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, str]] = []
        for pair in entities:
            if pair not in seen:
                seen.add(pair)
                unique.append(pair)
        return unique

    # -- Duplicate merging --

    def _merge_duplicates(self, stats: dict) -> None:
        memories = self._storage.get_all_memories_with_embeddings()
        if len(memories) < 2:
            return

        to_delete: set[int] = set()
        for i, mem_a in enumerate(memories):
            if mem_a["id"] in to_delete:
                continue
            if mem_a.get("is_protected"):
                continue
            for mem_b in memories[i + 1 :]:
                if mem_b["id"] in to_delete:
                    continue
                if mem_b.get("is_protected"):
                    continue
                if mem_a["embedding"] is None or mem_b["embedding"] is None:
                    continue
                sim = self._embeddings.similarity(
                    mem_a["embedding"], mem_b["embedding"]
                )
                if sim >= 0.98:
                    victim = (
                        mem_b["id"]
                        if mem_a["heat"] >= mem_b["heat"]
                        else mem_a["id"]
                    )
                    to_delete.add(victim)

        for mid in to_delete:
            self._storage.delete_memory(mid)
            stats["memories_deleted"] += 1

    # -- Action log processing --

    # Patterns for extracting intent from tool summaries
    _FILE_PATH_IN_SUMMARY = re.compile(
        r"""(?:["'])?(/[\w./@-]+(?:/[\w./@-]+)+)(?:["'])?"""
    )
    _GIT_COMMAND = re.compile(r"\bgit\s+(commit|push|pull|merge|rebase|checkout|branch|stash)")
    _KUBECTL_COMMAND = re.compile(r"\bkubectl\s+(apply|delete|rollout|scale|exec|get|describe|logs)")
    _DOCKER_COMMAND = re.compile(r"\bdocker(?:\s+compose)?\s+(up|down|build|push|pull|restart|exec)")
    _ERROR_IN_SUMMARY = re.compile(r"\b(error|failed|exception|traceback|crash|panic)\b", re.I)

    def _extract_outcome(self, actions: list[dict]) -> str | None:
        """Extract a concise outcome narrative from a group of tool actions.

        Returns None if no meaningful outcome can be inferred (the group
        should be silently dropped rather than stored as a raw log).
        """
        files_read: list[str] = []
        files_edited: list[str] = []
        files_created: list[str] = []
        commands_run: list[str] = []
        searches: list[str] = []
        errors_seen: list[str] = []

        for a in actions:
            tool = a["tool"]
            summary = a.get("summary", "") or ""

            # Classify by tool type
            if tool == "Read":
                m = self._FILE_PATH_IN_SUMMARY.search(summary)
                if m:
                    files_read.append(m.group(1))
            elif tool == "Edit":
                m = self._FILE_PATH_IN_SUMMARY.search(summary)
                if m:
                    files_edited.append(m.group(1))
            elif tool == "Write":
                m = self._FILE_PATH_IN_SUMMARY.search(summary)
                if m:
                    files_created.append(m.group(1))
            elif tool in ("Grep", "Glob"):
                # Extract search pattern
                pat = summary[:80].strip()
                if pat:
                    searches.append(pat)
            elif tool == "Bash":
                cmd = summary[:100].strip()
                if cmd:
                    commands_run.append(cmd)
                    if self._ERROR_IN_SUMMARY.search(summary):
                        errors_seen.append(cmd[:60])

            # Check any summary for error signals
            if self._ERROR_IN_SUMMARY.search(summary):
                errors_seen.append(summary[:60])

        # Build narrative from what happened
        parts: list[str] = []

        if files_edited or files_created:
            # Most interesting: something was changed
            all_modified = list(dict.fromkeys(files_edited + files_created))
            # Dedupe and shorten paths (keep last 2 components)
            short = ["/".join(p.split("/")[-2:]) for p in all_modified[:5]]
            verb = "edited" if files_edited else "created"
            if files_edited and files_created:
                verb = "modified"
            parts.append(f"{verb}: {', '.join(short)}")

        # Detect specific workflows
        git_ops = [self._GIT_COMMAND.search(c) for c in commands_run]
        git_ops = [m.group(1) for m in git_ops if m]
        if git_ops:
            parts.append(f"git: {', '.join(dict.fromkeys(git_ops))}")

        kubectl_ops = [self._KUBECTL_COMMAND.search(c) for c in commands_run]
        kubectl_ops = [m.group(1) for m in kubectl_ops if m]
        if kubectl_ops:
            parts.append(f"kubectl: {', '.join(dict.fromkeys(kubectl_ops))}")

        docker_ops = [self._DOCKER_COMMAND.search(c) for c in commands_run]
        docker_ops = [m.group(1) for m in docker_ops if m]
        if docker_ops:
            parts.append(f"docker: {', '.join(dict.fromkeys(docker_ops))}")

        if errors_seen:
            parts.append(f"errors encountered: {errors_seen[0]}")

        if searches and not parts:
            # Pure investigation session — only store if there were many searches
            if len(searches) >= 3:
                parts.append(f"investigated: {', '.join(searches[:3])}")

        if files_read and not parts:
            # Pure reading session — low value, skip unless many files
            if len(files_read) >= 5:
                short = ["/".join(p.split("/")[-2:]) for p in files_read[:5]]
                parts.append(f"reviewed: {', '.join(short)}")

        if not parts:
            return None

        return "; ".join(parts)

    def _process_action_log(self) -> dict:
        """Process unprocessed action_log entries into outcome-based memories.

        Groups actions by directory + 30-minute time windows, extracts the
        intent/outcome of each group, and stores only meaningful narratives.
        Raw tool-call logs are never stored.
        """
        from datetime import datetime, timezone

        stats = {"processed": 0, "memories_created": 0, "skipped_no_outcome": 0}

        try:
            rows = self._storage._conn.execute(
                "SELECT id, tool_name, tool_input_summary, directory, timestamp "
                "FROM action_log WHERE processed = 0 "
                "ORDER BY timestamp ASC LIMIT 200"
            ).fetchall()
        except Exception:
            return stats

        if not rows:
            return stats

        # Current time bucket — skip it since the window is still open
        now = datetime.now(timezone.utc)
        current_bucket = now.strftime("%Y-%m-%d-%H") + f"-{now.minute // 30}"

        # Group by directory + 30-min windows
        groups: dict[str, list] = {}
        for row in rows:
            directory = row[3] or "unknown"
            timestamp = row[4]
            try:
                dt = datetime.fromisoformat(timestamp)
                bucket = dt.strftime("%Y-%m-%d-%H") + f"-{dt.minute // 30}"
            except (ValueError, TypeError):
                bucket = "unknown"
            key = f"{directory}|{bucket}"
            if key not in groups:
                groups[key] = []
            groups[key].append({
                "id": row[0],
                "tool": row[1],
                "summary": row[2],
                "directory": directory,
                "bucket": bucket,
            })

        for key, actions in groups.items():
            directory = actions[0]["directory"]
            bucket = actions[0]["bucket"]

            # Skip the current (incomplete) time window — process it next cycle
            if bucket == current_bucket:
                continue

            if len(actions) >= 3:
                # Extract outcome narrative instead of raw tool dump
                outcome = self._extract_outcome(actions)

                if outcome is None:
                    stats["skipped_no_outcome"] += 1
                else:
                    content = f"Work session ({len(actions)} actions): {outcome}"

                    # Gate through write gate
                    gate_pass = True
                    if self._write_gate is not None:
                        should_store, _surprisal, _reason = (
                            self._write_gate.should_store(
                                content, directory, ["_action_stream", "_auto"]
                            )
                        )
                        if not should_store:
                            logger.debug(
                                "Action outcome rejected by write gate: %s",
                                _reason,
                            )
                            gate_pass = False

                    if gate_pass:
                        embedding = self._embeddings.encode(content)
                        self._storage.insert_memory({
                            "content": content,
                            "embedding": embedding,
                            "tags": ["_action_outcome", "_auto"],
                            "directory_context": directory,
                            "heat": 0.15,
                            "is_stale": False,
                            "file_hash": None,
                            "embedding_model": self._embeddings.get_model_name(),
                        })
                        stats["memories_created"] += 1

            # Mark all as processed (even if skipped — prevents reprocessing)
            ids = [a["id"] for a in actions]
            placeholders = ",".join("?" * len(ids))
            self._storage._conn.execute(
                f"UPDATE action_log SET processed = 1 WHERE id IN ({placeholders})",
                ids,
            )
            stats["processed"] += len(actions)

        self._storage._conn.commit()
        return stats
