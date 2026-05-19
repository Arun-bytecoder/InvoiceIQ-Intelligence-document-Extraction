"""
Invoice Extraction Pipeline
Core system for: invoice detection, field extraction, table parsing, validation.
Minimal LLM usage — primary reliance on pdfplumber, regex heuristics, layout analysis.

Fixes applied:
  Fix 1 — Invoice number regex: tightened patterns to avoid capturing partial words
  Fix 2 — extract_party_names: three-strategy parser handles two-column PDF layouts
           where pdfplumber merges "FROM: TO:" onto one line and both company names
           onto the next line as a single concatenated string
  Fix 3 — detect_currency: confidence boost when symbol appears 10+ times or
           currency code appears explicitly in text
"""

import re
import json
import math
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

import pdfplumber
import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class LineItem:
    description: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    tax_amount: float = 0.0
    discount_amount: float = 0.0
    line_total: float = 0.0
    confidence: float = 0.0


@dataclass
class ExtractedInvoice:
    invoice_id: str = ""
    invoice_number: str = ""
    seller_name: str = ""
    buyer_name: str = ""
    issue_date: str = ""
    currency: str = "USD"
    subtotal: float = 0.0
    tax_amount: float = 0.0
    discount_amount: float = 0.0
    total_amount: float = 0.0
    payment_terms_days: int = 30
    page_start: int = 0
    page_end: int = 0
    line_items: List[LineItem] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    raw_text: str = ""


@dataclass
class DocumentResult:
    document_id: str
    document_type: str
    invoice_count: int
    invoices: List[ExtractedInvoice]
    processing_notes: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────
# Regex patterns
# ─────────────────────────────────────────────

# FIX 1: tightened — most specific patterns first, require structured token shape
INVOICE_NUMBER_PATTERNS = [
    r'invoice\s*#\s*[:\s]*([A-Z]{2,5}[-/]\d{4}[-/]\d+)',
    r'invoice\s*(?:no|number|num|#)[.:\s]*([A-Z0-9/_-]{4,25})',
    r'(?:inv|bill|si|rec)[.\s#:_/-]+([A-Z0-9]{2,5}[-/]\d{4}[-/]\d+)',
    r'#\s*([A-Z]{2,5}[-/]?\d{4,10})',
    r'\b(INV[-/]?\d{4,10})\b',
    r'\b(INVOICE[-/]?\d{4,10})\b',
]

DATE_PATTERNS = [
    r'\b(\d{4}[-/]\d{2}[-/]\d{2})\b',
    r'\b(\d{2}[-/]\d{2}[-/]\d{4})\b',
    r'\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})\b',
    r'\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b',
]

CURRENCY_PATTERNS = {
    'USD': [r'\$', r'\bUSD\b', r'\bUS\$'],
    'EUR': [r'€', r'\bEUR\b'],
    'GBP': [r'£', r'\bGBP\b'],
    'CAD': [r'CA\$', r'\bCAD\b'],
    'AUD': [r'A\$', r'\bAUD\b'],
    'JPY': [r'¥', r'\bJPY\b', r'\bYEN\b'],
    'CHF': [r'\bCHF\b', r'\bFr\b'],
    'SGD': [r'S\$', r'\bSGD\b'],
}

AMOUNT_PATTERN = r'[\$€£¥]?\s*([\d,]+\.?\d{0,2})'

PAYMENT_TERMS_PATTERNS = [
    r'net\s+(\d+)\s*(?:days?)?',
    r'due\s+(?:in|within)\s+(\d+)\s*days?',
    r'payment\s+(?:terms?|due)[:\s]+(\d+)\s*days?',
    r'(\d+)\s*days?\s+(?:net|from\s+invoice)',
]

TOTAL_LABELS = [
    r'(?<!\w)(?:grand\s+)?total(?:\s+amount)?(?:\s+due)?[:\s]',
    r'(?<!\w)amount\s+due[:\s]',
    r'(?<!\w)total\s+payable[:\s]',
    r'(?<!\w)invoice\s+total[:\s]',
]

SUBTOTAL_LABELS = [
    r'sub[-\s]?total[:\s]',
    r'net\s+amount[:\s]',
    r'amount\s+before\s+tax[:\s]',
]

TAX_LABELS = [
    r'(?:sales\s+)?tax[:\s]',
    r'vat[:\s]',
    r'gst[:\s]',
    r'hst[:\s]',
    r'tax\s+amount[:\s]',
]

DISCOUNT_LABELS = [
    r'discount[:\s]',
    r'rebate[:\s]',
    r'credit[:\s]',
]

INVOICE_SIGNALS = [
    'invoice', 'bill to', 'ship to', 'payment terms', 'invoice date',
    'invoice number', 'invoice no', 'due date', 'subtotal', 'amount due',
    'tax invoice', 'commercial invoice', 'proforma invoice',
]

NON_INVOICE_SIGNALS = [
    'terms and conditions', 'terms of service', 'privacy policy',
    'shipping notice', 'packing slip', 'company profile', 'about us',
    'user agreement', 'refund policy', 'frequently asked questions',
]

INVOICE_BREAK_SIGNALS = [
    r'invoice\s+#?\s*[A-Z0-9]',
    r'bill\s+to\s*:',
    r'invoice\s+date\s*:',
    r'tax\s+invoice',
]


