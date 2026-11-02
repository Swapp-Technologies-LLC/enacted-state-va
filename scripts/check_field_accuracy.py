#!/usr/bin/env python3
"""
scripts/check_field_accuracy.py
Automated Field-Accuracy Verification Gate & Sidecar Generator for Legal Corpus Records.
Swapp Technologies LLC (Maryland) - Enacted Project

Checks displayed fields of legal records against their locally saved primary source documents.
Report-only mode: reports results without failing the build.
Generates verification/field_accuracy.json sidecar.
"""

import os
import sys
import json
import re
from datetime import datetime, timezone, date
import yaml

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

try:
    import pypdf
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False

# Fixed, listed set of date rendering patterns
DATE_RENDERINGS_LIST = [
    "YYYY-MM-DD",          # 2026-03-02 (ISO)
    "MM/DD/YYYY",          # 03/02/2026 (zero-padded)
    "M/D/YYYY",            # 3/2/2026 (unpadded)
    "M/DD/YYYY",           # 3/02/2026
    "MM/D/YYYY",           # 03/2/2026
    "Month D, YYYY",       # March 2, 2026
    "Month DD, YYYY",      # March 02, 2026
    "Mon D, YYYY",         # Mar 2, 2026
    "Mon. D, YYYY",        # Mar. 2, 2026
    "Mon DD, YYYY",        # Mar 02, 2026
    "Mon. DD, YYYY",       # Mar. 02, 2026
    "D Month YYYY",        # 2 March 2026
    "DD Month YYYY",       # 02 March 2026
    "D Mon YYYY",          # 2 Mar 2026
    "D Mon. YYYY",         # 2 Mar. 2026
    "Month YYYY",          # March 2026
    "Mon YYYY",            # Mar 2026
    "Mon. YYYY",           # Mar. 2026
]

# Patterns for narrative detection (non-quoted editorial characterisations and calculations)
NARRATIVE_PATTERNS = [
    (r"\b\d+\s+days\s+(?:from|after|following)[^.\n\)]*", "interval calculation (e.g. '45 days from...')"),
    (r"\bwithin\s+\d+\s+days\b[^.\n\)]*", "interval calculation (e.g. 'within X days')"),
    (r"\bunanimously\s+passed\b[^.\n\)]*", "voting characterisation ('unanimously passed')"),
    (r"\bunanimously\s+adopted\b[^.\n\)]*", "voting characterisation ('unanimously adopted')"),
    (r"\bpassed\s+unanimously\b[^.\n\)]*", "voting characterisation ('passed unanimously')"),
    (r"\badopted\s+unanimously\b[^.\n\)]*", "voting characterisation ('adopted unanimously')"),
    (r"\bby\s+a\s+vote\s+of\b[^.\n\)]*", "voting tally characterisation ('by a vote of')"),
    (r"\bwithout\s+amendment\b[^.\n\)]*", "amendment characterisation ('without amendment')"),
    (r"\bwith\s+(?:adopted\s+)?amendments?\b[^.\n\)]*", "amendment characterisation ('with amendments')"),
    (r"\bveto\s+overridden\b[^.\n\)]*", "legislative action ('veto overridden')"),
    (r"\bline[- ]item\s+veto\b[^.\n\)]*", "legislative action ('line-item veto')"),
    (r"\bnot\s+enacted\s+as\s+law\b[^.\n\)]*", "status characterisation ('not enacted as law')"),
    (r"\bpending\s+(?:council|committee)\s+review\b[^.\n\)]*", "status characterisation ('pending review')"),
]

def normalize_ws(s):
    """Collapses all whitespace sequences to a single space."""
    return " ".join(str(s).split())

def generate_date_renderings(date_val):
    """Generates the fixed, listed set of renderings for a given date value."""
    val_str = str(date_val).strip()
    renderings = [val_str]
    dt = None
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y"]:
        try:
            dt = datetime.strptime(val_str, fmt)
            break
        except ValueError:
            pass
            
    if dt:
        year = dt.year
        month = dt.month
        day = dt.day
        month_name = dt.strftime("%B")
        month_abbr = dt.strftime("%b")
        candidates = [
            f"{year:04d}-{month:02d}-{day:02d}",
            f"{month:02d}/{day:02d}/{year:04d}",
            f"{month}/{day}/{year:04d}",
            f"{month}/{day:02d}/{year:04d}",
            f"{month:02d}/{day}/{year:04d}",
            f"{month_name} {day}, {year:04d}",
            f"{month_name} {day:02d}, {year:04d}",
            f"{month_abbr} {day}, {year:04d}",
            f"{month_abbr}. {day}, {year:04d}",
            f"{month_abbr} {day:02d}, {year:04d}",
            f"{month_abbr}. {day:02d}, {year:04d}",
            f"{day} {month_name} {year:04d}",
            f"{day:02d} {month_name} {year:04d}",
            f"{day} {month_abbr} {year:04d}",
            f"{day} {month_abbr}. {year:04d}",
            f"{month_name} {year:04d}",
            f"{month_abbr} {year:04d}",
            f"{month_abbr}. {year:04d}"
        ]
        for c in candidates:
            if c not in renderings:
                renderings.append(c)
    return renderings

