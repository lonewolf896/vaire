# Step 4: Verification Plan

## 4a. Static verification (grep checks)

```
# 1. No _conn access outside storage.py (excluding tests, hooks, __main__)
grep -rn '_storage._conn\|storage\._conn' vaire/ --include='*.py' \
    | grep -v test | grep -v __pycache__ | grep -v hooks/ | grep -v __main__.py
# Expected: ZERO results

# 2. No self._conn inside storage.py (should all be self.__conn)
grep -n 'self\._conn[^_]' vaire/storage.py | grep -v '__conn'
# Expected: ZERO results

# 3. All new methods exist
grep -n 'def count_memories\|def sum_reconsolidation\|def count_causal' vaire/storage.py
grep -n 'def get_memories_by_store_type\|def get_anchored\|def get_recent_memories' vaire/storage.py
grep -n 'def get_memories_for_pruning\|def get_memories_for_strengthening' vaire/storage.py
grep -n 'def get_entity_name\|def get_entity_heat\|def get_entity_by_id' vaire/storage.py
grep -n 'def get_relationships_by_weight\|def relationship_exists' vaire/storage.py
grep -n 'def find_memories_mentioning\|def get_hot_memories_all' vaire/storage.py
# Expected: All methods present
```

## 4b. Unit tests

```
# Run targeted tests for each modified file's test counterpart
.venv/bin/python -m pytest vaire/tests/test_storage.py -x -q
.venv/bin/python -m pytest vaire/tests/test_compression.py -x -q
.venv/bin/python -m pytest vaire/tests/test_consolidation.py -x -q
.venv/bin/python -m pytest vaire/tests/test_curation.py -x -q
.venv/bin/python -m pytest vaire/tests/test_retrieval.py -x -q
.venv/bin/python -m pytest vaire/tests/test_cls_store.py -x -q
.venv/bin/python -m pytest vaire/tests/test_knowledge_graph.py -x -q
.venv/bin/python -m pytest vaire/tests/test_rules_engine.py -x -q
.venv/bin/python -m pytest vaire/tests/test_server.py -x -q
.venv/bin/python -m pytest vaire/tests/test_predictive_coding.py -x -q
.venv/bin/python -m pytest vaire/tests/test_restoration.py -x -q
.venv/bin/python -m pytest vaire/tests/test_fractal.py -x -q
.venv/bin/python -m pytest vaire/tests/test_metacognition.py -x -q
.venv/bin/python -m pytest vaire/tests/test_sleep_compute.py -x -q
.venv/bin/python -m pytest vaire/tests/test_reference_store_type.py -x -q
```

## 4c. Full test suite

```
.venv/bin/python -m pytest vaire/tests/ -x -q \
    --ignore=vaire/tests/test_stress.py \
    --ignore=vaire/tests/test_live_system.py
# Expected: All pass (872+ tests)
```

## 4d. QA container live tests

```
# Build and start isolated QA container
mkdir -p ~/.vaire-qa
UID=$(id -u) GID=$(id -g) docker compose -f docker-compose.qa.yml up -d --build

# Wait for healthy
for i in $(seq 1 30); do
    docker inspect vaire-qa --format='{{.State.Health.Status}}' | grep -q healthy && break
    sleep 2
done

# Run live tests against QA socket
VAIRE_SOCKET_PATH=$HOME/.vaire-qa/vaire.sock \
    .venv/bin/python -m pytest vaire/tests/test_live_system.py -v
# Expected: 22+ passed, 0 failed

# Teardown
docker compose -f docker-compose.qa.yml down
```

## 4e. Issues addressed by this phase

After completion, these review issues are resolved:

| Issue | Status |
|---|---|
| C1 (write-lock bypass) | RESOLVED — all writes go through StorageEngine |
| C2 (direct _conn access) | RESOLVED — _conn is name-mangled, 60 sites migrated |
| M9 (memory_stats direct access) | RESOLVED — uses count_memories() |
| M15 (_last_consolidated_episode_id race) | MITIGATED — reads via domain methods use thread-local conn |
| M16 (action_log TOCTOU) | MITIGATED — reads via get_unprocessed_action_log() |
| L26 (_anchor LIKE wildcard) | RESOLVED — get_anchored_memories uses quoted pattern |
| 30+ architecture violations | RESOLVED — all external _conn access eliminated |
