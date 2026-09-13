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
import io
import urllib.request
from typing import Dict, Any, List, Optional

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
    from project_analyzer import search_project
except ImportError:
    search_project = None


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


class DocumentDownloadAndVerifyAgent:
    """
    AI agent that downloads, reads, and legally verifies all RERA Karnataka
    project documents (PDFs) for a given project name or registration number.
    """

    DOWNLOAD_TIMEOUT = 30
    MAX_DOCS = 40
    UA = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self._logs: List[str] = []
        self._ssl_ctx = _make_ssl_ctx()

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
        cross = self._step5_cross_verify(per_doc)
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

        downloaded = []
        for i, doc in enumerate(doc_list, 1):
            yield _emit(f"[Step 2/5] Downloading ({i}/{len(doc_list)}): {doc['title'][:60]}...")
            result = self._download_single(doc)
            downloaded.append(result)
            size_kb = round(len(result["bytes"]) / 1024, 1) if result["bytes"] else 0
            ok = "OK" if result["bytes"] else "FAIL"
            yield _emit(f"   {ok} -- {doc['title'][:50]} ({size_kb} KB)")

        yield _emit(f"[Step 3/5] Extracting text from {len(downloaded)} PDFs...")
        per_doc = []
        for item in downloaded:
            if item["bytes"]:
                extracted = self._extract_text(item["bytes"], item["title"])
                item["text"] = extracted["text"]
                item["pages"] = extracted["pages"]
                item["extraction_method"] = extracted["method"]
                item["is_readable"] = extracted["is_readable"]
            else:
                item["text"] = ""
                item["pages"] = 0
                item["extraction_method"] = "none"
                item["is_readable"] = False
            yield _emit(
                f"   '{item['title'][:45]}' -> {item.get('pages', 0)} pages, "
                f"{len(item.get('text', ''))} chars [{item.get('extraction_method', 'none')}]"
            )
            classified = self._classify_and_extract(item)
            per_doc.append(classified)

        yield _emit("[Step 4/5] Cross-verifying legal entities across documents...")
        cross = self._step5_cross_verify(per_doc)
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

        def _add(title, url, category_key, category_name, annexure, file_type, desc):
            if not url or not isinstance(url, str):
                return
            url = url.strip()
            if not url.startswith("http") or url.endswith("DOC_ID=") or url in seen_urls:
                return
            seen_urls.add(url)
            docs.append({
                "title": title, "url": url, "categoryKey": category_key,
                "categoryName": category_name, "annexure": annexure,
                "fileType": file_type, "desc": desc, "reg_no": reg_no,
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
                 "Official RERA registration certificate.")
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
        proj_details = raw.get("project_details") or {}
        survey_tables = proj_details.get("survey_land_tables") or raw.get("survey_land_tables") or []
        for st in survey_tables:
            if not isinstance(st, dict):
                continue
            for k, val in st.items():
                if k.endswith(" URL") or k.endswith("_url"):
                    clean_title = re.sub(r"\s*URL.*$", "", k).replace("_url", "").strip()
                    for u in _urls(val):
                        _add(clean_title, u, "legal",
                             "Legal Title Deeds & Sanctioned Plans",
                             "Land Titling", "Legal Document",
                             f"Survey land titling: {clean_title}.")

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

    def _step2_download(self, doc_list: List[dict]) -> List[dict]:
        results = []
        total = len(doc_list)
        for i, doc in enumerate(doc_list, 1):
            self._log(f"Downloading [{i}/{total}]: {doc['title'][:55]}...")
            result = self._download_single(doc)
            size_kb = round(len(result["bytes"]) / 1024, 1) if result["bytes"] else 0
            ok = "OK" if result["bytes"] else f"FAIL: {result.get('error', 'unknown')[:50]}"
            self._log(f"  -> {ok} ({size_kb} KB)")
            results.append(result)
        return results

    def _download_single(self, doc: dict) -> dict:
        item = dict(doc)
        item["bytes"] = b""
        item["error"] = None
        url = doc.get("url", "")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": self.UA,
                "Accept": "application/pdf,application/octet-stream,*/*",
                "Referer": "https://rera.karnataka.gov.in/",
            })
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=self.DOWNLOAD_TIMEOUT) as resp:
                item["bytes"] = resp.read()
                item["http_status"] = resp.status
                item["content_type"] = resp.headers.get("Content-Type", "")
        except Exception as e:
            item["error"] = str(e)
        return item

    # ---- Step 3: Extract Text ----

    def _step3_extract_and_classify(self, downloaded: List[dict]) -> List[dict]:
        per_doc = []
        for item in downloaded:
            if item["bytes"]:
                extracted = self._extract_text(item["bytes"], item["title"])
                item["text"] = extracted["text"]
                item["pages"] = extracted["pages"]
                item["extraction_method"] = extracted["method"]
                item["is_readable"] = extracted["is_readable"]
            else:
                item["text"] = ""
                item["pages"] = 0
                item["extraction_method"] = "none"
                item["is_readable"] = False
            per_doc.append(self._classify_and_extract(item))
        return per_doc

    def _extract_text(self, pdf_bytes: bytes, title: str = "") -> Dict[str, Any]:
        result = {"text": "", "pages": 0, "method": "none", "is_readable": False}
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
        if HAS_PYMUPDF:
            try:
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                result["pages"] = doc.page_count
                texts = [page.get_text("text") for page in doc]
                doc.close()
                result["text"] = "\n".join(t for t in texts if t)
                result["method"] = "PyMuPDF"
                result["is_readable"] = bool(result["text"].strip())
                return result
            except Exception:
                result["method"] = "PyMuPDF_err"
        if not HAS_PDFPLUMBER and not HAS_PYMUPDF:
            result["method"] = "no_pdf_library_installed"
        return result

    # ---- Step 4: Classify & Extract Entities ----

    def _classify_and_extract(self, item: dict) -> dict:
        title_lower = (item.get("title", "") or "").lower()
        text = (item.get("text", "") or "")
        text_lower = text.lower()
        combined = title_lower + " " + text_lower

        doc_type = DOCTYPE_OTHER
        if any(k in combined for k in ["registration certificate", "rera cert", "certificate of registration"]):
            doc_type = DOCTYPE_RERA_CERT
        elif any(k in combined for k in ["encumbrance certificate", "form 15", "form 16", "nil encumbrance", "kaveri"]):
            doc_type = DOCTYPE_EC
        elif any(k in combined for k in ["sale deed", "title deed", "absolute sale", "registered deed"]):
            doc_type = DOCTYPE_SALE_DEED
        elif any(k in combined for k in ["pahani", "rtc", "rights tenancy", "bhoomi", "khatedar"]):
            doc_type = DOCTYPE_RTC
        elif any(k in combined for k in ["khata", "e-khata", "a-khata", "b-khata", "pid", "e-swathu"]):
            doc_type = DOCTYPE_KHATA
        elif any(k in combined for k in ["icai", "chartered accountant", "form 1", "annexure-53"]):
            doc_type = DOCTYPE_CA_FORM1
        elif any(k in combined for k in ["council of architecture", "coa", "architect", "form 2", "annexure-57"]):
            doc_type = DOCTYPE_ARCH_FORM2
        elif any(k in combined for k in ["structural engineer", "form 3", "annexure-55", "engineer licence"]):
            doc_type = DOCTYPE_ENG_FORM3
        elif any(k in combined for k in ["joint development", "jda", "development agreement"]):
            doc_type = DOCTYPE_JDA
        elif any(k in combined for k in ["mutation", "mr extract", "mutation register"]):
            doc_type = DOCTYPE_MUTATION
        elif any(k in combined for k in ["conversion order", "dc order", "alienation", "sec 95", "section 95 klr"]):
            doc_type = DOCTYPE_CONVERSION
        elif any(k in combined for k in ["sanction plan", "building plan", "layout plan", "approved plan"]):
            doc_type = DOCTYPE_PLAN

        item["doc_type"] = doc_type
        item["doc_type_label"] = CATEGORY_LABELS.get(doc_type, "Other")
        entities = {}
        issues = []

        if not item.get("bytes"):
            entities["download_status"] = "FAILED"
            issues.append("PDF could not be downloaded from RERA server.")
            item["entities"] = entities
            item["issues"] = issues
            item["status"] = "DOWNLOAD_FAILED"
            item["status_badge"] = "DOWNLOAD FAILED"
            return item

        if not item.get("is_readable", False) or len(text.strip()) < 30:
            entities["text_status"] = "SCANNED_PDF"
            entities["pages"] = item.get("pages", 0)
            issues.append("PDF is a scanned image -- OCR required for deeper analysis.")
            item["entities"] = entities
            item["issues"] = issues
            item["status"] = "SCANNED_PDF"
            item["status_badge"] = "SCANNED -- OCR NEEDED"
            return item

        entities["pages"] = item.get("pages", 0)
        entities["char_count"] = len(text)

        extractor_map = {
            DOCTYPE_RERA_CERT:  self._extract_rera_cert,
            DOCTYPE_SALE_DEED:  self._extract_sale_deed,
            DOCTYPE_EC:         self._extract_ec,
            DOCTYPE_RTC:        self._extract_rtc,
            DOCTYPE_KHATA:      self._extract_khata,
            DOCTYPE_CA_FORM1:   self._extract_ca_form1,
            DOCTYPE_ARCH_FORM2: self._extract_arch_form2,
            DOCTYPE_ENG_FORM3:  self._extract_eng_form3,
            DOCTYPE_CONVERSION: self._extract_conversion_order,
            DOCTYPE_PLAN:       self._extract_sanction_plan,
        }
        extractor = extractor_map.get(doc_type, self._extract_generic)
        entities.update(extractor(text))

        if doc_type == DOCTYPE_EC and entities.get("has_active_mortgage") and not entities.get("discharge_deed_found"):
            issues.append("Active mortgage found in EC -- no Discharge Deed detected.")
        if doc_type == DOCTYPE_RTC:
            if entities.get("ptcl_flag"):
                issues.append("PTCL/SC-ST Grant Land restriction -- prior Govt approval required.")
            if entities.get("kharab_flag"):
                issues.append("B-Kharab (Govt Reserve Land) detected.")
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

    def _extract_rera_cert(self, text: str) -> dict:
        e = {}
        e["registration_no"] = (self._find(r"(PRM/KA/RERA/\d{4}/\d{4}/[A-Z0-9]+)", text) or
                                 self._find(r"Registration\s*No[.:]?\s*([A-Z0-9/\-]+)", text))
        e["project_name"] = self._find(r"Project\s*Name[:\s]+([^\n]{5,80})", text)
        e["promoter_name"] = self._find(r"(?:Promoter|Developer|Firm)[:\s]+([^\n]{5,80})", text)
        e["validity_date"] = self._find(r"valid(?:ity)?\s*(?:upto|till|until|up to)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        e["issue_date"] = self._find(r"(?:issued?|granted?)\s*(?:on)?[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        return {k: v for k, v in e.items() if v is not None}

    def _extract_sale_deed(self, text: str) -> dict:
        e = {}
        e["vendor"] = self._find(r"(?:vendor|seller|transferor|executant)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["purchaser"] = self._find(r"(?:purchaser|buyer|transferee)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["sro"] = self._find(r"(?:sub.?registrar|sro)[^\n]{0,20}(?:office)?[:\s]+([A-Za-z\s]{3,40})", text)
        e["execution_date"] = self._find(r"(?:executed?|registered?)\s+(?:on|this|dated)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        e["consideration"] = self._find(r"(?:consideration|sale\s+price)[:\s]+(?:Rs\.?\s*)?(\d[\d,]+(?:\s*(?:lakhs?|lacs?|crores?))?)", text)
        e["survey_no"] = self._find(r"(?:survey\s+no|sy\.?\s*no\.?)[.:\s]+([0-9/A-Za-z\s,]+?)(?:\n|,|measuring|extent)", text)
        e["extent"] = self._find(r"(?:extent|area|measuring)[:\s]+([0-9.]+\s*(?:acres?|guntas?|sq\.?\s*(?:ft|m)))", text)
        e["doc_no"] = self._find(r"(?:document\s+no|doc\.?\s+no\.?)[:\s]+([0-9/\-]+)", text)
        return {k: v for k, v in e.items() if v is not None}

    def _extract_ec(self, text: str) -> dict:
        e = {}
        text_lower = text.lower()
        e["period"] = self._find(r"(?:from|period)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+to\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        e["property_owner"] = self._find(r"(?:owner|khatedar|party)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["survey_no"] = self._find(r"(?:survey\s+no|sy\.?\s*no\.?)[.:\s]+([0-9/A-Za-z\s,]+?)(?:\n|,)", text)
        e["nil_encumbrance"] = bool(re.search(r"\bnil\b|\bno\s+encumbrance\b|\bno\s+transaction\b", text_lower))
        has_mortgage = bool(re.search(r"\bmortgage\b|\bsimple\s+mortgage\b|\bhypothecation\b", text_lower))
        e["has_active_mortgage"] = has_mortgage
        if has_mortgage:
            e["mortgage_bank"] = self._find(r"(HDFC|ICICI|SBI|Axis|Canara|Bank\s+of\s+[A-Z]\w+|[A-Z]\w+\s+Bank)", text)
            e["mortgage_amount"] = self._find(r"(?:Rs\.?\s*)([\d,]+\s*(?:lakhs?|crores?)?)\s*(?:loan|mortgage)", text)
            e["discharge_deed_found"] = bool(re.search(r"\bdischarge\s+deed\b|\bform\s*16\b|\bnoc\b", text_lower))
        return {k: v for k, v in e.items() if v is not None}

    def _extract_rtc(self, text: str) -> dict:
        e = {}
        text_lower = text.lower()
        e["survey_no"] = self._find(r"(?:sy\.?\s*no\.?|survey\s+no)[:\s]+([0-9/A-Za-z]+)", text)
        e["khatedar_col9"] = self._find(r"(?:col\.?\s*9|column\s*9|khatedar|hissedhar)[:\s]+([A-Z][^,\n]{3,60})", text)
        e["extent"] = self._find(r"(?:extent|area|acres?)[:\s]+([0-9.]+\s*(?:acres?|guntas?))", text)
        e["ptcl_flag"] = bool(re.search(r"\bptcl\b|\bsc/st\b|\bscheduled\s+caste\b|\bsaguvali\b|\bgrant\s+land\b", text_lower))
        e["kharab_flag"] = bool(re.search(r"\bb.kharab\b|\bkharab\b|\bpd\s+kharab\b", text_lower))
        e["mutation_status"] = self._find(r"(?:mutation|mr\s+no)[:\s]+([A-Z0-9\-/\s]{2,30})", text)
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

    def _step5_cross_verify(self, per_doc: List[dict]) -> dict:
        passed = []
        warnings = []
        critical_flags = []

        def _get(doc_type: str, field: str):
            for d in per_doc:
                if d.get("doc_type") == doc_type:
                    val = (d.get("entities") or {}).get(field)
                    if val is not None:
                        return val
            return None

        ec_nil = _get(DOCTYPE_EC, "nil_encumbrance")
        ec_has_mortgage = _get(DOCTYPE_EC, "has_active_mortgage")
        ec_discharge = _get(DOCTYPE_EC, "discharge_deed_found")
        rtc_ptcl = _get(DOCTYPE_RTC, "ptcl_flag")
        khata_type = _get(DOCTYPE_KHATA, "khata_type")
        rera_reg = _get(DOCTYPE_RERA_CERT, "registration_no")

        entity_map = {
            "seller_from_deed": _get(DOCTYPE_SALE_DEED, "vendor"),
            "ec_owner": _get(DOCTYPE_EC, "property_owner"),
            "rtc_owner_col9": _get(DOCTYPE_RTC, "khatedar_col9"),
            "deed_survey_no": _get(DOCTYPE_SALE_DEED, "survey_no"),
            "ec_survey_no": _get(DOCTYPE_EC, "survey_no"),
            "rtc_survey_no": _get(DOCTYPE_RTC, "survey_no"),
            "deed_extent": _get(DOCTYPE_SALE_DEED, "extent"),
            "rtc_extent": _get(DOCTYPE_RTC, "extent"),
            "khata_type": khata_type,
            "rera_registration_no": rera_reg,
        }

        # Checks
        if any(d.get("doc_type") == DOCTYPE_RERA_CERT and d.get("bytes") for d in per_doc):
            passed.append("RERA Registration Certificate present and downloaded.")
        else:
            warnings.append("RERA Registration Certificate not found or not downloadable.")

        if any(d.get("doc_type") == DOCTYPE_EC and d.get("bytes") for d in per_doc):
            passed.append("Encumbrance Certificate (EC) present.")
        else:
            warnings.append("Encumbrance Certificate not found.")

        if any(d.get("doc_type") == DOCTYPE_RTC and d.get("bytes") for d in per_doc):
            passed.append("Bhoomi RTC / Pahani present.")
        else:
            warnings.append("Bhoomi RTC / Pahani not found.")

        if rtc_ptcl:
            critical_flags.append(
                "PTCL/SC-ST Govt Grant Restriction in RTC -- Transfer is VOID without AC permission. "
                "(Karnataka PTCL Act 1978, Sec 4 & 5)"
            )
        else:
            passed.append("No PTCL/SC-ST Grant Land restriction detected.")

        if ec_has_mortgage:
            if ec_discharge:
                passed.append("Active mortgage in EC -- Discharge Deed present, title appears clear.")
            else:
                critical_flags.append(
                    "Active bank mortgage in EC with NO Discharge Deed -- "
                    "Demand Bank NOC + registered Discharge Deed (Form 16) before payment."
                )
        elif ec_nil:
            passed.append("EC shows NIL encumbrance -- clean title chain.")

        if any(d.get("doc_type") == DOCTYPE_CA_FORM1 for d in per_doc):
            passed.append("CA Form 1 (Financial Audit) present.")
        else:
            warnings.append("CA Form 1 not found -- Escrow account audit unverified.")

        if any(d.get("doc_type") == DOCTYPE_ARCH_FORM2 for d in per_doc):
            passed.append("Architect Form 2 (COA) present.")
        else:
            warnings.append("Architect Form 2 not found -- Layout/FAR compliance unverified.")

        if any(d.get("doc_type") == DOCTYPE_ENG_FORM3 for d in per_doc):
            passed.append("Engineer Form 3 (Structural Certificate) present.")
        else:
            warnings.append("Engineer Form 3 not found -- Structural cert unverified.")

        if any(d.get("doc_type") == DOCTYPE_CONVERSION for d in per_doc):
            passed.append("DC Conversion Order present.")

        if khata_type == "A-Khata":
            passed.append("A-Khata certificate present -- property is bank loan eligible.")
        elif khata_type == "B-Khata":
            critical_flags.append(
                "B-Khata detected -- property in unauthorized layout; bank financing severely restricted."
            )

        if any(d.get("doc_type") == DOCTYPE_PLAN for d in per_doc):
            passed.append("Sanctioned Building Plan present.")
        else:
            warnings.append("Sanctioned Building Plan not found.")

        scanned = [d["title"] for d in per_doc if d.get("status") == "SCANNED_PDF"]
        if scanned:
            warnings.append(f"{len(scanned)} PDF(s) are scanned images -- OCR required: " + "; ".join(scanned[:3]))

        failed_dl = [d["title"] for d in per_doc if d.get("status") == "DOWNLOAD_FAILED"]
        if failed_dl:
            warnings.append(f"{len(failed_dl)} document(s) failed to download: " + "; ".join(failed_dl[:3]))

        return {
            "passed": passed,
            "warnings": warnings,
            "critical_flags": critical_flags,
            "entity_map": entity_map,
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
        n_critical = len(cross["critical_flags"])
        n_warn = len(cross["warnings"])
        if n_critical > 0:
            verdict = "FAIL -- CRITICAL LEGAL RISKS IDENTIFIED"
            verdict_badge = "DO NOT BUY"
            risk_score = max(0, 40 - (n_critical * 15))
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
                "total_documents": len(per_doc), "verified": verified,
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
            "legal_risk_score": 0, "final_verdict": "INCOMPLETE -- " + reason,
            "verdict_badge": "INCOMPLETE", "agent_logs": self._logs,
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
    print(f"SCORE   : {report['legal_risk_score']}/100")
    if report["cross_verification"]["critical_flags"]:
        print("\nCRITICAL FLAGS:")
        for f in report["cross_verification"]["critical_flags"]:
            print(f"  * {f}")
    if report["cross_verification"]["warnings"]:
        print("\nWARNINGS:")
        for w in report["cross_verification"]["warnings"]:
            print(f"  * {w}")
