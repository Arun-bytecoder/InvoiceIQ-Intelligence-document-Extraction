import { useState, useCallback } from "react";
import { useDropzone } from "react-dropzone";
import axios from "axios";
import {
  FileText, Upload, CheckCircle, AlertCircle,
  ChevronDown, ChevronUp, Loader2, Package
} from "lucide-react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

// ── Helpers ──
const fmt = (n) =>
  n == null ? "—" : Number(n).toLocaleString("en-US", { minimumFractionDigits: 2 });

const badge = (type) => {
  const colours = {
    single_invoice:          "bg-green-100 text-green-800",
    multiple_invoices:       "bg-blue-100 text-blue-800",
    invoice_with_extra_pages:"bg-yellow-100 text-yellow-800",
    repeated_invoice_copy:   "bg-purple-100 text-purple-800",
    non_invoice_document:    "bg-red-100 text-red-800",
  };
  return colours[type] || "bg-gray-100 text-gray-800";
};

// ── Sub-components ──
function ValidationBadge({ errors }) {
  if (!errors || errors.length === 0)
    return (
      <span className="flex items-center gap-1 text-green-600 text-sm font-medium">
        <CheckCircle size={14} /> Valid
      </span>
    );
  return (
    <div className="flex flex-wrap gap-1">
      {errors.map((e) => (
        <span key={e}
          className="bg-red-100 text-red-700 text-xs px-2 py-0.5 rounded-full font-mono">
          {e}
        </span>
      ))}
    </div>
  );
}

function ConfidenceBar({ score }) {
  const pct = Math.round((score || 0) * 100);
  const color = pct >= 80 ? "bg-green-500" : pct >= 60 ? "bg-yellow-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 bg-gray-200 rounded-full h-1.5">
        <div className={`${color} h-1.5 rounded-full`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-500 w-8">{pct}%</span>
    </div>
  );
}

