import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

_FTS_STOP_WORDS = frozenset({
    # Standard English stop words
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "of",
    "for", "and", "or", "but", "not", "with", "by", "from", "as",
    "be", "was", "were", "been", "are", "am", "do", "did", "does",
    "has", "had", "have", "will", "would", "could", "should", "may",
    "can", "this", "that", "these", "those", "what", "which", "who",
    "how", "when", "where", "why", "if", "then", "so", "no", "yes",
    "all", "any", "some", "my", "your", "its", "our", "their", "we",
    "he", "she", "they", "me", "him", "her", "us", "them",
    # Coding/conversation domain stop words
    "use", "using", "used", "like", "just", "get", "got", "set",
    "make", "made", "let", "try", "need", "want", "know", "think",
    "code", "file", "thing", "stuff",
})

_CAMEL_CASE_RE = re.compile(r'([a-z])([A-Z])')

_enrichment_pipeline = None


def _get_enrichment_pipeline(settings, embeddings_engine=None):
    global _enrichment_pipeline
    if _enrichment_pipeline is None:
        from vaire.enrichment import EnrichmentPipeline
        _enrichment_pipeline = EnrichmentPipeline(settings, embeddings_engine)
    return _enrichment_pipeline


class StorageEngine:
    def __init__(self, db_path: str, embedding_dim: int = 384):
        resolved = Path(db_path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(resolved)
        self._embedding_dim = embedding_dim
        # Each thread gets its own sqlite3.Connection.  SQLite WAL mode handles
        # concurrent readers across connections; writes serialise at the DB level.
        # This prevents SQLITE_MISUSE when the consolidation daemon thread and the
        # asyncio event-loop thread (and run_in_executor threads) all call storage
        # methods at the same time.
        self._tls = threading.local()
        self._init_schema()
        self._migrate_schema()

    def _make_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Wait up to 60 s when another writer holds the lock (e.g. consolidation
        # daemon competing with the write queue during bulk ingestion) instead of
        # raising OperationalError: database is locked immediately.
        conn.execute("PRAGMA busy_timeout=60000")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._make_connection()
            self._tls.conn = conn
        return conn

    def _init_schema(self):
        c = self._conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS episodes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                directory TEXT NOT NULL,
                raw_content TEXT NOT NULL,
                overlap_start INTEGER,
                overlap_end INTEGER
            );

            CREATE TABLE IF NOT EXISTS entities(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_accessed TEXT NOT NULL,
                heat REAL DEFAULT 1.0,
                archived INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS relationships(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_entity_id INTEGER NOT NULL,
                target_entity_id INTEGER NOT NULL,
                relationship_type TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                last_reinforced TEXT NOT NULL,
                FOREIGN KEY(source_entity_id) REFERENCES entities(id),
                FOREIGN KEY(target_entity_id) REFERENCES entities(id)
            );

            CREATE TABLE IF NOT EXISTS memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                embedding BLOB,
                tags TEXT DEFAULT '[]',
                source_episode_id INTEGER,
                directory_context TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_accessed TEXT NOT NULL,
                heat REAL DEFAULT 1.0,
                is_stale INTEGER DEFAULT 0,
                file_hash TEXT,
                FOREIGN KEY(source_episode_id) REFERENCES episodes(id)
            );

            CREATE TABLE IF NOT EXISTS consolidation_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                memories_added INTEGER DEFAULT 0,
                memories_updated INTEGER DEFAULT 0,
                memories_archived INTEGER DEFAULT 0,
                memories_deleted INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS file_hashes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath TEXT NOT NULL,
                hash TEXT NOT NULL,
                last_checked TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_heat ON memories(heat);
            CREATE INDEX IF NOT EXISTS idx_entities_heat ON entities(heat);
            CREATE INDEX IF NOT EXISTS idx_file_hashes_filepath ON file_hashes(filepath);
        """)

        # FTS5 virtual table — can't be inside executescript with IF NOT EXISTS reliably,
        # so we handle it separately.
        try:
            c.execute(
                "CREATE VIRTUAL TABLE memories_fts USING fts5("
                "content, content_rowid='id')"
            )
        except sqlite3.OperationalError:
            pass  # already exists

        # sqlite-vec virtual table for KNN vector search
        try:
            c.execute(
                f"CREATE VIRTUAL TABLE memory_vectors USING vec0("
                f"embedding float[{self._embedding_dim}])"
            )
        except sqlite3.OperationalError:
            pass  # already exists

        # Implicit embedding vec0 table for dual-vector architecture
        try:
            c.execute(
                f"CREATE VIRTUAL TABLE memory_implicit_vectors USING vec0("
                f"embedding float[{self._embedding_dim}])"
            )
        except sqlite3.OperationalError:
            pass  # already exists

        # -- user_profiles table --
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_name TEXT NOT NULL,
                attribute_type TEXT NOT NULL,
                attribute_key TEXT NOT NULL,
                attribute_value TEXT NOT NULL,
                evidence_memory_ids TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TEXT,
                updated_at TEXT,
                directory_context TEXT,
                UNIQUE(entity_name, attribute_type, attribute_key, directory_context)
            )
        """)

        # -- derived_beliefs table --
        c.execute("""
            CREATE TABLE IF NOT EXISTS derived_beliefs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                belief_type TEXT NOT NULL,
                subject TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence_memory_ids TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                embedding BLOB,
                embedding_model TEXT,
                created_at TEXT,
                updated_at TEXT,
                directory_context TEXT
            )
        """)

        # FTS5 for profiles
        try:
            c.execute(
                "CREATE VIRTUAL TABLE profiles_fts USING fts5("
                "entity_name, attribute_type, attribute_key, attribute_value, "
                "content=user_profiles, content_rowid=id)"
            )
        except sqlite3.OperationalError:
            pass

        # FTS5 for beliefs
        try:
            c.execute(
                "CREATE VIRTUAL TABLE beliefs_fts USING fts5("
                "subject, belief_type, content, "
                "content=derived_beliefs, content_rowid=id)"
            )
        except sqlite3.OperationalError:
            pass

        # Triggers for FTS sync — drop first to allow updates
        c.execute("DROP TRIGGER IF EXISTS memories_fts_insert")
        c.execute("DROP TRIGGER IF EXISTS memories_fts_update")
        for trigger_sql in [
            """
            CREATE TRIGGER IF NOT EXISTS memories_fts_insert
            AFTER INSERT ON memories
            BEGIN
                INSERT INTO memories_fts(rowid, content)
                VALUES (new.id, new.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memories_fts_update
            AFTER UPDATE ON memories
            BEGIN
                UPDATE memories_fts SET content = new.content WHERE rowid = new.id;
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memories_fts_delete
            AFTER DELETE ON memories
            BEGIN
                DELETE FROM memories_fts WHERE rowid = old.id;
            END
            """,
        ]:
            c.execute(trigger_sql)

        c.commit()

    def _migrate_schema(self):
        """Add v2 columns and tables. Safe to run repeatedly."""
        c = self._conn

        # -- New columns on existing tables --
        memory_columns = [
            ("surprise_score", "REAL DEFAULT 0.0"),
            ("importance", "REAL DEFAULT 0.5"),
            ("emotional_valence", "REAL DEFAULT 0.0"),
            ("confidence", "REAL DEFAULT 1.0"),
            ("access_count", "INTEGER DEFAULT 0"),
            ("useful_count", "INTEGER DEFAULT 0"),
            ("embedding_model", "TEXT"),
            ("contextual_prefix", "TEXT"),
            ("cluster_id", "INTEGER"),
            ("is_prospective", "INTEGER DEFAULT 0"),
            ("trigger_condition", "TEXT"),
            ("narrative_weight", "REAL DEFAULT 0.0"),
        ("compressed", "INTEGER DEFAULT 0"),
            # v3 frontier columns
            ("plasticity", "REAL DEFAULT 1.0"),
            ("stability", "REAL DEFAULT 0.0"),
            ("excitability", "REAL DEFAULT 1.0"),
            ("last_excitability_update", "TEXT"),
            ("store_type", "TEXT DEFAULT 'episodic'"),
            ("compression_level", "INTEGER DEFAULT 0"),
            ("original_content", "TEXT"),
            ("hdc_vector", "BLOB"),
            ("sr_x", "REAL DEFAULT 0.0"),
            ("sr_y", "REAL DEFAULT 0.0"),
            ("reconsolidation_count", "INTEGER DEFAULT 0"),
            ("last_reconsolidated", "TEXT"),
            ("provenance_agent", "TEXT DEFAULT 'default'"),
            ("vector_clock", "TEXT DEFAULT '{}'"),
            ("is_protected", "INTEGER DEFAULT 0"),
            ("slot_index", "INTEGER"),
            # enrichment pipeline columns
            ("enrichment_concepts", "TEXT DEFAULT NULL"),
            ("enrichment_comet", "TEXT DEFAULT NULL"),
            ("enrichment_queries", "TEXT DEFAULT NULL"),
            ("enrichment_logic", "TEXT DEFAULT NULL"),
            ("enriched_content", "TEXT DEFAULT NULL"),
            ("enrichment_model_versions", "TEXT DEFAULT NULL"),
            # v25 dual-vector columns
            ("implicit_embedding", "BLOB DEFAULT NULL"),
            ("implicit_embedding_model", "TEXT DEFAULT NULL"),
        ]
        for col_name, col_def in memory_columns:
            try:
                c.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass  # column already exists

        entity_columns = [
            ("causal_weight", "REAL DEFAULT 0.0"),
            ("domain", "TEXT"),
        ]
        for col_name, col_def in entity_columns:
            try:
                c.execute(f"ALTER TABLE entities ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass

        relationship_columns = [
            ("event_time", "TEXT"),
            ("record_time", "TEXT"),
            ("is_causal", "INTEGER DEFAULT 0"),
            ("confidence", "REAL DEFAULT 1.0"),
        ]
        for col_name, col_def in relationship_columns:
            try:
                c.execute(f"ALTER TABLE relationships ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass

        # -- New tables --
        c.executescript("""
            CREATE TABLE IF NOT EXISTS memory_clusters(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                level INTEGER DEFAULT 0,
                parent_cluster_id INTEGER,
                summary TEXT DEFAULT '',
                centroid_embedding BLOB,
                member_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                heat REAL DEFAULT 1.0
            );

            CREATE TABLE IF NOT EXISTS prospective_memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                trigger_condition TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                target_directory TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                triggered_at TEXT,
                triggered_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS narrative_entries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                directory_context TEXT NOT NULL,
                summary TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                key_decisions TEXT DEFAULT '[]',
                key_events TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                heat REAL DEFAULT 1.0
            );

            CREATE TABLE IF NOT EXISTS astrocyte_processes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                domain TEXT NOT NULL,
                specialization TEXT DEFAULT '',
                memory_ids TEXT DEFAULT '[]',
                entity_ids TEXT DEFAULT '[]',
                heat REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                last_active TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_cluster ON memories(cluster_id);
            CREATE INDEX IF NOT EXISTS idx_memories_surprise ON memories(surprise_score);
            CREATE INDEX IF NOT EXISTS idx_relationships_type ON relationships(relationship_type);
            CREATE INDEX IF NOT EXISTS idx_prospective_active ON prospective_memories(is_active);
        """)

        # -- v3 frontier tables --
        c.executescript("""
            CREATE TABLE IF NOT EXISTS memory_rules(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_value TEXT,
                condition TEXT NOT NULL,
                action TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS memory_archives(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_memory_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                archived_at TEXT NOT NULL,
                mismatch_score REAL DEFAULT 0.0,
                archive_reason TEXT DEFAULT '',
                FOREIGN KEY (original_memory_id) REFERENCES memories(id)
            );

            CREATE TABLE IF NOT EXISTS memory_transitions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_memory_id INTEGER NOT NULL,
                to_memory_id INTEGER NOT NULL,
                count INTEGER DEFAULT 1,
                last_transition TEXT NOT NULL,
                session_id TEXT DEFAULT '',
                UNIQUE(from_memory_id, to_memory_id)
            );

            CREATE TABLE IF NOT EXISTS causal_dag_edges(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_entity_id INTEGER NOT NULL,
                target_entity_id INTEGER NOT NULL,
                algorithm TEXT DEFAULT 'pc',
                confidence REAL DEFAULT 1.0,
                discovered_at TEXT NOT NULL,
                is_validated INTEGER DEFAULT 0,
                FOREIGN KEY (source_entity_id) REFERENCES entities(id),
                FOREIGN KEY (target_entity_id) REFERENCES entities(id)
            );

            CREATE INDEX IF NOT EXISTS idx_transitions_from ON memory_transitions(from_memory_id);
            CREATE INDEX IF NOT EXISTS idx_transitions_to ON memory_transitions(to_memory_id);
            CREATE INDEX IF NOT EXISTS idx_archives_original ON memory_archives(original_memory_id);
            CREATE INDEX IF NOT EXISTS idx_causal_dag_source ON causal_dag_edges(source_entity_id);
            CREATE INDEX IF NOT EXISTS idx_causal_dag_target ON causal_dag_edges(target_entity_id);
            CREATE INDEX IF NOT EXISTS idx_rules_scope ON memory_rules(scope);
        """)

        # -- Engram slots table --
        c.executescript("""
            CREATE TABLE IF NOT EXISTS engram_slots(
                slot_index INTEGER PRIMARY KEY,
                excitability REAL DEFAULT 0.0,
                last_activated TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memories_slot ON memories(slot_index);
        """)

        c.commit()

        # -- v4: Hippocampal Replay checkpoints --
        c.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT 'default',
                directory_context TEXT NOT NULL,
                current_task TEXT DEFAULT '',
                files_being_edited TEXT DEFAULT '[]',
                key_decisions TEXT DEFAULT '[]',
                open_questions TEXT DEFAULT '[]',
                next_steps TEXT DEFAULT '[]',
                active_errors TEXT DEFAULT '[]',
                custom_context TEXT DEFAULT '',
                epoch INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_active ON checkpoints(is_active, created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_epoch ON checkpoints(epoch)")

        # Action log — lightweight event capture from PostToolCall hooks
        c.execute("""
            CREATE TABLE IF NOT EXISTS action_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                tool_input_summary TEXT DEFAULT '',
                directory TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                timestamp TEXT NOT NULL,
                processed INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_action_log_processed ON action_log(processed, timestamp)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        c.commit()

    # -- helpers --

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for json_field in ("tags", "key_decisions", "key_events", "memory_ids", "entity_ids", "evidence_memory_ids"):
            if json_field in d and isinstance(d[json_field], str):
                try:
                    d[json_field] = json.loads(d[json_field])
                except (ValueError, TypeError):
                    d[json_field] = []
        for bool_field in ("archived", "is_stale", "is_prospective", "is_causal", "is_active", "compressed", "is_protected", "is_validated"):
            if bool_field in d:
                d[bool_field] = bool(d[bool_field])
        return d

    def _rows_to_dicts(self, rows: list[sqlite3.Row]) -> list[dict]:
        return [self._row_to_dict(r) for r in rows]

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # -- Episodes --

    def insert_episode(self, episode: dict) -> int:
        cur = self._conn.execute(
            "INSERT INTO episodes(session_id, timestamp, directory, raw_content, "
            "overlap_start, overlap_end) VALUES (?, ?, ?, ?, ?, ?)",
            (
                episode["session_id"],
                episode.get("timestamp", self._now_iso()),
                episode["directory"],
                episode["raw_content"],
                episode.get("overlap_start"),
                episode.get("overlap_end"),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_session_episodes(self, session_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM episodes WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    # -- Memories --

    def insert_memory(self, memory: dict, embeddings_engine=None, settings=None) -> int:
        tags_json = json.dumps(memory.get("tags", []))
        now = self._now_iso()
        embedding = memory.get("embedding")
        embedding_model = memory.get("embedding_model")
        cur = self._conn.execute(
            "INSERT INTO memories(content, embedding, tags, source_episode_id, "
            "directory_context, created_at, last_accessed, heat, is_stale, file_hash, "
            "embedding_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory["content"],
                embedding,
                tags_json,
                memory.get("source_episode_id"),
                memory["directory_context"],
                memory.get("created_at", now),
                memory.get("last_accessed", now),
                memory.get("heat", 1.0),
                int(memory.get("is_stale", False)),
                memory.get("file_hash"),
                embedding_model,
            ),
        )
        self._conn.commit()
        memory_id = cur.lastrowid
        # Enrich FTS content with split identifiers
        enriched = self._enrich_content_for_fts(memory["content"])
        if enriched != memory["content"]:
            self._conn.execute(
                "UPDATE memories_fts SET content = ? WHERE rowid = ?",
                (enriched, memory_id),
            )
            self._conn.commit()
        # Index-time enrichment
        enrichment_data = {}
        if (settings and getattr(settings, 'INDEX_ENRICHMENT_ENABLED', False)
                and len(memory["content"]) >= getattr(settings, 'ENRICHMENT_MIN_CONTENT_LENGTH', 20)
                and embeddings_engine is not None and embedding is not None):
            try:
                pipeline = _get_enrichment_pipeline(settings, embeddings_engine)
                result = pipeline.enrich(memory["content"], embedding, settings)
                enrichment_data = {
                    "enrichment_concepts": json.dumps(result.concepts) if result.concepts else None,
                    "enrichment_comet": json.dumps(result.comet_inferences) if result.comet_inferences else None,
                    "enrichment_queries": json.dumps(result.queries) if result.queries else None,
                    "enrichment_logic": json.dumps(result.logic_expansions) if result.logic_expansions else None,
                    "enriched_content": result.enriched_content or None,
                    "enrichment_model_versions": json.dumps(result.model_versions) if result.model_versions else None,
                }
                if any(v is not None for v in enrichment_data.values()):
                    set_clauses = []
                    params = []
                    for col, val in enrichment_data.items():
                        if val is not None:
                            set_clauses.append(f"{col} = ?")
                            params.append(val)
                    if set_clauses:
                        params.append(memory_id)
                        self._conn.execute(
                            f"UPDATE memories SET {', '.join(set_clauses)} WHERE id = ?",
                            params,
                        )
                        # Re-embed with enriched content for better semantic matching
                        # (FTS stays clean with original content only)
                        if enrichment_data.get("enriched_content") and embeddings_engine is not None:
                            new_embedding = embeddings_engine.encode_document_enriched(
                                memory["content"], enrichment_data["enriched_content"]
                            )
                            if new_embedding is not None:
                                self._conn.execute(
                                    "UPDATE memories SET embedding = ? WHERE id = ?",
                                    (new_embedding, memory_id),
                                )
                        self._conn.commit()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Enrichment failed: %s", e)

        # Profile extraction
        if (settings and getattr(settings, 'PROFILE_EXTRACTION_ENABLED', False)):
            try:
                from vaire.profiles import ProfileExtractor
                extractor = ProfileExtractor(self, settings)
                extractor.extract_and_store(
                    memory["content"], memory_id, memory["directory_context"]
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Profile extraction failed: %s", e)

        # Also insert into sqlite-vec for vector search
        if embedding is not None:
            self.insert_vector(memory_id, embedding)
        return memory_id

    def get_memory(self, memory_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return self._row_to_dict(row)

    def get_memories_by_heat(self, min_heat: float, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE heat >= ? ORDER BY heat DESC LIMIT ?",
            (min_heat, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def _enrich_content_for_fts(self, content: str) -> str:
        """Enrich content with split identifier tokens for better FTS matching."""
        tokens = content.split()
        extra_tokens = []
        for token in tokens:
            split = _CAMEL_CASE_RE.sub(r'\1 \2', token)
            split = split.replace('_', ' ')
            sub_tokens = split.split()
            if len(sub_tokens) > 1:
                extra_tokens.extend(t for t in sub_tokens if t != token)
        if extra_tokens:
            return content + " " + " ".join(extra_tokens)
        return content

    def _preprocess_fts_query(self, query: str) -> str:
        """Preprocess a query string for FTS5 MATCH with identifier splitting and stop word removal."""
        parts = []
        raw_tokens = query.split()

        for token in raw_tokens:
            # Strip FTS5-unsafe punctuation (?, !, ;, etc.) from token edges
            token = token.strip('?!,;:()[]{}"\'"')
            if not token:
                continue

            # Determine if this token looks like an entity
            is_entity = (
                token[0:1].isupper()
                or '_' in token
                or '.' in token
            ) if token else False

            # Split identifiers: CamelCase, snake_case, dotted
            split_term = _CAMEL_CASE_RE.sub(r'\1 \2', token)
            split_term = split_term.replace('_', ' ').replace('.', ' ')
            sub_tokens = split_term.split()

            # Filter: remove stop words and short terms
            filtered = [
                t for t in sub_tokens
                if t.lower() not in _FTS_STOP_WORDS and len(t) >= 2
            ]

            # If entity, add quoted phrase match for original term
            if is_entity and len(token) >= 2:
                parts.append(f'"{token}"')

            parts.extend(filtered)

        if not parts:
            return query

        return " OR ".join(parts)

    def search_memories_fts(
        self, query: str, min_heat: float = 0.1, limit: int = 5
    ) -> list[dict]:
        fts_query = self._preprocess_fts_query(query)
        rows = self._conn.execute(
            "SELECT m.* FROM memories m "
            "JOIN memories_fts fts ON m.id = fts.rowid "
            "WHERE memories_fts MATCH ? AND m.heat >= ? "
            "ORDER BY m.heat DESC LIMIT ?",
            (fts_query, min_heat, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def search_memories_fts_scored(
        self, query: str, min_heat: float = 0.1, limit: int = 50
    ) -> list[tuple[int, float]]:
        """FTS5 search returning (memory_id, bm25_score) tuples.

        BM25 scores from FTS5 are negative (more negative = better match).
        We negate them so higher = better.
        """
        fts_query = self._preprocess_fts_query(query)
        rows = self._conn.execute(
            "SELECT m.id, -bm25(memories_fts) as score FROM memories m "
            "JOIN memories_fts fts ON m.id = fts.rowid "
            "WHERE memories_fts MATCH ? AND m.heat >= ? "
            "ORDER BY score DESC LIMIT ?",
            (fts_query, min_heat, limit),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def update_memory_heat(self, memory_id: int, new_heat: float):
        self._conn.execute(
            "UPDATE memories SET heat = ? WHERE id = ?", (new_heat, memory_id)
        )
        self._conn.commit()

    def update_memory_staleness(self, memory_id: int, is_stale: bool):
        self._conn.execute(
            "UPDATE memories SET is_stale = ? WHERE id = ?",
            (int(is_stale), memory_id),
        )
        self._conn.commit()

    def delete_memory(self, memory_id: int):
        # Delete from sqlite-vec first (ignore if not present)
        try:
            self.delete_vector(memory_id)
        except Exception:
            pass
        # memory_archives has a bare FK to memories(id) without ON DELETE CASCADE,
        # so we must remove archive rows first or the DELETE will raise an IntegrityError.
        self._conn.execute(
            "DELETE FROM memory_archives WHERE original_memory_id = ?", (memory_id,)
        )
        self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._conn.commit()

    def get_memories_for_directory(
        self, directory: str, min_heat: float = 0.1
    ) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE directory_context = ? AND heat >= ? "
            "ORDER BY heat DESC",
            (directory, min_heat),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_stale_memories(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE is_stale = 1"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_memories_by_file_hash(self, file_hash: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE file_hash = ?", (file_hash,)
        ).fetchall()
        return self._rows_to_dicts(rows)

    # Group A — recall hot path (Phase 2 cache dependencies)

    def get_all_memories_for_cache(self) -> list[dict]:
        """Return all non-stale memories for cache warmup.

        Returns id, content, heat, embedding, tags, directory_context,
        created_at, last_accessed. Excludes stale rows.
        """
        rows = self._conn.execute(
            "SELECT id, content, heat, embedding, tags, directory_context, "
            "created_at, last_accessed FROM memories WHERE is_stale = 0"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_active_rules(self) -> list[dict]:
        """Return all active memory rules sorted by priority DESC."""
        rows = self._conn.execute(
            "SELECT * FROM memory_rules WHERE is_active = 1 ORDER BY priority DESC"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_all_memories_for_decay(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE heat > 0"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_all_memories_with_embeddings(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE embedding IS NOT NULL AND heat > 0"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def search_memories_by_content_date(
        self,
        date_hints: list[str],
        month_hints: list[str],
        session_hints: list[str],
        min_heat: float = 0.0,
        limit: int = 50,
    ) -> list[dict]:
        """Search memory content for temporal references using FTS5."""
        terms = []
        for hint in date_hints:
            terms.append('"' + hint + '"')
        for hint in month_hints:
            terms.append(hint)
        for hint in session_hints:
            terms.append(hint)
        if not terms:
            return []
        fts_query = " OR ".join(terms)
        rows = self._conn.execute(
            "SELECT m.* FROM memories m "
            "JOIN memories_fts fts ON m.id = fts.rowid "
            "WHERE memories_fts MATCH ? AND m.heat >= ? "
            "ORDER BY m.heat DESC LIMIT ?",
            (fts_query, min_heat, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def search_memories_by_timestamp_range(
        self,
        start_date: str,
        end_date: str,
        min_heat: float = 0.0,
        limit: int = 50,
    ) -> list[dict]:
        """Query memories by created_at timestamp range."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE created_at >= ? AND created_at <= ? "
            "AND heat >= ? ORDER BY created_at DESC LIMIT ?",
            (start_date, end_date, min_heat, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def search_memories_by_month(
        self,
        month_hints: list[str],
        min_heat: float = 0.0,
        limit: int = 200,
    ) -> list[int]:
        """Find memory IDs whose created_at falls in the given month(s).

        month_hints: list of month names like ['may', 'june'].
        Returns list of memory IDs.
        """
        month_map = {
            "january": "01", "february": "02", "march": "03", "april": "04",
            "may": "05", "june": "06", "july": "07", "august": "08",
            "september": "09", "october": "10", "november": "11", "december": "12",
        }
        conditions = []
        params: list = []
        for hint in month_hints:
            mm = month_map.get(hint.lower())
            if mm:
                # Match ISO dates like 2023-05-...
                conditions.append("substr(created_at, 6, 2) = ?")
                params.append(mm)
        if not conditions:
            return []
        where = " OR ".join(conditions)
        params.extend([min_heat, limit])
        rows = self._conn.execute(
            f"SELECT id FROM memories WHERE ({where}) AND heat >= ? LIMIT ?",
            params,
        ).fetchall()
        return [r[0] for r in rows]

    # -- Vector Search (sqlite-vec) --

    def insert_vector(self, memory_id: int, embedding: bytes):
        """Insert an embedding into the memory_vectors vec0 table."""
        self._conn.execute(
            "INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)",
            (memory_id, embedding),
        )
        self._conn.commit()

    def delete_vector(self, memory_id: int):
        """Delete an embedding from the memory_vectors vec0 table."""
        self._conn.execute(
            "DELETE FROM memory_vectors WHERE rowid = ?", (memory_id,)
        )
        self._conn.commit()

    def update_vector(self, memory_id: int, embedding: bytes):
        """Update an embedding in memory_vectors (delete + re-insert)."""
        self.delete_vector(memory_id)
        self.insert_vector(memory_id, embedding)

    def insert_implicit_vector(self, memory_id: int, embedding: bytes):
        """Insert an embedding into the memory_implicit_vectors vec0 table."""
        self._conn.execute(
            "INSERT INTO memory_implicit_vectors(rowid, embedding) VALUES (?, ?)",
            (memory_id, embedding),
        )
        self._conn.commit()

    def search_implicit_vectors(
        self,
        query_embedding: bytes,
        top_k: int = 10,
    ) -> list[tuple[int, float]]:
        """KNN search over implicit embedding vectors.

        Returns list of (memory_id, distance) tuples sorted by ascending distance.
        """
        fetch_k = min(top_k * 4, 4096)
        rows = self._conn.execute(
            "SELECT v.rowid, v.distance "
            "FROM memory_implicit_vectors v "
            "WHERE v.embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (query_embedding, fetch_k),
        ).fetchall()
        return [(row[0], row[1]) for row in rows[:top_k]]

    def search_vectors(
        self,
        query_embedding: bytes,
        top_k: int = 10,
        min_heat: float = 0.1,
    ) -> list[tuple[int, float]]:
        """KNN search via sqlite-vec, filtered by min_heat.

        Returns list of (memory_id, distance) tuples sorted by ascending distance.
        """
        # Fetch more candidates than needed so we have enough after heat filtering
        fetch_k = min(top_k * 4, 4096)  # sqlite-vec KNN limit is 4096
        rows = self._conn.execute(
            "SELECT v.rowid, v.distance, m.heat "
            "FROM memory_vectors v "
            "JOIN memories m ON m.id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (query_embedding, fetch_k),
        ).fetchall()
        results = []
        for row in rows:
            if row[2] >= min_heat:
                results.append((row[0], row[1]))
            if len(results) >= top_k:
                break
        return results

    def get_memories_needing_reembedding(self, current_model: str) -> list[dict]:
        """Return memories where embedding_model != current_model or is NULL."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE embedding IS NOT NULL "
            "AND (embedding_model IS NULL OR embedding_model != ?)",
            (current_model,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def update_memory_embedding(
        self, memory_id: int, embedding: bytes, embedding_model: str
    ):
        """Update a memory's embedding and embedding_model field."""
        self._conn.execute(
            "UPDATE memories SET embedding = ?, embedding_model = ? WHERE id = ?",
            (embedding, embedding_model, memory_id),
        )
        self._conn.commit()
        # Update the vec0 table too
        try:
            self.update_vector(memory_id, embedding)
        except Exception:
            # Vector may not exist yet; insert instead
            try:
                self.insert_vector(memory_id, embedding)
            except Exception:
                pass

    def recreate_vector_table(self, new_dim: int):
        """Recreate the memory_vectors vec0 table with new dimensions.

        This is needed when switching to a model with different output dimensions.
        All existing vectors are dropped — caller must re-embed after calling this.
        """
        self._conn.execute("DROP TABLE IF EXISTS memory_vectors")
        self._conn.execute(
            f"CREATE VIRTUAL TABLE memory_vectors USING vec0("
            f"embedding float[{new_dim}])"
        )
        self._conn.commit()
        self._embedding_dim = new_dim

    # -- Entities --

    def insert_entity(self, entity: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO entities(name, type, created_at, last_accessed, heat, archived) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                entity["name"],
                entity["type"],
                entity.get("created_at", now),
                entity.get("last_accessed", now),
                entity.get("heat", 1.0),
                int(entity.get("archived", False)),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_entity_by_name(self, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM entities WHERE name = ?", (name,)
        ).fetchone()
        return self._row_to_dict(row)

    def get_all_entities(
        self, min_heat: float = 0.0, include_archived: bool = False
    ) -> list[dict]:
        if include_archived:
            rows = self._conn.execute(
                "SELECT * FROM entities WHERE heat >= ? ORDER BY heat DESC",
                (min_heat,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM entities WHERE heat >= ? AND archived = 0 "
                "ORDER BY heat DESC",
                (min_heat,),
            ).fetchall()
        return self._rows_to_dicts(rows)

    def update_entity_heat(self, entity_id: int, new_heat: float):
        self._conn.execute(
            "UPDATE entities SET heat = ? WHERE id = ?", (new_heat, entity_id)
        )
        self._conn.commit()

    def get_all_entities_for_decay(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM entities WHERE archived = 0"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def archive_entity(self, entity_id: int):
        self._conn.execute(
            "UPDATE entities SET archived = 1 WHERE id = ?", (entity_id,)
        )
        self._conn.commit()

    def reinforce_entity(self, entity_id: int, heat_bump: float = 0.1):
        self._conn.execute(
            "UPDATE entities SET heat = MIN(heat + ?, 1.0), last_accessed = ? "
            "WHERE id = ?",
            (heat_bump, self._now_iso(), entity_id),
        )
        self._conn.commit()

    # -- Relationships --

    def insert_relationship(self, relationship: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO relationships(source_entity_id, target_entity_id, "
            "relationship_type, weight, created_at, last_reinforced) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                relationship["source_entity_id"],
                relationship["target_entity_id"],
                relationship["relationship_type"],
                relationship.get("weight", 1.0),
                relationship.get("created_at", now),
                relationship.get("last_reinforced", now),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_relationship_between(
        self, source_id: int, target_id: int
    ) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM relationships WHERE "
            "(source_entity_id = ? AND target_entity_id = ?) OR "
            "(source_entity_id = ? AND target_entity_id = ?)",
            (source_id, target_id, target_id, source_id),
        ).fetchone()
        return self._row_to_dict(row)

    def reinforce_relationship(self, rel_id: int, weight_increase: float = 1.0):
        self._conn.execute(
            "UPDATE relationships SET weight = weight + ?, last_reinforced = ? "
            "WHERE id = ?",
            (weight_increase, self._now_iso(), rel_id),
        )
        self._conn.commit()

    # -- Episodes (additional) --

    def get_episodes_since(self, episode_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM episodes WHERE id > ? ORDER BY id",
            (episode_id,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_max_episode_id(self) -> int:
        row = self._conn.execute("SELECT MAX(id) FROM episodes").fetchone()
        return row[0] if row[0] is not None else 0

    # -- File Hashes --

    def upsert_file_hash(self, filepath: str, hash_value: str):
        now = self._now_iso()
        existing = self._conn.execute(
            "SELECT id FROM file_hashes WHERE filepath = ?", (filepath,)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE file_hashes SET hash = ?, last_checked = ? WHERE filepath = ?",
                (hash_value, now, filepath),
            )
        else:
            self._conn.execute(
                "INSERT INTO file_hashes(filepath, hash, last_checked) VALUES (?, ?, ?)",
                (filepath, hash_value, now),
            )
        self._conn.commit()

    def get_file_hash(self, filepath: str) -> str | None:
        row = self._conn.execute(
            "SELECT hash FROM file_hashes WHERE filepath = ?", (filepath,)
        ).fetchone()
        return row["hash"] if row else None

    def get_filepath_by_hash(self, hash_value: str) -> str | None:
        row = self._conn.execute(
            "SELECT filepath FROM file_hashes WHERE hash = ? LIMIT 1",
            (hash_value,),
        ).fetchone()
        return row["filepath"] if row else None

    # -- Consolidation Log --

    def insert_consolidation_log(self, log: dict) -> int:
        cur = self._conn.execute(
            "INSERT INTO consolidation_log(timestamp, memories_added, memories_updated, "
            "memories_archived, memories_deleted, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (
                log.get("timestamp", self._now_iso()),
                log.get("memories_added", 0),
                log.get("memories_updated", 0),
                log.get("memories_archived", 0),
                log.get("memories_deleted", 0),
                log.get("duration_ms", 0),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    # -- Stats --

    def get_memory_stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE is_stale = 0 AND heat >= 0.05"
        ).fetchone()[0]
        archived = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE heat < 0.05"
        ).fetchone()[0]
        stale = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE is_stale = 1"
        ).fetchone()[0]
        avg_heat_row = self._conn.execute(
            "SELECT AVG(heat) FROM memories"
        ).fetchone()[0]
        avg_heat = avg_heat_row if avg_heat_row is not None else 0.0
        last_consolidation_row = self._conn.execute(
            "SELECT timestamp FROM consolidation_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_consolidation = (
            last_consolidation_row["timestamp"] if last_consolidation_row else None
        )
        return {
            "total_memories": total,
            "active_count": active,
            "archived_count": archived,
            "stale_count": stale,
            "avg_heat": avg_heat,
            "last_consolidation": last_consolidation,
        }

    # -- Memory Clusters --

    def insert_cluster(self, cluster: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO memory_clusters(name, level, parent_cluster_id, summary, "
            "centroid_embedding, member_count, created_at, last_updated, heat) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cluster["name"],
                cluster.get("level", 0),
                cluster.get("parent_cluster_id"),
                cluster.get("summary", ""),
                cluster.get("centroid_embedding"),
                cluster.get("member_count", 0),
                cluster.get("created_at", now),
                cluster.get("last_updated", now),
                cluster.get("heat", 1.0),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_cluster(self, cluster_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM memory_clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        return self._row_to_dict(row)

    def get_clusters_by_level(self, level: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memory_clusters WHERE level = ? ORDER BY heat DESC",
            (level,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def update_cluster(self, cluster_id: int, updates: dict):
        allowed = {
            "name", "level", "parent_cluster_id", "summary",
            "centroid_embedding", "member_count", "heat", "last_updated",
        }
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return
        if "last_updated" not in fields:
            fields["last_updated"] = self._now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [cluster_id]
        self._conn.execute(
            f"UPDATE memory_clusters SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()

    # -- Prospective Memories --

    def insert_prospective_memory(self, pm: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO prospective_memories(content, trigger_condition, trigger_type, "
            "target_directory, is_active, created_at, triggered_at, triggered_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pm["content"],
                pm["trigger_condition"],
                pm["trigger_type"],
                pm.get("target_directory"),
                int(pm.get("is_active", True)),
                pm.get("created_at", now),
                pm.get("triggered_at"),
                pm.get("triggered_count", 0),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_active_prospective_memories(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM prospective_memories WHERE is_active = 1"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def trigger_prospective_memory(self, pm_id: int):
        now = self._now_iso()
        self._conn.execute(
            "UPDATE prospective_memories SET triggered_at = ?, "
            "triggered_count = triggered_count + 1 WHERE id = ?",
            (now, pm_id),
        )
        self._conn.commit()

    # -- Narrative Entries --

    def insert_narrative_entry(self, entry: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO narrative_entries(directory_context, summary, period_start, "
            "period_end, key_decisions, key_events, created_at, heat) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["directory_context"],
                entry["summary"],
                entry["period_start"],
                entry["period_end"],
                json.dumps(entry.get("key_decisions", [])),
                json.dumps(entry.get("key_events", [])),
                entry.get("created_at", now),
                entry.get("heat", 1.0),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_narratives_for_directory(
        self, directory: str, limit: int = 10
    ) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM narrative_entries WHERE directory_context = ? "
            "ORDER BY period_end DESC LIMIT ?",
            (directory, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    # -- Astrocyte Processes --

    def insert_astrocyte_process(self, proc: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO astrocyte_processes(name, domain, specialization, "
            "memory_ids, entity_ids, heat, created_at, last_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proc["name"],
                proc["domain"],
                proc.get("specialization", ""),
                json.dumps(proc.get("memory_ids", [])),
                json.dumps(proc.get("entity_ids", [])),
                proc.get("heat", 1.0),
                proc.get("created_at", now),
                proc.get("last_active", now),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_astrocyte_processes(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM astrocyte_processes ORDER BY heat DESC"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def update_astrocyte_process(self, proc_id: int, updates: dict):
        allowed = {
            "name", "domain", "specialization", "memory_ids",
            "entity_ids", "heat", "last_active",
        }
        fields = {}
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k in ("memory_ids", "entity_ids"):
                fields[k] = json.dumps(v)
            else:
                fields[k] = v
        if not fields:
            return
        if "last_active" not in fields:
            fields["last_active"] = self._now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [proc_id]
        self._conn.execute(
            f"UPDATE astrocyte_processes SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()

    # -- Thermodynamics helpers --

    def update_memory_scores(
        self,
        memory_id: int,
        surprise_score: float | None = None,
        importance: float | None = None,
        emotional_valence: float | None = None,
    ):
        """Update computed thermodynamic scores on a memory."""
        fields = {}
        if surprise_score is not None:
            fields["surprise_score"] = surprise_score
        if importance is not None:
            fields["importance"] = importance
        if emotional_valence is not None:
            fields["emotional_valence"] = emotional_valence
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [memory_id]
        self._conn.execute(
            f"UPDATE memories SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()

    def update_memory_metamemory(
        self,
        memory_id: int,
        access_count: int,
        useful_count: int,
        confidence: float,
    ):
        """Update metamemory tracking fields."""
        self._conn.execute(
            "UPDATE memories SET access_count = ?, useful_count = ?, confidence = ? "
            "WHERE id = ?",
            (access_count, useful_count, confidence, memory_id),
        )
        self._conn.commit()

    def get_memories_in_time_window(
        self, center_time: str, window_minutes: int
    ) -> list[dict]:
        """Return memories created within window_minutes of center_time."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE heat > 0 "
            "AND abs((julianday(created_at) - julianday(?)) * 24 * 60) <= ?",
            (center_time, window_minutes),
        ).fetchall()
        return self._rows_to_dicts(rows)

    # -- Memory Rules --

    def insert_rule(self, rule: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO memory_rules(rule_type, scope, scope_value, condition, "
            "action, priority, created_at, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule["rule_type"],
                rule["scope"],
                rule.get("scope_value"),
                rule["condition"],
                rule["action"],
                rule.get("priority", 0),
                rule.get("created_at", now),
                int(rule.get("is_active", True)),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_rules_for_scope(self, scope: str, scope_value: str | None = None) -> list[dict]:
        if scope == "global":
            rows = self._conn.execute(
                "SELECT * FROM memory_rules WHERE scope = 'global' AND is_active = 1 "
                "ORDER BY priority DESC",
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM memory_rules WHERE scope = ? AND scope_value = ? "
                "AND is_active = 1 ORDER BY priority DESC",
                (scope, scope_value),
            ).fetchall()
        return self._rows_to_dicts(rows)

    def update_rule(self, rule_id: int, updates: dict):
        allowed = {"rule_type", "scope", "scope_value", "condition", "action", "priority", "is_active"}
        fields = {}
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k == "is_active":
                fields[k] = int(v)
            else:
                fields[k] = v
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [rule_id]
        self._conn.execute(
            f"UPDATE memory_rules SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()

    def delete_rule(self, rule_id: int):
        self._conn.execute("DELETE FROM memory_rules WHERE id = ?", (rule_id,))
        self._conn.commit()

    # -- Memory Archives --

    def insert_archive(self, archive: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO memory_archives(original_memory_id, content, embedding, "
            "archived_at, mismatch_score, archive_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (
                archive["original_memory_id"],
                archive["content"],
                archive.get("embedding"),
                archive.get("archived_at", now),
                archive.get("mismatch_score", 0.0),
                archive.get("archive_reason", ""),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_archives_for_memory(self, memory_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memory_archives WHERE original_memory_id = ? "
            "ORDER BY archived_at DESC",
            (memory_id,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    # -- Memory Transitions --

    def insert_transition(self, transition: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO memory_transitions(from_memory_id, to_memory_id, count, "
            "last_transition, session_id) VALUES (?, ?, ?, ?, ?)",
            (
                transition["from_memory_id"],
                transition["to_memory_id"],
                transition.get("count", 1),
                transition.get("last_transition", now),
                transition.get("session_id", ""),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_transition(self, from_id: int, to_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM memory_transitions WHERE from_memory_id = ? AND to_memory_id = ?",
            (from_id, to_id),
        ).fetchone()
        return self._row_to_dict(row)

    def increment_transition(self, from_id: int, to_id: int):
        now = self._now_iso()
        self._conn.execute(
            "UPDATE memory_transitions SET count = count + 1, last_transition = ? "
            "WHERE from_memory_id = ? AND to_memory_id = ?",
            (now, from_id, to_id),
        )
        self._conn.commit()

    def get_transitions_from(self, memory_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM memory_transitions WHERE from_memory_id = ? "
            "ORDER BY count DESC",
            (memory_id,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_all_transitions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT from_memory_id, to_memory_id, count FROM memory_transitions"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def update_memory_sr_coords(self, memory_id: int, sr_x: float, sr_y: float):
        self._conn.execute(
            "UPDATE memories SET sr_x = ?, sr_y = ? WHERE id = ?",
            (sr_x, sr_y, memory_id),
        )
        self._conn.commit()

    def get_memories_with_sr_coords(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, sr_x, sr_y FROM memories WHERE sr_x != 0.0 OR sr_y != 0.0"
        ).fetchall()
        return self._rows_to_dicts(rows)

    # -- Causal DAG Edges --

    def insert_causal_edge(self, edge: dict) -> int:
        now = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO causal_dag_edges(source_entity_id, target_entity_id, "
            "algorithm, confidence, discovered_at, is_validated) VALUES (?, ?, ?, ?, ?, ?)",
            (
                edge["source_entity_id"],
                edge["target_entity_id"],
                edge.get("algorithm", "pc"),
                edge.get("confidence", 1.0),
                edge.get("discovered_at", now),
                int(edge.get("is_validated", False)),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_causal_edges_for_entity(self, entity_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM causal_dag_edges WHERE source_entity_id = ? OR target_entity_id = ?",
            (entity_id, entity_id),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_all_causal_edges(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM causal_dag_edges ORDER BY confidence DESC"
        ).fetchall()
        return self._rows_to_dicts(rows)

    # -- Engram Slots --

    def init_engram_slots(self, num_slots: int):
        """Ensure all slot indices exist in the engram_slots table."""
        for i in range(num_slots):
            self._conn.execute(
                "INSERT OR IGNORE INTO engram_slots(slot_index, excitability, last_activated) "
                "VALUES (?, 0.0, ?)",
                (i, self._now_iso()),
            )
        self._conn.commit()

    def get_engram_slot(self, slot_index: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM engram_slots WHERE slot_index = ?", (slot_index,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_all_engram_slots(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM engram_slots ORDER BY slot_index"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_engram_slot(self, slot_index: int, excitability: float, last_activated: str):
        self._conn.execute(
            "UPDATE engram_slots SET excitability = ?, last_activated = ? "
            "WHERE slot_index = ?",
            (excitability, last_activated, slot_index),
        )
        self._conn.commit()

    def assign_memory_slot(self, memory_id: int, slot_index: int):
        """Assign a memory to an engram slot and update excitability timestamp."""
        now = self._now_iso()
        self._conn.execute(
            "UPDATE memories SET slot_index = ?, excitability = ?, "
            "last_excitability_update = ? WHERE id = ?",
            (slot_index, 1.0, now, memory_id),
        )
        self._conn.commit()

    def get_memories_in_slot(self, slot_index: int) -> list[dict]:
        """Return all memory IDs assigned to a given slot."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE slot_index = ? AND heat > 0 ORDER BY created_at",
            (slot_index,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_slot_occupancy(self) -> dict:
        """Return {slot_index: count} for all occupied slots."""
        rows = self._conn.execute(
            "SELECT slot_index, COUNT(*) as cnt FROM memories "
            "WHERE slot_index IS NOT NULL GROUP BY slot_index"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    # -- Context manager --

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def update_memory_compression(
        self,
        memory_id: int,
        content: str,
        embedding: bytes | None,
        compression_level: int,
        original_content: str | None = None,
    ):
        """Update a memory's content and compression level after compression."""
        if original_content is not None:
            self._conn.execute(
                "UPDATE memories SET content = ?, embedding = ?, compression_level = ?, "
                "original_content = ? WHERE id = ?",
                (content, embedding, compression_level, original_content, memory_id),
            )
        else:
            self._conn.execute(
                "UPDATE memories SET content = ?, embedding = ?, compression_level = ? "
                "WHERE id = ?",
                (content, embedding, compression_level, memory_id),
            )
        self._conn.commit()
        # Update the vec0 table too
        if embedding is not None:
            try:
                self.update_vector(memory_id, embedding)
            except Exception:
                try:
                    self.insert_vector(memory_id, embedding)
                except Exception:
                    pass

    # -- Checkpoints (Hippocampal Replay) --

    def insert_checkpoint(self, data: dict) -> int:
        """Insert a new checkpoint, deactivating all previous ones."""
        now = self._now_iso()
        self._conn.execute("UPDATE checkpoints SET is_active = 0 WHERE is_active = 1")
        cursor = self._conn.execute(
            """INSERT INTO checkpoints
               (session_id, directory_context, current_task, files_being_edited,
                key_decisions, open_questions, next_steps, active_errors,
                custom_context, epoch, created_at, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                data.get("session_id", "default"),
                data["directory_context"],
                data.get("current_task", ""),
                json.dumps(data.get("files_being_edited", [])),
                json.dumps(data.get("key_decisions", [])),
                json.dumps(data.get("open_questions", [])),
                json.dumps(data.get("next_steps", [])),
                json.dumps(data.get("active_errors", [])),
                data.get("custom_context", ""),
                data.get("epoch", 0),
                now,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def get_active_checkpoint(self) -> dict | None:
        """Get the most recent active checkpoint."""
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        for field in ("files_being_edited", "key_decisions", "open_questions", "next_steps", "active_errors"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except (ValueError, TypeError):
                    d[field] = []
        return d

    def get_current_epoch(self) -> int:
        """Get the current compaction epoch number."""
        row = self._conn.execute("SELECT MAX(epoch) FROM checkpoints").fetchone()
        return row[0] if row[0] is not None else 0

    def increment_epoch(self) -> int:
        """Increment and return the new epoch number."""
        current = self.get_current_epoch()
        return current + 1

    # -- User Profiles --

    def insert_profile(
        self,
        entity_name: str,
        attribute_type: str,
        attribute_key: str,
        attribute_value: str,
        memory_id: int | None = None,
        confidence: float = 0.5,
        directory_context: str | None = None,
    ) -> int:
        now = self._now_iso()
        # Check if profile already exists
        existing = self._conn.execute(
            "SELECT id, confidence, evidence_memory_ids FROM user_profiles "
            "WHERE entity_name = ? AND attribute_type = ? AND attribute_key = ? "
            "AND directory_context IS ?",
            (entity_name, attribute_type, attribute_key, directory_context),
        ).fetchone()

        if existing:
            row = dict(existing)
            new_confidence = min(row["confidence"] + 0.1, 1.0)
            try:
                evidence = json.loads(row["evidence_memory_ids"]) if isinstance(row["evidence_memory_ids"], str) else row["evidence_memory_ids"]
            except (ValueError, TypeError):
                evidence = []
            if memory_id is not None and memory_id not in evidence:
                evidence.append(memory_id)
            self._conn.execute(
                "UPDATE user_profiles SET attribute_value = ?, confidence = ?, "
                "evidence_memory_ids = ?, updated_at = ? WHERE id = ?",
                (attribute_value, new_confidence, json.dumps(evidence), now, row["id"]),
            )
            # Sync FTS
            self._conn.execute(
                "INSERT INTO profiles_fts(profiles_fts, rowid, entity_name, attribute_type, attribute_key, attribute_value) "
                "VALUES('delete', ?, ?, ?, ?, ?)",
                (row["id"], entity_name, attribute_type, attribute_key, attribute_value),
            )
            self._conn.execute(
                "INSERT INTO profiles_fts(rowid, entity_name, attribute_type, attribute_key, attribute_value) "
                "VALUES(?, ?, ?, ?, ?)",
                (row["id"], entity_name, attribute_type, attribute_key, attribute_value),
            )
            self._conn.commit()
            return row["id"]

        evidence = [memory_id] if memory_id is not None else []
        cursor = self._conn.execute(
            "INSERT INTO user_profiles "
            "(entity_name, attribute_type, attribute_key, attribute_value, "
            "evidence_memory_ids, confidence, created_at, updated_at, directory_context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_name, attribute_type, attribute_key, attribute_value,
             json.dumps(evidence), confidence, now, now, directory_context),
        )
        row_id = cursor.lastrowid
        # Sync FTS
        self._conn.execute(
            "INSERT INTO profiles_fts(rowid, entity_name, attribute_type, attribute_key, attribute_value) "
            "VALUES(?, ?, ?, ?, ?)",
            (row_id, entity_name, attribute_type, attribute_key, attribute_value),
        )
        self._conn.commit()
        return row_id

    def search_profiles_fts(self, query: str, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT u.* FROM profiles_fts f "
            "JOIN user_profiles u ON f.rowid = u.id "
            "WHERE profiles_fts MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_profiles_for_entity(self, entity_name: str, directory_context: str | None = None) -> list[dict]:
        if directory_context is not None:
            rows = self._conn.execute(
                "SELECT * FROM user_profiles WHERE entity_name = ? AND directory_context = ?",
                (entity_name, directory_context),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM user_profiles WHERE entity_name = ?",
                (entity_name,),
            ).fetchall()
        return self._rows_to_dicts(rows)

    # -- Derived Beliefs --

    def insert_belief(
        self,
        belief_type: str,
        subject: str,
        content: str,
        evidence_memory_ids: list[int] | None = None,
        confidence: float = 0.5,
        embedding: bytes | None = None,
        embedding_model: str | None = None,
        directory_context: str | None = None,
    ) -> int:
        now = self._now_iso()
        evidence = evidence_memory_ids or []
        cursor = self._conn.execute(
            "INSERT INTO derived_beliefs "
            "(belief_type, subject, content, evidence_memory_ids, confidence, "
            "embedding, embedding_model, created_at, updated_at, directory_context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (belief_type, subject, content, json.dumps(evidence), confidence,
             embedding, embedding_model, now, now, directory_context),
        )
        row_id = cursor.lastrowid
        # Sync FTS
        self._conn.execute(
            "INSERT INTO beliefs_fts(rowid, subject, belief_type, content) "
            "VALUES(?, ?, ?, ?)",
            (row_id, subject, belief_type, content),
        )
        self._conn.commit()
        return row_id

    def search_beliefs_fts(self, query: str, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT b.* FROM beliefs_fts f "
            "JOIN derived_beliefs b ON f.rowid = b.id "
            "WHERE beliefs_fts MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_beliefs_for_subject(self, subject: str, directory_context: str | None = None) -> list[dict]:
        if directory_context is not None:
            rows = self._conn.execute(
                "SELECT * FROM derived_beliefs WHERE subject = ? AND directory_context = ?",
                (subject, directory_context),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM derived_beliefs WHERE subject = ?",
                (subject,),
            ).fetchall()
        return self._rows_to_dicts(rows)

    # ── Group A additions — recall hot path ───────────────────────────────────

    def get_memories_by_ids(self, ids: list[int]) -> list[dict]:
        """Return memories for the given id list (order not guaranteed)."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})", ids
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_hot_memories_for_project(
        self, project_dir: str, min_heat: float = 0.0
    ) -> list[dict]:
        """Return non-stale memories for a directory, sorted heat DESC."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE directory_context = ? "
            "AND heat >= ? AND is_stale = 0 ORDER BY heat DESC",
            (project_dir, min_heat),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_fts_matches(
        self, query: str, limit: int = 20
    ) -> list[tuple[int, float]]:
        """Return (memory_id, bm25_score) pairs from FTS5 full-text search.

        BM25 scores from sqlite FTS5 are negative (lower = better match);
        we negate them so higher = better, consistent with vector scores.
        """
        try:
            rows = self._conn.execute(
                "SELECT m.id, -bm25(memories_fts) AS score "
                "FROM memories m "
                "JOIN memories_fts fts ON m.id = fts.rowid "
                "WHERE memories_fts MATCH ? AND m.is_stale = 0 "
                "ORDER BY score DESC LIMIT ?",
                (self._preprocess_fts_query(query), limit),
            ).fetchall()
            return [(int(r[0]), float(r[1])) for r in rows]
        except Exception:
            return []

    def get_relationships_for_entities(
        self, entity_names: list[str]
    ) -> list[dict]:
        """Return all relationships where either endpoint is in entity_names."""
        if not entity_names:
            return []
        placeholders = ",".join("?" * len(entity_names))
        rows = self._conn.execute(
            f"SELECT r.* FROM relationships r "
            f"JOIN entities e_src ON e_src.id = r.source_entity_id "
            f"JOIN entities e_tgt ON e_tgt.id = r.target_entity_id "
            f"WHERE e_src.name IN ({placeholders}) "
            f"OR e_tgt.name IN ({placeholders})",
            entity_names + entity_names,
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_entity_relationships_graph(self) -> list[dict]:
        """Return all non-archived entity relationships with endpoint names."""
        rows = self._conn.execute(
            "SELECT r.id, r.relationship_type, r.weight, r.created_at, "
            "r.last_reinforced, r.confidence, "
            "e_src.name AS source_name, e_tgt.name AS target_name "
            "FROM relationships r "
            "JOIN entities e_src ON e_src.id = r.source_entity_id "
            "JOIN entities e_tgt ON e_tgt.id = r.target_entity_id "
            "WHERE e_src.archived = 0 AND e_tgt.archived = 0"
        ).fetchall()
        return self._rows_to_dicts(rows)

    # ── Group B — write path ───────────────────────────────────────────────────

    def upsert_memory(
        self,
        content: str,
        embedding: bytes | None,
        heat: float,
        project_dir: str,
        tags: list[str],
        agent_id: str,
        *,
        importance: float = 0.5,
        is_protected: bool = False,
        memory_id: int | None = None,
        source_file: str | None = None,
        section_path: str | None = None,
    ) -> int:  # [COMMITS]
        """Insert a new memory or fully replace an existing one.

        If memory_id is given and exists, the row is updated in-place.
        Returns the memory_id (existing or newly assigned).
        """
        import json as _json
        tags_json = _json.dumps(tags)
        now = self._now_iso()

        if memory_id is not None:
            existing = self._conn.execute(
                "SELECT id FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE memories SET content=?, embedding=?, heat=?, "
                    "directory_context=?, tags=?, last_accessed=?, "
                    "provenance_agent=? WHERE id=?",
                    (content, embedding, heat, project_dir, tags_json,
                     now, agent_id, memory_id),
                )
                self._conn.commit()
                # Sync FTS
                enriched = self._enrich_content_for_fts(content)
                self._conn.execute(
                    "UPDATE memories_fts SET content=? WHERE rowid=?",
                    (enriched, memory_id),
                )
                self._conn.commit()
                # Sync vector table
                if embedding is not None:
                    try:
                        self.update_vector(memory_id, embedding)
                    except Exception:
                        try:
                            self.insert_vector(memory_id, embedding)
                        except Exception:
                            pass
                return memory_id

        # Insert new
        cur = self._conn.execute(
            "INSERT INTO memories(content, embedding, heat, importance, is_protected, "
            "directory_context, tags, created_at, last_accessed, is_stale, provenance_agent) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (content, embedding, heat, importance, int(is_protected),
             project_dir, tags_json, now, now, agent_id),
        )
        self._conn.commit()
        new_id = cur.lastrowid
        enriched = self._enrich_content_for_fts(content)
        self._conn.execute(
            "UPDATE memories_fts SET content=? WHERE rowid=?",
            (enriched, new_id),
        )
        self._conn.commit()
        if embedding is not None:
            try:
                self.insert_vector(new_id, embedding)
            except Exception:
                pass
        return new_id

    def update_memory_access(
        self, memory_id: int, new_heat: float, accessed_at: str
    ) -> None:  # [COMMITS]
        """Update heat and last_accessed together (single commit)."""
        self._conn.execute(
            "UPDATE memories SET heat=?, last_accessed=? WHERE id=?",
            (new_heat, accessed_at, memory_id),
        )
        self._conn.commit()

    def update_memory_tags(
        self, memory_id: int, tags: list[str], agent_id: str
    ) -> None:  # [COMMITS]
        import json as _json
        self._conn.execute(
            "UPDATE memories SET tags=?, provenance_agent=? WHERE id=?",
            (_json.dumps(tags), agent_id, memory_id),
        )
        self._conn.commit()

    def upsert_entity(
        self,
        name: str,
        entity_type: str,
        agent_id: str,
        heat: float = 1.0,
    ) -> int:  # [COMMITS]
        """Insert or reinforce an entity by name. Returns entity id."""
        now = self._now_iso()
        existing = self._conn.execute(
            "SELECT id FROM entities WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE entities SET heat=MIN(heat+0.1,1.0), last_accessed=?, "
                "archived=0 WHERE id=?",
                (now, existing[0]),
            )
            self._conn.commit()
            return int(existing[0])
        cur = self._conn.execute(
            "INSERT INTO entities(name, type, created_at, last_accessed, heat) "
            "VALUES (?,?,?,?,?)",
            (name, entity_type, now, now, heat),
        )
        self._conn.commit()
        return cur.lastrowid

    def upsert_relationship(
        self,
        source: str,
        target: str,
        rel_type: str,
        confidence: float = 1.0,
        event_time: str | None = None,
    ) -> int:  # [COMMITS]
        """Insert or reinforce a relationship between two entity names.

        Entities are created if they do not exist. Returns relationship id.
        """
        now = self._now_iso()
        event_time = event_time or now
        src_id = self.upsert_entity(source, "concept", "system")
        tgt_id = self.upsert_entity(target, "concept", "system")
        existing = self._conn.execute(
            "SELECT id FROM relationships "
            "WHERE source_entity_id=? AND target_entity_id=?",
            (src_id, tgt_id),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE relationships SET weight=weight+1, last_reinforced=?, "
                "confidence=?, event_time=? WHERE id=?",
                (now, confidence, event_time, existing[0]),
            )
            self._conn.commit()
            return int(existing[0])
        cur = self._conn.execute(
            "INSERT INTO relationships(source_entity_id, target_entity_id, "
            "relationship_type, weight, created_at, last_reinforced, "
            "confidence, event_time) VALUES (?,?,?,1.0,?,?,?,?)",
            (src_id, tgt_id, rel_type, now, now, confidence, event_time),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_memory_by_id(self, memory_id: int) -> dict | None:
        """Return a single memory by id, or None (alias with Phase 3 name)."""
        return self.get_memory(memory_id)

    def begin_transaction(self) -> None:
        """Begin an explicit SQLite transaction (deferred)."""
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    def rollback(self) -> None:
        """Roll back the current transaction."""
        self._conn.rollback()

    def batch_update_heat(
        self, updates: list[tuple[int, float]]
    ) -> None:  # [COMMITS]
        """Update heat for multiple memories in a single transaction."""
        if not updates:
            return
        self._conn.executemany(
            "UPDATE memories SET heat=? WHERE id=?",
            [(heat, mid) for mid, heat in updates],
        )
        self._conn.commit()

    # ── Group C — background / consolidation ──────────────────────────────────

    def get_memories_for_decay(
        self, older_than: str, min_heat: float = 0.0
    ) -> list[dict]:
        """Return non-stale memories last accessed before *older_than* ISO timestamp."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE last_accessed < ? "
            "AND heat >= ? AND is_stale = 0 ORDER BY heat ASC",
            (older_than, min_heat),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_duplicate_candidates(
        self, threshold: float = 0.95
    ) -> list[tuple[int, int, float]]:
        """Return (id_a, id_b, cosine_similarity) pairs above *threshold*.

        Loads all embeddings into numpy for brute-force pairwise comparison.
        Intended for infrequent groomer use — not hot-path.
        """
        import numpy as np

        rows = self._conn.execute(
            "SELECT id, embedding FROM memories "
            "WHERE embedding IS NOT NULL AND is_stale = 0"
        ).fetchall()
        if len(rows) < 2:
            return []

        ids = [int(r[0]) for r in rows]
        vecs = np.stack(
            [np.frombuffer(r[1], dtype=np.float32) for r in rows]
        ).astype(np.float32)

        # L2-normalise
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        vecs /= norms

        # Upper-triangle pairwise cosine similarity
        sim = vecs @ vecs.T
        pairs: list[tuple[int, int, float]] = []
        n = len(ids)
        for i in range(n):
            for j in range(i + 1, n):
                s = float(sim[i, j])
                if s >= threshold:
                    pairs.append((ids[i], ids[j], s))
        pairs.sort(key=lambda x: -x[2])
        return pairs

    def get_orphaned_entities(self) -> list[dict]:
        """Return non-archived entities not referenced by any relationship."""
        rows = self._conn.execute(
            "SELECT e.* FROM entities e "
            "WHERE e.archived = 0 "
            "AND e.id NOT IN ("
            "  SELECT source_entity_id FROM relationships "
            "  UNION SELECT target_entity_id FROM relationships"
            ")"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_stale_memories_by_age(
        self, days_since_access: int, max_heat: float = 0.3
    ) -> list[dict]:
        """Return memories not accessed in *days_since_access* days with heat <= max_heat.

        Named distinctly from the existing get_stale_memories() (which filters
        on the is_stale flag) to preserve backward compatibility.
        """
        from datetime import datetime, timezone, timedelta
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days_since_access)
        ).isoformat()
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE last_accessed < ? "
            "AND heat <= ? AND is_stale = 0 ORDER BY heat ASC",
            (cutoff, max_heat),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_consolidation_candidates(self, limit: int = 100) -> list[dict]:
        """Return lowest-heat non-stale memories as consolidation candidates."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE is_stale = 0 "
            "ORDER BY heat ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def mark_memory_archived(self, memory_id: int) -> None:  # [COMMITS]
        """Mark a memory as stale/archived so it is excluded from cache and search."""
        self._conn.execute(
            "UPDATE memories SET is_stale = 1 WHERE id = ?", (memory_id,)
        )
        self._conn.commit()

    def get_episodes_for_memory(self, memory_id: int) -> list[dict]:
        """Return episodes that sourced this memory (via source_episode_id)."""
        rows = self._conn.execute(
            "SELECT e.* FROM episodes e "
            "JOIN memories m ON m.source_episode_id = e.id "
            "WHERE m.id = ?",
            (memory_id,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def upsert_crdt_entry(
        self,
        memory_id: int,
        agent_id: str,
        operation: str,
        vector_clock: str,
        timestamp: str,
    ) -> None:  # [COMMITS]
        """Record a CRDT write operation for multi-agent convergence tracking.

        Creates the crdt_entries table on first use (lazy migration).
        """
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS crdt_entries("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "memory_id INTEGER NOT NULL,"
            "agent_id TEXT NOT NULL,"
            "operation TEXT NOT NULL,"
            "vector_clock TEXT NOT NULL,"
            "timestamp TEXT NOT NULL"
            ")"
        )
        self._conn.execute(
            "INSERT INTO crdt_entries(memory_id, agent_id, operation, "
            "vector_clock, timestamp) VALUES (?,?,?,?,?)",
            (memory_id, agent_id, operation, vector_clock, timestamp),
        )
        self._conn.commit()

    # ── Phase 7: Grooming queries ──────────────────────────────────────────────

    def get_memories_by_filter(
        self,
        directory: str | None = None,
        min_age_days: int | None = None,
        max_heat: float | None = None,
        tags: list[str] | None = None,
        store_type: str | None = None,
        compression_level: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return memories matching filter criteria, ordered oldest first. NO COMMIT."""
        from datetime import datetime, timedelta, timezone as _tz

        conditions: list[str] = []
        params: list = []

        if directory:
            conditions.append("directory_context = ?")
            params.append(directory)
        if min_age_days is not None:
            cutoff = (
                datetime.now(_tz.utc) - timedelta(days=min_age_days)
            ).isoformat()
            conditions.append("created_at <= ?")
            params.append(cutoff)
        if max_heat is not None:
            conditions.append("heat <= ?")
            params.append(max_heat)
        if tags:
            # Use JSON-quoted patterns so "py" does not match "python".
            # Tags are stored as '["tag1","tag2"]'; matching '%" tag "%' finds exact values.
            tag_conds = " OR ".join("tags LIKE ?" for _ in tags)
            conditions.append(f"({tag_conds})")
            for tag in tags:
                params.append(f'%"{tag}"%')
        if store_type:
            conditions.append("store_type = ?")
            params.append(store_type)
        if compression_level is not None:
            conditions.append("compression_level = ?")
            params.append(compression_level)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self._conn.execute(
            f"SELECT * FROM memories {where} ORDER BY created_at ASC LIMIT ?",
            params + [limit],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_orphan_memories(
        self,
        directory: str | None = None,
        max_heat: float = 0.15,
        include_tagged: bool = False,
        min_content_length: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return low-connectivity memories. NO COMMIT."""
        cond = "WHERE heat < ?"
        params: list = [max_heat]
        if not include_tagged:
            cond += " AND (tags IS NULL OR tags = '[]' OR tags = '')"
        if directory:
            cond += " AND directory_context = ?"
            params.append(directory)
        if min_content_length is not None:
            cond += " AND LENGTH(content) >= ?"
            params.append(min_content_length)
        rows = self._conn.execute(
            f"SELECT * FROM memories {cond} ORDER BY heat ASC LIMIT ?",
            params + [limit],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_duplicate_groups(
        self,
        threshold: float = 0.85,
        directory: str | None = None,
        limit: int = 50,
    ) -> list[list[dict]]:
        """Return groups of memories with embedding cosine similarity >= threshold. NO COMMIT."""
        import numpy as np

        dir_cond = "AND directory_context = ?" if directory else ""
        dir_params = [directory] if directory else []
        rows = self._conn.execute(
            "SELECT id, content, embedding, heat, created_at FROM memories "
            f"WHERE embedding IS NOT NULL AND heat > 0 {dir_cond}",
            dir_params,
        ).fetchall()

        if not rows:
            return []

        memories: list[dict] = []
        embeddings_list: list = []
        for row in rows:
            try:
                emb = np.frombuffer(bytes(row["embedding"]), dtype=np.float32).copy()
                norm = float(np.linalg.norm(emb))
                if norm > 0:
                    emb = emb / norm
                embeddings_list.append(emb)
                memories.append(dict(row))
            except Exception:
                pass

        if not embeddings_list:
            return []

        mat = np.stack(embeddings_list)  # (N, D)
        sims = (mat @ mat.T).astype(float)

        visited: set[int] = set()
        groups: list[list[dict]] = []

        for i in range(len(memories)):
            if i in visited:
                continue
            similar = [i]
            for j in range(i + 1, len(memories)):
                if j not in visited and sims[i, j] >= threshold:
                    similar.append(j)

            if len(similar) > 1:
                similar.sort(
                    key=lambda idx: memories[idx].get("heat", 0), reverse=True
                )
                group = []
                for idx in similar:
                    visited.add(idx)
                    group.append(
                        {
                            "memory_id": memories[idx]["id"],
                            "content_preview": (memories[idx].get("content") or "")[
                                :200
                            ],
                            "heat": memories[idx].get("heat", 0.0),
                            "created_at": memories[idx].get("created_at", ""),
                            "similarity_to_anchor": float(sims[similar[0], idx]),
                        }
                    )
                groups.append(group)
                if len(groups) >= limit:
                    break

        return groups

    def get_memory_full(self, memory_id: int) -> dict | None:
        """Full memory record including archive history. NO COMMIT."""
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        d = self._row_to_dict(row)
        archives = self._conn.execute(
            "SELECT * FROM memory_archives WHERE original_memory_id = ? "
            "ORDER BY archived_at DESC",
            (memory_id,),
        ).fetchall()
        d["archive_history"] = self._rows_to_dicts(archives)
        # Also include groomer-specific archive entries if the table exists.
        try:
            groomer_archives = self._conn.execute(
                "SELECT * FROM grooming_archives WHERE memory_id = ? "
                "ORDER BY archived_at DESC",
                (memory_id,),
            ).fetchall()
            d["groomer_archive_history"] = self._rows_to_dicts(groomer_archives)
        except Exception:
            d["groomer_archive_history"] = []
        return d

    def archive_memory(
        self,
        memory_id: int,
        replacement_id: int | None = None,
        reason: str = "groomer",
    ) -> None:  # [COMMITS]
        """Copy memory to grooming_archives. [COMMITS]"""
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS grooming_archives("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "memory_id INTEGER NOT NULL,"
            "replacement_id INTEGER,"
            "content TEXT NOT NULL,"
            "heat REAL DEFAULT 0,"
            "tags TEXT DEFAULT '[]',"
            "archived_at TEXT NOT NULL,"
            "reason TEXT DEFAULT 'groomer'"
            ")"
        )
        mem = self._conn.execute(
            "SELECT content, heat, tags FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if mem:
            self._conn.execute(
                "INSERT INTO grooming_archives "
                "(memory_id, replacement_id, content, heat, tags, archived_at, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    replacement_id,
                    mem["content"],
                    mem["heat"],
                    mem["tags"],
                    self._now_iso(),
                    reason,
                ),
            )
        self._conn.commit()

    def bulk_delete_by_filter(self, filter: dict) -> int:  # [COMMITS]
        """Delete memories matching filter. At least one criterion required. [COMMITS]"""
        if not any(
            filter.get(k)
            for k in ("directory", "min_age_days", "max_heat", "tags", "store_type", "compression_level")
        ):
            raise ValueError(
                "bulk_delete_by_filter requires at least one filter criterion"
            )
        memories = self.get_memories_by_filter(**filter)
        count = 0
        for mem in memories:
            self.delete_memory(mem["id"])
            count += 1
        return count

    def update_memory_full(
        self,
        memory_id: int,
        content: str | None = None,
        tags: list[str] | None = None,
        embedding: bytes | None = None,
        heat: float | None = None,
        directory_context: str | None = None,
        vector_clock: str | None = None,
        provenance_agent: str | None = None,
        last_accessed: str | None = None,
        **kwargs,
    ) -> None:  # [COMMITS]
        """Update multiple memory fields at once. Re-syncs FTS and vectors. [COMMITS]"""
        fields: dict = {}
        if content is not None:
            fields["content"] = content
        if tags is not None:
            fields["tags"] = json.dumps(tags)
        if embedding is not None:
            fields["embedding"] = embedding
        if heat is not None:
            fields["heat"] = heat
        if directory_context is not None:
            fields["directory_context"] = directory_context
        if vector_clock is not None:
            fields["vector_clock"] = vector_clock
        if provenance_agent is not None:
            fields["provenance_agent"] = provenance_agent
        # last_accessed is checked before the early-return guard so a caller
        # passing only last_accessed= still gets the update applied.
        if last_accessed is not None:
            fields["last_accessed"] = last_accessed
        if not fields:
            return
        if "last_accessed" not in fields:
            fields["last_accessed"] = self._now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE memories SET {set_clause} WHERE id = ?",
            list(fields.values()) + [memory_id],
        )
        if content is not None:
            enriched = self._enrich_content_for_fts(content)
            self._conn.execute(
                "UPDATE memories_fts SET content = ? WHERE rowid = ?",
                (enriched, memory_id),
            )
        if embedding is not None:
            try:
                self._conn.execute(
                    "UPDATE memory_vectors SET embedding = ? WHERE rowid = ?",
                    (embedding, memory_id),
                )
            except Exception:
                try:
                    self.insert_vector(memory_id, embedding)
                except Exception:
                    pass
        self._conn.commit()

    def promote_memory(self, memory_id: int) -> None:  # [COMMITS]
        """Set heat=1.0, is_protected=1, add _anchor tag. [COMMITS]"""
        mem = self.get_memory(memory_id)
        if mem is None:
            return
        existing_tags: list = mem.get("tags", [])
        if "_anchor" not in existing_tags:
            existing_tags = list(existing_tags) + ["_anchor"]
        self._conn.execute(
            "UPDATE memories SET heat=1.0, is_protected=1, tags=? WHERE id=?",
            (json.dumps(existing_tags), memory_id),
        )
        self._conn.commit()

    def demote_memory(self, memory_id: int) -> None:  # [COMMITS]
        """Set heat=0.01, is_protected=0, remove _anchor tag. [COMMITS]"""
        mem = self.get_memory(memory_id)
        if mem is None:
            return
        existing_tags = [t for t in (mem.get("tags") or []) if t != "_anchor"]
        self._conn.execute(
            "UPDATE memories SET heat=0.01, is_protected=0, tags=? WHERE id=?",
            (json.dumps(existing_tags), memory_id),
        )
        self._conn.commit()

    # ── Groomer query tools ─────────────────────────────────────────────────────

    def groomer_search(
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
        max_heat: float | None = None,
        limit: int = 50,
        fields: list[str] | None = None,
    ) -> list[dict]:
        """Raw filtered SELECT for groomer — no embedding search, no reranking. NO COMMIT."""
        conditions: list[str] = []
        params: list = []

        if max_heat is not None:
            conditions.append("heat <= ?")
            params.append(max_heat)
        if provenance_agent:
            conditions.append("provenance_agent = ?")
            params.append(provenance_agent)
        if content_like:
            conditions.append("content LIKE ?")
            params.append(f"%{content_like}%")
        if content_length_min is not None:
            conditions.append("LENGTH(content) >= ?")
            params.append(content_length_min)
        if content_length_max is not None:
            conditions.append("LENGTH(content) <= ?")
            params.append(content_length_max)
        if tags_empty is True:
            conditions.append("(tags IS NULL OR tags = '[]' OR tags = '')")
        elif tags_empty is False:
            conditions.append("tags IS NOT NULL AND tags != '[]' AND tags != ''")
        if created_before:
            conditions.append("created_at < ?")
            params.append(created_before)
        if created_after:
            conditions.append("created_at > ?")
            params.append(created_after)
        if id_range and len(id_range) == 2:
            conditions.append("id >= ? AND id <= ?")
            params.extend(id_range[:2])
        if directory:
            conditions.append("directory_context = ?")
            params.append(directory)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self._conn.execute(
            f"SELECT * FROM memories {where} ORDER BY id ASC LIMIT ?",
            params + [limit],
        ).fetchall()

        results = self._rows_to_dicts(rows)
        # Strip embeddings (not useful for groomer output, not JSON-serializable)
        for r in results:
            r.pop("embedding", None)

        if fields:
            allowed = set(fields) | {"id"}
            results = [{k: v for k, v in r.items() if k in allowed} for r in results]

        return results

    def groomer_provenance(self, directory: str | None = None) -> list[dict]:
        """Distinct provenance_agent values with counts, date ranges, top tags. NO COMMIT."""
        dir_cond = "WHERE directory_context = ?" if directory else ""
        dir_params = [directory] if directory else []

        rows = self._conn.execute(
            f"SELECT provenance_agent, COUNT(*) as count, "
            f"MIN(created_at) as first_seen, MAX(created_at) as last_seen, "
            f"GROUP_CONCAT(tags, '|||') as all_tags "
            f"FROM memories {dir_cond} "
            f"GROUP BY provenance_agent ORDER BY count DESC",
            dir_params,
        ).fetchall()

        result = []
        for row in rows:
            row_dict = dict(row)
            # Extract top tags from concatenated tag arrays
            tag_counts: dict[str, int] = {}
            raw_tags = row_dict.pop("all_tags", "") or ""
            for tag_json in raw_tags.split("|||"):
                try:
                    tags = json.loads(tag_json) if tag_json.strip() else []
                except (ValueError, TypeError):
                    tags = []
                for t in tags:
                    if t and not t.startswith("_"):
                        tag_counts[t] = tag_counts.get(t, 0) + 1
            top_tags = sorted(tag_counts, key=tag_counts.get, reverse=True)[:5]
            row_dict["top_tags"] = top_tags
            result.append(row_dict)
        return result

    def close(self):
        self._conn.close()