def generate_identifier_renderings(ident_val, field_name):
    """
    Generates the fixed, listed set of identifier renderings.
    Founder ruling Nov 1, 17:28 ET:
    For DC: B26-0610 <-> 26-610 when tagged as bill (<num type="bill">),
    and A26-0281 <-> 26-281 / D.C. Act 26-281 when tagged as act.
    """
    val_str = str(ident_val).strip()
    renderings = [val_str]
    
    # DC bill rules
    m = re.match(r"^B?(\d+)-0*(\d+)$", val_str)
    if m:
        p1, p2 = m.group(1), m.group(2)
        renderings.extend([
            f"{p1}-{p2}",
            f"{p1}-{int(p2):04d}",
            f"Bill {p1}-{p2}",
            f"Bill No. {p1}-{p2}",
            f"Bill {p1}-{int(p2):04d}",
            f"Bill No. {p1}-{int(p2):04d}",
            f'<num type="bill">{p1}-{p2}</num>'
        ])
        
    # DC act rules
    m_act = re.match(r"^A?(\d+)-0*(\d+)$", val_str)
    if m_act:
        p1, p2 = m_act.group(1), m_act.group(2)
        renderings.extend([
            f"{p1}-{p2}",
            f"{p1}-{int(p2):04d}",
            f"Act {p1}-{p2}",
            f"Act No. {p1}-{p2}",
            f"D.C. Act {p1}-{p2}",
            f"D.C. Act {p1}-{int(p2):04d}",
            f'<num type="act">{p1}-{p2}</num>'
        ])
        
    # Standard bill/ord patterns
    m_ord = re.search(r"(\d+)[-_](\d+)", val_str)
    if m_ord:
        p1, p2 = m_ord.group(1), m_ord.group(2)
        renderings.extend([
            f"{p1}-{p2}",
            f"{int(p1)}-{int(p2)}",
            f"Bill No. {p1}-{p2}",
            f"Bill {p1}-{p2}",
            f"Ordinance No. {p1}-{p2}",
            f"Ordinance {p1}-{p2}",
            f"Ord. No. {p1}-{p2}",
            f"Ord. {p1}-{p2}",
            f"ZTA {p1}-{p2}",
            f"ZTA {p1} -{p2}",
            f"CB-{p1}-{p2}",
            f"CB {p1}-{p2}",
            f"CB-{int(p1):03d}-{p2}",
            f"Council Bill No. {p1}-{p2}",
            f"Council Bill No. {int(p1)}-{p2}",
            f"Council Bill {p1}-{p2}"
        ])
        
    m_num = re.search(r"\b(\d{4,5})\b", val_str)
    if m_num:
        n = m_num.group(1)
        renderings.extend([
            n,
            f"Ordinance No. {n}",
            f"Ordinance {n}",
            f"Ord. No. {n}",
            f"Ord. {n}"
        ])

    return list(dict.fromkeys(renderings))

def extract_text_from_file(file_path):
    """Deterministic text extractor per page from PDF or text/XML/HTML files."""
    pages = []
    if not os.path.exists(file_path):
        return pages
        
    ext = os.path.splitext(file_path)[1].lower()
    is_pdf = ext == '.pdf'
    if not is_pdf:
        try:
            with open(file_path, 'rb') as f:
                head = f.read(5)
                if head.startswith(b'%PDF'):
                    is_pdf = True
        except Exception:
            pass

    if is_pdf:
        if HAVE_PYPDF:
            try:
                reader = pypdf.PdfReader(file_path)
                for page_idx, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    lines = [line.rstrip() for line in text.splitlines()]
                    pages.append((page_idx + 1, lines))
                return pages
            except Exception as e:
                print(f"[check_field_accuracy] Error reading PDF {file_path}: {e}", file=sys.stderr)
                return pages
        else:
            print(f"[check_field_accuracy] Warning: pypdf not available for {file_path}", file=sys.stderr)
            return pages
            
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        lines = [line.rstrip() for line in content.splitlines()]
        pages.append((1, lines))
    except Exception as e:
        print(f"[check_field_accuracy] Error reading text file {file_path}: {e}", file=sys.stderr)
        
    return pages

