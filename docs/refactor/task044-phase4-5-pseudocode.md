# TASK-044 Phases 4 & 5: Migration Script + Integration

## Phase 4: Migration Script (scripts/extract_references.py)

### 4A. CLI and Entrypoint

```
#!/usr/bin/env python3
"""Extract reference memories from Vaire DB into static files.

Usage:
    python scripts/extract_references.py --db ~/.vaire/memory.db --output reference/
    python scripts/extract_references.py --db ~/.vaire/memory.db --output reference/ --dry-run
    python scripts/extract_references.py --db ~/.vaire/memory.db --output reference/ --tasks-out reference/tasks-seed.json
"""
import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

NIST_800_53_FAMILIES = [
    "AC", "AT", "AU", "CA", "CM", "CP", "IA", "IR",
    "MA", "PE", "PL", "PM", "PS", "PT", "RA", "SA", "SC", "SI",
]

# Maps anchor prefix patterns to output file paths + manifest topic keys
OTHER_REFERENCE_MAP = {
    "NIST_CSF": {
        "path": "nist/csf-2.0-framework.md",
        "topic": "csf-2.0",
        "category": "nist",
        "description": "NIST Cybersecurity Framework 2.0",
        "keywords": ["csf", "cybersecurity", "framework", "nist"],
    },
    "NIST_SP_800-181": {
        "path": "nist/sp-800-181-nice.md",
        "topic": "nice-framework",
        "category": "nist",
        "description": "NICE Cybersecurity Workforce Framework (SP 800-181)",
        "keywords": ["nice", "workforce", "roles", "nist"],
    },
    "NIST_AI_100-2": {
        "path": "nist/ai-100-2-aml-taxonomy.md",
        "topic": "ai-100-2",
        "category": "nist",
        "description": "NIST AI 100-2 Adversarial ML Taxonomy",
        "keywords": ["ai", "adversarial", "machine learning", "taxonomy"],
    },
}

# Tag-based references (not identified by contextual_prefix)
TAG_REFERENCE_MAP = {
    "siem-field-inventory": {
        "path": "siem-field-inventory.md",
        "topic": "siem-fields",
        "category": "operational",
        "description": "SIEM field inventory and normalization mappings",
        "keywords": ["siem", "logging", "field mapping"],
    },
    "osint": {
        "path": "osint-sources.md",
        "topic": "osint-sources",
        "category": "operational",
        "description": "OSINT intelligence sources and techniques",
        "keywords": ["osint", "intelligence", "reconnaissance"],
    },
}

# Content-pattern-based references
CONTENT_REFERENCE_MAP = {
    "dev_standards": {
        "anchor_patterns": ["dev standards", "clean code"],
        "path": "dev-standards-reference.md",
        "topic": "dev-standards",
        "category": "operational",
        "description": "Development standards and clean code reference",
        "keywords": ["dev", "standards", "clean code", "best practices"],
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract reference memories from Vaire DB")
    parser.add_argument("--db", required=True, help="Path to Vaire memory.db")
    parser.add_argument("--output", required=True, help="Output directory for reference files")
    parser.add_argument("--tasks-out", default=None, help="Output path for tasks-seed.json")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be written without writing")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    output = Path(args.output)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # Step 1: Extract NIST 800-53 by family
        families = extract_800_53_families(conn)

        # Step 2: Extract other reference types (CSF, NICE, AI 100-2)
        other_refs = extract_other_references(conn)

        # Step 3: Extract tag-based references (SIEM, OSINT)
        tag_refs = extract_tag_references(conn)

        # Step 4: Extract content-pattern references (dev standards)
        content_refs = extract_content_references(conn)

        # Step 5: Extract task-repo memories
        tasks = extract_tasks(conn)

        # Step 6: Validate extraction completeness
        validate_extraction(families, other_refs, tag_refs, content_refs)

        if args.dry_run:
            report_dry_run(families, other_refs, tag_refs, content_refs, tasks)
            return

        # Step 7: Write all files
        write_800_53_files(output, families)
        write_other_reference_files(output, other_refs)
        write_tag_reference_files(output, tag_refs)
        write_content_reference_files(output, content_refs)

        # Step 8: Write tasks-seed.json
        tasks_out = Path(args.tasks_out) if args.tasks_out else output / "tasks-seed.json"
        write_tasks_seed(tasks_out, tasks)

        # Step 9: Compute hashes and generate manifest
        manifest = generate_manifest(output, families, other_refs, tag_refs, content_refs)
        manifest_path = output / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        logger.info("Manifest written: %s", manifest_path)

        # Step 10: Final validation of written files
        validate_output(output, manifest)

        # Step 11: Report
        report_summary(families, other_refs, tag_refs, content_refs, tasks, manifest)

    finally:
        conn.close()
```


### 4B. NIST 800-53 Extraction

```
def extract_800_53_families(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Query memories with NIST 800-53 anchor tags, group by control family.

    Returns: {"AC": [row1, row2, ...], "AT": [...], ...}
    Each row is a dict with keys: id, content, contextual_prefix, tags, heat, created_at
    """
    # Query pattern: contextual_prefix contains "NIST_800-53_"
    # AND tagged with _anchor (these are anchor memories)
    cursor = conn.execute("""
        SELECT id, content, contextual_prefix, tags, heat, created_at
        FROM memories
        WHERE contextual_prefix LIKE '%NIST_800-53_%'
        ORDER BY contextual_prefix
    """)

    families = defaultdict(list)
    unmatched = []

    for row in cursor:
        prefix = row["contextual_prefix"] or ""
        # Extract family code: "NIST standards reference: NIST_800-53_AC.md" -> "AC"
        family = extract_family_from_prefix(prefix)

        if family and family in NIST_800_53_FAMILIES:
            families[family].append(dict(row))
        else:
            unmatched.append(dict(row))
            logger.warning("800-53 memory with unrecognized family: id=%d prefix=%r", row["id"], prefix)

    logger.info("Extracted 800-53 families: %s", {k: len(v) for k, v in families.items()})
    if unmatched:
        logger.warning("%d 800-53 memories did not match any family", len(unmatched))

    return dict(families)


def extract_family_from_prefix(prefix: str) -> str | None:
    """Parse family code from contextual_prefix string.

    Input:  "NIST standards reference: NIST_800-53_AC.md"
    Output: "AC"

    Returns None if pattern does not match.
    """
    # Pattern: look for "NIST_800-53_{FAMILY}" where FAMILY is 2-letter uppercase
    import re
    match = re.search(r"NIST_800-53_([A-Z]{2})", prefix)
    if match:
        return match.group(1)
    return None
```


### 4C. Other Reference Extraction

```
def extract_other_references(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Extract CSF, NICE, AI 100-2 memories by contextual_prefix pattern.

    Returns: {"NIST_CSF": [rows], "NIST_SP_800-181": [rows], ...}
    """
    results = {}

    for prefix_key, ref_info in OTHER_REFERENCE_MAP.items():
        cursor = conn.execute("""
            SELECT id, content, contextual_prefix, tags, heat, created_at
            FROM memories
            WHERE contextual_prefix LIKE ?
            ORDER BY created_at
        """, (f"%{prefix_key}%",))

        rows = [dict(r) for r in cursor]
        results[prefix_key] = rows
        logger.info("Extracted %s: %d memories", prefix_key, len(rows))

    return results


def extract_tag_references(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Extract SIEM and OSINT memories by tag.

    Returns: {"siem-field-inventory": [rows], "osint": [rows]}
    """
    results = {}

    for tag_key, ref_info in TAG_REFERENCE_MAP.items():
        cursor = conn.execute("""
            SELECT id, content, contextual_prefix, tags, heat, created_at
            FROM memories
            WHERE tags LIKE ?
            ORDER BY created_at
        """, (f"%{tag_key}%",))

        rows = [dict(r) for r in cursor]
        results[tag_key] = rows
        logger.info("Extracted tag=%s: %d memories", tag_key, len(rows))

    return results


def extract_content_references(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Extract dev-standards and similar by contextual_prefix content patterns.

    Returns: {"dev_standards": [rows]}
    """
    results = {}

    for key, ref_info in CONTENT_REFERENCE_MAP.items():
        all_rows = []
        for pattern in ref_info["anchor_patterns"]:
            cursor = conn.execute("""
                SELECT id, content, contextual_prefix, tags, heat, created_at
                FROM memories
                WHERE contextual_prefix LIKE ?
                ORDER BY created_at
            """, (f"%{pattern}%",))
            all_rows.extend(dict(r) for r in cursor)

        # Deduplicate by memory id (patterns may overlap)
        seen_ids = set()
        deduped = []
        for row in all_rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                deduped.append(row)

        results[key] = deduped
        logger.info("Extracted %s: %d memories", key, len(deduped))

    return results
```


