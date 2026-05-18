# PS-2: Invoice Document Extraction System

## Overview

A pipeline for detecting, separating, and extracting structured data from invoice PDFs — including multi-invoice documents, repeated copies, and files with non-invoice pages. Uses classical NLP, layout analysis, and heuristics; **no LLM required** for core extraction.

Live_url : https://invoiceiq-intelligence-document.onrender.com

## Project Structure

```
invoice_system/
├── documents/              # 500 synthetic invoice PDFs
├── annotations.json        # Ground truth for all 500 documents
├── train_manifest.csv      # 400 training document rows
├── test_manifest.csv       # 100 test document rows
├── run_extraction.py       # CLI entry point
├── src/
│   ├── generate_dataset.py # Synthetic dataset generator
│   ├── extractor.py        # Core extraction pipeline
│   └── evaluate.py         # Evaluation against ground truth
└── README.md
```

## Dataset

| Document Type              | Count |
|---------------------------|-------|
| single_invoice            | 195   |
| multiple_invoices         | 155   |  ← 31% (requirement ≥ 25%)
| invoice_with_extra_pages  | 80    |
| repeated_invoice_copy     | 45    |
| non_invoice_document      | 25    |
| **Total**                 | **500** |

### Document variation includes
- 5 layout styles (classic, modern, minimal, compact, detailed)
- 8 currencies (USD, EUR, GBP, CAD, AUD, JPY, CHF, SGD)
- 1–40 line items per invoice
- 0–5 invoices per document
- Missing table borders, multi-line descriptions, tax/discount rows
- Optional image degradation: rotation (±10°), Gaussian blur, pixel noise (low/medium/high)
- Non-invoice pages: terms & conditions, shipping notices, company profiles

### Annotation schema

Each `annotations.json` record:
```json
{
  "document_id": "doc_0001",
  "document_type": "single_invoice",
  "invoice_count": 1,
  "invoices": [
    {
      "invoice_id": "doc_0001_inv1",
      "invoice_number": "INV-2023-04521",
      "seller_name": "Acme Corp",
      "buyer_name": "Buyer Inc",
      "issue_date": "2023-07-14",
      "currency": "USD",
      "subtotal": 12340.00,
      "tax_amount": 987.20,
      "discount_amount": 0.0,
      "total_amount": 13327.20,
      "payment_terms_days": 30,
      "page_start": 0,
      "page_end": 0,
      "line_items": [...],
      "validation_errors": []
    }
  ]
}
```

## Extraction Pipeline (`src/extractor.py`)

### Stage 1 — Page scoring
Each PDF page is scored 0–1 for "invoice-ness" using keyword heuristics against positive signals (`invoice`, `bill to`, `subtotal`, `amount due`, …) and negative signals (`terms and conditions`, `privacy policy`, …).

### Stage 2 — Invoice boundary detection
Sliding-window boundary detection identifies invoice start/end pages from score transitions and structural signals (`invoice #`, `bill to:`, `invoice date:`). Handles multi-invoice docs where invoices follow each other on successive pages.

### Stage 3 — Field extraction (per invoice)
- **pdfplumber** extracts text and tables from the relevant page range
- **Regex patterns** locate invoice number, issue date, currency, payment terms, seller, buyer, subtotal, tax, discount, and total
- **Fallback computation**: if amounts aren't found via regex, they're computed by summing line-item values

### Stage 4 — Table extraction
`pdfplumber.extract_tables()` retrieves raw table data. Column headers are mapped to canonical fields via synonym lookup (`qty` → quantity, `vat` → tax, etc.). Missing values are inferred from available columns.

### Stage 5 — Mathematical validation
For each invoice, the pipeline checks:
- `subtotal_mismatch`: sum(qty × unit_price) ≠ declared subtotal
- `tax_mismatch`: sum(line_item.tax) ≠ declared tax
- `discount_mismatch`: sum(line_item.discount) ≠ declared discount
- `total_mismatch`: subtotal − discount + tax ≠ declared total
- `missing_invoice_number`: no invoice number found
- `missing_line_items`: no line items extracted
- `duplicate_invoice_in_document`: same invoice number appears on multiple pages
- `non_invoice_page_detected`: low-scoring pages mixed with invoice pages

All checks use a 5% relative tolerance.

### Stage 6 — Document type classification
Rules-based from extracted facts:
- 0 invoices → `non_invoice_document`
- duplicate invoice numbers → `repeated_invoice_copy`
- >1 invoices → `multiple_invoices`
- 1 invoice + non-invoice pages → `invoice_with_extra_pages`
- else → `single_invoice`

## Test Results (100 held-out documents)

| Metric                        | Score  |
|------------------------------|--------|
| Document type accuracy        | 83.0%  |
| Invoice count MAE             | 0.26   |
| Issue date extraction         | 97.7%  |
| Payment terms extraction      | 97.7%  |
| Currency detection            | 89.8%  |
| Subtotal extraction           | 55.1%  |
| Tax amount extraction         | 54.7%  |
| Total amount extraction       | 44.7%  |
| Invoice number extraction     | 40.6%  |
| **Overall field score**       | **68.6%** |
| Validation error F1           | 53.6%  |
| Processing time               | 0.25s/doc |

## Usage

### Extract a single document
```bash
python run_extraction.py path/to/invoice.pdf
python run_extraction.py path/to/invoice.pdf --output result.json
```

### Regenerate dataset
```bash
python src/generate_dataset.py
```

### Evaluate against ground truth
```bash
python src/evaluate.py
```

## Dependencies

```
pdfplumber    # Text + table extraction from PDFs
pypdf         # PDF merging/splitting
reportlab     # Synthetic PDF generation
Pillow        # Image degradation
numpy         # Numerical operations
scikit-learn  # (available for future ML extensions)
faker         # Realistic synthetic data generation
```

Install: `pip install pdfplumber pypdf reportlab Pillow numpy scikit-learn faker`

## Design Decisions

- **No LLM for core extraction**: all field parsing uses compiled regex patterns and layout heuristics. An LLM call is only warranted as an optional fallback for ambiguous cases not covered by this system.
- **Tolerance-based validation**: 5% relative tolerance avoids false positives from rounding differences between OCR and ground truth.
- **Boundary detection via scoring**: avoids hardcoded page-count assumptions; works for 1-to-8-page documents.
- **Graceful degradation**: each stage falls back if a prior stage yields nothing (e.g. compute totals from line items if regex finds nothing).
