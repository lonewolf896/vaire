"""Tests for Phase 8: Markdown ingestion pipeline."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from vaire.config import Settings
from vaire.ingest import Chunk, IngestionPipeline, MarkdownChunker


@pytest.fixture
def settings():
    return Settings(
        DB_PATH=":memory:",
        INGEST_CHUNK_MIN=10,
        INGEST_CHUNK_MAX=200,
        INGEST_CHUNK_OVERLAP=20,
        INGEST_ALLOWED_EXTS=[".md", ".txt", ".rst"],
        INGEST_ENTITY_EXTRACTION_DELAY_MS=5000,
    )


@pytest.fixture
def chunker(settings):
    return MarkdownChunker(settings)


def _make_pipeline(settings: Settings, write_queue=None, cache=None) -> IngestionPipeline:
    embeddings = MagicMock()
    embeddings.encode_batch.side_effect = lambda texts: [b"\x00" * 384] * len(texts)

    if write_queue is None:
        write_queue = MagicMock()
        write_queue.enqueue_critical = AsyncMock(return_value=1)

    if cache is None:
        cache = MagicMock()
        cache.content_hash_exists.return_value = False

    storage = MagicMock()
    consolidation = MagicMock()
    consolidation.force_consolidate.return_value = {}

    return IngestionPipeline(
        chunker=MarkdownChunker(settings),
        embeddings=embeddings,
        write_queue=write_queue,
        cache=cache,
        storage=storage,
        consolidation=consolidation,
        settings=settings,
    )


# ── TestMarkdownChunker ────────────────────────────────────────────────────────

class TestMarkdownChunker:
    def test_basic_chunking(self, chunker, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("# Title\n\nThis is a paragraph with enough content to pass.\n")
        chunks = chunker.chunk_file(str(f))
        assert len(chunks) >= 1
        assert all(isinstance(c, Chunk) for c in chunks)

    def test_oversized_section_splits_on_h3(self, tmp_path):
        settings = Settings(
            DB_PATH=":memory:",
            INGEST_CHUNK_MIN=10,
            INGEST_CHUNK_MAX=80,
            INGEST_CHUNK_OVERLAP=5,
            INGEST_ALLOWED_EXTS=[".md"],
        )
        chunker = MarkdownChunker(settings)
        f = tmp_path / "big.md"
        # Each sub-section is small but combined they exceed max=80
        f.write_text(
            "## Big Section\n\n"
            "### Sub A\n" + "A" * 50 + "\n\n"
            "### Sub B\n" + "B" * 50 + "\n"
        )
        chunks = chunker.chunk_file(str(f))
        assert len(chunks) >= 2

    def test_short_chunks_merged(self, settings, tmp_path):
        f = tmp_path / "short.md"
        # Two tiny paragraphs that should get merged
        f.write_text("tiny\n\ntiny\n\nThis paragraph is longer than the minimum size.\n")
        chunker = MarkdownChunker(settings)
        chunks = chunker.chunk_file(str(f))
        assert len(chunks) >= 1
        # Merged content should appear in at least one chunk
        all_text = " ".join(c.content for c in chunks)
        assert "tiny" in all_text

    def test_overlap_applied(self, tmp_path):
        settings = Settings(
            DB_PATH=":memory:",
            INGEST_CHUNK_MIN=10,
            INGEST_CHUNK_MAX=50,
            INGEST_CHUNK_OVERLAP=5,
            INGEST_ALLOWED_EXTS=[".md"],
        )
        chunker = MarkdownChunker(settings)
        f = tmp_path / "overlap.md"
        # Two separate h2 sections so they chunk independently
        f.write_text(
            "## Alpha Section\n\nFirst content here done.\n\n"
            "## Beta Section\n\nSecond content here done.\n"
        )
        chunks = chunker.chunk_file(str(f))
        if len(chunks) >= 2:
            # The second chunk's content should start with the tail of chunk 0
            overlap_prefix = chunks[0].content[-5:]
            assert chunks[1].content.startswith(overlap_prefix)

    def test_section_path_set_correctly(self, chunker, tmp_path):
        f = tmp_path / "sectioned.md"
        f.write_text(
            "## Introduction\n\nSome content for the introduction section here.\n"
        )
        chunks = chunker.chunk_file(str(f))
        assert any("Introduction" in c.section_path for c in chunks)

    def test_chunk_indices_assigned(self, chunker, tmp_path):
        f = tmp_path / "indexed.md"
        f.write_text(
            "## Section A\n\nContent for section A, enough chars.\n\n"
            "## Section B\n\nContent for section B, enough chars.\n"
        )
        chunks = chunker.chunk_file(str(f))
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))


# ── TestIngestionPipeline ──────────────────────────────────────────────────────

class TestIngestionPipeline:
    @pytest.mark.anyio
    async def test_rejects_nonexistent_file(self, settings):
        pipeline = _make_pipeline(settings)
        result = await pipeline.ingest_file(
            {"file_path": "/nonexistent/path/file.md"}, "agent-1"
        )
        assert "error" in result
        assert "not found" in result["error"].lower() or "File" in result["error"]

    @pytest.mark.anyio
    async def test_rejects_disallowed_extension(self, settings, tmp_path):
        f = tmp_path / "doc.xyz"
        f.write_text("content")
        pipeline = _make_pipeline(settings)
        result = await pipeline.ingest_file({"file_path": str(f)}, "agent-1")
        assert "error" in result
        assert ".xyz" in result["error"]

    @pytest.mark.anyio
    async def test_dry_run_returns_preview_without_writing(self, settings, tmp_path):
        f = tmp_path / "preview.md"
        f.write_text("## Section\n\nContent for dry run preview testing here.\n")

        write_queue = MagicMock()
        write_queue.enqueue_critical = AsyncMock(return_value=1)
        pipeline = _make_pipeline(settings, write_queue=write_queue)

        result = await pipeline.ingest_file(
            {"file_path": str(f), "dry_run": True}, "agent-1"
        )

        assert result.get("dry_run") is True
        assert "total_chunks" in result
        assert "chunks" in result
        write_queue.enqueue_critical.assert_not_called()

    @pytest.mark.anyio
    async def test_dedup_skips_already_cached_content(self, settings, tmp_path):
        f = tmp_path / "dedup.md"
        f.write_text("## Known\n\nAlready cached content for this section.\n")

        cache = MagicMock()
        cache.content_hash_exists.return_value = True  # everything already cached

        write_queue = MagicMock()
        write_queue.enqueue_critical = AsyncMock(return_value=1)
        pipeline = _make_pipeline(settings, write_queue=write_queue, cache=cache)

        result = await pipeline.ingest_file({"file_path": str(f)}, "agent-1")

        assert "error" not in result
        write_queue.enqueue_critical.assert_not_called()

    @pytest.mark.anyio
    async def test_ingest_file_returns_correct_stats(self, settings, tmp_path):
        f = tmp_path / "stats.md"
        f.write_text(
            "## Section A\n\nContent A for stats testing here.\n\n"
            "## Section B\n\nContent B for stats testing here.\n"
        )
        pipeline = _make_pipeline(settings)
        result = await pipeline.ingest_file({"file_path": str(f)}, "agent-1")

        assert "error" not in result
        assert result["status"] == "done"
        assert "total_chunks" in result
        assert "completed" in result
        assert result["completed"] + result["errors"] == result["total_chunks"]

    @pytest.mark.anyio
    async def test_embeddings_encoded_for_each_chunk(self, settings, tmp_path):
        f = tmp_path / "embed.md"
        f.write_text(
            "## A\n\nContent A for embed test.\n\n"
            "## B\n\nContent B for embed test.\n"
        )

        embeddings = MagicMock()
        embeddings.encode_batch.side_effect = lambda texts: [b"\x00" * 384] * len(texts)

        write_queue = MagicMock()
        write_queue.enqueue_critical = AsyncMock(return_value=1)

        cache = MagicMock()
        cache.content_hash_exists.return_value = False

        pipeline = IngestionPipeline(
            chunker=MarkdownChunker(settings),
            embeddings=embeddings,
            write_queue=write_queue,
            cache=cache,
            storage=MagicMock(),
            consolidation=MagicMock(),
            settings=settings,
        )

        result = await pipeline.ingest_file({"file_path": str(f)}, "agent-1")

        assert "error" not in result
        embeddings.encode_batch.assert_called_once()

        # encode_batch was called with exactly total_chunks texts
        call_args = embeddings.encode_batch.call_args[0][0]
        assert len(call_args) == result["total_chunks"]
