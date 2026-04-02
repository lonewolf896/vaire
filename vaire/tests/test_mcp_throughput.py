"""MCP throughput tests — find breaking points under load.

Tests progressively increasing concurrency, batch sizes, and content
sizes to identify where the system degrades or fails.

Run with:
    .venv/bin/python -m pytest vaire/tests/test_mcp_throughput.py -v -s

The -s flag shows real-time throughput numbers as tests run.
"""
from __future__ import annotations

import asyncio
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from vaire.socket_client import VaireClient, VaireError

QA_SOCKET = Path.home() / ".vaire-qa" / "vaire.sock"
QA_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.qa.yml"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = "/tmp/vaire-throughput-test"
TEST_TAG = "throughput-test"

_skip = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="Docker not available",
)


@pytest.fixture(scope="module")
def qa_container():
    if shutil.which("docker") is None:
        pytest.skip("Docker not available")

    # Clean stale PID file and stop leftover containers
    pid_file = Path.home() / ".vaire-qa" / "vaire.pid"
    pid_file.unlink(missing_ok=True)
    subprocess.run(
        ["docker", "compose", "-f", str(QA_COMPOSE), "down"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "GID": str(os.getgid())},
        capture_output=True, timeout=60,
    )

    subprocess.run(
        ["docker", "compose", "-f", str(QA_COMPOSE), "up", "-d", "--build"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "GID": str(os.getgid())},
        capture_output=True, timeout=300,
    )
    for _ in range(30):
        r = subprocess.run(
            ["docker", "inspect", "vaire-qa", "--format", "{{.State.Health.Status}}"],
            capture_output=True, text=True,
        )
        if r.stdout.strip() == "healthy":
            break
        time.sleep(2)
    else:
        logs = subprocess.run(
            ["docker", "logs", "vaire-qa", "--tail", "30"],
            capture_output=True, text=True,
        )
        pytest.fail(f"QA container not healthy.\n{logs.stderr}\n{logs.stdout}")

    yield str(QA_SOCKET)

    subprocess.run(
        ["docker", "compose", "-f", str(QA_COMPOSE), "down"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "GID": str(os.getgid())},
        capture_output=True, timeout=60,
    )


@pytest.fixture
async def client(qa_container):
    c = VaireClient(qa_container, call_timeout=120.0)
    yield c
    await c.disconnect()


async def _force_store(client, content, context=TEST_DIR):
    """Store with force=True, return memory_id."""
    result = await client.call("remember", {
        "force": True,
        "content": content,
        "context": context,
        "tags": [TEST_TAG],
    })
    return result.get("id") or result.get("memory_id")


async def _cleanup_all(client):
    """Remove all throughput test memories."""
    try:
        resp = await client.call("recall", {
            "query": "throughput test",
            "max_results": 500,
            "context": TEST_DIR,
        })
        memories = resp.get("result", resp) if isinstance(resp, dict) else resp
        for m in memories:
            if m.get("_budget_meta"):
                continue
            mid = m.get("id")
            if mid:
                try:
                    await client.call("forget", {"memory_id": mid})
                except Exception:
                    pass
    except Exception:
        pass


def _report(label, count, elapsed, errors=0):
    """Print throughput stats."""
    rate = count / elapsed if elapsed > 0 else 0
    err_str = f", {errors} errors" if errors else ""
    print(f"  {label}: {count} ops in {elapsed:.1f}s = {rate:.1f} ops/s{err_str}")


# ═══════════════════════════════════════════════════════════════════════
# Sequential write throughput
# ═══════════════════════════════════════════════════════════════════════

