"""Entry point for python -m vaire."""

import argparse
import configparser
import sys
from pathlib import Path

from vaire import __version__
from vaire.server import main

VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")

STARTUP_BANNER = f"""\
=== Vaire v{__version__} ===
Biologically-inspired persistent memory engine for Claude Code

Active modules:
  * StorageEngine         (SQLite WAL + FTS5 + sqlite-vec)
  * EmbeddingEngine       (sentence-transformers)
  * SensoryBuffer         (episode capture)
  * MemoryThermodynamics  (surprise, importance, valence, decay)
  * KnowledgeGraph        (typed relationships, causal detection)
  * HippoRetriever        (PPR + vector + FTS5 + spreading activation + fractal)
  * MemoryCurator         (merge/link/create, contradiction, memify)
  * AstrocyteEngine       (background consolidation daemon)
  * AstrocytePool         (domain-aware processes: code/decisions/errors/deps)
  * SleepComputeEngine    (dream replay, compression, community detection)
  * FractalMemoryTree     (hierarchical multi-scale retrieval)
  * ProspectiveMemory     (future-oriented triggers)
  * NarrativeEngine       (autobiographical project stories)
  * StalenessDetector     (file-change watchdog)

MCP Tools: remember, recall, forget, validate_memory, get_project_context,
           consolidate_now, memory_stats, rate_memory, recall_hierarchical,
           drill_down, create_trigger, get_project_story

MCP Resources: memory://stats, memory://hot, memory://stale,
               memory://processes, memory://narrative/{{directory}}
"""


def _init_replay_lightweight(db_path=None):
    """Initialize only the engines needed for drain/restore (no daemons, no server)."""
    import logging
    # Suppress all library logging — hooks must only output data to stdout
    logging.disable(logging.CRITICAL)

    from vaire.config import Settings
    from vaire.storage import StorageEngine
    from vaire.embeddings import EmbeddingEngine
    from vaire.cognitive_map import CognitiveMap
    from vaire.metacognition import MetaCognition
    from vaire.knowledge_graph import KnowledgeGraph
    from vaire.retrieval import HippoRetriever
    from vaire.restoration import HippocampalReplay

    settings = Settings()
    storage = StorageEngine(db_path or settings.DB_PATH)
    embeddings = EmbeddingEngine(settings.EMBEDDING_MODEL)
    kg = KnowledgeGraph(storage, settings)
    cognitive_map = CognitiveMap(storage, settings)
    retriever = HippoRetriever(storage, embeddings, kg, settings)
    retriever.set_cognitive_map(cognitive_map)
    metacognition = MetaCognition(storage, embeddings, kg, settings)

    replay = HippocampalReplay(
        storage=storage,
        embeddings=embeddings,
        retriever=retriever,
        cognitive_map=cognitive_map,
        metacognition=metacognition,
        settings=settings,
    )
    return storage, replay


def cmd_drain(args):
    """Pre-compaction drain: save context to DB before Claude compacts."""
    import json
    directory = args.directory
    storage, replay = _init_replay_lightweight(args.db_path)
    try:
        result = replay.pre_compact_drain(directory)
        # Output JSON to stdout so hook can parse it if needed
        print(json.dumps(result))
    finally:
        storage.close()


def cmd_restore(args):
    """Post-compaction restore: reconstruct context and print markdown to stdout."""
    import json
    directory = args.directory
    storage, replay = _init_replay_lightweight(args.db_path)
    try:
        result = replay.restore(directory)
        formatted = result.get("formatted", "")
        if formatted:
            print(formatted)
    finally:
        storage.close()


