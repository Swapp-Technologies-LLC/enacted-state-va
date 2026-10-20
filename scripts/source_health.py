#!/usr/bin/env python3
"""
source_health.py
Source Health Tracking & Auto-Managed GitHub Issues (ig02).
Swapp Technologies LLC

Responsibilities:
1. Maintains status/sources.json: last reached, last changed, consecutive failures.
2. Auto-manages issues for unreachable sources:
   - Opens issue on first failure
   - Comments on subsequent failures while unreachable
   - Closes issue on recovery
3. Pluggable IssueClient allows 100% offline unit testing with stubs.
"""

import os
import sys
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List


DEFAULT_STATUS_PATH = Path("status") / "sources.json"


def get_iso_now() -> str:
    """Returns current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_source_health(status_path: Optional[Path] = None) -> Dict[str, Any]:
    """Loads source health data from JSON, or returns a fresh structure if absent."""
    p = status_path or DEFAULT_STATUS_PATH
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "sources" in data:
                    return data
        except Exception as exc:
            print(f"[SourceHealth] Error reading {p}: {exc}", file=sys.stderr)

    return {
        "version": "1.0",
        "last_run": get_iso_now(),
        "sources": {}
    }


def save_source_health(health_data: Dict[str, Any], status_path: Optional[Path] = None):
    """Saves source health data atomically to JSON."""
    p = status_path or DEFAULT_STATUS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    temp_p = p.with_suffix(".tmp")
    with open(temp_p, "w", encoding="utf-8") as f:
        json.dump(health_data, f, indent=2, sort_keys=True)
        f.write("\n")
    shutil.move(str(temp_p), str(p))


def record_source_check(
    source_id: str,
    url: str,
    success: bool,
    changed: bool = False,
    error: str = "",
    health_data: Optional[Dict[str, Any]] = None,
    now_iso: Optional[str] = None
) -> Dict[str, Any]:
    """
    Updates status for a specific source:
    - On success: resets consecutive_failures to 0, updates last_reached. If changed, updates last_changed.
    - On failure: increments consecutive_failures, updates status with error detail.
    """
    now = now_iso or get_iso_now()
    if health_data is None:
        health_data = {
            "version": "1.0",
            "last_run": now,
            "sources": {}
        }

    sources = health_data.setdefault("sources", {})
    entry = sources.get(source_id, {
        "source_id": source_id,
        "url": url,
        "last_reached": None,
        "last_changed": None,
        "consecutive_failures": 0,
        "status": "pending"
    })

    entry["url"] = url
    if success:
        entry["last_reached"] = now
        entry["consecutive_failures"] = 0
        entry["status"] = "ok"
        if changed or entry.get("last_changed") is None:
            entry["last_changed"] = now
    else:
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
        entry["status"] = f"unreachable: {error}" if error else "unreachable"

    sources[source_id] = entry
    health_data["last_run"] = now
    return entry


# ---------------------------------------------------------------------------
# Auto-managed Issue Client & Logic
# ---------------------------------------------------------------------------

class BaseIssueClient:
    """Abstract interface for managing source alert issues."""
    def list_open_issues(self, label: str = "source-alert") -> List[Dict[str, Any]]:
        raise NotImplementedError

    def create_issue(self, title: str, body: str, labels: List[str]) -> int:
        raise NotImplementedError

    def comment_issue(self, issue_number: int, body: str):
        raise NotImplementedError

    def close_issue(self, issue_number: int, comment: Optional[str] = None):
        raise NotImplementedError


class StubIssueClient(BaseIssueClient):
    """In-memory stub issue client for deterministic testing."""
    def __init__(self, initial_issues: Optional[List[Dict[str, Any]]] = None):
        self.issues: List[Dict[str, Any]] = initial_issues or []
        self.next_id = max([it.get("number", 0) for it in self.issues], default=0) + 1
        self.comments: List[Dict[str, Any]] = []

    def list_open_issues(self, label: str = "source-alert") -> List[Dict[str, Any]]:
        return [
            it for it in self.issues
            if it.get("state") == "open" and label in it.get("labels", [])
        ]

    def create_issue(self, title: str, body: str, labels: List[str]) -> int:
        num = self.next_id
        self.next_id += 1
        self.issues.append({
            "number": num,
            "title": title,
            "body": body,
            "labels": labels,
            "state": "open"
        })
        return num

    def comment_issue(self, issue_number: int, body: str):
        self.comments.append({"issue_number": issue_number, "body": body})

    def close_issue(self, issue_number: int, comment: Optional[str] = None):
        if comment:
            self.comment_issue(issue_number, comment)
        for it in self.issues:
            if it["number"] == issue_number:
                it["state"] = "closed"
                break


class GitHubCliIssueClient(BaseIssueClient):
    """Production issue client using GitHub CLI (`gh`)."""
    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")

    def _env(self) -> Dict[str, str]:
        env = os.environ.copy()
        if self.token:
            env["GH_TOKEN"] = self.token
            env["GITHUB_TOKEN"] = self.token
        return env

    def list_open_issues(self, label: str = "source-alert") -> List[Dict[str, Any]]:
        cmd = ["gh", "issue", "list", "--state", "open", "--label", label, "--json", "number,title,labels"]
        try:
            res = subprocess.run(cmd, env=self._env(), capture_output=True, text=True)
            if res.returncode != 0:
                return []
            return json.loads(res.stdout)
        except Exception:
            return []

    def create_issue(self, title: str, body: str, labels: List[str]) -> int:
        cmd = ["gh", "issue", "create", "--title", title, "--body", body]
        for lb in labels:
            cmd.extend(["--label", lb])
        try:
            res = subprocess.run(cmd, env=self._env(), capture_output=True, text=True)
            if res.returncode == 0:
                # gh output is URL: e.g. https://github.com/org/repo/issues/123
                m = res.stdout.strip().split("/")[-1]
                try:
                    return int(m)
                except ValueError:
                    return 0
        except Exception:
            return 0
        return 0

    def comment_issue(self, issue_number: int, body: str):
        cmd = ["gh", "issue", "comment", str(issue_number), "--body", body]
        try:
            subprocess.run(cmd, env=self._env(), capture_output=True, text=True)
        except Exception:
            pass

    def close_issue(self, issue_number: int, comment: Optional[str] = None):
        if comment:
            self.comment_issue(issue_number, comment)
        cmd = ["gh", "issue", "close", str(issue_number)]
        try:
            subprocess.run(cmd, env=self._env(), capture_output=True, text=True)
        except Exception:
            pass


def manage_source_alert_issues(
    health_data: Dict[str, Any],
    issue_client: Optional[BaseIssueClient] = None
) -> List[Dict[str, Any]]:
    """
    Evaluates source health data against open issues:
    - If consecutive_failures > 0: opens alert issue if not present; comments if already open.
    - If consecutive_failures == 0: closes alert issue if open.
    Returns audit action list.
    """
    if issue_client is None:
        if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
            issue_client = GitHubCliIssueClient()
        else:
            return []

    open_issues = issue_client.list_open_issues(label="source-alert")
    actions = []

    # Map existing open issues by source_id
    # Format of title: "[Source Alert] {source_id} unreachable"
    issue_by_source = {}
    for issue in open_issues:
        title = issue.get("title", "")
        if title.startswith("[Source Alert] "):
            parts = title[len("[Source Alert] "):].split()
            if parts:
                sid = parts[0]
                issue_by_source[sid] = issue

    sources = health_data.get("sources", {})
    now = health_data.get("last_run", get_iso_now())

    for source_id, info in sources.items():
        failures = info.get("consecutive_failures", 0)
        url = info.get("url", "")
        status_msg = info.get("status", "")

        existing_issue = issue_by_source.get(source_id)

        if failures > 0:
            if existing_issue is None:
                title = f"[Source Alert] {source_id} unreachable"
                body = (
                    f"### Automated Ingestion Source Health Alert\n\n"
                    f"- **Source ID:** `{source_id}`\n"
                    f"- **URL:** {url}\n"
                    f"- **Status:** `{status_msg}`\n"
                    f"- **Consecutive Failures:** {failures}\n"
                    f"- **First Detected:** {now}\n\n"
                    f"This source failed its reachability or extraction check. "
                    f"Per Rule 10, no model was called and no fields were inferred. "
                    f"This issue is auto-managed and will close automatically when the source recovers."
                )
                num = issue_client.create_issue(title, body, labels=["source-alert", "ingestion-failure"])
                actions.append({"action": "opened", "source_id": source_id, "issue_number": num})
            else:
                num = existing_issue["number"]
                comment_body = (
                    f"**Source Check Update ({now}):** Check failed again. "
                    f"Consecutive failures: {failures}. Status: `{status_msg}`."
                )
                issue_client.comment_issue(num, comment_body)
                actions.append({"action": "commented", "source_id": source_id, "issue_number": num})

        elif failures == 0:
            if existing_issue is not None:
                num = existing_issue["number"]
                close_comment = (
                    f"**Source Recovered ({now}):** Source `{source_id}` successfully reached and verified. "
                    f"Closing auto-managed alert issue."
                )
                issue_client.close_issue(num, comment=close_comment)
                actions.append({"action": "closed", "source_id": source_id, "issue_number": num})

    return actions