@_skip
class TestSequentialWrites:
    """Measure sequential write throughput at different batch sizes."""

    @pytest.mark.anyio
    async def test_10_sequential_writes(self, client):
        ids = []
        start = time.monotonic()
        for i in range(10):
            mid = await _force_store(
                client,
                f"Sequential throughput test {i} token {random.randint(100000, 999999)}: "
                "measuring baseline write performance for the memory engine.",
            )
            if mid:
                ids.append(mid)
        elapsed = time.monotonic() - start
        _report("10 sequential writes", len(ids), elapsed)
        assert len(ids) == 10
        for mid in ids:
            await client.call("forget", {"memory_id": mid})

    @pytest.mark.anyio
    async def test_50_sequential_writes(self, client):
        ids = []
        start = time.monotonic()
        for i in range(50):
            mid = await _force_store(
                client,
                f"Sequential batch-50 test {i} token {random.randint(100000, 999999)}: "
                "testing sustained write throughput over a larger batch.",
            )
            if mid:
                ids.append(mid)
        elapsed = time.monotonic() - start
        _report("50 sequential writes", len(ids), elapsed)
        assert len(ids) == 50
        for mid in ids:
            await client.call("forget", {"memory_id": mid})

    @pytest.mark.anyio
    async def test_100_sequential_writes(self, client):
        ids = []
        start = time.monotonic()
        for i in range(100):
            mid = await _force_store(
                client,
                f"Sequential batch-100 test {i} token {random.randint(100000, 999999)}: "
                "testing write throughput at scale for capacity planning.",
            )
            if mid:
                ids.append(mid)
        elapsed = time.monotonic() - start
        _report("100 sequential writes", len(ids), elapsed)
        assert len(ids) == 100
        for mid in ids:
            await client.call("forget", {"memory_id": mid})


# ═══════════════════════════════════════════════════════════════════════
# Concurrent write throughput
# ═══════════════════════════════════════════════════════════════════════

@_skip
class TestConcurrentWrites:
    """Measure concurrent write throughput — find where SQLite contention breaks."""

    async def _concurrent_writes(self, client, concurrency, total):
        """Run total writes with given concurrency level."""
        sem = asyncio.Semaphore(concurrency)
        ids = []
        errors = []

        async def write_one(i):
            async with sem:
                try:
                    mid = await _force_store(
                        client,
                        f"Concurrent-{concurrency} test {i} token {random.randint(100000, 999999)}: "
                        "testing concurrent write behavior under contention.",
                    )
                    if mid:
                        ids.append(mid)
                    else:
                        errors.append(f"write {i}: no id returned")
                except Exception as e:
                    errors.append(f"write {i}: {e}")

        start = time.monotonic()
        await asyncio.gather(*[write_one(i) for i in range(total)])
        elapsed = time.monotonic() - start
        return ids, errors, elapsed

    @pytest.mark.anyio
    async def test_concurrency_2(self, client):
        ids, errors, elapsed = await self._concurrent_writes(client, 2, 20)
        _report("concurrency=2, 20 writes", len(ids), elapsed, len(errors))
        assert len(errors) == 0, f"Errors: {errors}"
        for mid in ids:
            await client.call("forget", {"memory_id": mid})

    @pytest.mark.anyio
    async def test_concurrency_5(self, client):
        ids, errors, elapsed = await self._concurrent_writes(client, 5, 25)
        _report("concurrency=5, 25 writes", len(ids), elapsed, len(errors))
        assert len(errors) == 0, f"Errors: {errors}"
        for mid in ids:
            await client.call("forget", {"memory_id": mid})

    @pytest.mark.anyio
    async def test_concurrency_10(self, client):
        ids, errors, elapsed = await self._concurrent_writes(client, 10, 30)
        _report("concurrency=10, 30 writes", len(ids), elapsed, len(errors))
        assert len(errors) == 0, f"Errors: {errors}"
        for mid in ids:
            await client.call("forget", {"memory_id": mid})

    @pytest.mark.anyio
    async def test_concurrency_20(self, client):
        ids, errors, elapsed = await self._concurrent_writes(client, 20, 40)
        _report("concurrency=20, 40 writes", len(ids), elapsed, len(errors))
        # Allow some errors at high concurrency — we want to find the threshold
        if errors:
            print(f"  Errors at concurrency=20: {errors[:5]}...")
        for mid in ids:
            try:
                await client.call("forget", {"memory_id": mid})
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
# Concurrent read throughput
# ═══════════════════════════════════════════════════════════════════════

