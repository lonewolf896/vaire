# TASK-044: Static Reference Documentation System + Task Engine

## Overview

Two new subsystems for Vaire:
1. **Reference System** — Immutable static content (NIST, directives) served from repo-baked files via `load_reference` MCP tool
2. **Task Engine** — Mutable task state with local-first cache + 30s GitLab sync via 7 new MCP tools

## Architecture Decisions

- **Option 2+3**: Manifest-driven (no stubs) + repo-embedded (Docker-baked)
- References are immutable at runtime; updates require git commit + deploy
- Tasks are mutable; local cache is working copy, GitLab syncs every 30s
- Directives are a governance channel — hash-verified on every load
- All security hardening from threat analysis baked into design

## File Changes

| File | Change |
|---|---|
| `vaire/config.py` | Add reference + GitLab + task settings |
| `vaire/reference.py` | NEW — ReferenceLoader class |
| `vaire/task_engine.py` | NEW — TaskEngine class + GitLab sync |
| `vaire/gitlab_client.py` | NEW — GitLabClient (token-safe API wrapper) |
| `vaire/server.py` | Register 8 new MCP tools, add to dispatch table |
| `vaire/reference/` | NEW — directory with manifest + content files |
| `Dockerfile` | Add COPY vaire/reference/ /app/reference/ |
| `docker-compose.yml` | Add /data/tasks.json mapping |
| `scripts/extract_references.py` | NEW — migration script |
| `vaire/tests/test_reference.py` | NEW — reference system tests |
| `vaire/tests/test_task_engine.py` | NEW — task engine tests |

## Implementation Phases

1. Config + GitLab client + path security (shared infra)
2. Manifest loader + `load_reference` MCP tool + reference files + Dockerfile
3. Task engine + task MCP tools + GitLab sync thread
4. Migration script (extract from DB, seed tasks.json, delete originals)
5. Role updates + Vale wake cycle + full test suite
