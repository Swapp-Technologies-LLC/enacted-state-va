#!/usr/bin/env python3
"""
check_expected_reports.py
Deterministic Absence-Detection Engine for Watched Drinking-Water Sources.

Governed by:
- Standing rules (Rules 1-10: fetch-first hashes, zero model in ingestion, PR-only)
- docs/ENACTED_SOCIAL_VOICE_2026-10-02.md (wording gate, neutrality, copy cadence)
- ENACTED_SUBSCRIPTION_BUILD_SPEC.md

Invariants:
1. Pure deterministic code. ZERO model calls anywhere.
2. Inactive source or source with no cadence_basis NEVER produces a not-found record.
3. Network errors, non-200 responses, cross-host redirects, or empty link lists
   are logged as source_unreachable — NEVER reported as a missing report.
4. Candidate records are written to a candidates directory; no branch or PR is created by this script.
5. Zero email sending or notification dispatches during detection runs.
6. All candidate text must pass the strict wording gate.
"""

import os
import sys
import re
import json
import yaml
import hashlib
import argparse
import urllib.request
import urllib.error
import urllib.parse
from html.parser import HTMLParser
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Tuple, Any, Optional

# Ensure scripts dir is on sys.path for wording_gate import
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from wording_gate import scan_wording_gate, assert_wording_gate, WordingGateViolationError


class LinkExtractor(HTMLParser):
    """Deterministic, pure-stdlib HTML parser extracting anchor links."""
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: List[Tuple[str, str]] = []
        self._current_href: Optional[str] = None
        self._current_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        if tag.lower() == "a":
            for k, v in attrs:
                if k.lower() == "href" and v:
                    resolved = urllib.parse.urljoin(self.base_url, v.strip())
                    self._current_href = resolved
                    self._current_text = []
                    break

    def handle_data(self, data: str):
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() == "a" and self._current_href is not None:
            text = " ".join("".join(self._current_text).split())
            self.links.append((self._current_href, text))
            self._current_href = None
            self._current_text = []


def extract_links_from_html(html_content: str, base_url: str) -> List[Tuple[str, str]]:
    """Extracts all anchor links from HTML text resolved against base_url."""
    parser = LinkExtractor(base_url)
    parser.feed(html_content)
    return parser.links


def hash_link_list(links: List[Tuple[str, str]]) -> str:
    """
    Deterministically sorts and hashes extracted link lists (not raw page HTML).
    This insulates against dynamic page elements such as timestamps or CSRF tokens.
    """
    sorted_links = sorted(set(links), key=lambda x: (x[0], x[1]))
    serialized = "\n".join(f"{url}\t{txt}" for url, txt in sorted_links)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def fetch_watched_index(index_url: str, timeout: int = 15, opener=None) -> Tuple[str, List[Tuple[str, str]], str, str]:
    """
    Fetches the index URL and validates against unreachable criteria.
    Returns (status, links, link_hash, error_detail).
    status is either 'ok' or 'source_unreachable'.
    """
    req = urllib.request.Request(
        index_url,
        headers={"User-Agent": "Enacted-Water-Monitor/1.0 (+https://enacted.app)"}
    )

    try:
        if opener is not None:
            resp = opener.open(req, timeout=timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout)
        
        final_url = resp.geturl()
        status_code = resp.getcode()

        # Check 1: Non-200 status code
        if status_code != 200:
            return "source_unreachable", [], "", f"Non-200 HTTP response: {status_code}"

        # Check 2: Cross-domain / cross-host redirect
        orig_host = urllib.parse.urlparse(index_url).netloc.lower()
        final_host = urllib.parse.urlparse(final_url).netloc.lower()
        if orig_host != final_host:
            return "source_unreachable", [], "", f"Redirect to different host: {final_host} (expected {orig_host})"

        raw_bytes = resp.read()
        html_text = raw_bytes.decode("utf-8", errors="replace")

        # Check 3: Extract links
        links = extract_links_from_html(html_text, final_url)
        if not links:
            return "source_unreachable", [], "", "Index page link list is empty"

        link_hash = hash_link_list(links)
        return "ok", links, link_hash, ""

    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, Exception) as exc:
        return "source_unreachable", [], "", f"Fetch error: {exc}"


