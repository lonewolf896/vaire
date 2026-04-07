# TASK-044 Phase 2: Reference System — Manifest + load_reference + Files

## 2A. Manifest Schema (vaire/reference/manifest.json)

```json
{
  "schema_version": 1,
  "categories": {
    "directives": {
      "description": "Agent governance — prime directives and operational boundaries",
      "integrity": "required"
    },
    "nist": {
      "description": "NIST standards and frameworks",
      "integrity": "optional"
    },
    "operational": {
      "description": "SIEM fields, OSINT sources, dev standards",
      "integrity": "optional"
    }
  },
  "references": {
    "directive:all-agents": {
      "path": "directives/all-agents.md",
      "category": "directives",
      "description": "Prime directives for all agents",
      "keywords": ["directive", "policy", "governance"],
      "sections": ["Identity", "Operating Rules", "Boundaries"],
      "content_hash": "sha256:abc123...",
      "updated": "2026-04-04"
    },
    "800-53:AC": {
      "path": "nist/800-53/AC.md",
      "category": "nist",
      "description": "Access Control family, 23 controls",
      "keywords": ["access control", "account management", "least privilege"],
      "sections": ["AC-1", "AC-2", "AC-3"],
      "content_hash": "sha256:def456...",
      "updated": "2026-04-04"
    }
  }
}
```


## 2B. Manifest Loader + Schema Validation (vaire/reference.py)

