"""
Compylance — Flask API Backend
Serves the React frontend and exposes endpoints for the invoice extraction pipeline.

Endpoints:
  POST /api/extract          — upload a PDF, get extraction result
  GET  /api/stats            — live eval stats (from eval_*_results.json)
  GET  /api/history          — recent extraction history (in-memory)
  GET  /api/document/<id>    — re-fetch a cached result
  GET  /health               — liveness probe

Run:
  pip install flask flask-cors
  python app.py
"""

import json
import time
import uuid
import os
import traceback
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask import send_from_directory

# ── Resolve project root ──────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DIST_FOLDER = os.path.join(BASE_DIR, "frontend", "dist")
ASSETS_FOLDER = os.path.join(DIST_FOLDER, "assets")# where app.py lives
SRC_DIR  = BASE_DIR / "src"
UPLOAD_DIR = BASE_DIR / "uploads_tmp"
UPLOAD_DIR.mkdir(exist_ok=True)

import sys
sys.path.insert(0, str(SRC_DIR))

try:
    from extractor import process_document, result_to_dict
    EXTRACTOR_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] extractor not importable: {e}. Demo mode active.")
    EXTRACTOR_AVAILABLE = False

app = Flask(
    __name__,
    static_folder=DIST_FOLDER,
    static_url_path=""
)

CORS(app)

# In-memory history (last 50 extractions)
history: list = []
MAX_HISTORY = 50

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory(ASSETS_FOLDER, filename)

# Serve frontend
@app.route('/')
def serve():
    return send_from_directory(DIST_FOLDER, 'index.html')


@app.route('/<path:path>')
def catch_all(path):
    file_path = os.path.join(DIST_FOLDER, path)

    if os.path.exists(file_path):
        return send_from_directory(DIST_FOLDER, path)

    return send_from_directory(DIST_FOLDER, 'index.html')

# ─────────────────────────────────────────────────────────────────────────────
# Helper — demo result (used when real extractor isn't available)
# ─────────────────────────────────────────────────────────────────────────────

def make_demo_result(filename: str) -> dict:
    return {
        "document_id": "demo-" + uuid.uuid4().hex[:8],
        "document_type": "single_invoice",
        "invoice_count": 1,
        "invoices": [
            {
                "invoice_id": "demo_inv1",
                "invoice_number": "INV-2024-00042",
                "seller_name": "Acme Corporation",
                "buyer_name": "Globex Inc.",
                "issue_date": "2024-03-15",
                "currency": "USD",
                "subtotal": 12500.00,
                "tax_amount": 1000.00,
                "discount_amount": 250.00,
                "total_amount": 13250.00,
                "payment_terms_days": 30,
                "page_start": 0,
                "page_end": 0,
                "line_items": [
                    {"description": "Software Development", "quantity": 50, "unit_price": 200.00,
                     "tax_amount": 800.00, "discount_amount": 250.00, "line_total": 10550.00},
                    {"description": "Technical Support",   "quantity": 10, "unit_price": 150.00,
                     "tax_amount": 120.00, "discount_amount": 0.00,   "line_total": 1620.00},
                    {"description": "Cloud Hosting",       "quantity": 1,  "unit_price": 800.00,
                     "tax_amount": 80.00,  "discount_amount": 0.00,   "line_total": 880.00},
                    {"description": "API Integration",     "quantity": 5,  "unit_price": 100.00,
                     "tax_amount": 0.00,   "discount_amount": 0.00,   "line_total": 500.00},
                ],
                "validation_errors": [],
                "confidence_scores": {
                    "invoice_number": 0.95, "issue_date": 0.90,
                    "currency": 0.85,       "subtotal": 0.75,
                    "tax_amount": 0.70,     "total_amount": 0.80,
                    "payment_terms": 0.90,
                },
            }
        ],
        "processing_notes": ["Demo mode — extractor not loaded"],
        "_demo": True,
        "_filename": filename,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "extractor": EXTRACTOR_AVAILABLE})