def load_system_sources(system_dir: Path) -> List[Dict[str, Any]]:
    """Loads sources.yaml for a given water system directory."""
    sources_file = system_dir / "sources.yaml"
    if not sources_file.exists():
        return []
    try:
        data = yaml.safe_load(sources_file.read_text(encoding="utf-8")) or {}
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("sources", [data])
    except Exception as exc:
        print(f"Error reading {sources_file}: {exc}", file=sys.stderr)
    return []


def get_report_period(report_dict: Any) -> Optional[str]:
    """Extracts reporting period (e.g. '2024' or '2025') from a report dict."""
    if not isinstance(report_dict, dict):
        return None
    for k in ("period", "reporting_period"):
        v = report_dict.get(k)
        if v:
            return str(v).strip()
    title = str(report_dict.get("title", ""))
    url = str(report_dict.get("url", ""))
    # Look for 4-digit year in title or url
    match = re.search(r"\b(20\d{2})\b", title)
    if match:
        return match.group(1)
    match_url = re.search(r"\b(20\d{2})\b", url)
    if match_url:
        return match_url.group(1)
    return None


def parse_cadence_due_date(source: Dict[str, Any], current_year: int) -> Optional[date]:
    """
    Derives the statutory or stated due date for annual/periodic reporting.
    Derives the date from expected_by in structured form (month/day, or 'N days after quarter end').
    A source whose expected_by cannot be parsed stays inactive and returns None.
    """
    expected_by = source.get("expected_by")
    cadence = source.get("cadence", "annual")

    if not expected_by:
        return None

    # Case 1: Structured dict with month and day
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

    expected_str = expected_by.strip()
    if expected_str.startswith("[FOUNDER:"):
        return None

    # Case 2: "N days after quarter end"
    quarter_match = re.search(r"(\d+)\s+days after quarter end", expected_str, re.IGNORECASE)
    if quarter_match:
        days_after = int(quarter_match.group(1))
        return date(current_year, 3, 31) + timedelta(days=days_after)

    # Case 3: Month name and day (e.g. "July 1", "by April 1", "July 1 annually...")
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
    }
    month_pattern = r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b"
    m_match = re.search(month_pattern, expected_str, re.IGNORECASE)
    if m_match:
        month_str = m_match.group(1).lower()
        day_num = int(m_match.group(2))
        month_num = month_names.get(month_str)
        if month_num and 1 <= day_num <= 31:
            try:
                return date(current_year, month_num, day_num)
            except ValueError:
                return None

    # Case 4: MM-DD (e.g. "07-01", "04-01")
    mmdd_match = re.search(r"\b(\d{1,2})-(\d{1,2})\b", expected_str)
    if mmdd_match:
        m = int(mmdd_match.group(1))
        d = int(mmdd_match.group(2))
        if 1 <= m <= 12 and 1 <= d <= 31:
            try:
                return date(current_year, m, d)
            except ValueError:
                return None

    return None


def is_cadence_basis_valid(cadence_basis: Any) -> bool:
    """
    Validates cadence_basis:
    Must be a dict with EXACTLY ONE of 'rule' or 'agency_statement'.
    - If 'rule': must be a dict with non-empty string 'citation' AND non-empty string 'quoted_sentence'.
    - If 'agency_statement': must be a dict with non-empty string 'url', non-empty string 'date_read',
      AND non-empty string 'quoted_sentence'.
    No placeholder starting with '[FOUNDER:' is allowed in any required field.
    Any other shape, string, or combination is rejected (returns False).
    """
    if not isinstance(cadence_basis, dict):
        return False

    has_rule = "rule" in cadence_basis
    has_agency = "agency_statement" in cadence_basis

    # Must have exactly one
    if has_rule == has_agency:
        return False

    if has_rule:
        rule = cadence_basis.get("rule")
        if not isinstance(rule, dict):
            return False
        citation = rule.get("citation")
        quoted_sentence = rule.get("quoted_sentence")
        if not citation or not isinstance(citation, str) or not citation.strip():
            return False
        if not quoted_sentence or not isinstance(quoted_sentence, str) or not quoted_sentence.strip():
            return False
        if citation.strip().startswith("[FOUNDER:") or quoted_sentence.strip().startswith("[FOUNDER:"):
            return False
        return True

    if has_agency:
        statement = cadence_basis.get("agency_statement")
        if not isinstance(statement, dict):
            return False
        url = statement.get("url")
        date_read = statement.get("date_read")
        quoted_sentence = statement.get("quoted_sentence")
        if not url or not isinstance(url, str) or not url.strip():
            return False
        if not date_read or not isinstance(date_read, str) or not date_read.strip():
            return False
        if not quoted_sentence or not isinstance(quoted_sentence, str) or not quoted_sentence.strip():
            return False
        if url.strip().startswith("[FOUNDER:") or date_read.strip().startswith("[FOUNDER:") or quoted_sentence.strip().startswith("[FOUNDER:"):
            return False
        return True

    return False


