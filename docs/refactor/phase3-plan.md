# Phase 3: CRDT Vector Clock Persistence + mTLS CN Extraction

## Context

### Problem H9: CRDT vector clock lost on restart

`CRDTMemorySync.__init__()` initializes the vector clock as `{self._agent_id: 0}`.
This clock is never loaded from persistent storage. On every server restart, the
vector clock resets to 0. Post-restart local writes get lower clock values than
pre-restart writes, causing `compare_clocks()` to return incorrect "before" results.
This means stale remote data can silently overwrite newer local data.

Since multi-agent CRDT sync is actively used, this is a real data loss risk.

### Problem M12: mTLS CN header spoofing

The `MTLSMiddleware` reads client identity from the self-reported `X-Vaire-CN`
HTTP header. Any authenticated remote agent (valid cert holder) can set any value
for this header, impersonating another agent in provenance records. The middleware
should extract the actual CN from the TLS client certificate instead.

Since mTLS remote access is expanding, this is a real security gap.

## Files Modified

| File | Change |
|---|---|
| vaire/crdt_sync.py | Load/save vector clock from metadata table |
| vaire/server.py | Extract CN from TLS cert instead of trusting header |
| vaire/storage.py | Add `set_metadata_value()` method (if not yet added in Phase 1) |

## Dependencies

- Phase 1 adds `get_metadata_value()` to StorageEngine (used for loading clock)
- Phase 3 can run after Phase 1. Independent of Phase 2.
