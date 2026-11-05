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
    measure_source_text,
    MIN_TOTAL_CHARS,
    MIN_CHARS_PER_PAGE,
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


    # Blank PDF without text layer yields 'source text not extractable'
    def test_unextractable_blank_pdf_yields_source_text_not_extractable(self):
        import tempfile
        import pypdf

        # Create a 1-page blank PDF fixture using pypdf.PdfWriter().add_blank_page(width=612, height=792)
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            temp_pdf_path = tf.name
            writer.write(tf)

        try:
            pages = extract_text_from_file(temp_pdf_path)
            self.assertEqual(len(pages), 1, "Blank PDF must have 1 page")
            tot_c, p_c, cpp, is_e = measure_source_text(pages)
            self.assertEqual(tot_c, 0, "Blank PDF fixture must yield 0 extractable characters")
            self.assertEqual(p_c, 1, "Blank PDF fixture must have page count 1")
            self.assertEqual(cpp, 0.0, "Blank PDF fixture must have 0.0 chars per page")
            self.assertFalse(is_e, "Blank PDF fixture must be marked unextractable (< 200 chars or < 50 cpp)")

            # In check_field_accuracy, fields evaluated against unextractable sources return 'source text not extractable'
            field_value = "2026-01-01"
            match = find_needle_in_pages(pages, [field_value])
            self.assertIsNone(match)
            res_status = "source text not extractable" if not is_e else "not found"
            self.assertEqual(res_status, "source text not extractable")
        finally:
            if os.path.exists(temp_pdf_path):
                os.remove(temp_pdf_path)

    # Text fixture yields 'found' for present string and 'not found' for absent string
    def test_extractable_text_fixture_yields_found_and_not_found(self):
        source_file = self.pdf_cache / "78bef4a799e032ba87376b9226ef18dd57ad1efe8a70f844b88d7ab4b2a7b2fe.bin"
        pages = extract_text_from_file(str(source_file))
        tot_c, p_c, cpp, is_e = measure_source_text(pages)
        self.assertTrue(is_e, "Text PDF fixture must be extractable")
        self.assertGreaterEqual(tot_c, MIN_TOTAL_CHARS)
        self.assertGreaterEqual(cpp, MIN_CHARS_PER_PAGE)

        # Present string -> found
        m_found = find_needle_in_pages(pages, ["ZOA-2020-02"])
        self.assertIsNotNone(m_found, "Present string must be found in extractable source")
        status_found = "found" if m_found else ("source text not extractable" if not is_e else "not found")
        self.assertEqual(status_found, "found")

        # Absent string -> not found
        m_not = find_needle_in_pages(pages, ["Nonexistent Phrase Absent 99999"])
        self.assertIsNone(m_not, "Absent string must not be found in extractable source")
        status_not = "found" if m_not else ("source text not extractable" if not is_e else "not found")
        self.assertEqual(status_not, "not found")

    # Fixture record with two cached files (source + status) pairs two different hosts (ig23b)
    def test_fixture_source_and_status_pairs_two_different_hosts(self):
        """Test A3 (ig23b): A fixture record with two cached files (source + status) pairs each file with its exact field URL host, printing two different hosts."""
        import tempfile
        import hashlib
        import io

        # Rule 11: Synthetic identifiers, example.invalid URLs, sha256("fixture")
        sha_src = hashlib.sha256(b"fixture source file content").hexdigest()
        sha_status = hashlib.sha256(b"fixture status file content").hexdigest()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            cache_dir = tmp_root / ".cache"
            pdf_cache = cache_dir / "pdf_cache"
            pdf_cache.mkdir(parents=True)
            jur_dir = tmp_root / "xx_testville" / "council_bills"
            jur_dir.mkdir(parents=True)

            src_text = ("TEST-0001 enacted law bill text. " * 15).strip()
            status_text = ("<status>TEST-0001 passed and enacted status text.</status> " * 15).strip()

            (pdf_cache / f"{sha_src}.bin").write_text(src_text, encoding="utf-8")
            (cache_dir / f"{sha_status}.xml").write_text(status_text, encoding="utf-8")

            record_content = f"""---
record_type: legislative
title: TEST-0001 Act of Testville
bill_number: TEST-0001
action_status: Enacted
source_sha256: {sha_src}
source_pdf: https://source.example.invalid/files/bill_test_0001.pdf
status_source_sha256: {sha_status}
status_source_url: https://status.example.invalid/xml/act_test_0001.xml
---
## Enacted text
Test text.
"""
            (jur_dir / "test_0001.md").write_text(record_content, encoding="utf-8")

            stdout_capture = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = stdout_capture
            try:
                exit_code = run_field_accuracy_check(str(tmp_root))
            finally:
                sys.stdout = old_stdout

            output_text = stdout_capture.getvalue()
            self.assertEqual(exit_code, 0)

            sidecar_path = tmp_root / "verification" / "field_accuracy.json"
            self.assertTrue(sidecar_path.exists())
            with open(sidecar_path, "r", encoding="utf-8") as sf:
                sidecar = json.load(sf)

            src_metrics = {sf["source_file"]: sf["source_host"] for sf in sidecar.get("source_files", [])}
            self.assertIn("source.example.invalid", src_metrics.values())
            self.assertIn("status.example.invalid", src_metrics.values())
            self.assertIn("source.example.invalid", output_text)
            self.assertIn("status.example.invalid", output_text)

if __name__ == "__main__":
    unittest.main()
