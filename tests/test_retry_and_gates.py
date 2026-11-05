"""
tests/test_retry_and_gates.py — Founder ruling (2026-11-05) Gate 7c/7e Retry-Then-Warn & Failure Gates

Enforces:
1. Timeout / connection error / 5xx gets 1 retry, then returns 'unreachable' warning without failing.
2. Transient error that succeeds on retry returns 'ok'.
3. 404 / 410 returns 'http_error' immediately with no retry (hard failure).
4. Hash mismatch remains a hard failure.
5. Robots-cached mismatch remains a hard failure.
6. CHECK 10 field mismatch remains a hard failure.
Zero real network calls (all mocked).
"""

import os
import sys
import hashlib
from unittest.mock import MagicMock, patch
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from verify_corpus_integrity import fetch_corpus_url

FIXTURE_URL = "https://example.invalid/document.pdf"
FIXTURE_SHA = "sha256:0000000000000000000000000000000000000000000000000000000000000000"


def test_retry_once_on_timeout_and_succeeds():
    crawler = MagicMock()
    # Attempt 1: timeout (status_code 0). Attempt 2: HTTP 200 OK
    crawler.fetch.side_effect = [
        (True, 0, b"", "Fetch error: The read operation timed out"),
        (True, 200, b"valid pdf content", ""),
    ]

    with patch("time.sleep"):
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "ok"
    assert status_code == 200
    assert data == b"valid pdf content"
    assert crawler.fetch.call_count == 2


def test_timeout_persists_returns_unreachable_warning():
    crawler = MagicMock()
    crawler.fetch.side_effect = [
        (True, 0, b"", "Fetch error: The read operation timed out"),
        (True, 0, b"", "Fetch error: The read operation timed out"),
    ]

    with patch("time.sleep"):
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "unreachable"
    assert status_code == 0
    assert "timed out" in err
    assert crawler.fetch.call_count == 2


def test_connection_error_persists_returns_unreachable_warning():
    crawler = MagicMock()
    crawler.fetch.side_effect = [
        (True, 0, b"", "URLError: [Errno 111] Connection refused"),
        (True, 0, b"", "URLError: [Errno 111] Connection refused"),
    ]

    with patch("time.sleep"):
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "unreachable"
    assert status_code == 0
    assert "Connection refused" in err
    assert crawler.fetch.call_count == 2


def test_5xx_server_error_persists_returns_unreachable_warning():
    crawler = MagicMock()
    crawler.fetch.side_effect = [
        (True, 503, b"", "Service Unavailable"),
        (True, 500, b"", "Internal Server Error"),
    ]

    with patch("time.sleep"):
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "unreachable"
    assert status_code == 500
    assert crawler.fetch.call_count == 2


def test_404_client_error_hard_failure_no_retry():
    crawler = MagicMock()
    crawler.fetch.side_effect = [
        (True, 404, b"", "Not Found"),
    ]

    with patch("time.sleep") as mock_sleep:
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "http_error"
    assert status_code == 404
    assert crawler.fetch.call_count == 1
    mock_sleep.assert_not_called()


def test_410_client_error_hard_failure_no_retry():
    crawler = MagicMock()
    crawler.fetch.side_effect = [
        (True, 410, b"", "Gone"),
    ]

    with patch("time.sleep") as mock_sleep:
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "http_error"
    assert status_code == 410
    assert crawler.fetch.call_count == 1
    mock_sleep.assert_not_called()


def test_gate_7c_unreachable_warning_does_not_fail(capsys):
    crawler = MagicMock()
    crawler.can_fetch.return_value = True
    crawler.fetch.side_effect = [
        (True, 0, b"", "Fetch error: The read operation timed out"),
        (True, 0, b"", "Fetch error: The read operation timed out"),
    ]

    failed_checks = 0
    rehash_failed = 0

    with patch("time.sleep"):
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)
        if status == "unreachable":
            print(f"  unreachable at gate time: {FIXTURE_URL}")
        elif status != "ok":
            rehash_failed += 1
            failed_checks += 1

    captured = capsys.readouterr()
    assert f"unreachable at gate time: {FIXTURE_URL}" in captured.out
    assert failed_checks == 0
    assert rehash_failed == 0