### 4D. Task Extraction

```
def extract_tasks(conn: sqlite3.Connection) -> dict:
    """Extract task-repo memories and convert to tasks.json schema.

    Returns dict in tasks.json format:
    {
        "schema_version": 1,
        "version": 1,
        "next_id": N,
        "tasks": {
            "TASK-001": { ... },
            "TASK-002": { ... },
        }
    }
    """
    cursor = conn.execute("""
        SELECT id, content, contextual_prefix, tags, heat, created_at
        FROM memories
        WHERE tags LIKE '%task-repo%' AND tags LIKE '%task%'
        ORDER BY created_at
    """)

    tasks = {}
    max_task_num = 0

    for row in cursor:
        content = row["content"]
        tags_str = row["tags"] or ""

        task_id = parse_task_id(content, tags_str)
        if not task_id:
            logger.warning("Could not parse task ID from memory id=%d", row["id"])
            continue

        # Extract numeric part for next_id tracking
        task_num = parse_task_number(task_id)
        if task_num > max_task_num:
            max_task_num = task_num

        # Parse task fields from memory content
        task = parse_task_content(content, tags_str, row["created_at"])
        tasks[task_id] = task

    logger.info("Extracted %d tasks, max number=%d", len(tasks), max_task_num)

    return {
        "schema_version": 1,
        "version": 1,
        "next_id": max_task_num + 1,
        "tasks": tasks,
    }


def parse_task_id(content: str, tags: str) -> str | None:
    """Extract TASK-NNN from content or tags.

    Looks for pattern "TASK-\\d{3,}" in content first, then in tags.
    Returns first match or None.
    """
    import re
    # Try content first
    match = re.search(r"(TASK-\d{3,})", content)
    if match:
        return match.group(1)
    # Try tags
    match = re.search(r"(TASK-\d{3,})", tags)
    if match:
        return match.group(1)
    return None


def parse_task_number(task_id: str) -> int:
    """Extract numeric portion: "TASK-044" -> 44."""
    return int(task_id.split("-")[1])


def parse_task_content(content: str, tags: str, created_at: str) -> dict:
    """Parse a task memory's content into the tasks.json task schema.

    Expected output per task:
    {
        "title": "...",
        "status": "open" | "done" | "blocked",
        "role": "...",
        "priority": "low" | "medium" | "high",
        "description": "...",
        "created": "ISO date",
        "updated": "ISO date",
        "agent_block": null,
        "history": [{"ts": "...", "action": "migrated", "agent": "extract_references.py"}]
    }
    """
    # Derive status from tags: "status:open" -> "open", "status:done" -> "done"
    status = "open"  # default
    for tag in tags.split(","):
        tag = tag.strip()
        if tag.startswith("status:"):
            status = tag.split(":", 1)[1]
            break

    # Derive role from tags: "role:defender" -> "defender"
    role = ""
    for tag in tags.split(","):
        tag = tag.strip()
        if tag.startswith("role:"):
            role = tag.split(":", 1)[1]
            break

    # Derive priority from tags: "priority:high" -> "high"
    priority = "medium"  # default
    for tag in tags.split(","):
        tag = tag.strip()
        if tag.startswith("priority:"):
            priority = tag.split(":", 1)[1]
            break

    # Title: first line of content (or first sentence)
    lines = content.strip().split("\n")
    title = lines[0].strip() if lines else "Untitled"
    # Strip markdown heading markers
    if title.startswith("#"):
        title = title.lstrip("#").strip()
    # Truncate overlong titles
    if len(title) > 120:
        title = title[:117] + "..."

    # Description: remainder of content
    description = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

    return {
        "title": title,
        "status": status,
        "role": role,
        "priority": priority,
        "description": description,
        "created": created_at,
        "updated": created_at,
        "agent_block": None,
        "history": [
            {
                "ts": datetime.utcnow().isoformat() + "Z",
                "action": "migrated",
                "agent": "extract_references.py",
            }
        ],
    }
```


### 4E. File Writing

```
def write_800_53_files(output: Path, families: dict[str, list[dict]]) -> None:
    """Write one markdown file per 800-53 control family.

    Output: output/nist/800-53/{FAMILY}.md
    """
    family_dir = output / "nist" / "800-53"
    family_dir.mkdir(parents=True, exist_ok=True)

    for family, rows in families.items():
        file_path = family_dir / f"{family}.md"
        content = format_800_53_family(family, rows)

        # Validate UTF-8 before writing
        content.encode("utf-8")  # raises UnicodeEncodeError if invalid

        file_path.write_text(content, encoding="utf-8")
        logger.info("Wrote %s: %d bytes from %d memories", file_path, len(content), len(rows))


def format_800_53_family(family: str, rows: list[dict]) -> str:
    """Format a 800-53 family into a markdown document.

    Structure:
    # NIST 800-53 {Family Code}: {Family Name}
    
    ## {Control ID}
    {Memory content}
    
    ---
    
    ## {Control ID}
    ...
    """
    FAMILY_NAMES = {
        "AC": "Access Control",
        "AT": "Awareness and Training",
        "AU": "Audit and Accountability",
        "CA": "Assessment, Authorization, and Monitoring",
        "CM": "Configuration Management",
        "CP": "Contingency Planning",
        "IA": "Identification and Authentication",
        "IR": "Incident Response",
        "MA": "Maintenance",
        "PE": "Physical and Environmental Protection",
        "PL": "Planning",
        "PM": "Program Management",
        "PS": "Personnel Security",
        "PT": "PII Processing and Transparency",
        "RA": "Risk Assessment",
        "SA": "System and Services Acquisition",
        "SC": "System and Communications Protection",
        "SI": "System and Information Integrity",
    }

    # Sort rows by control number extracted from content
    # e.g., "AC-1" < "AC-2" < "AC-10"
    sorted_rows = sort_by_control_number(rows, family)

    header = f"# NIST 800-53: {family} - {FAMILY_NAMES.get(family, family)}\n\n"
    sections = []
    for row in sorted_rows:
        sections.append(row["content"].strip())

    return header + "\n\n---\n\n".join(sections) + "\n"


def sort_by_control_number(rows: list[dict], family: str) -> list[dict]:
    """Sort rows by control number within family.

    Extracts "{FAMILY}-{N}" pattern from content, sorts numerically.
    Rows without a parseable control number go at the end.
    """
    import re

    def sort_key(row):
        match = re.search(rf"{family}-(\d+)", row["content"])
        if match:
            return (0, int(match.group(1)))
        return (1, 0)  # unmatched rows go last

    return sorted(rows, key=sort_key)


def write_other_reference_files(output: Path, other_refs: dict[str, list[dict]]) -> None:
    """Write CSF, NICE, AI 100-2 to their respective files."""
    for prefix_key, rows in other_refs.items():
        if not rows:
            logger.warning("No memories found for %s, skipping file", prefix_key)
            continue

        ref_info = OTHER_REFERENCE_MAP[prefix_key]
        file_path = output / ref_info["path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)

        content = format_reference_document(ref_info["description"], rows)
        content.encode("utf-8")  # validate
        file_path.write_text(content, encoding="utf-8")
        logger.info("Wrote %s: %d bytes from %d memories", file_path, len(content), len(rows))


def write_tag_reference_files(output: Path, tag_refs: dict[str, list[dict]]) -> None:
    """Write SIEM, OSINT to their respective files."""
    for tag_key, rows in tag_refs.items():
        if not rows:
            logger.warning("No memories found for tag=%s, skipping file", tag_key)
            continue

        ref_info = TAG_REFERENCE_MAP[tag_key]
        file_path = output / ref_info["path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)

        content = format_reference_document(ref_info["description"], rows)
        content.encode("utf-8")
        file_path.write_text(content, encoding="utf-8")
        logger.info("Wrote %s: %d bytes from %d memories", file_path, len(content), len(rows))


def write_content_reference_files(output: Path, content_refs: dict[str, list[dict]]) -> None:
    """Write dev-standards and similar pattern-matched references."""
    for key, rows in content_refs.items():
        if not rows:
            logger.warning("No memories found for %s, skipping file", key)
            continue

        ref_info = CONTENT_REFERENCE_MAP[key]
        file_path = output / ref_info["path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)

        content = format_reference_document(ref_info["description"], rows)
        content.encode("utf-8")
        file_path.write_text(content, encoding="utf-8")
        logger.info("Wrote %s: %d bytes from %d memories", file_path, len(content), len(rows))


def format_reference_document(title: str, rows: list[dict]) -> str:
    """Format a generic reference document from memory rows.

    Structure:
    # {title}

    {memory 1 content}

    ---

    {memory 2 content}
    ...
    """
    header = f"# {title}\n\n"
    sections = [row["content"].strip() for row in rows]
    return header + "\n\n---\n\n".join(sections) + "\n"


def write_tasks_seed(tasks_out: Path, tasks: dict) -> None:
    """Write the tasks-seed.json file.

    Validates the task dict against expected schema before writing.
    Uses atomic write pattern: write to .tmp, then rename.
    """
    # Validate schema shape
    assert "schema_version" in tasks
    assert "version" in tasks
    assert "next_id" in tasks
    assert "tasks" in tasks
    assert isinstance(tasks["tasks"], dict)

    for task_id, task in tasks["tasks"].items():
        assert "title" in task, f"Task {task_id} missing title"
        assert "status" in task, f"Task {task_id} missing status"
        assert task["status"] in ("open", "done", "blocked", "abandoned"), \
            f"Task {task_id} has invalid status: {task['status']}"

    # Atomic write
    tmp_path = tasks_out.with_suffix(".tmp")
    tasks_out.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")
    tmp_path.rename(tasks_out)
    logger.info("Wrote tasks seed: %s (%d tasks)", tasks_out, len(tasks["tasks"]))
```


