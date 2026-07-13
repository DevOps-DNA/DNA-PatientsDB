"""
Core matching/merging pipeline.

Inputs:
  - POD files: scanned (image-only) GST invoice / delivery proof PDFs.
  - Purchase Order files: CGHS "Supplier Issued Details" PDFs with a real text layer.
  - Outward Details workbook: the sales register (xlsx) used to cross-check that a
    matched Order No / Invoice No pair genuinely exists in the outward records.

Flow:
  1. OCR every POD page -> invoice no, order no, patient name, copy type.
  2. Parse every Purchase Order page -> order no, patient name.
  3. Parse the Outward Details workbook -> {order_no: invoice_no} lookup.
  4. Anchor on the Purchase Order: for each PO entry, find its POD (by order no,
     falling back to fuzzy patient-name match), then verify the pair against the
     outward workbook.
  5. Emit a merged PDF (POD page, then PO page, per patient) plus a CSV report.
"""
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF
import pandas as pd
from pypdf import PdfReader, PdfWriter
from rapidfuzz import fuzz

INVOICE_RE = re.compile(r"(DN\s*/\s*\d{2}\s*-\s*\d{2}\s*/\s*\d{3,7})", re.I)
ORDER_RE = re.compile(r"Order\s*No\.?\s*[:\-]?\s*(\d{6,})", re.I)
PATIENT_RE = re.compile(r"Pt\s*,?\s*([A-Za-z][A-Za-z .'\-]{2,60})")
COPY_TYPE_RE = re.compile(
    r"(ORIGINAL FOR RECIPIENT|DUPLICATE FOR TRANSPORTER[^\n]*|TRIPLICATE FOR SUPPLIER|QUADRUPLICATE[^\n]*)",
    re.I,
)
COPY_PRIORITY = {"original": 0, "duplicate": 1, "triplicate": 2, "quadruplicate": 3}

ProgressFn = Optional[Callable[[str, int, int], None]]


def _progress(cb: ProgressFn, stage: str, current: int, total: int):
    if cb:
        cb(stage, current, total)


def normalize_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z ]", " ", name or "").upper()
    return " ".join(sorted(name.split()))


def normalize_invoice(inv: str) -> str:
    return re.sub(r"\s+", "", inv or "").upper()


def normalize_order(order) -> str:
    """Digits-only order number. Handles values pandas has read as floats
    (e.g. "2426060264.0" from a numeric xlsx column) without leaving a
    spurious trailing zero from the ".0"."""
    s = str(order).strip() if order is not None else ""
    if not s or s.lower() == "nan":
        return ""
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except ValueError:
        pass
    return re.sub(r"\D", "", s)


