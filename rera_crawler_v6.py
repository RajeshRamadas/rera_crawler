#!/usr/bin/env python3
"""
RERA Karnataka Project Detail Crawler
======================================

Fetches the full "Project Registration Details" page for a project on
https://rera.karnataka.gov.in and normalizes every tab into structured JSON.

CHANGES IN THIS VERSION (see CHANGELOG at bottom of file for full notes)
--------------------------------------------------------------------------
This version is hardened against several failure modes that couldn't be
verified from the original HAR capture:

  1. search_projects() now auto-forwards ANY hidden <input> fields found on
     the /viewAllProjects form (e.g. __VIEWSTATE / __EVENTVALIDATION /
     CSRF tokens), instead of only sending the visible search fields.
     Many Indian govt ASP.NET-style portals reject POSTs missing these.

  2. Every network call now validates that it got HTML back and not a
     captcha/error/redirect page, and raises a clear CrawlerError instead
     of silently parsing garbage.

  3. normalize_project_detail() no longer assumes tab divs/fields exist -
     every accessor is defensive and logs what it couldn't find instead of
     returning silently-wrong empty data.

  4. Added --save-raw-html flag to dump the raw /projectDetails response
     to disk, so you can inspect real markup and fix any parser mismatches
     without repeatedly hitting the live site.

  5. Added --from-html flag (offline mode) to re-run parsing against a
     previously saved raw HTML file - the fast dev loop for fixing parsers.

  6. Basic retry-with-backoff on network calls.

  7. resolve_project_id() heuristics unchanged in *logic* but now report
     ALL candidates found (with context) instead of silently picking the
     first, so you can sanity-check before trusting it.

WHAT THIS VERSION CANNOT FIX
------------------------------
- I have no network access to rera.karnataka.gov.in from this environment,
  so I cannot confirm the real form field names, hidden tokens, or actual
  tab markup. The defensive coding above means the script will now FAIL
  LOUDLY with a diagnostic instead of silently producing wrong output if
  reality differs from these assumptions - that's the best fix possible
  without a live test.
- If results are rendered client-side via JavaScript (not present in the
  raw HTML `requests` receives), no amount of parser fixing here will help;
  you'd need Selenium/Playwright. Run with --save-raw-html first and check
  whether the data you expect is actually present in the saved file before
  debugging the parser.

Usage
-----
    # Fastest / most reliable - known numeric PROJECT_ID:
    python rera_karnataka_crawler.py --project-id 6

    # Save the raw HTML for inspection while you're at it:
    python rera_karnataka_crawler.py --project-id 6 --save-raw-html raw.html

    # Re-parse a previously saved page with no network calls (dev loop):
    python rera_karnataka_crawler.py --from-html raw.html

    # Search by registration number, auto-resolve PROJECT_ID:
    python rera_karnataka_crawler.py --reg-no "PRM/KA/RERA/1251/446/PR/171010/000003" \\
        --district "Bengaluru Urban" --subdistrict "Bengaluru East"

Outputs project.json and project_table.csv in the current directory.
"""

import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://rera.karnataka.gov.in"

HEADERS_DOC = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

