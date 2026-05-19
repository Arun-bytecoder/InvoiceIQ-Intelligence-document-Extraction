#!/usr/bin/env python3
"""
Invoice Extraction CLI
Usage: python run_extraction.py <pdf_path> [--output <output.json>]
"""

import sys
import json
import argparse
from pathlib import Path
import pdfplumber

sys.path.insert(0, str(Path(__file__).parent / "src"))
from extractor import process_document, result_to_dict

with pdfplumber.open(sys.argv[1]) as pdf:
    for i, page in enumerate(pdf.pages):
        print(f"\n========= PAGE {i} RAW TEXT =========")
        print(repr(page.extract_text()))
        print("=====================================")


def main():
    parser = argparse.ArgumentParser(description="Extract invoices from a PDF document.")
    parser.add_argument("pdf_path", help="Path to the PDF file")
    parser.add_argument("--output", "-o", help="Output JSON path (default: stdout)")
    parser.add_argument("--doc-id", help="Document ID (default: filename stem)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    doc_id = args.doc_id or pdf_path.stem
    result = process_document(str(pdf_path), doc_id)
    output = result_to_dict(result)

    json_output = json.dumps(output, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(json_output)
        print(f"Saved to {args.output}")
    else:
        print(json_output)


if __name__ == "__main__":
    main()