def ocr_image(png_path: Path) -> str:
    out_base = png_path.with_suffix("")
    subprocess.run(
        ["tesseract", str(png_path), str(out_base)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return Path(str(out_base) + ".txt").read_text(errors="ignore")


# ---------------------------------------------------------------------------
# POD extraction (scanned, needs OCR)
# ---------------------------------------------------------------------------

def extract_pod_records(pdf_path: Path, tmpdir: Path, progress_cb: ProgressFn = None):
    from PIL import Image

    doc = fitz.open(pdf_path)
    records = []
    total = doc.page_count
    for i, page in enumerate(doc):
        _progress(progress_cb, f"OCR-ing POD: {pdf_path.name}", i + 1, total)
        mat = fitz.Matrix(300 / 72, 300 / 72)
        pix = page.get_pixmap(matrix=mat)
        full_png = tmpdir / f"pod_{pdf_path.stem}_{i}_full.png"
        pix.save(full_png)
        full_text = ocr_image(full_png)

        w, h = pix.width, pix.height
        im = Image.open(full_png)
        crop = im.crop((int(w * 0.55), 0, w, int(h * 0.26)))
        crop_png = tmpdir / f"pod_{pdf_path.stem}_{i}_crop.png"
        crop.save(crop_png)
        crop_text = ocr_image(crop_png)

        inv_m = INVOICE_RE.search(full_text)
        ord_m = ORDER_RE.search(crop_text) or ORDER_RE.search(full_text)
        pat_m = PATIENT_RE.search(full_text)
        copy_m = COPY_TYPE_RE.search(full_text)

        invoice_no = normalize_invoice(inv_m.group(1)) if inv_m else None
        order_no = normalize_order(ord_m.group(1)) if ord_m else None
        patient = pat_m.group(1).strip().rstrip(".,") if pat_m else None
        copy_type = copy_m.group(1).lower() if copy_m else "unknown"
        copy_rank = next((v for k, v in COPY_PRIORITY.items() if k in copy_type), 9)

        records.append(
            {
                "source_pdf": pdf_path.name,
                "page_index": i,
                "invoice_no": invoice_no,
                "order_no": order_no,
                "patient_name": patient,
                "copy_type": copy_type,
                "copy_rank": copy_rank,
            }
        )
    return records


def dedupe_pod_by_invoice(pod_records):
    """Keep one copy per invoice (prefer ORIGINAL FOR RECIPIENT)."""
    best = {}
    no_invoice = []
    for r in pod_records:
        key = r["invoice_no"]
        if not key:
            no_invoice.append(r)
            continue
        cur = best.get(key)
        if cur is None or r["copy_rank"] < cur["copy_rank"]:
            best[key] = r
    return list(best.values()) + no_invoice


# ---------------------------------------------------------------------------
# Purchase Order extraction (has a text layer)
# ---------------------------------------------------------------------------

def extract_po_records(pdf_path: Path, progress_cb: ProgressFn = None):
    doc = fitz.open(pdf_path)
    records = []
    total = doc.page_count
    for i, page in enumerate(doc):
        _progress(progress_cb, f"Reading Purchase Order: {pdf_path.name}", i + 1, total)
        text = page.get_text()
        ord_m = ORDER_RE.search(text)
        order_no = normalize_order(ord_m.group(1)) if ord_m else None

        words = page.get_text("words")
        ben_word = next((w for w in words if w[4] == "Beneficiary"), None)
        ben_id_word = next((w for w in words if w[4] == "Ben"), None)
        store_word = next((w for w in words if w[4] == "Store"), None)
        patient = None
        if ben_word and ben_id_word:
            x0, x1 = ben_word[0], ben_id_word[0]
            header_bottom = max(ben_word[3], ben_id_word[3])
            if store_word:
                header_bottom = max(header_bottom, store_word[3])
            col_words = [
                w
                for w in words
                if x0 <= (w[0] + w[2]) / 2 <= x1
                and header_bottom < w[1] < header_bottom + 100
            ]
            col_words.sort(key=lambda w: (round(w[1]), w[0]))
            patient = " ".join(w[4] for w in col_words).strip()

        records.append(
            {
                "source_pdf": pdf_path.name,
                "page_index": i,
                "order_no": order_no,
                "patient_name": patient,
            }
        )
    return records


# ---------------------------------------------------------------------------
# Outward Details workbook (xlsx) -- used only to cross-check/verify matches
# ---------------------------------------------------------------------------

ORDER_COL_HINTS = ["ordno", "order no", "orderno", "order_no", "order number"]
INVOICE_COL_HINTS = ["invno", "inv no", "invoice no", "invoiceno", "gstinvno", "pinvno"]


def _find_header_row(raw: pd.DataFrame, max_scan=15):
    for i in range(min(max_scan, len(raw))):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        has_order = any(any(h in c for h in ORDER_COL_HINTS) for c in cells)
        has_invoice = any(any(h in c for h in INVOICE_COL_HINTS) for c in cells)
        if has_order and has_invoice:
            return i
    return None


def _best_column_by_content(df: pd.DataFrame, candidate_cols, value_matches_fn, sample=200):
    """Among candidate columns, pick the one whose sampled values best match the
    expected content shape (not just the header name) -- e.g. a sheet can have
    both GSTInvNo (DN/26-27/NNNNN) and PInvNo (a different supplier reference)
    and only the header name isn't enough to tell them apart."""
    best_col, best_score = None, -1.0
    for col in candidate_cols:
        values = df[col].dropna().astype(str).head(sample)
        if len(values) == 0:
            continue
        score = sum(1 for v in values if value_matches_fn(v)) / len(values)
        if score > best_score:
            best_score, best_col = score, col
    return best_col, best_score


def parse_outward_xlsx(path: Path):
    """Returns (order_to_invoice dict, meta dict) or (None, meta) if columns
    couldn't be confidently detected."""
    raw = pd.read_excel(path, header=None, sheet_name=0)
    header_row = _find_header_row(raw)
    if header_row is None:
        return None, {"detected": False, "reason": "Could not find OrdNo/InvNo columns"}

    df = pd.read_excel(path, header=header_row, sheet_name=0)
    cols_lower = {str(c).strip().lower(): c for c in df.columns}

    order_candidates = [cols_lower[c] for c in cols_lower if any(h in c for h in ORDER_COL_HINTS)]
    invoice_candidates = [cols_lower[c] for c in cols_lower if any(h in c for h in INVOICE_COL_HINTS)]

    order_col, order_score = _best_column_by_content(
        df, order_candidates, lambda v: bool(re.fullmatch(r"\d{6,}", normalize_order(v)))
    )
    invoice_col, invoice_score = _best_column_by_content(
        df, invoice_candidates, lambda v: bool(INVOICE_RE.search(v))
    )

    if order_col is None or invoice_col is None or invoice_score <= 0:
        return None, {"detected": False, "reason": "Could not find OrdNo/InvNo columns with matching data"}

    mapping = {}
    for _, row in df.iterrows():
        order_no = normalize_order(str(row[order_col])) if pd.notna(row[order_col]) else None
        invoice_no = normalize_invoice(str(row[invoice_col])) if pd.notna(row[invoice_col]) else None
        if order_no:
            mapping[order_no] = invoice_no

    return mapping, {
        "detected": True,
        "order_col": str(order_col),
        "invoice_col": str(invoice_col),
        "rows": len(df),
        "header_row": header_row,
    }


# ---------------------------------------------------------------------------
# Matching: anchored on the Purchase Order
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    matched: list = field(default_factory=list)   # (po, pod, method, verification)
    unmatched_po: list = field(default_factory=list)
    unmatched_pod: list = field(default_factory=list)
    xlsx_meta: dict = field(default_factory=dict)


def match(po_records, pod_records, order_to_invoice, xlsx_meta, name_threshold=80) -> MatchResult:
    pod_by_order = {}
    for idx, p in enumerate(pod_records):
        if p["order_no"]:
            pod_by_order.setdefault(p["order_no"], idx)

    used_pod_idx = set()
    matched = []
    unmatched_po = []

    for po in po_records:
        pidx, method = None, None
        if po["order_no"] and po["order_no"] in pod_by_order:
            cand = pod_by_order[po["order_no"]]
            if cand not in used_pod_idx:
                pidx, method = cand, "order_no"

        if pidx is None and po["patient_name"]:
            best_score, best_idx = 0, None
            pn = normalize_name(po["patient_name"])
            for idx, pod in enumerate(pod_records):
                if idx in used_pod_idx or not pod["patient_name"]:
                    continue
                score = fuzz.token_sort_ratio(pn, normalize_name(pod["patient_name"]))
                if score > best_score:
                    best_score, best_idx = score, idx
            if best_idx is not None and best_score >= name_threshold:
                pidx, method = best_idx, f"patient_name({best_score:.0f})"

        if pidx is None:
            unmatched_po.append(po)
            continue

        used_pod_idx.add(pidx)
        pod = pod_records[pidx]

        # cross-check against the outward-details workbook
        verification = "no_xlsx_data"
        if order_to_invoice is not None:
            sheet_invoice = order_to_invoice.get(po["order_no"] or "")
            if sheet_invoice is None:
                verification = "order_not_in_outward"
            elif pod["invoice_no"] and sheet_invoice and pod["invoice_no"] == sheet_invoice:
                verification = "verified"
            elif sheet_invoice:
                verification = "order_in_outward_invoice_mismatch"
            else:
                verification = "order_in_outward_no_invoice_on_record"

        matched.append((po, pod, method, verification))

    unmatched_pod = [pod_records[i] for i in range(len(pod_records)) if i not in used_pod_idx]

    return MatchResult(matched, unmatched_po, unmatched_pod, xlsx_meta or {})


# ---------------------------------------------------------------------------
# Merge output
# ---------------------------------------------------------------------------

def build_merged_pdf(result: MatchResult, pod_paths, po_paths, out_pdf: Path, out_csv: Path):
    pod_readers = {p.name: PdfReader(str(p)) for p in pod_paths}
    po_readers = {p.name: PdfReader(str(p)) for p in po_paths}

    matched_sorted = sorted(
        result.matched, key=lambda m: normalize_name(m[0]["patient_name"] or "")
    )

    writer = PdfWriter()
    rows = [
        "patient_name,order_no,invoice_no,match_method,verification,pod_source,pod_page,po_source,po_page"
    ]
    for po, pod, method, verification in matched_sorted:
        writer.add_page(pod_readers[pod["source_pdf"]].pages[pod["page_index"]])
        writer.add_page(po_readers[po["source_pdf"]].pages[po["page_index"]])
        rows.append(
            ",".join(
                str(x).replace(",", " ")
                for x in [
                    po["patient_name"],
                    po["order_no"],
                    pod["invoice_no"],
                    method,
                    verification,
                    pod["source_pdf"],
                    pod["page_index"] + 1,
                    po["source_pdf"],
                    po["page_index"] + 1,
                ]
            )
        )

    if writer.pages:
        with open(out_pdf, "wb") as f:
            writer.write(f)
    out_csv.write_text("\n".join(rows))


def run_pipeline(
    pod_paths: list[Path],
    po_paths: list[Path],
    xlsx_path: Optional[Path],
    tmpdir: Path,
    out_pdf: Path,
    out_csv: Path,
    progress_cb: ProgressFn = None,
):
    pod_records = []
    for p in pod_paths:
        pod_records.extend(extract_pod_records(p, tmpdir, progress_cb))
    pod_records = dedupe_pod_by_invoice(pod_records)

    po_records = []
    for p in po_paths:
        po_records.extend(extract_po_records(p, progress_cb))

    order_to_invoice, xlsx_meta = (None, {"detected": False, "reason": "No workbook provided"})
    if xlsx_path is not None:
        _progress(progress_cb, "Reading Outward Details workbook", 1, 1)
        order_to_invoice, xlsx_meta = parse_outward_xlsx(xlsx_path)

    _progress(progress_cb, "Matching Purchase Orders to PODs", 1, 1)
    result = match(po_records, pod_records, order_to_invoice, xlsx_meta)

    _progress(progress_cb, "Building merged PDF", 1, 1)
    build_merged_pdf(result, pod_paths, po_paths, out_pdf, out_csv)

    return result