# ─────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────

def clean_amount(s: str) -> Optional[float]:
    """Parse a possibly-noisy amount string to float."""
    if not s:
        return None
    s = s.replace(',', '').strip()
    s = re.sub(r'[^\d.\-]', '', s)
    try:
        return float(s)
    except ValueError:
        return None


def extract_amounts_near_label(text: str, label_patterns: List[str]) -> Optional[float]:
    """
    Find amount that follows a label pattern in raw text.
    Uses the LAST match found — ensures TOTAL: wins over Subtotal:
    when both appear in the same text block.
    """
    for pat in label_patterns:
        # findall returns all matches; take the last one
        matches = list(re.finditer(pat + r'\s*[-]?\s*' + AMOUNT_PATTERN, text, re.IGNORECASE))
        if matches:
            m = matches[-1]
            val = clean_amount(m.group(len(m.groups())))
            if val is not None:
                return abs(val)
    return None

def extract_amounts_from_summary_section(text: str) -> Dict[str, Optional[float]]:
    """
    FIX 4 — Extract amounts from SUMMARY SECTION only (bottom of invoice).
    
    Avoids picking up amounts from the LINE ITEMS TABLE.
    Summary section is where "Subtotal:", "Tax:", "Discount:", "TOTAL:" appear.
    """
    results = {"subtotal": None, "tax": None, "discount": None, "total": None}
    
    lines = text.split('\n')
    
    # Find summary section by searching backwards from end
    # Summary section is typically in the last 15-20 lines of the document
    summary_start = -1
    for i in range(len(lines) - 1, max(len(lines) - 20, -1), -1):
        line_lower = lines[i].lower()
        # Look for summary keywords
        if any(kw in line_lower for kw in ['subtotal', 'tax', 'total', 'discount']):
            # Found a summary keyword — start extraction a few lines before
            summary_start = max(0, i - 5)
            break
    
    # If no summary section found, return empty (fallback to regex on full text)
    if summary_start == -1:
        return results
    
    # Build summary text from that point onwards
    summary_text = '\n'.join(lines[summary_start:])
    
    # Extract each field using regex patterns
    # These are more specific than the global patterns because we're in summary section
    
    # Subtotal: Pattern "Subtotal: Amount" or "Sub-total: Amount"
    m = re.search(r'sub[-\s]?total[:\s]+([\d,]+\.?\d{0,2})', summary_text, re.IGNORECASE)
    if m:
        results["subtotal"] = clean_amount(m.group(1))
    
    # Tax: Pattern "Tax: Amount" or "VAT: Amount" or "GST: Amount"
    m = re.search(r'(?:tax|vat|gst|hst)[:\s]+([\d,]+\.?\d{0,2})', summary_text, re.IGNORECASE)
    if m:
        results["tax"] = clean_amount(m.group(1))
    
    # Discount: Pattern "Discount: Amount" (can be negative with minus or parentheses)
    m = re.search(r'discount[:\s]+([-]?[\d,]+\.?\d{0,2})', summary_text, re.IGNORECASE)
    if m:
        results["discount"] = clean_amount(m.group(1))
    
    # Total: Pattern "TOTAL: Amount" or "Grand Total: Amount"
    m = re.search(r'(?:grand\s+)?total[:\s]+([\d,]+\.?\d{0,2})', summary_text, re.IGNORECASE)
    if m:
        results["total"] = clean_amount(m.group(1))
    
    return results


def detect_currency(text: str) -> Tuple[str, float]:
    """
    Return (currency_code, confidence).
    FIX 3: confidence boosted when symbol appears 10+ times or
    currency code appears explicitly in document text.
    """
    scores = {}
    for cur, patterns in CURRENCY_PATTERNS.items():
        score = 0
        for pat in patterns:
            score += len(re.findall(pat, text, re.IGNORECASE))
        if score > 0:
            scores[cur] = score

    if not scores:
        return "USD", 0.3

    best = max(scores, key=scores.get)

    # Base confidence: 5 hits → 1.0
    confidence = min(1.0, scores[best] / 5)

    # Boost: symbol appears on every line item (10+ hits) → almost certainly correct
    if scores[best] >= 10:
        confidence = max(confidence, 0.75)

    # Boost: currency code appears explicitly (e.g. "Currency: CHF")
    if re.search(rf'\b{best}\b', text, re.IGNORECASE):
        confidence = max(confidence, 0.80)

    return best, confidence


def score_page_as_invoice(text: str) -> float:
    """Return 0-1 score for how likely this page is an invoice."""
    text_lower = text.lower()
    pos_hits = sum(1 for s in INVOICE_SIGNALS if s in text_lower)
    neg_hits = sum(1 for s in NON_INVOICE_SIGNALS if s in text_lower)
    score = min(1.0, pos_hits / 4) - min(0.5, neg_hits / 2)
    return max(0.0, score)


def extract_invoice_number(text: str) -> Tuple[str, float]:
    for pat in INVOICE_NUMBER_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip(), 0.85
    return "", 0.0


