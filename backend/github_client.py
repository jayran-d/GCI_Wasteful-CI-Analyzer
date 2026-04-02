"""
GitHub API client for GCI.
Handles authentication, pagination, rate limiting, and common queries
against the GitHub Actions REST API.
"""

import time
import re
import io
import zipfile
import requests
from datetime import datetime, timezone


# Common error patterns to look for in job logs
LOG_ERROR_PATTERNS = [
    re.compile(r"Error: .+", re.IGNORECASE),
    re.compile(r"fatal: .+", re.IGNORECASE),
    re.compile(r"##\[error\].+"),
    re.compile(r"Process completed with exit code [1-9]\d*"),
    re.compile(r"The operation was canceled\."),
    re.compile(r"Canceling since .+ failed"),
    re.compile(r"A]rtifact .+ not found", re.IGNORECASE),
    re.compile(r"RequestError \[HttpError\]:.+"),
    re.compile(r"Cannot download .+"),
]


class GitHubClient:
    """Thin wrapper around the GitHub REST API v3 for Actions data."""

    BASE = "https://api.github.com"

    def __init__(self, token: str | None = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self.authenticated = token is not None
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.rate_remaining = None
        self.rate_reset = None
        self.rate_limit = 60  # default unauthenticated
        self.on_status = None
        self._etag_cache = {}  # url -> (etag, response_json)


    #helper functs
    def _respect_rate_limit(self):
        if self.rate_remaining is not None and self.rate_remaining < 5:
            sleep_for = max(0, (self.rate_reset or 0) - time.time()) + 1
            if sleep_for > 1:
                if self.on_status:
                    self.on_status(f"⏳ Rate limit: {self.rate_remaining} left, waiting {int(sleep_for)}s...")
                sleep_for = min(sleep_for, 30)
            time.sleep(sleep_for)

    def has_budget(self, needed: int = 1) -> bool:
        """Check if we have enough rate limit budget for N calls."""
        if self.rate_remaining is None:
            return True
        return self.rate_remaining > needed

    def _request(self, method: str, path: str, **kwargs):
        self._respect_rate_limit()
        kwargs.setdefault("timeout", 15)
        url = path if path.startswith("http") else f"{self.BASE}{path}"
        try:
            resp = self.session.request(method, url, **kwargs)
        except requests.exceptions.Timeout:
            if self.on_status:
                self.on_status(f"Timed out: {url[:80]}")
            raise
        except requests.exceptions.ConnectionError:
            if self.on_status:
                self.on_status(f"Connection failed: {url[:80]}")
            raise

        # Only update rate tracking if GitHub returned rate headers (redirects to S3 won't have them)
        if "X-RateLimit-Remaining" in resp.headers:
            self.rate_remaining = int(resp.headers["X-RateLimit-Remaining"])
            self.rate_reset = int(resp.headers.get("X-RateLimit-Reset", 0))
            self.rate_limit = int(resp.headers.get("X-RateLimit-Limit", self.rate_limit))

        if resp.status_code == 403 and self.rate_remaining == 0:
            reset_at = datetime.fromtimestamp(self.rate_reset, tz=timezone.utc)
            raise RuntimeError(
                f"GitHub API rate limit exceeded ({self.rate_limit}/hr). "
                f"Resets at {reset_at.strftime('%H:%M:%S UTC')}. Use a token for 5000/hr."
            )
        if resp.status_code == 410:
            return resp  # caller handles expired resources
        resp.raise_for_status()
        return resp

    def _get_json(self, path: str, params: dict | None = None):
        """GET with ETag caching i.e. 304 responses don't count against rate limit."""
        url = path if path.startswith("http") else f"{self.BASE}{path}"
        # Build cache key from url + sorted params
        cache_key = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items())) if params else "")

        headers = {}
        if cache_key in self._etag_cache:
            headers["If-None-Match"] = self._etag_cache[cache_key][0]

        self._respect_rate_limit()
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=15)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if self.on_status:
                self.on_status(f"{type(exc).__name__}: {url[:60]}")
            raise

        if "X-RateLimit-Remaining" in resp.headers:
            self.rate_remaining = int(resp.headers["X-RateLimit-Remaining"])
            self.rate_reset = int(resp.headers.get("X-RateLimit-Reset", 0))
            self.rate_limit = int(resp.headers.get("X-RateLimit-Limit", self.rate_limit))

        if resp.status_code == 304 and cache_key in self._etag_cache:
            return self._etag_cache[cache_key][1]  # return cached, didn't cost rate limit

        if resp.status_code == 403 and self.rate_remaining == 0:
            reset_at = datetime.fromtimestamp(self.rate_reset, tz=timezone.utc)
            raise RuntimeError(
                f"Rate limit exceeded ({self.rate_limit}/hr). Resets {reset_at.strftime('%H:%M:%S UTC')}."
            )
        resp.raise_for_status()

        data = resp.json()
        etag = resp.headers.get("ETag")
        if etag:
            self._etag_cache[cache_key] = (etag, data)
        return data

    def _paginate(self, path: str, params: dict | None = None, max_pages: int = 10):
        """Yield items across paginated responses."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        for page in range(1, max_pages + 1):
            params["page"] = page
            data = self._get_json(path, params)
            #FIX: GitHub wraps some endpoints in an object with a list value
            if isinstance(data, dict):
                # Find the list key (workflow_runs, jobs, workflows, etc.)
                for v in data.values():
                    if isinstance(v, list):
                        yield from v
                        if len(v) < params["per_page"]:
                            return
                        break
                else:
                    return
            elif isinstance(data, list):
                yield from data
                if len(data) < params["per_page"]:
                    return
            else:
                return


    @staticmethod
    def parse_repo(url_or_slug: str) -> tuple[str, str]:
        """Return (owner, repo) from a GitHub URL or 'owner/repo' slug."""
        url_or_slug = url_or_slug.strip().rstrip("/")
        m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url_or_slug)
        if m:
            return m.group(1), m.group(2)
        parts = url_or_slug.split("/")
        if len(parts) == 2:
            return parts[0], parts[1]
        raise ValueError(f"Cannot parse GitHub repo from: {url_or_slug}")


    def get_workflow_runs(
        self,
        owner: str,
        repo: str,
        created: str | None = None,
        status: str | None = None,
        event: str | None = None,
        max_pages: int = 10,
    ) -> list[dict]:
        """Return workflow runs, optionally filtered by date range / status."""
        params: dict = {}
        if created:
            params["created"] = created
        if status:
            params["status"] = status
        if event:
            params["event"] = event
        return list(
            self._paginate(f"/repos/{owner}/{repo}/actions/runs", params, max_pages)
        )

    def get_workflow_runs_paged(
    self,
    owner: str,
    repo: str,
    created: str | None = None,
    max_pages: int | None = None,
    ):
        """Yield (page_number, page_runs, total_count) per page for streaming."""
        if max_pages is not None:
            max_runs = max_pages * 100
        params: dict = {"per_page": 100}
        if created:
            params["created"] = created
        path = f"/repos/{owner}/{repo}/actions/runs"
        collected = 0
        pages_needed = (max_runs + 99) // 100  # ceil division
        for page in range(1, pages_needed + 1):
            params["page"] = page
            data = self._get_json(path, params)
            total_count = data.get("total_count", 0)
            page_runs = data.get("workflow_runs", [])
            remaining = max_runs - collected
            if len(page_runs) > remaining:
                page_runs = page_runs[:remaining]
            collected += len(page_runs)
            yield page, page_runs, total_count
            if collected >= max_runs or len(page_runs) < 100:
                break

    def get_runs_for_workflow(
        self, owner: str, repo: str, workflow_id: int, max_pages: int = 5
    ) -> list[dict]:
        """Return runs for a specific workflow by ID."""
        return list(
            self._paginate(
                f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
                max_pages=max_pages,
            )
        )

    def get_jobs_for_run(self, owner: str, repo: str, run_id: int) -> list[dict]:
        """Return jobs (with steps) for a specific workflow run."""
        return list(
            self._paginate(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                max_pages=5,
            )
        )

    def get_workflows(self, owner: str, repo: str) -> list[dict]:
        """Return the list of workflow definitions."""
        return list(self._paginate(f"/repos/{owner}/{repo}/actions/workflows", max_pages=3))

    def get_workflow_file(self, owner: str, repo: str, path: str) -> str | None:
        """Return the raw content of a workflow YAML file from the default branch."""
        try:
            data = self._get_json(f"/repos/{owner}/{repo}/contents/{path}")
            print(f"Fetched workflow file {path} (size {data.get('size', 0)} bytes)")
            if data.get("encoding") == "base64":
                import base64
                return base64.b64decode(data["content"]).decode()
            return data.get("content")
        except Exception:
            print(f"Failed to fetch workflow file {path} for {owner}/{repo}")
            return None

    def find_parent_runs_by_sha(
        self,
        owner: str,
        repo: str,
        head_sha: str,
        parent_workflow_names: list[str],
    ) -> list[dict]:
        """
        Find workflow runs that match a given head SHA and belong to one of
        the named parent workflows.  Returns a list of matching run dicts
        (usually 0 or 1), sorted most-recent first.
        """
        if not head_sha or not parent_workflow_names:
            return []

        # The runs endpoint supports filtering by head_sha directly
        try:
            runs = list(
                self._paginate(
                    f"/repos/{owner}/{repo}/actions/runs",
                    params={"head_sha": head_sha},
                    max_pages=2,
                )
            )
        except Exception:
            return []

        # Keep only runs whose workflow name is one of the expected parents
        parent_name_set = set(parent_workflow_names)
        matched = [r for r in runs if r.get("name") in parent_name_set]

        # Most recent first (by created_at descending)
        matched.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return matched

    def get_job_log_snippet(
        self,
        owner: str,
        repo: str,
        job_id: int,
        tail_lines: int = 80,
    ) -> dict | None:
        """
        Download the log for a single job and return a dict with:
          - matched_patterns: list of pattern-matched error lines
          - log_preview: last *tail_lines* lines of the log
        Returns None if the log cannot be retrieved.
        """
        try:
            resp = self._request(
                "GET",
                f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                allow_redirects=True,
                timeout=10,
            )
            if resp.status_code == 410:
                return None
            log_text = resp.text
        except Exception:
            return None

        lines = log_text.splitlines()
        preview = "\n".join(lines[-tail_lines:]) if len(lines) > tail_lines else log_text

        matched = []
        for line in lines:
            for pattern in LOG_ERROR_PATTERNS:
                if pattern.search(line):
                    # Strip ANSI escape codes and timestamp prefixes for readability
                    clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
                    clean = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.Z]+\s*", "", clean)
                    matched.append(clean)
                    break  # one match per line is enough

        return {
            "matched_patterns": matched,
            "log_preview": preview,
        }

    def download_run_logs(self, owner: str, repo: str, run_id: int) -> dict[str, str]:
        """Download and extract run logs. Returns {filename: log_text}."""
        try:
            resp = self._request(
                "GET",
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
                allow_redirects=True,
                timeout=10,
            )
            if resp.status_code == 410:
                return {}  # logs expired?
            logs = {}
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                for name in zf.namelist():
                    try:
                        logs[name] = zf.read(name).decode("utf-8", errors="replace")
                    except Exception:
                        pass
            return logs
        except Exception:
            return {}

    def get_commit_files(self, owner: str, repo: str, sha: str) -> list[str]:
        """Return list of changed file paths for a commit."""
        try:
            data = self._get_json(f"/repos/{owner}/{repo}/commits/{sha}")
            return [f["filename"] for f in data.get("files", [])]
        except Exception:
            return []

    def get_rate_limit(self) -> dict:
        return self._get_json("/rate_limit")