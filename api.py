"""
InvoiceIQ — FastAPI Backend
Wraps the extraction pipeline as a REST API.
"""

import os
import uuid
import tempfile
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))
from extractor import process_document, result_to_dict

app = FastAPI(title="InvoiceIQ", version="1.0.0")

# Allow React dev server during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok", "service": "InvoiceIQ"}


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    """
    Accept a PDF, PNG, or JPG upload.
    Run the extraction pipeline.
    Return structured JSON result.
    """
    # Validate file type
    allowed = {".pdf", ".png", ".jpg", ".jpeg"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Use PDF, PNG, or JPG."
        )

    # Save to temp file
    doc_id = f"doc_{uuid.uuid4().hex[:8]}"
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=ext, prefix=doc_id + "_"
    ) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = process_document(tmp_path, doc_id)
        return result_to_dict(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)


@app.post("/api/extract/batch")
async def extract_batch(files: list[UploadFile] = File(...)):
    """Process multiple files in one request."""
    results = []
    for file in files:
        ext = Path(file.filename).suffix.lower()
        doc_id = f"doc_{uuid.uuid4().hex[:8]}"
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=ext, prefix=doc_id + "_"
        ) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name
        try:
            result = process_document(tmp_path, doc_id)
            results.append(result_to_dict(result))
        except Exception as e:
            results.append({"document_id": doc_id, "error": str(e)})
        finally:
            os.unlink(tmp_path)
    return {"results": results, "count": len(results)}


# Serve React build in production
frontend_build = Path(__file__).parent / "frontend" / "dist"
if frontend_build.exists():
    app.mount("/", StaticFiles(directory=str(frontend_build), html=True))