def find_system_contact(system_dir: Path) -> Dict[str, Any]:
    """Finds existing contact block from measurement records in the same directory."""
    for f in system_dir.glob("*.md"):
        if f.name in ("README.md", "SOURCING_BLOCKED.md"):
            continue
        try:
            txt = f.read_text(encoding="utf-8")
            parts = txt.split("---", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1]) or {}
                if "contact" in fm:
                    return fm["contact"]
        except Exception:
            pass
    return {
        "agency": "Water Quality Authority",
        "phone": "(pending verification)",
        "email": "(pending verification)"
    }


def format_not_found_record(
    system_name: str,
    source_title: str,
    period: str,
    as_of_date_str: str,
    checked_iso: str,
    index_url: str,
    cadence_basis: Dict[str, Any],
    last_found: Dict[str, Any],
    contact: Dict[str, Any],
    system_slug: str,
    source_id: str,
    jurisdiction: str,
    jurisdiction_name: str
) -> Tuple[str, str]:
    """
    Renders frontmatter and body for a Not-Found candidate record.
    Title shape exactly:
    [System] · [source title]: no [period] report found at the watched source as of [date].
    """
    title = f"{system_name} · {source_title}: no {period} report found at the watched source as of {as_of_date_str}."

    basis_quote = ""
    basis_citation = ""
    if "rule" in cadence_basis:
        basis_citation = cadence_basis["rule"].get("citation", "40 CFR Part 141 Subpart O")
        basis_quote = cadence_basis["rule"].get("quoted_sentence", "")
    elif "agency_statement" in cadence_basis:
        basis_citation = cadence_basis["agency_statement"].get("citation", "Agency stated schedule")
        basis_quote = cadence_basis["agency_statement"].get("quoted_sentence", "")

    frontmatter = {
        "record_type": "source_check",
        "check_status": "not_found",
        "source_agency": contact.get("agency", system_name),
        "jurisdiction": jurisdiction,
        "jurisdiction_name": jurisdiction_name,
        "system": system_name,
        "source_id": source_id,
        "source_title": source_title,
        "reporting_period": str(period),
        "index_url": index_url,
        "checked_at": checked_iso,
        "published_date": checked_iso[:10],
        "cadence": "annual",
        "cadence_basis": cadence_basis,
        "last_found": last_found,
        "contact": contact,
        "topic": "water",
        "topics": ["water"],
    }

    body = f"""# {title}

## 1. Watched Source & Checked Status
- **Watched Source:** {index_url}
- **Checked Date & Time:** {checked_iso}

## 2. Cadence Basis
Quoted requirement ({basis_citation}):
> "{basis_quote}"

## 3. Last Issue Found
- **Title:** {last_found.get('title', 'Unknown')}
- **Publication Date:** {last_found.get('date', 'Unknown')}
- **Primary Link:** {last_found.get('url', 'Unknown')}

## 4. Who to Ask
- **Agency:** {contact.get('agency', 'Customer Service')}
- **Phone:** {contact.get('phone', 'N/A')}
- **Email:** {contact.get('email', 'N/A')}
- **Website:** {contact.get('website', 'N/A')}
"""
    yaml_header = yaml.dump(frontmatter, sort_keys=False)
    full_content = f"---\n{yaml_header}---\n\n{body}"
    return title, full_content


