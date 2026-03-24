# Vaire

<!-- mcp-name: io.github.lonewolf896/vaire -->

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-1215%20passed-brightgreen)](#testing)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

*Named after Vairë the Weaver, who weaves the story of the world in the halls of Mandos.*

Forked from [Zikkaron](https://github.com/amanhij/Zikkaron) by amanhij.

Your AI forgets you every time you close the tab. Every architecture decision you explained, every debugging rabbit hole you went down together, every "remember, we're using Postgres not SQLite" correction. Gone. You start the next session a stranger to your own tools.

Vaire is a persistent memory engine for Claude Code built on computational neuroscience. It remembers what you worked on, how you think, what you decided and why. Not as a dumb text dump that gets shoved into context, but as a living memory system that consolidates, forgets intelligently, and reconstructs the right context at the right time.

26 subsystems. 27 MCP tools. Runs entirely on your machine. One SQLite file.

## Executive Summary

Vaire is a persistent memory engine for Claude Code, grounded in computational neuroscience. Where conventional AI context management relies on flat text dumps, Vaire operates as a living memory system — one that consolidates, forgets intelligently, and reconstructs the right context at the right time. It runs entirely on your machine, backed by a single SQLite file, and exposes 27 MCP tools across 26 subsystems.

At its core, Vaire models human memory biology rather than mimicking a database. A predictive write gate filters incoming information against what the system already knows, blocking redundant writes — the same mechanism the neocortex uses to suppress predictable sensory input. Memories carry heat: frequently accessed memories stay hot; unused ones cool, compress from full content to gist to tags, and eventually fade. Retrieval is equally principled: a multi-signal WRRF fusion of vector similarity (all-MiniLM-L6-v2), BM25 full-text search, knowledge graph personalized PageRank, spreading activation, Hopfield, HDC, fractal, and SR signals — reranked by GTE-Reranker (gte-reranker-modernbert-base) — surfaces what matters, not just what matches.

At write time, Doc2Query (msmarco-t5-small-v1) generates synthetic search queries from each memory's content. These are embedded alongside the original text, improving recall for queries that don't share vocabulary with the stored memory. The result is measurably better retrieval on paraphrase and cross-domain lookups without any additional latency at query time.

Two capabilities set Vaire apart from simpler context systems. Hippocampal Replay guards against context compaction: hooks drain working state into a checkpoint before the window fills, then reconstruct context afterward from checkpoints, anchored facts, hot memories, and successor-representation predictions — rebuilding the cognitive map, not just replaying a transcript. Reconsolidation updates retrieved memories in place when context has shifted, and archives the prior version on severe mismatch, mirroring how biological memory rewrites on recall.

Version 1.3.0 adds zero-gap capture: PostToolUse hooks record every tool action automatically, and SessionStart hooks inject project context on every new session — eliminating the manual overhead of memory maintenance.

On established benchmarks, Vaire scores 87.9% Recall@10 and MRR 0.686 on LoCoMo, and 96.7% Recall@10 with MRR 0.945 on LongMemEval — where the prior published best is 78.4% Recall@10. Knowledge Update MRR reaches 1.000, a direct consequence of the reconsolidation mechanism.

Ingest chunks — markdown files, specs, runbooks — are stored with `is_protected=True` and `importance=0.8`, putting them on the slow-decay path (~59-day half-life) and shielding them from deduplication. They survive indefinitely unless explicitly forgotten.

---

## Three-tier consolidation: how Vaire thinks while you work

Vaire's consolidation daemon runs continuously, not just when you're idle. It operates on three tiers, modeled on the distinction between waking maintenance and deep sleep consolidation in biological memory:

**Light cycle (every 60 seconds)** — The always-on heartbeat. Applies thermodynamic decay to memory heat values (unused memories cool, accessed ones stay warm) and processes the action log. Raw tool calls captured by the PostToolUse hook are grouped into 30-minute windows and transformed into outcome narratives: "edited: consolidation.py; git: commit, push; errors encountered: timezone mismatch." Only complete time windows are processed — the current bucket stays open until it closes.

**Medium cycle (every 15 minutes)** — Keeps the knowledge graph fresh during active work. Extracts entities and relationships from new memories and merges near-duplicates. This is what makes `recall` and `navigate_memory` accurate even during long sessions — the graph doesn't go stale waiting for idle time.

**Full cycle (5 minutes of idle)** — The heavy machinery. Runs causal discovery (PC algorithm on entity relationships), the memify self-improvement cycle (prune dead memories, strengthen high-value ones, derive new patterns from the knowledge graph), CLS dual-store promotion (episodic to semantic), and rate-distortion compression of old memories. These phases are computationally expensive and benefit from running without contention.

**Sleep cycle (6-hour minimum gap)** — The deepest consolidation. Dream replay compares random memory pairs to discover non-obvious connections. Louvain community detection reorganizes the fractal hierarchy. Temporal compression collapses related episodes. This mirrors how biological sleep consolidates the day's experiences into structured long-term knowledge. The gap is configurable (`VAIRE_SLEEP_CYCLE_MIN_GAP_HOURS`) and persisted across container restarts.

Every incoming memory passes through a **predictive coding write gate** that computes surprisal — how much the new information violates what Vaire already knows. Redundant information is blocked. Action log entries get an additional **outcome extraction** step that transforms raw tool calls into what-was-accomplished narratives, and a **specificity filter** that only derives patterns from code identifiers (file paths, function names, error types) rather than generic English words.

The result: Vaire runs as an always-on daemon serving multiple agents, consolidating knowledge continuously without waiting for silence.

## What this actually feels like

**Monday.** You spend an hour debugging a nasty auth token race condition. Claude helps you trace it to a TTL mismatch between Redis and your JWT config. You fix it. Claude stores the memory.

**Thursday.** A user reports intermittent logouts. You open Claude Code in the same project. Before you even describe the bug, Claude recalls the Redis TTL fix from Monday, checks if it's related, and asks whether the middleware you added is handling the edge case where Redis restarts mid-session.

That's the difference. Not "here's your conversation history." Real recall. The kind where your tools understand the shape of what you've been building, not just the words you typed last time.

## Retrieval that actually works

We tested Vaire against [LoCoMo](https://snap-research.github.io/locomo/) (Maharana et al., ACL 2024), the standard benchmark for long conversation memory. 10 conversations, 1,986 questions, everything from simple factual lookups to multi-hop reasoning to adversarial trick questions designed to trip you up.

| | Vaire | What it means |
|---|---|---|
| **Recall@10** | **87.9%** | The right memory shows up in the top 10 nearly 9 times out of 10 |
| **MRR** | **0.686** | The correct answer is usually the first or second result |
| **Single-hop MRR** | **0.718** | Factual questions, almost always nails it on the first try |
| **Temporal MRR** | **0.705** | "When did X happen?" queries, strong time awareness |

The thing is, everything runs locally. No cloud APIs, no billion-parameter models. The full pipeline: a 22M-parameter embedding model for retrieval, Doc2Query enrichment at write time (msmarco-t5-small), and two cross-encoder rerankers at query time — GTE-Reranker-ModernBERT for ranking and NLI DeBERTa v3 for open-domain entailment scoring. All on your machine, in a SQLite file. Most systems that hit numbers like these need GPT-4 in the loop. Vaire gets there with Hopfield energy scoring, spreading activation, and local small models.

## Hippocampal Replay: Context that survives compaction

Here's a problem nobody talks about. Claude Code has a 200K token context window. During long sessions, when that window fills up, it *compacts*: summarizes older messages, strips tool outputs, paraphrases your instructions. Important nuance evaporates. Decisions you anchored early in the conversation dissolve into vague summaries.

**Hippocampal Replay** fixes this. Named after the neuroscience phenomenon where your brain replays important experiences during sleep to consolidate them into long-term memory, it treats context compaction as the "sleep" and replays what matters when Claude "wakes up."

**How it works:**

Before compaction hits, a hook fires. Vaire drains your active context: what you were working on, which files were open, what decisions you'd made, what errors were unresolved. It stores all of this as a checkpoint.

After compaction, a second hook fires. Vaire reconstructs your context intelligently. Not by dumping everything back in, but by assembling the right pieces: your latest checkpoint, any facts you'd anchored as critical, the hottest project memories, and predictions about what you'll need next based on your usage patterns.

You can also be explicit about what matters:

```
Tool: anchor
  content: "We're using the event-sourcing pattern. All state changes go through the event bus."
  reason: "Architecture constraint"
```

Anchored memories get maximum protection. They always survive compaction, no matter what.

**One-time setup per project:**

```
Tool: install_hooks
  project_directory: "/path/to/your/project"
```

After that, everything is automatic. You don't think about it. You don't call anything manually. The hooks fire, the context drains, the context restores. Your long sessions just... work.

## Zero-gap memory (v1.3.0)

Previous versions still had gaps. You'd work on something for an hour, making incremental progress, and Vaire's write gate would block half of it because each small step looked "unsurprising" relative to the last. You'd make a critical architecture decision and it would slowly decay into a gist. You'd come back to a new session and Claude would have no idea what you were just doing.

v1.3.0 fixes all of this:

**Adaptive write gate.** The system now tracks your last 10 stored memories. When you're clearly working on the same task — same directory, same timeframe, similar content — it lowers the surprisal threshold so incremental progress gets through. The gate still blocks noise. It just stops blocking your work.

**Decision auto-protection.** When you say "decided to use Redis instead of Memcached" or "chose the event-sourcing pattern over CRUD," Vaire detects the decision pattern and automatically marks it as protected. Protected memories never compress and never decay fast. Your decisions outlive your sessions.

**Automatic action capture with outcome extraction.** A `PostToolUse` hook fires after every single tool call Claude makes. File edits, bash commands, searches — all captured into a lightweight action log. The consolidation daemon processes these into outcome narratives that describe *what was accomplished* (files edited, git operations, errors encountered) rather than storing raw tool-call logs. Outcomes pass through the write gate, so only surprising or novel work sessions are retained. You don't call `remember` for routine work. The system just knows.

**Session context injection.** A `SessionStart` hook fires on every new session and injects your project context — hot memories, anchored facts, recent actions, last checkpoint — directly into Claude's context window. Claude starts every session already knowing what you were doing.

**Micro-checkpointing.** Instead of checkpointing every 50 tool calls, the system now auto-checkpoints on significant events: errors encountered, decisions made, high-surprise information. Critical state transitions are captured the moment they happen.

**Session coherence.** Memories created within the last 4 hours get a heat bonus that fades linearly. You'll never hit the "I just told you this 10 minutes ago" problem again.

All hooks work in both stdio and HTTP transport modes — they access the SQLite database directly, no server communication needed.

## Bulk ingestion

Vaire can ingest markdown files, runbooks, specs, and decision logs as structured memories. Chunks are semantically split, deduplicated by content hash, embedded with Doc2Query enrichment, and stored as `is_protected` memories that survive consolidation.

```
# Preview how a file will be chunked
ingest_preview("/workspace/project/ARCHITECTURE.md")

# Ingest a single file with tags
ingest_file("/workspace/project/ARCHITECTURE.md", tags=["architecture"], project_dir="/workspace/project")

# Ingest a whole directory (background job)
ingest_directory("/workspace/project/docs", tags=["docs"], project_dir="/workspace/project")

# Check job progress
ingest_status("job-id-returned-above")
```

**Important:** always pass `project_dir` as the host-side project root. Without it, each chunk gets its file path as `directory_context`, breaking `get_project_context()` scoping.

Ingested chunks use `importance=0.8` by default, keeping them warm for ~59 days without access (vs ~2 days at the default `importance=0.5`). Re-running `ingest_directory` is safe — content is deduplicated by hash.

## LongMemEval

We ran the full [LongMemEval](https://arxiv.org/abs/2410.10813) benchmark (Wu et al., ICLR 2025), the current standard for evaluating long-term interactive memory in chat assistants. 500 human-curated questions across six categories, each embedded in ~40 sessions of conversation history (~115k tokens). The benchmark tests things LoCoMo doesn't: whether you can recall what the assistant said (not just the user), whether you track when information changes over time, whether you know what you don't know, and whether you can reason across sessions that happened weeks apart.

| | Vaire | What it means |
|---|---|---|
| **Recall@10** | **96.7%** | The right memory shows up in the top 10 results for nearly every question |
| **MRR** | **0.945** | The correct answer is almost always the first result returned |
| **Knowledge Update MRR** | **1.000** | When user information changes, Vaire always surfaces the latest version first |

The paper's best reported retrieval hit 78.4% Recall@10 on this dataset. Vaire reaches 96.7% without any LLM in the retrieval loop.

Per-category retrieval breakdown:

| Category | MRR | Recall@10 |
|---|---|---|
| Single-session (user) | 0.973 | 1.000 |
| Single-session (assistant) | 0.964 | 0.964 |
| Single-session (preference) | 0.810 | 0.967 |
| Multi-session reasoning | 0.966 | 0.958 |
| Temporal reasoning | 0.902 | 0.955 |
| Knowledge updates | 1.000 | 0.979 |

Knowledge updates scored a perfect MRR because heat-based decay naturally pushes newer information above older versions of the same fact. This wasn't designed for the benchmark. It's just how the thermodynamic model works.

Temporal reasoning is the hardest category and our lowest MRR at 0.902, which still means the right memory is typically in the top two results. Questions like "how many weeks ago did I attend X" require matching against session timestamps, and our embedding-based retrieval handles this through the temporal metadata we embed directly in memory content.

Full QA evaluation (using Claude as both reader and judge) reached 75.6% overall accuracy, with standout performance on knowledge updates (85.9%) and assistant recall (94.6%). Multi-session reasoning (54.9%) is the main gap, and that's a reader synthesis problem, not retrieval. We retrieve the right sessions 95.8% of the time for multi-session questions. The reader just has to do more work connecting information across them.

Benchmark configuration: LongMemEval_S variant, round-level memory decomposition, fresh database per question, 500 questions evaluated end-to-end.

## The science under the hood

Vaire doesn't store memories the way a database stores rows. It treats them more like a brain treats experiences.

**Memories have temperature.** Every memory starts hot. If you keep accessing it, it stays hot. If you don't, it cools. Below a threshold, it compresses: first to a gist, then to tags, then eventually it fades entirely. This isn't a bug. It's rate-distortion optimal forgetting, the same mathematical framework your brain uses to decide what's worth keeping. Important memories resist compression. Surprising ones get a heat boost. Boring, redundant ones quietly disappear.

**Storage has a gatekeeper.** Not everything deserves to be remembered. Vaire maintains a predictive model of what it already knows, and only stores information that violates its expectations. Tell it the same thing twice and the write gate blocks the second attempt. This is predictive coding: the same mechanism your neocortex uses to filter sensory input. Only prediction errors get through.

**Retrieval changes the memory.** When you recall a memory in a new context, it doesn't just passively hand it back. It compares the retrieval context against the storage context, and if there's enough mismatch, it *reconsolidates*: updates the memory to reflect what's true now. Severe mismatch archives the old version and creates a new one. This is real neuroscience. Nader et al. showed in 2000 that retrieved memories become labile and can be rewritten. Your codebase evolves, and so do Vaire's memories of it.

**Memories compete for space.** A pool of engram slots, each with an excitability score that spikes on use and decays over time. When a new memory arrives, it goes to the most excitable slot. Memories in the same slot get temporally linked, creating chains of related experiences even when their content has nothing in common. This models how real neurons allocate engrams through CREB-dependent excitability.

**Background consolidation runs in tiers.** An astrocyte daemon runs continuously on three schedules. Light cycles (60s) apply heat decay and process the action log into outcome narratives. Medium cycles (15min) extract entities and merge duplicates to keep the knowledge graph fresh. Full cycles (on idle) run causal discovery, self-improvement, and rate-distortion compression. Sleep cycles (6h+ gap) trigger dream replay where random memory pairs are compared and new connections emerge. Four domain-specialized processes handle different types of knowledge at different rates: code structure, architectural decisions, error patterns, and dependencies.

**A cognitive map organizes everything.** Successor representations build a 2D map of concept space where memories that get accessed in similar contexts cluster together, even if their content is completely different. Debugging memories cluster near other debugging memories. Architecture decisions cluster together. Navigate this map, and you find related knowledge that keyword search would never surface.

## All 27 tools

| Tool | Purpose |
|------|---------|
| `remember` | Store a memory through the predictive coding write gate |
| `recall` | Multi-signal retrieval with heat-weighted ranking |
| `forget` | Delete a memory |
| `validate_memory` | Check staleness against current file state |
| `get_project_context` | Get hot memories for a directory |
| `consolidate_now` | Force a consolidation cycle |
| `memory_stats` | System statistics across all subsystems |
| `rate_memory` | Usefulness feedback for metamemory tracking |
| `recall_hierarchical` | Query the fractal hierarchy at a specific abstraction level |
| `drill_down` | Navigate into a memory cluster |
| `create_trigger` | Set prospective triggers that fire on matching context |
| `get_project_story` | Autobiographical narrative of a project |
| `add_rule` | Neuro-symbolic constraints for filtering and re-ranking |
| `get_rules` | List active rules |
| `navigate_memory` | Traverse concept space via successor representations |
| `get_causal_chain` | Causal ancestors and descendants for an entity |
| `assess_coverage` | Evaluate knowledge coverage with gap identification |
| `detect_gaps` | Find isolated entities, stale regions, missing connections |
| `checkpoint` | Snapshot working state for compaction recovery |
| `restore` | Reconstruct context after compaction via Hippocampal Replay |
| `anchor` | Mark critical facts as compaction-resistant |
| `install_hooks` | Enable auto-capture, context injection, and compaction recovery hooks |
| `sync_instructions` | Update CLAUDE.md with latest Vaire capabilities |
| `ingest_file` | Chunk and embed a single markdown/text/rst file as protected memories |
| `ingest_directory` | Recursively ingest a directory tree; skips already-ingested content |
| `ingest_preview` | Dry-run chunking preview — see what would be stored before committing |
| `ingest_status` | Poll background ingest job status by job ID |

## Architecture

Everything runs locally. A single SQLite database with WAL mode, FTS5 full-text search, and `sqlite-vec` for approximate nearest neighbor vector search.

26 subsystems organized into five layers:

<details>
<summary><strong>Core Storage and Retrieval</strong></summary>

| Module | Role |
|--------|------|
| `storage.py` | SQLite WAL engine, 16 tables, FTS5 indexing, `sqlite-vec` ANN search |
| `embeddings.py` | Sentence-transformer encoding (`all-MiniLM-L6-v2`), batched operations |
| `retrieval.py` | Four-signal fusion: vector similarity, FTS5 BM25, knowledge graph PPR, spreading activation |
| `models.py` | Pydantic data models for the full type hierarchy |
| `config.py` | Environment-based configuration with `VAIRE_` prefix |

</details>

<details>
<summary><strong>Memory Dynamics</strong></summary>

| Module | Role |
|--------|------|
| `thermodynamics.py` | Heat, surprise, importance, emotional valence, temporal decay |
| `reconsolidation.py` | Labile retrieval with three outcomes per Nader et al. (2000) |
| `predictive_coding.py` | Write gate that filters redundancy via prediction error |
| `engram.py` | Competitive slot allocation with CREB-like excitability |
| `compression.py` | Rate-distortion optimal forgetting over three compression levels |
| `staleness.py` | File-change watchdog via SHA-256 hash comparison |

</details>

<details>
<summary><strong>Consolidation and Organization</strong></summary>

| Module | Role |
|--------|------|
| `consolidation.py` | Three-tier astrocyte daemon: light (60s), medium (15min), full (idle), sleep (6h) |
| `astrocyte_pool.py` | Domain-specialized worker processes for code, decisions, errors, deps |
| `sleep_compute.py` | Dream replay, Louvain community detection, temporal compression |
| `fractal.py` | Multi-scale memory tree with drill-down navigation |
| `cls_store.py` | Complementary Learning Systems: fast episodic + slow semantic stores |

</details>

<details>
<summary><strong>Knowledge Structure</strong></summary>

| Module | Role |
|--------|------|
| `knowledge_graph.py` | Typed entity-relationship graph with Personalized PageRank |
| `causal_discovery.py` | PC algorithm for causal DAGs from coding session data |
| `cognitive_map.py` | Successor Representation for navigation-based retrieval |
| `narrative.py` | Autobiographical project story synthesis |
| `curation.py` | Duplicate merging, contradiction detection, cross-reference linking, entity specificity filtering |

</details>

<details>
<summary><strong>Frontier Capabilities</strong></summary>

| Module | Role |
|--------|------|
| `hopfield.py` | Modern continuous Hopfield networks (Ramsauer et al., 2021) |
| `hdc_encoder.py` | Hyperdimensional Computing in 10,000-dimensional bipolar space |
| `metacognition.py` | Self-assessment of knowledge coverage and gap detection |
| `rules_engine.py` | Hard and soft neuro-symbolic constraints |
| `crdt_sync.py` | Multi-agent memory sharing via CRDTs |
| `prospective.py` | Future-oriented triggers on directory, keyword, entity, or time |
| `sensory_buffer.py` | Episodic capture buffer for raw session content |
| `restoration.py` | Hippocampal Replay engine for context compaction resilience |

</details>

## Docker deployment (recommended)

The Docker image bundles all three ML models so they never re-download on startup:

- **all-MiniLM-L6-v2** (~90MB) — sentence embeddings
- **gte-reranker-modernbert-base** (~570MB) — cross-encoder reranker
- **doc2query/msmarco-t5-small-v1** (~80MB) — synthetic query generation

```bash
git clone https://github.com/lonewolf896/vaire.git
cd vaire
UID=$(id -u) GID=$(id -g) docker compose up -d
```

The server exposes a Unix domain socket at `~/.vaire/vaire.sock`. Install the thin MCP client proxy:

```bash
pip install -e .   # or: pip install vaire
```

Then configure Claude Code:

```json
{
  "mcpServers": {
    "vaire": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "vaire", "client"]
    }
  }
}
```

The client forwards all MCP calls to the running container over the socket. The database persists at `~/.vaire/memory.db`. Litestream streams WAL changes to `~/.vaire/replicas/` for continuous local backup.

To check server health:

```bash
docker compose ps
python -m vaire health
```

### stdio / SSE (lightweight, no Docker)

For single-user setups without Docker:

```bash
vaire                       # stdio (default)
vaire --transport sse       # persistent background server
```

SSE config:

```json
{
  "mcpServers": {
    "vaire": {
      "type": "sse",
      "url": "http://127.0.0.1:8742/sse"
    }
  }
}
```

Default port `8742`. Override with `--port`. Database defaults to `~/.vaire/memory.db`, override with `--db-path`. Note: Doc2Query and GTE-Reranker are loaded on first use and cached for the process lifetime.

## Configuration

All settings use the `VAIRE_` environment variable prefix:

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `VAIRE_PORT` | `8742` | Server port |
| `VAIRE_DB_PATH` | `~/.vaire/memory.db` | Database location |
| `VAIRE_SOCKET_PATH` | `~/.vaire/vaire.sock` | Unix domain socket path (Docker mode) |
| `VAIRE_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model |
| `VAIRE_DECAY_FACTOR` | `0.95` | Heat decay per hour for importance ≤ 0.7 |
| `VAIRE_IMPORTANCE_DECAY_FACTOR` | `0.998` | Heat decay per hour for importance > 0.7 (slow path) |
| `VAIRE_COLD_THRESHOLD` | `0.05` | Heat below which memories become archival candidates |
| `VAIRE_WRITE_GATE_THRESHOLD` | `0.55` | Minimum surprisal to pass the write gate |
| `VAIRE_INGEST_DEFAULT_IMPORTANCE` | `0.8` | Importance assigned to ingested chunks (keeps them on the slow decay path) |
| `VAIRE_HOPFIELD_BETA` | `8.0` | Hopfield network sharpness |
| `VAIRE_SR_DISCOUNT` | `0.9` | Successor representation discount factor |
| `VAIRE_COGNITIVE_LOAD_LIMIT` | `8` | Active context chunk limit |
| `VAIRE_WRRF_FTS_WEIGHT` | `0.3` | Weight of FTS5 keyword signal in WRRF fusion |
| `VAIRE_CANDIDATE_POOL_MULTIPLIER` | `5` | Candidate pool size multiplier for retrieval |
| `VAIRE_ACTION_LOG_INTERVAL` | `60` | Seconds between light cycles (decay + action log) |
| `VAIRE_MEDIUM_CYCLE_INTERVAL` | `900` | Seconds between medium cycles (entity extraction + merge) |
| `VAIRE_SLEEP_CYCLE_MIN_GAP_HOURS` | `6.0` | Minimum hours between full sleep cycles |
| `VAIRE_PATH_REMAP` | `` | Container→host path rewrite (`/container:/host`) for correct `directory_context` in Docker |

Full list in `vaire/config.py`.

## Documentation

- **[Installation Guide](docs/INSTALL.md)** — Docker and standalone setup, hooks, configuration
- **[Usage Guide](docs/USAGE.md)** — Day-to-day usage, tools reference, tips

## Testing

```bash
python -m pytest vaire/tests/ -x -q
```

1215 tests across 47 test files covering every subsystem.

## References

<details>
<summary>The papers and books behind the implementation</summary>

Ramsauer et al. "Hopfield Networks is All You Need" (ICLR 2021, arXiv:2008.02217)

Nader, Schafe, LeDoux. "Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval" (Nature 406, 2000)

Osan, Tort, Bhatt, Amaral. "Three outcomes of reconsolidation" (PLoS ONE, 2011)

McClelland, McNaughton, O'Reilly. "Why there are complementary learning systems in the hippocampus and neocortex" (Psychological Review 102, 1995)

Sun et al. "Organizing memories for generalization in complementary learning systems" (Nature Neuroscience 26, 2023)

Stachenfeld, Botvinick, Gershman. "The hippocampus as a predictive map" (Nature Neuroscience 20, 2017)

Whittington et al. "The Tolman-Eichenbaum Machine" (Cell 183, 2020)

Spirtes, Glymour, Scheines. *Causation, Prediction, and Search* (MIT Press, 2000)

Kanerva. *Sparse Distributed Memory* (MIT Press, 1988)

Frady, Kleyko, Sommer. "Variable Binding for Sparse Distributed Representations" (IEEE TNNLS, 2022)

Toth et al. "Optimal forgetting via rate-distortion theory" (PLoS Computational Biology, 2020)

Josselyn, Frankland. "Memory allocation: mechanisms and function" (Annual Review Neuroscience 41, 2018)

Rashid et al. "Competition between engrams influences fear memory formation and recall" (Science 353, 2016)

Zhou et al. "MetaRAG: Metacognitive Retrieval-Augmented Generation" (ACM Web, 2024)

</details>

## License

MIT
