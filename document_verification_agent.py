"""
Karnataka RERA Document Download & Legal Verification Agent
============================================================
AI agent that:
  1. Resolves all downloadable PDF document URLs for a project
  2. Downloads each PDF from the RERA Karnataka portal
  3. Extracts text content (pdfplumber -> PyMuPDF -> fallback)
  4. Classifies each doc type and extracts structured legal entities
  5. Cross-verifies entities across all documents
  6. Produces a structured legal verification report with PASS/CAUTION/FAIL verdict

Run standalone:
  python document_verification_agent.py "CASAGRAND MOONDANCE"
"""

import os
import sys
import re
import ssl
import json
import datetime
import difflib
import io
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Tuple

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    import pymupdf as fitz  # PyMuPDF (modern import alias)
    HAS_PYMUPDF = True
except ImportError:
    try:
        import fitz  # PyMuPDF (legacy alias fallback)
        HAS_PYMUPDF = True
    except ImportError:
        HAS_PYMUPDF = False

try:
    from PIL import Image as _PILImage
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# OCR support hierarchy (CPU-safe, no AVX2 required):
# 1. RapidOCR  — ONNX Runtime, works on all CPUs, no system binary needed
# 2. pytesseract — wrapper around tesseract binary (needs: apt install tesseract-ocr)
HAS_EASYOCR = False  # disabled — requires AVX2 (PyTorch), crashes on older CPUs
_EASYOCR_READER = None

HAS_RAPIDOCR = False
_RAPIDOCR_ENGINE = None
try:
    from rapidocr_onnxruntime import RapidOCR as _RapidOCR
    HAS_RAPIDOCR = True
except ImportError:
    pass

HAS_PYTESSERACT = False
HAS_KANNADA_OCR = False
TESSERACT_LANG = "eng"
try:
    import pytesseract as _pytesseract
    _pytesseract.get_tesseract_version()  # raises if binary missing
    HAS_PYTESSERACT = True
    try:
        if "kan" in _pytesseract.get_languages(config=""):
            HAS_KANNADA_OCR = True
            TESSERACT_LANG = "kan+eng"
    except Exception:
        pass
except Exception:
    pass

# Kannada terms used on Bhoomi RTC / Kaveri EC documents.
# NOTE: validate against real documents and extend as needed.
KN_PTCL_TERMS = ["ಪಿಟಿಸಿಎಲ್", "ದರಖಾಸ್ತು", "ಸಾಗುವಳಿ ಚೀಟಿ", "ಪರಿಶಿಷ್ಟ ಜಾತಿ", "ಪರಿಶಿಷ್ಟ ಪಂಗಡ", "ಮಂಜೂರಾತಿ ಜಮೀನು"]
KN_KHARAB_TERMS = ["ಖರಾಬು", "ಖರಾಬ್"]
KN_MORTGAGE_TERMS = ["ಅಡಮಾನ", "ಭೋಗ್ಯ"]
KN_RELEASE_TERMS = ["ಋಣ ಮುಕ್ತ", "ಋಣಮುಕ್ತ", "ಬಿಡುಗಡೆ ಪತ್ರ", "ಅಡಮಾನ ಬಿಡುಗಡೆ"]
KN_NIL_TERMS = ["ಋಣಭಾರ ಇಲ್ಲ", "ಯಾವುದೇ ವ್ಯವಹಾರ ಇಲ್ಲ", "ಯಾವುದೇ ಋಣಭಾರ"]

# RERA Karnataka annexure numbers -> document type (definitive when present in the title)
ANNEXURE_DOCTYPE = {
    40: "CONVERSION_ORDER",
    41: "SALE_DEED",
    42: "JOINT_DEV_AGREEMENT",
    43: "RTC_PAHANI",
    44: "MUTATION_EXTRACT",
    45: "KHATA",
    46: "ENCUMBRANCE_CERT",
    53: "CA_FORM1",
    55: "ENG_FORM3",
    57: "ARCH_FORM2",
}

# Column labels of the RERA Karnataka "survey land" grid. The crawler flattens each grid row
# into one key made of all column labels joined together; these let us split it back.
SURVEY_GRID_LABELS = sorted([
    "Survey Number", "Is Conversion/Alienation Done ?", "Is Sale/Title/Gift Deed Executed ?",
    "Is JDA Registered ?", "Khatha Number", "Type of Land", "Conversion/Alienation Order Number",
    "Sale Deed Number", "JDA Registered Date", "Khatha Date", "Tenure of Land",
    "Conversion/Alienation Date", "Deed Execution Date", "Khatha Issuing Authority",
    "No. of Land Owners", "Conversion/Alienation Authority", "Sub Registrar Office", "Type of Khatha",
    "Extent of Land", "Conversion/Alienation Type", "Sale/Title/Gift Deed (Annexure-41)",
    "Guidance Value", "Conversion/Alienation Order (Annexure-40)", "Extent of Land as per Deed",
    "JDA (Annexure-42)", "Extent of Land as per Khatha", "RTC (Annexure-43)", "Mutation (Annexure-44)",
    "Extent of Land as per Conversion/Alienation", "Deed Executed From", "Deed Executed To",
    "Extent of Land as per JDA", "Khatha (Annexure-45)", "Encumbrance Certificate (Annexure-46)",
], key=len, reverse=True)
SURVEY_GRID_COLUMNS = 5

try:
    from project_analyzer import search_project
except ImportError:
    search_project = None


def _get_easyocr_reader():
    return None  # disabled


def _get_rapidocr_engine():
    """Lazy-init RapidOCR engine (loads ONNX models once)."""
    global _RAPIDOCR_ENGINE
    if _RAPIDOCR_ENGINE is None and HAS_RAPIDOCR:
        try:
            _RAPIDOCR_ENGINE = _RapidOCR()
        except Exception:
            pass
    return _RAPIDOCR_ENGINE


DOCTYPE_RERA_CERT   = "RERA_CERT"
DOCTYPE_SALE_DEED   = "SALE_DEED"
DOCTYPE_EC          = "ENCUMBRANCE_CERT"
DOCTYPE_RTC         = "RTC_PAHANI"
DOCTYPE_KHATA       = "KHATA"
DOCTYPE_PLAN        = "SANCTION_PLAN"
DOCTYPE_CA_FORM1    = "CA_FORM1"
DOCTYPE_ARCH_FORM2  = "ARCH_FORM2"
DOCTYPE_ENG_FORM3   = "ENG_FORM3"
DOCTYPE_JDA         = "JOINT_DEV_AGREEMENT"
DOCTYPE_MUTATION    = "MUTATION_EXTRACT"
DOCTYPE_CONVERSION  = "CONVERSION_ORDER"
DOCTYPE_OTHER       = "OTHER"

CATEGORY_LABELS = {
    DOCTYPE_RERA_CERT:  "RERA Official Certificate",
    DOCTYPE_SALE_DEED:  "Sale / Title Deed",
    DOCTYPE_EC:         "Encumbrance Certificate",
    DOCTYPE_RTC:        "Bhoomi RTC / Pahani",
    DOCTYPE_KHATA:      "Khata Certificate",
    DOCTYPE_PLAN:       "Sanctioned Plan / Layout",
    DOCTYPE_CA_FORM1:   "CA Form 1 (Financial Audit)",
    DOCTYPE_ARCH_FORM2: "Architect Form 2 (COA)",
    DOCTYPE_ENG_FORM3:  "Engineer Form 3 (Structural)",
    DOCTYPE_JDA:        "Joint Development Agreement",
    DOCTYPE_MUTATION:   "Mutation Extract (MR)",
    DOCTYPE_CONVERSION: "DC Conversion Order",
    DOCTYPE_OTHER:      "Other Filing",
}


