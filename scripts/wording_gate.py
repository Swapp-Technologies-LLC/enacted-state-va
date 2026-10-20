"""
wording_gate.py
Enacted Source-Check & Water Neutrality Wording Gate.
Enforces the copy invariants defined in docs/ENACTED_SOCIAL_VOICE_2026-10-02.md
and the absence-detection specification:
Never, outside a verbatim quote:
late, overdue, missed, missing, failed, fails, delayed, behind, still, yet, finally,
required to (unless inside the quoted rule), should, must, any second person, we, ?, !.
Zero model calls. Pure deterministic regex scan.
"""

import re
from typing import List, Tuple

# Banned standalone tokens outside verbatim quotes
BANNED_WORDS = [
    "late",
    "overdue",
    "missed",
    "missing",
    "failed",
    "fails",
    "delayed",
    "behind",
    "still",
    "yet",
    "finally",
    "should",
    "must",
    # Second person pronouns
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
    # First person plural pronouns (Enacted speaks as "Enacted", never "we")
    "we",
    "us",
    "our",
    "ours",
    "ourselves",
    # Health/safety evaluative terms (ratified Oct 4, 2026 water rules)
    "safe",
    "unsafe",
    "clean",
    "contaminated",
    "toxic",
    "dangerous",
    "healthy",
    "drinkable",
    # Evaluative comparison terms (computed comparisons forbidden outside quotes)
    "passed",
    "compliant",
    "within",
    "exceeds",
    "above",
    "below",
    "elevated",
    "concerning",
    "boil",
    "filter",
    "flush",
]

# Multi-word phrase outside quoted rules
BANNED_PHRASES = [
    r"\brequired\s+to\b",
    r"\bforever\s+chemicals\b",
    r"\bwithin\s+limits\b",
    r"\btest\s+your\s+water\b",
    r"\bdon't\s+drink\b",
]

BANNED_PUNCTUATION = ["?", "!"]


def strip_verbatim_quotes(text: str) -> str:
    """
    Strips verbatim quotes, code blocks, and markup syntax from markdown/HTML text
    so that only Enacted's original editorial prose is checked.
    """
    # 1. Remove fenced code blocks (```...```)
    cleaned = re.sub(r"```[\s\S]*?```", " ", text)

    # 2. Remove inline code (`...`)
    cleaned = re.sub(r"`[^`]*`", " ", cleaned)

    # 3. Remove HTML head, style, and script tags
    cleaned = re.sub(r"<head[\s\S]*?</head>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<style[\s\S]*?</style>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<script[\s\S]*?</script>", " ", cleaned, flags=re.IGNORECASE)

    # 4. Remove HTML comments (<!-- ... -->) and DOCTYPE declarations (<!DOCTYPE ...>)
    cleaned = re.sub(r"<!--[\s\S]*?-->", " ", cleaned)
    cleaned = re.sub(r"<!DOCTYPE[^>]*>", " ", cleaned, flags=re.IGNORECASE)

    # 5. Remove HTML blockquote and code tags
    cleaned = re.sub(r"<blockquote[\s\S]*?</blockquote>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<code[\s\S]*?</code>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<pre[\s\S]*?</pre>", " ", cleaned, flags=re.IGNORECASE)

    # 5b. Remove quoted substrings in straight, curly quotation marks, or HTML entities
    cleaned = re.sub(r'"[^"]*"', " ", cleaned)
    cleaned = re.sub(r'“[^”]*”', " ", cleaned)
    cleaned = re.sub(r'&ldquo;[\s\S]*?&rdquo;', " ", cleaned)
    cleaned = re.sub(r'&ldquo;[\s\S]*?&rdquo;', " ", cleaned)
    cleaned = re.sub(r'&quot;[^&]*&quot;', " ", cleaned)

    # 5c. Allow ratified glyph labels and rule status phrases
    cleaned = re.sub(r'\bLimit not yet in force\b', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bProposed\.?\s+Not a rule yet\.?\b', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bThe compliance date is still ahead\b', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\babove AL\b', ' ', cleaned, flags=re.IGNORECASE)

    # 6. Remove remaining HTML tags (<tag ...>)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    # 7. Remove Markdown blockquote lines (lines beginning with optional whitespace and >)
    lines = cleaned.splitlines()
    non_quote_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        # Also strip YAML/quoted strings inside cadence_basis or verbatim quotes if in frontmatter
        if any(k in line for k in ('rule:', 'agency_statement:', 'basis:', 'quoted_text:', 'quoted_sentence:', 'citation:')):
            continue
        non_quote_lines.append(line)

    return "\n".join(non_quote_lines)


def scan_wording_gate(text: str) -> Tuple[bool, List[str]]:
    """
    Scans text against the wording gate.
    Returns (True, []) if clean, or (False, [violations]) if banned terms appear.
    """
    prose = strip_verbatim_quotes(text)
    violations = []

    # Check standalone banned words
    for word in BANNED_WORDS:
        pattern = rf"\b{re.escape(word)}\b"
        matches = list(re.finditer(pattern, prose, flags=re.IGNORECASE))
        if matches:
            violations.append(f"banned word '{word}' found ({len(matches)} time(s))")

    # Check multi-word phrases
    for phrase_pattern in BANNED_PHRASES:
        matches = list(re.finditer(phrase_pattern, prose, flags=re.IGNORECASE))
        if matches:
            violations.append(f"banned phrase matching '{phrase_pattern}' found ({len(matches)} time(s))")

    # Check banned punctuation (? and !)
    for punct in BANNED_PUNCTUATION:
        if punct in prose:
            count = prose.count(punct)
            violations.append(f"banned punctuation '{punct}' found ({count} time(s))")

    passed = len(violations) == 0
    return passed, violations


class WordingGateViolationError(ValueError):
    """Raised when text violates the Enacted wording gate."""
    pass


def assert_wording_gate(text: str, context_label: str = "text") -> None:
    """Raises WordingGateViolationError if the text contains any banned words outside quotes."""
    passed, violations = scan_wording_gate(text)
    if not passed:
        raise WordingGateViolationError(
            f"Wording gate violation in {context_label}: " + "; ".join(violations)
        )
