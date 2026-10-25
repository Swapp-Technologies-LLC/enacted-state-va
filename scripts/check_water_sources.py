#!/usr/bin/env python3
"""
check_water_sources.py
Unified Water Source Checker & Absence Detection Dispatcher (Slice I1 Fix Pass).
Swapp Technologies LLC

Requirements (Slice I1 Fix Pass):
1. Discovery: For each source with index_url, lists links on fetched index page.
   Matches links with issue_pattern or get_report_period year rule.
   A matching link that isn't last_found.url and belongs to current cycle is cached with SHA-256
   and recorded in manifest with note: "new report — record not yet written".
2. Existing same-URL hash check stays.
3. Absence detection: Decided strictly by check_expected_reports.py logic:
   Past due_date + grace_days per cadence_basis, and no issue for current cycle found on a
   *successfully fetched* index page.
4. Outages: An unreachable index page or report is an outage. Recorded in status/sources.json
   and source-alert issue, NEVER to absence.
5. Missing fields: If any water source lacks index_url, skip discovery for it without inventing one.
6. Meaningful changes only: status/sources.json is only committed if status flips, last_changed moves,
   or sources are added/removed. Full health with timestamps is always written to sources-health.json
   and $GITHUB_STEP_SUMMARY.
"""

import os
import sys
import re
import json
import yaml
import hashlib
import argparse
import urllib.parse
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
from check_expected_reports import (
    extract_links_from_html,
    hash_link_list,
    get_report_period,
    parse_cadence_due_date,
    is_cadence_basis_valid,
    format_not_found_record
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


def run_water_checks(
    repo_dir: Path,
    as_of: Optional[date] = None,
    filter_source: Optional[str] = None,
    crawler: Optional[EnactedCrawler] = None,
    status_path: Optional[Path] = None,
    issue_client: Optional[Any] = None,
    candidate_output_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Runs deterministic reachability, discovery, and cadence checks across watched water sources.
    - Zero model calls anywhere (Rule 10).
    - Caches new reports under .cache/pdf_cache/ with verified sha256 and manifest note.
    - Zero measurement records written automatically.
    - Evaluates absence candidates strictly from cadence_basis and index discovery.
    - Updates status/sources.json only on meaningful changes (status flip, last_changed, add/remove).
    """
    c = crawler or EnactedCrawler()
    today = as_of or date.today()
    now_iso = get_iso_now()

    water_dir = repo_dir / "water" / "measurements"
    sources_to_check = load_all_system_sources(water_dir)

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

    current_year = today.year
    expected_period = str(current_year - 1)

    for system_dir, src in sources_to_check:
        source_id = src.get("id", system_dir.name)
        source_title = src.get("title", "Water Quality Report")
        checked_sources += 1

        index_url = src.get("index_url")
        last_found = src.get("last_found", {})
        last_found_url = last_found.get("url")
        last_found_sha256 = last_found.get("sha256")
        last_found_period = get_report_period(last_found)
        issue_pattern = src.get("issue_pattern")
        cadence_basis = src.get("cadence_basis")
        active = src.get("active", True)

        source_success = True
        source_changed = False
        error_msg = ""
        index_fetched_ok = False
        links_on_index: List[Tuple[str, str]] = []
        discovered_report_for_current_cycle = False
        new_report_found_in_discovery = False

        # -------------------------------------------------------------------
        # 1. Index Page Fetch & Link Discovery (F1)
        # -------------------------------------------------------------------
        if is_valid_url(index_url):
            allowed, status_code, body, err = c.fetch(index_url)
            if not allowed:
                source_success = False
                error_msg = "disallowed_by_robots_txt"
            elif status_code != 200:
                source_success = False
                error_msg = err or f"HTTP {status_code}"
            else:
                html_text = body.decode("utf-8", errors="replace")
                links_on_index = extract_links_from_html(html_text, index_url)
                if not links_on_index:
                    source_success = False
                    error_msg = "Index page link list is empty"
                else:
                    index_fetched_ok = True

                    # Discover newer reports from index links
                    for link_url, link_text in links_on_index:
                        if link_url == last_found_url:
                            continue

                        link_period = get_report_period({"title": link_text, "url": link_url})
                        parsed_path = urllib.parse.urlparse(link_url).path
                        filename = os.path.basename(parsed_path.rstrip("/"))

                        is_match = False
                        cand_period_used = None

                        if issue_pattern:
                            # Test issue_pattern with candidate periods: link_period, expected_period, current_year
                            cand_periods = []
                            if link_period:
                                cand_periods.append(link_period)
                            for y in [expected_period, str(current_year)]:
                                if y not in cand_periods:
                                    cand_periods.append(y)

                            for cp in cand_periods:
                                pat = issue_pattern.replace("{period}", re.escape(cp))
                                if re.search(pat, filename, re.IGNORECASE) or re.search(pat, link_text, re.IGNORECASE):
                                    is_match = True
                                    cand_period_used = cp
                                    break
                        else:
                            # Use get_report_period year rule
                            lower_target = f"{filename} {link_text}".lower()
                            if link_period:
                                if any(term in lower_target for term in ("report", "ccr", "wqr")):
                                    is_match = True
                                    cand_period_used = link_period
                            elif re.search(rf"\b{expected_period}\b", lower_target) and any(term in lower_target for term in ("report", "ccr", "wqr")):
                                is_match = True
                                cand_period_used = expected_period

                        if is_match:
                            # Check whether it belongs to current/newer cycle
                            belongs_to_cycle = False
                            if cand_period_used:
                                if last_found_period:
                                    try:
                                        if int(cand_period_used) >= int(expected_period) or int(cand_period_used) > int(last_found_period):
                                            belongs_to_cycle = True
                                    except ValueError:
                                        if str(cand_period_used) >= str(expected_period):
                                            belongs_to_cycle = True
                                else:
                                    belongs_to_cycle = True
                            else:
                                belongs_to_cycle = True

                            if belongs_to_cycle:
                                # Fetch newly discovered report
                                r_allowed, r_code, r_bytes, r_err = c.fetch(link_url)
                                if not r_allowed:
                                    source_success = False
                                    error_msg = "new_report_disallowed_by_robots_txt"
                                elif r_code != 200:
                                    source_success = False
                                    error_msg = f"HTTP {r_code} fetching discovered report: {r_err}"
                                else:
                                    new_sha = hashlib.sha256(r_bytes).hexdigest()
                                    cached_file = cache_dir / f"{new_sha}.bin"
                                    cached_file.write_bytes(r_bytes)

                                    new_reports.append({
                                        "source_id": source_id,
                                        "system": system_dir.name,
                                        "report_url": link_url,
                                        "previous_url": last_found_url,
                                        "previous_sha256": last_found_sha256,
                                        "new_sha256": new_sha,
                                        "fetched_at": now_iso,
                                        "note": "new report — record not yet written"
                                    })
                                    source_changed = True
                                    changed_sources += 1
                                    discovered_report_for_current_cycle = True
                                    new_report_found_in_discovery = True
                                    print(f"[WaterCheck] Discovered new report for {source_id}: {link_url} ({new_sha})")
                                    break
        else:
            # index_url absent or placeholder ([FOUNDER: ...]): skip discovery without inventing
            pass

        # -------------------------------------------------------------------
        # 2. Same-URL Hash Check (F1)
        # -------------------------------------------------------------------
        if not new_report_found_in_discovery and source_success and is_valid_url(last_found_url):
            allowed, status_code, report_bytes, err = c.fetch(last_found_url)
            if not allowed:
                source_success = False
                error_msg = "disallowed_by_robots_txt"
            elif status_code != 200:
                source_success = False
                error_msg = err or f"HTTP {status_code}"
            else:
                fetched_sha256 = hashlib.sha256(report_bytes).hexdigest()
                if last_found_sha256 and fetched_sha256 != last_found_sha256:
                    # Same-URL changed report content
                    source_changed = True
                    changed_sources += 1
                    cached_file = cache_dir / f"{fetched_sha256}.bin"
                    cached_file.write_bytes(report_bytes)

                    new_reports.append({
                        "source_id": source_id,
                        "system": system_dir.name,
                        "report_url": last_found_url,
                        "previous_url": last_found_url,
                        "previous_sha256": last_found_sha256,
                        "new_sha256": fetched_sha256,
                        "fetched_at": now_iso,
                        "note": "new report — record not yet written"
                    })
                    print(
                        f"[WaterCheck] Changed content detected at same URL for {source_id}: "
                        f"{fetched_sha256} (was {last_found_sha256}). Cached to .cache/.",
                        file=sys.stderr
                    )
                else:
                    cached_file = cache_dir / f"{fetched_sha256}.bin"
                    if not cached_file.exists():
                        cached_file.write_bytes(report_bytes)

        # -------------------------------------------------------------------
        # 3. Absence Detection Decided by Cadence Basis & Index Check (F1)
        # -------------------------------------------------------------------
        if active and is_cadence_basis_valid(cadence_basis):
            due_date = parse_cadence_due_date(src, current_year)
            if due_date:
                grace_days = src.get("grace_days", 30)
                effective_deadline = due_date + timedelta(days=grace_days)

                # Rule: Before due -> no absence whatever happens
                if today > effective_deadline:
                    # Past due + grace!
                    # Rule: Outage (index unreachable) is NEVER an absence candidate!
                    if index_fetched_ok:
                        has_report_for_cycle = False

                        if last_found_period:
                            try:
                                if int(last_found_period) >= int(expected_period):
                                    has_report_for_cycle = True
                            except ValueError:
                                if str(last_found_period) == str(expected_period):
                                    has_report_for_cycle = True

                        if discovered_report_for_current_cycle:
                            has_report_for_cycle = True

                        # Check extracted links on index for current cycle
                        if not has_report_for_cycle and links_on_index:
                            for l_url, l_txt in links_on_index:
                                lp = get_report_period({"title": l_txt, "url": l_url})
                                if lp and str(lp) == str(expected_period):
                                    has_report_for_cycle = True
                                    break
                                if issue_pattern:
                                    pat = issue_pattern.replace("{period}", re.escape(expected_period))
                                    fn = os.path.basename(urllib.parse.urlparse(l_url).path.rstrip("/"))
                                    if re.search(pat, fn, re.IGNORECASE) or re.search(pat, l_txt, re.IGNORECASE):
                                        has_report_for_cycle = True
                                        break
                                else:
                                    fn = os.path.basename(urllib.parse.urlparse(l_url).path.rstrip("/"))
                                    lt = f"{fn} {l_txt}".lower()
                                    if re.search(rf"\b{expected_period}\b", lt) and any(t in lt for t in ("report", "ccr", "wqr")):
                                        has_report_for_cycle = True
                                        break

                        if not has_report_for_cycle:
                            candidate_title = f"{source_title}: no {expected_period} report found at watched source as of {today.isoformat()}."
                            absence_candidates.append({
                                "source_id": source_id,
                                "system": system_dir.name,
                                "period": expected_period,
                                "due_date": due_date.isoformat(),
                                "cutoff_date": effective_deadline.isoformat(),
                                "status": "expected_report_not_found",
                                "title": candidate_title
                            })
                            print(f"[WaterCheck] Absence candidate: {source_id} missing period {expected_period} (deadline {effective_deadline})")

        # -------------------------------------------------------------------
        # 4. Source Health Tracking
        # -------------------------------------------------------------------
        primary_url = index_url if is_valid_url(index_url) else (last_found_url if is_valid_url(last_found_url) else "")
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

    # Persist health data to status/sources.json ONLY on meaningful change (F3)
    save_source_health(health_data, st_path, only_if_meaningful=True)

    # Auto-manage GitHub alert issues (ig02)
    issue_actions = manage_source_alert_issues(health_data, issue_client=issue_client)

    return {
        "checked_sources": checked_sources,
        "changed_sources": changed_sources,
        "new_reports_count": len(new_reports),
        "absence_candidates_count": len(absence_candidates),
        "absence_candidates": absence_candidates,
        "issue_actions": issue_actions
    }


def main():
    parser = argparse.ArgumentParser(description="Enacted Water Sources Reachability, Discovery & Cadence Checker")
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
    print(f"[WaterCheck] Done. Checked: {res['checked_sources']} | Changed: {res['changed_sources']} | New reports: {res['new_reports_count']} | Absence candidates: {res['absence_candidates_count']}")
    if res['issue_actions']:
        print(f"[WaterCheck] Issue actions: {res['issue_actions']}")


if __name__ == "__main__":
    main()
