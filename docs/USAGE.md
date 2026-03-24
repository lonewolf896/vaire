# Usage Guide

This guide covers how to use Vaire day-to-day with Claude Code. For installation, see [INSTALL.md](INSTALL.md).

## Core concepts

**Memories** are the fundamental unit. Each memory has:
- **Content** — what was stored (text)
- **Heat** — how "active" it is (frequently accessed = hot, unused = cools and eventually compresses)
- **Importance** — how critical it is (higher = slower decay)
- **Directory context** — which project it belongs to
- **Tags** — for filtering and organization

Memories aren't static. They decay, compress, reconsolidate on retrieval, and compete for engram slots — modeled on how biological memory actually works.

## Everyday tools

### `remember` — Store something

Claude calls this automatically after significant work (via the PostToolUse hook). You can also ask explicitly:

> "Remember that we decided to use event sourcing for the order service."

What gets stored:
- Architecture decisions and rationale
- Bug root causes (not just the fix)
- Non-obvious patterns that worked
- Constraints discovered (API limits, env quirks)
- Dead ends worth avoiding

What doesn't need storing:
- Code that's in the repo (git is the source of truth)
- Temporary state or in-progress work

### `recall` — Retrieve relevant context

Claude calls this at session start and before tasks. You can also ask:

> "What do you remember about the auth middleware?"

Retrieval fuses eight signals: vector similarity, full-text search, knowledge graph PageRank, spreading activation, Hopfield energy, hyperdimensional computing, fractal hierarchy, and successor representations. Results are reranked by a cross-encoder.

### `get_project_context` — What's hot right now

Returns the most active memories for the current project directory. This is what gets injected at session start via the `UserPromptSubmit` hook.

### `forget` — Remove a memory

> "Forget that memory about the Redis config — we changed the approach."

Use when information is wrong or outdated and you don't want it surfacing in future recalls.

### `rate_memory` — Pin critical facts

> "That constraint about the database schema is critical — pin it."

Sets `importance=1.0` so the memory resists decay indefinitely. Use for hard constraints, API contracts, and deployment requirements that must never be forgotten.

---

## Hippocampal Replay

Long Claude Code sessions hit a context window limit. When that happens, Claude compresses older messages — and important nuance can evaporate.

Vaire handles this automatically if you've installed the hooks:

1. **PreCompact** — Before compression, Vaire snapshots your working state: current task, files being edited, key decisions, open questions, active errors
2. **PostCompact** — After compression, Vaire reconstructs context from the checkpoint, anchored facts, hot memories, and predictions about what you'll need next

You can also manually protect critical information:

### `anchor` — Protect a fact from compression

> "Anchor the fact that all state changes must go through the event bus."

Anchored memories get maximum heat and always survive context compression.

### `checkpoint` — Snapshot working state

> "Checkpoint where we are — we're halfway through the migration."

Captures: current task, files being edited, decisions made, open questions, next steps, active errors.

### `restore` — Rebuild context

> "Restore context for this project."

Reconstructs your working state from the latest checkpoint, anchored memories, hot context, and successor-representation predictions.

---

## Bulk ingestion

Ingest markdown files, runbooks, specs, and decision logs as structured memories.

### Preview first

Always preview before committing to verify chunking looks right:

```
Tool: ingest_preview
  file_path: "/workspace/project/ARCHITECTURE.md"
```

### Ingest a file

```
Tool: ingest_file
  file_path: "/workspace/project/ARCHITECTURE.md"
  tags: ["architecture"]
  project_dir: "/workspace/project"
```

Always pass `project_dir` as the host-side project root. Without it, `get_project_context()` scoping won't work correctly.

### Ingest a directory

```
Tool: ingest_directory
  directory_path: "/workspace/project/docs"
  tags: ["docs"]
  project_dir: "/workspace/project"
```

Recursively finds `.md`, `.txt`, and `.rst` files. Already-ingested content is skipped (content-hash dedup). For large directories, this runs as a background job — check progress with `ingest_status`.

### After ingestion

Force consolidation so entities and relationships are extracted:

```
Tool: consolidate_now
```

Then verify:

```
Tool: recall
  query: "architecture decisions"
```

Ingested chunks use `importance=0.8`, keeping them warm for ~59 days without access. Re-running ingestion is safe — duplicates are skipped.

---

## Advanced tools

### Knowledge exploration

| Tool | What it does |
|---|---|
| `recall_hierarchical` | Query the fractal hierarchy at a specific abstraction level |
| `drill_down` | Navigate into a memory cluster for more detail |
| `navigate_memory` | Traverse concept space via the cognitive map |
| `get_causal_chain` | Trace causal ancestors and descendants for an entity |
| `get_project_story` | Autobiographical narrative of a project |

### Quality and coverage

| Tool | What it does |
|---|---|
| `assess_coverage` | Evaluate how well Vaire knows a topic, with gap identification |
| `detect_gaps` | Find isolated entities, stale regions, missing connections |
| `validate_memory` | Check if a memory is stale against current file state |

### Rules and triggers

| Tool | What it does |
|---|---|
| `add_rule` | Create neuro-symbolic constraints for filtering and re-ranking |
| `get_rules` | List active rules |
| `create_trigger` | Set prospective triggers that fire when matching context appears |

### Maintenance

| Tool | What it does |
|---|---|
| `consolidate_now` | Force a full consolidation cycle (entity extraction, dream replay, dedup) |
| `memory_stats` | System statistics across all subsystems |

---

## How consolidation works

Vaire's consolidation daemon runs continuously on three tiers, designed for always-on operation with multiple agents checking in concurrently:

| Tier | Interval | What it does | Runs during activity? |
|---|---|---|---|
| **Light** | 60s | Heat decay + action log outcome extraction | Yes |
| **Medium** | 15min | Entity extraction + duplicate merging | Yes |
| **Full** | 5min idle | Causal discovery + memify + CLS + compression | No |
| **Sleep** | 6h min gap | Dream replay + community detection + temporal compression | No |

**Action log processing** transforms raw tool calls into outcome narratives. Instead of storing "Bash: kubectl get pods; Read: values.yaml; Edit: deployment.yaml", it extracts "edited: deployment.yaml; kubectl: apply, rollout." Only complete 30-minute time windows are processed — the current bucket stays open until it closes.

**Outcome extraction** passes through the write gate, so redundant work sessions are filtered. A specificity filter ensures derived patterns only use code identifiers (file paths, function names, error types) rather than generic words.

All intervals are configurable via environment variables (`VAIRE_ACTION_LOG_INTERVAL`, `VAIRE_MEDIUM_CYCLE_INTERVAL`, `VAIRE_SLEEP_CYCLE_MIN_GAP_HOURS`). The sleep cycle timestamp is persisted to the database so it survives container restarts.

---

## Tips

**Let the hooks do the work.** If you've installed the hooks, you rarely need to call `remember` manually. The PostToolUse hook captures every action, and the consolidation daemon transforms the raw action log into outcome-based memories automatically.

**Pin decisions early.** When you make an architecture decision, ask Claude to anchor or rate it with high importance. Decisions that aren't pinned will eventually decay if not accessed.

**Use tags for scoping.** Tags make recall faster and more precise. Tag ingested content by domain (`architecture`, `runbook`, `api-spec`) so you can filter later.

**Check coverage before deep work.** Before starting a complex task, use `assess_coverage` to see what Vaire knows and what gaps exist. This tells you whether you need to ingest more context first.

**`consolidate_now` is rarely needed.** The three-tier daemon handles consolidation continuously. Use it only after bulk ingestion or when you need entities extracted immediately.