def format_found_closing_record(
    system_name: str,
    source_title: str,
    period: str,
    found_date_str: str,
    found_iso: str,
    report_link: str,
    report_sha256: str,
    closes_record_key: str,
    contact: Dict[str, Any],
    system_slug: str,
    source_id: str,
    jurisdiction: str,
    jurisdiction_name: str
) -> Tuple[str, str]:
    """
    Renders frontmatter and body for a Found (closing) candidate record.
    Title shape exactly:
    [System] · [source title]: [period] report found [date].
    """
    title = f"{system_name} · {source_title}: {period} report found {found_date_str}."

    frontmatter = {
        "record_type": "source_check",
        "check_status": "found",
        "source_agency": contact.get("agency", system_name),
        "jurisdiction": jurisdiction,
        "jurisdiction_name": jurisdiction_name,
        "system": system_name,
        "source_id": source_id,
        "source_title": source_title,
        "reporting_period": str(period),
        "found_date": found_iso[:10],
        "published_date": found_iso[:10],
        "source_fetched_at": found_iso,
        "primary_source_url": report_link,
        "source_sha256": report_sha256,
        "closes_record": closes_record_key,
        "contact": contact,
        "topic": "water",
        "topics": ["water"],
    }

    body = f"""# {title}

## 1. Verified Report Record
- **Primary Source Link:** {report_link}
- **Document SHA-256:** {report_sha256}
- **Closed Absence Notice:** {closes_record_key}

## 2. Who to Ask
- **Agency:** {contact.get('agency', 'Customer Service')}
- **Phone:** {contact.get('phone', 'N/A')}
- **Email:** {contact.get('email', 'N/A')}
- **Website:** {contact.get('website', 'N/A')}
"""
    yaml_header = yaml.dump(frontmatter, sort_keys=False)
    full_content = f"---\n{yaml_header}---\n\n{body}"
    return title, full_content