def find_needle_in_pages(pages, candidates):
    """
    Searches for candidates in extracted pages after whitespace collapsing.
    Returns matched details or None.
    """
    for cand in candidates:
        norm_cand = normalize_ws(cand)
        if not norm_cand:
            continue
            
        for page_num, lines in pages:
            for line_idx, line in enumerate(lines):
                norm_line = normalize_ws(line)
                if norm_cand in norm_line:
                    return {
                        "status": "found",
                        "matched_string": cand,
                        "location": f"page {page_num}, line {line_idx + 1}"
                    }
                    
            page_text = " ".join(normalize_ws(l) for l in lines if l.strip())
            if norm_cand in page_text:
                return {
                    "status": "found",
                    "matched_string": cand,
                    "location": f"page {page_num}"
                }
                
    return None

def check_body_narratives(body_text):
    """Detects unquoted narrative calculations or characterisations in record body."""
    body_unfenced = re.sub(r"```.*?```", "", body_text, flags=re.DOTALL)
    findings = []
    for pattern, desc in NARRATIVE_PATTERNS:
        matches = re.finditer(pattern, body_unfenced, re.IGNORECASE)
        for m in matches:
            findings.append({
                "pattern_desc": desc,
                "matched_text": m.group(0).strip()
            })
    return findings