### 4F. Manifest Generation

```
def generate_manifest(
    output: Path,
    families: dict[str, list[dict]],
    other_refs: dict[str, list[dict]],
    tag_refs: dict[str, list[dict]],
    content_refs: dict[str, list[dict]],
) -> dict:
    """Generate manifest.json from all written files.

    Walks every file that was written, computes SHA256 hash, builds manifest entry.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")

    manifest = {
        "schema_version": 1,
        "categories": {
            "directives": {
                "description": "Agent governance - prime directives and operational boundaries",
                "integrity": "required",
            },
            "nist": {
                "description": "NIST standards and frameworks",
                "integrity": "optional",
            },
            "operational": {
                "description": "SIEM fields, OSINT sources, dev standards",
                "integrity": "optional",
            },
        },
        "references": {},
    }

    # Add 800-53 families
    for family in sorted(families.keys()):
        rel_path = f"nist/800-53/{family}.md"
        abs_path = output / rel_path
        if abs_path.exists():
            content_hash = compute_sha256(abs_path)
            # Extract section headers for the manifest
            sections = extract_section_headers(abs_path)
            manifest["references"][f"800-53:{family}"] = {
                "path": rel_path,
                "category": "nist",
                "description": f"NIST 800-53 {family} control family ({len(families[family])} controls)",
                "keywords": ["nist", "800-53", family.lower()],
                "sections": sections,
                "content_hash": f"sha256:{content_hash}",
                "updated": today,
            }

    # Add other references (CSF, NICE, AI 100-2)
    for prefix_key, rows in other_refs.items():
        if not rows:
            continue
        ref_info = OTHER_REFERENCE_MAP[prefix_key]
        abs_path = output / ref_info["path"]
        if abs_path.exists():
            content_hash = compute_sha256(abs_path)
            sections = extract_section_headers(abs_path)
            manifest["references"][ref_info["topic"]] = {
                "path": ref_info["path"],
                "category": ref_info["category"],
                "description": ref_info["description"],
                "keywords": ref_info["keywords"],
                "sections": sections,
                "content_hash": f"sha256:{content_hash}",
                "updated": today,
            }

    # Add tag-based references (SIEM, OSINT)
    for tag_key, rows in tag_refs.items():
        if not rows:
            continue
        ref_info = TAG_REFERENCE_MAP[tag_key]
        abs_path = output / ref_info["path"]
        if abs_path.exists():
            content_hash = compute_sha256(abs_path)
            sections = extract_section_headers(abs_path)
            manifest["references"][ref_info["topic"]] = {
                "path": ref_info["path"],
                "category": ref_info["category"],
                "description": ref_info["description"],
                "keywords": ref_info["keywords"],
                "sections": sections,
                "content_hash": f"sha256:{content_hash}",
                "updated": today,
            }

    # Add content-pattern references (dev standards)
    for key, rows in content_refs.items():
        if not rows:
            continue
        ref_info = CONTENT_REFERENCE_MAP[key]
        abs_path = output / ref_info["path"]
        if abs_path.exists():
            content_hash = compute_sha256(abs_path)
            sections = extract_section_headers(abs_path)
            manifest["references"][ref_info["topic"]] = {
                "path": ref_info["path"],
                "category": ref_info["category"],
                "description": ref_info["description"],
                "keywords": ref_info["keywords"],
                "sections": sections,
                "content_hash": f"sha256:{content_hash}",
                "updated": today,
            }

    # NOTE: directives/all-agents.md is NOT generated by this script.
    # It is hand-authored and committed to the repo. The manifest entry
    # for directive:all-agents must be added manually or by a separate step.

    return manifest


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_section_headers(file_path: Path) -> list[str]:
    """Extract ## section headers from a markdown file.

    Returns list of header text (without the ## prefix).
    """
    headers = []
    for line in file_path.read_text().split("\n"):
        if line.startswith("## "):
            headers.append(line[3:].strip())
    return headers
```


### 4G. Validation and Reporting

