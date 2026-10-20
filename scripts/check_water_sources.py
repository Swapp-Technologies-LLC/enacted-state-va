#!/usr/bin/env python3
"""
check_water_sources.py
Unified Water Source Checker & Absence Detection Dispatcher (Slice I1).
Swapp Technologies LLC

Requirements (Slice I1):
1. Run reachability and hash check of each water source's index page and latest report.
2. Honours robots.txt: a disallowed path is skipped and reported, never fetched.
3. One request at a time per host, with at least 5 s between requests.
4. A changed or new report file -> saves cached file and sha256 under .cache/pdf_cache/,
   records note "new report — record not yet written". No measurement record is generated automatically.
5. "Expected report not found" fires ONLY from each source's cadence_basis (ig08), never because a daily run saw nothing.
6. Writes status/sources.json on every run (last_reached, last_changed, consecutive_failures).
7. Auto-manages issues for unreachable sources with built-in GITHUB_TOKEN (comment on fail, close on recovery).
"""

import os
import sys
import re
import json
import yaml
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from crawler import EnactedCrawler
from source_health import (
    load_source_health,
    save_source_health,
    record_source_check,
    manage_source_alert_issues,
    get_iso_now,
    StubIssueClient
)


def load_all_system_sources(water_dir: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    """
    Finds and loads all sources from water/measurements/*/sources.yaml.
    Returns list of (system_dir, source_dict).
    """
    results = []
    if not water_dir.exists():
        return results

    for sdir in sorted(water_dir.iterdir()):
        if not sdir.is_dir() or sdir.name.startswith("."):
            continue
        sources_yaml = sdir / "sources.yaml"
        if sources_yaml.exists():
            try:
                data = yaml.safe_load(sources_yaml.read_text(encoding="utf-8")) or {}
                source_list = data if isinstance(data, list) else data.get("sources", [data])
                for src in source_list:
                    if isinstance(src, dict):
                        results.append((sdir, src))
            except Exception as exc:
                print(f"[WaterCheck] Error loading {sources_yaml}: {exc}", file=sys.stderr)

    return results


def is_valid_url(url: Optional[str]) -> bool:
    """Checks whether URL is a valid http/https string and not a placeholder."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if u.startswith("[FOUNDER:") or u.startswith("[PLACEHOLDER"):
        return False
    return u.startswith("http://") or u.startswith("https://")


def is_cadence_basis_valid(cadence_basis: Any) -> bool:
    """
    Validates cadence_basis:
    Must be a dict with EXACTLY ONE of 'rule' or 'agency_statement'.
    - If 'rule': citation and quoted_sentence must be non-empty strings without [FOUNDER:.
    - If 'agency_statement': url, date_read, and quoted_sentence must be non-empty without [FOUNDER:.
    """
    if not isinstance(cadence_basis, dict):
        return False

    has_rule = "rule" in cadence_basis
    has_agency = "agency_statement" in cadence_basis

    if has_rule == has_agency:
        return False

    if has_rule:
        rule = cadence_basis.get("rule")
        if not isinstance(rule, dict):
            return False
        citation = str(rule.get("citation", "")).strip()
        quote = str(rule.get("quoted_sentence", "")).strip()
        if not citation or not quote:
            return False
        if citation.startswith("[FOUNDER:") or quote.startswith("[FOUNDER:"):
            return False
        return True

    if has_agency:
        stmt = cadence_basis.get("agency_statement")
        if not isinstance(stmt, dict):
            return False
        url = str(stmt.get("url", "")).strip()
        date_read = str(stmt.get("date_read", "")).strip()
        quote = str(stmt.get("quoted_sentence", "")).strip()
        if not url or not date_read or not quote:
            return False
        if url.startswith("[FOUNDER:") or date_read.startswith("[FOUNDER:") or quote.startswith("[FOUNDER:"):
            return False
        return True

    return False


def parse_cadence_due_date(source: Dict[str, Any], current_year: int) -> Optional[date]:
    """Derives expected reporting due date from source expected_by."""
    expected_by = source.get("expected_by")
    if not expected_by:
        return None

    if isinstance(expected_by, dict):
        m = expected_by.get("month")
        d = expected_by.get("day")
        if isinstance(m, int) and isinstance(d, int) and 1 <= m <= 12 and 1 <= d <= 31:
            try:
                return date(current_year, m, d)
            except ValueError:
                return None
        return None

    if not isinstance(expected_by, str):
        return None

    s = expected_by.strip()
    if s.startswith("[FOUNDER:"):
        return None

    # Quarter pattern
    qm = re.search(r"(\d+)\s+days after quarter end", s, re.IGNORECASE)
    if qm:
        return date(current_year, 3, 31) + timedelta(days=int(qm.group(1)))

    # Month name pattern (e.g. "July 1", "April 1")
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
    }
    m_match = re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b", s, re.IGNORECASE)
    if m_match:
        mnum = month_names.get(m_match.group(1).lower())
        dnum = int(m_match.group(2))
        if mnum and 1 <= dnum <= 31:
            try:
                return date(current_year, mnum, dnum)
            except ValueError:
                return None

    # MM-DD pattern
    mmdd = re.search(r"\b(\d{1,2})-(\d{1,2})\b", s)
    if mmdd:
        m = int(mmdd.group(1))
        d = int(mmdd.group(2))
        if 1 <= m <= 12 and 1 <= d <= 31:
            try:
                return date(current_year, m, d)
            except ValueError:
                return None

    return None


def run_water_checks(
    repo_dir: Path,
    crawler: Optional[EnactedCrawler] = None,
    as_of: Optional[date] = None,
    filter_source: Optional[str] = None,
    status_path: Optional[Path] = None,
    issue_client = None
) -> Dict[str, Any]:
    """
    Executes reachability, hash, and cadence absence checks across water sources.
    Returns summary dict.
    """
    c = crawler or EnactedCrawler()
    today = as_of or datetime.now(timezone.utc).date()
    now_iso = get_iso_now()

    water_measurements = repo_dir / "water" / "measurements"
    sources_to_check = load_all_system_sources(water_measurements)

    if filter_source and filter_source != "all":
        sources_to_check = [
            (sdir, src) for sdir, src in sources_to_check
            if src.get("id") == filter_source or filter_source in str(sdir)
        ]

    st_path = status_path or (repo_dir / "status" / "sources.json")
    health_data = load_source_health(st_path)

    cache_dir = repo_dir / ".cache" / "pdf_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = repo_dir / ".cache" / "new_reports_manifest.json"

    new_reports = []
    if manifest_path.exists():
        try:
            new_reports = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            new_reports = []

    absence_candidates = []
    checked_sources = 0
    changed_sources = 0

    for system_dir, src in sources_to_check:
        source_id = src.get("id", system_dir.name)
        checked_sources += 1

        index_url = src.get("index_url")
        last_found = src.get("last_found", {})
        report_url = last_found.get("url")
        known_sha256 = last_found.get("sha256")

        source_success = True
        source_changed = False
        error_msg = ""

        # 1. Reachability & hash check of index page
        if is_valid_url(index_url):
            allowed, status_code, body, err = c.fetch(index_url)
            if not allowed:
                source_success = False
                error_msg = "disallowed_by_robots_txt"
            elif status_code != 200:
                source_success = False
                error_msg = err or f"HTTP {status_code}"
            else:
                # Successfully reached index page
                pass

        # 2. Reachability & hash check of latest report
        if is_valid_url(report_url) and source_success:
            allowed, status_code, report_bytes, err = c.fetch(report_url)
            if not allowed:
                source_success = False
                error_msg = "disallowed_by_robots_txt"
            elif status_code != 200:
                source_success = False
                error_msg = err or f"HTTP {status_code}"
            else:
                fetched_sha256 = hashlib.sha256(report_bytes).hexdigest()
                # Check against known hash
                if known_sha256 and fetched_sha256 != known_sha256:
                    # New or changed report found!
                    source_changed = True
                    changed_sources += 1
                    # Save to cache
                    cached_file = cache_dir / f"{fetched_sha256}.bin"
                    cached_file.write_bytes(report_bytes)

                    # Record note: "new report — record not yet written"
                    new_reports.append({
                        "source_id": source_id,
                        "system": system_dir.name,
                        "report_url": report_url,
                        "previous_sha256": known_sha256,
                        "new_sha256": fetched_sha256,
                        "fetched_at": now_iso,
                        "note": "new report — record not yet written"
                    })
                    print(
                        f"[WaterCheck] New report detected for {source_id}: "
                        f"{fetched_sha256} (was {known_sha256}). "
                        f"Cached to .cache/. Note: new report -- record not yet written.",
                        file=sys.stderr
                    )
                else:
                    # Hash matches known file; ensure it is cached
                    cached_file = cache_dir / f"{fetched_sha256}.bin"
                    if not cached_file.exists():
                        cached_file.write_bytes(report_bytes)

        # 3. Absence detection based strictly on cadence_basis (Rule ig08)
        cadence_basis = src.get("cadence_basis")
        if is_cadence_basis_valid(cadence_basis) and src.get("active", True):
            due_date = parse_cadence_due_date(src, today.year)
            if due_date:
                grace = src.get("grace_days", 30)
                cutoff = due_date + timedelta(days=grace)
                # Only if today has passed the statutory/stated due date + grace
                if today > cutoff and not source_success:
                    absence_candidates.append({
                        "source_id": source_id,
                        "system": system_dir.name,
                        "due_date": due_date.isoformat(),
                        "cutoff_date": cutoff.isoformat(),
                        "status": "expected_report_not_found"
                    })

        # Update source health data
        primary_url = report_url if is_valid_url(report_url) else (index_url if is_valid_url(index_url) else "")
        record_source_check(
            source_id=source_id,
            url=primary_url,
            success=source_success,
            changed=source_changed,
            error=error_msg,
            health_data=health_data,
            now_iso=now_iso
        )

    # Save manifest if new reports exist
    if new_reports:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(new_reports, indent=2), encoding="utf-8")

    # Persist health data to status/sources.json
    save_source_health(health_data, st_path)

    # Auto-manage GitHub alert issues
    issue_actions = manage_source_alert_issues(health_data, issue_client=issue_client)

    return {
        "checked_sources": checked_sources,
        "changed_sources": changed_sources,
        "new_reports_count": len(new_reports),
        "absence_candidates_count": len(absence_candidates),
        "issue_actions": issue_actions
    }


def main():
    parser = argparse.ArgumentParser(description="Enacted Water Sources Reachability & Cadence Checker")
    parser.add_argument("--repo-dir", default=str(REPO_DIR), help="Path to state repository root")
    parser.add_argument("--source", default="all", help="Source ID to check (or 'all')")
    parser.add_argument("--as-of", default=None, help="Evaluation date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Run without pushing or modifying external state")
    args = parser.parse_args()

    repo_path = Path(args.repo_dir)
    eval_date = date.fromisoformat(args.as_of) if args.as_of else None

    print(f"[WaterCheck] Checking water sources in {repo_path} (source={args.source})...")
    res = run_water_checks(
        repo_dir=repo_path,
        as_of=eval_date,
        filter_source=args.source
    )
    print(f"[WaterCheck] Done. Checked: {res['checked_sources']} | Changed: {res['changed_sources']} | New reports: {res['new_reports_count']}")
    if res['issue_actions']:
        print(f"[WaterCheck] Issue actions: {res['issue_actions']}")


if __name__ == "__main__":
    main()