def cmd_capture(args):
    """Lightweight action capture — writes directly to SQLite without ML models.

    Used by PostToolCall hooks and manual capture. Only imports sqlite3.

    When --from-stdin is set, reads the Claude Code hook JSON payload from stdin
    instead of relying on env-var arguments (which Claude Code does not set).
    Stdin JSON format: {"tool_name": "...", "session_id": "...", "tool_input": {...}}
    """
    import json
    import sqlite3
    from datetime import datetime, timezone
    from vaire.config import Settings

    settings = Settings()
    db_path = Path(args.db_path or settings.DB_PATH).expanduser()
    if not db_path.exists():
        sys.exit(0)  # DB not yet created — silently skip

    tool_name = getattr(args, "tool_name", None) or ""
    summary = getattr(args, "summary", None) or ""
    session_id = getattr(args, "session", None) or ""
    directory = getattr(args, "directory", None) or ""

    if getattr(args, "from_stdin", False):
        try:
            data = json.loads(sys.stdin.read())
            tool_name = data.get("tool_name", tool_name)
            session_id = data.get("session_id", session_id)
            if not summary:
                tool_input = data.get("tool_input", {})
                if tool_input:
                    summary = json.dumps(tool_input)[:200]
        except Exception:
            pass

    if not tool_name:
        sys.exit(0)  # nothing meaningful to capture

    conn = sqlite3.connect(str(db_path), timeout=1)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS action_log("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "tool_name TEXT NOT NULL,"
            "tool_input_summary TEXT DEFAULT '',"
            "directory TEXT DEFAULT '',"
            "session_id TEXT DEFAULT '',"
            "timestamp TEXT NOT NULL,"
            "processed INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO action_log (tool_name, tool_input_summary, directory, session_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                tool_name,
                summary,
                directory,
                session_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_approved_groomers(settings) -> frozenset[str]:
    """Load approved groomer agent_ids from ~/.vaire/vaire.ini.

    Falls back to empty set (prefix-based matching) if the file
    doesn't exist or has no [groomer] approved entry.
    """
    ini_path = Path(settings.DB_PATH).expanduser().parent / "vaire.ini"
    if not ini_path.exists():
        return frozenset()
    cfg = configparser.ConfigParser()
    cfg.read(ini_path)
    raw = cfg.get("groomer", "approved", fallback="")
    return frozenset(g.strip() for g in raw.split(",") if g.strip())


def cmd_server(args):
    """Start the Vaire shared memory Unix domain socket server."""
    import asyncio
    import signal as _signal
    from vaire.config import get_settings
    from vaire.server import (
        async_shutdown,
        build_dispatch_table,
        build_groomer_dispatch,
        build_ingest_dispatch,
        build_task_dispatch,
        init_engines,
        run_https,
        shutdown,
    )
    import vaire.server as _server_mod
    from vaire.socket_server import VaireSocketServer
    from vaire.token_manager import TokenManager

    settings = get_settings()

    _shutdown_event = asyncio.Event()

    async def _run():
        # Engine initialisation happens inside the event loop so that
        # asyncio.create_task() in WriteQueue.start() has a running loop.
        init_engines(db_path=args.db_path, start_daemons=True)

        loop = asyncio.get_running_loop()

        # Register SIGTERM/SIGINT after engines are fully initialised.
        # Set an event flag so the event loop can shut down gracefully
        # (drain write queue, stop server) instead of calling sys.exit().
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown_event.set)

        dispatch = build_dispatch_table()
        dispatch.update(build_ingest_dispatch(_server_mod._pipeline))
        dispatch.update(build_task_dispatch())
        groomer = build_groomer_dispatch()

        # Load approved groomers from ini file (not in code — runtime config)
        approved = _load_approved_groomers(settings)

        # Initialise token manager for socket authentication
        token_manager = TokenManager(
            str(settings.socket_auth_tokens_dir_resolved)
        )

        server = VaireSocketServer(
            socket_path=str(settings.socket_path_resolved),
            pid_file=str(settings.pid_file_resolved),
            dispatch_table=dispatch,
            groomer_methods=groomer,
            max_clients=settings.MAX_CLIENTS,
            approved_groomers=approved,
            rate_limit_per_minute=settings.RATE_LIMIT_PER_MINUTE,
            rate_limit_burst=settings.RATE_LIMIT_BURST,
            token_manager=token_manager,
            auth_enabled=settings.SOCKET_AUTH_ENABLED,
        )

        await server.start()
        try:
            tasks: set[asyncio.Task] = set()
            serve_task = asyncio.create_task(server.serve_forever())
            tasks.add(serve_task)

            # Start mTLS HTTPS server alongside the socket server if configured
            if settings.tls_enabled:
                https_task = asyncio.create_task(run_https(settings))
                tasks.add(https_task)

            shutdown_task = asyncio.create_task(_shutdown_event.wait())
            tasks.add(shutdown_task)

            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        finally:
            await server.stop()
            await async_shutdown()  # drain write queue before tearing down
            shutdown()

    asyncio.run(_run())


