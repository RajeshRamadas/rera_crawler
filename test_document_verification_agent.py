"""Scenario tests for DocumentDownloadAndVerifyAgent.

Needs the 5 sample projects from rera_data/project_details.jsonl loaded into the database
(DATABASE_URL), e.g.:
    python3 -c "import json,rera_crawler as r; s=r.PostgresStorage('<db uri>', force_init=True); [s.upsert_record(json.loads(l)) for l in open('rera_data/project_details.jsonl')]"
    python3 setup_api_columns.py
Run:  python3 test_document_verification_agent.py

Runs the agent on a real crawled project (AARADHYA GARDENS, surveys 113/7A and 113/7B) with the
downloads replaced by generated PDFs, so every check after the download step is exercised."""
import sys, io, json, time
import os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont
import document_verification_agent as M
from document_verification_agent import DocumentDownloadAndVerifyAgent as A

FAILS = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def pdf(text):
    d = fitz.open(); p = d.new_page()
    p.insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=10)
    return d.tobytes()


def scanned_pdf(text):
    im = Image.new("RGB", (1700, 900), "white")
    f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    ImageDraw.Draw(im).multiline_text((60, 60), text, fill="black", font=f, spacing=14)
    b = io.BytesIO(); im.save(b, "PNG")
    d = fitz.open(); p = d.new_page(width=850, height=450)
    p.insert_image(p.rect, stream=b.getvalue())
    return d.tobytes()


RERA = """ACK/KA/RERA/1251/310/PR/180826/010524
PRM/KA/RERA/1251/310/PR/030926/008907
AARADHYA GARDENS, SURVEY NO 113/7A, BENGALURU SOUTH
SAI SAMRUDHI CONSTRUCTIONS PVT LTD
03-09-2026
31-12-2030"""


def docs_for(sv, v):
    """v: dict of overrides for the scenario"""
    owner = v.get("rtc_owner", "Ramesh Gowda")
    ec = (f"ENCUMBRANCE CERTIFICATE\nECC-2026-0045\nPeriod: 01/04/2010 to 31/08/2026\n"
          f"Survey No {v.get('ec_survey', sv)}\nOwner: Ramesh Gowda\n" + v.get("ec_body", "No encumbrance found for the said period.\n"))
    rtc = (f"RTC / PAHANI  Sy. No: {sv}\nKhatedar: {owner}\nExtent: {v.get('rtc_extent', '2.10 acres')}\n"
           f"IMR2019000123 12/03/2019\n" + v.get("rtc_extra", ""))
    deed = (f"SALE DEED\nVendor: Sri Ramesh Gowda\nPurchaser: Sai Samrudhi Constructions\nSurvey No {sv}, measuring\n"
            "Extent: 2.10 acres\nRegistered on: 10/06/2022\nDocument No: 1234/2022\n")
    khata = "KHATA CERTIFICATE BBMP\nKhata No: 456/113/7A\nOwner: Sai Samrudhi Constructions\n" + v.get("khata", "A-Khata\n")
    conv = f"CONVERSION ORDER\nDC Order No: ALN/SR/45/2020\nSy. No: {sv}\nExtent: 2.10 acres\nDated: 01/02/2020\nConversion fee paid\n"
    return {M.DOCTYPE_EC: ec, M.DOCTYPE_RTC: v.get("rtc_text", rtc), M.DOCTYPE_MUTATION: rtc,
            M.DOCTYPE_SALE_DEED: deed, M.DOCTYPE_KHATA: khata, M.DOCTYPE_CONVERSION: conv}


CA = "FORM 1 Chartered Accountant: K. Rao\nICAI Membership No: 123456\nFinancial Year: 2025-26\nEscrow account balance: Rs 12,40,000\n"
ARCH = "FORM 2 Architect: P. Menon\nCOA: CA/2010/12345\nFAR: 2.25\nPlot Area: 9,200 sq ft\n"
ENG = "FORM 3 Structural Engineer: S. Iyer\nLicence No: BBMP/SE/778\nCertified: 10/08/2026\n"
FIRM_REG = "CERTIFICATE OF REGISTRATION OF FIRM\nRegistrar of Firms, Karnataka\nRegistration Certificate No 12345\nSai Samrudhi Constructions partnership firm registered under the Indian Partnership Act."


