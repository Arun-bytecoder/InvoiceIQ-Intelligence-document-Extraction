"""
Evaluation Script
Computes precision/recall/F1 for invoice detection, field extraction, and validation.
Runs on test_manifest.csv against annotations.json.
"""

import json
import csv
import time
import sys
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Any, Tuple

src_dir = Path(__file__).resolve().parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))
from extractor import process_document, result_to_dict


BASE_DIR = Path(__file__).resolve().parent.parent


def load_annotations(path: Path) -> Dict[str, dict]:
    with open(path) as f:
        records = json.load(f)
    return {r["document_id"]: r for r in records}


def load_manifest(path: Path) -> List[dict]:
    with open(path) as f:
        reader = csv.DictReader(f)
        return list(reader)


def safe_div(num, den):
    return num / den if den > 0 else 0.0


def field_match(pred_val, gt_val, field_type="string") -> float:
    """Return 0-1 match score for a field."""
    if pred_val is None or gt_val is None:
        return 0.0
    if field_type == "string":
        pred_s = str(pred_val).lower().strip()
        gt_s = str(gt_val).lower().strip()
        if pred_s == gt_s:
            return 1.0
        # Partial match
        if pred_s and gt_s and (pred_s in gt_s or gt_s in pred_s):
            return 0.5
        return 0.0
    elif field_type == "amount":
        try:
            p, g = float(pred_val), float(gt_val)
            if g == 0:
                return 1.0 if p == 0 else 0.0
            rel_err = abs(p - g) / max(abs(g), 1e-6)
            if rel_err <= 0.01:
                return 1.0
            elif rel_err <= 0.05:
                return 0.8
            elif rel_err <= 0.10:
                return 0.5
            return 0.0
        except Exception:
            return 0.0
    elif field_type == "integer":
        try:
            return 1.0 if int(pred_val) == int(gt_val) else 0.0
        except Exception:
            return 0.0
    elif field_type == "date":
        # Normalize comparison
        p = str(pred_val).strip()[:10]
        g = str(gt_val).strip()[:10]
        return 1.0 if p == g else 0.0


def match_invoices(pred_invoices: List[dict], gt_invoices: List[dict]) -> List[Tuple[dict, dict]]:
    """
    Best-effort matching of predicted to ground-truth invoices.
    Match by invoice_number if available, otherwise by page overlap.
    Returns list of (pred, gt) pairs.
    """
    matched = []
    used_gt = set()

    # First pass: match by invoice_number
    for pred in pred_invoices:
        pnum = pred.get("invoice_number", "").strip()
        for j, gt in enumerate(gt_invoices):
            if j in used_gt:
                continue
            gnum = gt.get("invoice_number", "").strip()
            if pnum and gnum and pnum.lower() == gnum.lower():
                matched.append((pred, gt))
                used_gt.add(j)
                break

    # Second pass: match by page overlap for unmatched
    used_pred_ids = {id(p) for p, _ in matched}
    for pred in pred_invoices:
        if id(pred) in used_pred_ids:
            continue
        pp_s = pred.get("page_start", 0)
        pp_e = pred.get("page_end", 0)
        best_overlap = 0
        best_j = None
        for j, gt in enumerate(gt_invoices):
            if j in used_gt:
                continue
            gp_s = gt.get("page_start", 0)
            gp_e = gt.get("page_end", 0)
            overlap = max(0, min(pp_e, gp_e) - max(pp_s, gp_s) + 1)
            if overlap > best_overlap:
                best_overlap = overlap
                best_j = j
        if best_j is not None and best_overlap > 0:
            matched.append((pred, gt_invoices[best_j]))
            used_gt.add(best_j)
            used_pred_ids.add(id(pred))

    return matched


