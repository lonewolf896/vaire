import configparser
from functools import lru_cache
from pathlib import Path
from typing import Any, Tuple, Type

from pydantic import model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

# ── INI config loading ────────────────────────────────────────────────────────
# Read vaire.ini (local overrides) and set environment variables for any keys
# not already present.  Environment variables always win.
#
# Section/key mapping:
#   [paths]   host_workspace  → VAIRE_HOST_WORKSPACE  (special: also builds PATH_REMAP)
#   [*]       <key>           → VAIRE_<KEY>  (uppercased)

_INI_SEARCH_PATHS = [
    Path("vaire.ini"),                       # repo root (dev)
    Path.home() / ".vaire" / "vaire.ini",    # user-level
]


def _load_ini() -> dict[str, str]:
    """Load the first vaire.ini found and return as a flat {KEY: value} dict.

    Keys are uppercased field names (no VAIRE_ prefix).
    """
    cfg = configparser.ConfigParser()
    for p in _INI_SEARCH_PATHS:
        if p.is_file():
            cfg.read(p)
            break
    else:
        return {}

    ini_values: dict[str, str] = {}
    for section in cfg.sections():
        for key, value in cfg.items(section):
            ini_values[key.upper()] = value

    # Special: build PATH_REMAP from host_workspace if not already set
    host_ws = ini_values.get("HOST_WORKSPACE", "")
    if host_ws and "PATH_REMAP" not in ini_values:
        host_ws = str(Path(host_ws).expanduser())
        ini_values["PATH_REMAP"] = f"/workspace:{host_ws}"

    return ini_values


_INI_VALUES = _load_ini()

# Cache for _parse_remap results, keyed by PATH_REMAP string value.
_REMAP_NONE = object()  # sentinel for "parsed but result is None"
_remap_cache: dict = {}


