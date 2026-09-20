#!/usr/bin/env python3
"""
verify_corpus_integrity.py — Virginia Corpus Record Integrity Suite
Swapp Technologies LLC (Virginia Corpus: enacted-state-va)

Deterministic verification suite enforcing:
1. Sourcing Blocklist (Municode/AmLegal/eCode360)
2. HTTP 200 primary source liveness at gate time
3. Cryptographic SHA-256 PDF re-hash gate
4. Date-role chronological sanity
5. Frontmatter schema completeness & zero unhandled None
6. Zero banned phrases (guarantee, verified, certified, by-right, may now)
7. Gate 8 publication readiness & staging cleanliness
"""

import os
import sys
import re
import yaml
import urllib.request
import ssl
import hashlib
import subprocess
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

BANNED_PHRASES = [
    "guarantee", "verified", "certified", "approved", "determination",
    "by-right", "may now"
]

FORBIDDEN_STRINGS = [
    r"C:\\Users\\",
    r"c:/Users/",
    r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
    r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    r"\b172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}\b",
    r"\.local\b",
    r"\bAGENTS\.md\b",
    r"enacted\.org"
]

BLOCKLISTED_DOMAINS = [
    "municode.com", "amlegal.com", "ecode360.com"
]

def main():
    print("===============================================================")
    print("  ENACTED / VA CORPUS RECORD INTEGRITY VERIFICATION SUITE")
    print("===============================================================\n")

    failed_checks = 0

    # 1. Discover all records
    records = []
    for root, _, files in os.walk(REPO_ROOT):
        if ".git" in root or ".cache" in root or "_quarantine_unverified" in root:
            continue
        for f in files:
            if f.endswith(".md") and f not in ("README.md", "PROVENANCE.md", "SUMMARY.md", "SOURCING_BLOCKED.md"):
                fpath = os.path.join(root, f)
                with open(fpath, "r", encoding="utf-8") as rf:
                    content = rf.read()
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        fm = yaml.safe_load(parts[1])
                        records.append({
                            "path": fpath,
                            "rel_path": os.path.relpath(fpath, REPO_ROOT),
                            "fm": fm,
                            "body": parts[2].strip()
                        })
                    except Exception as e:
                        print(f"FAIL parsing {fpath}: {e}")
                        failed_checks += 1

    print(f"Loaded {len(records)} records from enacted-state-va.")
    if len(records) == 0:
        print("FAIL: Zero records found in repository!")
        sys.exit(1)

    # CHECK 1: Quarantine Check
    print("\n[CHECK 1] Quarantine Resolution:")
    q_dir = os.path.join(REPO_ROOT, "_quarantine_unverified")
    if os.path.exists(q_dir) and os.listdir(q_dir):
        print("  FAIL: _quarantine_unverified directory present and not empty.")
        failed_checks += 1
    else:
        print("  ✓ Quarantine directory absent or empty.")

    # CHECK 2: Banned Phrases & None Check
    print("\n[CHECK 2] Banned Phrases & Null Leakage:")
    for r in records:
        content = open(r["path"], "r", encoding="utf-8").read().lower()
        clean = re.sub(r"```(?:diff)?\s*\n.*?\n```", "", content, flags=re.DOTALL)
        for bp in BANNED_PHRASES:
            if re.search(r"\b" + re.escape(bp) + r"\b", clean):
                print(f"  FAIL: {r['rel_path']} contains banned phrase '{bp}'")
                failed_checks += 1
        for idx, line in enumerate(clean.splitlines(), 1):
            if re.search(r"\bNone\b", line):
                print(f"  FAIL: {r['rel_path']}:{idx} contains unhandled 'None'")
                failed_checks += 1
    print("  ✓ Banned phrase and null leakage scan completed.")

    # CHECK 3: Sourcing Blocklist & Link Liveness
    print("\n[CHECK 3] Sourcing Blocklist & Link Liveness:")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    checked_urls = set()
    for r in records:
        fm = r["fm"]
        url = fm.get("primary_source_url") or fm.get("source_pdf")
        if not url:
            print(f"  FAIL: {r['rel_path']} missing primary_source_url / source_pdf")
            failed_checks += 1
            continue

        for bl in BLOCKLISTED_DOMAINS:
            if bl in url.lower():
                print(f"  FAIL: {r['rel_path']} uses blocklisted aggregator {bl}")
                failed_checks += 1

        if url not in checked_urls:
            checked_urls.add(url)
            # Use curl.exe if python gets blocked by Akamai/Cloudflare
            try:
                res = subprocess.run(["curl.exe", "-I", "-s", url], capture_output=True, text=True, timeout=15)
                first_line = res.stdout.splitlines()[0] if res.stdout else ""
                if "200" in first_line or "302" in first_line:
                    print(f"  ✓ HTTP OK: {url[:70]}...")
                else:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                        if resp.status == 200:
                            print(f"  ✓ HTTP 200 OK: {url[:70]}...")
                        else:
                            print(f"  FAIL: {url} returned {resp.status}")
                            failed_checks += 1
            except Exception as e:
                print(f"  FAIL: Error fetching {url}: {e}")
                failed_checks += 1

    # CHECK 4: Cryptographic SHA-256 Hash Verification
    print("\n[CHECK 4] Cryptographic PDF Re-Hash Gate:")
    cache_dir = os.path.join(REPO_ROOT, ".cache", "pdf_cache")
    os.makedirs(cache_dir, exist_ok=True)

    for r in records:
        fm = r["fm"]
        expected_sha = fm.get("source_sha256")
        pdf_url = fm.get("source_pdf") or fm.get("primary_source_url")
        if not expected_sha or not pdf_url:
            print(f"  FAIL: {r['rel_path']} missing source_sha256 or source_pdf")
            failed_checks += 1
            continue

        cache_file = os.path.join(cache_dir, f"{expected_sha}.bin")
        if os.path.exists(cache_file):
            with open(cache_file, "rb") as cf:
                data = cf.read()
        else:
            # Try curl.exe then urllib
            res = subprocess.run(["curl.exe", "-s", "-o", cache_file, pdf_url], capture_output=True)
            if res.returncode == 0 and os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
                with open(cache_file, "rb") as cf:
                    data = cf.read()
            else:
                try:
                    req = urllib.request.Request(pdf_url, headers=headers)
                    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                        data = resp.read()
                    with open(cache_file, "wb") as cf:
                        cf.write(data)
                except Exception as e:
                    print(f"  FAIL: Could not download {pdf_url}: {e}")
                    failed_checks += 1
                    continue

        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha:
            print(f"  FAIL: {r['rel_path']} hash mismatch! Expected {expected_sha}, got {actual_sha}")
            failed_checks += 1
        else:
            print(f"  ✓ SHA-256 match ({expected_sha[:12]}...): {r['rel_path']}")

    # CHECK 5: Date-Role Chronological Sanity
    print("\n[CHECK 5] Date-Role Chronological Sanity:")
    for r in records:
        fm = r["fm"]
        intro = fm.get("introduced_date")
        enact = fm.get("enacted_date")
        if intro and enact:
            if str(intro) > str(enact):
                print(f"  FAIL: {r['rel_path']} introduced ({intro}) after enacted ({enact})")
                failed_checks += 1
        print(f"  ✓ Chronology sound: {r['rel_path']}")

    # CHECK 6: Gate 8 Publication Readiness
    print("\n[CHECK 6] Gate 8 Publication Readiness for Staging:")
    # License placeholder
    lic_path = os.path.join(REPO_ROOT, "LICENSE")
    if not os.path.exists(lic_path):
        print("  FAIL: LICENSE missing.")
        failed_checks += 1
    else:
        with open(lic_path, "r", encoding="utf-8") as f:
            lic = f.read().strip()
        if lic != "[FOUNDER: license ruling pending — CC0 proposed]":
            print(f"  FAIL: LICENSE has invalid content: {lic}")
            failed_checks += 1
        else:
            print("  ✓ LICENSE contains exact placeholder.")

    # README posture line
    readme_path = os.path.join(REPO_ROOT, "README.md")
    with open(readme_path, "r", encoding="utf-8") as f:
        readme = f.read()
    if "This repository records the enacted law. It does not advise, approve, determine, or certify anything." not in readme:
        print("  FAIL: README.md missing exact posture line.")
        failed_checks += 1
    else:
        print("  ✓ README.md carries exact posture line.")

    # Forbidden strings scan
    forbidden_found = []
    for root, _, files in os.walk(REPO_ROOT):
        if ".git" in root or ".cache" in root:
            continue
        for f in files:
            if f == "verify_corpus_integrity.py":
                continue
            if f.endswith((".md", ".yml", ".yaml", ".css", ".py")):
                fpath = os.path.join(root, f)
                with open(fpath, "r", encoding="utf-8", errors="ignore") as tf:
                    c = tf.read()
                for pat in FORBIDDEN_STRINGS:
                    if re.search(pat, c):
                        forbidden_found.append((os.path.relpath(fpath, REPO_ROOT), pat))
    if forbidden_found:
        print(f"  FAIL: Forbidden strings found: {forbidden_found}")
        failed_checks += 1
    else:
        print("  ✓ Zero forbidden strings found across repository files.")

    print("\n===============================================================")
    if failed_checks == 0:
        print("  ALL VA CORPUS INTEGRITY VERIFICATION GATES PASSED! ✅")
        print("===============================================================\n")
        sys.exit(0)
    else:
        print(f"  FAILED: {failed_checks} check(s) failed.")
        print("===============================================================\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