function LineItemsTable({ items }) {
  const [open, setOpen] = useState(false);
  if (!items || items.length === 0)
    return <p className="text-sm text-gray-400 italic">No line items extracted.</p>;

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800 font-medium mb-2">
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        {items.length} Line Items
      </button>
      {open && (
        <div className="overflow-x-auto rounded-lg border border-gray-200">
          <table className="w-full text-xs">
            <thead className="bg-gray-50 text-gray-600 uppercase">
              <tr>
                {["Description","Qty","Unit Price","Discount","Tax","Total"].map(h => (
                  <th key={h} className="px-3 py-2 text-left font-semibold">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item, i) => (
                <tr key={i} className={i % 2 === 0 ? "bg-white" : "bg-gray-50"}>
                  <td className="px-3 py-2 text-gray-800 max-w-xs">
                    {item.description}
                  </td>
                  <td className="px-3 py-2 text-right">{item.quantity}</td>
                  <td className="px-3 py-2 text-right">{fmt(item.unit_price)}</td>
                  <td className="px-3 py-2 text-right text-orange-600">
                    {item.discount_amount > 0 ? `-${fmt(item.discount_amount)}` : "—"}
                  </td>
                  <td className="px-3 py-2 text-right text-blue-600">
                    {item.tax_amount > 0 ? fmt(item.tax_amount) : "—"}
                  </td>
                  <td className="px-3 py-2 text-right font-semibold">{fmt(item.line_total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function InvoiceCard({ inv, currency, index }) {
  const [expanded, setExpanded] = useState(true);
  const conf = inv.confidence_scores || {};

  return (
    <div className="border border-gray-200 rounded-xl overflow-hidden">
      {/* Card header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-5 py-4
                   bg-gradient-to-r from-slate-800 to-slate-700 text-white">
        <div className="flex items-center gap-3">
          <span className="bg-white/20 text-white text-xs font-bold
                           px-2.5 py-1 rounded-full">
            INV {index + 1}
          </span>
          <span className="font-semibold">{inv.invoice_number || "No Number"}</span>
        </div>
        <div className="flex items-center gap-4 text-sm">
          <span>{inv.currency} {fmt(inv.total_amount)}</span>
          {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </div>
      </button>

      {expanded && (
        <div className="p-5 space-y-5">
          {/* Parties */}
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-blue-50 rounded-lg p-4">
              <p className="text-xs text-blue-500 font-semibold uppercase mb-1">Seller</p>
              <p className="font-semibold text-gray-800">{inv.seller_name || "—"}</p>
            </div>
            <div className="bg-purple-50 rounded-lg p-4">
              <p className="text-xs text-purple-500 font-semibold uppercase mb-1">Buyer</p>
              <p className="font-semibold text-gray-800">{inv.buyer_name || "—"}</p>
            </div>
          </div>

          {/* Meta row */}
          <div className="grid grid-cols-3 gap-3 text-sm">
            {[
              { label: "Date",     value: inv.issue_date },
              { label: "Currency", value: inv.currency },
              { label: "Net Days", value: inv.payment_terms_days },
            ].map(({ label, value }) => (
              <div key={label} className="bg-gray-50 rounded-lg p-3">
                <p className="text-xs text-gray-500 mb-0.5">{label}</p>
                <p className="font-medium text-gray-800">{value || "—"}</p>
              </div>
            ))}
          </div>

          {/* Financial summary */}
          <div className="bg-gray-50 rounded-xl p-4 space-y-2">
            <div className="flex justify-between text-sm">
              <span className="text-gray-500">Subtotal</span>
              <span>{inv.currency} {fmt(inv.subtotal)}</span>
            </div>
            {inv.discount_amount > 0 && (
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Discount</span>
                <span className="text-orange-600">− {inv.currency} {fmt(inv.discount_amount)}</span>
              </div>
            )}
            <div className="flex justify-between text-sm">
              <span className="text-gray-500">Tax</span>
              <span className="text-blue-600">+ {inv.currency} {fmt(inv.tax_amount)}</span>
            </div>
            <div className="flex justify-between font-bold text-base pt-2
                            border-t border-gray-200">
              <span>Total</span>
              <span className="text-slate-800">{inv.currency} {fmt(inv.total_amount)}</span>
            </div>
          </div>

          {/* Validation */}
          <div>
            <p className="text-xs text-gray-500 font-semibold uppercase mb-2">
              Validation
            </p>
            <ValidationBadge errors={inv.validation_errors} />
          </div>

          {/* Confidence */}
          <div>
            <p className="text-xs text-gray-500 font-semibold uppercase mb-2">
              Confidence Scores
            </p>
            <div className="grid grid-cols-2 gap-x-6 gap-y-2">
              {Object.entries(conf).map(([key, val]) => (
                <div key={key}>
                  <div className="flex justify-between text-xs text-gray-500 mb-0.5">
                    <span>{key.replace(/_/g, " ")}</span>
                  </div>
                  <ConfidenceBar score={val} />
                </div>
              ))}
            </div>
          </div>

          {/* Line items */}
          <div>
            <p className="text-xs text-gray-500 font-semibold uppercase mb-2">
              Line Items
            </p>
            <LineItemsTable items={inv.line_items} />
          </div>

          {/* Pages */}
          <p className="text-xs text-gray-400">
            Pages {inv.page_start + 1} – {inv.page_end + 1}
          </p>
        </div>
      )}
    </div>
  );
}

function ResultPanel({ result }) {
  if (!result) return null;
  const { document_type, invoice_count, invoices, processing_notes } = result;

  return (
    <div className="space-y-4">
      {/* Document summary */}
      <div className="flex items-center gap-3 flex-wrap">
        <span className={`text-xs font-bold px-3 py-1.5 rounded-full uppercase
                          tracking-wide ${badge(document_type)}`}>
          {document_type?.replace(/_/g, " ")}
        </span>
        <span className="text-sm text-gray-500">
          {invoice_count} invoice{invoice_count !== 1 ? "s" : ""} detected
        </span>
        {processing_notes?.map((n, i) => (
          <span key={i} className="text-xs text-gray-400 italic">{n}</span>
        ))}
      </div>

      {/* Invoice cards */}
      <div className="space-y-4">
        {invoices?.map((inv, i) => (
          <InvoiceCard key={inv.invoice_id} inv={inv} index={i} />
        ))}
      </div>

      {/* Raw JSON toggle */}
      <details className="mt-4">
        <summary className="text-xs text-gray-400 cursor-pointer hover:text-gray-600">
          View raw JSON
        </summary>
        <pre className="mt-2 bg-gray-900 text-green-400 text-xs p-4 rounded-xl
                        overflow-auto max-h-96">
          {JSON.stringify(result, null, 2)}
        </pre>
      </details>
    </div>
  );
}

// ── Main App ──
export default function App() {
  const [result,  setResult]  = useState(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState(null);
  const [filename, setFilename] = useState(null);

  const onDrop = useCallback(async (files) => {
    if (!files.length) return;
    const file = files[0];
    setFilename(file.name);
    setLoading(true);
    setError(null);
    setResult(null);

    const form = new FormData();
    form.append("file", file);

    try {
      const { data } = await axios.post(`${API}/api/extract`, form, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setResult(data);
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { "application/pdf": [".pdf"],
              "image/png": [".png"],
              "image/jpeg": [".jpg", ".jpeg"] },
    multiple: false,
  });

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-slate-900 text-white px-6 py-4 flex items-center gap-3">
        <Package size={24} className="text-blue-400" />
        <div>
          <h1 className="text-lg font-bold leading-none">InvoiceIQ</h1>
          <p className="text-xs text-slate-400">Invoice Intelligence Pipeline — PS-2</p>
        </div>
        <span className="ml-auto text-xs bg-green-500/20 text-green-400
                         px-2 py-1 rounded-full font-mono">
          API LIVE
        </span>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-8 space-y-8">
        {/* Drop zone */}
        <div
          {...getRootProps()}
          className={`border-2 border-dashed rounded-2xl p-12 text-center
                      cursor-pointer transition-all
                      ${isDragActive
                        ? "border-blue-500 bg-blue-50"
                        : "border-gray-300 bg-white hover:border-blue-400 hover:bg-blue-50/40"
                      }`}>
          <input {...getInputProps()} />
          <Upload size={36} className="mx-auto mb-4 text-gray-400" />
          <p className="text-gray-700 font-semibold text-lg">
            {isDragActive ? "Drop the file here" : "Drop an invoice PDF here"}
          </p>
          <p className="text-gray-400 text-sm mt-1">or click to browse</p>
          <p className="text-gray-300 text-xs mt-3">PDF · PNG · JPG</p>
        </div>

        {/* Loading */}
        {loading && (
          <div className="flex items-center justify-center gap-3 py-8 text-blue-600">
            <Loader2 size={24} className="animate-spin" />
            <span className="font-medium">Processing {filename}…</span>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="flex items-start gap-3 bg-red-50 border border-red-200
                          text-red-700 rounded-xl p-4">
            <AlertCircle size={20} className="shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold">Extraction failed</p>
              <p className="text-sm mt-0.5">{error}</p>
            </div>
          </div>
        )}

        {/* Results */}
        {result && !loading && <ResultPanel result={result} />}
      </main>
    </div>
  );
}