class IniSettingsSource(PydanticBaseSettingsSource):
    """Pydantic-settings source that reads from vaire.ini.

    Inserted AFTER env vars in the source chain so env vars always win.
    """

    def get_field_value(
        self, field: Any, field_name: str
    ) -> Tuple[Any, str, bool]:
        val = _INI_VALUES.get(field_name.upper())
        return val, field_name, False

    def __call__(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            val, _, _ = self.get_field_value(field_info, field_name)
            if val is not None:
                d[field_name] = val
        return d


class Settings(BaseSettings):
    PORT: int = 8742
    IDLE_THRESHOLD_SECONDS: int = 300
    ACTION_LOG_INTERVAL: int = 60  # light cycle: decay + action log processing
    MEDIUM_CYCLE_INTERVAL: int = 900  # medium cycle: + entity extraction + merge (15 min)
    SLEEP_CYCLE_MIN_GAP_HOURS: float = 6.0  # minimum hours between full sleep cycles
    DECAY_FACTOR: float = 0.95
    COLD_THRESHOLD: float = 0.05
    HOT_THRESHOLD: float = 0.7
    MAX_EPISODE_TOKENS: int = 50000
    OVERLAP_TOKENS: int = 2000
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    DAEMON_CHECK_INTERVAL: int = 30
    DB_PATH: str = "~/.vaire/memory.db"
    HOST_WORKSPACE: str = ""  # host-side workspace path (used to build PATH_REMAP)

    # v2 settings
    IMPORTANCE_DECAY_FACTOR: float = 0.998
    SURPRISE_BOOST: float = 0.3
    EMOTIONAL_DECAY_RESISTANCE: float = 0.5
    DREAM_REPLAY_PAIRS: int = 20
    FRACTAL_LEVELS: int = 3
    CLUSTER_SIMILARITY_THRESHOLD: float = 0.7
    PPR_DAMPING: float = 0.85
    PPR_ITERATIONS: int = 50
    CAUSAL_THRESHOLD: int = 2
    SYNAPTIC_WINDOW_MINUTES: int = 30
    SYNAPTIC_BOOST: float = 0.2
    NUM_ASTROCYTE_PROCESSES: int = 4
    NARRATIVE_INTERVAL_HOURS: int = 24
    CONTEXTUAL_PREFIX_ENABLED: bool = True
    CURATION_SIMILARITY_THRESHOLD: float = 0.85

    # v3 frontier settings
    HOPFIELD_BETA: float = 8.0  # Hopfield sharpness (low=blended, high=precise)
    HOPFIELD_MAX_PATTERNS: int = 5000  # Max patterns in Hopfield energy store
    RECONSOLIDATION_LOW_THRESHOLD: float = 0.45  # Below this: no modification on recall
    RECONSOLIDATION_HIGH_THRESHOLD: float = 0.7  # Above this: archive old + create new
    RECONSOLIDATION_COOLDOWN_HOURS: float = 24.0  # Min hours between reconsolidations
    PLASTICITY_SPIKE: float = 0.3  # How much plasticity increases on access
    PLASTICITY_HALF_LIFE_HOURS: float = 6.0  # Plasticity decay half-life
    STABILITY_INCREMENT: float = 0.1  # Stability increase per successful retrieval
    EXCITABILITY_HALF_LIFE_HOURS: float = 6.0  # Engram excitability decay half-life
    EXCITABILITY_BOOST: float = 0.5  # Excitability increase on slot activation
    WRITE_GATE_THRESHOLD: float = 0.4  # Min surprisal to pass write gate
    DEDUP_THRESHOLD: float = 0.85         # Cosine similarity above which = duplicate
    DEDUP_WINDOW_HOURS: int = 24          # Time window for dedup check
    COMPRESSION_GIST_AGE_HOURS: float = 168.0  # 7 days before gist compression
    COMPRESSION_TAG_AGE_HOURS: float = 720.0  # 30 days before tag compression
    HDC_DIMENSIONS: int = 10000  # Hyperdimensional vector size
    SR_DISCOUNT: float = 0.9  # Successor representation discount factor γ
    SR_UPDATE_RATE: float = 0.1  # Incremental SR update learning rate
    COGNITIVE_LOAD_LIMIT: int = 4  # Max chunks in active context (Cowan's 4±1)
    CRDT_AGENT_ID: str = "default"  # Agent identifier for multi-agent CRDT

    # v4: Hippocampal Replay settings
    REPLAY_MAX_RESTORE_MEMORIES: int = 8  # Max memories to include in restoration
    REPLAY_ANCHOR_HEAT: float = 1.0  # Heat assigned to anchored memories
    REPLAY_CHECKPOINT_AUTO_INTERVAL: int = 50  # Auto-checkpoint every N tool calls

    # v5: Zero-gap memory persistence settings
    WRITE_GATE_CONTINUITY_DISCOUNT: float = 0.15  # Threshold reduction for task-continuous content
    WRITE_GATE_CONTINUITY_WINDOW: int = 10  # Number of recent stores to track for continuity
    MICRO_CHECKPOINT_ENABLED: bool = True  # Auto-checkpoint on significant events
    MICRO_CHECKPOINT_COOLDOWN: int = 5  # Min tool calls between micro-checkpoints
    SESSION_COHERENCE_BONUS: float = 0.2  # Heat bonus for current-session memories
    SESSION_COHERENCE_WINDOW_HOURS: float = 4.0  # How long the session coherence lasts
    REINJECTION_ENABLED: bool = False  # Disabled: 10s+ per write. Agents call recall() when needed.
    REINJECTION_MAX_RESULTS: int = 3  # Max related memories to reinject
    DECISION_AUTO_PROTECT: bool = True  # Auto-protect detected decisions from decay
    ACTION_STREAM_ENABLED: bool = True  # Capture tool actions in sensory buffer

    # v6: WRRF (Weighted Reciprocal Rank Fusion) settings
    WRRF_K: int = 60  # RRF constant k
    WRRF_CANDIDATE_MULTIPLIER: int = 10  # Candidate pool = max_results * this
    WRRF_VECTOR_WEIGHT: float = 1.0
    WRRF_FTS_WEIGHT: float = 0.0
    WRRF_PPR_WEIGHT: float = 0.5
    WRRF_SPREADING_WEIGHT: float = 0.3
    WRRF_HOPFIELD_WEIGHT: float = 0.2
    WRRF_HDC_WEIGHT: float = 0.3
    WRRF_FRACTAL_WEIGHT: float = 0.2
    WRRF_SR_WEIGHT: float = 0.3
    RERANKER_ENABLED: bool = True
    RERANKER_TOP_K: int = 50

    # v7: Query routing settings
    QUERY_ROUTING_ENABLED: bool = True
    TEMPORAL_KEYWORDS: str = "yesterday,today,last week,last month,last session,recently,before,after,when,during,while,since,until,earlier,later,previous,next,morning,evening,night,ago,back then"
    CODE_KEYWORDS: str = "function,class,method,variable,import,error,bug,fix,refactor,implement,API,endpoint,database,schema,test,deploy"
    RELATIONAL_KEYWORDS: str = "relationship,connection,related,between,link,cause,effect,impact,influence,depend,lead to,result in"

    # v8: Confidence gating settings
    CONFIDENCE_GATING_ENABLED: bool = True
    CONFIDENCE_MIN_RESULTS: int = 3
    CONFIDENCE_SCORE_SPREAD_THRESHOLD: float = 0.15
    CONFIDENCE_TOP_SCORE_THRESHOLD: float = 0.5
    CONFIDENCE_FALLBACK_STRATEGY: str = "expand"

    # v9: Temporal retrieval settings
    TEMPORAL_RETRIEVAL_ENABLED: bool = True
    TEMPORAL_BOOST_WEIGHT: float = 0.4
    TEMPORAL_DECAY_DAYS: int = 30
    TEMPORAL_EXACT_MATCH_BOOST: float = 2.0

    # v10: Cross-encoder reranking settings
    CROSS_ENCODER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    CROSS_ENCODER_ENABLED: bool = True  # FlashRank ONNX is fast enough for CPU
    CROSS_ENCODER_TOP_K: int = 20
    CROSS_ENCODER_WEIGHT: float = 0.6  # CE weight in blend (retrieval gets 1-this)

    # v11: FTS5 enhancement settings
    FTS5_PORTER_STEMMER: bool = False
    FTS5_IDENTIFIER_SPLITTING: bool = True
    FTS5_ENTITY_BOOST: bool = True
    FTS5_MIN_TERM_LENGTH: int = 2

    # v12: Graph signal optimization settings
    GRAPH_MAX_HOPS: int = 2
    GRAPH_MIN_EDGE_WEIGHT: float = 0.1
    GRAPH_SPREADING_DECAY: float = 0.5
    GRAPH_SPREADING_MAX_DEPTH: int = 2
    GRAPH_ENTITY_MIN_LENGTH: int = 3
    GRAPH_ENTITY_EXTRA_STOPWORDS: str = ""  # comma-separated additional stopwords

    # v13: Adversarial protection settings
    ADVERSARIAL_DETECTION_ENABLED: bool = True
    ADVERSARIAL_SCORE_GAP_THRESHOLD: float = 0.05
    ADVERSARIAL_DIVERSITY_ENFORCEMENT: bool = True
    ADVERSARIAL_MIN_CONFIDENCE: float = 0.3

    # v14: Embedding enhancement settings
    CANDIDATE_POOL_MULTIPLIER: int = 20
    EMBEDDING_CACHE_SIZE: int = 128
    QUERY_PREFIX: str = ""

    # v16: Query expansion (pseudo-HyDE) settings
    QUERY_EXPANSION_ENABLED: bool = True

    # v15: Fusion optimization settings
    COMBMNZ_ENABLED: bool = False  # CombMNZ: multiply fused score by signal count
    FUSION_NORM: str = "zscore"  # "zscore", "minmax", or "raw"

    # v17 Index-Time Enrichment Settings
    INDEX_ENRICHMENT_ENABLED: bool = True
    CONCEPTNET_ENRICHMENT_ENABLED: bool = True
    CONCEPTNET_MIN_EDGE_WEIGHT: float = 1.0
    CONCEPTNET_MAX_TERMS: int = 10
    CONCEPTNET_RELATIONS: str = "IsA,UsedFor,HasProperty,AtLocation,MotivatedByGoal,CausesDesire,CapableOf"
    COMET_ENRICHMENT_ENABLED: bool = True
    COMET_QUERY_EXPANSION_ENABLED: bool = False  # COMET at query time for open_domain
    COMET_MODEL: str = "mismayil/comet-bart-ai2"
    COMET_NUM_BEAMS: int = 5
    COMET_TOP_K_PER_RELATION: int = 3
    COMET_MIN_CONFIDENCE: float = 0.3
    COMET_RELATIONS: str = "xAttr,xIntent,xWant"
    DOC2QUERY_ENRICHMENT_ENABLED: bool = True
    DOC2QUERY_MODEL: str = "doc2query/msmarco-t5-small-v1"
    DOC2QUERY_NUM_QUERIES: int = 5
    LOGIC_ENRICHMENT_ENABLED: bool = True
    FPA_SIMILARITY_THRESHOLD: float = 0.25
    ENRICHMENT_MIN_CONTENT_LENGTH: int = 20

    # v18 Structured Profiles (Memobase)
    PROFILE_EXTRACTION_ENABLED: bool = True
    PROFILE_CONFIDENCE_DIRECT: float = 0.7
    PROFILE_CONFIDENCE_INFERRED: float = 0.4
    PROFILE_SEARCH_WEIGHT: float = 0.8
    PROFILE_SUMMARY_ENABLED: bool = True

    # v19 Derived Beliefs (Hindsight)
    DERIVED_BELIEFS_ENABLED: bool = True
    BELIEF_MIN_CONFIDENCE: float = 0.3
    BELIEF_HIGH_CONFIDENCE_BOOST: float = 1.2
    BELIEF_SEARCH_PRIORITY_FOR_OPEN_DOMAIN: bool = True

    # v20 Comparison Query Routing
    COMPARISON_DUAL_SEARCH_ENABLED: bool = True
    COMPARISON_TOP_K_PER_OPTION: int = 10

    # v21 Fusion Method
    FUSION_METHOD: str = "convex"

    # v22 Advanced Reranking — GTE-Reranker
    GTE_RERANKER_ENABLED: bool = True
    GTE_RERANKER_MODEL: str = "Alibaba-NLP/gte-reranker-modernbert-base"
    GTE_RERANKER_MAX_LENGTH: int = 512
    GTE_RERANKER_FALLBACK_TO_FLASHRANK: bool = True

    # v23 NLI Entailment Scoring
    NLI_RERANKING_ENABLED: bool = True
    NLI_MODEL: str = "cross-encoder/nli-deberta-v3-base"
    NLI_WEIGHT: float = 0.3
    NLI_ONLY_FOR_OPEN_DOMAIN: bool = True

    # v24 Multi-Passage Evidence Aggregation
    MULTI_PASSAGE_RERANKING_ENABLED: bool = True
    MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD: float = 0.3
    MULTI_PASSAGE_MAX_CLUSTER_SIZE: int = 3

    # v25 Dual-Vector Architecture (prep only, not active until DualCSE trained)
    DUAL_VECTORS_ENABLED: bool = False
    IMPLICIT_EMBEDDING_MODEL: str = ""
    IMPLICIT_VECTOR_WEIGHT: float = 0.5

    # ── Response size control ────────────────────────────────────────────────
    RECALL_DEFAULT_MAX_TOKENS: int = 8000   # Applied when caller omits max_tokens (≈28K chars)
    RECALL_STRIP_INTERNAL_FIELDS: bool = True  # Strip enrichment/internal fields from responses

    # ── Socket authentication ────────────────────────────────────────────────
    SOCKET_AUTH_ENABLED: bool = True
    SOCKET_AUTH_TOKENS_DIR: str = "~/.vaire/tokens"

    # Phase 1: Unix domain socket server settings
    SOCKET_PATH: str = "~/.vaire/vaire.sock"
    PID_FILE: str = "~/.vaire/vaire.pid"
    WRITE_BATCH_SIZE: int = 20          # flush write queue after N queued writes
    WRITE_BATCH_INTERVAL_MS: int = 50   # or after this many milliseconds
    MAX_CLIENTS: int = 32               # max concurrent socket connections
    GROOMER_ID_PREFIX: str = "groomer-" # agent_id prefix that grants groomer role
    CALL_TIMEOUT_SECONDS: float = 30.0  # per-call timeout on the thin client

    # Phase 8: Markdown ingestion
    INGEST_CHUNK_MIN: int = 200            # minimum chars per chunk
    INGEST_CHUNK_MAX: int = 2000           # maximum chars per chunk
    INGEST_CHUNK_OVERLAP: int = 100        # overlap chars between adjacent chunks
    INGEST_ALLOWED_EXTS: list[str] = [".md", ".txt", ".rst"]
    INGEST_ENTITY_EXTRACTION_DELAY_MS: int = 5000  # delay before triggering consolidation
    # Importance assigned to ingested chunks. Must exceed 0.7 to use
    # IMPORTANCE_DECAY_FACTOR (0.998/hr) instead of DECAY_FACTOR (0.95/hr).
    # At 0.8: chunks stay warm ~59 days without access (vs ~2 days at 0.5).
    INGEST_DEFAULT_IMPORTANCE: float = 0.8
    # Path prefix remap applied to directory_context during ingestion.
    # Use when the server runs in Docker: "CONTAINER_PREFIX:HOST_PREFIX"
    # e.g. "/workspace:/home/alice/workspace" rewrites /workspace/foo → /home/alice/workspace/foo
    PATH_REMAP: str = ""

    # ── mTLS remote access ────────────────────────────────────────────────────
    TLS_CERT: str = ""              # Server certificate PEM path
    TLS_KEY: str = ""               # Server private key PEM path
    TLS_CA: str = ""                # CA cert PEM (verifies client certs)
    HTTPS_BIND: str = ""            # "host:port" — empty = disabled
    HTTPS_TRANSPORT: str = "streamable-http"  # "sse" or "streamable-http"
    HTTPS_ALLOWED_HOSTS: str = "*"  # comma-separated allowed Host headers; "*" disables check (mTLS is auth boundary)

    # ── Reference system ────────────────────────────────────────────────────
    REFERENCE_PATH: str = "/app/reference"       # container path (baked into image)
    REFERENCE_MANIFEST: str = "manifest.json"    # filename within REFERENCE_PATH

    # ── GitLab task sync ─────────────────────────────────────────────────────
    GITLAB_API_URL: str = ""                     # e.g. "https://gitlab.example.com/api/v4"
    GITLAB_PROJECT_ID: str = ""                  # e.g. "42"
    GITLAB_TOKEN: str = ""                       # project access token — env var ONLY
    GITLAB_TASKS_FILE: str = "tasks.json"        # path within repo
    GITLAB_TASKS_BRANCH: str = "main"
    TASK_SYNC_INTERVAL: int = 30                 # seconds between GitLab syncs
    TASK_HEARTBEAT_TTL: int = 30                 # minutes before task considered abandoned
    TASK_DATA_PATH: str = "/data/tasks.json"     # runtime writable task file
    TASK_CREATE_ALLOWED: str = ""                # comma-separated agent_id prefixes; empty = unrestricted

    # ── Security limits ───────────────────────────────────────────────────────
    MAX_CONTENT_LENGTH: int = 50000   # max chars for memory content
    MAX_TAG_COUNT: int = 50           # max tags per memory
    MAX_TAG_LENGTH: int = 100         # max chars per individual tag
    RATE_LIMIT_PER_MINUTE: int = 120  # per-connection request rate limit
    RATE_LIMIT_BURST: int = 20        # burst allowance above steady rate
    INGEST_ALLOWED_DIRS: str = ""     # comma-separated allowed dir prefixes for ingest
    REGEX_TIMEOUT_MATCHES: int = 100  # max matches per regex scan in groomer
    PROMPT_INJECTION_DETECTION: bool = True  # flag suspicious content on write

    model_config = {"env_prefix": "VAIRE_"}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        """Priority (highest first): init kwargs → env vars → vaire.ini → defaults."""
        return (
            init_settings,
            env_settings,
            IniSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _validate_ingest_chunk_sizes(self) -> "Settings":
        if self.INGEST_CHUNK_OVERLAP < 0:
            raise ValueError(
                f"INGEST_CHUNK_OVERLAP ({self.INGEST_CHUNK_OVERLAP}) must be >= 0"
            )
        if self.INGEST_CHUNK_OVERLAP >= self.INGEST_CHUNK_MAX:
            raise ValueError(
                f"INGEST_CHUNK_OVERLAP ({self.INGEST_CHUNK_OVERLAP}) must be "
                f"less than INGEST_CHUNK_MAX ({self.INGEST_CHUNK_MAX})"
            )
        if self.INGEST_CHUNK_MIN > self.INGEST_CHUNK_MAX:
            raise ValueError(
                f"INGEST_CHUNK_MIN ({self.INGEST_CHUNK_MIN}) must be "
                f"<= INGEST_CHUNK_MAX ({self.INGEST_CHUNK_MAX})"
            )
        return self

    @model_validator(mode="after")
    def _validate_tls_settings(self) -> "Settings":
        tls_fields = [self.TLS_CERT, self.TLS_KEY, self.TLS_CA]
        set_count = sum(1 for f in tls_fields if f)
        if 0 < set_count < 3:
            raise ValueError(
                "TLS_CERT, TLS_KEY, and TLS_CA must all be set together "
                f"(got {set_count}/3)"
            )
        if self.HTTPS_BIND and not all(tls_fields):
            raise ValueError(
                "HTTPS_BIND requires TLS_CERT, TLS_KEY, and TLS_CA to be set"
            )
        if self.HTTPS_BIND and ":" not in self.HTTPS_BIND:
            raise ValueError(
                f"HTTPS_BIND must be 'host:port', got '{self.HTTPS_BIND}'"
            )
        return self

    @model_validator(mode="after")
    def _validate_gitlab_settings(self) -> "Settings":
        gitlab_fields = [self.GITLAB_API_URL, self.GITLAB_PROJECT_ID]
        if any(gitlab_fields) and not all(gitlab_fields):
            raise ValueError("GITLAB_API_URL and GITLAB_PROJECT_ID must both be set")
        return self

    @property
    def tls_enabled(self) -> bool:
        return bool(self.TLS_CERT and self.TLS_KEY and self.TLS_CA and self.HTTPS_BIND)

    @property
    def https_host(self) -> str:
        if not self.HTTPS_BIND:
            return ""
        return self.HTTPS_BIND.rsplit(":", 1)[0]

    @property
    def https_port(self) -> int:
        if not self.HTTPS_BIND:
            return 0
        return int(self.HTTPS_BIND.rsplit(":", 1)[1])

    @property
    def tls_cert_resolved(self) -> Path:
        return Path(self.TLS_CERT).expanduser()

    @property
    def tls_key_resolved(self) -> Path:
        return Path(self.TLS_KEY).expanduser()

    @property
    def tls_ca_resolved(self) -> Path:
        return Path(self.TLS_CA).expanduser()

    @property
    def ingest_allowed_dirs_list(self) -> list[str]:
        if not self.INGEST_ALLOWED_DIRS:
            return []
        return [d.strip() for d in self.INGEST_ALLOWED_DIRS.split(",") if d.strip()]

    @property
    def socket_auth_tokens_dir_resolved(self) -> Path:
        return Path(self.SOCKET_AUTH_TOKENS_DIR).expanduser()

    @property
    def db_path_resolved(self) -> Path:
        return Path(self.DB_PATH).expanduser()

    @property
    def socket_path_resolved(self) -> Path:
        return Path(self.SOCKET_PATH).expanduser()

    @property
    def pid_file_resolved(self) -> Path:
        return Path(self.PID_FILE).expanduser()

    @property
    def reference_path_resolved(self) -> Path:
        return Path(self.REFERENCE_PATH)

    @property
    def reference_manifest_resolved(self) -> Path:
        return self.reference_path_resolved / self.REFERENCE_MANIFEST

    @property
    def task_data_path_resolved(self) -> Path:
        return Path(self.TASK_DATA_PATH).expanduser()

    @property
    def gitlab_enabled(self) -> bool:
        return bool(self.GITLAB_API_URL and self.GITLAB_PROJECT_ID and self.GITLAB_TOKEN)

    @property
    def task_create_allowed_list(self) -> list[str]:
        if not self.TASK_CREATE_ALLOWED:
            return []
        return [p.strip() for p in self.TASK_CREATE_ALLOWED.split(",") if p.strip()]

    @staticmethod
    def _prefix_matches(path: str, prefix: str) -> bool:
        """Check if path starts with prefix at a directory boundary.

        Prevents '/workspace' from matching '/workspace2/foo'.
        Only matches if the prefix is followed by '/' or is the entire path.
        """
        if not path.startswith(prefix):
            return False
        return len(path) == len(prefix) or path[len(prefix)] == "/"

    def _parse_remap(self) -> tuple[str, str] | None:
        """Parse PATH_REMAP into (container_prefix, host_prefix).

        Returns None if unset, malformed, or either prefix is empty.
        Result is cached in module-level dict keyed by PATH_REMAP value.
        """
        cached = _remap_cache.get(self.PATH_REMAP)
        if cached is not None:
            return cached if cached != _REMAP_NONE else None
        if not self.PATH_REMAP or ":" not in self.PATH_REMAP:
            _remap_cache[self.PATH_REMAP] = _REMAP_NONE
            return None
        container_prefix, host_prefix = self.PATH_REMAP.split(":", 1)
        if not container_prefix or not host_prefix:
            _remap_cache[self.PATH_REMAP] = _REMAP_NONE
            return None
        result = (container_prefix, host_prefix)
        _remap_cache[self.PATH_REMAP] = result
        return result

    def remap_path(self, path: str) -> str:
        """Rewrite a container-side path to its host equivalent.

        Reads PATH_REMAP ("container_prefix:host_prefix").  No-op if unset.
        """
        parsed = self._parse_remap()
        if parsed is None:
            return path
        container_prefix, host_prefix = parsed
        if self._prefix_matches(path, container_prefix):
            return host_prefix + path[len(container_prefix):]
        return path

    def reverse_remap_path(self, path: str) -> str:
        """Rewrite a host-side path back to its container equivalent.

        Inverse of remap_path().  No-op if PATH_REMAP is unset.
        """
        parsed = self._parse_remap()
        if parsed is None:
            return path
        container_prefix, host_prefix = parsed
        if self._prefix_matches(path, host_prefix):
            return container_prefix + path[len(host_prefix):]
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
