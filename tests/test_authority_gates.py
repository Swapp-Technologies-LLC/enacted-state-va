"""
tests/test_authority_gates.py — Rule 11 Fixture Tests for Authority Contacts Verification

Enforces Rule 11:
Test fixtures never carry real identifiers — use xx/testville, TEST-0001,
https://example.invalid/..., and sha256 of fixture bytes.
"""

import os
import sys
import hashlib
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from verify_corpus_integrity import validate_authority_data, validate_authority_file

FIXTURE_HTML = (
    b"<!DOCTYPE html>\n"
    b"<html>\n"
    b"<head><title>Testville Zoning</title></head>\n"
    b"<body>\n"
    b"<h1>Testville Zoning Division</h1>\n"
    b"<p>For questions regarding zoning compliance, please call the Testville Zoning Division at 555-0199 or email zoning@example.invalid.</p>\n"
    b"<p>Council Clerk Office: 555-0188</p>\n"
    b"</body>\n"
    b"</html>\n"
)
FIXTURE_SHA256 = hashlib.sha256(FIXTURE_HTML).hexdigest()

@pytest.fixture
def fixture_env(tmp_path):
    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_file = cache_dir / f"{FIXTURE_SHA256}.html"
    cached_file.write_bytes(FIXTURE_HTML)

    jur_dir = tmp_path / "xx" / "testville"
    jur_dir.mkdir(parents=True, exist_ok=True)

    base_auth = {
        "zoning_office": {
            "office": "Testville Zoning Division",
            "phone": "555-0199",
            "email": "zoning@example.invalid",
            "web": "https://example.invalid/zoning",
            "source_url": "https://example.invalid/zoning",
            "source_sha256": FIXTURE_SHA256,
            "source_fetched_at": "2026-10-16T00:00:00Z",
            "quoted_sentence": "For questions regarding zoning compliance, please call the Testville Zoning Division at 555-0199 or email zoning@example.invalid."
        }
    }
    return {
        "tmp_path": tmp_path,
        "cache_dir": str(cache_dir),
        "jur_dir": str(jur_dir),
        "base_auth": base_auth,
        "cached_file": str(cached_file)
    }

def test_valid_authority_passes(fixture_env):
    errors = validate_authority_data(fixture_env["base_auth"], "xx/testville", fixture_env["cache_dir"])
    assert errors == [], f"Expected 0 errors, got: {errors}"

def test_missing_file_failure(fixture_env):
    missing_path = os.path.join(fixture_env["jur_dir"], "authority.yaml")
    errors = validate_authority_file(missing_path, fixture_env["cache_dir"])
    assert len(errors) == 1
    assert "file does not exist" in errors[0]

def test_missing_required_field_failure(fixture_env):
    auth = dict(fixture_env["base_auth"])
    auth["zoning_office"] = dict(auth["zoning_office"])
    del auth["zoning_office"]["phone"]

    errors = validate_authority_data(auth, "xx/testville", fixture_env["cache_dir"])
    assert any("missing required field 'phone'" in e for e in errors)

def test_cached_file_missing_failure(fixture_env):
    auth = dict(fixture_env["base_auth"])
    auth["zoning_office"] = dict(auth["zoning_office"])
    fake_sha = hashlib.sha256(b"nonexistent_fixture").hexdigest()
    auth["zoning_office"]["source_sha256"] = fake_sha

    errors = validate_authority_data(auth, "xx/testville", fixture_env["cache_dir"])
    assert any(f"cached file .cache/{fake_sha}.html does not exist" in e for e in errors)

def test_sha256_mismatch_failure(fixture_env):
    # Alter the bytes of the cached file
    with open(fixture_env["cached_file"], "wb") as f:
        f.write(b"<html>tampered content</html>")

    errors = validate_authority_data(fixture_env["base_auth"], "xx/testville", fixture_env["cache_dir"])
    assert any("sha256 mismatch" in e for e in errors)

def test_text_mismatch_failure(fixture_env):
    auth = dict(fixture_env["base_auth"])
    auth["zoning_office"] = dict(auth["zoning_office"])
    auth["zoning_office"]["office"] = "Nonexistent Department of Fiction"

    errors = validate_authority_data(auth, "xx/testville", fixture_env["cache_dir"])
    assert any("office: 'Nonexistent Department of Fiction' does not appear in cached page text" in e for e in errors)

def test_phone_not_in_quoted_sentence_failure(fixture_env):
    auth = dict(fixture_env["base_auth"])
    auth["zoning_office"] = dict(auth["zoning_office"])
    # Quoted sentence that does not contain the phone 555-0199
    auth["zoning_office"]["quoted_sentence"] = "For questions regarding zoning compliance, please call Testville Zoning Division."

    errors = validate_authority_data(auth, "xx/testville", fixture_env["cache_dir"])
    assert any("quoted_sentence: does not contain phone '555-0199'" in e for e in errors)

def test_email_mismatch_failure(fixture_env):
    auth = dict(fixture_env["base_auth"])
    auth["zoning_office"] = dict(auth["zoning_office"])
    auth["zoning_office"]["email"] = "unlisted@example.invalid"

    errors = validate_authority_data(auth, "xx/testville", fixture_env["cache_dir"])
    assert any("email: 'unlisted@example.invalid' does not appear in cached page text" in e for e in errors)

def test_missing_zoning_office_section(fixture_env):
    auth = {"legislative_body": dict(fixture_env["base_auth"]["zoning_office"])}
    errors = validate_authority_data(auth, "xx/testville", fixture_env["cache_dir"])
    assert any("missing required section 'zoning_office'" in e for e in errors)

def test_legislative_body_validation(fixture_env):
    auth = dict(fixture_env["base_auth"])
    auth["legislative_body"] = {
        "office": "Council Clerk Office",
        "phone": "555-0188",
        "web": "https://example.invalid/council",
        "source_url": "https://example.invalid/council",
        "source_sha256": FIXTURE_SHA256,
        "source_fetched_at": "2026-10-16T00:00:00Z",
        "quoted_sentence": "Council Clerk Office: 555-0188"
    }
    errors = validate_authority_data(auth, "xx/testville", fixture_env["cache_dir"])
    assert errors == []

    # Missing field in legislative_body
    del auth["legislative_body"]["office"]
    errors = validate_authority_data(auth, "xx/testville", fixture_env["cache_dir"])
    assert any("legislative_body: missing required field 'office'" in e for e in errors)