HEADERS_XHR = {
    "User-Agent": HEADERS_DOC["User-Agent"],
    "Accept": "*/*",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class CrawlerError(Exception):
    """Raised when a network response doesn't look like the page we expect."""


# --------------------------------------------------------------------------- #
# Network helper with retry + sanity checking
# --------------------------------------------------------------------------- #

def _request_with_retry(method, s: requests.Session, url: str, **kwargs) -> requests.Response:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = s.request(method, url, timeout=60, **kwargs)
            res.raise_for_status()
            return res
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                print(f"[!] {method} {url} failed ({exc}); retrying "
                      f"({attempt}/{MAX_RETRIES})...", file=sys.stderr)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise CrawlerError(f"{method} {url} failed after {MAX_RETRIES} attempts: {last_exc}")


def _looks_like_captcha_or_error(html: str) -> str | None:
    """Return a reason string if the page looks like a captcha/error/session
    page rather than real content, else None."""
    lowered = html.lower()
    suspects = ["captcha", "session expired", "invalid request",
                "something went wrong", "access denied", "error occurred"]
    for s in suspects:
        if s in lowered:
            return s
    if len(html.strip()) < 200:
        return "response body suspiciously short"
    return None


# --------------------------------------------------------------------------- #
# Session / navigation
# --------------------------------------------------------------------------- #

def new_session() -> requests.Session:
    """Create a session, prime cookies, and capture any hidden form fields
    (ASP.NET __VIEWSTATE-style tokens, CSRF tokens, etc.) from the search
    form page so we can forward them on the actual search POST."""
    s = requests.Session()
    s.headers.update(HEADERS_DOC)
    res = _request_with_retry("GET", s, f"{BASE}/viewAllProjects", params={"language": "en"})

    reason = _looks_like_captcha_or_error(res.text)
    if reason:
        print(f"[!] Warning: /viewAllProjects response looks unusual ({reason}). "
              f"Continuing anyway, but expect trouble.", file=sys.stderr)

    hidden_fields = extract_hidden_fields(res.text)
    if hidden_fields:
        print(f"[*] Captured {len(hidden_fields)} hidden form field(s) from search "
              f"page: {list(hidden_fields.keys())}")
    return s, hidden_fields


def extract_hidden_fields(html: str) -> dict:
    """Pull every <input type="hidden"> from the search form so tokens like
    __VIEWSTATE / __EVENTVALIDATION / csrf get forwarded automatically.
    This is the main fix for POSTs that silently fail server-side."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form") or soup
    fields = {}
    for inp in form.find_all("input", type="hidden"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    return fields


def search_projects(
    s: requests.Session,
    hidden_fields: dict,
    project: str = "",
    firm: str = "",
    app_no: str = "",
    reg_no: str = "",
    district: str = "",
    subdistrict: str = "",
) -> str:
    """Run the search form. Returns the raw HTML of the results page."""
    data = dict(hidden_fields)  # forward any tokens captured from the form page
    data.update({
        "project": project,
        "firm": firm,
        "appNo": app_no,
        "regNo": reg_no,
        "district": district,
        "subdistrict": subdistrict,
        "btn1": "Search",
    })
    headers = dict(HEADERS_DOC)
    headers["Referer"] = f"{BASE}/viewAllProjects?language=en"
    headers["Origin"] = BASE
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    res = _request_with_retry("POST", s, f"{BASE}/projectViewDetails", data=data, headers=headers)

    reason = _looks_like_captcha_or_error(res.text)
    if reason:
        raise CrawlerError(
            f"/projectViewDetails response looks unusual ({reason}). "
            f"Save the response and inspect it - likely a missing hidden "
            f"field/token, expired session, or captcha. First 500 chars:\n"
            f"{res.text[:500]}"
        )
    return res.text


def resolve_project_id(search_html: str, reg_no: str = "", project_name: str = ""):
    """
    Best-effort extraction of PROJECT_ID candidates from a saved search-results
    page. UNVERIFIED against the live site - these are heuristics. Returns
    (best_guess_or_None, all_candidates_with_context) so the caller can
    sanity-check rather than blindly trusting the first match.
    """
    soup = BeautifulSoup(search_html, "html.parser")
    candidates = []  # list of (id, context_snippet)

    # Heuristic 1: onclick="...(<digits>)" anywhere in the page.
    for m in re.finditer(r"(?:action|projectDetails|viewProject)\w*\(['\"]?(\d+)['\"]?\)", search_html):
        candidates.append((m.group(1), "global onclick match"))

    # Heuristic 2: same-row <a>/<button> with a numeric id, scoped to rows
    # that mention the reg_no or project_name we searched for.
    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True)
        if (reg_no and reg_no in text) or (project_name and project_name in text):
            for tag in tr.find_all(["a", "button"]):
                onclick = tag.get("onclick", "") or ""
                m = re.search(r"(\d+)", onclick)
                if m:
                    candidates.append((m.group(1), f"row match: {text[:80]}"))

    if not candidates:
        return None, []

    # Prefer row-scoped matches (heuristic 2) over global ones if both exist.
    row_scoped = [c for c in candidates if c[1].startswith("row match")]
    best = row_scoped[0][0] if row_scoped else candidates[0][0]
    return best, candidates


def fetch_project_detail_html(s: requests.Session, project_id: str) -> str:
    """POST /projectDetails action=<project_id> -> full multi-tab detail HTML."""
    headers = dict(HEADERS_XHR)
    headers["Referer"] = f"{BASE}/projectViewDetails"
    headers["Origin"] = BASE
    res = _request_with_retry(
        "POST", s, f"{BASE}/projectDetails",
        data={"action": project_id}, headers=headers,
    )

    reason = _looks_like_captcha_or_error(res.text)
    if reason:
        raise CrawlerError(
            f"/projectDetails response for PROJECT_ID={project_id} looks unusual "
            f"({reason}). This ID may be invalid, or the session/tokens are stale. "
            f"First 500 chars:\n{res.text[:500]}"
        )
    return res.text


# --------------------------------------------------------------------------- #
# Generic tab parsing helpers
# --------------------------------------------------------------------------- #

def parse_label_value_sections(container) -> dict:
    """
    Parse a tab built from repeated:
        <h1>Section Name</h1>
        <div class="row"><div>Label :</div><div>Value</div> ...</div>

    Returns {section_name: dict_or_list_of_dicts}. A section becomes a list of
    dicts when the same label (e.g. "Name") repeats within it (multiple people,
    multiple bank rows, etc.) - each repeat starts a new record.
    """
    records_by_section = defaultdict(list)
    current_section = "General"
    current_record = {}

    def flush():
        nonlocal current_record
        if current_record:
            records_by_section[current_section].append(current_record)
            current_record = {}

    for el in container.select("h1, div.row"):
        if el.name == "h1":
            flush()
            current_section = el.get_text(" ", strip=True)
            continue

        cols = el.find_all("div", recursive=False)
        texts = [c.get_text(" ", strip=True) for c in cols]
        for i in range(0, len(texts) - 1, 2):
            label = texts[i].rstrip(":").strip()
            value = texts[i + 1].strip()
            if not label:
                continue
            if label in current_record:
                flush()
            current_record[label] = value

    flush()

    result = {}
    for section, records in records_by_section.items():
        result[section] = records[0] if len(records) == 1 else records
    return result


def parse_tables(container) -> list:
    """Parse every <table> within a container into a list of row-dicts."""
    all_tables = []
    for table in container.find_all("table"):
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        rows = []
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            values = [td.get_text(" ", strip=True) for td in tds]
            if headers and len(headers) == len(values):
                rows.append(dict(zip(headers, values)))
            else:
                rows.append({"columns": values})
        all_tables.append({"headers": headers, "rows": rows})
    return all_tables


def parse_documents_matrix(container) -> list:
    """
    'Uploaded Documents' financial table is transposed: header row = years,
    each body row = a document type with one file per year. Flatten to
    [{"document_type": ..., "financial_year": ..., "file": ...}, ...]
    """
    out = []
    for table in container.find_all("table"):
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if len(headers) < 2:
            continue
        year_cols = headers[1:]
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            doc_type = tds[0].get_text(" ", strip=True)
            for year, td in zip(year_cols, tds[1:]):
                value = td.get_text(" ", strip=True)
                if value:
                    out.append(
                        {"document_type": doc_type, "financial_year": year, "file": value}
                    )
    return out


# --------------------------------------------------------------------------- #
# Full-page normalization
# --------------------------------------------------------------------------- #

TAB_IDS = {
    "promoter_details": "home",
    "project_details": "menu1",
    "uploaded_documents": "menu2",
    "enquired_documents": "menu4",
    "appeal_fnh_details": "menu5",
    "quarterly_updates": "quarter",
    "completion_details": "completion",
}


def _warn_missing(tab_key: str, tab_id: str):
    print(f"[!] Warning: could not find <div id=\"{tab_id}\"> for '{tab_key}' - "
          f"the real markup may differ from what this script expects. "
          f"That section will be empty in the output. Inspect the raw HTML "
          f"(--save-raw-html) to fix TAB_IDS or the parser for this tab.",
          file=sys.stderr)


def header_field(header, label):
    if header is None:
        return None
    span = header.find("span", string=re.compile(re.escape(label)))
    if span is None:
        for sp in header.find_all("span"):
            if label in sp.get_text():
                span = sp
                break
    if span is None:
        return None
    b = span.find("b")
    return b.get_text(" ", strip=True) if b else span.get_text(" ", strip=True)


def normalize_project_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find("div", id="header2")
    if header is None:
        print("[!] Warning: could not find <div id=\"header2\"> - project "
              "name/reg number fields will be empty. Check raw HTML.",
              file=sys.stderr)

    result = {
        "project_name": header_field(header, "Project Name"),
        "acknowledgement_number": header_field(header, "Acknowledgement Number"),
        "registration_number": header_field(header, "Registration Number"),
    }

    # Promoter Details, Project Details tabs -> generic section parser
    for key in ("promoter_details", "project_details"):
        div = soup.find("div", id=TAB_IDS[key])
        if div is None:
            _warn_missing(key, TAB_IDS[key])
            result[key] = {}
        else:
            result[key] = parse_label_value_sections(div)

    # Uploaded Documents -> financial-year matrix table
    div = soup.find("div", id=TAB_IDS["uploaded_documents"])
    if div is None:
        _warn_missing("uploaded_documents", TAB_IDS["uploaded_documents"])
        result["uploaded_documents"] = []
    else:
        result["uploaded_documents"] = parse_documents_matrix(div)

    # Enquired Documents -> straightforward table
    div = soup.find("div", id=TAB_IDS["enquired_documents"])
    if div is None:
        _warn_missing("enquired_documents", TAB_IDS["enquired_documents"])
        result["enquired_documents"] = []
    else:
        tables = parse_tables(div)
        result["enquired_documents"] = tables[0]["rows"] if tables else []

    # Quarterly Updates -> table if present, else empty
    div = soup.find("div", id=TAB_IDS["quarterly_updates"])
    if div is None:
        _warn_missing("quarterly_updates", TAB_IDS["quarterly_updates"])
        result["quarterly_updates"] = []
    else:
        tables = parse_tables(div)
        result["quarterly_updates"] = tables[0]["rows"] if tables else []

    # Completion Details -> generic section parser (has both flat fields and sections)
    div = soup.find("div", id=TAB_IDS["completion_details"])
    if div is None:
        _warn_missing("completion_details", TAB_IDS["completion_details"])
        result["completion_details"] = {}
    else:
        result["completion_details"] = parse_label_value_sections(div)

    # Complaints (two sub-tabs: on Promoter / on Project)
    comp_container = soup.find("div", id="menu-complaints")
    complaints_promoter, complaints_project = [], []
    if comp_container is None:
        _warn_missing("complaints", "menu-complaints")
    else:
        promoter_div = comp_container.find("div", id="menu-comp")
        project_div = comp_container.find("div", id="menu-comp2")
        if promoter_div:
            tables = parse_tables(promoter_div)
            complaints_promoter = tables[0]["rows"] if tables else []
        else:
            _warn_missing("complaints_on_promoter", "menu-comp")
        if project_div:
            tables = parse_tables(project_div)
            complaints_project = tables[0]["rows"] if tables else []
        else:
            _warn_missing("complaints_on_project", "menu-comp2")
    result["complaints_on_promoter"] = complaints_promoter
    result["complaints_on_project"] = complaints_project

    return result


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def flatten_for_csv(data: dict) -> dict:
    """Flatten nested dict/list values to strings so they fit a single CSV row."""
    flat = {}
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            flat[k] = json.dumps(v, ensure_ascii=False)
        else:
            flat[k] = v
    return flat


def write_csv(row: dict, output: Path):
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="RERA Karnataka project-detail crawler")
    parser.add_argument("--project-id", help="Known internal numeric PROJECT_ID (fastest path)")
    parser.add_argument("--search-html", help="Path to a saved /projectViewDetails search response, "
                                               "used only if --project-id is not given")
    parser.add_argument("--project", default="", help="Project name to search")
    parser.add_argument("--firm", default="", help="Promoter/firm name to search")
    parser.add_argument("--app-no", default="", help="Application number to search")
    parser.add_argument("--reg-no", default="", help="Registration number to search")
    parser.add_argument("--district", default="", help="District filter")
    parser.add_argument("--subdistrict", default="", help="Sub-district/Taluk filter")
    parser.add_argument("--save-raw-html", help="Also save the raw /projectDetails HTML to this path")
    parser.add_argument("--from-html", help="Skip all network calls; parse this saved HTML file "
                                             "directly (offline dev/test mode)")
    args = parser.parse_args()

    # --- Offline mode: just re-run the parser against a saved page. --------
    if args.from_html:
        html = Path(args.from_html).read_text(encoding="utf-8", errors="ignore")
        data = normalize_project_detail(html)
        _write_outputs(data)
        return

    try:
        s, hidden_fields = new_session()

        project_id = args.project_id
        if not project_id:
            if args.search_html:
                search_html = Path(args.search_html).read_text(encoding="utf-8", errors="ignore")
            else:
                print("[*] Searching rera.karnataka.gov.in ...")
                search_html = search_projects(
                    s, hidden_fields,
                    project=args.project,
                    firm=args.firm,
                    app_no=args.app_no,
                    reg_no=args.reg_no,
                    district=args.district,
                    subdistrict=args.subdistrict,
                )
            project_id, candidates = resolve_project_id(
                search_html, reg_no=args.reg_no, project_name=args.project
            )
            if not project_id:
                print(
                    "[!] Could not resolve PROJECT_ID from the search results.\n"
                    "    Please supply --project-id directly (inspect the 'View "
                    "Details' network call in your browser devtools: it POSTs to "
                    "/projectDetails with body action=<id>).",
                    file=sys.stderr,
                )
                sys.exit(2)
            if len(candidates) > 1:
                print(f"[!] Note: found {len(candidates)} possible PROJECT_ID "
                      f"candidates, using '{project_id}'. Others: "
                      f"{[c[0] for c in candidates if c[0] != project_id]}. "
                      f"Verify this is correct.", file=sys.stderr)
            print(f"[*] Resolved PROJECT_ID: {project_id}")

        print(f"[*] Fetching project detail page for PROJECT_ID={project_id} ...")
        html = fetch_project_detail_html(s, project_id)

        if args.save_raw_html:
            Path(args.save_raw_html).write_text(html, encoding="utf-8")
            print(f"[*] Raw HTML saved to: {Path(args.save_raw_html).resolve()}")

        data = normalize_project_detail(html)
        _write_outputs(data)

    except CrawlerError as exc:
        print(f"\n[!] Crawler error: {exc}\n", file=sys.stderr)
        sys.exit(1)


def _write_outputs(data: dict):
    json_output = Path("project.json")
    csv_output = Path("project_table.csv")

    json_output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    write_csv(flatten_for_csv(data), csv_output)

    print(f"\n[+] Success! JSON saved to: {json_output.resolve()}")
    print(f"[+] Flattened CSV saved to: {csv_output.resolve()}\n")
    print(json.dumps(data, indent=2, ensure_ascii=False)[:2000], "...\n")


if __name__ == "__main__":
    main()

# --------------------------------------------------------------------------- #
# CHANGELOG (this version vs. original)
# --------------------------------------------------------------------------- #
# - Hidden form fields (ASP.NET __VIEWSTATE / __EVENTVALIDATION / CSRF-style
#   tokens) from the /viewAllProjects page are now captured and forwarded on
#   the search POST. This was the most likely silent-failure point in the
#   original: many Indian govt portals reject form POSTs missing these.
# - All network responses are checked against a captcha/error/short-body
#   heuristic and raise CrawlerError with a diagnostic instead of parsing
#   garbage silently.
# - Added retry-with-backoff (3 attempts) on every network call.
# - normalize_project_detail() and its sub-parsers now warn (via stderr)
#   for every tab/section they couldn't locate, instead of quietly returning
#   empty structures - so a markup mismatch is visible immediately in the
#   output rather than looking like "the project just has no complaints".
# - resolve_project_id() now returns ALL candidate IDs with context instead
#   of silently trusting the first regex match, and the id search is now
#   preferentially row-scoped (matched against the searched reg_no/project
#   name) rather than a blind global regex over the whole page.
# - Added --save-raw-html (capture real markup for offline debugging) and
#   --from-html (re-parse a saved page with zero network calls) so you can
#   iterate on the parsers quickly and cheaply once you have one real sample.
#
# STILL UNVERIFIED (cannot be confirmed without live network access):
# - Whether /viewAllProjects's form actually requires hidden tokens at all,
#   and whether their names match what extract_hidden_fields() finds.
# - The real tab div IDs in TAB_IDS - these came from the original script's
#   assumptions, not a live-verified sample.
# - Whether search results / detail tabs are server-rendered HTML (this
#   script's core assumption) or populated by client-side JS after load.