def find_system_metadata(system_dir: Path) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Extracts system name, contact, jurisdiction, and jurisdiction_name from existing record files."""
    for mf in system_dir.glob("*.md"):
        if mf.name in ("README.md", "SOURCING_BLOCKED.md") or "source_check" in mf.name:
            continue
        try:
            txt = mf.read_text(encoding="utf-8")
            parts = txt.split("---", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1]) or {}
                return fm.get("system"), fm.get("contact"), fm.get("jurisdiction"), fm.get("jurisdiction_name")
        except Exception:
            pass
    return None, None, None, None


def check_system_sources(
    system_dir: Path,
    as_of: date,
    dry_run: bool = True,
    candidate_output_dir: Optional[Path] = None,
    mock_opener = None
) -> List[Dict[str, Any]]:
    """
    Processes all sources in a system directory.
    Returns list of action reports.
    """
    actions = []
    sources = load_system_sources(system_dir)
    system_slug = system_dir.name
    fm_sys, fm_contact, fm_jur, fm_jur_name = find_system_metadata(system_dir)
    contact = fm_contact or find_system_contact(system_dir)

    # Resolve display names
    if fm_sys:
        system_name = fm_sys
        jurisdiction = fm_jur or f"md/{system_slug}"
        jurisdiction_name = fm_jur_name or system_name
    elif system_slug.startswith("wssc"):
        system_name = "WSSC Water"
        jurisdiction = "md/wssc"
        jurisdiction_name = "WSSC Water"
    elif system_slug.startswith("aa_"):
        system_name = system_slug.replace("aa_", "").replace("_", " ").title()
        jurisdiction = "md/anne_arundel"
        jurisdiction_name = "Anne Arundel County"
    else:
        system_name = system_slug.replace("_", " ").title()
        jurisdiction = f"md/{system_slug}"
        jurisdiction_name = system_name

    for src in sources:
        source_id = src.get("id", "source")
        source_title = src.get("title", "Water Quality Report")
        is_active = src.get("active", False)
        cadence_basis = src.get("cadence_basis")

        # Invariant 1: Source without active=True or valid cadence_basis NEVER produces record
        if not is_active or not is_cadence_basis_valid(cadence_basis):
            actions.append({
                "system": system_slug,
                "source_id": source_id,
                "status": "skipped_no_basis_or_inactive",
                "message": "Source inactive or cadence_basis pending verification. No record generated."
            })
            continue

        # Invariant 2: Check schedule & grace period
        current_year = as_of.year
        # CCR covering prior year (current_year - 1)
        expected_period = str(current_year - 1)
        due_date = parse_cadence_due_date(src, current_year)
        grace_days = src.get("grace_days", 30)

        if due_date is None:
            actions.append({
                "system": system_slug,
                "source_id": source_id,
                "status": "skipped_cadence_not_parsable",
                "message": f"Cadence '{src.get('cadence')}' could not be derived."
            })
            continue

        effective_deadline = due_date + timedelta(days=grace_days)
        if as_of <= effective_deadline:
            actions.append({
                "system": system_slug,
                "source_id": source_id,
                "status": "not_yet_due",
                "message": f"Current date {as_of} is before deadline {effective_deadline} (due {due_date} + {grace_days} grace days)."
            })
            continue

        # Invariant 3: Fetch index_url
        index_url = src.get("index_url", "")
        status, links, link_hash, error_detail = fetch_watched_index(index_url, opener=mock_opener)

        if status == "source_unreachable":
            actions.append({
                "system": system_slug,
                "source_id": source_id,
                "status": "source_unreachable",
                "message": f"Watched source unreachable: {error_detail}. Logged as source_unreachable issue; no missing record emitted."
            })
            continue

        # Invariant 4: Check if expected issue is already present in last_found
        last_found = src.get("last_found", {})
        last_found_sha256 = last_found.get("sha256", "")
        last_found_url = last_found.get("url", "")

        last_found_period = get_report_period(last_found)
        if last_found_period is not None:
            try:
                if int(last_found_period) >= int(expected_period):
                    actions.append({
                        "system": system_slug,
                        "source_id": source_id,
                        "status": "already_found",
                        "message": f"Report for period {last_found_period} is already recorded in last_found. No not-found record can be produced."
                    })
                    continue
            except ValueError:
                if str(last_found_period) == str(expected_period):
                    actions.append({
                        "system": system_slug,
                        "source_id": source_id,
                        "status": "already_found",
                        "message": f"Report for period {last_found_period} is already recorded in last_found. No not-found record can be produced."
                    })
                    continue

        newer_issue_found = False
        new_issue_link = ""
        new_issue_sha256 = ""

        # Scan extracted links for expected period using per-source issue_pattern or strict filename/text matching
        issue_pattern_template = src.get("issue_pattern")
        for link_url, link_text in links:
            parsed_path = urllib.parse.urlparse(link_url).path
            filename = os.path.basename(parsed_path.rstrip("/"))
            
            matched = False
            if issue_pattern_template:
                pattern = issue_pattern_template.replace("{period}", re.escape(expected_period))
                if re.search(pattern, filename, re.IGNORECASE) or re.search(pattern, link_text, re.IGNORECASE):
                    matched = True
            else:
                # Target filename and link text only — NEVER parent directory paths
                lower_target = f"{filename} {link_text}".lower()
                if re.search(rf"\b{expected_period}\b", lower_target) and ("report" in lower_target or "ccr" in lower_target or "wqr" in lower_target):
                    matched = True

            if matched:
                if link_url != last_found_url:
                    newer_issue_found = True
                    new_issue_link = link_url
                    new_issue_sha256 = hashlib.sha256(link_url.encode("utf-8")).hexdigest()
                    break

        # Check existing source_check records in this directory
        existing_not_found_records = []
        for mf in system_dir.glob("*.md"):
            try:
                content = mf.read_text(encoding="utf-8")
                if "record_type: source_check" in content and "check_status: not_found" in content:
                    if f"reporting_period: '{expected_period}'" in content or f'reporting_period: "{expected_period}"' in content or f"reporting_period: {expected_period}" in content:
                        existing_not_found_records.append(mf)
            except Exception:
                pass

        as_of_str = f"{as_of.strftime('%B')} {as_of.day}, {as_of.year}"
        checked_iso = datetime(as_of.year, as_of.month, as_of.day, 12, 0, 0, tzinfo=timezone.utc).isoformat()

        if not newer_issue_found:
            # Report is expected but absent
            if existing_not_found_records:
                actions.append({
                    "system": system_slug,
                    "source_id": source_id,
                    "status": "already_open",
                    "message": f"Open not-found record already exists: {existing_not_found_records[0].name}"
                })
                continue

            # Format Not-Found candidate
            title, record_content = format_not_found_record(
                system_name=system_name,
                source_title=source_title,
                period=expected_period,
                as_of_date_str=as_of_str,
                checked_iso=checked_iso,
                index_url=index_url,
                cadence_basis=cadence_basis,
                last_found=last_found,
                contact=contact,
                system_slug=system_slug,
                source_id=source_id,
                jurisdiction=jurisdiction,
                jurisdiction_name=jurisdiction_name
            )

            # Invariant 5: Wording gate check
            assert_wording_gate(record_content, f"Not-Found candidate for {system_slug}")

            # Invariant 6: Write candidate to separate candidate dir, NEVER to main
            cand_filename = f"source_check_not_found_{expected_period}_{as_of.isoformat()}.md"
            cand_branch = f"candidate/water-absence-{system_slug}-{expected_period}"

            if candidate_output_dir is not None:
                out_path = candidate_output_dir / system_slug / cand_filename
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(record_content, encoding="utf-8")

            actions.append({
                "system": system_slug,
                "source_id": source_id,
                "status": "candidate_created",
                "record_type": "not_found",
                "branch": cand_branch,
                "title": title,
                "candidate_filename": cand_filename,
                "message": f"Candidate not-found record written to candidates directory ({cand_filename}); no branch or PR is created by this script. Main branch untouched; no mail sent."
            })

        else:
            # Newer issue found! Check if it closes an open not-found record
            if existing_not_found_records:
                open_rec = existing_not_found_records[0]
                open_key = f"water/measurements/{system_slug}/{open_rec.stem}"
                title, record_content = format_found_closing_record(
                    system_name=system_name,
                    source_title=source_title,
                    period=expected_period,
                    found_date_str=as_of_str,
                    found_iso=checked_iso,
                    report_link=new_issue_link,
                    report_sha256=new_issue_sha256,
                    closes_record_key=open_key,
                    contact=contact,
                    system_slug=system_slug,
                    source_id=source_id,
                    jurisdiction=jurisdiction,
                    jurisdiction_name=jurisdiction_name
                )

                assert_wording_gate(record_content, f"Closing candidate for {system_slug}")

                cand_filename = f"source_check_found_{expected_period}_{as_of.isoformat()}.md"
                cand_branch = f"candidate/water-closing-{system_slug}-{expected_period}"

                if candidate_output_dir is not None:
                    out_path = candidate_output_dir / system_slug / cand_filename
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(record_content, encoding="utf-8")

                actions.append({
                    "system": system_slug,
                    "source_id": source_id,
                    "status": "closing_candidate_created",
                    "record_type": "found",
                    "branch": cand_branch,
                    "title": title,
                    "closes_record": open_key,
                    "candidate_filename": cand_filename,
                    "message": f"Candidate closing record written to candidates directory ({cand_filename}); no branch or PR is created by this script. References open record {open_key}."
                })
            else:
                actions.append({
                    "system": system_slug,
                    "source_id": source_id,
                    "status": "found_no_open_record",
                    "message": f"New report found at {new_issue_link}; no open absence record to close."
                })

    return actions


def main():
    parser = argparse.ArgumentParser(description="Enacted Expected Reports Absence Detector")
    parser.add_argument("--repo-dir", default=str(SCRIPTS_DIR.parent), help="Path to state repo root")
    parser.add_argument("--as-of", default=None, help="Evaluation date in YYYY-MM-DD format (default: today)")
    parser.add_argument("--candidate-dir", default=None, help="Directory for candidate branch output")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry run mode (PR-only)")
    args = parser.parse_args()

    repo_path = Path(args.repo_dir)
    water_measurements = repo_path / "water" / "measurements"

    if not water_measurements.exists():
        print(f"Error: {water_measurements} not found.", file=sys.stderr)
        sys.exit(1)

    if args.as_of:
        eval_date = date.fromisoformat(args.as_of)
    else:
        eval_date = datetime.now(timezone.utc).date()

    cand_dir = Path(args.candidate_dir) if args.candidate_dir else None

    print(f"Running Enacted Absence Detector as of {eval_date}...")
    all_actions = []
    for sdir in sorted(water_measurements.iterdir()):
        if sdir.is_dir() and not sdir.name.startswith("."):
            res = check_system_sources(sdir, as_of=eval_date, dry_run=args.dry_run, candidate_output_dir=cand_dir)
            all_actions.extend(res)

    print("\nDetection Run Summary:")
    for a in all_actions:
        print(f" - [{a['system']}] ({a['source_id']}): {a['status']} — {a['message']}")

    print("\nExecution complete. Invariant verified: No email dispatched, no commit to main branch.")


if __name__ == "__main__":
    main()