```
def validate_extraction(
    families: dict,
    other_refs: dict,
    tag_refs: dict,
    content_refs: dict,
) -> None:
    """Pre-write validation: check that extraction produced reasonable results."""
    # Warn (don't error) on empty families — some may legitimately have no content
    empty_families = [f for f in NIST_800_53_FAMILIES if f not in families or not families.get(f)]
    if empty_families:
        logger.warning("No content found for 800-53 families: %s", empty_families)

    # Check for unexpectedly low counts
    total_800_53 = sum(len(v) for v in families.values())
    if total_800_53 < 10:
        logger.warning("Only %d total 800-53 memories found — expected ~320", total_800_53)

    # Check other refs have content
    for key, rows in other_refs.items():
        if not rows:
            logger.warning("No memories found for reference: %s", key)

    for key, rows in tag_refs.items():
        if not rows:
            logger.warning("No memories found for tag reference: %s", key)


def validate_output(output: Path, manifest: dict) -> None:
    """Post-write validation: verify all files are valid and manifest is correct."""
    errors = []

    # Every manifest reference must have a file on disk
    for topic, entry in manifest.get("references", {}).items():
        file_path = output / entry["path"]
        if not file_path.exists():
            errors.append(f"MISSING: {topic} -> {entry['path']}")
            continue

        # Verify UTF-8 readability
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            errors.append(f"UTF-8 ERROR: {topic} -> {e}")
            continue

        # Verify non-empty
        if len(content.strip()) == 0:
            errors.append(f"EMPTY: {topic} -> {entry['path']}")

        # Verify hash matches
        actual_hash = f"sha256:{compute_sha256(file_path)}"
        if actual_hash != entry["content_hash"]:
            errors.append(f"HASH MISMATCH: {topic} expected={entry['content_hash']} actual={actual_hash}")

    # Validate manifest schema shape
    required_top = {"schema_version", "categories", "references"}
    if set(manifest.keys()) != required_top:
        errors.append(f"Manifest has unexpected top-level keys: {set(manifest.keys()) - required_top}")

    if errors:
        for e in errors:
            logger.error("Validation: %s", e)
        raise RuntimeError(f"Output validation failed with {len(errors)} error(s)")

    logger.info("Output validation passed: %d references verified", len(manifest.get("references", {})))


def report_dry_run(families, other_refs, tag_refs, content_refs, tasks) -> None:
    """Print what would be written without actually writing."""
    print("=== DRY RUN REPORT ===")
    print()
    print("800-53 families:")
    for family in sorted(families.keys()):
        print(f"  {family}: {len(families[family])} memories -> nist/800-53/{family}.md")
    print()
    print("Other references:")
    for key, rows in other_refs.items():
        info = OTHER_REFERENCE_MAP[key]
        print(f"  {info['topic']}: {len(rows)} memories -> {info['path']}")
    print()
    print("Tag references:")
    for key, rows in tag_refs.items():
        info = TAG_REFERENCE_MAP[key]
        print(f"  {info['topic']}: {len(rows)} memories -> {info['path']}")
    print()
    print("Content references:")
    for key, rows in content_refs.items():
        info = CONTENT_REFERENCE_MAP[key]
        print(f"  {info['topic']}: {len(rows)} memories -> {info['path']}")
    print()
    print(f"Tasks: {len(tasks.get('tasks', {}))} tasks -> tasks-seed.json")
    total_memories = (
        sum(len(v) for v in families.values())
        + sum(len(v) for v in other_refs.values())
        + sum(len(v) for v in tag_refs.values())
        + sum(len(v) for v in content_refs.values())
    )
    print(f"\nTotal reference memories: {total_memories}")


def report_summary(families, other_refs, tag_refs, content_refs, tasks, manifest) -> None:
    """Print final summary of what was written."""
    print("=== EXTRACTION COMPLETE ===")
    print()

    file_count = len(manifest.get("references", {}))
    total_bytes = 0
    for topic, entry in manifest.get("references", {}).items():
        # Size was computed during manifest generation; re-stat for report
        # (Pseudocode: in implementation, accumulate sizes during writing)
        print(f"  {topic}: {entry['path']} [{entry['content_hash'][:20]}...]")

    print()
    print(f"Files written: {file_count}")
    print(f"Tasks migrated: {len(tasks.get('tasks', {}))}")
    print(f"Manifest: manifest.json")
    print()
    print("Next steps (manual, creator-approved):")
    print("  1. Review generated files for correctness")
    print("  2. Commit reference/ directory to repo")
    print("  3. Build Docker image to bake references in")
    print("  4. Groomer: bulk_delete ~320 original memories")
    print("  5. Add breadcrumb anchor: 'Static references available via load_reference(list=True)'")
    print("  6. Verify recall queries still work via breadcrumb")


if __name__ == "__main__":
    main()
```


---


## Phase 5: Integration

### 5A. Role Updates (Groomer update_content operations)

These are NOT code changes. They are Vaire memory content updates applied by the groomer
(or manually by creator) after the reference system is live.

```
# Role: Defender
# Current memory content (in "Context to pull" section):
#   3. recall("NIST CSF 2.0 core")
# Updated content:
#   3. load_reference("csf-2.0") -- or recall for operational context

# Implementation: groomer update_content on the defender role memory
#   Find memory: recall("role:defender") -> get id
#   Update: replace line containing 'recall("NIST CSF 2.0 core")'
#     with 'load_reference("csf-2.0") -- or recall for operational context'


# Role: Architect
# Current memory content:
#   recall("NIST 800-53 {family}")
# Updated content:
#   load_reference("800-53:{family}")

# Implementation: groomer update_content on the architect role memory
#   Find memory: recall("role:architect") -> get id
#   Update: replace recall("NIST 800-53") references with load_reference("800-53:...")


# ALL Roles: Add directive loading as first context step
# Add to every role's "Context to pull" section:
#   0. load_reference("directive:all-agents")  # Prime directive -- always load first
#
# Affected roles: Builder, Defender, Attacker, Architect, Dev, Vaire-Dev, Worker
#
# Implementation: for each role memory:
#   1. recall("role:{name}") -> get id and content
#   2. Find the "Context to pull" section (or equivalent)
#   3. Prepend: '0. load_reference("directive:all-agents")  # Prime directive'
#   4. groomer update_content with new content
#
# NOTE: numbering of existing steps shifts by 1 (old step 1 becomes step 1, old step 2 stays 2, etc.)
# The "0." numbering is intentional -- directive load is step zero, before any recall.

# Validation: after updating each role, recall("role:{name}") and verify:
#   - load_reference("directive:all-agents") appears first in context section
#   - No recall() calls remain for content that is now in the reference system
#   - All other role content is unchanged
```


### 5B. Vale Wake Cycle Addition

```
# File: vaire/vale_wake.py (or equivalent wake cycle module)
# Add a new phase to the wake cycle: reference_health_check

def reference_health_check(server):
    """Check reference system health during Vale wake cycle.

    Runs every wake. Non-blocking: flags issues for creator, never auto-fixes.
    """

    # Step 1: Verify reference system is operational
    try:
        index = server.load_reference(list=True)
    except Exception as e:
        # Reference system down -- flag it
        server.remember(
            f"Reference system unavailable during wake check: {e}",
            context="/home/user/workspace/vaire",
            tags=["creator-approval-required", "defender-review"],
        )
        return  # Can't continue checks without reference system

    # Step 2: Run health check on all reference files
    try:
        health = server.reference_loader.verify_health()
    except Exception as e:
        server.remember(
            f"Reference health check failed: {e}",
            context="/home/user/workspace/vaire",
            tags=["creator-approval-required", "defender-review"],
        )
        return

    # Step 3: Report any issues
    issues = {topic: status for topic, status in health.items() if status != "OK"}
    if issues:
        summary = "; ".join(f"{t}: {s}" for t, s in sorted(issues.items()))
        server.remember(
            f"Reference integrity issues detected: {summary}",
            context="/home/user/workspace/vaire",
            tags=["creator-approval-required", "defender-review"],
        )
        # DO NOT attempt to fix. DO NOT modify files. Just flag.
        logger.warning("Reference health issues: %s", issues)

    # Step 4: Staleness check
    stale = server.reference_loader.check_staleness(max_age_days=180)
    if stale:
        summary = ", ".join(f"{s['topic']} ({s['age_days']}d)" for s in stale[:10])
        if len(stale) > 10:
            summary += f" ... and {len(stale) - 10} more"

        server.remember(
            f"Standards review needed: {len(stale)} references older than 6 months: {summary}",
            context="/home/user/workspace/vaire",
            tags=["creator-approval-required", "reference-review"],
        )
        # DO NOT auto-update. DO NOT create tasks. Just flag for creator.

    logger.info(
        "Reference health check complete: %d ok, %d issues, %d stale",
        sum(1 for s in health.values() if s == "OK"),
        len(issues),
        len(stale),
    )


# Integration point: add to the wake cycle sequence
# In vale_wake.py main wake function:
#
# def wake_cycle(server):
#     ...existing phases...
#     reference_health_check(server)   # <-- add after existing phases
#     ...
```


### 5C. Test Plan

#### 5C-1. test_reference.py

