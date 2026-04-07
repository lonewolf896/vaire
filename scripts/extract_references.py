#!/usr/bin/env python3
"""Extract reference memories from Vaire DB into static reference files.

Usage:
    python scripts/extract_references.py --db ~/.vaire/memory.db --output reference/
    python scripts/extract_references.py --db ~/.vaire/memory.db --output reference/ --dry-run
"""
import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FAMILY_NAMES = {
    "AC": "Access Control",
    "AT": "Awareness and Training",
    "AU": "Audit and Accountability",
    "CA": "Assessment, Authorization, and Monitoring",
    "CM": "Configuration Management",
    "CP": "Contingency Planning",
    "IA": "Identification and Authentication",
    "IN": "Information",
    "IR": "Incident Response",
    "MA": "Maintenance",
    "MP": "Media Protection",
    "PE": "Physical and Environmental Protection",
    "PL": "Planning",
    "PM": "Program Management",
    "PS": "Personnel Security",
    "PT": "PII Processing and Transparency",
    "RA": "Risk Assessment",
    "SA": "System and Services Acquisition",
    "SC": "System and Communications Protection",
    "SI": "System and Information Integrity",
    "SR": "Supply Chain Risk Management",
}

OTHER_REFERENCES = {
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
    "NIST_800-53B": {
        "path": "nist/800-53B-baselines.md",
        "topic": "800-53B-baselines",
        "category": "nist",
        "description": "NIST 800-53B Control Baselines",
        "keywords": ["nist", "800-53b", "baselines", "controls"],
    },
}

TAG_REFERENCES = {
    "osint": {
        "path": "osint-sources.md",
        "topic": "osint-sources",
        "category": "operational",
        "description": "OSINT intelligence sources and techniques",
        "keywords": ["osint", "intelligence", "reconnaissance"],
    },
}


def extract_family_from_prefix(prefix: str) -> str | None:
    """Extract 800-53 family code from contextual_prefix."""
    match = re.search(r"NIST_800-53_([A-Z]{2})\.", prefix)
    if match:
        return match.group(1)
    return None


def sort_by_control_number(rows: list[dict], family: str) -> list[dict]:
    """Sort rows by control number within family (AC-1 < AC-2 < AC-10)."""
    def sort_key(row):
        match = re.search(rf"{family}-(\d+)", row["content"])
        if match:
            return (0, int(match.group(1)))
        return (1, 0)
    return sorted(rows, key=sort_key)


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_section_headers(path: Path) -> list[str]:
    """Extract ## section headers from a markdown file."""
    headers = []
    for line in path.read_text().split("\n"):
        if line.startswith("## "):
            # Extract just the control ID (e.g. "AC-1" from "## AC-1: Policy...")
            header = line[3:].strip()
            # Try to get just the control ID for cleaner index
            m = re.match(r"([A-Z]{2}-\d+)", header)
            if m:
                headers.append(m.group(1))
            else:
                headers.append(header)
    return headers


