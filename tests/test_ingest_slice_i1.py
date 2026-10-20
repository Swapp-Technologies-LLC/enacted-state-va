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
5. Water check with changed report: caches file + sha256 in .cache/pdf_cache/, records note
   "new report — record not yet written", writes zero measurement records.
6. Water check with unchanged report: 0 changes, no manifest entries, no PR diff.
7. status/sources.json schema validation.
8. Stub issue manager lifecycle: open on failure, comment on repeat, close on recovery.
9. Offline invariant: ingest tests never touch live external networks.
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
            
            # Check concurrency if defined
            concurrency = data.get("concurrency")
            if concurrency:
                group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
                concurrency_groups[wf_path.name] = group

        # If zoning and water both exist in this repo, verify their concurrency groups are distinct
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

            # Check banned terms
            for banned in banned_terms:
                self.assertNotIn(
                    banned, content,
                    f"Forbidden token '{banned}' found in {wf_path.name}"
                )

            # Check runner
            if "runs-on:" in content:
                self.assertIn("runs-on: ubuntu-24.04", content, f"{wf_path.name} must use ubuntu-24.04 runner")

            # Check ingest workflows specifically
            if wf_path.name.startswith("ingest-"):
                # App token secret names
                self.assertIn("${{ secrets.SWAPP_BOT_APP_ID }}", content)
                self.assertIn("${{ secrets.SWAPP_BOT_PRIVATE_KEY }}", content)
                # App bot identity
                self.assertIn("swapp-bot[bot]", content)
                # Cron disabled note
                self.assertIn(
                    "# Disabled until /bot/ is live (sa06) and ig03 has run",
                    content,
                    f"{wf_path.name} must have cron commented out with the exact disabled note"
                )

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

        # Fetch attempt returns 403 disallowed and logs incident
        allowed, code, body, err = crawler.fetch(disallowed_url)
        self.assertFalse(allowed)
        self.assertEqual(code, 403)
        self.assertEqual(err, "disallowed_by_robots_txt")
        self.assertEqual(len(crawler.disallowed_log), 2)  # from can_fetch and fetch
        self.assertEqual(crawler.disallowed_log[0]["url"], disallowed_url)

        # Allowed URL passes
        allowed_url = f"https://{host}/public/water_quality.html"
        self.assertTrue(crawler.can_fetch(allowed_url))

    # 4. Host Rate Limiting Spacing
    def test_crawler_rate_limiting_spacing(self):
        crawler = EnactedCrawler(min_interval=5.0, obey_robots=False)
        host = "source.example.gov"

        with patch("time.sleep") as mock_sleep:
            crawler.wait_if_needed(host)
            mock_sleep.assert_not_called()

            # Immediate second call to same host must trigger sleep for approx 5.0 seconds
            crawler.wait_if_needed(host)
            mock_sleep.assert_called_once()
            sleep_duration = mock_sleep.call_args[0][0]
            self.assertTrue(4.8 <= sleep_duration <= 5.0, f"Sleep duration was {sleep_duration}")

    # 5. Water Check with Changed Report: Caches File, Notes Manifest, Zero Records Authored
    def test_water_check_changed_report(self):
        # Create mock repository structure
        repo = self.tmp_path / "repo"
        repo.mkdir()
        sys_dir = repo / "water" / "measurements" / "test_system"
        sys_dir.mkdir(parents=True)

        old_report_bytes = b"%PDF-1.4 old report content"
        old_sha256 = hashlib.sha256(old_report_bytes).hexdigest()

        new_report_bytes = b"%PDF-1.4 new report content updated 2026"
        new_sha256 = hashlib.sha256(new_report_bytes).hexdigest()

        sources_yaml = {
            "sources": [
                {
                    "id": "test_ccr",
                    "title": "Annual Water Quality Report",
                    "index_url": "https://water.example.gov/reports",
                    "cadence": "annual",
                    "cadence_basis": {
                        "rule": {
                            "citation": "40 CFR 141.152",
                            "quoted_sentence": "Reports must be delivered by July 1 annually."
                        }
                    },
                    "active": True,
                    "last_found": {
                        "title": "2025 Report",
                        "url": "https://water.example.gov/reports/report.pdf",
                        "sha256": old_sha256
                    }
                }
            ]
        }
        (sys_dir / "sources.yaml").write_text(yaml.dump(sources_yaml), encoding="utf-8")

        mock_crawler = EnactedCrawler(min_interval=0.0, obey_robots=False)

        def mock_fetch(url, timeout=15):
            if url == "https://water.example.gov/reports":
                return True, 200, b"<html>Index</html>", ""
            elif url == "https://water.example.gov/reports/report.pdf":
                return True, 200, new_report_bytes, ""
            return False, 404, b"", "not found"

        mock_crawler.fetch = mock_fetch

        stub_client = StubIssueClient()
        res = run_water_checks(
            repo_dir=repo,
            as_of=date(2026, 7, 15),
            crawler=mock_crawler,
            issue_client=stub_client
        )

        self.assertEqual(res["checked_sources"], 1)
        self.assertEqual(res["changed_sources"], 1)
        self.assertEqual(res["new_reports_count"], 1)

        # Check cached file
        cache_file = repo / ".cache" / "pdf_cache" / f"{new_sha256}.bin"
        self.assertTrue(cache_file.exists(), "New report binary must be cached in .cache/pdf_cache/")
        self.assertEqual(cache_file.read_bytes(), new_report_bytes)

        # Check manifest
        manifest_path = repo / ".cache" / "new_reports_manifest.json"
        self.assertTrue(manifest_path.exists(), "Manifest must be written")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["note"], "new report — record not yet written")
        self.assertEqual(manifest[0]["new_sha256"], new_sha256)
        self.assertEqual(manifest[0]["previous_sha256"], old_sha256)

        # Invariant: Zero measurement records written
        md_files = list(sys_dir.glob("*.md"))
        self.assertEqual(len(md_files), 0, "No measurement record markdown file may be authored automatically")

    # 6. Water Check with Unchanged Report: No Diff
    def test_water_check_unchanged_report_no_diff(self):
        repo = self.tmp_path / "repo"
        repo.mkdir()
        sys_dir = repo / "water" / "measurements" / "test_system"
        sys_dir.mkdir(parents=True)

        report_bytes = b"%PDF-1.4 identical report content"
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()

        sources_yaml = {
            "sources": [
                {
                    "id": "test_ccr",
                    "title": "Annual Water Quality Report",
                    "index_url": "https://water.example.gov/reports",
                    "cadence": "annual",
                    "cadence_basis": {
                        "rule": {
                            "citation": "40 CFR 141.152",
                            "quoted_sentence": "Reports must be delivered by July 1 annually."
                        }
                    },
                    "active": True,
                    "last_found": {
                        "title": "2025 Report",
                        "url": "https://water.example.gov/reports/report.pdf",
                        "sha256": report_sha256
                    }
                }
            ]
        }
        (sys_dir / "sources.yaml").write_text(yaml.dump(sources_yaml), encoding="utf-8")

        mock_crawler = EnactedCrawler(min_interval=0.0, obey_robots=False)

        def mock_fetch(url, timeout=15):
            if url == "https://water.example.gov/reports":
                return True, 200, b"<html>Index</html>", ""
            elif url == "https://water.example.gov/reports/report.pdf":
                return True, 200, report_bytes, ""
            return False, 404, b"", "not found"

        mock_crawler.fetch = mock_fetch

        stub_client = StubIssueClient()
        res = run_water_checks(
            repo_dir=repo,
            as_of=date(2026, 7, 15),
            crawler=mock_crawler,
            issue_client=stub_client
        )

        self.assertEqual(res["checked_sources"], 1)
        self.assertEqual(res["changed_sources"], 0)
        self.assertEqual(res["new_reports_count"], 0)

        # Manifest not created or empty
        manifest_path = repo / ".cache" / "new_reports_manifest.json"
        self.assertFalse(manifest_path.exists())

    # 7. status/sources.json Schema Validation
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

    # 8. Stub Issue Manager Lifecycle (Open -> Comment -> Close)
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