```
import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from vaire.reference import ReferenceLoader
from vaire.security import PathSecurityError


# --- Fixtures ---

@pytest.fixture
def reference_dir(tmp_path):
    """Create a temporary reference directory with valid files and manifest."""
    # Create directory structure
    nist_dir = tmp_path / "nist" / "800-53"
    nist_dir.mkdir(parents=True)
    directives_dir = tmp_path / "directives"
    directives_dir.mkdir()

    # Write sample reference files
    ac_content = "# NIST 800-53: AC - Access Control\n\n## AC-1\nPolicy content.\n\n## AC-2\nAccount management.\n"
    (nist_dir / "AC.md").write_text(ac_content)

    directive_content = "# Prime Directive\n\n## Identity\nYou are an agent.\n\n## Operating Rules\nFollow rules.\n"
    (directives_dir / "all-agents.md").write_text(directive_content)

    # Compute real hashes
    ac_hash = compute_sha256(nist_dir / "AC.md")
    directive_hash = compute_sha256(directives_dir / "all-agents.md")

    # Write manifest
    manifest = {
        "schema_version": 1,
        "categories": {
            "directives": {"description": "Agent governance", "integrity": "required"},
            "nist": {"description": "NIST standards", "integrity": "optional"},
        },
        "references": {
            "800-53:AC": {
                "path": "nist/800-53/AC.md",
                "category": "nist",
                "description": "Access Control family",
                "keywords": ["nist", "800-53", "ac"],
                "sections": ["AC-1", "AC-2"],
                "content_hash": f"sha256:{ac_hash}",
                "updated": "2026-04-01",
            },
            "directive:all-agents": {
                "path": "directives/all-agents.md",
                "category": "directives",
                "description": "Prime directives for all agents",
                "keywords": ["directive", "policy"],
                "sections": ["Identity", "Operating Rules"],
                "content_hash": f"sha256:{directive_hash}",
                "updated": "2026-04-01",
            },
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    return tmp_path


@pytest.fixture
def loader(reference_dir, mock_settings):
    """Create and initialize a ReferenceLoader."""
    # mock_settings.reference_path_resolved = reference_dir
    # mock_settings.reference_manifest_resolved = reference_dir / "manifest.json"
    loader = ReferenceLoader(mock_settings)
    loader.load_manifest()
    return loader


# --- Test 1: Manifest loading ---

def test_manifest_load_valid(loader):
    """Load a well-formed manifest, verify parsing."""
    # GIVEN: a valid manifest was loaded by the fixture
    # WHEN: we check the loaded state
    # THEN: manifest is loaded with correct reference count
    assert loader._loaded is True
    assert "800-53:AC" in loader._manifest["references"]
    assert "directive:all-agents" in loader._manifest["references"]
    assert len(loader._manifest["references"]) == 2
    assert len(loader._manifest["categories"]) == 2


# --- Test 2: Schema rejects unknown keys ---

def test_manifest_schema_reject_unknown_keys(reference_dir, mock_settings):
    """Add unknown key to manifest, expect ValueError."""
    # GIVEN: manifest with extra top-level key "foo"
    manifest = json.loads((reference_dir / "manifest.json").read_text())
    manifest["foo"] = "bar"
    (reference_dir / "manifest.json").write_text(json.dumps(manifest))

    # WHEN: loading manifest
    loader = ReferenceLoader(mock_settings)

    # THEN: ValueError raised mentioning unknown key
    with pytest.raises(ValueError, match="Unknown manifest keys"):
        loader.load_manifest()


# --- Test 3: Schema rejects missing keys ---

def test_manifest_schema_reject_missing_keys(reference_dir, mock_settings):
    """Remove required key from manifest, expect ValueError."""
    # GIVEN: manifest missing "categories" key
    manifest = json.loads((reference_dir / "manifest.json").read_text())
    del manifest["categories"]
    (reference_dir / "manifest.json").write_text(json.dumps(manifest))

    # WHEN: loading
    loader = ReferenceLoader(mock_settings)

    # THEN: ValueError raised mentioning missing key
    with pytest.raises(ValueError, match="Missing manifest key"):
        loader.load_manifest()


# --- Test 4: Schema version check ---

def test_manifest_schema_version_check(reference_dir, mock_settings):
    """Wrong schema_version, expect ValueError."""
    # GIVEN: manifest with schema_version 99
    manifest = json.loads((reference_dir / "manifest.json").read_text())
    manifest["schema_version"] = 99
    (reference_dir / "manifest.json").write_text(json.dumps(manifest))

    # WHEN: loading
    loader = ReferenceLoader(mock_settings)

    # THEN: ValueError about unexpected schema version
    with pytest.raises(ValueError, match="schema version"):
        loader.load_manifest()


# --- Test 5: Load reference by topic ---

def test_load_reference_by_topic(loader):
    """Load a known topic, verify content returned."""
    # WHEN: loading 800-53:AC
    content = loader.load("800-53:AC")

    # THEN: content contains AC family material
    assert "Access Control" in content
    assert "AC-1" in content


# --- Test 6: Section extraction ---

def test_load_reference_section_extraction(loader):
    """Load specific section, verify only that section returned."""
    # WHEN: loading section "AC-2" from 800-53:AC
    content = loader.load("800-53:AC", section="AC-2")

    # THEN: contains AC-2 content but not AC-1
    assert "AC-2" in content
    assert "Account management" in content
    assert "AC-1" not in content or content.index("AC-2") < content.index("AC-1") if "AC-1" in content else True
    # Actually stricter: AC-1 should NOT appear (section extraction cuts at next header)
    assert "Policy content" not in content


# --- Test 7: Unknown topic ---

def test_load_reference_unknown_topic(loader):
    """Unknown topic raises KeyError with helpful message."""
    # WHEN: loading a nonexistent topic
    # THEN: KeyError with available topics listed
    with pytest.raises(KeyError, match="Unknown reference topic"):
        loader.load("nonexistent-topic")


# --- Test 8: List mode ---

def test_load_reference_list(loader):
    """Verify list mode returns index with categories and references."""
    # WHEN: listing all references
    result = loader.list_references()

    # THEN: includes both categories and both references
    assert "categories" in result
    assert "references" in result
    assert "800-53:AC" in result["references"]
    assert "directive:all-agents" in result["references"]
    assert "nist" in result["categories"]


# --- Test 9: List mode with category filter ---

def test_load_reference_list_filtered(loader):
    """Verify category filter restricts results."""
    # WHEN: listing only nist category
    result = loader.list_references(category="nist")

    # THEN: only nist references returned
    assert "800-53:AC" in result["references"]
    assert "directive:all-agents" not in result["references"]
    assert "directives" not in result["categories"]


# --- Test 10: Path traversal blocked ---

def test_path_traversal_blocked(reference_dir, mock_settings):
    """Topic with '../' in manifest path raises PathSecurityError on validation."""
    # GIVEN: manifest entry with path traversal attempt
    manifest = json.loads((reference_dir / "manifest.json").read_text())
    manifest["references"]["evil"] = {
        "path": "../../../etc/passwd",
        "category": "nist",
        "description": "Evil",
        "keywords": ["evil"],
        "content_hash": "sha256:000",
        "updated": "2026-01-01",
    }
    (reference_dir / "manifest.json").write_text(json.dumps(manifest))

    # WHEN: loading manifest
    loader = ReferenceLoader(mock_settings)

    # THEN: PathSecurityError during path verification
    with pytest.raises(PathSecurityError):
        loader.load_manifest()


# --- Test 11: Directive hash verification failure ---

def test_directive_hash_verification(loader, reference_dir):
    """Modify directive file after manifest load, expect integrity failure on load."""
    # GIVEN: directive file is modified after manifest load
    directive_path = reference_dir / "directives" / "all-agents.md"
    directive_path.write_text("TAMPERED CONTENT\n")

    # WHEN: loading the directive
    # THEN: PathSecurityError due to hash mismatch
    with pytest.raises(PathSecurityError, match="Integrity check failed"):
        loader.load("directive:all-agents")


# --- Test 12: Directive hash OK ---

def test_directive_hash_ok(loader):
    """Correct hash allows directive to load normally."""
    # WHEN: loading directive (file unchanged since manifest creation)
    content = loader.load("directive:all-agents")

    # THEN: content loads successfully
    assert "Prime Directive" in content


# --- Test 13: Directive audit logging ---

def test_directive_audit_logged(loader):
    """Verify logger.info called with DIRECTIVE LOAD for directive topics."""
    # WHEN: loading a directive topic
    with patch("vaire.reference.logger") as mock_logger:
        loader.load("directive:all-agents", agent_id="test-agent")

    # THEN: audit log entry created
    mock_logger.info.assert_any_call(
        "DIRECTIVE LOAD: topic=%r agent=%s",
        "directive:all-agents",
        "test-agent",
    )


# --- Test 14: Health check all OK ---

def test_health_check_all_ok(loader):
    """All files present and hashes match -> all OK."""
    # WHEN: running health check
    results = loader.verify_health()

    # THEN: all statuses are OK
    assert all(status == "OK" for status in results.values())
    assert len(results) == 2


# --- Test 15: Health check missing file ---

def test_health_check_missing_file(loader, reference_dir):
    """Remove a file, verify MISSING status."""
    # GIVEN: AC.md is deleted
    (reference_dir / "nist" / "800-53" / "AC.md").unlink()

    # WHEN: running health check
    results = loader.verify_health()

    # THEN: AC topic is MISSING
    assert results["800-53:AC"] == "MISSING"
    assert results["directive:all-agents"] == "OK"


# --- Test 16: Health check hash mismatch ---

def test_health_check_hash_mismatch(loader, reference_dir):
    """Modify file, verify HASH_MISMATCH status."""
    # GIVEN: AC.md content is modified
    (reference_dir / "nist" / "800-53" / "AC.md").write_text("Modified content\n")

    # WHEN: running health check
    results = loader.verify_health()

    # THEN: AC topic reports hash mismatch
    assert results["800-53:AC"] == "HASH_MISMATCH"


# --- Test 17: Staleness check ---

def test_staleness_check(loader):
    """Set old dates, verify stale list returned."""
    # GIVEN: manifest entries have dates older than 180 days
    # (The fixture uses "2026-04-01" which is only 3 days old -- not stale)
    # Modify manifest to have old dates for testing
    loader._manifest["references"]["800-53:AC"]["updated"] = "2025-01-01"

    # WHEN: checking staleness
    stale = loader.check_staleness(max_age_days=180)

    # THEN: AC is flagged as stale, directive is not
    stale_topics = [s["topic"] for s in stale]
    assert "800-53:AC" in stale_topics
    assert "directive:all-agents" not in stale_topics
    assert stale[0]["age_days"] > 180
```


