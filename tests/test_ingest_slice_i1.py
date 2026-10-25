#!/usr/bin/env python3
"""
test_ingest_slice_i1.py
Acceptance and Integrity Test Suite for Ingest Slice I1 (Deterministic Workflows & Water Checks).
Swapp Technologies LLC

Tests:
1. Workflows parse as valid YAML with distinct concurrency groups.
2. Workflow hygiene: zero [FOUNDER:, zero PATs (ENACTED_BOT_TOKEN), zero personal emails,
   zero firearms strings, crons commented out with disabled note, ubuntu-24.04 runner,
   actions/create-github-app-token using SWAPP_BOT_APP_ID & SWAPP_BOT_PRIVATE_KEY.
3. Robots.txt: a path disallowed by robots.txt is skipped and logged, never fetched.
4. Rate limiting: enforces >= 5 s spacing between requests to the same host.
5. Discovery: An index fixture lists the known report plus a newer-year file ->
   newer one is cached, manifest note is written, covered for both issue_pattern and year rule.
6. Same-URL changed bytes -> cached and noted in manifest.
7. Absence: Past due + grace, index fetched OK, no current-cycle link -> exactly one absence candidate.
8. Outages: Past due + grace, index unreachable -> source health failure and issue, ZERO absence candidates.
9. Before due: No absence whatever happens.
10. Health commit policy (F3): Two consecutive no-change runs -> no diff in status/sources.json;
    a status flip -> updates status/sources.json.
11. status/sources.json schema validation.
12. Stub issue manager lifecycle: open on failure, comment on repeat, close on recovery.
"""

import os
import sys
import json
import yaml
import time
import shutil
import hashlib
import tempfile
import unittest
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

TESTS_DIR = Path(__file__).resolve().parent
REPO_DIR = TESTS_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from crawler import EnactedCrawler, ENACTED_USER_AGENT
from source_health import (
    load_source_health,
    save_source_health,
    record_source_check,
    manage_source_alert_issues,
    has_meaningful_change,
    StubIssueClient,
    get_iso_now
)
from check_water_sources import run_water_checks, load_all_system_sources