```
import json
import logging
import re
from pathlib import Path
from vaire.config import Settings

logger = logging.getLogger(__name__)

# Strict schema: only these keys allowed in manifest
_MANIFEST_REQUIRED_KEYS = {"schema_version", "categories", "references"}
_REF_REQUIRED_KEYS = {"path", "category", "description", "keywords", "content_hash", "updated"}
_REF_OPTIONAL_KEYS = {"sections"}
_CAT_REQUIRED_KEYS = {"description", "integrity"}
EXPECTED_SCHEMA_VERSION = 1


class ReferenceLoader:
    """Loads and serves static reference documents from repo-baked files.
    
    Security:
    - Topics are manifest keys only — no path construction from user input
    - All paths jail-checked against reference root
    - Directive files hash-verified on every load
    - Every directive load audit-logged with agent_id
    """
    
    def __init__(self, settings: Settings):
        self._root = settings.reference_path_resolved
        self._manifest_path = settings.reference_manifest_resolved
        self._manifest: dict = {}
        self._loaded = False
    
    def load_manifest(self) -> None:
        """Load and validate manifest from disk. Called once at startup."""
        if not self._manifest_path.exists():
            logger.warning("Reference manifest not found at %s", self._manifest_path)
            self._manifest = {"schema_version": 1, "categories": {}, "references": {}}
            self._loaded = True
            return
        
        raw = json.loads(self._manifest_path.read_text())
        self._validate_manifest(raw)
        self._verify_all_paths(raw)
        self._manifest = raw
        self._loaded = True
        logger.info(
            "Reference manifest loaded: %d references in %d categories",
            len(raw.get("references", {})),
            len(raw.get("categories", {})),
        )
    
    def _validate_manifest(self, raw: dict) -> None:
        """Strict schema validation. Rejects unknown keys."""
        # Top-level keys
        unknown_top = set(raw.keys()) - _MANIFEST_REQUIRED_KEYS
        if unknown_top:
            raise ValueError(f"Unknown manifest keys: {unknown_top}")
        for key in _MANIFEST_REQUIRED_KEYS:
            if key not in raw:
                raise ValueError(f"Missing manifest key: {key}")
        
        if raw["schema_version"] != EXPECTED_SCHEMA_VERSION:
            raise ValueError(f"Unexpected schema version: {raw['schema_version']}")
        
        # Category validation
        for cat_name, cat in raw["categories"].items():
            unknown = set(cat.keys()) - _CAT_REQUIRED_KEYS
            if unknown:
                raise ValueError(f"Unknown category keys in '{cat_name}': {unknown}")
            for key in _CAT_REQUIRED_KEYS:
                if key not in cat:
                    raise ValueError(f"Category '{cat_name}' missing key: {key}")
            if cat["integrity"] not in ("required", "optional"):
                raise ValueError(f"Category '{cat_name}' integrity must be 'required' or 'optional'")
        
        # Reference validation
        for ref_name, ref in raw["references"].items():
            allowed = _REF_REQUIRED_KEYS | _REF_OPTIONAL_KEYS
            unknown = set(ref.keys()) - allowed
            if unknown:
                raise ValueError(f"Unknown reference keys in '{ref_name}': {unknown}")
            for key in _REF_REQUIRED_KEYS:
                if key not in ref:
                    raise ValueError(f"Reference '{ref_name}' missing key: {key}")
            if ref["category"] not in raw["categories"]:
                raise ValueError(f"Reference '{ref_name}' has unknown category: {ref['category']}")
    
    def _verify_all_paths(self, raw: dict) -> None:
        """Verify every reference file exists and is within jail."""
        for ref_name, ref in raw["references"].items():
            target = self._root / ref["path"]
            verify_path_jail(target, self._root)  # raises PathSecurityError
            if not target.exists():
                raise FileNotFoundError(f"Reference file missing: {ref_name} -> {ref['path']}")
    
    def list_references(self, category: str | None = None) -> dict:
        """Return manifest index (topics, descriptions, categories).
        
        Does NOT return file content — just metadata for discovery.
        """
        refs = {}
        for topic, entry in self._manifest.get("references", {}).items():
            if category and entry["category"] != category:
                continue
            refs[topic] = {
                "description": entry["description"],
                "category": entry["category"],
                "keywords": entry["keywords"],
                "sections": entry.get("sections", []),
                "updated": entry["updated"],
            }
        return {
            "categories": {
                name: cat["description"]
                for name, cat in self._manifest.get("categories", {}).items()
                if not category or name == category
            },
            "references": refs,
        }
    
    def load(self, topic: str, section: str | None = None, agent_id: str = "") -> str:
        """Load reference content by topic key.
        
        Security flow:
        1. Topic must exist in manifest (no path construction from input)
        2. Resolve file path, jail-check against reference root
        3. If category integrity == "required": verify hash on every load
        4. If directive: audit log the access
        5. If section specified: extract section (header-to-next-header)
        """
        if not self._loaded:
            raise RuntimeError("Manifest not loaded — call load_manifest() first")
        
        entry = self._manifest.get("references", {}).get(topic)
        if entry is None:
            available = list(self._manifest.get("references", {}).keys())
            raise KeyError(
                f"Unknown reference topic: {topic!r}. "
                f"Use load_reference(list=True) to see available topics. "
                f"Available: {available[:10]}"
            )
        
        # Resolve and jail-check
        target = verify_path_jail(self._root / entry["path"], self._root)
        
        if not target.exists():
            raise FileNotFoundError(f"Reference file missing: {topic} -> {entry['path']}")
        
        # Integrity check: always for "required" categories, never for "optional"
        cat = self._manifest["categories"][entry["category"]]
        if cat["integrity"] == "required":
            if not verify_file_hash(target, entry["content_hash"]):
                logger.critical(
                    "INTEGRITY FAILURE: Reference %r hash mismatch (agent=%s)",
                    topic, agent_id,
                )
                raise PathSecurityError(f"Integrity check failed for {topic}")
        
        # Audit log for directives
        if topic.startswith("directive:"):
            logger.info("DIRECTIVE LOAD: topic=%r agent=%s", topic, agent_id)
        
        content = target.read_text()
        
        # Section extraction
        if section:
            content = self._extract_section(content, section)
        
        return content
    
    def _extract_section(self, content: str, section: str) -> str:
        """Extract content between ## section header and next ## header.
        
        Searches for a line starting with ## that contains the section string.
        Returns everything from that line to the next ## header (exclusive).
        """
        lines = content.split("\n")
        start_idx = None
        section_lower = section.lower()
        
        for i, line in enumerate(lines):
            if line.startswith("## ") and section_lower in line.lower():
                start_idx = i
                break
        
        if start_idx is None:
            raise KeyError(f"Section {section!r} not found in document")
        
        # Find next header at same or higher level
        end_idx = len(lines)
        for i in range(start_idx + 1, len(lines)):
            if lines[i].startswith("## ") or lines[i].startswith("# "):
                end_idx = i
                break
        
        return "\n".join(lines[start_idx:end_idx]).strip()
    
    def verify_health(self) -> dict:
        """Health check: verify all files exist and hashes match.
        
        Called by Vale wake cycle. Returns {topic: status} dict.
        """
        results = {}
        for topic, entry in self._manifest.get("references", {}).items():
            try:
                target = verify_path_jail(self._root / entry["path"], self._root)
                if not target.exists():
                    results[topic] = "MISSING"
                    continue
                if verify_file_hash(target, entry["content_hash"]):
                    results[topic] = "OK"
                else:
                    results[topic] = "HASH_MISMATCH"
            except Exception as e:
                results[topic] = f"ERROR: {e}"
        return results
    
    def check_staleness(self, max_age_days: int = 180) -> list[dict]:
        """Check for references older than max_age_days.
        
        Returns list of stale references for creator review.
        Called by Vale wake cycle — results flagged for creator approval.
        """
        from datetime import datetime, timedelta
        stale = []
        cutoff = datetime.now() - timedelta(days=max_age_days)
        
        for topic, entry in self._manifest.get("references", {}).items():
            try:
                updated = datetime.strptime(entry["updated"], "%Y-%m-%d")
                if updated < cutoff:
                    stale.append({
                        "topic": topic,
                        "category": entry["category"],
                        "last_updated": entry["updated"],
                        "age_days": (datetime.now() - updated).days,
                    })
            except (ValueError, KeyError):
                stale.append({
                    "topic": topic,
                    "category": entry.get("category", "unknown"),
                    "last_updated": "INVALID",
                    "age_days": -1,
                })
        return stale
```