#### 5C-2. test_task_engine.py

```
import json
import pytest
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from vaire.task_engine import TaskEngine


# --- Fixtures ---

@pytest.fixture
def task_file(tmp_path):
    """Path to a task file in a temp directory."""
    return tmp_path / "tasks.json"


@pytest.fixture
def engine(task_file, mock_settings):
    """Create a TaskEngine with test settings."""
    # mock_settings.task_data_path_resolved = task_file
    # mock_settings.TASK_CREATE_ALLOWED = "groomer-,creator"
    # mock_settings.TASK_HEARTBEAT_TTL = 30  (minutes)
    engine = TaskEngine(mock_settings)
    engine.start()  # or init, whatever the startup method is
    return engine


# --- Test 1: Create task ---

def test_create_task(engine):
    """Create a task, verify it appears in cache and has history."""
    # WHEN: creating a task as an allowed agent
    task_id = engine.create_task(
        agent_id="creator",
        title="Test task",
        description="A test task",
        role="dev",
        priority="medium",
    )

    # THEN: task exists in engine cache
    task = engine.get_task(task_id)
    assert task is not None
    assert task["title"] == "Test task"
    assert task["status"] == "open"
    assert task["role"] == "dev"
    assert len(task["history"]) == 1
    assert task["history"][0]["action"] == "created"
    assert task["history"][0]["agent"] == "creator"


# --- Test 2: Create task permission denied ---

def test_create_task_permission_denied(engine):
    """Non-allowed agent cannot create tasks."""
    # WHEN: creating a task as an unauthorized agent
    # THEN: PermissionError
    with pytest.raises(PermissionError, match="not allowed to create tasks"):
        engine.create_task(
            agent_id="random-agent",
            title="Unauthorized task",
            description="Should fail",
            role="dev",
            priority="low",
        )


# --- Test 3: Claim task ---

def test_claim_task(engine):
    """Claim a task, verify agent block and history."""
    # GIVEN: an open task exists
    task_id = engine.create_task(
        agent_id="creator", title="Claimable", description="", role="dev", priority="medium",
    )

    # WHEN: an agent claims it
    engine.claim_task(task_id, agent_id="worker-1")

    # THEN: agent_block is set, status is claimed, history records it
    task = engine.get_task(task_id)
    assert task["status"] == "claimed"
    assert task["agent_block"]["agent_id"] == "worker-1"
    assert task["agent_block"]["heartbeat"] is not None
    assert any(h["action"] == "claimed" for h in task["history"])


# --- Test 4: Claim one-at-a-time ---

def test_claim_task_one_at_a_time(engine):
    """Agent cannot claim second task while holding first."""
    # GIVEN: two open tasks, agent already holds one
    t1 = engine.create_task(agent_id="creator", title="Task 1", description="", role="dev", priority="medium")
    t2 = engine.create_task(agent_id="creator", title="Task 2", description="", role="dev", priority="medium")
    engine.claim_task(t1, agent_id="worker-1")

    # WHEN: same agent tries to claim second task
    # THEN: error (one-at-a-time enforcement)
    with pytest.raises(Exception, match="already holds"):
        engine.claim_task(t2, agent_id="worker-1")


# --- Test 5: Claim already-claimed task ---

def test_claim_task_already_claimed(engine):
    """Cannot claim a task held by another agent with active heartbeat."""
    # GIVEN: task claimed by another agent
    t1 = engine.create_task(agent_id="creator", title="Held", description="", role="dev", priority="medium")
    engine.claim_task(t1, agent_id="worker-1")

    # WHEN: different agent tries to claim
    # THEN: error
    with pytest.raises(Exception, match="already claimed"):
        engine.claim_task(t1, agent_id="worker-2")


# --- Test 6: Claim abandoned task ---

def test_claim_abandoned_task(engine):
    """Task with expired heartbeat can be reclaimed by another agent."""
    # GIVEN: task claimed by agent-1, heartbeat is expired
    t1 = engine.create_task(agent_id="creator", title="Abandoned", description="", role="dev", priority="medium")
    engine.claim_task(t1, agent_id="worker-1")

    # Simulate expired heartbeat by backdating
    task = engine.get_task(t1)
    expired_time = (datetime.utcnow() - timedelta(minutes=engine._heartbeat_ttl + 5)).isoformat() + "Z"
    task["agent_block"]["heartbeat"] = expired_time
    engine._save_task(t1, task)  # internal: write back modified task

    # WHEN: another agent claims it
    engine.claim_task(t1, agent_id="worker-2")

    # THEN: new agent holds the task
    task = engine.get_task(t1)
    assert task["agent_block"]["agent_id"] == "worker-2"
    assert any(h["action"] == "reclaimed" for h in task["history"])


# --- Test 7: Update task owner-only ---

def test_update_task_owner_only(engine):
    """Non-owner cannot update a claimed task."""
    # GIVEN: task claimed by worker-1
    t1 = engine.create_task(agent_id="creator", title="Owned", description="", role="dev", priority="medium")
    engine.claim_task(t1, agent_id="worker-1")

    # WHEN: worker-2 tries to update
    # THEN: PermissionError
    with pytest.raises(PermissionError, match="not the owner"):
        engine.update_task(t1, agent_id="worker-2", description="Hijacked")


# --- Test 8: Update refreshes heartbeat ---

def test_update_task_heartbeat_refresh(engine):
    """Update by owner refreshes the heartbeat timestamp."""
    # GIVEN: claimed task with known heartbeat
    t1 = engine.create_task(agent_id="creator", title="HB test", description="", role="dev", priority="medium")
    engine.claim_task(t1, agent_id="worker-1")
    old_hb = engine.get_task(t1)["agent_block"]["heartbeat"]

    # Small pause to ensure timestamp differs
    # (In real implementation, mock datetime; in pseudocode, conceptual)

    # WHEN: owner updates the task
    engine.update_task(t1, agent_id="worker-1", description="Updated description")

    # THEN: heartbeat is newer
    new_hb = engine.get_task(t1)["agent_block"]["heartbeat"]
    assert new_hb >= old_hb  # >= because resolution might be same second in tests


# --- Test 9: Complete task ---

def test_complete_task(engine):
    """Complete a task, verify status=done and agent block wiped."""
    # GIVEN: claimed task
    t1 = engine.create_task(agent_id="creator", title="Completable", description="", role="dev", priority="medium")
    engine.claim_task(t1, agent_id="worker-1")

    # WHEN: owner completes it
    engine.complete_task(t1, agent_id="worker-1")

    # THEN: status is done, agent block is None
    task = engine.get_task(t1)
    assert task["status"] == "done"
    assert task["agent_block"] is None
    assert any(h["action"] == "completed" for h in task["history"])


# --- Test 10: Release task ---

def test_release_task(engine):
    """Release a task, verify status=open and agent block wiped."""
    # GIVEN: claimed task
    t1 = engine.create_task(agent_id="creator", title="Releasable", description="", role="dev", priority="medium")
    engine.claim_task(t1, agent_id="worker-1")

    # WHEN: owner releases it
    engine.release_task(t1, agent_id="worker-1")

    # THEN: status is open, agent block is None
    task = engine.get_task(t1)
    assert task["status"] == "open"
    assert task["agent_block"] is None
    assert any(h["action"] == "released" for h in task["history"])


# --- Test 11: List tasks filtered ---

def test_list_tasks_filtered(engine):
    """Filter tasks by status and role."""
    # GIVEN: tasks with different statuses and roles
    engine.create_task(agent_id="creator", title="T1", description="", role="dev", priority="medium")
    t2 = engine.create_task(agent_id="creator", title="T2", description="", role="defender", priority="high")
    engine.claim_task(t2, agent_id="worker-1")

    # WHEN: filtering by status=open
    open_tasks = engine.list_tasks(status="open")
    assert len(open_tasks) == 1
    assert open_tasks[0]["title"] == "T1"

    # WHEN: filtering by role=defender
    defender_tasks = engine.list_tasks(role="defender")
    assert len(defender_tasks) == 1
    assert defender_tasks[0]["title"] == "T2"


# --- Test 12: List tasks abandoned flag ---

def test_list_tasks_abandoned_flag(engine):
    """Tasks with stale heartbeat show _abandoned flag in listing."""
    # GIVEN: claimed task with expired heartbeat
    t1 = engine.create_task(agent_id="creator", title="Stale", description="", role="dev", priority="medium")
    engine.claim_task(t1, agent_id="worker-1")

    # Backdate heartbeat
    task = engine.get_task(t1)
    expired = (datetime.utcnow() - timedelta(minutes=engine._heartbeat_ttl + 5)).isoformat() + "Z"
    task["agent_block"]["heartbeat"] = expired
    engine._save_task(t1, task)

    # WHEN: listing tasks
    tasks = engine.list_tasks()

    # THEN: task has _abandoned flag
    stale_task = [t for t in tasks if t["_id"] == t1][0]
    assert stale_task["_abandoned"] is True


# --- Test 13: Version increments ---

def test_version_increments(engine):
    """Every mutation bumps the file version."""
    # GIVEN: initial version
    v0 = engine._version

    # WHEN: creating a task
    engine.create_task(agent_id="creator", title="V test", description="", role="dev", priority="medium")
    v1 = engine._version
    assert v1 == v0 + 1

    # WHEN: another mutation
    t = list(engine._tasks.keys())[0]
    engine.claim_task(t, agent_id="worker-1")
    v2 = engine._version
    assert v2 == v1 + 1


# --- Test 14: Atomic file write ---

def test_atomic_file_write(engine, task_file):
    """Verify writes use .tmp + rename pattern."""
    # WHEN: creating a task (which triggers a file write)
    with patch("pathlib.Path.rename") as mock_rename:
        with patch("pathlib.Path.write_text") as mock_write:
            engine.create_task(
                agent_id="creator", title="Atomic", description="", role="dev", priority="medium",
            )

    # THEN: write was to .tmp file, rename was to final path
    # Verify the tmp path was used (ends with .tmp)
    mock_write.assert_called_once()
    write_target = mock_write.call_args  # pseudocode: verify called on .tmp path
    mock_rename.assert_called_once()
    rename_target = mock_rename.call_args[0][0]  # should be task_file path


# --- Test 15: History append-only ---

def test_history_append_only(engine):
    """History only grows, never shrinks."""
    # GIVEN: task with creation history
    t = engine.create_task(agent_id="creator", title="History", description="", role="dev", priority="medium")
    assert len(engine.get_task(t)["history"]) == 1

    # WHEN: claim
    engine.claim_task(t, agent_id="worker-1")
    assert len(engine.get_task(t)["history"]) == 2

    # WHEN: update
    engine.update_task(t, agent_id="worker-1", description="Changed")
    assert len(engine.get_task(t)["history"]) == 3

    # WHEN: complete
    engine.complete_task(t, agent_id="worker-1")
    assert len(engine.get_task(t)["history"]) == 4

    # THEN: history never decreased, each entry is unique
    history = engine.get_task(t)["history"]
    actions = [h["action"] for h in history]
    assert actions == ["created", "claimed", "updated", "completed"]
```


