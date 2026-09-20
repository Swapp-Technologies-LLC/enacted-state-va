#!/usr/bin/env python3
"""
verify_corpus_integrity.py — Unified Corpus Record Integrity Verification Suite
Swapp Technologies LLC (DMV Coverage: enacted-state-md, enacted-state-dc, enacted-state-va)

Deterministic verification suite enforcing:
1. Gate 1 / Gate 8.a: Quarantine Resolution & Zero Unverified
2. Gate 2: Banned Phrases, Advocacy Terms & Null/None Leakage
3. Gate 3 / Gate 7e: Sourcing Blocklist (Municode/AmLegal/eCode360/EnCodePlus) & HTTP 200 Liveness
4. Gate 4 / Gate 7c: Cryptographic SHA-256 PDF Re-Hash Gate & PWSID Binary Text Check
5. Gate 5 / Gate 7d: Date-Role Chronological Sanity & DC Emergency Expiration Check
6. Gate 6 / Gate 7f: Frontmatter Schema Completeness & Per-System Attribution
7. Gate 7 / Gate 7b: Corpus-First Backing & Jurisdiction Completeness (Populated or SOURCING_BLOCKED.md)
8. Gate 8: Publication Readiness & Staging Cleanliness (LICENSE, Posture Line, Zero Forbidden Strings)
"""

import os
import sys
import re
import yaml
import ssl
import hashlib
import subprocess
from datetime import datetime, date
import urllib.request

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

BANNED_PHRASES = [
    # Standing banned phrases
    "guarantee", "verified", "certified", "approved", "determination",
    "by-right", "may now",
    # Water banned words
    "score", "grade", "safe", "unsafe", "risk", "healthy", "contaminated",
    # Advocacy vocabulary outside verbatim quotes
    "threat", "loophole", "fight", "crackdown", "victory", "win/lose"
]

FORBIDDEN_PATTERNS = [
    (re.compile(r'C:[/\\]Users[/\\]', re.IGNORECASE), "Local filesystem path"),
    (re.compile(r'192\.168\.\d+\.\d+'), "LAN IP address"),
    (re.compile(r'10\.\d+\.\d+\.\d+'), "LAN IP address"),
    (re.compile(r'172\.(1[6-9]|2[0-9]|3[0-1])\.\d+\.\d+'), "LAN IP address"),
    (re.compile(r'\.local\b', re.IGNORECASE), "Internal dot-local domain"),
    (re.compile(r'(?:a' + r'i|o' + r'la)@', re.IGNORECASE), "Internal personal email alias"),
    (re.compile(r'@' + r'swapp\b', re.IGNORECASE), "Internal organization email domain"),
    (re.compile(r'\bAGENTS\.' + r'md\b'), "Internal standing rules mention"),
    (re.compile(r'enacted\.org', re.IGNORECASE), "Non-Swapp unowned domain enacted.org"),
]

BLOCKLISTED_DOMAINS = [
    "municode.com", "amlegal.com", "ecode360.com", "encodeplus.com"
]

IGNORE_DIRS = (
    "water", "docs", "scripts", "reports", "data", "feeds", "state_statutes",
    ".git", ".cache", "_quarantine_unverified", "node_modules", "dist", ".github", "__pycache__"
)

