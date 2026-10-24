"""One JSON file on a branch of this repo, read and written through GitHub's contents API.

The app's Alerts tool saves the alert settings this way and the alert bot reads them back every
minute. Writing needs a token with write access to the repo's contents (the app's GH_TOKEN, the
same one the Bettors stars use); reading works with the Actions GITHUB_TOKEN, or with no token
from raw.githubusercontent.com, which GitHub caches for a few minutes.
"""

from __future__ import annotations

import base64
import json
import os
import threading

import requests

REPO = os.environ.get("GITHUB_REPOSITORY") or "capypullup/kalshi-combo-tracker"


class GitHubFile:
    def __init__(self, path: str, branch: str, token: str | None = None, repo: str = REPO, session=None):
        self.path, self.branch, self.token = path, branch, token
        self.api = f"https://api.github.com/repos/{repo}"
        self.raw = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        self.session = session or requests.Session()
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                         "X-GitHub-Api-Version": "2022-11-28"})
        self.lock = threading.Lock()

    def read(self) -> tuple[dict | None, str | None]:
        """The file's JSON and its sha (None without a token), or (None, None) when the branch or file
        isn't there yet."""
        if not self.token:
            resp = self.session.get(self.raw, timeout=20)
            if resp.status_code == 404:
                return None, None
            resp.raise_for_status()
            return resp.json(), None
        resp = self.session.get(f"{self.api}/contents/{self.path}", params={"ref": self.branch}, timeout=20)
        if resp.status_code == 404:
            return None, None
        resp.raise_for_status()
        body = resp.json()
        return json.loads(base64.b64decode(body.get("content") or "") or b"null"), body.get("sha")

    def _ensure_branch(self) -> None:
        """Start the branch from the default branch's head the first time anything is saved."""
        if self.session.get(f"{self.api}/git/ref/heads/{self.branch}", timeout=20).status_code == 200:
            return
        resp = self.session.get(self.api, timeout=20)
        resp.raise_for_status()
        resp = self.session.get(f"{self.api}/git/ref/heads/{resp.json()['default_branch']}", timeout=20)
        resp.raise_for_status()
        resp = self.session.post(f"{self.api}/git/refs", json={"ref": f"refs/heads/{self.branch}",
                                                               "sha": resp.json()["object"]["sha"]}, timeout=20)
        if resp.status_code not in (201, 422):  # 422: someone just made it
            resp.raise_for_status()

    def write(self, data: dict, message: str) -> None:
        """Save `data` as the file (whatever is there now: the last save wins)."""
        if not self.token:
            raise RuntimeError("saving needs a GitHub token")
        content = base64.b64encode(json.dumps(data, indent=1, sort_keys=True).encode()).decode()
        with self.lock:
            for attempt in range(2):
                _, sha = self.read()
                if sha is None:
                    self._ensure_branch()
                payload = {"message": message, "branch": self.branch, "content": content}
                if sha:
                    payload["sha"] = sha
                resp = self.session.put(f"{self.api}/contents/{self.path}", json=payload, timeout=20)
                if resp.status_code in (409, 422) and attempt == 0:  # saved meanwhile somewhere else: take its sha
                    continue
                resp.raise_for_status()
                return


class MemoryFile:
    """A GitHubFile stand-in that keeps the file in memory (the demo app and tests)."""

    def __init__(self, data: dict | None = None):
        self.data, self.saves = data, 0

    def read(self) -> tuple[dict | None, str | None]:
        return (json.loads(json.dumps(self.data)) if self.data is not None else None), (str(self.saves) if self.saves else None)

    def write(self, data: dict, message: str) -> None:
        self.data, self.saves = json.loads(json.dumps(data)), self.saves + 1