#### 5C-3. test_gitlab_client.py

```
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import httpx

from vaire.gitlab_client import GitLabClient, GitLabConflictError


# --- Fixtures ---

@pytest.fixture
def client():
    """Create a GitLabClient with test config."""
    return GitLabClient(
        api_url="https://gitlab.example.com/api/v4",
        project_id="42",
        token="glpat-test-token-1234",
    )


# --- Test 1: Read file ---

def test_read_file(client):
    """Mock httpx GET, verify file content is base64-decoded."""
    # GIVEN: GitLab API returns base64-encoded file content
    import base64
    file_content = '{"tasks": {}}'
    encoded = base64.b64encode(file_content.encode()).decode()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "content": encoded,
        "encoding": "base64",
        "last_commit_id": "abc123",
    }

    with patch.object(client._http, "get", return_value=mock_response):
        result = client.read_file("tasks.json", branch="main")

    # THEN: decoded content and commit_id returned
    assert result["content"] == file_content
    assert result["last_commit_id"] == "abc123"


# --- Test 2: Write file optimistic lock ---

def test_write_file_optimistic_lock(client):
    """409 response raises GitLabConflictError."""
    # GIVEN: GitLab returns 409 Conflict (file changed since last read)
    mock_response = MagicMock()
    mock_response.status_code = 409
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Conflict", request=MagicMock(), response=mock_response,
    )

    with patch.object(client._http, "put", return_value=mock_response):
        mock_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            "Conflict", request=MagicMock(), response=mock_response,
        ))

        # THEN: GitLabConflictError raised
        with pytest.raises(GitLabConflictError):
            client.write_file(
                path="tasks.json",
                content='{"tasks": {}}',
                commit_message="update",
                branch="main",
                last_commit_id="abc123",
            )


# --- Test 3: Write file create on 404 ---

def test_write_file_create_on_404(client):
    """PUT 404 falls back to POST (file doesn't exist yet)."""
    # GIVEN: PUT returns 404, POST succeeds
    mock_404 = MagicMock()
    mock_404.status_code = 404
    mock_404.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_404,
    )

    mock_201 = MagicMock()
    mock_201.status_code = 201

    with patch.object(client._http, "put", return_value=mock_404) as mock_put:
        mock_put.return_value.raise_for_status = mock_404.raise_for_status
        with patch.object(client._http, "post", return_value=mock_201) as mock_post:
            client.write_file(
                path="tasks.json",
                content='{"tasks": {}}',
                commit_message="create",
                branch="main",
                last_commit_id=None,
            )

    # THEN: POST was called as fallback
    mock_post.assert_called_once()


# --- Test 4: Token not in repr ---

def test_token_not_in_repr(client):
    """Verify repr() does not expose the token."""
    # WHEN: converting client to string/repr
    r = repr(client)

    # THEN: token value is masked
    assert "glpat-test-token-1234" not in r
    assert "***" in r or "REDACTED" in r


# --- Test 5: TLS always on ---

def test_tls_always_on(client):
    """Verify httpx.Client is created with verify=True (TLS validation enabled)."""
    # THEN: the internal httpx client has TLS verification enabled
    # (Check that httpx.Client was instantiated with verify=True or default)
    assert client._http._transport._pool._ssl_context is not None  # pseudocode
    # In practice: verify the Client constructor arg or inspect the transport
    # Alternative: patch httpx.Client and assert verify was not set to False
```


#### 5C-4. test_task_sync.py