def evaluate(manifest_path: Path, annotations: Dict[str, dict], base_dir: Path) -> dict:
    manifest = load_manifest(manifest_path)

    # Metrics accumulators
    doc_type_tp = doc_type_fp = doc_type_fn = 0
    invoice_count_errors = []
    field_scores = defaultdict(list)
    validation_error_tp = validation_error_fp = validation_error_fn = 0
    line_item_count_errors = []

    processing_times = []
    errors = []

    print(f"\nEvaluating {len(manifest)} documents...")

    for row in manifest:
        doc_id = row["document_id"]
        file_path = base_dir / row["file_path"]
        gt = annotations.get(doc_id)
        if gt is None:
            errors.append(f"{doc_id}: no ground truth")
            continue

        # Run extraction
        t0 = time.time()
        try:
            result = process_document(str(file_path), doc_id)
            pred = result_to_dict(result)
        except Exception as e:
            errors.append(f"{doc_id}: extraction error: {e}")
            pred = {
                "document_id": doc_id,
                "document_type": "non_invoice_document",
                "invoice_count": 0,
                "invoices": [],
            }
        elapsed = time.time() - t0
        processing_times.append(elapsed)

        # ── Document type ──
        if pred["document_type"] == gt["document_type"]:
            doc_type_tp += 1
        else:
            doc_type_fp += 1

        # ── Invoice count ──
        pred_cnt = pred["invoice_count"]
        gt_cnt = gt["invoice_count"]
        invoice_count_errors.append(abs(pred_cnt - gt_cnt))

        # ── Per-invoice field extraction ──
        pred_invs = pred.get("invoices", [])
        gt_invs = gt.get("invoices", [])
        pairs = match_invoices(pred_invs, gt_invs)

        for p_inv, g_inv in pairs:
            field_scores["invoice_number"].append(
                field_match(p_inv.get("invoice_number"), g_inv.get("invoice_number"), "string")
            )
            field_scores["issue_date"].append(
                field_match(p_inv.get("issue_date"), g_inv.get("issue_date"), "date")
            )
            field_scores["currency"].append(
                field_match(p_inv.get("currency"), g_inv.get("currency"), "string")
            )
            field_scores["subtotal"].append(
                field_match(p_inv.get("subtotal"), g_inv.get("subtotal"), "amount")
            )
            field_scores["tax_amount"].append(
                field_match(p_inv.get("tax_amount"), g_inv.get("tax_amount"), "amount")
            )
            field_scores["total_amount"].append(
                field_match(p_inv.get("total_amount"), g_inv.get("total_amount"), "amount")
            )
            field_scores["payment_terms_days"].append(
                field_match(p_inv.get("payment_terms_days"), g_inv.get("payment_terms_days"), "integer")
            )
            # Line item count
            line_item_count_errors.append(
                abs(len(p_inv.get("line_items", [])) - len(g_inv.get("line_items", [])))
            )

            # ── Validation errors (set comparison) ──
            pred_errs = set(p_inv.get("validation_errors", []))
            gt_errs = set(g_inv.get("validation_errors", []))
            validation_error_tp += len(pred_errs & gt_errs)
            validation_error_fp += len(pred_errs - gt_errs)
            validation_error_fn += len(gt_errs - pred_errs)

    # ── Compute summary metrics ──
    doc_accuracy = safe_div(doc_type_tp, len(manifest))
    avg_count_err = safe_div(sum(invoice_count_errors), len(invoice_count_errors)) if invoice_count_errors else 0

    field_avg = {k: safe_div(sum(v), len(v)) for k, v in field_scores.items()}
    overall_field_score = safe_div(sum(field_avg.values()), len(field_avg)) if field_avg else 0

    val_precision = safe_div(validation_error_tp, validation_error_tp + validation_error_fp)
    val_recall = safe_div(validation_error_tp, validation_error_tp + validation_error_fn)
    val_f1 = safe_div(2 * val_precision * val_recall, val_precision + val_recall)

    avg_item_err = safe_div(sum(line_item_count_errors), len(line_item_count_errors)) if line_item_count_errors else 0
    avg_time = safe_div(sum(processing_times), len(processing_times))

    results = {
        "n_documents": len(manifest),
        "n_errors": len(errors),
        "avg_processing_time_s": round(avg_time, 3),

        "document_type_accuracy": round(doc_accuracy, 4),

        "invoice_count_mae": round(avg_count_err, 4),

        "field_extraction_scores": {k: round(v, 4) for k, v in field_avg.items()},
        "overall_field_score": round(overall_field_score, 4),

        "validation_error_detection": {
            "precision": round(val_precision, 4),
            "recall": round(val_recall, 4),
            "f1": round(val_f1, 4),
        },

        "line_item_count_mae": round(avg_item_err, 4),

        "processing_errors": errors[:10],  # first 10
    }
    return results


def main():
    annotations = load_annotations(BASE_DIR / "annotations.json")

    print("=" * 60)
    print("INVOICE EXTRACTION PIPELINE — EVALUATION")
    print("=" * 60)

    for split, manifest_file in [("TRAIN", "train_manifest.csv"), ("TEST", "test_manifest.csv")]:
        print(f"\n{'─'*60}")
        print(f"Split: {split}")
        print(f"{'─'*60}")
        results = evaluate(BASE_DIR / manifest_file, annotations, BASE_DIR)

        print(f"\nDocuments evaluated:         {results['n_documents']}")
        print(f"Processing errors:           {results['n_errors']}")
        print(f"Avg processing time:         {results['avg_processing_time_s']:.3f}s / doc")
        print(f"\nDocument Type Accuracy:      {results['document_type_accuracy']:.1%}")
        print(f"Invoice Count MAE:           {results['invoice_count_mae']:.3f}")
        print(f"\nField Extraction Scores:")
        for field, score in results["field_extraction_scores"].items():
            print(f"  {field:<25} {score:.1%}")
        print(f"  {'Overall':<25} {results['overall_field_score']:.1%}")
        print(f"\nValidation Error Detection:")
        ved = results["validation_error_detection"]
        print(f"  Precision:  {ved['precision']:.1%}")
        print(f"  Recall:     {ved['recall']:.1%}")
        print(f"  F1:         {ved['f1']:.1%}")
        print(f"\nLine Item Count MAE:         {results['line_item_count_mae']:.3f}")

        # Save results
        out_path = BASE_DIR / f"eval_{split.lower()}_results.json"
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nSaved: {out_path}")

    print("\n" + "=" * 60)
    print("Evaluation complete.")


if __name__ == "__main__":
    main()