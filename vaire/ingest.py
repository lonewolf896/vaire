"""Markdown ingestion pipeline for Vaire.

Reads .md (and optionally .txt, .rst) files, chunks them semantically,
deduplicates against the cache, and enqueues embeddings + storage writes
through the write queue.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vaire.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    content: str
    source_file: str
    section_path: list[str]   # ["h2 heading", "h3 heading"]
    chunk_index: int
    char_offset: int


@dataclass
class IngestionJob:
    job_id: str
    file_path: str
    total_chunks: int
    completed: int
    errors: int
    status: str               # "running" | "done" | "error"
    started_at: str
    finished_at: str | None = None


class MarkdownChunker:
    """Splits a Markdown file into semantically meaningful chunks."""

    def __init__(self, settings: Settings) -> None:
        self._min = settings.INGEST_CHUNK_MIN
        self._max = settings.INGEST_CHUNK_MAX
        self._overlap = settings.INGEST_CHUNK_OVERLAP
        self._h2_re = re.compile(r"^## .+", re.MULTILINE)
        self._h3_re = re.compile(r"^### .+", re.MULTILINE)
        self._para_re = re.compile(r"\n{2,}")

    def chunk_file(self, file_path: str) -> list[Chunk]:
        text = Path(file_path).read_text(encoding="utf-8")
        if not text.strip():
            return []
        chunks = self._split_on_pattern(text, self._h2_re, file_path, [])

        # For oversized sections split on h3
        expanded: list[Chunk] = []
        for chunk in chunks:
            if len(chunk.content) > self._max:
                sub = self._split_on_pattern(
                    chunk.content, self._h3_re, file_path, list(chunk.section_path)
                )
                expanded.extend(sub)
            else:
                expanded.append(chunk)

        # Split on paragraphs
        para_expanded: list[Chunk] = []
        for chunk in expanded:
            para_expanded.extend(self._split_paragraphs(chunk))

        # Merge short chunks then apply overlap
        merged = self._merge_short(para_expanded)
        with_overlap = self._apply_overlap(merged)

        for i, chunk in enumerate(with_overlap):
            chunk.chunk_index = i

        return with_overlap

    def _split_on_pattern(
        self,
        text: str,
        pattern: re.Pattern,
        source_file: str,
        section_path: list[str],
    ) -> list[Chunk]:
        matches = list(pattern.finditer(text))
        if not matches:
            return [
                Chunk(
                    content=text,
                    source_file=source_file,
                    section_path=list(section_path),
                    chunk_index=0,
                    char_offset=0,
                )
            ]

        chunks: list[Chunk] = []

        # Text before the first heading
        if matches[0].start() > 0:
            pre = text[: matches[0].start()].strip()
            if pre:
                chunks.append(
                    Chunk(
                        content=pre,
                        source_file=source_file,
                        section_path=list(section_path),
                        chunk_index=0,
                        char_offset=0,
                    )
                )

        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section_text = text[start:end].strip()
            heading = match.group(0).lstrip("#").strip()
            child_path = list(section_path) + [heading]
            chunks.append(
                Chunk(
                    content=section_text,
                    source_file=source_file,
                    section_path=child_path,
                    chunk_index=0,
                    char_offset=start,
                )
            )

        return chunks

    def _split_paragraphs(self, chunk: Chunk) -> list[Chunk]:
        paragraphs = self._para_re.split(chunk.content)
        result: list[Chunk] = []
        offset = chunk.char_offset

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(para) > self._max:
                result.extend(self._hard_split(para, chunk))
            else:
                result.append(
                    Chunk(
                        content=para,
                        source_file=chunk.source_file,
                        section_path=list(chunk.section_path),
                        chunk_index=0,
                        char_offset=offset,
                    )
                )
            offset += len(para) + 2  # approximate

        return result if result else [chunk]

    def _hard_split(self, text: str, parent: Chunk) -> list[Chunk]:
        # Ensure stride is large enough to make progress: at least 25% of max,
        # capped to avoid an explosion when overlap is misconfigured >= max.
        stride = max(self._max - self._overlap, max(self._max // 4, 50))
        chunks: list[Chunk] = []
        pos = 0
        while pos < len(text):
            end = min(pos + self._max, len(text))
            chunks.append(
                Chunk(
                    content=text[pos:end],
                    source_file=parent.source_file,
                    section_path=list(parent.section_path),
                    chunk_index=0,
                    char_offset=parent.char_offset + pos,
                )
            )
            pos += stride
        return chunks

    def _merge_short(self, chunks: list[Chunk]) -> list[Chunk]:
        if not chunks:
            return chunks

        result: list[Chunk] = []
        carry = chunks[0]

        for chunk in chunks[1:]:
            if len(carry.content) < self._min:
                carry = Chunk(
                    content=carry.content + "\n" + chunk.content,
                    source_file=carry.source_file,
                    section_path=carry.section_path,
                    chunk_index=0,
                    char_offset=carry.char_offset,
                )
            else:
                result.append(carry)
                carry = chunk

        # Handle last carry
        if len(carry.content) < self._min and result:
            last = result[-1]
            result[-1] = Chunk(
                content=last.content + "\n" + carry.content,
                source_file=last.source_file,
                section_path=last.section_path,
                chunk_index=0,
                char_offset=last.char_offset,
            )
        else:
            result.append(carry)

        return result

    def _apply_overlap(self, chunks: list[Chunk]) -> list[Chunk]:
        # Guard: -0 is not 0 in Python slice notation; content[-0:] returns the
        # entire string.  Return early if overlap is zero to avoid duplication.
        if len(chunks) <= 1 or self._overlap == 0:
            return list(chunks)

        result = [chunks[0]]
        for i in range(1, len(chunks)):
            prev_tail = chunks[i - 1].content[-self._overlap:]
            chunk = chunks[i]
            result.append(
                Chunk(
                    content=prev_tail + chunk.content,
                    source_file=chunk.source_file,
                    section_path=chunk.section_path,
                    chunk_index=chunk.chunk_index,
                    char_offset=chunk.char_offset,
                )
            )

        return result


class IngestionPipeline:
    """Orchestrates file ingestion: chunk → embed → dedup → enqueue."""

    def __init__(
        self,
        chunker: MarkdownChunker,
        embeddings: Any,
        write_queue: Any,
        cache: Any,
        storage: Any,
        consolidation: Any,
        settings: Settings,
    ) -> None:
        self._chunker = chunker
        self._embeddings = embeddings
        self._write_queue = write_queue
        self._cache = cache
        self._storage = storage
        self._consolidation = consolidation
        self._settings = settings
        self._active_jobs: dict[str, IngestionJob] = {}
        # Set to True during ingest_directory to suppress per-file deferred
        # consolidation tasks (one consolidated run happens at the end instead).
        self._bulk_ingest_active: bool = False

    async def ingest_file(self, params: dict, agent_id: str) -> dict:
        file_path = params.get("file_path", "")
        dry_run = params.get("dry_run", False)
        project_dir = params.get("project_dir", "")
        tags = params.get("tags", [])

        # S1: validate
        p = Path(file_path)
        if not p.exists():
            return {"error": f"File not found: {file_path}"}
        if not p.is_file():
            return {"error": f"Not a file: {file_path}"}
        if p.suffix.lower() not in self._settings.INGEST_ALLOWED_EXTS:
            return {"error": f"Unsupported extension: {p.suffix!r}"}

        try:
            chunks = self._chunker.chunk_file(file_path)
        except Exception as exc:
            return {"error": f"Failed to read/chunk file: {exc}"}

        if dry_run:
            return {
                "dry_run": True,
                "file_path": file_path,
                "total_chunks": len(chunks),
                "chunks": [
                    {
                        "content": c.content[:200],
                        "section_path": c.section_path,
                        "chunk_index": c.chunk_index,
                    }
                    for c in chunks
                ],
            }

        job_id = str(uuid.uuid4())
        job = IngestionJob(
            job_id=job_id,
            file_path=file_path,
            total_chunks=len(chunks),
            completed=0,
            errors=0,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._active_jobs[job_id] = job

        texts = [c.content for c in chunks]
        embeddings = self._embeddings.encode_batch(texts)

        for chunk, embedding in zip(chunks, embeddings):
            if self._cache.content_hash_exists(chunk.content):
                continue
            try:
                effective_dir = self._settings.remap_path(
                    project_dir or chunk.source_file
                )
                await self._write_queue.enqueue_critical(
                    "upsert_memory",
                    content=chunk.content,
                    embedding=embedding,
                    heat=1.0,
                    importance=self._settings.INGEST_DEFAULT_IMPORTANCE,
                    is_protected=True,
                    project_dir=effective_dir,
                    tags=tags,
                    agent_id=agent_id,
                    source_file=chunk.source_file,
                    section_path="/".join(chunk.section_path),
                )
                job.completed += 1
            except Exception as exc:
                logger.error(
                    "Failed to ingest chunk %d of %s: %s",
                    chunk.chunk_index,
                    file_path,
                    exc,
                )
                job.errors += 1

        job.status = "done"
        job.finished_at = datetime.now(timezone.utc).isoformat()

        # Skip per-file deferred consolidation during bulk ingest — ingest_directory
        # runs a single consolidated pass after all files finish to avoid 78 concurrent
        # consolidation threads competing for the write lock.
        if not self._bulk_ingest_active:
            asyncio.create_task(self._extract_entities_deferred(job_id))

        return {
            "job_id": job_id,
            "file_path": file_path,
            "total_chunks": job.total_chunks,
            "completed": job.completed,
            "errors": job.errors,
            "status": job.status,
        }

    async def ingest_directory(self, params: dict, agent_id: str) -> dict:
        dir_path = params.get("directory_path", "")
        p = Path(dir_path)
        if not p.exists() or not p.is_dir():
            return {"error": f"Directory not found: {dir_path}"}

        files: list[Path] = []
        for ext in self._settings.INGEST_ALLOWED_EXTS:
            files.extend(p.rglob(f"*{ext}"))

        total: dict[str, Any] = {
            "total_chunks": 0,
            "completed": 0,
            "errors": 0,
            "files": 0,
        }

        # Pause the consolidation daemon and suppress per-file deferred tasks for
        # the duration of the directory ingest.  A single consolidation runs after
        # all writes are committed.
        if hasattr(self._consolidation, "pause"):
            self._consolidation.pause()
        self._bulk_ingest_active = True
        try:
            for f in files:
                file_params = dict(params)
                file_params["file_path"] = str(f)
                result = await self.ingest_file(file_params, agent_id)
                if "error" not in result and not result.get("dry_run"):
                    total["total_chunks"] += result.get("total_chunks", 0)
                    total["completed"] += result.get("completed", 0)
                    total["errors"] += result.get("errors", 0)
                    total["files"] += 1
        finally:
            self._bulk_ingest_active = False
            if hasattr(self._consolidation, "resume"):
                self._consolidation.resume()

        # Single deferred consolidation for the entire directory.
        asyncio.create_task(self._extract_entities_deferred("directory"))

        return {"directory_path": dir_path, **total}

    async def ingest_status(self, params: dict, agent_id: str) -> dict:
        job_id = params.get("job_id", "")
        job = self._active_jobs.get(job_id)
        if job is None:
            return {"error": f"Job not found: {job_id}"}
        return {
            "job_id": job.job_id,
            "file_path": job.file_path,
            "total_chunks": job.total_chunks,
            "completed": job.completed,
            "errors": job.errors,
            "status": job.status,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    async def ingest_preview(self, params: dict, agent_id: str) -> dict:
        preview_params = dict(params)
        preview_params["dry_run"] = True
        return await self.ingest_file(preview_params, agent_id)

    async def _extract_entities_deferred(self, job_id: str) -> None:
        delay = self._settings.INGEST_ENTITY_EXTRACTION_DELAY_MS / 1000.0
        await asyncio.sleep(delay)
        # If a new bulk ingest started while we were sleeping, skip — the new
        # ingest will schedule its own consolidation at the end.
        if self._bulk_ingest_active:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._consolidation.force_consolidate)