```
import json
import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timedelta

from vaire.task_engine import TaskEngine, TaskSync
from vaire.gitlab_client import GitLabClient, GitLabConflictError


# --- Fixtures ---

@pytest.fixture
def sync(engine, mock_gitlab_client):
    """Create a TaskSync instance with mocked GitLab client."""
    return TaskSync(engine=engine, gitlab=mock_gitlab_client)


@pytest.fixture
def mock_gitlab_client():
    """Mock GitLabClient that returns controllable responses."""
    client = MagicMock(spec=GitLabClient)
    # Default: return empty remote tasks
    client.read_file.return_value = {
        "content": json.dumps({
            "schema_version": 1,
            "version": 1,
            "next_id": 1,
            "tasks": {},
        }),
        "last_commit_id": "abc123",
    }
    return client


def make_task(task_id, status="open", agent_id=None, heartbeat=None):
    """Helper: create a task dict for testing."""
    task = {
        "title": f"Task {task_id}",
        "status": status,
        "role": "dev",
        "priority": "medium",
        "description": "",
        "created": "2026-04-01T00:00:00Z",
        "updated": "2026-04-01T00:00:00Z",
        "agent_block": None,
        "history": [{"ts": "2026-04-01T00:00:00Z", "action": "created", "agent": "test"}],
    }
    if agent_id:
        hb = heartbeat or datetime.utcnow().isoformat() + "Z"
        task["agent_block"] = {"agent_id": agent_id, "heartbeat": hb}
        task["status"] = "claimed"
    return task


# --- Test 1: Sync dirty flag ---

def test_sync_dirty_flag(sync, engine, mock_gitlab_client):
    """dirty=True triggers sync, dirty=False skips."""
    # GIVEN: engine is not dirty
    engine._dirty = False

    # WHEN: sync runs
    sync.sync_once()

    # THEN: no GitLab calls made
    mock_gitlab_client.read_file.assert_not_called()
    mock_gitlab_client.write_file.assert_not_called()

    # GIVEN: engine is dirty
    engine._dirty = True

    # WHEN: sync runs
    sync.sync_once()

    # THEN: GitLab read + write called
    mock_gitlab_client.read_file.assert_called_once()


# --- Test 2: Merge local wins active ---

def test_merge_local_wins_active(sync):
    """Local agent block with newer heartbeat wins over remote."""
    # GIVEN: both local and remote have same task, local heartbeat is newer
    local_task = make_task("TASK-001", agent_id="worker-1",
                          heartbeat=datetime.utcnow().isoformat() + "Z")
    remote_task = make_task("TASK-001", agent_id="worker-2",
                           heartbeat=(datetime.utcnow() - timedelta(minutes=5)).isoformat() + "Z")

    # WHEN: merging
    merged = sync._merge_task("TASK-001", local_task, remote_task)

    # THEN: local agent block wins
    assert merged["agent_block"]["agent_id"] == "worker-1"


# --- Test 3: Merge history union ---

def test_merge_history_union(sync):
    """Histories from both sides merged and deduped."""
    # GIVEN: local and remote have overlapping + unique history entries
    local_task = make_task("TASK-001")
    local_task["history"] = [
        {"ts": "2026-04-01T00:00:00Z", "action": "created", "agent": "test"},
        {"ts": "2026-04-02T00:00:00Z", "action": "claimed", "agent": "worker-1"},
    ]
    remote_task = make_task("TASK-001")
    remote_task["history"] = [
        {"ts": "2026-04-01T00:00:00Z", "action": "created", "agent": "test"},  # duplicate
        {"ts": "2026-04-02T12:00:00Z", "action": "updated", "agent": "worker-1"},
    ]

    # WHEN: merging
    merged = sync._merge_task("TASK-001", local_task, remote_task)

    # THEN: all 3 unique entries present, sorted by timestamp
    assert len(merged["history"]) == 3
    actions = [h["action"] for h in merged["history"]]
    assert actions == ["created", "claimed", "updated"]


# --- Test 4: Merge done wins ---

def test_merge_done_wins(sync):
    """If either side is done, merged result is done."""
    # GIVEN: local is claimed, remote is done
    local_task = make_task("TASK-001", agent_id="worker-1")
    remote_task = make_task("TASK-001", status="done")

    # WHEN: merging
    merged = sync._merge_task("TASK-001", local_task, remote_task)

    # THEN: status is done, agent block wiped
    assert merged["status"] == "done"
    assert merged["agent_block"] is None


# --- Test 5: Merge remote-only task ---

def test_merge_remote_only_task(sync, engine, mock_gitlab_client):
    """Task in remote not in local gets pulled in."""
    # GIVEN: remote has a task that local doesn't
    remote_tasks = {
        "schema_version": 1, "version": 2, "next_id": 2,
        "tasks": {"TASK-001": make_task("TASK-001")},
    }
    mock_gitlab_client.read_file.return_value = {
        "content": json.dumps(remote_tasks),
        "last_commit_id": "abc123",
    }
    engine._dirty = True

    # WHEN: sync runs
    sync.sync_once()

    # THEN: task appears in local
    assert engine.get_task("TASK-001") is not None


# --- Test 6: Merge local-only task ---

def test_merge_local_only_task(sync, engine, mock_gitlab_client):
    """Task in local not in remote gets pushed out."""
    # GIVEN: local has a task, remote is empty
    engine.create_task(agent_id="creator", title="Local only", description="", role="dev", priority="medium")
    engine._dirty = True

    # WHEN: sync runs
    sync.sync_once()

    # THEN: write_file was called with the local task included
    mock_gitlab_client.write_file.assert_called_once()
    written_content = json.loads(mock_gitlab_client.write_file.call_args.kwargs["content"])
    assert len(written_content["tasks"]) == 1


# --- Test 7: Version monotonicity ---

def test_version_monotonicity(sync, engine, mock_gitlab_client):
    """Remote version < local version triggers warning."""
    # GIVEN: remote version is behind local
    remote_tasks = {"schema_version": 1, "version": 1, "next_id": 1, "tasks": {}}
    mock_gitlab_client.read_file.return_value = {
        "content": json.dumps(remote_tasks), "last_commit_id": "abc123",
    }
    engine._version = 5
    engine._dirty = True

    # WHEN: sync runs
    with patch("vaire.task_engine.logger") as mock_logger:
        sync.sync_once()

    # THEN: warning logged about version regression
    mock_logger.warning.assert_any_call(
        # Pseudocode: assert any warning call mentions version or regression
    )


# --- Test 8: Schema version mismatch ---

def test_schema_version_mismatch(sync, engine, mock_gitlab_client):
    """Different schema_version in remote causes merge refusal."""
    # GIVEN: remote has schema_version 2 (local expects 1)
    remote_tasks = {"schema_version": 2, "version": 1, "next_id": 1, "tasks": {}}
    mock_gitlab_client.read_file.return_value = {
        "content": json.dumps(remote_tasks), "last_commit_id": "abc123",
    }
    engine._dirty = True

    # WHEN: sync runs
    # THEN: merge is refused, dirty flag stays True
    sync.sync_once()
    assert engine._dirty is True  # not cleared because merge refused
    mock_gitlab_client.write_file.assert_not_called()


# --- Test 9: GitLab unreachable ---

def test_gitlab_unreachable(sync, engine, mock_gitlab_client):
    """Network failure keeps dirty=True for retry next cycle."""
    # GIVEN: GitLab read raises connection error
    mock_gitlab_client.read_file.side_effect = Exception("Connection refused")
    engine._dirty = True

    # WHEN: sync runs
    sync.sync_once()  # should not raise

    # THEN: dirty flag still True (will retry next cycle)
    assert engine._dirty is True


# --- Test 10: Conflict retry ---

def test_conflict_retry(sync, engine, mock_gitlab_client):
    """409 triggers re-read and retry, max 3 attempts."""
    # GIVEN: first write returns 409, second write succeeds
    engine._dirty = True
    engine.create_task(agent_id="creator", title="Conflict", description="", role="dev", priority="medium")

    remote_tasks = {"schema_version": 1, "version": 1, "next_id": 1, "tasks": {}}
    mock_gitlab_client.read_file.return_value = {
        "content": json.dumps(remote_tasks), "last_commit_id": "abc123",
    }

    call_count = 0
    def write_side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise GitLabConflictError("409 Conflict")
        return None  # success on second attempt

    mock_gitlab_client.write_file.side_effect = write_side_effect

    # WHEN: sync runs
    sync.sync_once()

    # THEN: write was retried, read was called again for fresh data
    assert mock_gitlab_client.write_file.call_count == 2
    assert mock_gitlab_client.read_file.call_count >= 2  # re-read on conflict
```