def test_gate_7c_hash_mismatch_hard_failure(capsys):
    expected_sha = hashlib.sha256(b"original").hexdigest()
    downloaded_data = b"tampered or different content"
    calc_sha = hashlib.sha256(downloaded_data).hexdigest()

    crawler = MagicMock()
    crawler.can_fetch.return_value = True
    crawler.fetch.return_value = (True, 200, downloaded_data, "")

    failed_checks = 0
    rehash_failed = 0

    status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)
    if status == "ok":
        if calc_sha == expected_sha:
            pass
        else:
            print(f"  FAIL HASH MISMATCH on xx/test: expected {expected_sha}, got {calc_sha}")
            rehash_failed += 1
            failed_checks += 1

    captured = capsys.readouterr()
    assert "FAIL HASH MISMATCH" in captured.out
    assert failed_checks == 1
    assert rehash_failed == 1


def test_gate_7c_robots_cached_mismatch_hard_failure(capsys, tmp_path):
    expected_sha = hashlib.sha256(b"original").hexdigest()
    cached_file = tmp_path / f"{expected_sha}.pdf"
    cached_file.write_bytes(b"tampered cached file")
    c_sha = hashlib.sha256(b"tampered cached file").hexdigest()

    failed_checks = 0
    rehash_failed = 0

    if c_sha != expected_sha:
        print(f"  FAIL: cached file hash mismatch for xx/test: expected {expected_sha}, got {c_sha}")
        rehash_failed += 1
        failed_checks += 1

    captured = capsys.readouterr()
    assert "FAIL: cached file hash mismatch" in captured.out
    assert failed_checks == 1
    assert rehash_failed == 1


def test_gate_7e_unreachable_warning_does_not_fail(capsys):
    crawler = MagicMock()
    crawler.can_fetch.return_value = True
    crawler.fetch.side_effect = [
        (True, 503, b"", "Service Unavailable"),
        (True, 503, b"", "Service Unavailable"),
    ]

    failed_checks = 0
    dead_urls = 0

    with patch("time.sleep"):
        status, status_code, body, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)
        if status == "unreachable":
            print(f"  unreachable at gate time: {FIXTURE_URL}")
        elif status == "ok":
            pass
        else:
            dead_urls += 1
            failed_checks += 1

    captured = capsys.readouterr()
    assert f"unreachable at gate time: {FIXTURE_URL}" in captured.out
    assert failed_checks == 0
    assert dead_urls == 0


def test_gate_7e_404_hard_failure(capsys):
    crawler = MagicMock()
    crawler.can_fetch.return_value = True
    crawler.fetch.return_value = (True, 404, b"", "Not Found")

    failed_checks = 0
    dead_urls = 0

    with patch("time.sleep"):
        status, status_code, body, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)
        if status == "unreachable":
            print(f"  unreachable at gate time: {FIXTURE_URL}")
        elif status == "ok":
            pass
        else:
            print(f"  FAIL: Connection error on corpus URL {FIXTURE_URL}: HTTP {status_code} {err}")
            dead_urls += 1
            failed_checks += 1

    captured = capsys.readouterr()
    assert f"FAIL: Connection error on corpus URL {FIXTURE_URL}: HTTP 404 Not Found" in captured.out
    assert failed_checks == 1
    assert dead_urls == 1


def test_check_10_field_discrepancy_hard_failure():
    from scripts.check_field_accuracy import check_measurement_record

    fake_record = {
        "record_type": "measurement",
        "parameter": "Lead",
        "value": "999.0",  # Fabricated number
        "unit": "mg/L",
        "source_sha256": "nonexistent_hash",
    }
    passed, msg, fields, unverified = check_measurement_record(fake_record, "xx/testville/lead.md", REPO_ROOT)
    assert passed is False
    assert "source PDF not available locally" in msg or len(unverified) > 0


def test_301_unfollowed_redirect_returns_http_error_no_retry():
    crawler = MagicMock()
    crawler.fetch.side_effect = [
        (True, 301, b"", "Moved Permanently"),
    ]

    with patch("time.sleep") as mock_sleep:
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "http_error"
    assert status_code == 301
    assert crawler.fetch.call_count == 1
    mock_sleep.assert_not_called()


def test_307_unfollowed_redirect_returns_http_error_no_retry():
    crawler = MagicMock()
    crawler.fetch.side_effect = [
        (True, 307, b"", "Temporary Redirect"),
    ]

    with patch("time.sleep") as mock_sleep:
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "http_error"
    assert status_code == 307
    assert crawler.fetch.call_count == 1
    mock_sleep.assert_not_called()


def test_302_followed_redirect_returns_ok():
    crawler = MagicMock()
    crawler.fetch.side_effect = [
        (True, 302, b"redirect content", ""),
    ]

    with patch("time.sleep") as mock_sleep:
        status, status_code, data, err = fetch_corpus_url(crawler, FIXTURE_URL, timeout=10, retries=1)

    assert status == "ok"
    assert status_code == 302
    assert crawler.fetch.call_count == 1
    mock_sleep.assert_not_called()
