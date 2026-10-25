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


def has_meaningful_change(baseline_data: Dict[str, Any], current_data: Dict[str, Any]) -> bool:
    """
    Checks if a meaningful change occurred between baseline and current health data:
    - Status flipped (e.g. ok <-> unreachable)
    - last_changed timestamp moved
    - Sources added or removed
    Timestamp-only changes (last_run, last_reached) do NOT count as meaningful.
    """
    b_sources = baseline_data.get("sources", {}) if isinstance(baseline_data, dict) else {}
    c_sources = current_data.get("sources", {}) if isinstance(current_data, dict) else {}

    if set(b_sources.keys()) != set(c_sources.keys()):
        return True

    for sid, c_info in c_sources.items():
        b_info = b_sources.get(sid, {})
        # Status flip (ok vs unreachable / error change)
        b_status = b_info.get("status", "")
        c_status = c_info.get("status", "")
        if b_status != c_status:
            return True

        # last_changed moved
        if b_info.get("last_changed") != c_info.get("last_changed"):
            return True

    return False


def save_source_health(
    health_data: Dict[str, Any],
    status_path: Optional[Path] = None,
    only_if_meaningful: bool = True,
    step_summary_path: Optional[Path] = None,
    artifact_path: Optional[Path] = None
) -> bool:
    """
    Saves source health data atomically to status/sources.json only if meaningful changes occurred.
    Always writes full current health (with timestamps) to artifact_path (sources-health.json)
    and appends a markdown table to step_summary_path ($GITHUB_STEP_SUMMARY).
    Returns True if status_path was updated, False if skipped due to no meaningful changes.
    """
    p = status_path or DEFAULT_STATUS_PATH
    repo_root = p.parent.parent if p.parent.name == "status" else Path(".")

    # 1. Determine if we should update on-disk status/sources.json
    should_save = True
    if only_if_meaningful and p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            should_save = has_meaningful_change(disk_data, health_data)
        except Exception:
            should_save = True

    if should_save:
        p.parent.mkdir(parents=True, exist_ok=True)
        temp_p = p.with_suffix(".tmp")
        with open(temp_p, "w", encoding="utf-8") as f:
            json.dump(health_data, f, indent=2, sort_keys=True)
            f.write("\n")
        shutil.move(str(temp_p), str(p))

    # 2. Always write full health artifact (sources-health.json)
    art_path = artifact_path or (repo_root / "sources-health.json")
    try:
        art_path.parent.mkdir(parents=True, exist_ok=True)
        temp_art = art_path.with_suffix(".tmp")
        with open(temp_art, "w", encoding="utf-8") as af:
            json.dump(health_data, af, indent=2, sort_keys=True)
            af.write("\n")
        shutil.move(str(temp_art), str(art_path))
    except Exception as exc:
        print(f"[SourceHealth] Error writing artifact {art_path}: {exc}", file=sys.stderr)

    # 3. Always write to GITHUB_STEP_SUMMARY if available
    summary_target = step_summary_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_target:
        try:
            summary_p = Path(summary_target)
            now_str = health_data.get("last_run", get_iso_now())
            lines = [
                "",
                "### Ingestion Source Health Run Summary",
                f"- **Evaluated At (UTC):** `{now_str}`",
                "",
                "| Source ID | Status | Consecutive Failures | Last Reached | Last Changed |",
                "| :--- | :--- | :--- | :--- | :--- |"
            ]
            for sid, sinfo in sorted(health_data.get("sources", {}).items()):
                st = sinfo.get("status", "unknown")
                fails = sinfo.get("consecutive_failures", 0)
                reached = sinfo.get("last_reached") or "never"
                changed = sinfo.get("last_changed") or "never"
                lines.append(f"| `{sid}` | `{st}` | {fails} | {reached} | {changed} |")
            lines.append("")
            with open(summary_p, "a", encoding="utf-8") as sf:
                sf.write("\n".join(lines) + "\n")
        except Exception as exc:
            print(f"[SourceHealth] Error writing step summary: {exc}", file=sys.stderr)

    return should_save


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