def make_agent(v, fail_all=False, scanned_rera=False):
    a = A()

    def fake(doc):
        item = dict(doc, error=None, http_status=200, content_type="application/pdf")
        if fail_all:
            item.update(bytes=b"", error="connection refused")
            return item
        hint, sv, t = doc.get("doc_type_hint"), doc.get("survey_no"), doc.get("title", "")
        per_sv = v.get("per_survey", {}).get(sv, {})
        if hint == M.DOCTYPE_RERA_CERT:
            body = scanned_pdf(RERA) if scanned_rera else pdf(RERA)
        elif sv and hint in (M.DOCTYPE_EC, M.DOCTYPE_RTC, M.DOCTYPE_MUTATION, M.DOCTYPE_SALE_DEED,
                             M.DOCTYPE_KHATA, M.DOCTYPE_CONVERSION):
            body = pdf(docs_for(sv, {**v, **per_sv})[hint])
        elif "Annexure-53" in t: body = pdf(CA)
        elif "Annexure-57" in t: body = pdf(ARCH)
        elif "Annexure-55" in t: body = pdf(ENG)
        elif "Annexure - 1 )" in t: body = pdf(FIRM_REG)
        else: body = pdf("Supporting promoter document " + t + " filed with the authority for records.")
        item["bytes"] = body
        return item

    a._download_single = fake
    return a


def run(name, v=None, **kw):
    a = make_agent(v or {}, **kw)
    t0 = time.time()
    r = a.run("AARADHYA GARDENS")
    print(f"\n===== {name} ({time.time()-t0:.1f}s) =====")
    print("verdict:", r["verdict_badge"], "|", r["final_verdict"], "| score", r["legal_risk_score"])
    cv = r["cross_verification"]
    for k in ("critical_flags", "warnings", "passed"):
        print(f" {k}:"); [print("     -", x[:160]) for x in cv[k]]
    return r, cv


def has(lst, s):
    return any(s.lower() in x.lower() for x in lst)


# 1. Clean project
r, cv = run("CLEAN")
check(r["verdict_badge"] in ("CLEAR TO PROCEED", "REVIEW REQUIRED"), "clean project passes")
check(not cv["critical_flags"], "clean: no critical flags")
check(has(cv["passed"], "Owner names consistent"), "clean: owner chain cross-checked")
check(has(cv["passed"], "matches the RERA project record"), "clean: RERA number matched to DB")
check(r["entity_map"]["rera_validity_date"] == "31-12-2030", "RERA validity date = 31-12-2030 (was issue date)")
firm = [d for d in r["per_document_results"] if "Annexure - 1 )" in d["title"]][0]
check(firm["doc_type"] == "OTHER", "firm registration (Annexure-1) not mistaken for RERA cert")
types = {d["doc_type"] for d in r["per_document_results"]}
check({"RTC_PAHANI", "MUTATION_EXTRACT", "ENCUMBRANCE_CERT", "SALE_DEED", "KHATA", "CONVERSION_ORDER"} <= types,
      "land docs classified by annexure")

# 2. Owner / survey / extent mismatch, PTCL, mortgage, B-khata
r, cv = run("RISKY", {"per_survey": {"113/7B": {"rtc_owner": "Lakshmamma", "ec_survey": "45/2",
                                               "rtc_extent": "1.20 acres"}},
                      "rtc_extra": "Saguvali chit - PTCL grant land\n",
                      "ec_body": "Simple Mortgage in favour of Canara Bank Rs 2,50,00,000 loan dated 12/05/2021\n",
                      "khata": "B-Khata\n"})
check(r["verdict_badge"] == "DO NOT BUY", "risky: DO NOT BUY")
check(has(cv["critical_flags"], "Owner mismatch on Sy. No 113/7B"), "owner mismatch flagged (bug 1)")
check(has(cv["critical_flags"], "Survey number mismatch"), "survey number mismatch flagged")
check(has(cv["warnings"], "Extent mismatch on Sy. No 113/7B"), "extent mismatch flagged")
check(has(cv["critical_flags"], "PTCL"), "PTCL flagged")
check(has(cv["critical_flags"], "Active bank mortgage"), "active mortgage flagged")
check(has(cv["critical_flags"], "B-Khata"), "B-Khata flagged")

# 3. Released mortgage + bare 'NOC' word
r, cv = run("RELEASED MORTGAGE", {"ec_body": "Simple Mortgage in favour of Canara Bank dated 12/05/2021\n"
                                            "Release Deed registered 03/03/2024, mortgage released.\n"})