def extract_date(text: str) -> Tuple[str, float]:
    date_ctx = re.search(
        r'(?:invoice\s+date|issue\s+date|date)[:\s]+([^\n]{5,30})',
        text, re.IGNORECASE
    )
    search_text = date_ctx.group(1) if date_ctx else text
    for pat in DATE_PATTERNS:
        m = re.search(pat, search_text, re.IGNORECASE)
        if m:
            raw = m.group(1)
            normalized = normalize_date(raw)
            return normalized, 0.8 if date_ctx else 0.6
    return "", 0.0


def normalize_date(raw: str) -> str:
    from datetime import datetime
    raw = raw.strip()
    if re.match(r'\d{4}-\d{2}-\d{2}', raw):
        return raw
    m = re.match(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', raw)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except Exception:
            pass
    for fmt in ["%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
                "%B %d %Y", "%b %d %Y", "%d %B, %Y"]:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return raw


def extract_payment_terms(text: str) -> Tuple[int, float]:
    for pat in PAYMENT_TERMS_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return int(m.group(1)), 0.85
    return 30, 0.2


def extract_party_names(text: str) -> Tuple[str, str, float, float]:
    """
    Extract seller (FROM) and buyer (TO) company names.

    FIX 2 — Three-strategy parser:

    Strategy 1 — Two-column layout:
        pdfplumber collapses the two-column address block into:
            'FROM: TO:'                                   ← label line
            'Lopez, Lowery and Coffey Reynolds-Andrade'   ← names on one line
        We use the document header (first line, typically the seller in
        ALL CAPS) as a word-set anchor to find where the seller name ends
        and the buyer name begins in that concatenated string.

    Strategy 2 — Single-column layout:
        FROM: and TO: each appear on their own line with the name on the
        next line, or on the same line as "FROM: Company Name".

    Strategy 3 — Last resort:
        Use the first and second meaningful lines of the document.
    """
    seller, buyer = "", ""
    s_conf, b_conf = 0.0, 0.0

    lines = text.split('\n')

    # ── Strategy 1: two-column layout — "FROM: TO:" on one line ──
    for i, line in enumerate(lines):
        stripped = line.strip()

        if re.search(r'from\s*:', stripped, re.IGNORECASE) and \
           re.search(r'\bto\s*:', stripped, re.IGNORECASE):

            # Next non-empty line has both names concatenated without a gap:
            # "Lopez, Lowery and Coffey Reynolds-Andrade"
            for j in range(i + 1, min(i + 4, len(lines))):
                name_line = lines[j].strip()
                if len(name_line) < 3:
                    continue

                # ── Approach A: document header as seller anchor ──
                # The very first line of the document is usually the seller
                # company header in ALL CAPS e.g. "LOPEZ, LOWERY AND COFFEY"
                # Build a set of significant words from it and walk name_line
                # token by token — stop at the first word not in that set.
                header_seller = lines[0].strip()
                header_words = set(
                    w.lower()
                    for w in re.split(r'[\s,&.]+', header_seller)
                    if len(w) > 2
                )

                if header_words:
                    tokens = list(re.finditer(r'\S+', name_line))
                    best_end_pos = 0

                    for tok in tokens:
                        word = re.sub(r'[,.]', '', tok.group()).lower()
                        # Allow small connector words through
                        if word in ('and', 'or', 'of', 'the', '&'):
                            best_end_pos = tok.end()
                            continue
                        if word in header_words:
                            best_end_pos = tok.end()
                        else:
                            break  # first word not in seller set → buyer starts here

                    if best_end_pos > 0:
                        seller_candidate = name_line[:best_end_pos].strip()
                        buyer_candidate  = name_line[best_end_pos:].strip()
                        if seller_candidate:
                            seller = seller_candidate[:80]
                            s_conf = 0.92
                        if buyer_candidate:
                            buyer  = buyer_candidate[:80]
                            b_conf = 0.92
                        break

                # ── Approach B: split on 2+ consecutive spaces ──
                parts = re.split(r'\s{2,}', name_line)
                if len(parts) >= 2:
                    seller = parts[0].strip()[:80]
                    buyer  = parts[-1].strip()[:80]
                    s_conf = 0.88
                    b_conf = 0.88
                    break

                # ── Approach C: column-position fallback ──
                to_match  = re.search(r'\bto\s*:', stripped, re.IGNORECASE)
                split_col = to_match.start() if to_match else len(name_line) // 2
                seller = name_line[:split_col].strip()[:80]
                buyer  = name_line[split_col:].strip()[:80]
                s_conf = 0.70
                b_conf = 0.70
                break
            break  # found the FROM:/TO: label line — stop outer loop

    # ── Strategy 2: single-column layout ──
    if not seller:
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Label on its own line
            if re.match(r'from\s*:\s*$', stripped, re.IGNORECASE):
                for j in range(i + 1, min(i + 4, len(lines))):
                    candidate = lines[j].strip()
                    if len(candidate) > 2 and not re.match(
                        r'^(invoice|date|currency|payment|to|bill|description)',
                        candidate, re.IGNORECASE
                    ):
                        seller = candidate[:80]
                        s_conf = 0.85
                        break
                break
            # Label + name on same line: "FROM: Company Name"
            m = re.match(r'from\s*:\s*([A-Za-z].+)', stripped, re.IGNORECASE)
            if m:
                seller = m.group(1).strip()[:80]
                s_conf = 0.85
                break

    if not buyer:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.match(
                r'(?:bill\s+to|ship\s+to|(?<!\w)to)\s*:\s*$',
                stripped, re.IGNORECASE
            ):
                for j in range(i + 1, min(i + 4, len(lines))):
                    candidate = lines[j].strip()
                    if len(candidate) > 2 and not re.match(
                        r'^(invoice|date|currency|payment|from|description)',
                        candidate, re.IGNORECASE
                    ):
                        buyer  = candidate[:80]
                        b_conf = 0.85
                        break
                break
            m = re.match(
                r'(?:bill\s+to|ship\s+to|(?<!\w)to)\s*:\s*([A-Za-z].+)',
                stripped, re.IGNORECASE
            )
            if m:
                buyer  = m.group(1).strip()[:80]
                b_conf = 0.85
                break

    # ── Strategy 3: last resort ──
    skip_re = re.compile(
        r'^(invoice|date|currency|payment|from|to|bill|ship|'
        r'description|qty|unit|tax|discount|total|subtotal)',
        re.IGNORECASE
    )
    if not seller:
        for line in lines:
            candidate = line.strip()
            if (len(candidate) > 3
                    and re.search(r'[A-Za-z]{3,}', candidate)
                    and not skip_re.match(candidate)):
                seller = candidate[:80]
                s_conf = 0.50
                break

    if not buyer:
        for line in lines:
            candidate = line.strip()
            if (len(candidate) > 3
                    and candidate != seller
                    and re.search(r'[A-Za-z]{3,}', candidate)
                    and not skip_re.match(candidate)):
                buyer  = candidate[:80]
                b_conf = 0.30
                break

    return seller, buyer, s_conf, b_conf


# ─────────────────────────────────────────────
# Table extraction
# ─────────────────────────────────────────────

HEADER_SYNONYMS = {
    "description": ["description", "item", "service", "details", "product", "desc"],
    "quantity":    ["quantity", "qty", "units", "count", "hours", "pcs"],
    "unit_price":  ["unit price", "unit cost", "rate", "price", "unit"],
    "discount":    ["discount", "disc", "rebate"],
    "tax":         ["tax", "vat", "gst", "hst"],
    "total":       ["total", "amount", "line total", "subtotal", "net"],
}


def map_column_headers(headers: List[str]) -> Dict[str, int]:
    """Map raw column headers to canonical field names."""
    col_map = {}
    for i, h in enumerate(headers):
        if h is None:
            continue
        h_lower = str(h).lower().strip()
        for field_name, synonyms in HEADER_SYNONYMS.items():
            if any(syn in h_lower for syn in synonyms):
                if field_name not in col_map:
                    col_map[field_name] = i
    return col_map


def parse_table_rows(table: List[List], col_map: Dict[str, int]) -> List[LineItem]:
    items = []
    for row in table:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue

        def get_col(name):
            idx = col_map.get(name)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        desc = str(get_col("description") or "").strip()
        if not desc or desc.lower() in ("description", "item", "service"):
            continue

        qty_raw   = get_col("quantity")
        up_raw    = get_col("unit_price")
        disc_raw  = get_col("discount")
        tax_raw   = get_col("tax")
        total_raw = get_col("total")

        qty          = clean_amount(str(qty_raw))   if qty_raw   else 1.0
        unit_price   = clean_amount(str(up_raw))    if up_raw    else None
        discount_amt = clean_amount(str(disc_raw))  if disc_raw  else 0.0
        tax_amt      = clean_amount(str(tax_raw))   if tax_raw   else 0.0
        line_total   = clean_amount(str(total_raw)) if total_raw else None

        if qty          is None: qty          = 1.0
        if discount_amt is None: discount_amt = 0.0
        if tax_amt      is None: tax_amt      = 0.0

        # Infer missing values
        if unit_price and line_total is None:
            line_total = round((qty * unit_price) - discount_amt + tax_amt, 2)
        elif line_total and unit_price is None and qty:
            unit_price = round((line_total + discount_amt - tax_amt) / qty, 2)
        elif line_total and unit_price and qty is None:
            qty = 1.0

        items.append(LineItem(
            description=desc,
            quantity=qty or 1.0,
            unit_price=unit_price or 0.0,
            tax_amount=tax_amt,
            discount_amount=discount_amt,
            line_total=line_total or 0.0,
            confidence=0.75 if (unit_price and line_total) else 0.4,
        ))
    return items

def parse_line_items_from_text(text: str) -> List[LineItem]:
    """
    Fallback parser for borderless tables where pdfplumber extract_tables()
    returns nothing.

    Reads raw page text line by line. Detects the header row
    (Description Qty Unit Price Discount Tax Total) then parses each
    subsequent line using the pattern:

        <description text>  <qty>  <currency+amount>  <currency+amount>
                            <currency+amount>  <currency+amount>

    Handles:
    - Multi-line descriptions with (ref: XX-XXXX) on a continuation line
    - Currency prefixes: CA$, S$, Fr, $, £, €, ¥
    - Lines that are continuations (ref: ...) appended to previous item
    - Stop at Subtotal / Discount / Tax / TOTAL summary rows
    """
    items: List[LineItem] = []
    lines = text.split('\n')

    # Pattern to detect a currency+amount token like CA$1,726.88 or Fr3,034.98
    AMOUNT_TOKEN = re.compile(
        r'^(?:CA\$|S\$|A\$|US\$|Fr|CHF|SGD|CAD|AUD|USD|EUR|GBP|£|€|¥|\$)'
        r'[\d,]+\.?\d*$',
        re.IGNORECASE
    )

    # Pattern to detect a plain integer quantity (1-4 digits, standalone)
    QTY_TOKEN = re.compile(r'^\d{1,4}$')

    # Pattern to detect summary rows — stop parsing here
    SUMMARY_ROW = re.compile(
        r'^(subtotal|discount|tax|total|note)[:\s]', re.IGNORECASE
    )

    # Pattern to detect table header row
    HEADER_ROW = re.compile(
        r'description\s+qty\s+unit\s+price', re.IGNORECASE
    )

    # Pattern to detect a (ref: ...) continuation line
    REF_LINE = re.compile(r'^\(ref:', re.IGNORECASE)

    def strip_currency(s: str) -> Optional[float]:
        """Remove currency prefix and parse to float."""
        s = re.sub(r'^(?:CA\$|S\$|A\$|US\$|Fr|CHF|SGD|CAD|AUD|USD|EUR|GBP|[£€¥$])', '', s)
        return clean_amount(s)

    in_table = False
    pending_item: Optional[LineItem] = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Detect table header — start parsing after this
        if HEADER_ROW.search(stripped):
            in_table = True
            if pending_item:
                items.append(pending_item)
                pending_item = None
            continue

        if not in_table:
            continue

        # Stop at summary rows
        if SUMMARY_ROW.match(stripped):
            if pending_item:
                items.append(pending_item)
                pending_item = None
            break

        # (ref: XX-XXXX) — continuation of previous item description
        if REF_LINE.match(stripped):
            if pending_item:
                pending_item.description += '\n' + stripped
            continue

        # Tokenise the line
        tokens = stripped.split()
        if len(tokens) < 3:
            # Too short — probably a continuation description line
            if pending_item:
                pending_item.description += ' ' + stripped
            continue

        # Find the rightmost block of: qty + up to 4 currency amounts
        # Walk tokens from the right, collecting amount tokens
        amounts = []
        qty_idx = None

        i = len(tokens) - 1
        while i >= 0 and len(amounts) < 4:
            tok = tokens[i]
            if AMOUNT_TOKEN.match(tok):
                amounts.insert(0, strip_currency(tok))
                i -= 1
            else:
                break

        # The token just before the amounts should be the quantity
        if i >= 0 and QTY_TOKEN.match(tokens[i]):
            qty_idx = i
            qty = float(tokens[i])
            desc = ' '.join(tokens[:i])
        else:
            # Could not find qty — skip this line
            if pending_item:
                pending_item.description += ' ' + stripped
            continue

        # Need at least 2 amounts (unit price + total)
        if len(amounts) < 2:
            continue

        # Map amounts by count:
        # 2 amounts → unit_price, total
        # 3 amounts → unit_price, (disc or tax), total
        # 4 amounts → unit_price, discount, tax, total
        unit_price    = amounts[0] or 0.0
        discount_amt  = 0.0
        tax_amt       = 0.0
        line_total    = 0.0

        if len(amounts) == 2:
            line_total = amounts[1] or 0.0
        elif len(amounts) == 3:
            # middle amount could be discount or tax — use column position
            # heuristic: if it is 0, treat as discount; else tax
            discount_amt = amounts[1] or 0.0
            line_total   = amounts[2] or 0.0
        elif len(amounts) >= 4:
            discount_amt = amounts[1] or 0.0
            tax_amt      = amounts[2] or 0.0
            line_total   = amounts[3] or 0.0

        # Save previous item and start new one
        if pending_item:
            items.append(pending_item)

        pending_item = LineItem(
            description=desc,
            quantity=qty,
            unit_price=unit_price,
            discount_amount=discount_amt,
            tax_amount=tax_amt,
            line_total=line_total,
            confidence=0.80,
        )

    # Append last item
    if pending_item:
        items.append(pending_item)

    return items

def extract_tables_from_page(page) -> List[LineItem]:
    """
    Extract line items from a pdfplumber page.
    Primary: structured table extraction (works for bordered tables).
    Fallback: text-based parser (works for borderless tables).
    """
    items = []
    tables = page.extract_tables()

    if tables:
        for table in tables:
            if not table or len(table) < 2:
                continue
            header_row = table[0]
            col_map = map_column_headers(header_row)
            if "description" not in col_map and "total" not in col_map:
                continue
            parsed = parse_table_rows(table[1:], col_map)
            items.extend(parsed)

    # If structured extraction found nothing or only zero-value items,
    # fall back to text-based parsing
    valid_items = [i for i in items if i.line_total > 0 or i.unit_price > 0]
    if not valid_items:
        text = page.extract_text() or ""
        items = parse_line_items_from_text(text)

    return items


# ─────────────────────────────────────────────
# Invoice boundary detection
# ─────────────────────────────────────────────

def detect_invoice_boundaries(pages_text: List[str]) -> List[Tuple[int, int]]:
    """
    Return list of (page_start, page_end) tuples, one per detected invoice.
    Uses invoice signal density scoring + break signal heuristics.

    Improvements over original:
    - Expanded INVOICE_BREAK_SIGNALS catch more invoice-start patterns
    - New invoice detected when break_count >= 2 AND prior page had any
      end signal (subtotal/tax/discount/total) — not just grand total
    - Also triggers on break_count >= 3 alone (very strong start signals)
    - Score threshold lowered to 0.2 to catch short/sparse invoice pages
    """
    n = len(pages_text)
    if n == 0:
        return []

    scores     = [score_page_as_invoice(t) for t in pages_text]
    boundaries = []
    in_invoice = False
    inv_start  = -1

    # Expanded break signals — more invoice-start anchor patterns
    expanded_break_signals = [
        r'invoice\s*#',
        r'invoice\s+no',
        r'invoice\s+number',
        r'bill\s+to\s*:',
        r'invoice\s+date\s*:',
        r'tax\s+invoice',
        r'date\s*:\s*\d{4}',
        r'payment\s+terms',
    ]

    for i, score in enumerate(scores):
        if score >= 0.2:
            if not in_invoice:
                in_invoice = True
                inv_start  = i
            else:
                text = pages_text[i].lower()

                # Count invoice-start signals on this page
                break_count = sum(
                    len(re.findall(pat, text, re.IGNORECASE))
                    for pat in expanded_break_signals
                )

                # Check if the previous page had any invoice-end signals
                prev_text = pages_text[i - 1].lower() if i > 0 else ""

                had_total = any(
                    lbl in prev_text
                    for lbl in [
                        "total amount", "amount due", "grand total",
                        "total:", "total due"
                    ]
                )
                had_invoice_end = any(
                    lbl in prev_text
                    for lbl in ["subtotal", "tax:", "discount:"]
                )

                # New invoice boundary when:
                # (a) 2+ start signals AND prev page had any end signal, OR
                # (b) 3+ start signals alone (very strong evidence)
                is_new_invoice = (
                    (break_count >= 2 and (had_total or had_invoice_end))
                    or break_count >= 3
                )

                if is_new_invoice:
                    boundaries.append((inv_start, i - 1))
                    inv_start = i
        else:
            if in_invoice:
                boundaries.append((inv_start, i - 1))
                in_invoice = False

    if in_invoice:
        boundaries.append((inv_start, n - 1))

    # Fallback: no boundaries found but some invoice signal exists
    if not boundaries and any(s > 0 for s in scores):
        best = max(range(n), key=lambda i: scores[i])
        if scores[best] >= 0.2:
            boundaries = [(0, n - 1)]

    return boundaries

# ─────────────────────────────────────────────
# Per-invoice extraction
# ─────────────────────────────────────────────

def extract_invoice_from_pages(
    pdf_path: str,
    page_start: int,
    page_end: int,
    doc_id: str,
    inv_idx: int,
) -> ExtractedInvoice:
    """Extract a single invoice spanning pages [page_start, page_end]."""
    inv            = ExtractedInvoice()
    inv.invoice_id = f"{doc_id}_inv{inv_idx}"
    inv.page_start = page_start
    inv.page_end   = page_end

    all_text  = ""
    all_items: List[LineItem] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for pi in range(page_start, min(page_end + 1, len(pdf.pages))):
                page      = pdf.pages[pi]
                text      = page.extract_text() or ""
                all_text += text + "\n"
                items     = extract_tables_from_page(page)
                all_items.extend(items)
    except Exception as e:
        logger.warning(
            f"pdfplumber error on {pdf_path} pages {page_start}-{page_end}: {e}"
        )

    inv.raw_text   = all_text
    inv.line_items = all_items

    # ── Field extraction ──
    conf = {}

    inv.invoice_number,   conf["invoice_number"]  = extract_invoice_number(all_text)
    inv.issue_date,       conf["issue_date"]       = extract_date(all_text)
    inv.currency,         conf["currency"]         = detect_currency(all_text)
    inv.payment_terms_days, conf["payment_terms"]  = extract_payment_terms(all_text)

    seller, buyer, sc, bc = extract_party_names(all_text)
    inv.seller_name    = seller
    inv.buyer_name     = buyer
    conf["seller_name"] = sc
    conf["buyer_name"]  = bc

    # Amount extraction via regex labels
    summary_amounts = extract_amounts_from_summary_section(all_text)
    
    # Use summary values if found, otherwise fallback to regex on full text
    sub   = summary_amounts.get("subtotal")   or extract_amounts_near_label(all_text, SUBTOTAL_LABELS)
    tax   = summary_amounts.get("tax")        or extract_amounts_near_label(all_text, TAX_LABELS)
    disc  = summary_amounts.get("discount")   or extract_amounts_near_label(all_text, DISCOUNT_LABELS)
    total = summary_amounts.get("total")      or extract_amounts_near_label(all_text, TOTAL_LABELS)
    
    if any(v is None for v in [sub, tax, disc, total]):
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for pi in range(page_start, min(page_end + 1, len(pdf.pages))):
                    spatial = extract_amounts_spatial(pdf.pages[pi])
                    if sub   is None and spatial["subtotal"]  is not None:
                        sub   = spatial["subtotal"]
                    if tax   is None and spatial["tax"]       is not None:
                        tax   = spatial["tax"]
                    if disc  is None and spatial["discount"]  is not None:
                        disc  = spatial["discount"]
                    if total is None and spatial["total"]     is not None:
                        total = spatial["total"]
        except Exception:
            pass

    # Fallback: compute from line items when regex finds nothing
    if all_items and sub  is None:
        sub  = round(sum(i.quantity * i.unit_price for i in all_items), 2)
    if all_items and disc is None:
        disc = round(sum(i.discount_amount         for i in all_items), 2)
    if all_items and tax  is None:
        tax  = round(sum(i.tax_amount              for i in all_items), 2)

    inv.subtotal        = sub   or 0.0
    inv.tax_amount      = tax   or 0.0
    inv.discount_amount = disc  or 0.0
    inv.total_amount    = total or 0.0

    # Infer total if not found in text
    if inv.total_amount == 0.0 and inv.subtotal > 0:
        inv.total_amount = round(
            inv.subtotal - inv.discount_amount + inv.tax_amount, 2
        )

    conf["subtotal"]     = 0.7 if sub   else 0.3
    conf["tax_amount"]   = 0.7 if tax   else 0.3
    conf["total_amount"] = 0.8 if total else 0.4

    inv.confidence_scores = conf
    return inv


# ─────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────

TOLERANCE = 0.10  # 10% relative-error tolerance for all amount checks


def validate_invoice(inv: ExtractedInvoice) -> List[str]:
    errors = []

    if not inv.invoice_number:
        errors.append("missing_invoice_number")

    if not inv.line_items:
        errors.append("missing_line_items")

    if inv.subtotal > 0 and inv.line_items:
        expected_sub = round(sum(i.quantity * i.unit_price for i in inv.line_items), 2)
        abs_diff = abs(expected_sub - inv.subtotal)
        rel_diff = abs_diff / max(inv.subtotal, 1)
        # Only flag if BOTH relative AND absolute difference are meaningful
        if rel_diff > TOLERANCE and abs_diff > 10.0:
            errors.append("subtotal_mismatch")

    if inv.tax_amount > 0 and inv.line_items:
        expected_tax = round(sum(i.tax_amount for i in inv.line_items), 2)
        abs_diff = abs(expected_tax - inv.tax_amount)
        rel_diff = abs_diff / max(inv.tax_amount, 1)
        if rel_diff > TOLERANCE and abs_diff > 5.0:
            errors.append("tax_mismatch")

    if inv.discount_amount > 0 and inv.line_items:
        expected_disc = round(sum(i.discount_amount for i in inv.line_items), 2)
        abs_diff = abs(expected_disc - inv.discount_amount)
        rel_diff = abs_diff / max(inv.discount_amount, 1)
        if rel_diff > TOLERANCE and abs_diff > 5.0:
            errors.append("discount_mismatch")

    if inv.total_amount > 0 and inv.subtotal > 0:
        expected_total = round(inv.subtotal - inv.discount_amount + inv.tax_amount, 2)
        abs_diff = abs(expected_total - inv.total_amount)
        rel_diff = abs_diff / max(inv.total_amount, 1)
        if rel_diff > TOLERANCE and abs_diff > 10.0:
            errors.append("total_mismatch")

    return errors


def detect_duplicates(invoices: List[ExtractedInvoice]) -> bool:
    """Return True if any two invoices share the same invoice number."""
    if len(invoices) < 2:
        return False
    numbers = [inv.invoice_number for inv in invoices if inv.invoice_number]
    return len(numbers) != len(set(numbers))


def detect_non_invoice_pages(pdf_path: str, pages_text: List[str]) -> bool:
    """Return True if any page is clearly non-invoice content."""
    for t in pages_text:
        score     = score_page_as_invoice(t)
        non_score = sum(1 for s in NON_INVOICE_SIGNALS if s in t.lower())
        if score < 0.15 and non_score > 0:
            return True
    return False


# ─────────────────────────────────────────────
# Document type classification
# ─────────────────────────────────────────────

def classify_document_type(
    invoice_count: int,
    has_non_invoice: bool,
    has_duplicates: bool,
    page_scores: List[float],
) -> str:
    if invoice_count == 0:
        return "non_invoice_document"
    if has_duplicates:
        return "repeated_invoice_copy"
    if invoice_count > 1:
        return "multiple_invoices"
    if has_non_invoice:
        return "invoice_with_extra_pages"
    return "single_invoice"


# ─────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────

def process_document(pdf_path: str, doc_id: str = None) -> DocumentResult:
    """
    Full pipeline: detect invoices, extract fields & tables, validate.
    Returns a DocumentResult with all extracted invoices.
    """
    if doc_id is None:
        doc_id = Path(pdf_path).stem

    notes      = []
    pages_text = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                pages_text.append(t)
    except Exception as e:
        logger.error(f"Failed to open {pdf_path}: {e}")
        return DocumentResult(
            document_id=doc_id,
            document_type="non_invoice_document",
            invoice_count=0,
            invoices=[],
            processing_notes=[f"Error: {e}"]
        )

    if not pages_text:
        return DocumentResult(
            document_id=doc_id,
            document_type="non_invoice_document",
            invoice_count=0,
            invoices=[],
            processing_notes=["Empty document"]
        )

    page_scores = [score_page_as_invoice(t) for t in pages_text]
    
    boundaries  = detect_invoice_boundaries(pages_text)
    
    # TEMPORARY DEBUG: force to include all pages if multiple pages exist
    if len(pages_text) > 1 and boundaries:
        # Extend last boundary to include all pages
        start, _ = boundaries[-1]
        boundaries[-1] = (start, len(pages_text) - 1)
    
    
    
    notes.append(
        f"Detected {len(boundaries)} invoice boundary(ies) "
        f"across {len(pages_text)} page(s)"
    )

    invoices: List[ExtractedInvoice] = []
    for idx, (ps, pe) in enumerate(boundaries):
        inv                   = extract_invoice_from_pages(pdf_path, ps, pe, doc_id, idx + 1)
        inv.validation_errors = validate_invoice(inv)
        invoices.append(inv)

    has_non_invoice = detect_non_invoice_pages(pdf_path, pages_text)
    has_duplicates  = detect_duplicates(invoices)

    if has_duplicates:
        for inv in invoices:
            if "duplicate_invoice_in_document" not in inv.validation_errors:
                inv.validation_errors.append("duplicate_invoice_in_document")

    if has_non_invoice:
        for inv in invoices:
            if "non_invoice_page_detected" not in inv.validation_errors:
                inv.validation_errors.append("non_invoice_page_detected")

    doc_type = classify_document_type(
        len(invoices), has_non_invoice, has_duplicates, page_scores
    )

    return DocumentResult(
        document_id=doc_id,
        document_type=doc_type,
        invoice_count=len(invoices),
        invoices=invoices,
        processing_notes=notes,
    )
    
def extract_amounts_spatial(page) -> Dict[str, Optional[float]]:
    """
    Fix 4: Use pdfplumber word bounding boxes to extract summary amounts.

    Key insight: invoice summary rows (Subtotal, Tax, Discount, Total)
    always appear in the BOTTOM section of the page, AFTER the line-item
    table. The word "Total" also appears as a TABLE COLUMN HEADER near
    the top. Restricting the search to the bottom 40% of the page
    eliminates all false matches from table headers and line-item rows.

    Returns: {"subtotal": float|None, "tax": float|None,
              "discount": float|None, "total": float|None}
    """
    results: Dict[str, Optional[float]] = {
        "subtotal": None,
        "tax":      None,
        "discount": None,
        "total":    None,
    }

    label_map = {
        "subtotal": SUBTOTAL_LABELS,
        "tax":      TAX_LABELS,
        "discount": DISCOUNT_LABELS,
        "total":    TOTAL_LABELS,
    }

    try:
        words = page.extract_words()
        page_height = page.height
    except Exception:
        return results

    if not words:
        return results

    # Only consider words in the bottom 40% of the page.
    # Summary rows always live here; table headers and line items do not.
    bottom_threshold = page_height * 0.60
    word_list = [
        (w["text"], w["x0"], w["top"], w["x1"], w["bottom"])
        for w in words
        if w["top"] >= bottom_threshold
    ]

    # If the page has very few words (e.g. page 2 with just Tax + Total),
    # use all words on that page instead
    if len(word_list) < 4:
        word_list = [
            (w["text"], w["x0"], w["top"], w["x1"], w["bottom"])
            for w in words
        ]

    for field_key, patterns in label_map.items():
        if results[field_key] is not None:
            continue

        for txt, x0, y0, x1, y1 in word_list:
            label_found = any(
                re.search(pat, txt, re.IGNORECASE) for pat in patterns
            )
            if not label_found:
                continue

            # Collect numeric values strictly to the right on the same line
            candidates = []
            for other_txt, ox0, oy0, ox1, oy1 in word_list:
                if ox0 <= x0:
                    continue
                if abs(oy0 - y0) > 12:
                    continue
                val = clean_amount(other_txt)
                if val is not None and val > 0:
                    candidates.append((ox0, val))

            if candidates:
                candidates.sort(key=lambda c: c[0])
                results[field_key] = candidates[-1][1]
                break

    return results


def result_to_dict(result: DocumentResult) -> dict:
    """Serialize DocumentResult to annotation-compatible dict."""
    def inv_to_dict(inv: ExtractedInvoice) -> dict:
        return {
            "invoice_id":         inv.invoice_id,
            "invoice_number":     inv.invoice_number,
            "seller_name":        inv.seller_name,
            "buyer_name":         inv.buyer_name,
            "issue_date":         inv.issue_date,
            "currency":           inv.currency,
            "subtotal":           inv.subtotal,
            "tax_amount":         inv.tax_amount,
            "discount_amount":    inv.discount_amount,
            "total_amount":       inv.total_amount,
            "payment_terms_days": inv.payment_terms_days,
            "page_start":         inv.page_start,
            "page_end":           inv.page_end,
            "line_items": [
                {
                    "description":     item.description,
                    "quantity":        item.quantity,
                    "unit_price":      item.unit_price,
                    "tax_amount":      item.tax_amount,
                    "discount_amount": item.discount_amount,
                    "line_total":      item.line_total,
                }
                for item in inv.line_items
            ],
            "validation_errors": inv.validation_errors,
            "confidence_scores": inv.confidence_scores,
        }

    return {
        "document_id":      result.document_id,
        "document_type":    result.document_type,
        "invoice_count":    result.invoice_count,
        "invoices":         [inv_to_dict(inv) for inv in result.invoices],
        "processing_notes": result.processing_notes,
    }