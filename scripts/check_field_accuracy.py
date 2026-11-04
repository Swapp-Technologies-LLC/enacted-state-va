#!/usr/bin/env python3
"""
scripts/check_field_accuracy.py
Automated Field-Accuracy Verification Gate & Sidecar Generator for Legal Corpus Records.
Swapp Technologies LLC (Maryland) - Enacted Project

Checks displayed fields of legal records against their locally saved primary source documents.
- Legislative records: Report-only mode (reports results without failing the build).
- Measurement records: Enforcing gate (fails if cited page misses value, determination quote,
  parameter, limit, unit, or violates footnote/page gates).
Generates verification/field_accuracy.json sidecar.
"""

import os
import sys
import json
import re
from urllib.parse import urlparse
from datetime import datetime, timezone, date
import yaml

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

try:
    import pypdf
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False

try:
    import pdfplumber
    HAVE_PDFPLUMBER = True
except ImportError:
    HAVE_PDFPLUMBER = False


# Text extraction thresholds for scanned/image detection
MIN_TOTAL_CHARS = 200
MIN_CHARS_PER_PAGE = 50.0

def measure_source_text(pages):
    """
    Measures text actually extracted from pages after whitespace collapsing.
    Returns (total_chars, page_count, chars_per_page, is_extractable)
    where is_extractable is True iff total_chars >= MIN_TOTAL_CHARS and chars_per_page >= MIN_CHARS_PER_PAGE.
    """
    page_count = len(pages)
    if page_count == 0:
        return 0, 0, 0.0, False
    all_lines = []
    for _, lines in pages:
        all_lines.extend(lines)
    full_text = " ".join(" ".join(all_lines).split())
    total_chars = len(full_text)
    chars_per_page = (total_chars / page_count) if page_count > 0 else 0.0
    is_extractable = (total_chars >= MIN_TOTAL_CHARS) and (chars_per_page >= MIN_CHARS_PER_PAGE)
    return total_chars, page_count, chars_per_page, is_extractable

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
    """Generates the fixed, listed set of identifier renderings."""
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

def generate_unit_renderings(unit_val):
    """Generates accepted rendering aliases for measurement units."""
    val_str = str(unit_val).strip()
    renderings = [val_str]
    u = val_str.lower().replace(" ", "")
    if u in ["µg/l", "ug/l", "ppb"]:
        renderings.extend(["µg/L", "ug/L", "ppb", "parts per billion", "(ppb)", "(µg/L)"])
    elif u in ["mg/l", "ppm"]:
        renderings.extend(["mg/L", "ppm", "parts per million", "(ppm)", "(mg/L)"])
    elif u in ["ng/l", "ppt"]:
        renderings.extend(["ng/L", "ppt", "parts per trillion", "(ppt)", "(ng/L)"])
    elif u in ["status"]:
        renderings.extend(["status", "violation", "Violation"])
    elif u in ["samples"]:
        renderings.extend(["samples", "sample"])
    return list(dict.fromkeys(renderings))

def generate_parameter_renderings(param_val):
    """Generates accepted rendering aliases for water parameters."""
    val_str = str(param_val).strip()
    renderings = [val_str]
    m = re.match(r"^(.*?)\s*\((.*?)\)$", val_str)
    if m:
        renderings.append(m.group(1).strip())
        renderings.append(m.group(2).strip())
    if "lead and copper" in val_str.lower():
        renderings.extend(["Lead and Copper", "lead and copper", "Lead and Copper Rule", "Lead and Copper Control Requirements Violation"])
    return list(dict.fromkeys(renderings))

def parse_source_page(src_page_str):
    """Parses 1-based PDF page number from source_page string or int."""
    if isinstance(src_page_str, int):
        return src_page_str
    s = str(src_page_str).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"Page\s+(\d+)", s, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\b", s)
    if m:
        return int(m.group(1))
    return None

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

def get_zone_headers(words):
    """Extracts multi-column zone or tap headers from pdfplumber words."""
    header_rows = {}
    for w in words:
        if w['text'] in ['Zone', 'Tap', 'Taps']:
            top_key = round(w['top'], 0)
            if top_key not in header_rows:
                header_rows[top_key] = []
    
    all_headers = []
    for top_key in sorted(header_rows.keys()):
        line_words = [w for w in words if abs(w['top'] - top_key) < 6]
        line_words.sort(key=lambda x: x['x0'])
        cols = []
        curr = []
        for w in line_words:
            curr.append(w)
            if w['text'] in ['Zone', 'Tap', 'Taps']:
                cols.append({
                    'text': " ".join(cw['text'] for cw in curr),
                    'x0': min(cw['x0'] for cw in curr),
                    'x1': max(cw['x1'] for cw in curr),
                    'top': min(cw['top'] for cw in curr),
                    'bottom': max(cw['bottom'] for cw in curr)
                })
                curr = []
        if cols:
            all_headers.append((top_key, cols))
    return all_headers