check(not has(cv["critical_flags"], "mortgage"), "released mortgage is not flagged (bug 8)")
check(has(cv["passed"], "released/discharged"), "released mortgage reported as released")
r, cv = run("MORTGAGE + UNRELATED NOC", {"ec_body": "Simple Mortgage in favour of Canara Bank dated 12/05/2021\n"
                                                   "Fire NOC obtained for the building.\n"})
check(has(cv["critical_flags"], "Active bank mortgage"), "a fire NOC no longer counts as a discharge deed (bug 8)")

# 4. Nothing downloaded
r, cv = run("ALL DOWNLOADS FAIL", fail_all=True)
check(r["verdict_badge"] == "UNABLE TO VERIFY" and r["legal_risk_score"] is None, "no downloads -> UNABLE TO VERIFY (bug 3)")
check(not cv["passed"], f"no downloads -> nothing passed (bug 2) {cv['passed']}")

# 5. RTC that cannot really be read (e.g. Kannada OCR'd with English only)
garbage = "Ee ae ere ord dee cea orem eee aed ede Oe reese ered eee oee aed eed ee"
r, cv = run("UNREADABLE RTC", {"rtc_text": garbage + "\n" + garbage})
check(not has(cv["passed"], "No PTCL"), "unreadable RTC -> no false 'No PTCL' pass (bug 4)")
check(has(cv["warnings"], "No readable RTC for Sy. No(s): 113/7A, 113/7B"), "unreadable RTC -> coverage warning")
check(has(cv["warnings"], "mutation extracts only"), "unreadable RTC -> PTCL check marked as partial")
check(r["verdict_badge"] == "MANUAL REVIEW REQUIRED", "unreadable RTC -> manual review verdict")

# 6. Kannada RTC text (as produced by kan+eng OCR or a text-layer PDF)
kn_rtc = ("ಪಹಣಿ ಪತ್ರಿಕೆ Sy. No: 113/7A ಗ್ರಾಮ ಉತ್ತರಹಳ್ಳಿ ಹೋಬಳಿ ಕೆಂಗೇರಿ ತಾಲ್ಲೂಕು ಬೆಂಗಳೂರು ದಕ್ಷಿಣ\n"
          "Khatedar: Ramesh Gowda ದರಖಾಸ್ತು ಮಂಜೂರು ಜಮೀನು\n")
it = A()._classify_and_extract({"title": "RTC (Annexure-43) -- Survey No 113/7A", "doc_type_hint": "RTC_PAHANI",
                                 "bytes": b"x", "text": kn_rtc, "is_readable": True, "pages": 1,
                                 "extraction_method": "pdfplumber"})
check(it["entities"].get("ptcl_flag") and it["status"] == "CRITICAL", "Kannada grant-land term flagged (bug 4)")
check(it["entities"].get("language") == "Kannada", "Kannada RTC detected as Kannada")
kn_ec = "ಋಣಭಾರ ಪ್ರಮಾಣ ಪತ್ರ Survey No 113/7A Owner: Ramesh Gowda ಅಡಮಾನ ಕೆನರಾ ಬ್ಯಾಂಕ್ " * 2
it = A()._classify_and_extract({"title": "Encumbrance Certificate (Annexure-46) -- Survey No 113/7A",
                                 "doc_type_hint": "ENCUMBRANCE_CERT", "bytes": b"x", "text": kn_ec,
                                 "is_readable": True, "pages": 1, "extraction_method": "pdfplumber"})
check(it["entities"].get("has_active_mortgage"), "Kannada mortgage term (ಅಡಮಾನ) flagged")

# 7. Scanned RERA certificate (OCR)
r, cv = run("SCANNED RERA CERT", scanned_rera=True)
check(r["entity_map"]["rera_registration_no"] == "PRM/KA/RERA/1251/310/PR/030926/008907", "OCR read RERA number")
check(r["entity_map"]["rera_validity_date"] == "31-12-2030", "OCR validity date correct (bug 5)")

# 8. Upload path
open(os.path.join(tempfile.gettempdir(), "up.pdf"), "wb").write(pdf(docs_for("113/7A", {})[M.DOCTYPE_EC]))
res = A().verify_local_document(os.path.join(tempfile.gettempdir(), "up.pdf"), "EC")
check(res["doc_type"] == "ENCUMBRANCE_CERT" and res["status"] == "VERIFIED" and "bytes" not in res,
      f"verify_local_document works (bug 9) -> {res['doc_type']} {res['status']}")
json.dumps(res)  # must be JSON serialisable for the DB

print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
