# TASK-044 Phase 1: Config + GitLab Client + Path Security

## 1A. Config additions (vaire/config.py)

Add to Settings class after the existing security limits section:

```
# ── Reference system ─────────────────────────────────────────────────
REFERENCE_PATH: str = "/app/reference"       # container path (baked into image)
REFERENCE_MANIFEST: str = "manifest.json"    # filename within REFERENCE_PATH

# ── GitLab task sync ─────────────────────────────────────────────────
GITLAB_API_URL: str = ""                     # e.g. "https://gitlab.example.com/api/v4"
GITLAB_PROJECT_ID: str = ""                  # e.g. "42"
GITLAB_TOKEN: str = ""                       # project access token — env var ONLY
GITLAB_TASKS_FILE: str = "tasks.json"        # path within repo
GITLAB_TASKS_BRANCH: str = "main"
TASK_SYNC_INTERVAL: int = 30                 # seconds between GitLab syncs
TASK_HEARTBEAT_TTL: int = 30                 # minutes before task considered abandoned
TASK_DATA_PATH: str = "/data/tasks.json"     # runtime writable task file
TASK_PENDING_LOG: str = "/data/tasks-pending.jsonl"  # offline mutation queue
TASK_CREATE_ALLOWED: str = "groomer-,creator" # comma-separated agent_id prefixes allowed to create tasks
```

Add validator:
```
@model_validator(mode="after")
def _validate_gitlab_settings(self) -> "Settings":
    # If any GitLab field is set, API_URL and PROJECT_ID are required
    # TOKEN is required but only validated at runtime (may come from env)
    gitlab_fields = [self.GITLAB_API_URL, self.GITLAB_PROJECT_ID]
    if any(gitlab_fields) and not all(gitlab_fields):
        raise ValueError("GITLAB_API_URL and GITLAB_PROJECT_ID must both be set")
    return self
```

Add properties:
```
@property
def reference_path_resolved(self) -> Path:
    return Path(self.REFERENCE_PATH)

@property
def reference_manifest_resolved(self) -> Path:
    return self.reference_path_resolved / self.REFERENCE_MANIFEST

@property
def task_data_path_resolved(self) -> Path:
    return Path(self.TASK_DATA_PATH).expanduser()

@property
def task_pending_log_resolved(self) -> Path:
    return Path(self.TASK_PENDING_LOG).expanduser()

@property
def gitlab_enabled(self) -> bool:
    return bool(self.GITLAB_API_URL and self.GITLAB_PROJECT_ID and self.GITLAB_TOKEN)

@property
def task_create_allowed_list(self) -> list[str]:
    return [p.strip() for p in self.TASK_CREATE_ALLOWED.split(",") if p.strip()]
```


## 1B. GitLab Client (vaire/gitlab_client.py)

NEW FILE. Token-safe, TLS-verified, never-log-token.

```
import hashlib
import httpx
import json
import base64
import logging

logger = logging.getLogger(__name__)

class GitLabClient:
    """Minimal GitLab Repository Files API client.
    
    Security:
    - Token never appears in __repr__, logs, or error messages
    - TLS verification always enabled (verify=True)
    - Only interacts with a single known file in a single project
    """
    
    def __init__(self, api_url: str, project_id: str, token: str):
        self._api_url = api_url.rstrip("/")
        self._project_id = project_id
        self._token = token          # PRIVATE — never serialized
        self._client = httpx.Client(
            timeout=10.0,
            verify=True,             # ALWAYS — no override
            headers={"PRIVATE-TOKEN": self._token},
        )
    
    def __repr__(self):
        return f"GitLabClient(api_url={self._api_url!r}, project_id={self._project_id!r}, token=***)"
    
    def _file_url(self, file_path: str) -> str:
        # URL-encode the file path (slashes become %2F)
        encoded = file_path.replace("/", "%2F")
        return f"{self._api_url}/projects/{self._project_id}/repository/files/{encoded}"
    
    def read_file(self, file_path: str, branch: str = "main") -> tuple[str, str]:
        """Read a file from GitLab.
        
        Returns: (content_string, last_commit_id)
        Raises: GitLabError on failure
        """
        url = self._file_url(file_path)
        resp = self._client.get(url, params={"ref": branch})
        
        if resp.status_code == 404:
            raise FileNotFoundError(f"GitLab file not found: {file_path}")
        if resp.status_code != 200:
            # NEVER include response body (might echo token in error)
            raise GitLabError(f"GitLab GET failed: HTTP {resp.status_code}")
        
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["last_commit_id"]
    
    def write_file(self, file_path: str, content: str, commit_message: str,
                   branch: str = "main", last_commit_id: str | None = None) -> str:
        """Write a file to GitLab (create or update).
        
        Args:
            last_commit_id: If provided, enables optimistic locking.
                           409 Conflict if file was modified since this commit.
        
        Returns: new commit_id
        Raises: GitLabConflictError on 409, GitLabError on other failures
        """
        url = self._file_url(file_path)
        body = {
            "branch": branch,
            "content": content,
            "commit_message": commit_message,
        }
        if last_commit_id:
            body["last_commit_id"] = last_commit_id
        
        # Try PUT (update) first, fall back to POST (create)
        resp = self._client.put(url, json=body)
        if resp.status_code == 404:
            # File doesn't exist yet — create it
            resp = self._client.post(url, json=body)
        
        if resp.status_code == 409:
            raise GitLabConflictError("Optimistic lock conflict — file modified since last read")
        if resp.status_code not in (200, 201):
            raise GitLabError(f"GitLab write failed: HTTP {resp.status_code}")
        
        return resp.json().get("commit_id", "")
    
    def close(self):
        self._client.close()


class GitLabError(Exception):
    """Base GitLab API error. Never includes token in message."""
    pass

class GitLabConflictError(GitLabError):
    """409 Conflict — optimistic lock failure."""
    pass
```

Security notes:
- httpx.Client(verify=True) — no parameter to disable
- Token only in headers, never in URL params
- Error messages never include response body (could echo token)
- __repr__ masks token
- No token in commit messages


## 1C. Path Security Utilities

These go into vaire/reference.py (Phase 2 file) but are designed here.

```
import hashlib
from pathlib import Path

class PathSecurityError(Exception):
    """Raised on path traversal or integrity failure."""
    pass


def verify_path_jail(target: Path, jail: Path) -> Path:
    """Ensure resolved target is within jail directory.
    
    Args:
        target: Path to verify (will be resolved)
        jail: Root directory that target must be within
    
    Returns: resolved target path
    Raises: PathSecurityError if target escapes jail
    """
    resolved = target.resolve()
    jail_resolved = jail.resolve()
    
    if not resolved.is_relative_to(jail_resolved):
        # Log the attempt but DON'T log the actual path (could be probing)
        raise PathSecurityError("Path traversal blocked")
    
    return resolved


def verify_file_hash(path: Path, expected_hash: str) -> bool:
    """Verify SHA256 hash of file content.
    
    Args:
        path: File to hash
        expected_hash: Expected hash in format "sha256:<hex>"
    
    Returns: True if match
    Raises: PathSecurityError if mismatch (for integrity=required files)
    """
    if not expected_hash.startswith("sha256:"):
        raise ValueError(f"Unsupported hash format: {expected_hash}")
    
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = expected_hash[7:]  # strip "sha256:" prefix
    return actual == expected


def compute_file_hash(path: Path) -> str:
    """Compute SHA256 hash of file. Returns 'sha256:<hex>'."""
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
```