## 2C. load_reference MCP Tool (vaire/server.py additions)

Add to server.py:

```
# Global reference loader — initialized in init_engines
_reference_loader: ReferenceLoader | None = None

@mcp_server.tool()
def load_reference(
    topic: str = "",
    section: str | None = None,
    list: bool = False,
    category: str | None = None,
) -> dict | str:
    """Load a reference document from the static reference library.
    
    Examples:
        load_reference(list=True)                    # list all available references
        load_reference(list=True, category="nist")   # list NIST references only
        load_reference(topic="800-53:AC")            # full AC family document
        load_reference(topic="800-53:AC", section="AC-17")  # just AC-17
        load_reference(topic="directive:all-agents") # prime directive (hash-verified)
    
    Returns document content or reference index.
    """
    if _reference_loader is None:
        return {"error": "Reference system not initialized"}
    
    if list:
        return _reference_loader.list_references(category=category)
    
    if not topic:
        return {"error": "Provide topic= or list=True"}
    
    # agent_id is injected by dispatch layer, not by caller
    # This function signature doesn't include it — dispatch wrapper adds it
    try:
        content = _reference_loader.load(topic, section=section)
        return {"topic": topic, "section": section, "content": content}
    except KeyError as e:
        return {"error": str(e)}
    except FileNotFoundError as e:
        return {"error": str(e)}
    except PathSecurityError as e:
        return {"error": "Access denied"}
```

NOTE: The dispatch wrapper needs to pass agent_id through to the loader for audit logging. Modified wrapper pattern:

```
# In build_dispatch_table, load_reference needs agent_id forwarded:
def _make_wrapper(fn):
    _needs_agent = fn.__name__ in {"load_reference"}
    if fn.__name__ in _executor_tools:
        async def handler(agent_id: str = "", **params):
            loop = asyncio.get_running_loop()
            if _needs_agent:
                params["_agent_id"] = agent_id
            return await loop.run_in_executor(None, lambda: fn(**params))
    else:
        async def handler(agent_id: str = "", **params):
            if _needs_agent:
                params["_agent_id"] = agent_id
            return fn(**params)
    handler.__name__ = fn.__name__
    return handler
```

