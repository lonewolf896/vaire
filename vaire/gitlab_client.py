"""Minimal GitLab Repository Files API client.

Security:
- Token never appears in __repr__, logs, or error messages
- TLS verification always enabled (verify=True)
- Only interacts with a single known file in a single project
"""

import base64
import logging
from urllib.parse import quote as url_quote

import httpx

logger = logging.getLogger(__name__)


class GitLabError(Exception):
    """Base GitLab API error. Never includes token in message."""


class GitLabConflictError(GitLabError):
    """409 Conflict — optimistic lock failure."""


class GitLabClient:
    """Token-safe, TLS-verified GitLab file API wrapper."""

    def __init__(self, api_url: str, project_id: str, token: str):
        self._api_url = api_url.rstrip("/")
        self._project_id = project_id
        self._token = token  # PRIVATE — never serialized
        self._client = httpx.Client(
            timeout=10.0,
            verify=True,  # ALWAYS — no override
            headers={"PRIVATE-TOKEN": self._token},
        )

    def __repr__(self) -> str:
        return (
            f"GitLabClient(api_url={self._api_url!r}, "
            f"project_id={self._project_id!r}, token=***)"
        )

    def _file_url(self, file_path: str) -> str:
        # Full URL-encode the file path (slashes, spaces, etc.)
        encoded = url_quote(file_path, safe="")
        return (
            f"{self._api_url}/projects/{self._project_id}"
            f"/repository/files/{encoded}"
        )

    def read_file(
        self, file_path: str, branch: str = "main"
    ) -> tuple[str, str]:
        """Read a file from GitLab.

        Returns: (content_string, last_commit_id)
        Raises: FileNotFoundError on 404, GitLabError on other failures
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

    def write_file(
        self,
        file_path: str,
        content: str,
        commit_message: str,
        branch: str = "main",
        last_commit_id: str | None = None,
    ) -> str:
        """Write a file to GitLab (create or update).

        Args:
            last_commit_id: If provided, enables optimistic locking.
                           409 Conflict if file was modified since this commit.

        Returns: new commit_id
        Raises: GitLabConflictError on 409, GitLabError on other failures
        """
        url = self._file_url(file_path)
        body: dict = {
            "branch": branch,
            "content": content,
            "commit_message": commit_message,
        }
        if last_commit_id:
            body["last_commit_id"] = last_commit_id

        # Try PUT (update) first, fall back to POST (create)
        resp = self._client.put(url, json=body)
        if resp.status_code == 404:
            resp = self._client.post(url, json=body)

        if resp.status_code == 409:
            raise GitLabConflictError(
                "Optimistic lock conflict — file modified since last read"
            )
        if resp.status_code not in (200, 201):
            raise GitLabError(
                f"GitLab write failed: HTTP {resp.status_code}"
            )

        return resp.json().get("commit_id", "")

    def close(self) -> None:
        self._client.close()