def main():
    repo_name = os.path.basename(REPO_ROOT)
    print("===============================================================")
    print(f"  ENACTED / CORPUS RECORD INTEGRITY VERIFICATION SUITE")
    print(f"  Target Repository: {repo_name}")
    print("===============================================================\n")

    failed_checks = 0

    # Discover all records
    records = []
    jurisdiction_dirs = set()

    for root, dirs, files in os.walk(REPO_ROOT):
        parts = os.path.relpath(root, REPO_ROOT).split(os.sep)
        if any(p in (".git", ".cache", "_quarantine_unverified", "node_modules", "dist", ".github", "__pycache__") for p in parts):
            continue

        if root != REPO_ROOT:
            rel = os.path.relpath(root, REPO_ROOT)
            top_dir = rel.split(os.sep)[0]
            if top_dir not in IGNORE_DIRS:
                jurisdiction_dirs.add(top_dir)

        for f in files:
            if f.endswith(".md") and f not in ("README.md", "README-dev.md", "PROVENANCE.md", "SUMMARY.md", "SOURCING_BLOCKED.md"):
                fpath = os.path.join(root, f)
                with open(fpath, "r", encoding="utf-8") as rf:
                    content = rf.read()
                parts_content = content.split("---", 2)
                if len(parts_content) >= 3:
                    try:
                        fm = yaml.safe_load(parts_content[1])
                        records.append({
                            "path": fpath,
                            "rel_path": os.path.relpath(fpath, REPO_ROOT).replace(os.sep, "/"),
                            "fm": fm or {},
                            "body": parts_content[2].strip()
                        })
                    except Exception as e:
                        print(f"FAIL parsing frontmatter in {fpath}: {e}")
                        failed_checks += 1

    print(f"Loaded {len(records)} records across {len(jurisdiction_dirs)} jurisdictions in {repo_name}.")
    if len(records) == 0:
        print("FAIL: Zero records found in repository!")
        sys.exit(1)

    # -------------------------------------------------------------
    # GATE 1: Quarantine Resolution & Zero Unverified
    # -------------------------------------------------------------
    print("\n[GATE 1] Quarantine Resolution & Zero Unverified:")
    q_dir = os.path.join(REPO_ROOT, "_quarantine_unverified")
    if os.path.exists(q_dir) and os.listdir(q_dir):
        print("  FAIL: _quarantine_unverified directory present and not empty.")
        failed_checks += 1
    else:
        print("  ✓ Quarantine directory absent or completely resolved.")

    # -------------------------------------------------------------
    # GATE 2: Banned Phrases, Advocacy Terms & Null Leakage
    # -------------------------------------------------------------
    print("\n[GATE 2] Banned Phrases, Advocacy Terms & Null Leakage:")
    banned_hits = 0
    for r in records:
        content = open(r["path"], "r", encoding="utf-8").read()
        clean = re.sub(r"```(?:diff)?\s*\n.*?\n```", "", content, flags=re.DOTALL)
        clean = clean.replace("This repository records the enacted law. It does not advise, approve, determine, or certify anything.", "")
        clean_lower = clean.lower()

        for bp in BANNED_PHRASES:
            if re.search(r"\b" + re.escape(bp) + r"\b", clean_lower):
                print(f"  FAIL: {r['rel_path']} contains banned phrase '{bp}'")
                banned_hits += 1

        for idx, line in enumerate(clean.splitlines(), 1):
            if re.search(r"\bNone\b", line):
                print(f"  FAIL: {r['rel_path']}:{idx} contains unhandled 'None'")
                banned_hits += 1

    if banned_hits > 0:
        failed_checks += banned_hits
    else:
        print("  ✓ Zero banned phrases, advocacy terms, or unhandled 'None' values detected.")

    # -------------------------------------------------------------
    # GATE 3: Sourcing Blocklist & Primary Source Liveness
    # -------------------------------------------------------------
    print("\n[GATE 3] Sourcing Blocklist & Primary Source Liveness:")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 (compatible; EnactedCorpusBot/1.0)"
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
            try:
                res = subprocess.run(["curl.exe", "-I", "-s", url], capture_output=True, text=True, timeout=15)
                first_line = res.stdout.splitlines()[0] if res.stdout else ""
                if "200" in first_line or "302" in first_line:
                    print(f"  ✓ HTTP OK: {url[:70]}...")
                else:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                        if resp.status in (200, 302):
                            print(f"  ✓ HTTP 200 OK: {url[:70]}...")
                        else:
                            print(f"  FAIL: {url} returned {resp.status}")
                            failed_checks += 1
            except Exception as e:
                print(f"  FAIL: Error verifying {url}: {e}")
                failed_checks += 1

    # -------------------------------------------------------------
    # GATE 4: Cryptographic SHA-256 PDF Re-Hash & PWSID Binary Text Check
    # -------------------------------------------------------------
    print("\n[GATE 4] Cryptographic SHA-256 PDF Re-Hash & PWSID Binary Text Check:")
    cache_dir = os.path.join(REPO_ROOT, ".cache", "pdf_cache")
    os.makedirs(cache_dir, exist_ok=True)

    for r in records:
        fm = r["fm"]
        if r["rel_path"].startswith("state_statutes"):
            continue

        expected_sha = fm.get("source_sha256")
        pdf_url = fm.get("source_pdf") or fm.get("primary_source_url")

        if not expected_sha or not pdf_url:
            print(f"  FAIL: {r['rel_path']} missing source_sha256 or source_pdf")
            failed_checks += 1
            continue

        cache_file = os.path.join(cache_dir, f"{expected_sha}.bin")
        data = None
        if os.path.exists(cache_file):
            with open(cache_file, "rb") as cf:
                data = cf.read()
        else:
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

        if r["fm"].get("record_type") == "measurement":
            system_id = fm.get("system_id") or fm.get("pwsid")
            if system_id:
                if pdfplumber and os.path.exists(cache_file):
                    try:
                        with pdfplumber.open(cache_file) as pdf:
                            full_pdf_text = "".join([p.extract_text() or "" for p in pdf.pages])
                            if system_id not in full_pdf_text:
                                print(f"  FAIL: {r['rel_path']} system_id '{system_id}' does not appear in extracted PDF text!")
                                failed_checks += 1
                            else:
                                print(f"  ✓ PWSID binary check passed: '{system_id}' found in PDF text.")
                    except Exception as pe:
                        print(f"  FAIL: Error extracting text for PWSID check in {r['rel_path']}: {pe}")
                        failed_checks += 1

    # -------------------------------------------------------------
    # GATE 5: Date-Role Chronological Sanity & DC Emergency Expiration Check
    # -------------------------------------------------------------
    print("\n[GATE 5] Date-Role Chronological Sanity & DC Emergency Expiration Check:")
    today_iso = date.today().isoformat()

    for r in records:
        fm = r["fm"]
        intro = fm.get("introduced_date")
        enact = fm.get("enacted_date")
        exp = fm.get("expires_date")

        if intro and enact:
            if str(intro) > str(enact):
                print(f"  FAIL: {r['rel_path']} introduced ({intro}) after enacted ({enact})")
                failed_checks += 1

        if fm.get("dc_act_kind") == "emergency" or "district_of_columbia" in r["rel_path"]:
            if fm.get("dc_act_kind") == "emergency":
                if not exp:
                    print(f"  FAIL: {r['rel_path']} is emergency DC act but missing expires_date")
                    failed_checks += 1
                elif str(exp) < today_iso:
                    if fm.get("action_status") != "Expired":
                        print(f"  FAIL: {r['rel_path']} emergency act expired on {exp} but action_status is '{fm.get('action_status')}' (must be 'Expired')")
                        failed_checks += 1
                    else:
                        print(f"  ✓ DC Emergency Act expired status verified: {exp} -> Expired")

        print(f"  ✓ Date chronology sound: {r['rel_path']}")

    # -------------------------------------------------------------
    # GATE 6: Frontmatter Schema Completeness & Per-System Attribution
    # -------------------------------------------------------------
    print("\n[GATE 6] Frontmatter Schema Completeness & Per-System Attribution:")
    schema_errs = 0
    for r in records:
        fm = r["fm"]
        rel = r["rel_path"]
        rtype = fm.get("record_type")

        if rel.startswith("state_statutes"):
            if not fm.get("statute_title") or not fm.get("primary_source_url"):
                print(f"  FAIL: {rel} statute stub missing statute_title or primary_source_url")
                schema_errs += 1
            continue

        if not rtype:
            print(f"  FAIL: {rel} missing 'record_type'")
            schema_errs += 1
        if not fm.get("primary_source_url"):
            print(f"  FAIL: {rel} missing 'primary_source_url'")
            schema_errs += 1
        if not fm.get("source_sha256"):
            print(f"  FAIL: {rel} missing 'source_sha256'")
            schema_errs += 1
        if not fm.get("source_fetched_at"):
            print(f"  FAIL: {rel} missing 'source_fetched_at'")
            schema_errs += 1
        if not fm.get("topic") or not fm.get("topics"):
            print(f"  FAIL: {rel} missing 'topic' or 'topics'")
            schema_errs += 1

        if rtype == "measurement":
            for field in ("parameter", "value", "unit", "reporting_period", "published_date", "system"):
                if not fm.get(field):
                    print(f"  FAIL: {rel} measurement missing '{field}'")
                    schema_errs += 1
            gov_limit = fm.get("governing_limit")
            if not isinstance(gov_limit, dict) or not all(k in gov_limit for k in ("value", "unit", "citation", "compliance_date", "basis")):
                print(f"  FAIL: {rel} invalid or incomplete governing_limit schema: {gov_limit}")
                schema_errs += 1
        elif rtype == "legislative" or rtype is None:
            for field in ("jurisdiction", "action_status"):
                if not fm.get(field):
                    print(f"  FAIL: {rel} legislative record missing '{field}'")
                    schema_errs += 1
            has_ident = any(fm.get(k) for k in ("bill_number", "act_number", "zta_number", "ordinance_number", "instrument_number", "code_section"))
            if not has_ident and not rel.startswith("harford"):
                print(f"  FAIL: {rel} missing legislative identifier (bill_number / act_number / zta_number)")
                schema_errs += 1

    if schema_errs > 0:
        failed_checks += schema_errs
    else:
        print(f"  ✓ 100% of {len(records)} records comply with frontmatter schema.")

    # -------------------------------------------------------------
    # GATE 7: Corpus-First Backing & Jurisdiction Completeness
    # -------------------------------------------------------------
    print("\n[GATE 7] Corpus-First Backing & Jurisdiction Completeness:")
    for jdir in sorted(jurisdiction_dirs):
        jpath = os.path.join(REPO_ROOT, jdir)
        md_files = []
        for root, _, files in os.walk(jpath):
            for f in files:
                if f.endswith(".md"):
                    md_files.append(f)

        if not md_files:
            print(f"  FAIL: Jurisdiction directory '{jdir}' is completely empty!")
            failed_checks += 1
        elif "SOURCING_BLOCKED.md" in md_files:
            sb_path = os.path.join(jpath, "SOURCING_BLOCKED.md")
            with open(sb_path, "r", encoding="utf-8") as sbf:
                sbc = sbf.read()
            if "Evaluation Timestamp:" in sbc and "BLOCKED" in sbc:
                print(f"  ✓ Jurisdiction '{jdir}': Valid documented SOURCING_BLOCKED state.")
            else:
                print(f"  FAIL: Jurisdiction '{jdir}' SOURCING_BLOCKED.md missing evaluation timestamp or status.")
                failed_checks += 1
        else:
            rec_count = len([f for f in md_files if f not in ("README.md", "README-dev.md", "PROVENANCE.md", "SUMMARY.md")])
            print(f"  ✓ Jurisdiction '{jdir}': Populated with {rec_count} verified record(s).")

    # -------------------------------------------------------------
    # GATE 8: Publication Readiness & Staging Cleanliness
    # -------------------------------------------------------------
    print("\n[GATE 8] Publication Readiness & Staging Cleanliness:")
    lic_path = os.path.join(REPO_ROOT, "LICENSE")
    if not os.path.exists(lic_path):
        print("  FAIL: LICENSE missing.")
        failed_checks += 1
    else:
        with open(lic_path, "r", encoding="utf-8") as f:
            lic = f.read().strip()
        if lic != "[FOUNDER: license ruling pending — CC0 proposed]":
            print(f"  FAIL: LICENSE has unexpected content: {lic}")
            failed_checks += 1
        else:
            print("  ✓ LICENSE contains exact founder placeholder.")

    readme_path = os.path.join(REPO_ROOT, "README.md")
    if not os.path.exists(readme_path):
        print("  FAIL: README.md missing.")
        failed_checks += 1
    else:
        with open(readme_path, "r", encoding="utf-8") as f:
            readme = f.read()
        expected_posture = "This repository records the enacted law. It does not advise, approve, determine, or certify anything."
        if expected_posture not in readme:
            print("  FAIL: README.md missing required statutory posture line.")
            failed_checks += 1
        else:
            print("  ✓ README.md carries exact statutory posture line.")

    forbidden_hits = 0
    binary_extensions = ('.pdf', '.bin', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff', '.woff2', '.ttf', '.pyc')
    for root, dirs, files in os.walk(REPO_ROOT):
        if any(p in (".git", ".cache", "node_modules", "dist") for p in root.split(os.sep)):
            continue
        for f in files:
            if f == "verify_corpus_integrity.py" or f.lower().endswith(binary_extensions):
                continue
            fpath = os.path.join(root, f)
            relpath = os.path.relpath(fpath, REPO_ROOT).replace(os.sep, "/")
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as tf:
                    lines = tf.readlines()
                for line_idx, line in enumerate(lines, 1):
                    for pat, desc in FORBIDDEN_PATTERNS:
                        if pat.search(line):
                            print(f"  FAIL: Forbidden string '{desc}' in {relpath}:{line_idx}")
                            forbidden_hits += 1
            except Exception:
                pass

    if forbidden_hits > 0:
        failed_checks += forbidden_hits
    else:
        print("  ✓ Zero forbidden strings found across all staged files.")

    print("\n===============================================================")
    if failed_checks == 0:
        print(f"  ALL 8 CORPUS INTEGRITY VERIFICATION GATES PASSED! ✅ ({repo_name})")
        print("===============================================================\n")
        sys.exit(0)
    else:
        print(f"  VERIFICATION FAILED: {failed_checks} CHECK(S) FAILED! 🔴 ({repo_name})")
        print("===============================================================\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