def cmd_client(args):
    """Start the Vaire thin MCP client (stdio transport)."""
    from vaire.socket_client import mcp
    mcp.run(transport="stdio")


def cmd_groomer(args):
    """Start the Vaire groomer MCP client (stdio transport)."""
    from vaire.socket_client import groomer_mcp
    groomer_mcp.run(transport="stdio")


def cmd_health(args):
    """Health check — ping the socket server and exit 0 (healthy) or 1 (unhealthy).

    Designed for use as a Docker HEALTHCHECK. Imports no ML models.
    Verifies the socket accepts a fresh connection AND the response has
    expected structure (not just that the process is alive).
    """
    import asyncio
    import logging

    logging.disable(logging.CRITICAL)

    from vaire.config import Settings
    from vaire.socket_client import VaireClient

    settings = Settings()
    socket_path = str(settings.socket_path_resolved)

    async def _check() -> bool:
        client = VaireClient(socket_path, call_timeout=4.0)
        try:
            result = await client.call("memory_stats", {})
            await client.disconnect()
        except Exception:
            return False

        # Validate response structure — not just "no exception"
        if not isinstance(result, dict):
            return False
        if "total_memories" not in result:
            return False

        return True

    ok = asyncio.run(_check())
    sys.exit(0 if ok else 1)


def cmd_token(args):
    """Manage socket authentication tokens."""
    from vaire.config import get_settings
    from vaire.token_manager import TokenManager

    settings = get_settings()
    tokens_dir = args.tokens_dir or str(settings.socket_auth_tokens_dir_resolved)
    manager = TokenManager(tokens_dir)

    action = args.token_action
    if action == "create":
        agent_name = args.agent_name
        try:
            secret = manager.create(agent_name)
            print(f"Token created for agent: {agent_name}")
            print(f"Secret: {secret}")
            print(f"Token file: {manager.tokens_dir / f'{agent_name}.token'}")
            print()
            print("The client will auto-detect this token if the tokens")
            print("directory is the default (~/.vaire/tokens/).")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    elif action == "revoke":
        agent_name = args.agent_name
        if manager.revoke(agent_name):
            print(f"Token revoked for agent: {agent_name}")
        else:
            print(f"No token found for agent: {agent_name}", file=sys.stderr)
            sys.exit(1)

    elif action == "list":
        tokens = manager.list_tokens()
        if not tokens:
            print("No tokens found.")
            return
        print(f"{'Agent Name':<30} {'Created':<25} {'Path'}")
        print("-" * 90)
        from datetime import datetime, timezone
        for t in tokens:
            created = datetime.fromtimestamp(t.created_at, tz=timezone.utc)
            print(f"{t.agent_name:<30} {created.isoformat():<25} {t.token_path}")

    else:
        print(f"Unknown token action: {action}", file=sys.stderr)
        sys.exit(1)