class TestIngestSliceI1(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # 1. Workflow YAML Validity & Concurrency Separation
    def test_workflows_valid_yaml_and_concurrency(self):
        workflows_dir = REPO_DIR / ".github" / "workflows"
        self.assertTrue(workflows_dir.exists(), "Workflows dir must exist")

        workflow_files = list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))
        self.assertTrue(len(workflow_files) >= 2, "Expected at least 2 workflows")

        concurrency_groups = {}
        for wf_path in workflow_files:
            content = wf_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            self.assertIsInstance(data, dict, f"Failed to parse YAML in {wf_path.name}")

            concurrency = data.get("concurrency")
            if concurrency:
                group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
                concurrency_groups[wf_path.name] = group

        zoning_wf = concurrency_groups.get("ingest-zoning.yml")
        water_wf = concurrency_groups.get("ingest-water.yml")
        if zoning_wf and water_wf:
            self.assertNotEqual(
                zoning_wf, water_wf,
                "Zoning and water workflows must have separate concurrency groups so one failure never blocks the other"
            )

    # 2. Workflow Hygiene
    def test_workflow_hygiene(self):
        workflows_dir = REPO_DIR / ".github" / "workflows"
        workflow_files = list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))

        banned_terms = ["[FOUNDER:", "ENACTED_BOT_TOKEN", "hola-there", "firearms"]
        for wf_path in workflow_files:
            content = wf_path.read_text(encoding="utf-8")

            for banned in banned_terms:
                self.assertNotIn(
                    banned, content,
                    f"Forbidden token '{banned}' found in {wf_path.name}"
                )

            if "runs-on:" in content:
                self.assertIn("runs-on: ubuntu-24.04", content, f"{wf_path.name} must use ubuntu-24.04 runner")

            if wf_path.name.startswith("ingest-"):
                self.assertIn("${{ secrets.SWAPP_BOT_APP_ID }}", content)
                self.assertIn("${{ secrets.SWAPP_BOT_PRIVATE_KEY }}", content)
                self.assertIn("swapp-bot[bot]", content)
                self.assertIn(
                    "# Disabled until /bot/ is live (sa06) and ig03 has run",
                    content,
                    f"{wf_path.name} must have cron commented out with the exact disabled note"
                )
                self.assertIn("Upload Health Artifact", content)

    # 3. Robots.txt Compliance
    def test_crawler_robots_txt_disallowed(self):
        crawler = EnactedCrawler(min_interval=0.0)
        host = "water.example.gov"
        robots_content = (
            "User-agent: *\n"
            "Disallow: /reports/\n"
            "Disallow: /internal/\n"
            "Allow: /public/\n"
        )
        crawler.register_robots_txt(host, robots_content)

        disallowed_url = f"https://{host}/reports/2026_ccr.pdf"
        self.assertFalse(crawler.can_fetch(disallowed_url))

        allowed, code, body, err = crawler.fetch(disallowed_url)
        self.assertFalse(allowed)
        self.assertEqual(code, 403)
        self.assertEqual(err, "disallowed_by_robots_txt")
        self.assertEqual(len(crawler.disallowed_log), 2)
        self.assertEqual(crawler.disallowed_log[0]["url"], disallowed_url)

        allowed_url = f"https://{host}/public/water_quality.html"
        self.assertTrue(crawler.can_fetch(allowed_url))

    # 4. Host Rate Limiting Spacing
    def test_crawler_rate_limiting_spacing(self):
        crawler = EnactedCrawler(min_interval=5.0, obey_robots=False)
        host = "source.example.gov"

        with patch("time.sleep") as mock_sleep:
            crawler.wait_if_needed(host)
            mock_sleep.assert_not_called()

            crawler.wait_if_needed(host)
            mock_sleep.assert_called_once()
            sleep_duration = mock_sleep.call_args[0][0]
            self.assertTrue(4.8 <= sleep_duration <= 5.0, f"Sleep duration was {sleep_duration}")

    # 5. Discovery of Newer-Year File: Both Issue Pattern and Year Rule (F1)
    def test_discovery_newer_year_file_cached_and_noted(self):
        repo = self.tmp_path / "repo"
        repo.mkdir()

        # System 1: uses issue_pattern
        sys1_dir = repo / "water" / "measurements" / "sys1_pattern"
        sys1_dir.mkdir(parents=True)
        old_bytes_1 = b"%PDF-1.4 sys1 2024 report"
        new_bytes_1 = b"%PDF-1.4 sys1 2025 report updated"
        new_sha_1 = hashlib.sha256(new_bytes_1).hexdigest()

        src1 = {
            "sources": [
                {
                    "id": "sys1_ccr",
                    "title": "Sys1 Quality Report",
                    "index_url": "https://water1.example.gov/index.html",
                    "cadence": "annual",
                    "cadence_basis": {
                        "rule": {
                            "citation": "40 CFR 141.152",
                            "quoted_sentence": "Reports must be delivered by July 1 annually."
                        }
                    },
                    "issue_pattern": r"(?:{period}[-_ ]*(?:ccr|water[-_ ]*quality[-_ ]*report)|(?:ccr|water[-_ ]*quality[-_ ]*report)[-_ ]*{period})",
                    "active": True,
                    "last_found": {
                        "title": "2024 Report",
                        "url": "https://water1.example.gov/reports/2024_ccr.pdf",
                        "sha256": hashlib.sha256(old_bytes_1).hexdigest()
                    }
                }
            ]
        }
        (sys1_dir / "sources.yaml").write_text(yaml.dump(src1), encoding="utf-8")

        # System 2: uses year rule (no issue_pattern)
        sys2_dir = repo / "water" / "measurements" / "sys2_year_rule"
        sys2_dir.mkdir(parents=True)
        old_bytes_2 = b"%PDF-1.4 sys2 2024 report"
        new_bytes_2 = b"%PDF-1.4 sys2 2025 report updated"
        new_sha_2 = hashlib.sha256(new_bytes_2).hexdigest()

        src2 = {
            "sources": [
                {
                    "id": "sys2_ccr",
                    "title": "Sys2 Annual Report",
                    "index_url": "https://water2.example.gov/index.html",
                    "cadence": "annual",
                    "cadence_basis": {
                        "rule": {
                            "citation": "40 CFR 141.152",
                            "quoted_sentence": "Reports must be delivered by July 1 annually."
                        }
                    },
                    "active": True,
                    "last_found": {
                        "title": "2024 Annual Water Quality Report",
                        "url": "https://water2.example.gov/reports/report_2024.pdf",
                        "sha256": hashlib.sha256(old_bytes_2).hexdigest()
                    }
                }
            ]
        }
        (sys2_dir / "sources.yaml").write_text(yaml.dump(src2), encoding="utf-8")

        mock_crawler = EnactedCrawler(min_interval=0.0, obey_robots=False)

        def mock_fetch(url, timeout=15):
            if url == "https://water1.example.gov/index.html":
                html = (
                    '<html><body>'
                    '<a href="https://water1.example.gov/reports/2024_ccr.pdf">2024 CCR</a>'
                    '<a href="https://water1.example.gov/reports/2025_ccr.pdf">2025 CCR</a>'
                    '</body></html>'
                )
                return True, 200, html.encode("utf-8"), ""
            elif url == "https://water1.example.gov/reports/2025_ccr.pdf":
                return True, 200, new_bytes_1, ""

            elif url == "https://water2.example.gov/index.html":
                html = (
                    '<html><body>'
                    '<a href="https://water2.example.gov/reports/report_2024.pdf">2024 Water Quality Report</a>'
                    '<a href="https://water2.example.gov/reports/report_2025.pdf">2025 Water Quality Report</a>'
                    '</body></html>'
                )
                return True, 200, html.encode("utf-8"), ""
            elif url == "https://water2.example.gov/reports/report_2025.pdf":
                return True, 200, new_bytes_2, ""

            return False, 404, b"", "not found"

        mock_crawler.fetch = mock_fetch

        stub_client = StubIssueClient()
        res = run_water_checks(
            repo_dir=repo,
            as_of=date(2026, 7, 15),
            crawler=mock_crawler,
            issue_client=stub_client
        )

        self.assertEqual(res["checked_sources"], 2)
        self.assertEqual(res["changed_sources"], 2)
        self.assertEqual(res["new_reports_count"], 2)

        # Both binaries cached under .cache/pdf_cache/{sha256}.bin
        cache_file_1 = repo / ".cache" / "pdf_cache" / f"{new_sha_1}.bin"
        self.assertTrue(cache_file_1.exists())
        self.assertEqual(cache_file_1.read_bytes(), new_bytes_1)

        cache_file_2 = repo / ".cache" / "pdf_cache" / f"{new_sha_2}.bin"
        self.assertTrue(cache_file_2.exists())
        self.assertEqual(cache_file_2.read_bytes(), new_bytes_2)

        # Manifest entries verified
        manifest_path = repo / ".cache" / "new_reports_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 2)
        for entry in manifest:
            self.assertEqual(entry["note"], "new report — record not yet written")

        # Zero measurement markdown records authored
        self.assertEqual(len(list(sys1_dir.glob("*.md"))), 0)
        self.assertEqual(len(list(sys2_dir.glob("*.md"))), 0)

    # 6. Same-URL Changed Bytes Detected
    def test_same_url_changed_bytes(self):
        repo = self.tmp_path / "repo"
        repo.mkdir()
        sys_dir = repo / "water" / "measurements" / "test_system"
        sys_dir.mkdir(parents=True)

        old_bytes = b"%PDF-1.4 old content"
        old_sha = hashlib.sha256(old_bytes).hexdigest()
        new_bytes = b"%PDF-1.4 updated content at identical URL"
        new_sha = hashlib.sha256(new_bytes).hexdigest()

        sources_yaml = {
            "sources": [
                {
                    "id": "test_ccr",
                    "title": "Water Quality Report",
                    "cadence": "annual",
                    "active": True,
                    "last_found": {
                        "title": "2025 Report",
                        "url": "https://water.example.gov/report.pdf",
                        "sha256": old_sha
                    }
                }
            ]
        }
        (sys_dir / "sources.yaml").write_text(yaml.dump(sources_yaml), encoding="utf-8")

        mock_crawler = EnactedCrawler(min_interval=0.0, obey_robots=False)

        def mock_fetch(url, timeout=15):
            if url == "https://water.example.gov/report.pdf":
                return True, 200, new_bytes, ""
            return False, 404, b"", "not found"

        mock_crawler.fetch = mock_fetch

        stub_client = StubIssueClient()
        res = run_water_checks(
            repo_dir=repo,
            as_of=date(2026, 7, 15),
            crawler=mock_crawler,
            issue_client=stub_client
        )

        self.assertEqual(res["changed_sources"], 1)
        self.assertEqual(res["new_reports_count"], 1)
        cache_file = repo / ".cache" / "pdf_cache" / f"{new_sha}.bin"
        self.assertTrue(cache_file.exists())
        self.assertEqual(cache_file.read_bytes(), new_bytes)

    # 7. Absence: Past Due + Grace, Index OK, No Current-Cycle Link -> Exactly 1 Candidate (F1)
    def test_absence_past_due_plus_grace_index_ok_no_link(self):
        repo = self.tmp_path / "repo"
        repo.mkdir()
        sys_dir = repo / "water" / "measurements" / "test_system"
        sys_dir.mkdir(parents=True)

        sources_yaml = {
            "sources": [
                {
                    "id": "test_ccr",
                    "title": "Water Quality Report",
                    "index_url": "https://water.example.gov/reports",
                    "cadence": "annual",
                    "cadence_basis": {
                        "rule": {
                            "citation": "40 CFR 141.152(b)",
                            "quoted_sentence": "Reports must be delivered by July 1 each year."
                        }
                    },
                    "expected_by": "July 1 annually",
                    "grace_days": 30,
                    "active": True,
                    "last_found": {
                        "title": "2024 Report",
                        "url": "https://water.example.gov/reports/2024_report.pdf",
                        "sha256": "abcdef123456"
                    }
                }
            ]
        }
        (sys_dir / "sources.yaml").write_text(yaml.dump(sources_yaml), encoding="utf-8")

        mock_crawler = EnactedCrawler(min_interval=0.0, obey_robots=False)

        def mock_fetch(url, timeout=15):
            if url == "https://water.example.gov/reports":
                # Index fetched OK, but only contains old 2024 report link (no 2025 link)
                html = '<html><body><a href="https://water.example.gov/reports/2024_report.pdf">2024 Report</a></body></html>'
                return True, 200, html.encode("utf-8"), ""
            elif url == "https://water.example.gov/reports/2024_report.pdf":
                return True, 200, b"%PDF-1.4 2024 unchanged", ""
            return False, 404, b"", "not found"

        mock_crawler.fetch = mock_fetch

        # As of Aug 15, 2026: past July 1 + 30 days grace (July 31).
        stub_client = StubIssueClient()
        res = run_water_checks(
            repo_dir=repo,
            as_of=date(2026, 8, 15),
            crawler=mock_crawler,
            issue_client=stub_client
        )

        self.assertEqual(res["absence_candidates_count"], 1)
        cand = res["absence_candidates"][0]
        self.assertEqual(cand["source_id"], "test_ccr")
        self.assertEqual(cand["period"], "2025")
        self.assertEqual(cand["status"], "expected_report_not_found")

    # 8. Outages: Past Due + Grace, Index Unreachable -> Health Failure, ZERO Absence Candidates (F1)
    def test_outage_past_due_plus_grace_index_unreachable_no_absence_candidate(self):
        repo = self.tmp_path / "repo"
        repo.mkdir()
        sys_dir = repo / "water" / "measurements" / "test_system"
        sys_dir.mkdir(parents=True)

        sources_yaml = {
            "sources": [
                {
                    "id": "test_ccr",
                    "title": "Water Quality Report",
                    "index_url": "https://water.example.gov/reports",
                    "cadence": "annual",
                    "cadence_basis": {
                        "rule": {
                            "citation": "40 CFR 141.152(b)",
                            "quoted_sentence": "Reports must be delivered by July 1 each year."
                        }
                    },
                    "expected_by": "July 1 annually",
                    "grace_days": 30,
                    "active": True,
                    "last_found": {
                        "title": "2024 Report",
                        "url": "https://water.example.gov/reports/2024_report.pdf",
                        "sha256": "abcdef123456"
                    }
                }
            ]
        }
        (sys_dir / "sources.yaml").write_text(yaml.dump(sources_yaml), encoding="utf-8")

        mock_crawler = EnactedCrawler(min_interval=0.0, obey_robots=False)

        def mock_fetch(url, timeout=15):
            # Index page suffers an outage (HTTP 500)
            if url == "https://water.example.gov/reports":
                return True, 500, b"Server Error", "Internal Server Error"
            return False, 404, b"", "not found"

        mock_crawler.fetch = mock_fetch

        stub_client = StubIssueClient()
        res = run_water_checks(
            repo_dir=repo,
            as_of=date(2026, 8, 15),
            crawler=mock_crawler,
            issue_client=stub_client
        )

        # Invariant: Outages NEVER produce an absence candidate!
        self.assertEqual(res["absence_candidates_count"], 0)
        # Outage opens an issue on source alert
        self.assertEqual(len(stub_client.list_open_issues()), 1)
        self.assertEqual(res["issue_actions"][0]["action"], "opened")

    # 9. Before Due Date: No Absence Whatever Happens (F1)
    def test_before_due_date_no_absence(self):
        repo = self.tmp_path / "repo"
        repo.mkdir()
        sys_dir = repo / "water" / "measurements" / "test_system"
        sys_dir.mkdir(parents=True)

        sources_yaml = {
            "sources": [
                {
                    "id": "test_ccr",
                    "title": "Water Quality Report",
                    "index_url": "https://water.example.gov/reports",
                    "cadence": "annual",
                    "cadence_basis": {
                        "rule": {
                            "citation": "40 CFR 141.152(b)",
                            "quoted_sentence": "Reports must be delivered by July 1 each year."
                        }
                    },
                    "expected_by": "July 1 annually",
                    "grace_days": 30,
                    "active": True,
                    "last_found": {
                        "title": "2024 Report",
                        "url": "https://water.example.gov/reports/2024_report.pdf",
                        "sha256": "abcdef123456"
                    }
                }
            ]
        }
        (sys_dir / "sources.yaml").write_text(yaml.dump(sources_yaml), encoding="utf-8")

        mock_crawler = EnactedCrawler(min_interval=0.0, obey_robots=False)

        def mock_fetch(url, timeout=15):
            if url == "https://water.example.gov/reports":
                return True, 200, b"<html><body>Index</body></html>", ""
            return False, 404, b"", "not found"

        mock_crawler.fetch = mock_fetch

        stub_client = StubIssueClient()
        # Evaluated on June 1, 2026: before July 1 due date
        res = run_water_checks(
            repo_dir=repo,
            as_of=date(2026, 6, 1),
            crawler=mock_crawler,
            issue_client=stub_client
        )

        self.assertEqual(res["absence_candidates_count"], 0)

    # 10. Meaningful Health Change & PR Suppression Policy (F3)
    def test_meaningful_health_change_pr_suppression(self):
        status_path = self.tmp_path / "status" / "sources.json"
        artifact_path = self.tmp_path / "sources-health.json"
        summary_path = self.tmp_path / "summary.md"

        initial_health = {
            "version": "1.0",
            "last_run": "2026-10-20T16:00:00Z",
            "sources": {
                "src_a": {
                    "source_id": "src_a",
                    "url": "https://example.gov/a",
                    "status": "ok",
                    "consecutive_failures": 0,
                    "last_reached": "2026-10-20T16:00:00Z",
                    "last_changed": "2026-09-14T00:00:00Z"
                }
            }
        }
        # Save initial file
        save_source_health(initial_health, status_path, only_if_meaningful=False, artifact_path=artifact_path)
        initial_content = status_path.read_text(encoding="utf-8")

        # Run 2: Unchanged run where only timestamps advance (no status flip, no last_changed move)
        run2_health = {
            "version": "1.0",
            "last_run": "2026-10-21T06:00:00Z",
            "sources": {
                "src_a": {
                    "source_id": "src_a",
                    "url": "https://example.gov/a",
                    "status": "ok",
                    "consecutive_failures": 0,
                    "last_reached": "2026-10-21T06:00:00Z",
                    "last_changed": "2026-09-14T00:00:00Z"
                }
            }
        }
        updated = save_source_health(
            run2_health, status_path, only_if_meaningful=True,
            step_summary_path=summary_path, artifact_path=artifact_path
        )
        self.assertFalse(updated, "status/sources.json must NOT be updated when only timestamps change")
        self.assertEqual(status_path.read_text(encoding="utf-8"), initial_content, "File on disk must remain bit-for-bit identical")
        # Full health was written to artifact and summary
        self.assertTrue(artifact_path.exists())
        self.assertIn("2026-10-21T06:00:00Z", artifact_path.read_text(encoding="utf-8"))
        self.assertTrue(summary_path.exists())

        # Run 3: Status flip occurs (source becomes unreachable)
        run3_health = {
            "version": "1.0",
            "last_run": "2026-10-22T06:00:00Z",
            "sources": {
                "src_a": {
                    "source_id": "src_a",
                    "url": "https://example.gov/a",
                    "status": "unreachable: HTTP 500",
                    "consecutive_failures": 1,
                    "last_reached": "2026-10-21T06:00:00Z",
                    "last_changed": "2026-09-14T00:00:00Z"
                }
            }
        }
        updated_flip = save_source_health(
            run3_health, status_path, only_if_meaningful=True,
            step_summary_path=summary_path, artifact_path=artifact_path
        )
        self.assertTrue(updated_flip, "status/sources.json MUST be updated on a status flip")
        self.assertIn("unreachable: HTTP 500", status_path.read_text(encoding="utf-8"))

    # 11. status/sources.json Schema Validation
    def test_sources_json_schema(self):
        status_file = REPO_DIR / "status" / "sources.json"
        self.assertTrue(status_file.exists(), "status/sources.json must exist in repo")

        data = json.loads(status_file.read_text(encoding="utf-8"))
        self.assertIn("version", data)
        self.assertIn("last_run", data)
        self.assertIn("sources", data)
        self.assertIsInstance(data["sources"], dict)

        for sid, src in data["sources"].items():
            self.assertEqual(src.get("source_id"), sid)
            self.assertIn("url", src)
            self.assertIn("last_reached", src)
            self.assertIn("last_changed", src)
            self.assertIn("consecutive_failures", src)
            self.assertIn("status", src)
            self.assertIsInstance(src["consecutive_failures"], int)

    # 12. Stub Issue Manager Lifecycle (Open -> Comment -> Close)
    def test_stub_issue_manager_lifecycle(self):
        client = StubIssueClient()
        source_id = "test_src"
        url = "https://example.gov/water"

        health = {
            "version": "1.0",
            "last_run": get_iso_now(),
            "sources": {}
        }

        # Step 1: First failure -> opens issue
        record_source_check(source_id, url, success=False, error="HTTP 500", health_data=health)
        actions = manage_source_alert_issues(health, issue_client=client)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "opened")
        self.assertEqual(len(client.list_open_issues()), 1)
        issue_num = actions[0]["issue_number"]

        # Step 2: Repeat failure -> comments on existing issue
        record_source_check(source_id, url, success=False, error="HTTP 503", health_data=health)
        actions = manage_source_alert_issues(health, issue_client=client)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "commented")
        self.assertEqual(len(client.comments), 1)
        self.assertEqual(len(client.list_open_issues()), 1)

        # Step 3: Recovery -> closes issue
        record_source_check(source_id, url, success=True, health_data=health)
        actions = manage_source_alert_issues(health, issue_client=client)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "closed")
        self.assertEqual(len(client.list_open_issues()), 0)


if __name__ == "__main__":
    unittest.main()