def extract_800_53(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Extract NIST 800-53 memories grouped by family."""
    cursor = conn.execute("""
        SELECT id, content, contextual_prefix
        FROM memories
        WHERE contextual_prefix LIKE '%NIST\\_800-53\\_%' ESCAPE '\\'
          AND contextual_prefix NOT LIKE '%NIST\\_800-53B%' ESCAPE '\\'
        ORDER BY contextual_prefix
    """)

    families: dict[str, list[dict]] = defaultdict(list)
    for row in cursor:
        prefix = row["contextual_prefix"] or ""
        family = extract_family_from_prefix(prefix)
        if family:
            families[family].append(dict(row))
        else:
            logger.warning("Unrecognized family in prefix: id=%d %r", row["id"], prefix[:80])

    for f in sorted(families):
        logger.info("  800-53 %s: %d memories", f, len(families[f]))
    return dict(families)


def extract_other_references(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Extract CSF, NICE, AI 100-2, 800-53B memories."""
    results = {}
    for key, info in OTHER_REFERENCES.items():
        cursor = conn.execute("""
            SELECT id, content, contextual_prefix
            FROM memories
            WHERE contextual_prefix LIKE ?
            ORDER BY created_at
        """, (f"%{key}%",))
        rows = [dict(r) for r in cursor]
        results[key] = rows
        logger.info("  %s: %d memories", key, len(rows))
    return results


def extract_tag_references(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Extract OSINT and similar by tag."""
    results = {}
    for tag, info in TAG_REFERENCES.items():
        cursor = conn.execute("""
            SELECT id, content, contextual_prefix
            FROM memories
            WHERE tags LIKE ?
            ORDER BY created_at
        """, (f"%{tag}%",))
        rows = [dict(r) for r in cursor]
        results[tag] = rows
        logger.info("  tag=%s: %d memories", tag, len(rows))
    return results


def format_800_53_family(family: str, rows: list[dict]) -> str:
    """Format one 800-53 family into a single markdown file."""
    name = FAMILY_NAMES.get(family, family)
    sorted_rows = sort_by_control_number(rows, family)
    header = f"# NIST 800-53: {family} — {name}\n\n"
    sections = [row["content"].strip() for row in sorted_rows]
    return header + "\n\n---\n\n".join(sections) + "\n"


def format_reference_doc(title: str, rows: list[dict]) -> str:
    """Format a generic reference document."""
    header = f"# {title}\n\n"
    sections = [row["content"].strip() for row in rows]
    return header + "\n\n---\n\n".join(sections) + "\n"


def write_files(
    output: Path,
    families: dict[str, list[dict]],
    other_refs: dict[str, list[dict]],
    tag_refs: dict[str, list[dict]],
) -> list[int]:
    """Write all reference files. Returns list of extracted memory IDs."""
    memory_ids = []

    # 800-53 families
    family_dir = output / "nist" / "800-53"
    family_dir.mkdir(parents=True, exist_ok=True)
    for family, rows in sorted(families.items()):
        content = format_800_53_family(family, rows)
        path = family_dir / f"{family}.md"
        path.write_text(content, encoding="utf-8")
        memory_ids.extend(r["id"] for r in rows)
        logger.info("Wrote %s (%d bytes, %d memories)", path.name, len(content), len(rows))

    # Other references
    for key, rows in other_refs.items():
        if not rows:
            continue
        info = OTHER_REFERENCES[key]
        path = output / info["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        content = format_reference_doc(info["description"], rows)
        path.write_text(content, encoding="utf-8")
        memory_ids.extend(r["id"] for r in rows)
        logger.info("Wrote %s (%d bytes, %d memories)", path.name, len(content), len(rows))

    # Tag references
    for tag, rows in tag_refs.items():
        if not rows:
            continue
        info = TAG_REFERENCES[tag]
        path = output / info["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        content = format_reference_doc(info["description"], rows)
        path.write_text(content, encoding="utf-8")
        memory_ids.extend(r["id"] for r in rows)
        logger.info("Wrote %s (%d bytes, %d memories)", path.name, len(content), len(rows))

    return memory_ids


def generate_manifest(
    output: Path,
    families: dict[str, list[dict]],
    other_refs: dict[str, list[dict]],
    tag_refs: dict[str, list[dict]],
) -> dict:
    """Generate manifest.json from written files."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    manifest: dict = {
        "schema_version": 1,
        "categories": {
            "directives": {
                "description": "Agent governance — prime directives and operational boundaries",
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

    # 800-53 families
    for family in sorted(families.keys()):
        rel_path = f"nist/800-53/{family}.md"
        abs_path = output / rel_path
        if not abs_path.exists():
            continue
        sections = extract_section_headers(abs_path)
        manifest["references"][f"800-53:{family}"] = {
            "path": rel_path,
            "category": "nist",
            "description": f"NIST 800-53 {family} — {FAMILY_NAMES.get(family, family)} ({len(families[family])} controls)",
            "keywords": ["nist", "800-53", family.lower()],
            "sections": sections,
            "content_hash": f"sha256:{compute_sha256(abs_path)}",
            "updated": today,
        }

    # Other references
    for key, rows in other_refs.items():
        if not rows:
            continue
        info = OTHER_REFERENCES[key]
        abs_path = output / info["path"]
        if not abs_path.exists():
            continue
        sections = extract_section_headers(abs_path)
        manifest["references"][info["topic"]] = {
            "path": info["path"],
            "category": info["category"],
            "description": info["description"],
            "keywords": info["keywords"],
            "sections": sections,
            "content_hash": f"sha256:{compute_sha256(abs_path)}",
            "updated": today,
        }

    # Tag references
    for tag, rows in tag_refs.items():
        if not rows:
            continue
        info = TAG_REFERENCES[tag]
        abs_path = output / info["path"]
        if not abs_path.exists():
            continue
        sections = extract_section_headers(abs_path)
        manifest["references"][info["topic"]] = {
            "path": info["path"],
            "category": info["category"],
            "description": info["description"],
            "keywords": info["keywords"],
            "sections": sections,
            "content_hash": f"sha256:{compute_sha256(abs_path)}",
            "updated": today,
        }

    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="Extract reference memories from Vaire DB into static files"
    )
    parser.add_argument("--db", required=True, help="Path to Vaire memory.db")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--dry-run", action="store_true", help="Report only")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    output = Path(args.output)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        logger.info("Extracting references from %s ...", db_path)
        families = extract_800_53(conn)
        other_refs = extract_other_references(conn)
        tag_refs = extract_tag_references(conn)

        total_800_53 = sum(len(v) for v in families.values())
        total_other = sum(len(v) for v in other_refs.values())
        total_tag = sum(len(v) for v in tag_refs.values())
        total = total_800_53 + total_other + total_tag

        logger.info(
            "Found %d reference memories (%d 800-53, %d other, %d tag)",
            total, total_800_53, total_other, total_tag,
        )

        if args.dry_run:
            logger.info("DRY RUN — no files written")
            logger.info("Would write %d 800-53 family files", len(families))
            logger.info("Would write %d other reference files", sum(1 for v in other_refs.values() if v))
            logger.info("Would write %d tag reference files", sum(1 for v in tag_refs.values() if v))
            return

        output.mkdir(parents=True, exist_ok=True)
        memory_ids = write_files(output, families, other_refs, tag_refs)
        manifest = generate_manifest(output, families, other_refs, tag_refs)

        manifest_path = output / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        logger.info("Wrote manifest.json (%d references)", len(manifest["references"]))

        # Write extracted IDs for deletion step
        ids_path = output / "extracted_ids.json"
        ids_path.write_text(json.dumps(sorted(set(memory_ids))) + "\n")
        logger.info("Wrote extracted_ids.json (%d unique IDs)", len(set(memory_ids)))

        logger.info("Done. %d files, %d memories extracted.", len(manifest["references"]) + 1, len(set(memory_ids)))

    finally:
        conn.close()


if __name__ == "__main__":
    main()