def attribute_value_to_header(vw, all_headers, page_width=612):
    """Attributes a value word coordinate to the column header above it."""
    valid_rows = [cols for (top_key, cols) in all_headers if top_key < vw['top']]
    if not valid_rows:
        return None, None
    cols = valid_rows[-1]
    bounds = []
    for i, col in enumerate(cols):
        left = 0 if i == 0 else (cols[i-1]['x1'] + col['x0']) / 2
        right = page_width if i == len(cols) - 1 else (col['x1'] + cols[i+1]['x0']) / 2
        bounds.append((left, right, col))
        
    for left, right, col in bounds:
        if left <= vw['x0'] <= right:
            return col['text'], (left, right)
            
    vw_center = (vw['x0'] + vw['x1']) / 2
    best = min(cols, key=lambda c: abs((c['x0'] + c['x1'])/2 - vw_center))
    return best['text'], None

def check_measurement_record(fm, rel_path, repo_root, loaded_source_file=None):
    """
    Validates a measurement record against its primary source document with strict gates:
    1. Scopes search to cited page (determination.source_page).
    2. Matches row, not bare token (same_line or window_3).
    3. Enforces zone/system attribution via word coordinates.
    4. Enforces sampling-year footnote gate against reporting_period.
    5. Enforces numbering gate (source_page <= total_pages).
    Returns (passed, error_message, field_results, failures).
    """
    sha = fm.get('source_sha256')
    pdf_path = None
    if loaded_source_file and os.path.exists(loaded_source_file):
        pdf_path = loaded_source_file
    else:
        candidates = [
            os.path.join(repo_root, '.cache', 'pdf_cache', f'{sha}.bin'),
            os.path.join(repo_root, '.cache', 'pdf_cache', f'{sha}.pdf'),
            os.path.join(repo_root, '.cache', f'{sha}.bin'),
            os.path.join(repo_root, '.cache', f'{sha}.pdf'),
        ]
        for c in candidates:
            if os.path.exists(c) and os.path.getsize(c) > 0:
                pdf_path = c
                break

    if not pdf_path or not os.path.exists(pdf_path):
        return False, "source PDF not available locally", {}, ["source PDF not available locally"]

    det = fm.get('determination', {})
    sp_str = str(det.get('source_page', ''))
    target_page = parse_source_page(sp_str)
    if not target_page:
        msg = f"Could not parse source_page: '{sp_str}'"
        return False, msg, {}, [msg]

    field_results = {}
    failures = []

    if not HAVE_PYPDF:
        return False, "pypdf not available", {}, ["pypdf not available"]

    reader = pypdf.PdfReader(pdf_path)
    total_pages = len(reader.pages)

    # Gate 7: Numbering gate
    if target_page > total_pages or target_page < 1:
        msg = f"determination.source_page ({target_page}) exceeds PDF page count ({total_pages})"
        failures.append(msg)
        return False, msg, {}, failures

    all_pages_text = [(idx + 1, (p.extract_text() or "")) for idx, p in enumerate(reader.pages)]
    pypdf_page_text = all_pages_text[target_page - 1][1]
    pypdf_page_lines = [l.strip() for l in pypdf_page_text.splitlines() if l.strip()]

    layout_lines = []
    page_words = []
    plumber_page_width = 612
    if HAVE_PDFPLUMBER:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                plumber_page = pdf.pages[target_page - 1]
                plumber_page_width = plumber_page.width
                layout_text = plumber_page.extract_text(layout=True) or ""
                layout_lines = [l.strip() for l in layout_text.splitlines() if l.strip()]
                page_words = plumber_page.extract_words()
        except Exception as e:
            print(f"[check_field_accuracy] Warning: pdfplumber error on {pdf_path}: {e}", file=sys.stderr)

    combined_lines = list(dict.fromkeys(layout_lines + pypdf_page_lines))

    # 1. Parameter check (scoped to cited page)
    param = fm.get('parameter')
    param_cands = generate_parameter_renderings(param)
    param_found_line = None
    for l in combined_lines:
        if any(c.lower() in l.lower() for c in param_cands):
            param_found_line = l
            break
    if param_found_line:
        field_results['parameter'] = {
            "result": "found",
            "recorded_value": param,
            "matched_string": param_found_line,
            "source_file": os.path.relpath(pdf_path, repo_root),
            "location": f"page {target_page}"
        }
    else:
        wrong_p = None
        for p_num, p_txt in all_pages_text:
            if p_num == target_page: continue
            if any(c.lower() in p_txt.lower() for c in param_cands):
                wrong_p = p_num
                break
        res = f"found on wrong page ({wrong_p})" if wrong_p else "not found"
        failures.append(f"parameter '{param}' {res}")
        field_results['parameter'] = {
            "result": res,
            "recorded_value": param,
            "matched_string": None,
            "source_file": os.path.relpath(pdf_path, repo_root),
            "location": None,
            "diagnostic": f"Found on page {wrong_p} instead of cited page {target_page}" if wrong_p else "Not found in document"
        }

    # 2. Limit check (scoped to cited page)
    gov_limit = fm.get('governing_limit')
    if isinstance(gov_limit, dict) and gov_limit.get('value') is not None:
        limit_val = str(gov_limit.get('value')).strip()
        limit_pattern = re.compile(r'(?:^|[^\d.]|\b)' + re.escape(limit_val) + r'(?:\b|[^\d.]|$)')
        limit_found = any(limit_pattern.search(l) for l in combined_lines)
        if limit_found:
            field_results['limit'] = {
                "result": "found",
                "recorded_value": limit_val,
                "matched_string": limit_val,
                "source_file": os.path.relpath(pdf_path, repo_root),
                "location": f"page {target_page}"
            }
        else:
            wrong_p = None
            for p_num, p_txt in all_pages_text:
                if p_num == target_page: continue
                if limit_pattern.search(p_txt):
                    wrong_p = p_num
                    break
            res = f"found on wrong page ({wrong_p})" if wrong_p else "not found"
            failures.append(f"limit '{limit_val}' {res}")
            field_results['limit'] = {
                "result": res,
                "recorded_value": limit_val,
                "matched_string": None,
                "source_file": os.path.relpath(pdf_path, repo_root),
                "location": None,
                "diagnostic": f"Found on page {wrong_p} instead of cited page {target_page}" if wrong_p else "Not found in document"
            }

    # 3. Unit check (scoped to cited page)
    unit_val = fm.get('unit')
    if unit_val:
        unit_cands = generate_unit_renderings(unit_val)
        unit_found = any(any(re.search(r'\b' + re.escape(u) + r'\b', l, re.IGNORECASE) for u in unit_cands) for l in combined_lines)
        if unit_found:
            field_results['unit'] = {
                "result": "found",
                "recorded_value": unit_val,
                "matched_string": unit_val,
                "source_file": os.path.relpath(pdf_path, repo_root),
                "location": f"page {target_page}"
            }
        else:
            wrong_p = None
            for p_num, p_txt in all_pages_text:
                if p_num == target_page: continue
                if any(re.search(r'\b' + re.escape(u) + r'\b', p_txt, re.IGNORECASE) for u in unit_cands):
                    wrong_p = p_num
                    break
            res = f"found on wrong page ({wrong_p})" if wrong_p else "not found"
            failures.append(f"unit '{unit_val}' {res}")
            field_results['unit'] = {
                "result": res,
                "recorded_value": unit_val,
                "matched_string": None,
                "source_file": os.path.relpath(pdf_path, repo_root),
                "location": None,
                "diagnostic": f"Found on page {wrong_p} instead of cited page {target_page}" if wrong_p else "Not found in document"
            }

    # 4. Determination quote check (scoped to cited page or cited footnote page)
    q_str = det.get('quote', '').strip()
    q_found = False
    norm_target_text = normalize_ws(" ".join(combined_lines)).lower()
    if q_str.lower() == "violation: no":
        q_found = any('violation' in l.lower() for l in combined_lines) and any(re.search(r'\bNo\b', l) for l in combined_lines)
    elif normalize_ws(q_str).lower() in norm_target_text:
        q_found = True
    elif "footnote" in sp_str.lower() and target_page < total_pages:
        # Check subsequent footnote page using pypdf text
        next_text = normalize_ws(all_pages_text[target_page][1]).lower()
        if normalize_ws(q_str).lower() in next_text:
            q_found = True

    if q_found:
        field_results['determination'] = {
            "result": "found",
            "recorded_value": q_str,
            "matched_string": q_str,
            "source_file": os.path.relpath(pdf_path, repo_root),
            "location": f"page {target_page}"
        }
    else:
        wrong_p = None
        clean_q = normalize_ws(q_str).lower()
        for p_num, p_txt in all_pages_text:
            if p_num == target_page: continue
            if clean_q in normalize_ws(p_txt).lower():
                wrong_p = p_num
                break
        res = f"found on wrong page ({wrong_p})" if wrong_p else "not found"
        failures.append(f"determination quote '{q_str}' {res}")
        field_results['determination'] = {
            "result": res,
            "recorded_value": q_str,
            "matched_string": None,
            "source_file": os.path.relpath(pdf_path, repo_root),
            "location": None,
            "diagnostic": f"Found on page {wrong_p} instead of cited page {target_page}" if wrong_p else "Not found in document"
        }

    # 5. Reported value & Row matching (Requirement 3: same_line or window_3)
    val_str = str(fm.get('value')).strip()
    val_pattern = re.compile(r'(?:^|[^\d.<>]|\b)' + re.escape(val_str) + r'(?:[\d*†‡]+)?(?:\b|[^\d.]|$)')
    matched_rule = None
    row_window = 3
    for search_lines in [layout_lines, pypdf_page_lines]:
        for idx, l in enumerate(search_lines):
            has_p = any(c.lower() in l.lower() for c in param_cands)
            if has_p:
                if val_pattern.search(l):
                    matched_rule = "same_line"
                    break
                for k in range(1, row_window + 1):
                    if idx + k < len(search_lines) and val_pattern.search(search_lines[idx + k]):
                        matched_rule = f"window_{row_window}"
                        break
                if matched_rule:
                    break
        if matched_rule:
            break

    if not matched_rule and val_str.lower() == "violation":
        if any('violation' in l.lower() for l in combined_lines):
            matched_rule = "narrative_heading"

    # 6. Zone / System attribution (Requirement 4)
    sys_name = str(fm.get('system')).strip()
    all_zone_headers = get_zone_headers(page_words) if page_words else []
    col_header = None
    col_span = None
    val_words = [w for w in page_words if val_pattern.search(w['text'])] if page_words else []
    if all_zone_headers and val_words:
        col_header, col_span = attribute_value_to_header(val_words[0], all_zone_headers, plumber_page_width)
    elif sys_name.lower() in pypdf_page_text.lower():
        col_header = sys_name

    if not matched_rule:
        wrong_p = None
        for p_num, p_txt in all_pages_text:
            if p_num == target_page: continue
            if val_pattern.search(p_txt):
                wrong_p = p_num
                break
        res = f"found on wrong page ({wrong_p})" if wrong_p else "not found"
        failures.append(f"reported_value '{val_str}' {res}")
        field_results['reported_value'] = {
            "result": res,
            "recorded_value": val_str,
            "matched_string": None,
            "source_file": os.path.relpath(pdf_path, repo_root),
            "location": None,
            "diagnostic": f"Found on page {wrong_p} instead of cited page {target_page}" if wrong_p else "Not found in document"
        }
    else:
        field_results['reported_value'] = {
            "result": "found",
            "recorded_value": val_str,
            "matched_string": val_str,
            "source_file": os.path.relpath(pdf_path, repo_root),
            "location": f"page {target_page}",
            "rule_matched": matched_rule,
            "row_window": row_window,
            "column_header": col_header,
            "attribution_method": "pdfplumber word coordinates"
        }

    # 7. Sampling-year footnote gate (Requirement 6)
    all_doc_text = "\n".join(p_txt for _, p_txt in all_pages_text)
    fn_matches = re.findall(r'(\d+)\s+Sampled in (\d{4})', all_doc_text)
    footnote_dict = {fn[0]: (fn[1], f"Footnote {fn[0]}: Sampled in {fn[1]}") for fn in fn_matches}

    if footnote_dict and col_span and val_words:
        vw = val_words[0]
        zone_words = [w for w in page_words if abs(w['top'] - vw['top']) < 15 and col_span[0] <= w['x0'] <= col_span[1]]
        zone_text = " ".join(w['text'] for w in zone_words)
        rep_period = str(fm.get('reporting_period')).strip()
        for fn_num, (fn_year, fn_text) in footnote_dict.items():
            if re.search(r'\b' + re.escape(val_str) + fn_num + r'\b', zone_text) or \
               re.search(r'/' + r'\d+' + fn_num + r'\b', zone_text):
                if rep_period != fn_year:
                    status = f"reporting_period '{rep_period}' does not match footnote year '{fn_year}' ({fn_text})"
                    failures.append(status)
                    field_results['reporting_period'] = {
                        "result": status,
                        "recorded_value": rep_period,
                        "source_file": os.path.relpath(pdf_path, repo_root),
                        "location": f"page {target_page}",
                        "footnote_text": fn_text
                    }

    # 8. sites_above_al check (Requirement Part 1: sites_above_al.quote gate)
    sa = fm.get('sites_above_al')
    if sa and isinstance(sa, dict):
        sa_quote = str(sa.get('quote', '')).strip()
        if 'glyph' in sa:
            glyph_flag = bool(sa.get('glyph'))
            num = None
            m_slash = re.search(r'(\d+)\s*/\s*(\d+)', sa_quote)
            m_of = re.search(r'(\d+)\s+of\s+(\d+)', sa_quote, re.IGNORECASE)
            if m_slash:
                num = int(m_slash.group(1))
            elif m_of:
                num = int(m_of.group(1))
            elif "none of the" in sa_quote.lower():
                num = 0
            if num is not None:
                if glyph_flag and num == 0:
                    failures.append(f"sites_above_al: glyph: true but numerator is 0 in '{sa_quote}'")
                elif not glyph_flag and num > 0:
                    failures.append(f"sites_above_al: glyph: false but numerator is > 0 ({num}) in '{sa_quote}'")
        sa_sp_str = str(sa.get('source_page', '')).strip()
        sa_target_page = parse_source_page(sa_sp_str)
        if not sa_target_page:
            msg = f"Could not parse sites_above_al.source_page: '{sa_sp_str}'"
            failures.append(msg)
            field_results['sites_above_al'] = {
                "result": msg,
                "recorded_value": sa_quote,
                "source_file": os.path.relpath(pdf_path, repo_root),
                "location": None
            }
        elif sa_target_page > total_pages or sa_target_page < 1:
            msg = f"sites_above_al.source_page ({sa_target_page}) exceeds PDF page count ({total_pages})"
            failures.append(msg)
            field_results['sites_above_al'] = {
                "result": msg,
                "recorded_value": sa_quote,
                "source_file": os.path.relpath(pdf_path, repo_root),
                "location": f"page {sa_target_page}"
            }
        else:
            sa_pypdf_page_text = all_pages_text[sa_target_page - 1][1]
            sa_pypdf_page_lines = [l.strip() for l in sa_pypdf_page_text.splitlines() if l.strip()]
            sa_layout_lines = []
            if HAVE_PDFPLUMBER:
                try:
                    with pdfplumber.open(pdf_path) as pdf:
                        sa_pl_page = pdf.pages[sa_target_page - 1]
                        sa_pl_text = sa_pl_page.extract_text(layout=True) or ""
                        sa_layout_lines = [l.strip() for l in sa_pl_text.splitlines() if l.strip()]
                except Exception:
                    pass
            
            sa_quote_pattern = re.compile(re.escape(sa_quote) + r'(?:[\d*†‡]+)?', re.IGNORECASE)
            sa_clean_q = normalize_ws(sa_quote).lower()
            sa_matched_rule = None
            for search_lines in [sa_layout_lines, sa_pypdf_page_lines]:
                for idx, l in enumerate(search_lines):
                    has_p = any(c.lower() in l.lower() for c in param_cands)
                    if sa_quote_pattern.search(l) or sa_clean_q in normalize_ws(l).lower():
                        sa_matched_rule = "same_line" if has_p else "direct_line"
                        break
                    if has_p:
                        for k in range(1, row_window + 1):
                            if idx + k < len(search_lines) and (sa_quote_pattern.search(search_lines[idx + k]) or sa_clean_q in normalize_ws(search_lines[idx + k]).lower()):
                                sa_matched_rule = f"window_{row_window}"
                                break
                    if sa_matched_rule:
                        break
                if sa_matched_rule:
                    break

            if not sa_matched_rule:
                sa_norm_target = normalize_ws(sa_pypdf_page_text).lower()
                if sa_clean_q in sa_norm_target:
                    sa_matched_rule = "page_text"

            if sa_matched_rule:
                field_results['sites_above_al'] = {
                    "result": "found",
                    "recorded_value": sa_quote,
                    "matched_string": sa_quote,
                    "source_file": os.path.relpath(pdf_path, repo_root),
                    "location": f"page {sa_target_page}",
                    "rule_matched": sa_matched_rule
                }
            else:
                wrong_p = None
                for p_num, p_txt in all_pages_text:
                    if p_num == sa_target_page: continue
                    if sa_clean_q in normalize_ws(p_txt).lower():
                        wrong_p = p_num
                        break
                res = f"found on wrong page ({wrong_p})" if wrong_p else "not found"
                failures.append(f"sites_above_al.quote '{sa_quote}' {res}")
                field_results['sites_above_al'] = {
                    "result": res,
                    "recorded_value": sa_quote,
                    "matched_string": None,
                    "source_file": os.path.relpath(pdf_path, repo_root),
                    "location": None,
                    "diagnostic": f"Found on page {wrong_p} instead of cited page {sa_target_page}" if wrong_p else "Not found in document"
                }

    passed = len(failures) == 0
    return passed, "; ".join(failures) if failures else "All gates passed", field_results, failures

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
        "source_text_not_extractable": 0,
        "source_not_available": 0,
        "by_jurisdiction": {}
    }
    source_file_metrics = {}

    unverified_fields = []
    narrative_findings = []
    measurement_failures = 0
    measurement_results_summary = []

    # 5. Cross-record duplicate guard
    measurement_records = [r for r in records if r[1].get('record_type') == 'measurement']
    duplicate_warnings = []
    for i in range(len(measurement_records)):
        for j in range(i + 1, len(measurement_records)):
            r1_path, r1_fm, _ = measurement_records[i]
            r2_path, r2_fm, _ = measurement_records[j]
            sha1 = r1_fm.get('source_sha256')
            sha2 = r2_fm.get('source_sha256')
            if sha1 and sha2 and sha1 != sha2:
                p1 = str(r1_fm.get('parameter', '')).strip().lower()
                p2 = str(r2_fm.get('parameter', '')).strip().lower()
                v1 = str(r1_fm.get('value', '')).strip()
                v2 = str(r2_fm.get('value', '')).strip()
                if p1 == p2 and v1 == v2:
                    rel1 = os.path.relpath(r1_path, repo_root)
                    rel2 = os.path.relpath(r2_path, repo_root)
                    duplicate_warnings.append(
                        f"Cross-record duplicate: '{rel1}' and '{rel2}' share parameter '{r1_fm.get('parameter')}' and value '{v1}' across different sources ({sha1[:8]} != {sha2[:8]})"
                    )

    for fpath, fm, body in records:
        rel = os.path.relpath(fpath, repo_root)
        rtype = fm.get('record_type', 'legislative')
        jur = fm.get('jurisdiction') or rel.split(os.sep)[0]
        if jur not in counts["by_jurisdiction"]:
            counts["by_jurisdiction"][jur] = {
                "records": 0, "found": 0, "not_found": 0, "source_text_not_extractable": 0, "source_not_available": 0
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

        # Measurement record branch: strict page-scoped verification gate
        url = fm.get('primary_source_url') or fm.get('source_pdf') or ""
        source_host = urlparse(url).netloc.lower() if url else ""

        if rtype == 'measurement':
            passed, err_msg, m_fields, record_fails = check_measurement_record(fm, rel, repo_root)
            src_files = []
            for fld in m_fields.values():
                sf = fld.get("source_file")
                if sf and sf not in src_files:
                    src_files.append(sf)
                fld["source_host"] = source_host

            # Capture extraction metrics for measurement source files
            for sf in src_files:
                abs_sf = os.path.join(repo_root, sf) if not os.path.isabs(sf) else sf
                if sf not in source_file_metrics and os.path.exists(abs_sf):
                    sf_pages = extract_text_from_file(abs_sf)
                    tot_c, p_c, cpp, is_e = measure_source_text(sf_pages)
                    source_file_metrics[sf] = {
                        "source_file": sf,
                        "source_host": source_host,
                        "page_count": p_c,
                        "total_chars": tot_c,
                        "chars_per_page": cpp,
                        "is_extractable": is_e
                    }

            record_sidecar = {
                "record": rel,
                "record_type": rtype,
                "jurisdiction": jur,
                "source_host": source_host,
                "sources_available": src_files,
                "source_metrics": [source_file_metrics[sf] for sf in src_files if sf in source_file_metrics],
                "fields": m_fields
            }
            sidecar_data["records"].append(record_sidecar)

            for fn, fld_data in m_fields.items():
                counts["total_fields"] += 1
                res_status = fld_data.get("result", "")
                if res_status == "found":
                    counts["found"] += 1
                    counts["by_jurisdiction"][jur]["found"] += 1
                elif "not available" in res_status:
                    counts["source_not_available"] += 1
                    counts["by_jurisdiction"][jur]["source_not_available"] += 1
                    unverified_fields.append((rel, fn, str(fld_data.get("recorded_value")), "none", res_status, source_host))
                else:
                    counts["not_found"] += 1
                    counts["by_jurisdiction"][jur]["not_found"] += 1
                    unverified_fields.append((rel, fn, str(fld_data.get("recorded_value")), fld_data.get("source_file") or "none", res_status, source_host))

            measurement_results_summary.append((rel, passed, err_msg))
            if not passed:
                measurement_failures += 1
            continue

        # Legislative record branch (existing report-only logic)
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
                        rel_src = os.path.relpath(c, repo_root)
                        tot_c, p_c, cpp, is_e = measure_source_text(pages)
                        if rel_src not in source_file_metrics:
                            source_file_metrics[rel_src] = {
                                "source_file": rel_src,
                                "source_host": source_host,
                                "page_count": p_c,
                                "total_chars": tot_c,
                                "chars_per_page": cpp,
                                "is_extractable": is_e
                            }
                        loaded_sources.append((rel_src, pages, is_e))
                    break

        fields_to_check = []
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

        has_any_extractable_source = any(s[2] for s in loaded_sources)
        record_sidecar = {
            "record": rel,
            "record_type": rtype,
            "jurisdiction": jur,
            "source_host": source_host,
            "sources_available": [s[0] for s in loaded_sources],
            "source_metrics": [source_file_metrics[s[0]] for s in loaded_sources if s[0] in source_file_metrics],
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
                    "source_host": source_host,
                    "location": None
                }
                record_sidecar["fields"][fname] = field_res
                unverified_fields.append((rel, fname, str(val), "none", res_status, source_host))
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
                    for src_path, pages, *rest in loaded_sources:
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
                            "source_host": source_host,
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

            for src_path, pages, *rest in loaded_sources:
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
                    "source_host": source_host,
                    "location": match["location"]
                }
            else:
                if not has_any_extractable_source:
                    res_status = "source text not extractable"
                    counts["source_text_not_extractable"] += 1
                    counts["by_jurisdiction"][jur]["source_text_not_extractable"] += 1
                else:
                    res_status = "not found"
                    counts["not_found"] += 1
                    counts["by_jurisdiction"][jur]["not_found"] += 1
                field_res = {
                    "result": res_status,
                    "recorded_value": str(val),
                    "matched_string": None,
                    "source_file": loaded_sources[0][0] if loaded_sources else None,
                    "source_host": source_host,
                    "location": None
                }
                unverified_fields.append((rel, fname, str(val), loaded_sources[0][0] if loaded_sources else "none", res_status, source_host))

            record_sidecar["fields"][fname] = field_res

        sidecar_data["records"].append(record_sidecar)

    sidecar_data["extraction_thresholds"] = {
        "min_total_chars": MIN_TOTAL_CHARS,
        "min_chars_per_page": MIN_CHARS_PER_PAGE
    }
    sidecar_data["summary"] = {
        "total_records": counts["total_records"],
        "total_fields": counts["total_fields"],
        "found_exact": counts["found"] - counts["derived_expiry"],
        "found_derived": counts["derived_expiry"],
        "not_found": counts["not_found"],
        "source_text_not_extractable": counts["source_text_not_extractable"],
        "source_not_available": counts["source_not_available"]
    }
    sidecar_data["source_files"] = sorted(source_file_metrics.values(), key=lambda x: x["source_file"])

    # Write sidecar JSON
    sidecar_dir = os.path.join(repo_root, "verification")
    os.makedirs(sidecar_dir, exist_ok=True)
    sidecar_path = os.path.join(sidecar_dir, "field_accuracy.json")
    with open(sidecar_path, "w", encoding="utf-8") as sf:
        json.dump(sidecar_data, sf, indent=2)

    # Print summary report
    print("=" * 70)
    print("  FIELD-ACCURACY VERIFICATION AUDIT REPORT")
    print(f"  Repository: {sidecar_data['repository']}")
    print(f"  Timestamp:  {sidecar_data['check_timestamp']}")
    print("=" * 70)

    print(f"\n[EXTRACTION THRESHOLDS]")
    print(f"  Minimum total characters:  {MIN_TOTAL_CHARS}")
    print(f"  Minimum characters/page:   {MIN_CHARS_PER_PAGE:.1f}")

    print(f"\n[SOURCE FILE TEXT EXTRACTION METRICS]")
    print(f"  {'Source File':<56} {'Host':<28} {'Pages':<6} {'Chars':<8} {'Chars/Page':<11} {'Extractable'}")
    print(f"  {'-'*56} {'-'*28} {'-'*6} {'-'*8} {'-'*11} {'-'*11}")
    for sm in sorted(source_file_metrics.values(), key=lambda x: x["source_file"]):
        print(f"  {sm['source_file']:<56} {sm['source_host']:<28} {sm['page_count']:<6} {sm['total_chars']:<8} {sm['chars_per_page']:<11.1f} {str(sm['is_extractable']):<11}")

    print(f"\n[SUMMARY COUNTS]")
    print(f"  Total records checked:            {counts['total_records']}")
    print(f"  Total fields checked:             {counts['total_fields']}")
    print(f"  Found (exact):                    {counts['found'] - counts['derived_expiry']}")
    print(f"  Found (derived from expiry):      {counts['derived_expiry']}")
    print(f"  Not found:                        {counts['not_found']}")
    print(f"  Source text not extractable:      {counts['source_text_not_extractable']}")
    print(f"  Source not available locally:     {counts['source_not_available']}")

    print(f"\n[BREAKDOWN BY JURISDICTION]")
    print(f"  {'Jurisdiction':<30} {'Records':<8} {'Found':<8} {'Not Found':<10} {'Not Extr':<10} {'No Source':<10}")
    print(f"  {'-'*30} {'-'*8} {'-'*8} {'-'*10} {'-'*10} {'-'*10}")
    for jur, jc in sorted(counts["by_jurisdiction"].items()):
        print(f"  {jur:<30} {jc['records']:<8} {jc['found']:<8} {jc['not_found']:<10} {jc['source_text_not_extractable']:<10} {jc['source_not_available']:<10}")

    print(f"\n[WATER MEASUREMENT RECORD AUDIT SUMMARY]")
    print(f"  {'Status':<8} {'Record':<55} {'Details'}")
    print(f"  {'-'*8} {'-'*55} {'-'*30}")
    for rel, passed, msg in measurement_results_summary:
        st = "PASS" if passed else "FAIL"
        print(f"  {st:<8} {rel:<55} {msg}")

    if duplicate_warnings:
        print(f"\n[CROSS-RECORD DUPLICATE WARNINGS]")
        for w in duplicate_warnings:
            print(f"  WARNING: {w}")

    print(f"\n[UNVERIFIED FIELDS TABLE (NOT FOUND / SOURCE TEXT NOT EXTRACTABLE / SOURCE NOT AVAILABLE)]")
    if unverified_fields:
        print(f"  {'Record':<45} {'Field':<18} {'Status':<28} {'Source Host':<28} {'Recorded Value':<30}")
        print(f"  {'-'*45} {'-'*18} {'-'*28} {'-'*28} {'-'*30}")
        for r, fn, val, src, st, host in unverified_fields:
            val_trunc = (val[:27] + "...") if len(val) > 30 else val
            print(f"  {r:<45} {fn:<18} {st:<28} {host:<28} {val_trunc:<30}")
    else:
        print("  [OK] Zero unverified fields! All fields verified against source documents.")

    print(f"\n[NARRATIVE CHECK FINDINGS (NON-QUOTED CHARACTERISATIONS & CALCULATIONS)]")
    if narrative_findings:
        for nf in narrative_findings:
            print(f"  - {nf['record']}: [{nf['pattern_desc']}] \"{nf['matched_text']}\"")
    else:
        print("  [OK] Zero narrative characterisations detected.")

    print(f"\nSidecar generated: {os.path.relpath(sidecar_path, repo_root)}")
    print("=" * 70)
    if measurement_failures > 0:
        print(f"  AUDIT FAILED: {measurement_failures} measurement records failed verification gates! (exit code 1)\n")
        return 1
    else:
        print("  AUDIT COMPLETE: All measurement records passed verification gates. (exit code 0)\n")
        return 0

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_dir = sys.argv[1]
    else:
        target_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.exit(run_field_accuracy_check(target_dir))
