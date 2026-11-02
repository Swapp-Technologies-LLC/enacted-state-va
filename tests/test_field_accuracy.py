"""
tests/test_field_accuracy.py
Unit & Regression Tests for Field-Accuracy Verification Gate (ig17).
Swapp Technologies LLC (Maryland)
"""

import os
import sys
import json
import unittest
from pathlib import Path
from datetime import date

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from scripts.check_field_accuracy import (
    normalize_ws,
    generate_date_renderings,
    generate_identifier_renderings,
    extract_text_from_file,
    find_needle_in_pages,
    check_body_narratives,
    run_field_accuracy_check,
    DATE_RENDERINGS_LIST
)

class TestFieldAccuracy(unittest.TestCase):
    def setUp(self):
        self.pdf_cache = REPO_DIR / ".cache" / "pdf_cache"

    # 1. Found path on real saved source: Arlington ZOA-2020-02 (78bef4a799e0...)
    def test_found_path_real_source(self):
        source_file = self.pdf_cache / "78bef4a799e032ba87376b9226ef18dd57ad1efe8a70f844b88d7ab4b2a7b2fe.bin"
        self.assertTrue(source_file.exists(), f"Real fixture {source_file} must exist")
        pages = extract_text_from_file(str(source_file))
        self.assertGreater(len(pages), 0, "Source PDF must have extractable pages")

        # In Arlington ZOA-2020-02 source: "ZOA-2020-02"
        res = find_needle_in_pages(pages, ["ZOA-2020-02"])
        self.assertIsNotNone(res)
        self.assertEqual(res["status"], "found")

    # 2. Not found path on real saved source: unstated action_status
    def test_not_found_path_real_source(self):
        source_file = self.pdf_cache / "78bef4a799e032ba87376b9226ef18dd57ad1efe8a70f844b88d7ab4b2a7b2fe.bin"
        pages = extract_text_from_file(str(source_file))

        # Invented text not in source
        res = find_needle_in_pages(pages, ["Invented Board Resolution Clause 999"])
        self.assertIsNone(res, "Invented text must not be found")

    # 3. Source not available locally path
    def test_source_not_available_locally(self):
        missing_source = self.pdf_cache / "nonexistent_source_hash_88888.bin"
        pages = extract_text_from_file(str(missing_source))
        self.assertEqual(len(pages), 0, "Missing source must produce 0 pages")

    # 4. Colon-vs-hyphen title differs -> not found
    def test_colon_vs_hyphen_title_differs(self):
        source_file = self.pdf_cache / "78bef4a799e032ba87376b9226ef18dd57ad1efe8a70f844b88d7ab4b2a7b2fe.bin"
        pages = extract_text_from_file(str(source_file))

        # Test colon vs hyphen mismatch
        title_colon = "ZOA-2020-02: Nonconforming Buildings"
        res_colon = find_needle_in_pages(pages, [title_colon])
        self.assertIsNone(res_colon, "Colon title must not match source text")

    # 5. ISO record date vs verbal date in source -> found
    def test_iso_vs_verbal_date_renderings(self):
        # Real source: Arlington ZOA-2020-02 enacted on September 12, 2020
        source_file = self.pdf_cache / "78bef4a799e032ba87376b9226ef18dd57ad1efe8a70f844b88d7ab4b2a7b2fe.bin"
        pages = extract_text_from_file(str(source_file))

        candidates = generate_date_renderings("2020-09-12")
        self.assertIn("September 12, 2020", candidates)
        res = find_needle_in_pages(pages, candidates)
        self.assertIsNotNone(res, "ISO date 2020-09-12 must match 'September 12, 2020' in source")
        self.assertEqual(res["matched_string"], "September 12, 2020")

    # 6. Computed date absent from source -> not found
    def test_computed_date_absent_from_source(self):
        # Fairfax ZO-112.1-2024-9 carries effective_date: '2024-09-11'
        # The source document only contains the board action date September 10, 2024
        source_file = self.pdf_cache / "c8c406346e3ce328d0719a057479e88773d06af7a4cac05ba928106c345c2c25.bin"
        pages = extract_text_from_file(str(source_file))

        candidates = generate_date_renderings("2024-09-11")
        res = find_needle_in_pages(pages, candidates)
        self.assertIsNone(res, "Computed date absent from source must be not found")

    # 7. Narrative check detects editorial calculations & characterisations
    def test_narrative_sentence_detection(self):
        body = """
        ## Chronology
        - Status: unanimously adopted by the board
        - Term: 30 days after final vote
        """
        findings = check_body_narratives(body)
        descs = [f["pattern_desc"] for f in findings]
        self.assertTrue(any("30 days after" in f["matched_text"] for f in findings))
        self.assertTrue(any("unanimously adopted" in d for d in descs))

    # 8. Sidecar generation and schema integrity
    def test_sidecar_generation(self):
        exit_code = run_field_accuracy_check(str(REPO_DIR))
        self.assertEqual(exit_code, 0, "Field-accuracy audit must return 0 in report-only mode")

        sidecar_path = REPO_DIR / "verification" / "field_accuracy.json"
        self.assertTrue(sidecar_path.exists(), "Sidecar verification/field_accuracy.json must exist")

        with open(sidecar_path, "r", encoding="utf-8") as sf:
            data = json.load(sf)

        self.assertIn("repository", data)
        self.assertIn("records", data)
        self.assertEqual(len(data["records"]), 6)

if __name__ == "__main__":
    unittest.main()