def run_field_accuracy_check(repo_root):
    pdf_cache = os.path.join(repo_root, ".cache", "pdf_cache")
    cache_dir = os.path.join(repo_root, ".cache")

    records = []
    for root, dirs, files in os.walk(repo_root):
        parts = root.split(os.sep)
        if any(p in parts for p in ['_quarantine_unverified', 'tests', 'node_modules', '.git', 'dev']):
            continue
        for f in files:
            if f.endswith('.md') and not f.startswith(('README', 'PROVENANCE', 'SOURCING_BLOCKED')):
                fpath = os.path.join(root, f)
                with open(fpath, 'r', encoding='utf-8') as fh:
                    text = fh.read()
                if not text.startswith('---'):
                    continue
                parts_text = text.split('---', 2)
                if len(parts_text) < 3:
                    continue
                try:
                    fm = yaml.safe_load(parts_text[1])
                except Exception:
                    continue
                if isinstance(fm, dict):
                    records.append((fpath, fm, parts_text[2]))

    records.sort(key=lambda x: x[0])
    today = date.today()

    sidecar_data = {
        "repository": os.path.basename(os.path.abspath(repo_root)),
        "check_timestamp": datetime.now(timezone.utc).isoformat(),
        "date_renderings_rules": DATE_RENDERINGS_LIST,
        "records": []
    }

    counts = {
        "total_records": len(records),
        "total_fields": 0,
        "found": 0,
        "derived_expiry": 0,
        "not_found": 0,
        "source_not_available": 0,
        "by_jurisdiction": {}
    }

    unverified_fields = []
    narrative_findings = []

    for fpath, fm, body in records:
        rel = os.path.relpath(fpath, repo_root)
        rtype = fm.get('record_type', 'legislative')
        jur = fm.get('jurisdiction') or rel.split(os.sep)[0]
        if jur not in counts["by_jurisdiction"]:
            counts["by_jurisdiction"][jur] = {
                "records": 0, "found": 0, "not_found": 0, "source_not_available": 0
            }
        counts["by_jurisdiction"][jur]["records"] += 1

        # Check narrative patterns in body
        record_narratives = check_body_narratives(body)
        for nf in record_narratives:
            narrative_findings.append({
                "record": rel,
                "pattern_desc": nf["pattern_desc"],
                "matched_text": nf["matched_text"]
            })

        # Find saved source files
        source_shas = [fm.get('source_sha256'), fm.get('status_source_sha256'), fm.get('veto_letter_sha256')]
        loaded_sources = []
        for s in source_shas:
            if not s:
                continue
            candidates = [
                os.path.join(pdf_cache, f"{s}.pdf"),
                os.path.join(pdf_cache, f"{s}.bin"),
                os.path.join(cache_dir, f"{s}.xml"),
                os.path.join(cache_dir, f"{s}.html"),
                os.path.join(cache_dir, f"{s}.bin"),
                os.path.join(cache_dir, f"{s}")
            ]
            for c in candidates:
                if os.path.exists(c) and os.path.getsize(c) > 0:
                    pages = extract_text_from_file(c)
                    if pages:
                        loaded_sources.append((os.path.relpath(c, repo_root), pages))
                    break

        fields_to_check = []
        if rtype == 'legislative':
            if fm.get('title'):
                fields_to_check.append(('title', fm.get('title'), 'text'))
            if fm.get('action_status'):
                fields_to_check.append(('action_status', fm.get('action_status'), 'status'))
            for dfield in ['introduced_date', 'vote_date', 'passed_date', 'adopted_date', 
                           'enacted_date', 'signed_date', 'effective_date', 'effective_date_base',
                           'effective_date_overridden_amendments', 'expires_date', 'published_date', 
                           'public_hearing_date', 'vetoed_date', 'veto_override_date']:
                if fm.get(dfield):
                    fields_to_check.append((dfield, fm.get(dfield), 'date'))
            for ifield in ['bill_number', 'act_number', 'ordinance_number', 'zta_number']:
                if fm.get(ifield):
                    fields_to_check.append((ifield, fm.get(ifield), 'identifier'))
            sponsors = fm.get('sponsors') or fm.get('lead_sponsors')
            if sponsors:
                if isinstance(sponsors, list):
                    for sp in sponsors:
                        fields_to_check.append(('sponsor', sp, 'text'))
                else:
                    fields_to_check.append(('sponsor', sponsors, 'text'))
        elif rtype == 'measurement':
            title = fm.get('title') or fm.get('source_title')
            if title:
                fields_to_check.append(('title', title, 'text'))
            if fm.get('parameter'):
                fields_to_check.append(('parameter', fm.get('parameter'), 'text'))
            if fm.get('value') is not None:
                fields_to_check.append(('reported_value', fm.get('value'), 'text'))
            if fm.get('unit'):
                fields_to_check.append(('unit', fm.get('unit'), 'text'))
            gov_limit = fm.get('governing_limit')
            if isinstance(gov_limit, dict) and gov_limit.get('value') is not None:
                fields_to_check.append(('limit', gov_limit.get('value'), 'text'))
            det = fm.get('determination')
            if isinstance(det, dict):
                if det.get('quote'):
                    fields_to_check.append(('determination', det.get('quote'), 'text'))
                elif det.get('status'):
                    fields_to_check.append(('determination', det.get('status'), 'text'))
            for dfield in ['reporting_period', 'compliance_date', 'file_last_saved']:
                if fm.get(dfield):
                    fields_to_check.append((dfield, fm.get(dfield), 'date'))

        record_sidecar = {
            "record": rel,
            "record_type": rtype,
            "jurisdiction": jur,
            "sources_available": [s[0] for s in loaded_sources],
            "fields": {}
        }

        for fname, val, ftype in fields_to_check:
            counts["total_fields"] += 1
            if not loaded_sources:
                res_status = "source not available locally"
                counts["source_not_available"] += 1
                counts["by_jurisdiction"][jur]["source_not_available"] += 1
                field_res = {
                    "result": res_status,
                    "recorded_value": str(val),
                    "matched_string": None,
                    "source_file": None,
                    "location": None
                }
                record_sidecar["fields"][fname] = field_res
                unverified_fields.append((rel, fname, str(val), "none", res_status))
                continue

            # Special case: derived status (founder ruling Nov 1, 17:28 ET)
            if fname == 'action_status' and str(val).strip().lower() == 'expired':
                exp_date = fm.get('expires_date')
                exp_found = False
                exp_location = None
                exp_source = None
                if exp_date:
                    exp_dt = None
                    try:
                        exp_dt = datetime.strptime(str(exp_date).strip(), "%Y-%m-%d").date()
                    except Exception:
                        pass
                    exp_renderings = generate_date_renderings(exp_date)
                    for src_path, pages in loaded_sources:
                        m_exp = find_needle_in_pages(pages, exp_renderings)
                        if m_exp:
                            exp_found = True
                            exp_location = m_exp["location"]
                            exp_source = src_path
                            break
                    if exp_found and exp_dt and exp_dt < today:
                        res_status = "derived from stated expiry"
                        counts["found"] += 1
                        counts["derived_expiry"] += 1
                        counts["by_jurisdiction"][jur]["found"] += 1
                        field_res = {
                            "result": res_status,
                            "recorded_value": str(val),
                            "matched_string": f"expires_date {exp_date} found before run date",
                            "source_file": exp_source,
                            "location": exp_location
                        }
                        record_sidecar["fields"][fname] = field_res
                        continue

            match = None
            matched_src = None
            if ftype == 'date':
                candidates = generate_date_renderings(val)
            elif ftype == 'identifier':
                candidates = generate_identifier_renderings(val, fname)
            else:
                candidates = [str(val)]

            for src_path, pages in loaded_sources:
                match = find_needle_in_pages(pages, candidates)
                if match:
                    matched_src = src_path
                    break

            if match:
                res_status = "found"
                counts["found"] += 1
                counts["by_jurisdiction"][jur]["found"] += 1
                field_res = {
                    "result": res_status,
                    "recorded_value": str(val),
                    "matched_string": match["matched_string"],
                    "source_file": matched_src,
                    "location": match["location"]
                }
            else:
                res_status = "not found"
                counts["not_found"] += 1
                counts["by_jurisdiction"][jur]["not_found"] += 1
                field_res = {
                    "result": res_status,
                    "recorded_value": str(val),
                    "matched_string": None,
                    "source_file": loaded_sources[0][0] if loaded_sources else None,
                    "location": None
                }
                unverified_fields.append((rel, fname, str(val), loaded_sources[0][0] if loaded_sources else "none", res_status))

            record_sidecar["fields"][fname] = field_res

        sidecar_data["records"].append(record_sidecar)

    # Write sidecar JSON
    sidecar_dir = os.path.join(repo_root, "verification")
    os.makedirs(sidecar_dir, exist_ok=True)
    sidecar_path = os.path.join(sidecar_dir, "field_accuracy.json")
    with open(sidecar_path, "w", encoding="utf-8") as sf:
        json.dump(sidecar_data, sf, indent=2)

    # Print summary report
    print("=" * 70)
    print("  FIELD-ACCURACY VERIFICATION AUDIT REPORT (REPORT-ONLY MODE)")
    print(f"  Repository: {sidecar_data['repository']}")
    print(f"  Timestamp:  {sidecar_data['check_timestamp']}")
    print("=" * 70)
    print(f"\n[SUMMARY COUNTS]")
    print(f"  Total records checked:            {counts['total_records']}")
    print(f"  Total fields checked:             {counts['total_fields']}")
    print(f"  Found (exact):                    {counts['found'] - counts['derived_expiry']}")
    print(f"  Found (derived from expiry):      {counts['derived_expiry']}")
    print(f"  Not found:                        {counts['not_found']}")
    print(f"  Source not available locally:     {counts['source_not_available']}")

    print(f"\n[BREAKDOWN BY JURISDICTION]")
    print(f"  {'Jurisdiction':<30} {'Records':<8} {'Found':<8} {'Not Found':<10} {'No Source':<10}")
    print(f"  {'-'*30} {'-'*8} {'-'*8} {'-'*10} {'-'*10}")
    for jur, jc in sorted(counts["by_jurisdiction"].items()):
        print(f"  {jur:<30} {jc['records']:<8} {jc['found']:<8} {jc['not_found']:<10} {jc['source_not_available']:<10}")

    print(f"\n[UNVERIFIED FIELDS TABLE (NOT FOUND / SOURCE NOT AVAILABLE)]")
    if unverified_fields:
        print(f"  {'Record':<45} {'Field':<18} {'Status':<25} {'Recorded Value':<35}")
        print(f"  {'-'*45} {'-'*18} {'-'*25} {'-'*35}")
        for r, fn, val, src, st in unverified_fields:
            val_trunc = (val[:32] + "...") if len(val) > 35 else val
            print(f"  {r:<45} {fn:<18} {st:<25} {val_trunc:<35}")
    else:
        print("  [OK] Zero unverified fields!")

    print(f"\n[NARRATIVE CHECK FINDINGS (NON-QUOTED CHARACTERISATIONS & CALCULATIONS)]")
    if narrative_findings:
        for nf in narrative_findings:
            print(f"  - {nf['record']}: [{nf['pattern_desc']}] \"{nf['matched_text']}\"")
    else:
        print("  [OK] Zero narrative characterisations detected.")

    print(f"\nSidecar generated: {os.path.relpath(sidecar_path, repo_root)}")
    print("=" * 70)
    print("  AUDIT COMPLETE: Report-only mode finished (exit code 0).\n")
    return 0

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_dir = sys.argv[1]
    else:
        target_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.exit(run_field_accuracy_check(target_dir))
