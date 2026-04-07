# Phase 3, Step 3: Verification

## 3a. CRDT vector clock persistence

### Unit test pseudocode

```
def test_vector_clock_survives_restart():
    storage = StorageEngine(tmp_path / "test.db")
    settings = Settings(DB_PATH=str(tmp_path / "test.db"), CRDT_AGENT_ID="agent-a")
    
    # Create first instance, increment clock several times
    crdt1 = CRDTMemorySync(storage, settings)
    crdt1.increment_clock()  # {agent-a: 1}
    crdt1.increment_clock()  # {agent-a: 2}
    crdt1.increment_clock()  # {agent-a: 3}
    
    # Verify clock is at 3
    assert crdt1._vector_clock["agent-a"] == 3
    
    # Simulate restart: create new instance with same storage
    crdt2 = CRDTMemorySync(storage, settings)
    
    # Clock should load from metadata, not reset to 0
    assert crdt2._vector_clock["agent-a"] == 3
    
    # Further increments should continue from 3
    crdt2.increment_clock()
    assert crdt2._vector_clock["agent-a"] == 4
    
    storage.close()


def test_vector_clock_handles_missing_metadata():
    storage = StorageEngine(tmp_path / "fresh.db")
    settings = Settings(DB_PATH=str(tmp_path / "fresh.db"), CRDT_AGENT_ID="agent-a")
    
    # Fresh DB — no crdt_vector_clock in metadata
    crdt = CRDTMemorySync(storage, settings)
    
    # Should start at 0, not crash
    assert crdt._vector_clock == {"agent-a": 0}
    
    storage.close()


def test_vector_clock_preserves_multi_agent():
    storage = StorageEngine(tmp_path / "test.db")
    settings = Settings(DB_PATH=str(tmp_path / "test.db"), CRDT_AGENT_ID="agent-a")
    
    crdt = CRDTMemorySync(storage, settings)
    crdt.increment_clock()  # {agent-a: 1}
    
    # Simulate receiving a remote clock with agent-b
    crdt._vector_clock["agent-b"] = 5
    crdt._save_vector_clock()
    
    # Restart
    crdt2 = CRDTMemorySync(storage, settings)
    assert crdt2._vector_clock["agent-a"] == 1
    assert crdt2._vector_clock["agent-b"] == 5
    
    storage.close()


def test_sync_memories_persists_merged_clock():
    storage = StorageEngine(tmp_path / "test.db")
    settings = Settings(DB_PATH=str(tmp_path / "test.db"), CRDT_AGENT_ID="agent-a")
    
    crdt = CRDTMemorySync(storage, settings)
    
    # Insert a local memory
    mid = storage.insert_memory({"content": "local memory", "directory_context": "/test"})
    storage.update_memory_full(mid, vector_clock='{"agent-a": 1}')
    
    # Sync with a remote memory that has agent-b clock
    remote_memories = [{
        "content": "remote memory",
        "directory_context": "/test",
        "vector_clock": '{"agent-b": 7}',
        "provenance_agent": "agent-b",
    }]
    crdt.sync_memories(remote_memories)
    
    # Instance clock should now know about agent-b
    assert crdt._vector_clock.get("agent-b", 0) == 7
    
    # Restart — should preserve agent-b's clock
    crdt2 = CRDTMemorySync(storage, settings)
    assert crdt2._vector_clock.get("agent-b", 0) == 7
    
    storage.close()
```

### Manual verification

```
# Start QA container, do some remember() calls, check metadata:
sqlite3 ~/.vaire-qa/memory.db "SELECT * FROM metadata WHERE key = 'crdt_vector_clock'"
# Should show a JSON clock with the agent's counter > 0

# Restart the container:
docker restart vaire-qa

# Verify clock survived:
sqlite3 ~/.vaire-qa/memory.db "SELECT * FROM metadata WHERE key = 'crdt_vector_clock'"
# Same value as before restart
```

---

## 3b. mTLS CN extraction

### Unit test pseudocode

```
def test_extract_cert_cn_from_peercert():
    peercert = {
        "subject": (
            (("commonName", "test-remote-host"),),
        )
    }
    cn = _cn_from_peercert(peercert)
    assert cn == "test-remote-host"


def test_extract_cert_cn_missing_cn():
    peercert = {
        "subject": (
            (("organizationName", "Acme Inc"),),
        )
    }
    cn = _cn_from_peercert(peercert)
    assert cn is None


def test_middleware_prefers_cert_over_header():
    scope = {
        "type": "http",
        "extensions": {
            "tls": {
                "peercert": {
                    "subject": ((("commonName", "real-agent"),),)
                }
            }
        },
        "headers": [(b"x-vaire-cn", b"spoofed-agent")],
        "client": ("10.0.0.1", 8743),
    }
    cn = MTLSMiddleware._extract_cert_cn(scope)
    assert cn == "real-agent"  # cert wins


def test_middleware_falls_back_to_header():
    scope = {
        "type": "http",
        "extensions": {},
        "headers": [(b"x-vaire-cn", b"header-agent")],
        "client": ("10.0.0.1", 8743),
    }
    cn = MTLSMiddleware._extract_cert_cn(scope)
    assert cn == "header-agent"  # fallback


def test_middleware_returns_unknown_when_nothing():
    scope = {
        "type": "http",
        "extensions": {},
        "headers": [],
        "client": ("10.0.0.1", 8743),
    }
    cn = MTLSMiddleware._extract_cert_cn(scope)
    assert cn == "unknown"
```

### Live mTLS test

```
# From a remote host with client cert:
curl --cert client.crt --key client.key --cacert ca.crt \
     -H "X-Vaire-CN: spoofed-name" \
     https://<server>:8743/health

# Check server logs:
# If cert CN extraction worked: transport_ctx should have the real cert CN
# If fallback: should see WARNING log about using X-Vaire-CN header
```

---

## 3c. Existing tests

```
# Run CRDT tests
.venv/bin/python -m pytest vaire/tests/test_crdt.py -x -q
.venv/bin/python -m pytest vaire/tests/test_phase4_crdt.py -x -q

# Run mTLS tests
.venv/bin/python -m pytest vaire/tests/test_mtls.py -x -q
.venv/bin/python -m pytest vaire/tests/test_transport_context.py -x -q
.venv/bin/python -m pytest vaire/tests/test_transport.py -x -q

# Run server tests
.venv/bin/python -m pytest vaire/tests/test_server.py -x -q

# Full suite
.venv/bin/python -m pytest vaire/tests/ -x -q \
    --ignore=vaire/tests/test_stress.py \
    --ignore=vaire/tests/test_live_system.py
```

---

## 3d. Issues resolved

| Issue | Status |
|---|---|
| H9 (CRDT vector clock not persisted) | RESOLVED — clock saved to metadata on every increment, loaded on init |
| M12 (mTLS CN header spoofing) | MITIGATED — cert CN extraction when available, header fallback with warning |