def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _annexure_no(title: str) -> Optional[int]:
    m = re.search(r"annexure\s*-\s*(\d+)", title or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def _has_any(text: str, terms: List[str]) -> bool:
    return any(t in text for t in terms)


def _kannada_chars(text: str) -> int:
    return sum(1 for c in (text or "") if 'ಀ' <= c <= '೿')


def _norm_survey(s: Optional[str]) -> str:
    """'Sy. No 113 / 7A' -> '113/7A'"""
    s = re.sub(r"\s+", "", str(s or "")).upper().strip(".,;:")
    return s


_NAME_STOP = {"sri", "shri", "smt", "kum", "mr", "mrs", "ms", "dr", "late", "s/o", "d/o", "w/o",
              "son", "of", "wife", "daughter", "and", "others", "m/s", "pvt", "ltd", "private",
              "limited", "llp"}


def _name_tokens(name: Optional[str]) -> List[str]:
    toks = re.findall(r"[a-z]+", (name or "").lower())
    return [t for t in toks if t not in _NAME_STOP and len(t) > 1]


def _names_match(a: Optional[str], b: Optional[str]) -> bool:
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    sa, sb = set(ta), set(tb)
    if sa <= sb or sb <= sa:
        return True
    return difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.85


def _extent_sqm(extent: Optional[str]) -> Optional[float]:
    """Convert '2.10 acres', '1 Acre 10 Guntas', '1266 Sq Mtrs', '9,200 sq ft' to square metres."""
    if not extent:
        return None
    s = str(extent).lower().replace(",", "")
    total = 0.0
    found = False
    for num, unit in re.findall(r"([\d.]+)\s*(acres?|guntas?|sq\.?\s*(?:ft|feet|m|mtrs?|meters?|metres?))", s):
        try:
            v = float(num)
        except ValueError:
            continue
        found = True
        if unit.startswith("acre"):
            total += v * 4046.86
        elif unit.startswith("gunta"):
            total += v * 101.17
        elif "ft" in unit or "feet" in unit:
            total += v * 0.092903
        else:
            total += v
    return round(total, 1) if found else None


def _parse_date(s: Optional[str]) -> Optional[datetime.date]:
    if not s:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _split_grid_labels(row_text: str) -> Optional[List[str]]:
    """Split a flattened grid row ('RTC (Annexure-43) Conversion/Alienation Order (Annexure-40) ...')
    back into its column labels. Returns None if the row contains unknown text."""
    labels = []
    rest = row_text.strip()
    while rest:
        for lab in SURVEY_GRID_LABELS:
            if rest.startswith(lab) and (len(rest) == len(lab) or rest[len(lab)] == " "):
                labels.append(lab)
                rest = rest[len(lab):].strip()
                break
        else:
            return None
    return labels


class DocumentDownloadAndVerifyAgent:
    """
    AI agent that downloads, reads, and legally verifies all RERA Karnataka
    project documents (PDFs) for a given project name or registration number.
    """

    DOWNLOAD_TIMEOUT = 20      # seconds per attempt
    DOWNLOAD_ATTEMPTS = 2
    DOWNLOAD_WORKERS = 6       # parallel downloads (keep low to be polite to the RERA server)
    MAX_DOCS = 80
    UA = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self._logs: List[str] = []
        self._ssl_ctx = _make_ssl_ctx()
        self._project_surveys: List[str] = []
        self._docs_truncated = 0

    # ---- Public entry points ----

    def run(self, query: str) -> Dict[str, Any]:
        """Full pipeline: resolve -> download -> extract -> classify -> cross-verify -> report."""
        self._logs = []
        self._log(f"Agent START -- Query: '{query}'")
        db_rec, doc_list = self._step1_resolve(query)
        if not doc_list:
            return self._empty_report(query, "No downloadable documents found for this project.")
        downloaded = self._step2_download(doc_list)
        per_doc = self._step3_extract_and_classify(downloaded)
        cross = self._step5_cross_verify(per_doc, db_rec)
        report = self._step6_report(query, db_rec, per_doc, cross)
        self._log(f"Agent DONE -- Verdict: {report['final_verdict']}")
        report["agent_logs"] = self._logs
        return report

    def run_streaming(self, query: str):
        """Generator that yields SSE-formatted strings: 'data: <json>\n\n'"""
        self._logs = []

        def _sse(payload: dict) -> str:
            return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        def _emit(msg: str) -> str:
            self._logs.append(msg)
            return _sse({"type": "log", "message": msg, "ts": _ts()})

        yield _emit(f"Agent START -- Resolving documents for: '{query}'")
        db_rec, doc_list = self._step1_resolve(query)
        if not doc_list:
            yield _sse({"type": "error", "message": "No downloadable documents found."})
            return

        yield _emit(f"[Step 1/5] Found {len(doc_list)} downloadable documents.")
        yield _emit(f"[Step 2/5] Downloading {len(doc_list)} documents ({self.DOWNLOAD_WORKERS} in parallel)...")
        downloaded: List[Optional[dict]] = [None] * len(doc_list)
        done = 0
        for idx, result in self._iter_downloads(doc_list):
            downloaded[idx] = result
            done += 1
            size_kb = round(len(result["bytes"]) / 1024, 1) if result["bytes"] else 0
            ok = "OK" if result["bytes"] else ("SKIP (HTML)" if result.get("skip") else "FAIL")
            yield _emit(f"   ({done}/{len(doc_list)}) {ok} -- {result['title'][:50]} ({size_kb} KB)")

        yield _emit(f"[Step 3/5] Extracting text from {len(downloaded)} documents...")
        per_doc = []
        for item in downloaded:
            classified = self._process_item(item)
            per_doc.append(classified)
            method = classified.get("extraction_method", "none")
            ocr_flag = " [OCR]" if "ocr" in method.lower() else ""
            yield _emit(
                f"   '{classified['title'][:40]}' -> {classified.get('pages', 0)}p, "
                f"{len(classified.get('text', ''))}chars [{method}]{ocr_flag} => {classified.get('doc_type')}"
            )

        yield _emit("[Step 4/5] Cross-verifying legal entities across documents...")
        cross = self._step5_cross_verify(per_doc, db_rec)
        for flag in cross["critical_flags"]:
            yield _emit(f"   CRITICAL: {flag}")
        for w in cross["warnings"]:
            yield _emit(f"   WARNING: {w}")
        for p in cross["passed"]:
            yield _emit(f"   PASS: {p}")

        yield _emit("[Step 5/5] Generating final legal verification report...")
        report = self._step6_report(query, db_rec, per_doc, cross)
        report["agent_logs"] = self._logs
        yield _sse({"type": "report", "data": report})

    def verify_local_document(self, file_path: str, document_type: str = "General Document") -> Dict[str, Any]:
        """Analyse a single user-uploaded file (PDF or image) and return its classification,
        extracted entities and issues. Used by POST /api/documents/upload."""
        with open(file_path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(file_path.lower())[1]
        content_type = {
            ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff",
        }.get(ext, "application/octet-stream")
        hint_map = {
            "rtc": DOCTYPE_RTC, "pahani": DOCTYPE_RTC, "ec": DOCTYPE_EC, "encumbrance": DOCTYPE_EC,
            "title deed": DOCTYPE_SALE_DEED, "sale deed": DOCTYPE_SALE_DEED,
            "rera": DOCTYPE_RERA_CERT, "khata": DOCTYPE_KHATA, "khatha": DOCTYPE_KHATA,
            "mutation": DOCTYPE_MUTATION, "conversion": DOCTYPE_CONVERSION, "plan": DOCTYPE_PLAN,
        }
        dt = (document_type or "").strip().lower()
        hint = next((v for k, v in hint_map.items() if dt == k or dt.startswith(k + " ") or k in dt.split(" / ")), None)
        item = {
            "title": f"{document_type} -- {os.path.basename(file_path)}",
            "url": None, "bytes": data, "error": None, "content_type": content_type,
            "doc_type_hint": hint,
        }
        result = self._process_item(item)
        result.pop("bytes", None)
        result.pop("error", None)
        result["file_name"] = os.path.basename(file_path)
        result["file_size_kb"] = round(len(data) / 1024, 2)
        result["document_type"] = document_type
        result["text_preview"] = (result.pop("text", "") or "")[:1500]
        return result

    # ---- Step 1: Resolve documents ----

    def _step1_resolve(self, query: str):
        db_rec = None
        if search_project:
            try:
                db_rec = search_project(query)
            except Exception as e:
                self._log(f"DB search error: {e}")
        if not db_rec:
            self._log("Project not found in DB.")
            return None, []
        raw = db_rec.get("data") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        doc_list = self._extract_all_document_urls(raw, db_rec)
        # Keep land-title documents first so they are never cut off by MAX_DOCS
        doc_list.sort(key=lambda d: 0 if d.get("doc_type_hint") else 1)
        self._docs_truncated = max(0, len(doc_list) - self.MAX_DOCS)
        if self._docs_truncated:
            self._log(f"[Step 1] {self._docs_truncated} documents beyond MAX_DOCS={self.MAX_DOCS} not processed.")
        doc_list = doc_list[:self.MAX_DOCS]
        self._log(
            f"[Step 1] Matched: '{db_rec.get('project_name')}' "
            f"(Reg: {db_rec.get('registration_no')}) -- {len(doc_list)} docs"
        )
        return db_rec, doc_list

    def _extract_all_document_urls(self, raw: dict, db_rec: dict) -> List[Dict[str, str]]:
        docs = []
        seen_urls = set()
        reg_no = db_rec.get("registration_no", "")

        # URL patterns that are known to return HTML pages, not documents
        _HTML_URL_PATTERNS = (
            "casagrandegroup.com", "casagrandeindia.com",
            "facebook.com", "linkedin.com", "twitter.com",
            "/promoter-profile", "/project-details",
        )

        def _add(title, url, category_key, category_name, annexure, file_type, desc,
                 doc_type_hint=None, survey_no=None):
            if not url or not isinstance(url, str):
                return
            url = url.strip()
            if not url.startswith("http") or url.endswith("DOC_ID=") or url in seen_urls:
                return
            # Skip known HTML/website URLs that aren't legal documents
            if any(pat in url for pat in _HTML_URL_PATTERNS):
                return
            seen_urls.add(url)
            docs.append({
                "title": title, "url": url, "categoryKey": category_key,
                "categoryName": category_name, "annexure": annexure,
                "fileType": file_type, "desc": desc, "reg_no": reg_no,
                "doc_type_hint": doc_type_hint, "survey_no": survey_no,
            })

        def _urls(val):
            if isinstance(val, list):
                return [v for v in val if isinstance(v, str)]
            if isinstance(val, str):
                return [val]
            return []

        # 1. RERA Certificates
        for u in _urls(raw.get("certificate_url")):
            _add("RERA Official Registration Certificate", u, "certs",
                 "RERA Official Certificates", "Official Certificate", "PDF",
                 "Official RERA registration certificate.",
                 doc_type_hint=DOCTYPE_RERA_CERT)
        if raw.get("renewed_certificate_url"):
            _add("Renewed RERA Registration Certificate", raw["renewed_certificate_url"],
                 "certs", "RERA Official Certificates", "Renewal Order", "PDF",
                 "Renewal certificate.")
        if raw.get("further_extension_order_url"):
            _add("RERA Further Extension Order", raw["further_extension_order_url"],
                 "certs", "RERA Official Certificates", "Extension Order", "PDF",
                 "Extension order.")
        if raw.get("covid_certificate_url"):
            _add("RERA Force Majeure / COVID Extension Order", raw["covid_certificate_url"],
                 "certs", "RERA Official Certificates", "COVID Order", "PDF",
                 "COVID timeline extension order.")

        # 2. Promoter Credentials
        prom = raw.get("promoter_details") or {}
        if isinstance(prom, dict):
            for sec_name, content in prom.items():
                if isinstance(content, dict):
                    for key, val in content.items():
                        if key.endswith(" URL") or key.endswith("_url"):
                            label = re.sub(r"\s*URL$", "", key).replace("_url", "").strip()
                            for u in _urls(val):
                                _add(label, u, "promoter",
                                     "Promoter Credentials & Corporate Filings",
                                     "Promoter Credential", "Corporate PDF",
                                     f"Promoter filing: {label}.")

        # 3. Project Details & Land Titling
        proj = raw.get("project_details") or {}
        if isinstance(proj, dict):
            for sec_name, content in proj.items():
                if isinstance(content, dict):
                    for key, val in content.items():
                        if key.endswith(" URL") or key.endswith("_url"):
                            label = re.sub(r"\s*URL$", "", key).replace("_url", "").strip()
                            for u in _urls(val):
                                _add(f"{sec_name} -- {label}", u, "legal",
                                     "Legal Title Deeds & Sanctioned Plans",
                                     "Title & Legal", "Legal Document",
                                     f"Legal doc: {sec_name} - {label}.")

        # 4. Uploaded Documents Matrix
        for d in (raw.get("uploaded_documents") or []):
            if not isinstance(d, dict):
                continue
            raw_url = d.get("file_url") or d.get("url") or d.get("download_url")
            if isinstance(raw_url, list):
                raw_url = raw_url[0] if raw_url else None
            if not raw_url:
                continue
            doc_name = (d.get("financial_year") or d.get("document_name") or
                        d.get("document_type") or d.get("name") or "Document Attachment")
            _add(doc_name, raw_url, "statutory",
                 "Statutory Annexures & CA / Engineer / Architect Forms",
                 "Statutory Form", "PDF Attachment", f"RERA statutory filing: {doc_name}.")

        # 5. Enquired Documents
        for d in (raw.get("enquired_documents") or []):
            if not isinstance(d, dict):
                continue
            raw_url = d.get("file_url") or d.get("url") or d.get("download_url")
            if isinstance(raw_url, list):
                raw_url = raw_url[0] if raw_url else None
            if not raw_url:
                continue
            doc_name = d.get("document_name") or d.get("document_type") or "Enquired Document"
            _add(doc_name, raw_url, "other", "Enquired & Miscellaneous Filings",
                 "Enquired Doc", "Document", "Enquired document submitted to RERA.")

        # 6. Survey Land Tables
        # The crawler stores the per-survey land grid as a list of dicts, one per grid COLUMN,
        # where each key is a whole grid ROW of labels joined together. Rebuild which column
        # (and therefore which annexure) each URL belongs to, and which survey number it is for.
        proj_details = raw.get("project_details") or {}
        survey_tables = proj_details.get("survey_land_tables") or raw.get("survey_land_tables") or []
        survey_tables = [st for st in survey_tables if isinstance(st, dict)]
        self._project_surveys = []
        def _row0(st: dict) -> str:
            # Value of the first grid row ("Survey Number | Is Conversion/Alienation Done ? | ...").
            # Looked up by label because JSONB does not preserve key order.
            for k, v in st.items():
                if "Survey Number Is Conversion/Alienation Done" in k and not k.endswith("URL"):
                    return str(v).strip()
            return ""

        group_survey, col = None, 0
        for i, st in enumerate(survey_tables):
            items = list(st.items())
            first_val = _row0(st)
            nxt = survey_tables[i + 1] if i + 1 < len(survey_tables) else {}
            nxt_first = _row0(nxt).upper() if nxt else ""
            # A new survey group starts with the "Survey Number" column; the next column is
            # "Is Conversion/Alienation Done ?" (YES/NO).
            if group_survey is None or (col >= SURVEY_GRID_COLUMNS and nxt_first in ("YES", "NO")):
                group_survey, col = first_val, 0
                if first_val and first_val not in self._project_surveys:
                    self._project_surveys.append(first_val)
            else:
                col += 1
            for k, val in items:
                if not (k.endswith(" URL") or k.endswith("_url")):
                    continue
                row = re.sub(r"\s*URL$", "", k).replace("_url", "")
                row = re.sub(r"^For Survey Number \S+\s+", "", row)
                row = row.split(" Land Owner / Co-promoter")[0].strip()
                labels = _split_grid_labels(row)
                label = None
                if labels and col < len(labels):
                    label = labels[col]
                elif labels is None:
                    anns = re.findall(r"[A-Za-z/ ]+\(Annexure-\d+\)", row)
                    label = anns[0].strip() if len(anns) == 1 else None
                if label is None:
                    if col >= SURVEY_GRID_COLUMNS:
                        label = "Land Owner Document"
                    else:
                        label = "Land Document"
                ann = _annexure_no(label)
                title = f"{label} -- Survey No {group_survey}"
                for u in _urls(val):
                    _add(title, u, "legal",
                         "Legal Title Deeds & Sanctioned Plans",
                         f"Annexure-{ann}" if ann else "Land Titling", "Legal Document",
                         f"Survey land titling: {label} for Sy. No {group_survey}.",
                         doc_type_hint=ANNEXURE_DOCTYPE.get(ann), survey_no=group_survey)

        # 7. Quarterly Progress Updates
        for qu in (raw.get("quarterly_updates") or []):
            if not isinstance(qu, dict):
                continue
            cols = qu.get("columns") or []
            col_urls = qu.get("column_urls") or []
            for col_idx, urls_val in enumerate(col_urls):
                label = cols[col_idx] if col_idx < len(cols) else f"Attachment {col_idx+1}"
                for u in _urls(urls_val):
                    is_photo = "photo" in label.lower() or "image" in label.lower()
                    _add(label.replace("*", "").strip(), u,
                         "photos" if is_photo else "other",
                         "Construction Progress Photographs" if is_photo else "Quarterly Progress Reports",
                         "Site Photo" if is_photo else "Quarterly Filing",
                         "JPEG Image" if is_photo else "PDF Report",
                         f"Quarterly progress: {label}.")

        # 8. Completion Details
        comp_details = raw.get("completion_details") or {}
        if isinstance(comp_details, dict):
            for sec, content in comp_details.items():
                if isinstance(content, dict):
                    for k, val in content.items():
                        if k.endswith(" URL") or k.endswith("_url"):
                            label = re.sub(r"\s*URL.*$", "", k).replace("_url", "").strip()
                            for u in _urls(val):
                                _add(f"{sec} -- {label}", u, "completion",
                                     "Completion & Loan Filings",
                                     "Completion Doc", "PDF Document",
                                     f"Completion filing: {label}.")
        return docs

    # ---- Step 2: Download PDFs ----

    def _iter_downloads(self, doc_list: List[dict]):
        """Download in parallel; yields (index, result) as each download finishes."""
        with ThreadPoolExecutor(max_workers=self.DOWNLOAD_WORKERS) as pool:
            futures = {pool.submit(self._download_single, doc): i for i, doc in enumerate(doc_list)}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:  # never let one bad download kill the run
                    result = dict(doc_list[i], bytes=b"", error=str(e))
                yield i, result

    def _step2_download(self, doc_list: List[dict]) -> List[dict]:
        results: List[Optional[dict]] = [None] * len(doc_list)
        t0 = time.time()
        for i, result in self._iter_downloads(doc_list):
            results[i] = result
            size_kb = round(len(result["bytes"]) / 1024, 1) if result["bytes"] else 0
            ok = "OK" if result["bytes"] else f"FAIL: {(result.get('error') or 'unknown')[:50]}"
            self._log(f"Download {result['title'][:55]} -> {ok} ({size_kb} KB)")
        self._log(f"[Step 2] Downloaded {sum(1 for r in results if r['bytes'])}/{len(results)} "
                  f"in {time.time() - t0:.1f}s")
        return results

    def _download_single(self, doc: dict) -> dict:
        item = dict(doc)
        item["bytes"] = b""
        item["error"] = None
        url = doc.get("url", "")
        for attempt in range(1, self.DOWNLOAD_ATTEMPTS + 1):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": self.UA,
                    "Accept": "application/pdf,application/octet-stream,image/*,*/*",
                    "Referer": "https://rera.karnataka.gov.in/",
                })
                with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=self.DOWNLOAD_TIMEOUT) as resp:
                    item["http_status"] = resp.status
                    item["content_type"] = resp.headers.get("Content-Type", "")
                    # Skip HTML pages — website URLs, not legal documents
                    if "text/html" in item["content_type"].lower():
                        item["error"] = "HTML page — not a legal document"
                        item["skip"] = True
                        return item
                    # RERA servers use chunked encoding and sometimes send a premature EOF;
                    # the bytes already buffered are normally the complete document.
                    buf = io.BytesIO()
                    try:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            buf.write(chunk)
                    except Exception as read_err:
                        if "IncompleteRead" in type(read_err).__name__ or "IncompleteRead" in str(read_err):
                            if getattr(read_err, "partial", None):
                                buf.write(read_err.partial)
                        else:
                            raise
                    raw = buf.getvalue()
                    if raw:
                        item["bytes"] = raw
                        item["error"] = None
                        return item
                    item["error"] = "Empty response body"
            except Exception as e:
                item["error"] = str(e)
                if attempt < self.DOWNLOAD_ATTEMPTS:
                    time.sleep(1.5 * attempt)
        return item

    # ---- Step 3: Extract Text ----

    def _process_item(self, item: dict) -> dict:
        """Extract text from one downloaded item, then classify it and extract entities."""
        if item.get("skip"):
            item.update({
                "text": "", "pages": 0, "extraction_method": "skipped_html", "is_readable": False,
                "status": "SKIPPED", "status_badge": "SKIPPED — WEBSITE URL",
                "doc_type": DOCTYPE_OTHER, "doc_type_label": "Website URL (Skipped)",
                "entities": {}, "issues": ["URL returned an HTML page, not a legal document."],
            })
            return item
        if item.get("bytes"):
            data = item["bytes"]
            ct = (item.get("content_type") or "").lower()
            is_image = any(m in ct for m in ("image/jpeg", "image/png", "image/jpg", "image/tiff")) or \
                data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] in (b"II*\x00", b"MM\x00*")
            extracted = self._ocr_image_bytes(data, item.get("title", "")) if is_image \
                else self._extract_text(data, item.get("title", ""))
            item["text"] = extracted["text"]
            item["pages"] = extracted["pages"]
            item["extraction_method"] = extracted["method"]
            item["is_readable"] = extracted["is_readable"]
        else:
            item["text"] = ""
            item["pages"] = 0
            item["extraction_method"] = "none"
            item["is_readable"] = False
        return self._classify_and_extract(item)

    def _step3_extract_and_classify(self, downloaded: List[dict]) -> List[dict]:
        return [self._process_item(item) for item in downloaded]

    def _extract_text(self, pdf_bytes: bytes, title: str = "") -> Dict[str, Any]:
        result = {"text": "", "pages": 0, "method": "none", "is_readable": False}
        # --- Layer 1: pdfplumber (best for text-layer PDFs) ---
        if HAS_PDFPLUMBER:
            try:
                buf = io.BytesIO(pdf_bytes)
                with pdfplumber.open(buf) as pdf:
                    result["pages"] = len(pdf.pages)
                    texts = [p.extract_text() or "" for p in pdf.pages]
                    result["text"] = "\n".join(t for t in texts if t)
                    result["method"] = "pdfplumber"
                    result["is_readable"] = bool(result["text"].strip())
                    if result["is_readable"]:
                        return result
            except Exception:
                result["method"] = "pdfplumber_err"
        # --- Layer 2: PyMuPDF (wider format support) ---
        if HAS_PYMUPDF:
            try:
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                result["pages"] = doc.page_count
                texts = [page.get_text("text") for page in doc]
                doc.close()
                result["text"] = "\n".join(t for t in texts if t)
                result["method"] = "PyMuPDF"
                result["is_readable"] = bool(result["text"].strip())
                if result["is_readable"]:
                    return result
            except Exception:
                result["method"] = "PyMuPDF_err"
        # --- Layer 3: OCR fallback for scanned PDFs ---
        # Render each PDF page to an image and run OCR
        if HAS_PYMUPDF and (HAS_RAPIDOCR or HAS_PYTESSERACT):
            try:
                ocr_texts = []
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                n_pages = min(doc.page_count, 6)  # cap at 6 pages for speed
                result["pages"] = doc.page_count
                for page_num in range(n_pages):
                    page = doc[page_num]
                    # Render at 200 DPI
                    mat = fitz.Matrix(200 / 72, 200 / 72)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    img_bytes = pix.tobytes("png")
                    ocr_texts.append(self._ocr_image_bytes_raw(img_bytes))
                doc.close()
                combined = "\n".join(t for t in ocr_texts if t)
                if combined.strip():
                    result["text"] = combined
                    ocr_lib = "rapidocr" if HAS_RAPIDOCR else "tesseract"
                    result["method"] = f"OCR_{ocr_lib}"
                    result["is_readable"] = True
                    return result
            except Exception as e:
                result["method"] = f"OCR_err:{str(e)[:40]}"
        if not HAS_PDFPLUMBER and not HAS_PYMUPDF:
            result["method"] = "no_pdf_library_installed"
        return result

    def _ocr_image_bytes(self, img_bytes: bytes, title: str = "") -> Dict[str, Any]:
        """Run OCR on a raw image (JPEG/PNG bytes) — for direct image URLs."""
        result = {"text": "", "pages": 1, "method": "none", "is_readable": False}
        text = self._ocr_image_bytes_raw(img_bytes)
        if text:
            result["text"] = text
            ocr_lib = "rapidocr" if HAS_RAPIDOCR else "tesseract"
            result["method"] = f"OCR_image_{ocr_lib}"
            result["is_readable"] = True
        return result

    def _ocr_image_bytes_raw(self, img_bytes: bytes) -> str:
        """Core OCR: RapidOCR (ONNX, CPU-safe) -> pytesseract fallback."""
        # RapidOCR path — ONNX Runtime, works on all CPUs
        if HAS_RAPIDOCR:
            try:
                engine = _get_rapidocr_engine()
                if engine:
                    from PIL import Image as PILImg
                    import numpy as np
                    img = PILImg.open(io.BytesIO(img_bytes)).convert("RGB")
                    arr = np.array(img)
                    result, _ = engine(arr)
                    if result:
                        return " ".join(str(line[1]) for line in result if line and len(line) > 1)
            except Exception:
                pass
        # Pytesseract fallback — needs `apt install tesseract-ocr`
        if HAS_PYTESSERACT and HAS_PILLOW:
            try:
                from PIL import Image as PILImg
                img = PILImg.open(io.BytesIO(img_bytes))
                return _pytesseract.image_to_string(img, lang=TESSERACT_LANG)
            except Exception:
                pass
        return ""

    # ---- Step 4: Classify & Extract Entities ----

    def _detect_doc_type(self, item: dict) -> str:
        # 1. Type known from where the URL came from (certificate field / survey grid column)
        if item.get("doc_type_hint"):
            return item["doc_type_hint"]
        title = item.get("title", "") or ""
        # 2. Annexure number in the title is definitive
        ann = _annexure_no(title)
        if ann in ANNEXURE_DOCTYPE:
            return ANNEXURE_DOCTYPE[ann]
        if ann is not None:
            # Other annexures (PAN, GST, partnership deed, firm registration...) are promoter
            # filings -- don't let generic words like "registration certificate" misclassify them.
            return DOCTYPE_OTHER
        # 3. Fall back to keywords in title + text
        combined = (title + " " + (item.get("text", "") or "")).lower()
        if "encumbrance certificate" in combined or "ಋಣಭಾರ ಪ್ರಮಾಣ" in combined:
            return DOCTYPE_EC
        if "icai" in combined or "chartered accountant" in combined:
            return DOCTYPE_CA_FORM1
        if "council of architecture" in combined or "registration of coa" in combined:
            return DOCTYPE_ARCH_FORM2
        if "local authority licence" in combined or "structural engineer" in combined:
            return DOCTYPE_ENG_FORM3
        if any(k in combined for k in ["landconversion.karnataka.gov.in", "conversion order",
                                        "alienation order", "dc order", "sec 95"]):
            return DOCTYPE_CONVERSION
        if "prm/ka/rera" in combined or "real estate regulatory authority" in combined:
            return DOCTYPE_RERA_CERT
        if any(k in combined for k in ["sale deed", "title deed", "absolute sale", "registered deed"]):
            return DOCTYPE_SALE_DEED
        if "mutation register" in combined or re.search(r"\bimr[a-z0-9]+", combined):
            return DOCTYPE_MUTATION
        if any(k in combined for k in ["pahani", "rights tenancy", "bhoomi", "khatedar", "ಪಹಣಿ"]) or \
                re.search(r"\brtc\b", combined):
            return DOCTYPE_RTC
        if any(k in combined for k in ["e-khata", "a-khata", "b-khata", "e-swathu", "khata certificate",
                                        "khata no", "khatha no"]):
            return DOCTYPE_KHATA
        if any(k in combined for k in ["joint development", "development agreement"]) or re.search(r"\bjda\b", combined):
            return DOCTYPE_JDA
        if any(k in combined for k in ["sanction plan", "building plan", "layout plan", "approved plan"]):
            return DOCTYPE_PLAN
        return DOCTYPE_OTHER

    # Fields that must be found for a land document's content checks to be trusted
    KEY_FIELDS = {
        DOCTYPE_RTC: ("survey_no", "khatedar_col9"),
        DOCTYPE_MUTATION: ("survey_no", "khatedar_col9", "mutation_no"),
        DOCTYPE_EC: ("survey_no", "property_owner", "ec_no"),
        DOCTYPE_KHATA: ("khata_no", "owner"),
        DOCTYPE_RERA_CERT: ("registration_no",),
        DOCTYPE_SALE_DEED: ("vendor", "purchaser", "survey_no"),
    }

    def _classify_and_extract(self, item: dict) -> dict:
        text = (item.get("text", "") or "")
        doc_type = self._detect_doc_type(item)

        item["doc_type"] = doc_type
        item["doc_type_label"] = CATEGORY_LABELS.get(doc_type, "Other")
        entities = {}
        issues = []

        if not item.get("bytes"):
            entities["download_status"] = "FAILED"
            if item.get("error"):
                entities["download_error"] = str(item["error"])[:200]
            issues.append("Document could not be downloaded from RERA server.")
            item.update(entities=entities, issues=issues, status="DOWNLOAD_FAILED",
                        status_badge="DOWNLOAD FAILED")
            return item

        english_words = len(re.findall(r"[A-Za-z]{3,}", text))
        meaningful = english_words >= 5 or _kannada_chars(text) > 20
        if not item.get("is_readable", False) or len(text.strip()) < 30 or not meaningful:
            entities["text_status"] = "SCANNED_PDF"
            entities["pages"] = item.get("pages", 0)
            issues.append("No readable text could be extracted (scanned image / OCR failed) -- review manually.")
            item.update(entities=entities, issues=issues, status="SCANNED_PDF",
                        status_badge="SCANNED -- OCR NEEDED")
            return item

        entities["pages"] = item.get("pages", 0)
        entities["char_count"] = len(text)

        extractor_map = {
            DOCTYPE_RERA_CERT:  self._extract_rera_cert,
            DOCTYPE_SALE_DEED:  self._extract_sale_deed,
            DOCTYPE_EC:         self._extract_ec,
            DOCTYPE_RTC:        self._extract_rtc,
            DOCTYPE_MUTATION:   self._extract_rtc,
            DOCTYPE_KHATA:      self._extract_khata,
            DOCTYPE_CA_FORM1:   self._extract_ca_form1,
            DOCTYPE_ARCH_FORM2: self._extract_arch_form2,
            DOCTYPE_ENG_FORM3:  self._extract_eng_form3,
            DOCTYPE_CONVERSION: self._extract_conversion_order,
            DOCTYPE_PLAN:       self._extract_sanction_plan,
        }
        extractor = extractor_map.get(doc_type, self._extract_generic)
        entities.update(extractor(text))

        # If none of a land document's key fields were found, the absence of red flags
        # (PTCL, mortgage...) proves nothing -- e.g. a Kannada RTC OCR'd without Kannada support.
        key_fields = self.KEY_FIELDS.get(doc_type)
        if key_fields and not any(entities.get(f) for f in key_fields):
            entities["content_unverified"] = True
            note = "Key fields (survey no / owner) not found in text"
            if entities.get("language") == "Kannada" or _kannada_chars(text) > 20:
                note += " -- document is in Kannada"
            if "OCR" in (item.get("extraction_method") or "") and not HAS_KANNADA_OCR:
                note += " -- Kannada OCR pack (tesseract-ocr-kan) not installed"
            issues.append(note + "; red-flag checks could not be completed.")

        if doc_type == DOCTYPE_EC and entities.get("has_active_mortgage") and not entities.get("discharge_deed_found"):
            issues.append("Active mortgage found in EC -- no Discharge/Release Deed detected.")
        if doc_type in (DOCTYPE_RTC, DOCTYPE_MUTATION):
            if entities.get("ptcl_flag"):
                issues.append("PTCL/SC-ST Grant Land restriction -- prior Govt approval required.")
            if entities.get("kharab_flag"):
                issues.append("Kharab (Govt Reserve Land) entry detected.")
        if doc_type == DOCTYPE_KHATA and entities.get("khata_type") == "B-Khata":
            issues.append("B-Khata -- bank financing restricted, legal title questionable.")
        if doc_type == DOCTYPE_CA_FORM1 and entities.get("escrow_balance_zero"):
            issues.append("CA Form 1: Escrow balance may be zero -- verify RERA compliance.")

        item["entities"] = entities
        item["issues"] = issues
        has_critical = any("PTCL" in i or "mortgage" in i.lower() or "B-Khata" in i for i in issues)
        item["status"] = "CRITICAL" if has_critical else ("CAUTION" if issues else "VERIFIED")
        badges = {"VERIFIED": "VERIFIED", "CAUTION": "CAUTION", "CRITICAL": "CRITICAL FLAG"}
        item["status_badge"] = badges.get(item["status"], "REVIEWED")
        return item

    def _find(self, pattern: str, text: str, group: int = 1, flags=re.IGNORECASE):
        m = re.search(pattern, text, flags)
        return _clean(m.group(group)) if m else None

    def _is_kannada(self, text: str) -> bool:
        """Returns True if the text is predominantly Kannada script."""
        kannada_chars = sum(1 for c in text if '\u0C80' <= c <= '\u0CFF')
        return kannada_chars > 20

    def _extract_rera_cert(self, text: str) -> dict:
        e = {}
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        # RERA reg no — flexible: 3 or 4 digit segments
        e["registration_no"] = (
            self._find(r"(PRM/KA/RERA/\d{3,4}/\d{3,4}/[A-Z0-9/]+)", text) or
            self._find(r"Registration\s*No[.:]?\s*([A-Z0-9/\-]+)", text)
        )
        # ACK no is the acknowledgement number (line 0 often)
        e["ack_no"] = self._find(r"(ACK/KA/RERA/[A-Z0-9/]+)", text)
        # Project name: look for line containing "SURVEY NO" or after RERA no line
        e["project_name"] = self._find(r"([A-Z][A-Z\s]+),\s*SURVEY NO", text)
        # Promoter: line after project address (3rd–5th line typically)
        if len(lines) >= 4:
            # Find first ALL-CAPS line that looks like a company name
            for line in lines[2:]:
                if re.match(r'^[A-Z][A-Z\s\.]{10,}(?:PVT|LTD|LIMITED|BUILDERS|DEVELOPERS|PROPERTIES).*', line):
                    e["promoter_name"] = line
                    break
        e["promoter_name"] = e.get("promoter_name") or self._find(
            r"(?:Promoter|Developer|Firm)[:\s]+([^\n]{5,80})", text
        )
        # Dates: explicit "valid upto" wins; otherwise the cert lists the issue date first
        # and the validity (completion) date last.
        dates = re.findall(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b", text)
        explicit = self._find(r"valid(?:ity)?\s*(?:upto|till|until|up to)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        if dates:
            e["issue_date"] = dates[0]
        if explicit:
            e["validity_date"] = explicit
        elif len(dates) >= 2:
            e["validity_date"] = dates[-1]
        return {k: v for k, v in e.items() if v is not None}

    def _extract_sale_deed(self, text: str) -> dict:
        e = {}
        e["vendor"] = self._find(r"(?:vendor|seller|transferor|executant)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["purchaser"] = self._find(r"(?:purchaser|buyer|transferee)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["sro"] = self._find(r"(?:sub.?registrar|sro)[^\n]{0,20}(?:office)?[:\s]+([A-Za-z\s]{3,40})", text)
        e["execution_date"] = self._find(r"(?:executed?|registered?)\s+(?:on|this|dated)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        e["consideration"] = self._find(r"(?:consideration|sale\s+price)[:\s]+(?:Rs\.?\s*)?(\d[\d,]+(?:\s*(?:lakhs?|lacs?|crores?))?)", text)
        e["survey_no"] = self._find(r"(?:survey\s+no|sy\.?\s*no\.?)[.:\s]+([0-9][0-9/A-Za-z]*(?:\s*/\s*[0-9A-Za-z]+)*)", text)
        e["extent"] = self._find(r"(?:extent|area|measuring)[:\s]+([0-9.,]+\s*(?:acres?|guntas?|sq\.?\s*(?:ft|m\w*))(?:\s*[0-9.]+\s*guntas?)?)", text)
        e["doc_no"] = self._find(r"(?:document\s+no|doc\.?\s+no\.?)[:\s]+([0-9/\-]+)", text)
        return {k: v for k, v in e.items() if v is not None}

    _RELEASE_RE = re.compile(
        r"\b(?:discharge|release|reconveyance|redemption)\s+deed\b"
        r"|\bmortgage\s+(?:is\s+|has\s+been\s+)?(?:released|discharged|redeemed|cleared|closed)\b"
        r"|\bloan\s+(?:fully\s+)?(?:closed|repaid|discharged)\b"
        r"|\bno\s+objection\s+certificate\s+(?:from|issued\s+by)\s+[a-z ]*bank\b"
        r"|\bbank\s+noc\b|\bform\s*16\b",
        re.IGNORECASE)
    _MORTGAGE_RE = re.compile(r"\b(?:simple\s+|equitable\s+|usufructuary\s+)?mortgage\b|\bhypothecation\b",
                              re.IGNORECASE)

    def _extract_ec(self, text: str) -> dict:
        e = {}
        text_lower = text.lower()
        is_kannada = _kannada_chars(text) > 20
        e["language"] = "Kannada" if is_kannada else "English"
        e["ec_no"] = self._find(r"((?:ECC-[0-9A-Z\-]+|RRN-EC-[A-Z0-9\-]+))", text)
        e["period"] = self._find(r"(?:from|period)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+to\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        e["property_owner"] = self._find(r"(?:owner|khatedar|party)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["survey_no"] = self._find(r"(?:survey\s+no|sy\.?\s*no\.?)[.:\s]+([0-9/A-Za-z]+(?:\s*/\s*[0-9A-Za-z]+)*)", text)
        has_mortgage = bool(self._MORTGAGE_RE.search(text)) or _has_any(text, KN_MORTGAGE_TERMS)
        released = bool(self._RELEASE_RE.search(text)) or _has_any(text, KN_RELEASE_TERMS)
        e["has_active_mortgage"] = has_mortgage and not released
        if has_mortgage:
            e["mortgage_found"] = True
            e["discharge_deed_found"] = released
            e["mortgage_bank"] = self._find(r"(HDFC|ICICI|SBI|Axis|Canara|Bank\s+of\s+[A-Z]\w+|[A-Z]\w+\s+Bank)", text)
            e["mortgage_amount"] = self._find(r"(?:Rs\.?\s*)([\d,]+\s*(?:lakhs?|crores?)?)\s*(?:loan|mortgage)", text)
        nil_phrase = bool(re.search(r"\bnil\s+encumbrance\b|\bno\s+encumbrances?\b|\bno\s+transactions?\b"
                                    r"|\bencumbrance\s*[:\-]\s*nil\b", text_lower)) or _has_any(text, KN_NIL_TERMS)
        e["nil_encumbrance"] = nil_phrase and not has_mortgage
        if is_kannada and not HAS_KANNADA_OCR:
            e["note"] = "EC is in Kannada script — install tesseract-ocr-kan for full entity extraction"
        return {k: v for k, v in e.items() if v is not None}

    def _extract_rtc(self, text: str) -> dict:
        e = {}
        text_lower = text.lower()
        is_kannada = _kannada_chars(text) > 20
        e["language"] = "Kannada" if is_kannada else "English"
        e["survey_no"] = self._find(r"(?:sy\.?\s*no\.?|survey\s+no)[:\s]+([0-9/A-Za-z]+(?:\s*/\s*[0-9A-Za-z]+)*)", text)
        e["khatedar_col9"] = self._find(r"(?:col\.?\s*9|column\s*9|khatedar|hissedhar)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["extent"] = self._find(r"(?:extent|area)[:\s]+([0-9.]+\s*(?:acres?|guntas?)(?:\s*[0-9.]+\s*guntas?)?)", text)
        e["ptcl_flag"] = bool(re.search(r"\bptcl\b|\bsc/st\b|\bscheduled\s+(?:caste|tribe)\b|\bsaguvali\b"
                                        r"|\bgrant(?:ed)?\s+land\b|\bdarkhast\b", text_lower)) or _has_any(text, KN_PTCL_TERMS)
        e["kharab_flag"] = bool(re.search(r"\b[ab]\s*-?\s*kharab\b|\bkharab\b|\bpd\s+kharab\b", text_lower)) or \
            _has_any(text, KN_KHARAB_TERMS)
        e["mutation_no"] = self._find(r"(IMR[A-Z0-9]+)", text)
        e["mutation_date"] = self._find(r"(\d{2}/\d{2}/\d{4})", text)
        if is_kannada and not HAS_KANNADA_OCR:
            e["note"] = "RTC/Mutation is in Kannada script — install tesseract-ocr-kan for full entity extraction"
        return {k: v for k, v in e.items() if v is not None}

    def _extract_khata(self, text: str) -> dict:
        e = {}
        text_lower = text.lower()
        e["khata_no"] = self._find(r"(?:khata\s+no|khatha\s+no)[.:\s]+([0-9/\-A-Z]+)", text)
        e["pid"] = self._find(r"(?:pid|property\s+id)[.:\s]+([0-9\-/]+)", text)
        e["owner"] = self._find(r"(?:owner|occupant|holder)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["ulb"] = self._find(r"(BBMP|CMC|TMC|Panchayat|Municipal\s+Corporation)", text)
        if "a-khata" in text_lower or "a khata" in text_lower:
            e["khata_type"] = "A-Khata"
        elif "b-khata" in text_lower or "b khata" in text_lower or "property tax register" in text_lower:
            e["khata_type"] = "B-Khata"
        else:
            e["khata_type"] = "Unknown"
        e["tax_paid_year"] = self._find(r"(?:tax\s+paid|property\s+tax)[:\s]+([0-9\-]+)", text)
        return {k: v for k, v in e.items() if v is not None}

    def _extract_ca_form1(self, text: str) -> dict:
        e = {}
        e["ca_name"] = self._find(r"(?:chartered\s+accountant|ca)[:\s]+([A-Z][^,\n]{3,50})", text)
        e["icai_no"] = self._find(r"(?:icai|membership\s+no)[:\s]+([A-Z0-9\-/]+)", text)
        e["financial_year"] = self._find(r"(?:financial\s+year|fy)[:\s]+([0-9\-/]+)", text)
        e["escrow_balance"] = self._find(r"(?:escrow|designated\s+account)[^:]*[:\s]+(?:Rs\.?\s*)?(\d[\d,]*)", text)
        escrow_str = str(e.get("escrow_balance", "1"))
        clean_escrow = escrow_str.replace(",", "").replace(".", "").replace("0", "").replace(" ", "")
        e["escrow_balance_zero"] = len(clean_escrow) == 0
        return {k: v for k, v in e.items() if v is not None}

    def _extract_arch_form2(self, text: str) -> dict:
        e = {}
        e["architect_name"] = self._find(r"(?:architect)[:\s]+([A-Z][^,\n]{3,50})", text)
        e["coa_no"] = self._find(r"(?:coa|council\s+of\s+architecture)[:\s]+([A-Z0-9\-/]+)", text)
        e["far"] = self._find(r"(?:far|floor\s+area\s+ratio)[:\s]+([0-9.]+)", text)
        e["plot_area"] = self._find(r"(?:plot\s+area|site\s+area)[:\s]+([0-9,.]+\s*(?:sq\.?\s*(?:ft|m)))", text)
        return {k: v for k, v in e.items() if v is not None}

    def _extract_eng_form3(self, text: str) -> dict:
        e = {}
        e["engineer_name"] = self._find(r"(?:structural\s+engineer|engineer)[:\s]+([A-Z][^,\n]{3,50})", text)
        e["licence_no"] = self._find(r"(?:licence\s+no|license\s+no)[:\s]+([A-Z0-9\-/]+)", text)
        e["certification_date"] = self._find(r"(?:certified?|dated?)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        return {k: v for k, v in e.items() if v is not None}

    def _extract_conversion_order(self, text: str) -> dict:
        e = {}
        e["dc_order_no"] = self._find(r"(?:order\s+no|dc\s+order)[:\s]+([A-Z0-9\-/\s]+?)(?:\n|,)", text)
        e["survey_no"] = self._find(r"(?:sy\.?\s*no\.?|survey\s+no)[:\s]+([0-9/A-Za-z\s]+?)(?:\n|,)", text)
        e["extent"] = self._find(r"(?:extent|area)[:\s]+([0-9.]+\s*(?:acres?|guntas?))", text)
        e["conversion_date"] = self._find(r"(?:dated?|issued?)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        e["conversion_fine_paid"] = bool(re.search(r"(?:fine|conversion\s+fee)\s+paid", text.lower()))
        return {k: v for k, v in e.items() if v is not None}

    def _extract_sanction_plan(self, text: str) -> dict:
        e = {}
        e["approval_authority"] = self._find(r"(BBMP|BDA|BMRDA|BIAPPA|Town\s+Planning)", text)
        e["approval_no"] = self._find(r"(?:approval\s+no|sanction\s+no)[:\s]+([A-Z0-9\-/]+)", text)
        e["approval_date"] = self._find(r"(?:approved?\s+on|sanctioned?\s+on|dated?)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        e["floors_approved"] = self._find(r"(?:floor|storey)[:\s]+([0-9]+)", text)
        e["fire_noc"] = bool(re.search(r"(?:fire\s+noc|fire\s+clearance)", text.lower()))
        return {k: v for k, v in e.items() if v is not None}

    def _extract_generic(self, text: str) -> dict:
        e = {}
        e["summary"] = _clean(text[:400]) if text else "No text extracted."
        survey = self._find(r"(?:sy\.?\s*no\.?|survey\s+no)[:\s]+([0-9/A-Za-z\s,]+?)(?:\n|,)", text)
        if survey:
            e["survey_no_found"] = survey
        return e

    # ---- Step 5: Cross-Verification ----

    @staticmethod
    def _readable(d: dict) -> bool:
        """Downloaded, text extracted, and key fields found."""
        return (d.get("status") in ("VERIFIED", "CAUTION", "CRITICAL")
                and not (d.get("entities") or {}).get("content_unverified"))

    def _step5_cross_verify(self, per_doc: List[dict], db_rec: Optional[dict] = None) -> dict:
        passed: List[str] = []
        warnings: List[str] = []
        critical_flags: List[str] = []

        def of(*types):
            return [d for d in per_doc if d.get("doc_type") in types]

        def ent(d, f):
            return (d.get("entities") or {}).get(f)

        def first(docs, f):
            for d in docs:
                if ent(d, f) is not None:
                    return ent(d, f)
            return None

        def presence(label, types, required=True):
            docs = of(*types)
            got = [d for d in docs if d.get("bytes")]
            readable = [d for d in docs if self._readable(d)]
            if readable:
                passed.append(f"{label} present and read ({len(readable)} document(s)).")
            elif got:
                warnings.append(f"{label} downloaded but its text could not be read -- verify manually.")
            elif docs:
                warnings.append(f"{label} listed on RERA but could not be downloaded.")
            elif required:
                warnings.append(f"{label} not found.")
            return readable

        # ---- Presence of key documents ----
        rera_docs = presence("RERA Registration Certificate", [DOCTYPE_RERA_CERT])
        ec_docs = presence("Encumbrance Certificate (EC)", [DOCTYPE_EC])
        rtc_docs = presence("Bhoomi RTC / Pahani", [DOCTYPE_RTC])
        mut_docs = presence("Mutation Extract", [DOCTYPE_MUTATION], required=False)
        deed_docs = presence("Sale / Title Deed", [DOCTYPE_SALE_DEED], required=False)
        khata_docs = presence("Khata", [DOCTYPE_KHATA], required=False)
        ca_docs = presence("CA Form 1 (Financial Audit)", [DOCTYPE_CA_FORM1])
        presence("Architect Form 2 (COA)", [DOCTYPE_ARCH_FORM2])
        presence("Engineer Form 3 (Structural Certificate)", [DOCTYPE_ENG_FORM3])
        presence("DC Conversion Order", [DOCTYPE_CONVERSION], required=False)
        presence("Sanctioned Building Plan", [DOCTYPE_PLAN])
        land_docs = rtc_docs + mut_docs

        # ---- PTCL / Kharab (RTC + Mutation) ----
        ptcl = [d for d in land_docs if ent(d, "ptcl_flag")]
        if ptcl:
            critical_flags.append(
                "PTCL/SC-ST Govt Grant Restriction in RTC/Mutation (" +
                "; ".join(d["title"] for d in ptcl[:3]) +
                ") -- Transfer is VOID without AC permission. (Karnataka PTCL Act 1978, Sec 4 & 5)")
        elif rtc_docs:
            passed.append(f"No PTCL/SC-ST Grant Land restriction in {len(rtc_docs)} readable RTC(s)"
                          + (f" and {len(mut_docs)} mutation extract(s)." if mut_docs else "."))
        elif mut_docs:
            warnings.append("PTCL/SC-ST grant-land check done on mutation extracts only -- no readable RTC.")
        else:
            warnings.append("PTCL/SC-ST grant-land check NOT done -- no readable RTC/Mutation document.")
        kharab = [d for d in land_docs if ent(d, "kharab_flag")]
        if kharab:
            warnings.append("Kharab (Govt reserve land) entry in RTC: " + "; ".join(d["title"] for d in kharab[:3]))

        # ---- Encumbrance ----
        if ec_docs:
            active = [d for d in ec_docs if ent(d, "has_active_mortgage")]
            released = [d for d in ec_docs if ent(d, "mortgage_found") and not ent(d, "has_active_mortgage")]
            if active:
                critical_flags.append(
                    "Active bank mortgage in EC with NO Discharge/Release Deed (" +
                    "; ".join(d["title"] for d in active[:3]) +
                    ") -- Demand Bank NOC + registered Discharge Deed before payment.")
            if released:
                passed.append("Mortgage in EC shows as released/discharged (" +
                              "; ".join(d["title"] for d in released[:3]) + ").")
            if not active and all(ent(d, "nil_encumbrance") for d in ec_docs):
                passed.append("All readable ECs show NIL encumbrance.")
            elif not active and not released:
                warnings.append("EC has entries but no clear NIL/mortgage status -- review EC transactions manually.")
        else:
            warnings.append("Encumbrance (mortgage) check NOT done -- no readable EC.")

        # ---- Khata ----
        ktypes = {ent(d, "khata_type") for d in khata_docs}
        if "B-Khata" in ktypes:
            critical_flags.append("B-Khata detected -- property in unauthorized layout; bank financing severely restricted.")
        elif "A-Khata" in ktypes:
            passed.append("A-Khata certificate present -- property is bank loan eligible.")
        elif khata_docs:
            warnings.append("Khata type (A/B) could not be determined from the Khata document.")

        # ---- CA escrow ----
        if any(ent(d, "escrow_balance_zero") for d in ca_docs if self._readable(d)):
            warnings.append("CA Form 1: escrow (designated account) balance appears to be zero.")

        # ---- RERA certificate vs database ----
        db_reg = _norm_survey((db_rec or {}).get("registration_no"))
        cert_reg = _norm_survey(first(rera_docs, "registration_no"))
        if cert_reg and db_reg:
            if cert_reg == db_reg:
                passed.append("RERA certificate registration number matches the RERA project record.")
            else:
                critical_flags.append(
                    f"RERA certificate number {cert_reg} does NOT match project registration {db_reg}.")
        validity = _parse_date(first(rera_docs, "validity_date"))
        if validity and validity < datetime.date.today():
            warnings.append(f"RERA registration validity ({validity.isoformat()}) has passed -- check for an extension order.")

        # ---- Survey-wise cross-document consistency ----
        project_surveys = [_norm_survey(x) for x in self._project_surveys if x]

        def _survey_eq(a, b):
            a, b = _norm_survey(a), _norm_survey(b)
            return bool(a and b) and (a == b or a.rstrip("P") == b.rstrip("P"))

        for d in ec_docs + land_docs + deed_docs:
            filed_for = d.get("survey_no")
            found = ent(d, "survey_no")
            if not found:
                continue
            if filed_for and not _survey_eq(filed_for, found):
                critical_flags.append(
                    f"Survey number mismatch: '{d['title']}' is filed for Sy. No {filed_for} "
                    f"but the document shows Sy. No {found}.")
            elif not filed_for and project_surveys and not any(_survey_eq(found, p) for p in project_surveys):
                warnings.append(f"'{d['title']}' refers to Sy. No {found}, which is not in the project's "
                                f"survey list ({', '.join(project_surveys[:6])}).")

        def _sv(d):
            return d.get("survey_no") or ent(d, "survey_no") or "?"

        groups: Dict[str, Dict[str, List[dict]]] = {}
        for kind, docs in (("ec", ec_docs), ("rtc", land_docs), ("deed", deed_docs)):
            for d in docs:
                groups.setdefault(_norm_survey(_sv(d)), {}).setdefault(kind, []).append(d)
        unassigned_deeds = groups.pop("?", {}).get("deed", [])

        owners_checked = 0
        for sv, g in groups.items():
            deeds = g.get("deed") or unassigned_deeds
            vendors = [ent(d, "vendor") for d in deeds if ent(d, "vendor")]
            buyers = [ent(d, "purchaser") for d in deeds if ent(d, "purchaser")]
            deed_parties = vendors + buyers
            rtc_owners = [ent(d, "khatedar_col9") for d in g.get("rtc", []) if ent(d, "khatedar_col9")]
            ec_owners = [ent(d, "property_owner") for d in g.get("ec", []) if ent(d, "property_owner")]

            for o in rtc_owners:
                if deed_parties:
                    owners_checked += 1
                    if not any(_names_match(o, p) for p in deed_parties):
                        critical_flags.append(
                            f"Owner mismatch on Sy. No {sv}: RTC khatedar '{o}' is neither the deed vendor "
                            f"({', '.join(vendors) or 'n/a'}) nor the purchaser ({', '.join(buyers) or 'n/a'}).")
            for o in ec_owners:
                ref = deed_parties + rtc_owners
                if ref:
                    owners_checked += 1
                    if not any(_names_match(o, p) for p in ref):
                        warnings.append(
                            f"EC owner '{o}' on Sy. No {sv} does not match the deed parties or RTC khatedar "
                            f"({', '.join(ref)}) -- check the title chain.")

            deed_ext = next((_extent_sqm(ent(d, "extent")) for d in deeds if ent(d, "extent")), None)
            rtc_ext = next((_extent_sqm(ent(d, "extent")) for d in g.get("rtc", []) if ent(d, "extent")), None)
            if deed_ext and rtc_ext:
                diff = abs(deed_ext - rtc_ext) / max(deed_ext, rtc_ext)
                if diff > 0.05:
                    warnings.append(f"Extent mismatch on Sy. No {sv}: deed {deed_ext:.0f} sq m vs RTC "
                                    f"{rtc_ext:.0f} sq m ({diff:.0%} difference).")
                else:
                    passed.append(f"Land extent on Sy. No {sv} matches between deed and RTC.")

        if owners_checked and not any("Owner mismatch" in f for f in critical_flags) \
                and not any("EC owner" in w for w in warnings):
            passed.append(f"Owner names consistent across deed / RTC / EC ({owners_checked} comparison(s)).")
        elif not owners_checked and (land_docs or ec_docs):
            warnings.append("Owner-name cross-check NOT done -- owners could not be read from both the deed and RTC/EC.")

        # Survey coverage: every survey in the project should have a readable RTC and EC
        if project_surveys:
            no_rtc = [p for p in project_surveys if not any(_survey_eq(_sv(d), p) for d in rtc_docs)]
            no_ec = [p for p in project_surveys if not any(_survey_eq(_sv(d), p) for d in ec_docs)]
            if no_rtc:
                warnings.append(f"No readable RTC for Sy. No(s): {', '.join(no_rtc[:10])}.")
            if no_ec:
                warnings.append(f"No readable EC for Sy. No(s): {', '.join(no_ec[:10])}.")

        # ---- Processing problems ----
        scanned = [d["title"] for d in per_doc if d.get("status") == "SCANNED_PDF"]
        if scanned:
            warnings.append(f"{len(scanned)} document(s) had no readable text -- OCR required: " + "; ".join(scanned[:3]))
        failed_dl = [d["title"] for d in per_doc if d.get("status") == "DOWNLOAD_FAILED"]
        if failed_dl:
            warnings.append(f"{len(failed_dl)} document(s) failed to download: " + "; ".join(failed_dl[:3]))
        if self._docs_truncated:
            warnings.append(f"{self._docs_truncated} further document(s) were not processed (limit {self.MAX_DOCS}).")

        rtc0 = (land_docs or [{}])[0]
        ec0 = (ec_docs or [{}])[0]
        deed0 = (deed_docs or [{}])[0]
        entity_map = {
            "seller_from_deed": ent(deed0, "vendor") if deed0 else None,
            "purchaser_from_deed": ent(deed0, "purchaser") if deed0 else None,
            "ec_owner": ent(ec0, "property_owner") if ec0 else None,
            "rtc_owner_col9": ent(rtc0, "khatedar_col9") if rtc0 else None,
            "deed_survey_no": ent(deed0, "survey_no") if deed0 else None,
            "ec_survey_no": ent(ec0, "survey_no") if ec0 else None,
            "rtc_survey_no": ent(rtc0, "survey_no") if rtc0 else None,
            "deed_extent": ent(deed0, "extent") if deed0 else None,
            "rtc_extent": ent(rtc0, "extent") if rtc0 else None,
            "khata_type": next(iter(k for k in ktypes if k and k != "Unknown"), None),
            "rera_registration_no": cert_reg or None,
            "rera_validity_date": first(rera_docs, "validity_date"),
            "project_survey_numbers": ", ".join(self._project_surveys) or None,
        }

        core_readable = bool(rera_docs) and bool(ec_docs) and bool(rtc_docs)

        def _dedupe(xs):
            return list(dict.fromkeys(xs))

        return {
            "passed": _dedupe(passed),
            "warnings": _dedupe(warnings),
            "critical_flags": _dedupe(critical_flags),
            "entity_map": entity_map,
            "core_documents_verified": core_readable,
        }

    # ---- Step 6: Produce Report ----

    def _step6_report(self, query: str, db_rec, per_doc: List[dict], cross: dict) -> dict:
        project_name = (db_rec or {}).get("project_name") or query
        reg_no = (db_rec or {}).get("registration_no") or "N/A"
        verified = sum(1 for d in per_doc if d.get("status") == "VERIFIED")
        caution = sum(1 for d in per_doc if d.get("status") == "CAUTION")
        critical_c = sum(1 for d in per_doc if d.get("status") == "CRITICAL")
        scanned = sum(1 for d in per_doc if d.get("status") == "SCANNED_PDF")
        failed = sum(1 for d in per_doc if d.get("status") == "DOWNLOAD_FAILED")
        downloaded = sum(1 for d in per_doc if d.get("bytes"))
        n_critical = len(cross["critical_flags"])
        n_warn = len(cross["warnings"])
        if downloaded == 0:
            verdict = "INCOMPLETE -- NO DOCUMENTS COULD BE DOWNLOADED, NOTHING WAS VERIFIED"
            verdict_badge = "UNABLE TO VERIFY"
            risk_score = None
        elif n_critical > 0:
            verdict = "FAIL -- CRITICAL LEGAL RISKS IDENTIFIED"
            verdict_badge = "DO NOT BUY"
            risk_score = max(0, 40 - (n_critical * 15))
        elif not cross.get("core_documents_verified"):
            verdict = "INCOMPLETE -- RERA CERTIFICATE / EC / RTC COULD NOT ALL BE READ"
            verdict_badge = "MANUAL REVIEW REQUIRED"
            risk_score = 50
        elif n_warn > 3:
            verdict = "CAUTION -- MULTIPLE COMPLIANCE GAPS"
            verdict_badge = "BUY WITH CAUTION"
            risk_score = 65
        elif n_warn > 0:
            verdict = "CONDITIONAL PASS -- MINOR ITEMS TO VERIFY"
            verdict_badge = "REVIEW REQUIRED"
            risk_score = 80
        else:
            verdict = "PASS -- ALL VERIFIED DOCUMENTS CLEAR"
            verdict_badge = "CLEAR TO PROCEED"
            risk_score = 96
        doc_results = [{k: v for k, v in d.items() if k != "bytes"} for d in per_doc]
        return {
            "project_name": project_name,
            "registration_no": reg_no,
            "query": query,
            "generated_at": datetime.datetime.now().isoformat(),
            "summary": {
                "total_documents": len(per_doc), "downloaded": downloaded, "verified": verified,
                "caution": caution, "critical": critical_c,
                "scanned_pdf": scanned, "download_failed": failed,
            },
            "cross_verification": {
                "passed": cross["passed"],
                "warnings": cross["warnings"],
                "critical_flags": cross["critical_flags"],
            },
            "entity_map": cross["entity_map"],
            "per_document_results": doc_results,
            "legal_risk_score": risk_score,
            "final_verdict": verdict,
            "verdict_badge": verdict_badge,
            "kannada_ocr_available": HAS_KANNADA_OCR,
        }

    def _log(self, msg: str):
        self._logs.append(f"[{_ts()}] {msg}")

    def _empty_report(self, query: str, reason: str) -> dict:
        return {
            "project_name": query, "registration_no": "N/A", "query": query,
            "generated_at": datetime.datetime.now().isoformat(),
            "summary": {"total_documents": 0, "verified": 0, "caution": 0, "critical": 0, "scanned_pdf": 0, "download_failed": 0},
            "cross_verification": {"passed": [], "warnings": [reason], "critical_flags": []},
            "entity_map": {}, "per_document_results": [],
            "legal_risk_score": None, "final_verdict": "INCOMPLETE -- " + reason,
            "verdict_badge": "UNABLE TO VERIFY", "agent_logs": self._logs,
        }


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "CASAGRAND MOONDANCE"
    print(f"\nRERA Document Verification Agent -- Query: '{query}'\n{'='*60}")
    agent = DocumentDownloadAndVerifyAgent()
    report = agent.run(query)
    print(f"\nPROJECT : {report['project_name']}")
    print(f"REG NO  : {report['registration_no']}")
    print(f"DOCS    : {report['summary']['total_documents']} scanned")
    print(f"VERDICT : {report['verdict_badge']} -- {report['final_verdict']}")
    score = report['legal_risk_score']
    print(f"SCORE   : {score if score is not None else 'n/a'}/100")
    if report["cross_verification"]["critical_flags"]:
        print("\nCRITICAL FLAGS:")
        for f in report["cross_verification"]["critical_flags"]:
            print(f"  * {f}")
    if report["cross_verification"]["warnings"]:
        print("\nWARNINGS:")
        for w in report["cross_verification"]["warnings"]:
            print(f"  * {w}")