Actually, cleaner approach: load_reference gets its own wrapper in the dispatch table (same as groomer tools), not the generic _make_wrapper:

```
# Add load_reference to the dispatch table with agent_id forwarding
async def _load_reference_handler(agent_id: str = "", **params):
    if _reference_loader is None:
        return {"error": "Reference system not initialized"}
    if params.get("list"):
        return _reference_loader.list_references(category=params.get("category"))
    topic = params.get("topic", "")
    if not topic:
        return {"error": "Provide topic= or list=True"}
    try:
        content = _reference_loader.load(topic, section=params.get("section"), agent_id=agent_id)
        return {"topic": topic, "section": params.get("section"), "content": content}
    except KeyError as e:
        return {"error": str(e)}
    except FileNotFoundError as e:
        return {"error": str(e)}
    except PathSecurityError as e:
        return {"error": "Access denied"}
```

Hmm, this duplicates logic. Better: keep the @mcp_server.tool() for the MCP interface, and have the dispatch handler call a separate internal function that accepts agent_id. Let's use the existing pattern — keep the MCP tool as-is, and in the dispatch table add it with the agent_id-aware wrapper.


## 2D. Remote Access Rules

load_reference is allowed for remote agents (read-only, immutable content):

```
# In the REMOTE_ALLOWED set (server.py, wherever remote permissions are checked):
REMOTE_ALLOWED_TOOLS |= {"load_reference"}
```


## 2E. init_engines Addition

```
# In init_engines(), after all existing initialization:
from vaire.reference import ReferenceLoader
global _reference_loader
_reference_loader = ReferenceLoader(_settings)
try:
    _reference_loader.load_manifest()
except Exception:
    logger.warning("Reference system unavailable — manifest load failed", exc_info=True)
    # Non-fatal: server runs without references
```


## 2F. Reference Directory Structure

```
vaire/reference/
    manifest.json
    directives/
        all-agents.md            # Prime directive — loaded by every agent on session start
    nist/
        800-53/
            AC.md                # Access Control
            AT.md                # Awareness and Training
            AU.md                # Audit and Accountability
            CA.md                # Assessment, Authorization, Monitoring
            CM.md                # Configuration Management
            CP.md                # Contingency Planning
            IA.md                # Identification and Authentication
            IR.md                # Incident Response
            MA.md                # Maintenance
            PE.md                # Physical and Environmental
            PL.md                # Planning
            PM.md                # Program Management
            PS.md                # Personnel Security
            PT.md                # PII Processing and Transparency
            RA.md                # Risk Assessment
            SA.md                # System and Services Acquisition
            SC.md                # System and Communications Protection
            SI.md                # System and Information Integrity
        800-53B-baselines.md
        csf-2.0-framework.md
        ai-100-2-aml-taxonomy.md
        sp-800-181-nice.md
    siem-field-inventory.md
    osint-sources.md
    dev-standards-reference.md
```


## 2G. Dockerfile Addition

After the existing `COPY vaire/ vaire/` line:

```
# Static reference library — immutable at runtime, versioned with code
COPY vaire/reference/ /app/reference/
```

This goes AFTER the vaire/ copy since reference/ is inside vaire/.
Wait — actually vaire/reference/ will be copied as part of `COPY vaire/ vaire/` already.
But we want it at /app/reference/, not /app/vaire/reference/.

Two options:
1. Separate COPY: `COPY vaire/reference/ /app/reference/` (explicit, clear intent)
2. Symlink: reference/ lives outside vaire/ package

Option 1 is cleaner — reference files aren't Python code, they're data.
But this means reference/ is copied TWICE (once as part of vaire/, once to /app/reference/).

Better: move reference/ to top level of repo (not inside vaire/ package):

```
reference/              # top-level, not inside Python package
    manifest.json
    directives/
    nist/
    ...
```

Dockerfile:
```
COPY reference/ /app/reference/
```

This avoids double-copy and makes clear that references are data, not code.
Config default: REFERENCE_PATH = "/app/reference" (matches container path).

DECISION: reference/ at repo root, not inside vaire/ package.