@_skip
class TestConcurrentReads:
    """Measure concurrent read throughput."""

    @pytest.mark.anyio
    async def test_10_concurrent_recalls(self, client):
        queries = [f"test query {i}" for i in range(10)]
        start = time.monotonic()
        results = await asyncio.gather(*[
            client.call("recall", {"query": q, "max_results": 5})
            for q in queries
        ])
        elapsed = time.monotonic() - start
        _report("10 concurrent recalls", len(results), elapsed)
        assert len(results) == 10

    @pytest.mark.anyio
    async def test_25_concurrent_recalls(self, client):
        queries = [f"test query {i}" for i in range(25)]
        start = time.monotonic()
        results = await asyncio.gather(*[
            client.call("recall", {"query": q, "max_results": 5})
            for q in queries
        ])
        elapsed = time.monotonic() - start
        _report("25 concurrent recalls", len(results), elapsed)
        assert len(results) == 25

    @pytest.mark.anyio
    async def test_50_concurrent_recalls(self, client):
        queries = [f"test query {i}" for i in range(50)]
        start = time.monotonic()
        results = await asyncio.gather(*[
            client.call("recall", {"query": q, "max_results": 5})
            for q in queries
        ])
        elapsed = time.monotonic() - start
        _report("50 concurrent recalls", len(results), elapsed)
        assert len(results) == 50

    @pytest.mark.anyio
    async def test_50_concurrent_memory_stats(self, client):
        start = time.monotonic()
        results = await asyncio.gather(*[
            client.call("memory_stats", {})
            for _ in range(50)
        ])
        elapsed = time.monotonic() - start
        _report("50 concurrent memory_stats", len(results), elapsed)
        assert len(results) == 50


# ═══════════════════════════════════════════════════════════════════════
# Mixed read/write throughput
# ═══════════════════════════════════════════════════════════════════════

@_skip
class TestMixedWorkload:
    """Simulate realistic workload: reads and writes interleaved."""

    @pytest.mark.anyio
    async def test_mixed_20_writes_80_reads(self, client):
        """80/20 read/write split — typical agent usage pattern."""
        sem = asyncio.Semaphore(5)
        write_ids = []
        read_results = []
        errors = []

        async def do_write(i):
            async with sem:
                try:
                    mid = await _force_store(
                        client,
                        f"Mixed workload write {i} token {random.randint(100000, 999999)}",
                    )
                    if mid:
                        write_ids.append(mid)
                except Exception as e:
                    errors.append(f"write {i}: {e}")

        async def do_read(i):
            async with sem:
                try:
                    r = await client.call("recall", {
                        "query": f"mixed workload query {i}",
                        "max_results": 3,
                    })
                    read_results.append(r)
                except Exception as e:
                    errors.append(f"read {i}: {e}")

        ops = []
        for i in range(100):
            if i % 5 == 0:
                ops.append(do_write(i))
            else:
                ops.append(do_read(i))

        start = time.monotonic()
        await asyncio.gather(*ops)
        elapsed = time.monotonic() - start

        _report(
            f"mixed 20w/{len(read_results)}r",
            len(write_ids) + len(read_results),
            elapsed,
            len(errors),
        )
        assert len(errors) == 0, f"Errors: {errors[:5]}"
        for mid in write_ids:
            await client.call("forget", {"memory_id": mid})


# ═══════════════════════════════════════════════════════════════════════
# Content size scaling
# ═══════════════════════════════════════════════════════════════════════

@_skip
class TestContentSizeScaling:
    """Measure how content size affects write/recall latency."""

    @pytest.mark.anyio
    async def test_size_scaling(self, client):
        """Write + recall at increasing sizes, report latency per size."""
        sizes = [100, 500, 1000, 2000, 5000, 10000, 20000, 40000]
        print()  # newline for readability

        for size in sizes:
            content = f"Size-{size} test {random.randint(100000, 999999)}: " + "x" * (size - 30)

            # Write
            w_start = time.monotonic()
            mid = await _force_store(client, content)
            w_elapsed = (time.monotonic() - w_start) * 1000

            # Recall
            r_start = time.monotonic()
            await client.call("recall", {
                "query": content[:50],
                "max_results": 3,
            })
            r_elapsed = (time.monotonic() - r_start) * 1000

            print(f"  {size:>6} chars: write={w_elapsed:>7.0f}ms  recall={r_elapsed:>7.0f}ms")

            if mid:
                await client.call("forget", {"memory_id": mid})

            assert w_elapsed < 30000, f"Write at {size} chars took {w_elapsed}ms (>30s)"
            assert r_elapsed < 30000, f"Recall at {size} chars took {r_elapsed}ms (>30s)"