@app.route("/api/extract", methods=["POST"])
def extract():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    tmp_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
    f.save(str(tmp_path))

    t0 = time.time()
    try:
        if EXTRACTOR_AVAILABLE:
            result_obj = process_document(str(tmp_path), Path(f.filename).stem)
            result = result_to_dict(result_obj)
        else:
            result = make_demo_result(f.filename)
            time.sleep(0.3)   # simulate processing

        elapsed = round(time.time() - t0, 3)
        result["_processing_time_s"] = elapsed
        result["_filename"] = f.filename
        result["_id"] = uuid.uuid4().hex

        # Store in history
        history.insert(0, {
            "id":             result["_id"],
            "filename":       f.filename,
            "document_type":  result.get("document_type", "unknown"),
            "invoice_count":  result.get("invoice_count", 0),
            "processing_time": elapsed,
            "timestamp":      time.time(),
            "result":         result,
        })
        if len(history) > MAX_HISTORY:
            history.pop()

        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


@app.route("/api/stats")
def stats():
    """Return latest eval results from JSON files."""
    out = {}
    for split in ("test", "train"):
        p = BASE_DIR / f"eval_{split}_results.json"
        if p.exists():
            with open(p) as fh:
                out[split] = json.load(fh)
    # fallback demo stats
    if not out:
        out = {
            "test": {
                "n_documents": 100, "n_errors": 0,
                "avg_processing_time_s": 0.488,
                "document_type_accuracy": 0.94,
                "invoice_count_mae": 0.26,
                "field_extraction_scores": {
                    "invoice_number": 1.0, "issue_date": 0.9787,
                    "currency": 0.9149,   "subtotal": 0.6007,
                    "tax_amount": 0.6064, "total_amount": 0.4071,
                    "payment_terms_days": 0.9787,
                },
                "overall_field_score": 0.7838,
                "validation_error_detection": {
                    "precision": 0.4082, "recall": 0.9524, "f1": 0.5714,
                },
                "line_item_count_mae": 6.674,
            },
            "train": {
                "n_documents": 400, "n_errors": 0,
                "avg_processing_time_s": 0.493,
                "document_type_accuracy": 0.905,
                "invoice_count_mae": 0.325,
                "field_extraction_scores": {
                    "invoice_number": 1.0, "issue_date": 0.9590,
                    "currency": 0.9240,   "subtotal": 0.6450,
                    "tax_amount": 0.6580, "total_amount": 0.4480,
                    "payment_terms_days": 0.9650,
                },
                "overall_field_score": 0.80,
                "validation_error_detection": {
                    "precision": 0.371, "recall": 0.927, "f1": 0.530,
                },
                "line_item_count_mae": 6.260,
            },
        }
    return jsonify(out)


@app.route("/api/history")
def get_history():
    return jsonify([
        {k: v for k, v in item.items() if k != "result"}
        for item in history
    ])


@app.route("/api/document/<doc_id>")
def get_document(doc_id):
    for item in history:
        if item["id"] == doc_id:
            return jsonify(item["result"])
    return jsonify({"error": "Not found"}), 404


# ── Serve React SPA (if built) ────────────────────────────────────────────────
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    dist = Path(app.static_folder)
    if dist.exists() and (dist / path).exists():
        return send_from_directory(app.static_folder, path)
    if dist.exists() and (dist / "index.html").exists():
        return send_from_directory(app.static_folder, "index.html")
    return jsonify({"message": "Compylance API running. Build frontend with: cd frontend && npm run build"}), 200


if __name__ == "__main__":
    print("=" * 55)
    print("  Compylance Invoice Extraction API")
    print(f"  Extractor: {'✓ loaded' if EXTRACTOR_AVAILABLE else '✗ demo mode'}")
    print("  http://localhost:5000")
    print("=" * 55)

    app.run(host="0.0.0.0", port=5000)