def cmd_context(args):
    """Lightweight context query — reads hot memories without loading ML models.

    Used by SessionStart hooks to inject context on every session.
    """
    import json
    import sqlite3
    from vaire.config import Settings

    settings = Settings()
    db_path = Path(args.db_path or settings.DB_PATH).expanduser()
    if not db_path.exists():
        return

    directory = args.directory
    conn = sqlite3.connect(str(db_path), timeout=2)
    try:
        conn.row_factory = sqlite3.Row

        hot = conn.execute(
            "SELECT content, heat FROM memories "
            "WHERE directory_context = ? AND heat > 0.5 "
            "ORDER BY heat DESC LIMIT 6",
            (directory,),
        ).fetchall()

        anchored = conn.execute(
            "SELECT content FROM memories "
            "WHERE is_protected = 1 AND heat > 0 AND tags LIKE '%_anchor%' "
            "ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()

    if not hot and not anchored:
        return

    print("# Vaire — Session Context\n")
    if anchored:
        print("## Critical Facts")
        for row in anchored:
            print(f"- {row['content'][:200]}")
        print()
    if hot:
        print("## Project Context")
        for row in hot:
            content = row["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"- [{row['heat']:.1f}] {content}")
        print()
    print(f"*Context for: {directory}*")


def cli():
    parser = argparse.ArgumentParser(description="Vaire memory engine MCP server")
    subparsers = parser.add_subparsers(dest="command")

    # Default server mode (no subcommand)
    parser.add_argument("--port", type=int, default=None, help="Server port (default: 8742)")
    parser.add_argument("--db-path", type=str, default=None, help="SQLite database path")
    parser.add_argument(
        "--transport",
        type=str,
        default="stdio",
        choices=VALID_TRANSPORTS,
        help="MCP transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress startup banner",
    )

    # drain subcommand
    drain_parser = subparsers.add_parser("drain", help="Pre-compaction context drain")
    drain_parser.add_argument("directory", help="Project directory")
    drain_parser.add_argument("--db-path", type=str, default=None, help="SQLite database path")

    # restore subcommand
    restore_parser = subparsers.add_parser("restore", help="Post-compaction context restore")
    restore_parser.add_argument("directory", help="Project directory")
    restore_parser.add_argument("--db-path", type=str, default=None, help="SQLite database path")

    # server subcommand — shared Unix domain socket server
    server_parser = subparsers.add_parser("server", help="Start the shared socket server")
    server_parser.add_argument("--db-path", type=str, default=None, help="SQLite database path")

    # client subcommand — thin MCP proxy client
    subparsers.add_parser("client", help="Start the thin MCP client (stdio)")

    # groomer subcommand — groomer MCP client with elevated permissions
    subparsers.add_parser("groomer", help="Start the groomer MCP client (stdio)")

    # capture subcommand (used by PostToolCall hooks)
    capture_parser = subparsers.add_parser("capture", help="Lightweight action capture")
    capture_parser.add_argument("--tool", dest="tool_name", default="", help="Tool name")
    capture_parser.add_argument("--summary", type=str, default="", help="Tool input summary")
    capture_parser.add_argument("--directory", type=str, default="", help="Working directory")
    capture_parser.add_argument("--session", type=str, default="", help="Session ID")
    capture_parser.add_argument("--from-stdin", action="store_true", default=False,
                                help="Read tool_name/session_id/tool_input from Claude Code JSON on stdin")
    capture_parser.add_argument("--db-path", type=str, default=None, help="SQLite database path")

    # token subcommand — manage socket auth tokens
    token_parser = subparsers.add_parser("token", help="Manage socket auth tokens")
    token_parser.add_argument("--tokens-dir", type=str, default=None,
                              help="Tokens directory (default: VAIRE_SOCKET_AUTH_TOKENS_DIR)")
    token_sub = token_parser.add_subparsers(dest="token_action")
    token_create = token_sub.add_parser("create", help="Create a new agent token")
    token_create.add_argument("agent_name", help="Agent name (becomes the file stem)")
    token_revoke = token_sub.add_parser("revoke", help="Revoke an agent token")
    token_revoke.add_argument("agent_name", help="Agent name to revoke")
    token_sub.add_parser("list", help="List all agent tokens")

    # health subcommand (used by Docker HEALTHCHECK)
    subparsers.add_parser("health", help="Ping the socket server; exits 0 if healthy")

    # context subcommand (used by SessionStart hooks)
    context_parser = subparsers.add_parser("context", help="Lightweight context query")
    context_parser.add_argument("directory", help="Project directory")
    context_parser.add_argument("--db-path", type=str, default=None, help="SQLite database path")

    args = parser.parse_args()

    if args.command == "server":
        cmd_server(args)
    elif args.command == "client":
        cmd_client(args)
    elif args.command == "groomer":
        cmd_groomer(args)
    elif args.command == "drain":
        cmd_drain(args)
    elif args.command == "restore":
        cmd_restore(args)
    elif args.command == "capture":
        cmd_capture(args)
    elif args.command == "context":
        cmd_context(args)
    elif args.command == "health":
        cmd_health(args)
    elif args.command == "token":
        if not getattr(args, "token_action", None):
            print("Usage: python -m vaire token {create|revoke|list}", file=sys.stderr)
            sys.exit(1)
        cmd_token(args)
    else:
        # Default: run MCP server
        if not args.quiet and args.transport != "stdio":
            print(STARTUP_BANNER, file=sys.stderr)
            print(f"Transport: {args.transport}", file=sys.stderr)
            if args.port:
                print(f"Port: {args.port}", file=sys.stderr)
            if args.db_path:
                print(f"Database: {args.db_path}", file=sys.stderr)
            print(file=sys.stderr)

        main(port=args.port, db_path=args.db_path, transport=args.transport)


if __name__ == "__main__":
    cli()