# ═══════════════════════════════════════════════════════════════════════
# Sustained load
# ═══════════════════════════════════════════════════════════════════════

@_skip
class TestSustainedLoad:
    """Run continuous operations for a fixed duration to find degradation."""

    @pytest.mark.anyio
    async def test_30s_sustained_writes(self, client):
        """Write continuously for 30 seconds, measure throughput over time."""
        duration = 30
        ids = []
        errors = []
        buckets = []  # (timestamp, cumulative_count)

        start = time.monotonic()
        i = 0
        while time.monotonic() - start < duration:
            try:
                mid = await _force_store(
                    client,
                    f"Sustained write {i} token {random.randint(100000, 999999)}: "
                    "testing write throughput stability over extended periods.",
                )
                if mid:
                    ids.append(mid)
            except Exception as e:
                errors.append(f"write {i}: {e}")
            i += 1
            # Record bucket every 5 seconds
            elapsed = time.monotonic() - start
            if len(buckets) < int(elapsed / 5) + 1:
                buckets.append((elapsed, len(ids)))

        total_elapsed = time.monotonic() - start

        print()
        print(f"  Sustained writes over {total_elapsed:.0f}s:")
        print(f"    Total: {len(ids)} stored, {len(errors)} errors")
        print(f"    Overall rate: {len(ids) / total_elapsed:.1f} ops/s")

        # Print throughput per 5s bucket
        prev_count = 0
        for elapsed, count in buckets:
            bucket_ops = count - prev_count
            print(f"    {elapsed:>5.0f}s: {count:>4} total ({bucket_ops:>3} in last 5s)")
            prev_count = count

        if errors:
            print(f"    First error: {errors[0]}")

        # Cleanup
        for mid in ids:
            try:
                await client.call("forget", {"memory_id": mid})
            except Exception:
                pass

        # Degradation check: last bucket should not be < 50% of first bucket
        if len(buckets) >= 4:
            first_rate = buckets[1][1] - buckets[0][1]  # ops in first 5s after warmup
            last_rate = buckets[-1][1] - buckets[-2][1]  # ops in last 5s
            if first_rate > 0:
                degradation = last_rate / first_rate
                print(f"    Degradation ratio: {degradation:.2f} (last/first bucket)")
                if degradation < 0.5:
                    print(f"    WARNING: >50% throughput degradation detected")

    @pytest.mark.anyio
    async def test_30s_sustained_mixed(self, client):
        """Mixed read/write for 30 seconds."""
        duration = 30
        write_count = 0
        read_count = 0
        write_ids = []
        errors = []

        start = time.monotonic()
        i = 0
        while time.monotonic() - start < duration:
            try:
                if i % 3 == 0:
                    # Write
                    mid = await _force_store(
                        client,
                        f"Sustained mixed {i} token {random.randint(100000, 999999)}",
                    )
                    if mid:
                        write_ids.append(mid)
                    write_count += 1
                else:
                    # Read
                    await client.call("recall", {
                        "query": f"sustained mixed query {random.randint(1, 100)}",
                        "max_results": 3,
                    })
                    read_count += 1
            except Exception as e:
                errors.append(f"op {i}: {e}")
            i += 1

        elapsed = time.monotonic() - start
        total = write_count + read_count

        print()
        print(f"  Sustained mixed over {elapsed:.0f}s:")
        print(f"    Writes: {write_count}, Reads: {read_count}, Total: {total}")
        print(f"    Rate: {total / elapsed:.1f} ops/s")
        print(f"    Errors: {len(errors)}")

        # Cleanup
        for mid in write_ids:
            try:
                await client.call("forget", {"memory_id": mid})
            except Exception:
                pass
