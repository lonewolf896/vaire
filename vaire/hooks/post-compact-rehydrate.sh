#!/usr/bin/env bash
# Vaire Hippocampal Replay — PostCompact Rehydration Hook
# Restores context from Vaire after Claude Code compacts the conversation.
# stdout is injected into Claude's context.

# Get current directory for context
CWD=$(pwd)

# Restore context directly via CLI (prints formatted markdown to stdout)
vaire restore "$CWD" 2>/dev/null

exit 0
