#!/usr/bin/env python3
"""
RERA Karnataka Unified Project Detail Crawler (v9)
==================================================

Combines top-level project discovery (extracting summary info across all projects)
with deep detailed tab-level extraction (v9 parser with link & document extraction)
into a continuous, crash-safe, resumable crawl loop that saves to JSONL and CSV.

FEATURES
--------
1. TOP-LEVEL DISCOVERY:
   Scrapes all project summary info (Acknowledgement No, Registration No,
   Project Name, Firm Name) from the /viewAllProjects portal dataset.

2. DETAILED EXTRACTION (v9 PARSER):
   For each discovered property, resolves its internal PROJECT_ID, fetches
   the multi-tab details (/projectDetails), and normalizes:
     - Header info
     - Promoter Details
     - Project Details
     - Uploaded Documents (financial-year matrix with download URLs)
     - Enquired Documents (tables with download URLs)
     - Quarterly Updates & Completion Details (including embedded image/doc URLs)
     - Complaints (on Promoter and on Project)

3. CONTINUOUS & CRASH-SAFE LOOP:
   Appends each property's full details incrementally to project_details.jsonl.
   Periodically and automatically rebuilds project_table.csv (union of all fields).
   Tracks crawled reg_nos in done_regnos.json so stopping (Ctrl+C) and restarting
   resumes right where it left off.

4. SINGLE PROJECT & OFFLINE MODES:
   Can still crawl a single property by --project-id or --reg-no, or re-parse
   saved raw HTML via --from-html without network access.

USAGE EXAMPLES
--------------
  # Continuous / bulk loop over ALL discovered properties:
  python rera_crawler_v9.py --all

  # Continuous loop over first N properties with custom delay & output dir:
  python rera_crawler_v9.py --all --limit 20 --delay 1.5 --output-dir ./rera_data

  # Rebuild CSV from existing project_details.jsonl:
  python rera_crawler_v9.py --rebuild-csv-only

  # Single project by numeric PROJECT_ID:
  python rera_crawler_v9.py --project-id 6

  # Offline re-parsing of saved HTML:
  python rera_crawler_v9.py --from-html raw.html
"""

import argparse
import csv
import html as html_module
import json
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
REBUILD_EVERY = 25  # Rebuild CSV every N new records in continuous mode

# Regex matching the embedded JS project array pushes on /viewAllProjects
_DISCOVERY_PATTERN = re.compile(
    r"applicationNameList\s*\.push\('([^']*)'\);\s*"
    r"applicationNameList2\s*\.push\('([^']*)'\);\s*"
    r"applicationNameList3\s*\.push\('([^']*)'\);\s*"
    r"applicationNameList4\s*\.push\('([^']*)'\);",
    re.S,
)


class CrawlerError(Exception):
    """Raised when network, discovery, or parsing encounters an unrecoverable issue."""


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


def _looks_like_captcha_or_error(html_text: str) -> str | None:
    """Return a reason string if the response looks like a captcha/error page."""
    lowered = html_text.lower()
    suspects = ["captcha", "session expired", "invalid request",
                "something went wrong", "access denied", "error occurred"]
    for s in suspects:
        if s in lowered:
            return s
    if len(html_text.strip()) < 200:
        return "response body suspiciously short"
    return None


# --------------------------------------------------------------------------- #
# Session & Navigation
# --------------------------------------------------------------------------- #

def new_session() -> tuple[requests.Session, dict]:
    """Create a session and capture hidden input tokens from search page."""
    s = requests.Session()
    s.headers.update(HEADERS_DOC)
    res = _request_with_retry("GET", s, f"{BASE}/viewAllProjects", params={"language": "en"})

    reason = _looks_like_captcha_or_error(res.text)
    if reason:
        print(f"[!] Warning: /viewAllProjects response looks unusual ({reason}).", file=sys.stderr)

    hidden_fields = extract_hidden_fields(res.text)
    if hidden_fields:
        print(f"[*] Captured {len(hidden_fields)} hidden form field(s): {list(hidden_fields.keys())}")
    return s, hidden_fields


# Each worker thread gets its OWN requests.Session + hidden-field set, created
# lazily on first use. requests.Session is not guaranteed thread-safe, so
# sharing one session object across concurrent workers risks cookie/connection
# races; a session-per-thread avoids that while still reusing TCP connections
# within a thread across all the projects it handles.
_thread_local = threading.local()


def get_thread_session() -> tuple[requests.Session, dict]:
    if not hasattr(_thread_local, "session"):
        _thread_local.session, _thread_local.hidden_fields = new_session()
    return _thread_local.session, _thread_local.hidden_fields


def extract_hidden_fields(html_text: str) -> dict:
    """Pull every <input type="hidden"> from form so ASP.NET/CSRF tokens are forwarded."""
    soup = BeautifulSoup(html_text, "html.parser")
    form = soup.find("form") or soup
    fields = {}
    for inp in form.find_all("input", type="hidden"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    return fields


# --------------------------------------------------------------------------- #
# Top-Level Project Discovery
# --------------------------------------------------------------------------- #

def discover_projects(s: requests.Session, save_discovery_page: Path | None = None) -> list[dict]:
    """
    Scrape top-level summary details for ALL approved projects from /viewAllProjects.
    Returns list of dicts: [{acknowledgement_no, registration_no, project_name, firm_name}, ...]
    """
    res = _request_with_retry("GET", s, f"{BASE}/viewAllProjects", params={"language": "en"})
    html_text = res.text

    if save_discovery_page:
        save_discovery_page.mkdir(parents=True, exist_ok=True)
        (save_discovery_page / "discovery_page.html").write_text(html_text, encoding="utf-8")

    # Strategy 1: Primary contiguous JS push regex match
    matches = _DISCOVERY_PATTERN.findall(html_text)

    # Strategy 2: Relaxed independent JS array extraction (resilient to JS refactoring/intervening code)
    if not matches:
        a1 = re.findall(r"applicationNameList\s*\.push\('([^']*)'\)", html_text)
        a2 = re.findall(r"applicationNameList2\s*\.push\('([^']*)'\)", html_text)
        a3 = re.findall(r"applicationNameList3\s*\.push\('([^']*)'\)", html_text)
        a4 = re.findall(r"applicationNameList4\s*\.push\('([^']*)'\)", html_text)
        if a1 and len(a1) == len(a2) == len(a3) == len(a4):
            matches = list(zip(a1, a2, a3, a4))

    # Strategy 3: HTML DOM table parsing fallback (if page switches to HTML rendering)
    if not matches:
        soup = BeautifulSoup(html_text, "html.parser")
        dom_matches = []
        for tr in soup.find_all("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) >= 4:
                reg_candidate = None
                for col in tds:
                    if "PRM/KA/RERA" in col or "ACK/KA/RERA" in col or "RERA" in col:
                        reg_candidate = col
                        break
                if reg_candidate:
                    dom_matches.append((tds[0], tds[1] if len(tds)>1 else reg_candidate, tds[2] if len(tds)>2 else "", tds[3] if len(tds)>3 else ""))
        if dom_matches:
            matches = dom_matches

    if not matches:
        raise CrawlerError(
            "Discovery found 0 projects in /viewAllProjects page across all 3 extraction strategies (JS push, independent arrays, DOM table)."
        )

    projects = []
    seen_regnos = set()
    for ack_no, reg_no, name, firm in matches:
        reg_no = html_module.unescape(reg_no).strip()
        if not reg_no or reg_no in seen_regnos:
            continue
        seen_regnos.add(reg_no)
        projects.append({
            "acknowledgement_no": html_module.unescape(ack_no).strip(),
            "registration_no": reg_no,
            "project_name": html_module.unescape(name).strip(),
            "firm_name": html_module.unescape(firm).strip(),
        })

    return projects


# --------------------------------------------------------------------------- #
# Single Property Search & ID Resolution
# --------------------------------------------------------------------------- #

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
    """Run search form POST for a specific property."""
    data = dict(hidden_fields)
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
            f"First 500 chars:\n{res.text[:500]}"
        )
    return res.text


def resolve_project_id(search_html: str, reg_no: str = "", project_name: str = ""):
    """Extract numeric PROJECT_ID candidate from search results page HTML."""
    soup = BeautifulSoup(search_html, "html.parser")
    candidates = []

    # Heuristic 1 (Highest priority): Row-scoped match for reg_no or project_name
    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True)
        if (reg_no and reg_no in text) or (project_name and project_name in text):
            for tag in tr.find_all(["a", "button"]):
                title = tag.get("title", "") or ""
                onclick = tag.get("onclick", "") or ""
                tag_id = (tag.get("id", "") or "").strip()

                if tag_id.isdigit() and ("Project Details" in title or "showFileApplicationPreview" in onclick):
                    candidates.append((tag_id, f"row exact button id: {text[:80]}"))
                elif tag_id.isdigit():
                    candidates.append((tag_id, f"row tag id: {text[:80]}"))
                else:
                    m = re.search(r"(\d+)", onclick)
                    if m:
                        candidates.append((m.group(1), f"row onclick match: {text[:80]}"))

    # Heuristic 2: Global button id match with Project Details title or showFileApplicationPreview
    if not candidates:
        for tag in soup.find_all(["a", "button"]):
            title = tag.get("title", "") or ""
            onclick = tag.get("onclick", "") or ""
            tag_id = (tag.get("id", "") or "").strip()
            if tag_id.isdigit() and ("Project Details" in title or "showFileApplicationPreview" in onclick):
                candidates.append((tag_id, "global button id match"))

    # Heuristic 3: Global onclick regex match
    if not candidates:
        for m in re.finditer(r"(?:action|projectDetails|viewProject)\w*\(['\"]?(\d+)['\"]?\)", search_html):
            candidates.append((m.group(1), "global onclick match"))

    if not candidates:
        return None, []

    return candidates[0][0], candidates


def parse_search_result_row(search_html: str, reg_no: str = "", project_name: str = "") -> dict:
    """
    Parse search result table row from /projectViewDetails.
    Extracts status, district, taluk, dates, and PDF certificate download URLs.
    """
    soup = BeautifulSoup(search_html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return {}

    for table in tables:
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if not headers:
            continue

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            row_text = tr.get_text(" ", strip=True)
            if (reg_no and reg_no in row_text) or (project_name and project_name in row_text) or (not reg_no and not project_name):
                row_data = {}
                for header, td in zip(headers, tds):
                    val = td.get_text(" ", strip=True)
                    clean_header = re.sub(r"[^\w\s]", "", header.strip().lower())
                    clean_header = re.sub(r"\s+", "_", clean_header).strip("_")
                    if not clean_header or clean_header in ("sno", "s_no", "view_project_details"):
                        continue
                    if val:
                        row_data[clean_header] = val
                    links = extract_cell_links(td)
                    if links:
                        row_data[f"{clean_header}_url"] = links
                if row_data:
                    return row_data
    return {}



def fetch_project_detail_html(s: requests.Session, project_id: str) -> str:
    """Fetch detail page HTML for a given internal PROJECT_ID."""
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
            f"/projectDetails response for PROJECT_ID={project_id} looks unusual ({reason})."
        )
    return res.text


# --------------------------------------------------------------------------- #
# Generic v9 Tab Parsing Helpers (with URL extraction)
# --------------------------------------------------------------------------- #

def extract_cell_links(td) -> list[str]:
    """Pull downloadable absolute URLs (<a href> or <img src>) from a DOM cell."""
    urls = []
    for a in td.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.lower().startswith("javascript:"):
            urls.append(requests.compat.urljoin(BASE, href))
    for img in td.find_all("img", src=True):
        src = img["src"].strip()
        if src:
            urls.append(requests.compat.urljoin(BASE, src))

    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_label_value_sections(container) -> dict:
    """Parse section divs with Label: Value pairs and capture embedded document URLs."""
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
        links = [extract_cell_links(c) for c in cols]
        for i in range(0, len(texts) - 1, 2):
            label = texts[i].rstrip(":").strip()
            value = texts[i + 1].strip()
            if not label:
                continue
            if label in current_record:
                flush()
            current_record[label] = value
            value_urls = links[i + 1] if i + 1 < len(links) else []
            if value_urls:
                current_record[f"{label} URL"] = value_urls

    flush()

    result = {}
    for section, records in records_by_section.items():
        result[section] = records[0] if len(records) == 1 else records
    return result


def parse_tables(container) -> list[dict]:
    """Parse html tables into rows of dicts, expanding colspan/rowspan into a 2D grid
    so column headers and values remain correctly aligned.
    """
    if not container:
        return []
    all_tables = []
    for table in container.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue

        # 1. Build a 2D grid to resolve rowspan and colspan spanning
        grid = {}
        max_col = 0

        for r_idx, tr in enumerate(trs):
            c_idx = 0
            cells = tr.find_all(["th", "td"])
            for cell in cells:
                while (r_idx, c_idx) in grid:
                    c_idx += 1

                try:
                    rowspan = max(1, int(cell.get("rowspan", 1)))
                except (ValueError, TypeError):
                    rowspan = 1
                try:
                    colspan = max(1, int(cell.get("colspan", 1)))
                except (ValueError, TypeError):
                    colspan = 1

                text = cell.get_text(" ", strip=True)
                links = extract_cell_links(cell)
                is_header = (cell.name == "th")

                cell_info = {"text": text, "links": links, "is_header": is_header}

                for r_offset in range(rowspan):
                    for c_offset in range(colspan):
                        curr_r = r_idx + r_offset
                        curr_c = c_idx + c_offset
                        grid[(curr_r, curr_c)] = cell_info
                        if curr_c > max_col:
                            max_col = curr_c

                c_idx += colspan

        num_rows = len(trs)
        num_cols = max_col + 1 if grid else 0
        if num_cols == 0:
            continue

        # 2. Separate header rows from data rows
        header_rows = []
        data_row_indices = []

        for r in range(num_rows):
            row_cells = [grid.get((r, c)) for c in range(num_cols)]
            has_headers = any(cell and cell["is_header"] for cell in row_cells)
            all_headers = all(cell and cell["is_header"] for cell in row_cells if cell and cell["text"])

            if has_headers and (r == 0 or all_headers):
                header_rows.append(row_cells)
            else:
                if any(cell and cell["text"] for cell in row_cells):
                    data_row_indices.append(r)

        # 3. Construct headers list (combining multi-level header rows if present)
        headers = []
        if header_rows:
            combined = []
            for c in range(num_cols):
                parts = []
                for h_row in header_rows:
                    cell = h_row[c]
                    if cell and cell["text"] and cell["text"] not in parts:
                        parts.append(cell["text"])
                combined.append(" ".join(parts).strip())
            headers = combined

        # 4. Extract data rows mapped to header keys
        rows = []
        for r in data_row_indices:
            row_cells = [grid.get((r, c)) for c in range(num_cols)]
            values = [cell["text"] if cell else "" for cell in row_cells]
            links = [cell["links"] if cell else [] for cell in row_cells]

            if headers and len(headers) == len(values):
                row = {}
                for header, val, url_list in zip(headers, values, links):
                    if not header:
                        header = "unnamed_column"
                    key = header
                    dup_cnt = 1
                    while key in row:
                        dup_cnt += 1
                        key = f"{header}_{dup_cnt}"
                    row[key] = val
                    if url_list:
                        row[f"{key} URL"] = url_list
                if row:
                    rows.append(row)
            else:
                row = {"columns": values}
                if any(links):
                    row["column_urls"] = links
                rows.append(row)

        all_tables.append({"headers": headers, "rows": rows})

    return all_tables


def parse_documents_matrix(container) -> list[dict]:
    """Parse transposed financial uploaded documents matrix into flat records with URLs."""
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
                urls = extract_cell_links(td)
                if value or urls:
                    entry = {"document_type": doc_type, "financial_year": year, "file": value}
                    if urls:
                        entry["file_url"] = urls
                    out.append(entry)
    return out


def parse_all_label_value_pairs(container) -> tuple[dict, list[str]]:
    """Parse all label-value pairs and iframe URLs across any column layout (col-md-6, col-md-3, etc.)."""
    if not container:
        return {}, []
    data = {}
    iframes = []
    for iframe in container.find_all("iframe", src=True):
        src = iframe["src"].strip()
        if src:
            iframes.append(src)

    for r in container.find_all("div", class_="row"):
        ps = r.find_all("p", recursive=True)
        i = 0
        while i < len(ps) - 1:
            p1, p2 = ps[i], ps[i + 1]
            t1 = p1.get_text(" ", strip=True)
            t2 = p2.get_text(" ", strip=True)
            if ":" in t1 or "text-right" in p1.get("class", []):
                label = re.sub(r"[\s:]+$", "", t1).strip()
                val = t2.strip()
                if label and label not in data:
                    data[label] = val
                    links = extract_cell_links(p2)
                    if links:
                        data[f"{label} URL"] = links
                i += 2
            else:
                i += 1
    return data, iframes


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
    # Quiet optional section warnings during batch crawling
    pass



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


def normalize_project_detail(html_text: str) -> dict:
    """Normalize complete multi-tab detail page HTML into structured JSON."""
    soup = BeautifulSoup(html_text, "html.parser")
    header = soup.find("div", id="header2")

    result = {
        "project_name": header_field(header, "Project Name"),
        "acknowledgement_number": header_field(header, "Acknowledgement Number"),
        "registration_number": header_field(header, "Registration Number"),
    }

    # Promoter Details tab
    div = soup.find("div", id=TAB_IDS["promoter_details"])
    if div is None:
        _warn_missing("promoter_details", TAB_IDS["promoter_details"])
        result["promoter_details"] = {}
    else:
        result["promoter_details"] = parse_label_value_sections(div)

    # Project Details tab
    div = soup.find("div", id=TAB_IDS["project_details"])
    if div is None:
        _warn_missing("project_details", TAB_IDS["project_details"])
        result["project_details"] = {}
    else:
        sections = parse_label_value_sections(div)
        tables = parse_tables(div)
        if tables:
            all_table_rows = []
            for t in tables:
                all_table_rows.extend(t.get("rows", []))
            sections["survey_land_tables"] = all_table_rows
        result["project_details"] = sections

    # Uploaded Documents tab (menu2) - new format: contains both Location/Overview specs and Document Matrix.
    # Old pre-~2020 format: menu2 contains only document file links (NOCs, certs, etc.);
    # the overview data (Latitude, Longitude, District, Total Area, etc.) lives in menu1 instead.
    div = soup.find("div", id=TAB_IDS["uploaded_documents"])
    if div is None:
        _warn_missing("uploaded_documents", TAB_IDS["uploaded_documents"])
        result["uploaded_documents"] = []
        result["project_overview_details"] = {}
        result["map_iframes"] = []
    else:
        result["uploaded_documents"] = parse_documents_matrix(div)
        overview_pairs, map_iframes = parse_all_label_value_pairs(div)

        # -- Old-format detection & fallback ----------------------------------
        # Signals that the overview data is NOT in menu2 but in menu1 (old format).
        # Key fields present in every real project overview; absent if menu2 only has documents.
        _OLD_OVERVIEW_SIGNALS = {"District", "Project Address", "Total Area Of Land (Sq Mtr)",
                                  "Total Area Of Land (Sq Mtr) (A1+A2)", "Latitude",
                                  "North Latitude", "Approving Authority"}
        is_old_format = not any(k in overview_pairs for k in _OLD_OVERVIEW_SIGNALS)
        if is_old_format:
            menu1_div = soup.find("div", id=TAB_IDS["project_details"])
            if menu1_div:
                old_overview, old_iframes = parse_all_label_value_pairs(menu1_div)
                # Merge old overview into overview_pairs, not overwriting any new-format keys.
                for k, v in old_overview.items():
                    if k not in overview_pairs:
                        overview_pairs[k] = v
                if not map_iframes:
                    map_iframes = old_iframes
                result["_layout_format"] = "legacy_pre2020"
            else:
                result["_layout_format"] = "legacy_pre2020_no_menu1"
        else:
            result["_layout_format"] = "current"
        # ---------------------------------------------------------------------

        result["project_overview_details"] = overview_pairs
        result["map_iframes"] = map_iframes

    # Enquired Documents tab
    div = soup.find("div", id=TAB_IDS["enquired_documents"])
    if div is None:
        _warn_missing("enquired_documents", TAB_IDS["enquired_documents"])
        result["enquired_documents"] = []
    else:
        tables = parse_tables(div)
        result["enquired_documents"] = tables[0]["rows"] if tables else []

    # Quarterly Updates tab
    div = soup.find("div", id=TAB_IDS["quarterly_updates"])
    if div is None:
        _warn_missing("quarterly_updates", TAB_IDS["quarterly_updates"])
        result["quarterly_updates"] = []
    else:
        tables = parse_tables(div)
        result["quarterly_updates"] = tables[0]["rows"] if tables else []

    # Completion Details tab
    div = soup.find("div", id=TAB_IDS["completion_details"])
    if div is None:
        _warn_missing("completion_details", TAB_IDS["completion_details"])
        result["completion_details"] = {}
    else:
        result["completion_details"] = parse_label_value_sections(div)

    # Complaints tab
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
        if project_div:
            tables = parse_tables(project_div)
            complaints_project = tables[0]["rows"] if tables else []
    result["complaints_on_promoter"] = complaints_promoter
    result["complaints_on_project"] = complaints_project

    return result


def clean_value(val):
    """Recursively strip excessive whitespace, newlines, and tabs from strings and nested dicts/lists."""
    if isinstance(val, str):
        return re.sub(r"\s+", " ", val).strip()
    elif isinstance(val, dict):
        return {clean_value(k): clean_value(v) for k, v in val.items() if k}
    elif isinstance(val, list):
        return [clean_value(x) for x in val]
    return val


def extract_location_details(raw_data: dict, search_info: dict) -> dict:
    """Extract and consolidate geographic, survey, coordinates, boundaries, and address details.

    Coordinate extraction handles all RERA Karnataka key variants:
      Plain:          "Latitude", "Longitude"
      Directional:    "Latitude North", "Latitude South", "Longitude East", "Longitude West"
      Parenthesised:  "Latitude(N)", "Longitude(E)", etc.
      Degree-symbol:  values may end in degree sign, "N", etc.
    Coordinates in Karnataka are all positive numbers (~12–18°N, ~74–78°E); direction labels refer to plot boundary edges and signs are preserved as parsed.
    """
    district = search_info.get("district")
    taluk = search_info.get("taluk")

    promoter_section = raw_data.get("promoter_details", {}).get("Promoter Details", {})
    if not isinstance(promoter_section, dict):
        promoter_section = {}
    p_district = promoter_section.get("District")
    p_taluk = promoter_section.get("Taluk")
    pin_code = promoter_section.get("PIN Code")
    promoter_address = promoter_section.get("Promoter Address")

    overview = raw_data.get("project_overview_details", {})
    if not isinstance(overview, dict):
        overview = {}
    map_iframes = raw_data.get("map_iframes", [])

    # ------------------------------------------------------------------
    # Coordinate extraction helpers
    # ------------------------------------------------------------------
    # Two coordinate formats are used across RERA Karnataka registrations:
    #
    #   NEW format (post-~2020)  — decimal degrees in menu2 overview:
    #     "12.9716"            → 12.9716   (plain)
    #     "-12.9716"           → -12.9716  (negative)
    #     "+12.9716"           → 12.9716   (explicit plus)
    #     "12.9716°"           → 12.9716   (degree symbol)
    #     "12.9716° N"         → 12.9716   (direction suffix)
    #     "N 12.9716"          → 12.9716   (direction prefix)
    #     "N/A", ""            → None      (garbage → None)
    #
    #   OLD format (pre-~2020) — DMS in menu1 boundary coord fields:
    #     "12°59'45.61"N"      → 12.9960028  (DMS with direction)
    #     "77°42'17.22"E"      → 77.7047833  (DMS with direction)
    #     degrees°minutes'seconds"direction  (all three parts required)
    #
    # _parse_coord() tries DMS first, then falls back to the first float.
    _DMS_RE = re.compile(
        r"([+-]?\d+)[°º]\s*(\d+)['\'′]\s*(\d+(?:\.\d+)?)[\"″]?\s*([NSEW])?",
        re.IGNORECASE,
    )
    _FLOAT_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")

    def _parse_coord(raw_str):
        """Convert a raw coordinate string to a float decimal degree value, or None.

        Handles both DMS (12°59'45.61"N) and decimal-degree (12.9716) formats.
        DMS is tried first so pre-2020 boundary coords parse correctly.
        """
        if raw_str is None:
            return None
        s = str(raw_str).strip()
        # -- DMS: degrees°minutes'seconds["direction] --
        m = _DMS_RE.search(s)
        if m:
            deg, mins, secs, direction = m.groups()
            val = float(deg) + float(mins) / 60.0 + float(secs) / 3600.0
            if direction and direction.upper() in ("S", "W"):
                val = -val
            return round(val, 7)
        # -- Decimal degrees: first float in the string --
        m = _FLOAT_RE.search(s)
        return float(m.group()) if m else None

    def _extract_coord_by_keys(keys):
        for k in keys:
            raw = overview.get(k)
            if raw is not None and str(raw).strip():
                val = _parse_coord(raw)
                if val is not None:
                    return val
        return None

    boundary_coordinates = {
        "north": {
            "latitude": _extract_coord_by_keys(["Latitude North", "North Latitude", "Latitude(N)", "Latitude (N)", "Lat North", "North Lat"]),
            "longitude": _extract_coord_by_keys(["Longitude North", "North Longitude", "Longitude(N)", "Longitude (N)", "Long North", "North Long"]),
        },
        "south": {
            "latitude": _extract_coord_by_keys(["Latitude South", "South Latitude", "Latitude(S)", "Latitude (S)", "Lat South", "South Lat"]),
            "longitude": _extract_coord_by_keys(["Longitude South", "South Longitude", "Longitude(S)", "Longitude (S)", "Long South", "South Long"]),
        },
        "east": {
            "latitude": _extract_coord_by_keys(["Latitude East", "East Latitude", "Latitude(E)", "Latitude (E)", "Lat East", "East Lat"]),
            "longitude": _extract_coord_by_keys(["Longitude East", "East Longitude", "Longitude(E)", "Longitude (E)", "Long East", "East Long"]),
        },
        "west": {
            "latitude": _extract_coord_by_keys(["Latitude West", "West Latitude", "Latitude(W)", "Latitude (W)", "Lat West", "West Lat"]),
            "longitude": _extract_coord_by_keys(["Longitude West", "West Longitude", "Longitude(W)", "Longitude (W)", "Long West", "West Long"]),
        },
    }

    # Authoritative center coordinates (extracted strictly from "Latitude" & "Longitude")
    lat = _extract_coord_by_keys(["Latitude"])
    lng = _extract_coord_by_keys(["Longitude"])

    coordinate_source = "rera_html" if (lat is not None and lng is not None) else None

    # ------------------------------------------------------------------
    # Old-format fallback: pre-2020 pages only publish boundary edge coords
    # ("North Latitude", "South Latitude", etc.) — no single center point.
    # Derive a best-effort center by averaging the N/S latitudes and E/W
    # longitudes already resolved above as boundary_coordinates.
    # Only applied when at least one center coord is still missing.
    #
    # IMPORTANT: Bounds validation is applied to each boundary value before
    # averaging. RERA promoters occasionally paste a latitude value into a
    # longitude field (or vice-versa), which produces a coordinate wildly
    # outside Karnataka.  We skip such corrupt values and try the other edge
    # (e.g. if West Longitude is corrupt, use only East Longitude).
    # ------------------------------------------------------------------
    _KA_LAT = (11.0, 19.0)   # Karnataka latitude  bounds
    _KA_LNG = (74.0, 79.5)   # Karnataka longitude bounds

    def _valid_lat(v):
        return v is not None and _KA_LAT[0] <= v <= _KA_LAT[1]

    def _valid_lng(v):
        return v is not None and _KA_LNG[0] <= v <= _KA_LNG[1]

    if lat is None or lng is None:
        n_lat = boundary_coordinates["north"]["latitude"]
        s_lat = boundary_coordinates["south"]["latitude"]
        e_lng = boundary_coordinates["east"]["longitude"]
        w_lng = boundary_coordinates["west"]["longitude"]

        # Filter to only in-bounds values before averaging
        valid_lats = [v for v in (n_lat, s_lat) if _valid_lat(v)]
        valid_lngs = [v for v in (e_lng, w_lng) if _valid_lng(v)]

        derived_lat = round(sum(valid_lats) / len(valid_lats), 7) if valid_lats else None
        derived_lng = round(sum(valid_lngs) / len(valid_lngs), 7) if valid_lngs else None

        if derived_lat is not None and derived_lng is not None:
            lat = derived_lat
            lng = derived_lng
            coordinate_source = "boundary_avg"

    # ------------------------------------------------------------------
    # Iframe fallback: if HTML missed at least one coordinate, try parsing
    # the Google Maps embed src as a COMPLETE PAIR only (no partial mixing).
    # ------------------------------------------------------------------
    def _coords_from_iframe(iframe_src: str):
        """Return (lat, lng) floats parsed from a Google Maps embed URL, or (None, None)."""
        if not iframe_src:
            return None, None
        for pat in (
            r"[?&]q=([-\.\d]+),([-\.\d]+)",
            r"[?&]ll=([-\.\d]+),([-\.\d]+)",
            r"@([-\.\d]+),([-\.\d]+)",
        ):
            m = re.search(pat, iframe_src)
            if m:
                try:
                    return float(m.group(1)), float(m.group(2))
                except ValueError:
                    continue
        return None, None

    if lat is None or lng is None:
        for src in map_iframes:
            fb_lat, fb_lng = _coords_from_iframe(src)
            if fb_lat is not None and fb_lng is not None:
                lat, lng = fb_lat, fb_lng
                coordinate_source = "google_maps_iframe"
                break

    # ------------------------------------------------------------------
    # GeoJSON geometry:
    #
    # RERA Karnataka does NOT publish polygon coordinates — only a center
    # point and legal-schedule text for the four cardinal boundaries.
    #
    # We produce two levels of geometry:
    #   1. "center"  — GeoJSON Point from the selected coordinate source.
    #                  Prefer RERA HTML coordinates; Google Maps iframe is fallback.
    #   2. "bbox_polygon" — GeoJSON Polygon approximated from the total
    #      land area (sq m) as a square centred on the Point.
    #      This is clearly flagged as an approximation so downstream
    #      consumers are not misled.
    #
    # Derivation of half-side from area:
    #   side = sqrt(area_sq_m)  =>  half_side = side / 2
    #   1 degree latitude  ~ 111,320 m  (near-constant)
    #   1 degree longitude ~ 111,320 * cos(lat_rad) m  (varies with lat)
    # ------------------------------------------------------------------
    import math as _math

    def _build_geometry(center_lat, center_lng, area_sq_m=None, coord_source=None):
        """
        Return a dict with:
          center            -- GeoJSON Point  {type, coordinates}
          bbox_polygon      -- GeoJSON Polygon (approximate square, or None)
          area_source       -- label for how the bbox was derived
          coordinate_source -- label for coordinate origin ("rera_html", "google_maps_iframe", or None)
        """
        if center_lat is None or center_lng is None:
            return {
                "center": None,
                "bbox_polygon": None,
                "area_source": None,
                "coordinate_source": coord_source,
            }

        geojson_point = {
            "type": "Point",
            "coordinates": [round(center_lng, 7), round(center_lat, 7)],
        }

        bbox_poly = None
        area_source = None
        if area_sq_m and area_sq_m > 0:
            half_side_m = _math.sqrt(area_sq_m) / 2.0
            lat_deg = half_side_m / 111_320.0
            lng_deg = half_side_m / (111_320.0 * _math.cos(_math.radians(center_lat)))
            min_lat = round(center_lat - lat_deg, 7)
            max_lat = round(center_lat + lat_deg, 7)
            min_lng = round(center_lng - lng_deg, 7)
            max_lng = round(center_lng + lng_deg, 7)
            # GeoJSON Polygon: exterior ring, 5 points (closed)
            bbox_poly = {
                "type": "Polygon",
                "coordinates": [[
                    [min_lng, min_lat],
                    [max_lng, min_lat],
                    [max_lng, max_lat],
                    [min_lng, max_lat],
                    [min_lng, min_lat],
                ]],
            }
            area_source = "total_land_area_sq_mtr"

        return {
            "center": geojson_point,
            "bbox_polygon": bbox_poly,
            "area_source": area_source,
            "coordinate_source": coord_source,
            "_note": (
                "bbox_polygon is an equal-area square approximation centred on the "
                "project point. RERA Karnataka does not publish polygon boundary "
                "coordinates; use the legal schedule text in boundaries{} for the "
                "authoritative description."
            ) if bbox_poly else None,
        }

    # Try to get total land area from overview for bbox derivation
    _area_raw = overview.get("Total Area Of Land (Sq Mtr) (A1+A2)") or overview.get("Total Area Of Land (Sq Mtr)")
    _area_sq_m = None
    if _area_raw:
        try:
            _area_sq_m = float(str(_area_raw).replace(",", "").strip())
        except ValueError:
            pass

    geometry = _build_geometry(lat, lng, _area_sq_m, coordinate_source)

    # ------------------------------------------------------------------
    # Boundary schedules -- accept both plain and prefixed key variants
    # ------------------------------------------------------------------
    def _first_str(*keys):
        for k in keys:
            v = overview.get(k)
            if v and str(v).strip():
                return str(v).strip()
        return None

    b_north = _first_str("North Schedule", "North_Schedule", "Boundary North", "North Boundary")
    b_south = _first_str("South Schedule", "South_Schedule", "Boundary South", "South Boundary")
    b_east  = _first_str("East Schedule",  "East_Schedule",  "Boundary East",  "East Boundary")
    b_west  = _first_str("West Schedule",  "West_Schedule",  "Boundary West",  "West Boundary")

    survey_tables = raw_data.get("project_details", {}).get("survey_land_tables", [])

    # ------------------------------------------------------------------
    # Survey number and sub-registrar extraction — column-semantic, not
    # pattern-reactive.
    #
    # parse_tables() produces two row shapes depending on whether the
    # <table> had <th> headers that matched the column count:
    #
    #   Keyed row:      {"Survey No": "59/1", "Sub Registrar": "...", ...}
    #   Headerless row: {"columns": ["59/1", "...", ...]}
    #
    # Strategy:
    #   Phase 1 (keyed rows) — read well-known column names directly.
    #     No pattern matching needed; the header IS the semantic label.
    #   Phase 2 (headerless fallback) — apply the Karnataka Revenue Dept
    #     survey-number grammar:  digits, optional suffix letter, optional
    #     slash-sub-number repetition.  Examples: "59", "59/1", "59/1A",
    #     "59/1A/2", "Sy.No.59/1".  A cell is accepted only if the regex
    #     fills the entire token after stripping a "Sy" / "Sy.No." prefix.
    #     Numeric-only values that look like areas, amounts, or IDs
    #     (>= 6 digits, or contain a decimal point) are rejected outright.
    # ------------------------------------------------------------------

    # Karnataka Revenue Dept survey number grammar (anchored)
    _SY_RE = re.compile(
        r"^(?:Sy\.?\s*No\.?\s*)?(\d{1,5}[A-Za-z]?(?:/\d{1,5}[A-Za-z]?)*)$",
        re.IGNORECASE,
    )

    # Known header variants across RERA Karnataka form versions
    _SY_HEADERS  = {"Survey No", "Sy No", "Sy.No", "Sy.No.", "Survey Number",
                    "SurveyNo", "Survey No.", "Sy No."}
    _SUB_REG_HEADERS = {
        # Exact column-header match; both plain and hyphenated forms appear
        # across different RERA Karnataka form versions.
        "Sub Registrar", "Sub-Registrar", "Sub Registrar Office",
        "SubRegistrar", "Sub Registrar Name", "Sub Reg",
        "Sub Registrar Of Assurances", "SRO",
    }
    _ADDR_HEADERS = {"Address", "Land Address", "Property Address",
                     "Site Address", "Project Address"}

    # ------------------------------------------------------------------
    # SRO name normaliser: strip verbose boilerplate that RERA promoters
    # often paste in full ("Sub Registrar Office, Basavanagudi, Bengaluru
    # South, Karnataka - 560004").  We want just the meaningful name part.
    # Examples:
    #   "Sub Registrar Office, JP Nagar"  → "JP Nagar"
    #   "JP Nagar Sub Registrar"          → "JP Nagar"
    #   "Sub Registrar, Yelahanka"        → "Yelahanka"
    #   "Yelahanka"                       → "Yelahanka"  (already clean)
    # ------------------------------------------------------------------
    _SRO_STRIP_RE = re.compile(
        r"(?:Sub[\s\-]Registrar(?:\s+(?:Office|Of\s+Assurances))?[,\s]*"
        r"|[,\s]*Sub[\s\-]Registrar(?:\s+(?:Office|Of\s+Assurances))?)",
        re.IGNORECASE,
    )

    _INVALID_SRO_TERMS = {
        "residential", "e khatha", "own land", "by sale", "dc office",
        "dc office bengaluru", "dc office benglauru", "village development and panchayat raj"
    }

    def _is_valid_sro(val: str) -> bool:
        v = val.strip()
        if not v or v.isdigit():
            return False
        if re.search(r"\b\d+\s*(?:acres?|guntas?|sq\s*mtr|sqft)\b", v, re.I):
            return False
        if re.search(r"\b(?:tower|flat|apt|apartment|khatha|residential)\b", v, re.I):
            return False
        if v.lower() in _INVALID_SRO_TERMS:
            return False
        return True

    def _normalize_sro(raw: str) -> str:
        """Return the meaningful part of an SRO name, stripping generic boilerplate."""
        cleaned = _SRO_STRIP_RE.sub(" ", raw).strip(" ,;-")
        first_token = cleaned.split(",")[0].strip()
        return first_token if first_token else cleaned

    survey_nos    = []
    sub_registrars = []
    land_addresses = []

    for row in survey_tables:
        # ---- Phase 1: keyed row (headers matched) ----
        if isinstance(row, dict) and "columns" not in row:
            for h, val in row.items():
                if not val or h.endswith(" URL"):
                    continue
                val_str = str(val).strip()
                h_norm  = h.strip()

                # Extract survey number embedded in multi-level header string
                m_hdr_sy = re.search(r"Survey\s+Number\s+([0-9]+(?:/[0-9A-Za-z]+)?)", h_norm, re.I)
                if m_hdr_sy:
                    sy_val = m_hdr_sy.group(1)
                    if sy_val not in survey_nos:
                        survey_nos.append(sy_val)

                # Check exact header or suffix/substring match (supporting multi-level headers)
                h_lower = h_norm.lower()
                is_sy = h_norm in _SY_HEADERS or any(h_lower.endswith(" " + sy.lower()) for sy in _SY_HEADERS)
                is_subreg = h_norm in _SUB_REG_HEADERS or any(sro.lower() in h_lower for sro in _SUB_REG_HEADERS)
                is_addr = h_norm in _ADDR_HEADERS or any(addr.lower() in h_lower for addr in _ADDR_HEADERS)

                if is_sy:
                    # Value may be comma/semicolon separated multi-entry
                    for token in re.split(r"[,;\s]+", val_str):
                        token = token.strip()
                        if token and token not in survey_nos:
                            survey_nos.append(token)

                elif is_subreg:
                    normed = _normalize_sro(val_str)
                    if normed and _is_valid_sro(normed) and normed not in sub_registrars:
                        sub_registrars.append(normed)

                elif is_addr:
                    if val_str not in land_addresses:
                        land_addresses.append(val_str)

        # ---- Phase 2: headerless fallback ----
        else:
            cols = row.get("columns", [])
            for cell in cols:
                cell_str = str(cell).strip()
                if not cell_str:
                    continue

                # Survey number: must match grammar, must not look like a
                # decimal area / large numeric ID
                m = _SY_RE.match(cell_str)
                if m:
                    captured = m.group(1)
                    # Reject bare integers >= 6 digits (IDs/amounts)
                    # and anything containing a decimal point (areas)
                    if "." not in captured and len(re.sub(r"\D", "", captured)) <= 5:
                        if captured not in survey_nos:
                            survey_nos.append(captured)

                # Sub-registrar: the phrase "Sub Registrar" (or hyphenated) is a
                # reliable linguistic anchor — every Karnataka SRO name contains it.
                # We cannot do better in a headerless row without external SRO data.
                # Normalise before storing so the value is the location name only.
                if re.search(r"Sub[\s\-]Registrar", cell_str, re.IGNORECASE):
                    normed = _normalize_sro(cell_str)
                    if normed and normed not in sub_registrars:
                        sub_registrars.append(normed)

                # Address: contains a strong Karnataka locale signal
                if re.search(r"Karnataka|Bengaluru|Bangalore", cell_str) and len(cell_str) > 20:
                    if cell_str not in land_addresses:
                        land_addresses.append(cell_str)

    full_address = overview.get("Project Address") or (land_addresses[0] if land_addresses else promoter_address)

    # Address / description survey number regex fallback.
    # Old-format projects embed the survey number in "Project Address" or
    # "Project Description" (e.g. "Sy.No. 28/2, Hoodi Village...") rather
    # than in a structured land-survey table.
    if not survey_nos:
        # Combine address + project description for broader coverage.
        proj_desc = overview.get("Project Description") or ""
        addr_text = " ".join(filter(None, [full_address, proj_desc]))
        # Match patterns like "28/2", "59/1A", "123/4B" but avoid plain integers
        for sn in re.findall(r"\b(\d+/\d+[A-Za-z]?)\b", addr_text):
            if sn not in survey_nos:
                survey_nos.append(sn)

    raw_maps_url = map_iframes[0] if map_iframes else None
    clean_maps_url = None
    if lat is not None and lng is not None:
        clean_maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    elif raw_maps_url:
        m = re.search(r"(?:[?&](?:q|ll)=|@)([-+]?\d+\.\d+),([-+]?\d+\.\d+)", raw_maps_url)
        if m:
            clean_maps_url = f"https://www.google.com/maps/search/?api=1&query={m.group(1)},{m.group(2)}"
        else:
            clean_maps_url = re.sub(r"[?&]output=embed", "", raw_maps_url).replace("&amp;", "&")

    return {
        "district": district or overview.get("District") or p_district,
        "taluk": taluk or overview.get("Taluk") or p_taluk,
        "pin_code": overview.get("Pin Code") or pin_code,
        "survey_numbers": survey_nos,
        "sub_registrar_offices": sub_registrars,   # list; one per land parcel; normalised SRO name
        "address": full_address,
        "latitude": lat,
        "longitude": lng,
        "coordinate_source": coordinate_source,
        "boundary_coordinates": boundary_coordinates,
        "geometry": geometry,
        "google_maps_url": clean_maps_url,
        "boundaries": {
            "north_schedule": b_north,
            "south_schedule": b_south,
            "east_schedule": b_east,
            "west_schedule": b_west,
        }
    }


def validate_record(record: dict) -> dict:
    """Assess data quality and completeness of a scraped RERA project record.

    Checks spatial, identity, location, and geometry attributes.
    Returns a structured data_quality dictionary.
    """
    loc = record.get("project_location", {})
    lat = loc.get("latitude")
    lng = loc.get("longitude")
    coord_src = loc.get("coordinate_source")
    
    # Karnataka geographic coordinate bounds check
    valid_coords = False
    if lat is not None and lng is not None and coord_src is not None:
        try:
            f_lat, f_lng = float(lat), float(lng)
            if 11.0 <= f_lat <= 19.0 and 74.0 <= f_lng <= 79.5:
                valid_coords = True
        except (ValueError, TypeError):
            pass

    survey_nos = loc.get("survey_numbers", [])
    sros = loc.get("sub_registrar_offices", [])
    address = loc.get("address") or record.get("district")
    district = loc.get("district") or record.get("district")
    
    reg_no = record.get("registration_no")
    proj_name = record.get("project_name")
    firm_name = record.get("promoter_name")
    
    geom = loc.get("geometry", {})
    has_center = bool(geom.get("center"))
    has_bbox = bool(geom.get("bbox_polygon"))
    
    # Spatial relationship sanity checks on boundary coordinates
    b_coords = loc.get("boundary_coordinates", {})
    north_lat = b_coords.get("north", {}).get("latitude")
    south_lat = b_coords.get("south", {}).get("latitude")
    east_lng = b_coords.get("east", {}).get("longitude")
    west_lng = b_coords.get("west", {}).get("longitude")

    valid_boundary_rel = True
    warnings = []
    
    if north_lat is not None and south_lat is not None:
        if float(north_lat) <= float(south_lat):
            valid_boundary_rel = False
            warnings.append(
                f"Malformed boundary coordinates: North latitude ({north_lat}) must be greater than South latitude ({south_lat})"
            )
    if east_lng is not None and west_lng is not None:
        if float(east_lng) <= float(west_lng):
            valid_boundary_rel = False
            warnings.append(
                f"Malformed boundary coordinates: East longitude ({east_lng}) must be greater than West longitude ({west_lng})"
            )

    # Absolute geographic bounding-box validation on all boundary points
    for direction in ("north", "south", "east", "west"):
        point = b_coords.get(direction, {})
        p_lat = point.get("latitude")
        p_lng = point.get("longitude")
        if p_lat is not None:
            if not (11.0 <= float(p_lat) <= 19.0):
                valid_boundary_rel = False
                warnings.append(f"Boundary coordinate out of Karnataka bounds: {direction} latitude ({p_lat})")
        if p_lng is not None:
            if not (74.0 <= float(p_lng) <= 79.5):
                valid_boundary_rel = False
                warnings.append(f"Boundary coordinate out of Karnataka bounds: {direction} longitude ({p_lng})")

    checks = {
        "has_registration_no": bool(reg_no),
        "has_project_name": bool(proj_name),
        "has_promoter_name": bool(firm_name),
        "has_coordinates": valid_coords,
        "has_survey_numbers": bool(survey_nos),
        "has_sub_registrar": bool(sros),
        "has_address": bool(address or district),
        "has_geometry_center": has_center,
        "has_geometry_bbox": has_bbox,
        "valid_boundary_relationships": valid_boundary_rel,
    }
    
    if not valid_coords:
        if coord_src is None:
            warnings.append("Missing authoritative center geographic coordinates (coordinate_source is null)")
        else:
            warnings.append("Missing or out-of-bounds geographic coordinates")
    if not survey_nos:
        warnings.append("Missing land survey numbers")
    if not sros:
        warnings.append("Missing Sub-Registrar Office jurisdiction")
    if not (address or district):
        warnings.append("Missing property location address/district")
    if not has_bbox:
        warnings.append("Missing total land area for GeoJSON bounding-box construction")

    passed_checks = sum(1 for v in checks.values() if v)
    total_checks = len(checks)
    quality_score = int(round((passed_checks / total_checks) * 100))
    
    is_complete = all([
        checks["has_registration_no"],
        checks["has_project_name"],
        checks["has_coordinates"],
        checks["has_survey_numbers"],
        checks["valid_boundary_relationships"]
    ])

    return {
        "is_complete": is_complete,
        "quality_score": quality_score,
        "checks": checks,
        "warnings": warnings,
    }


def build_clean_structured_record(
    raw_data: dict,
    reg_no: str,
    project_name: str,
    acknowledgement_no: str = None,
    promoter_name: str = None,
    project_id: str = None,
    search_listing_details: dict = None
) -> dict:
    """Build a clean, standardized, highly structured RERA project record."""
    search_info = search_listing_details or raw_data.get("search_listing_details") or {}
    location_info = extract_location_details(raw_data, search_info)

    overview_details = raw_data.get("project_overview_details", {})
    promoter_sec = raw_data.get("promoter_details", {})
    p_details = promoter_sec.get("Promoter Details", {}) if isinstance(promoter_sec, dict) else {}

    record = {
        "registration_no": reg_no or raw_data.get("registration_number") or raw_data.get("_registration_no") or overview_details.get("Registration Number"),
        "acknowledgement_no": acknowledgement_no or raw_data.get("acknowledgement_number") or raw_data.get("_acknowledgement_no") or overview_details.get("Acknowledgement Number"),
        "project_name": project_name or raw_data.get("project_name") or overview_details.get("Project Name"),
        # Old format uses "Name"; new format uses "Promoter Name".
        "promoter_name": promoter_name or search_info.get("promoter_name") or raw_data.get("_firm_name_from_listing") or p_details.get("Promoter Name") or p_details.get("Name"),
        "status": search_info.get("status") or overview_details.get("Project Status"),
        "district": location_info.get("district"),
        "taluk": location_info.get("taluk"),
        "project_type": search_info.get("project_type") or overview_details.get("Project Type"),
        "approved_on": search_info.get("approved_on"),
        "proposed_completion_date": search_info.get("proposed_completion_date"),
        "proposed_completion_date_at_registration": search_info.get("proposed_completion_date_at_the_time_of_registration"),
        "certificate_url": search_info.get("certificate_url"),
        "covid_certificate_url": search_info.get("covid_certificate_url"),
        "renewed_certificate_url": search_info.get("renewed_certificate_url"),
        "further_extension_order_url": search_info.get("further_extension_order_url"),
        "internal_project_id": project_id or raw_data.get("_project_id"),
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "project_location": location_info,
        "search_listing_details": search_info,
        "project_overview_details": raw_data.get("project_overview_details", {}),
        "promoter_details": raw_data.get("promoter_details", {}),
        "project_details": raw_data.get("project_details", {}),
        "uploaded_documents": raw_data.get("uploaded_documents", []),
        "enquired_documents": raw_data.get("enquired_documents", []),
        "quarterly_updates": raw_data.get("quarterly_updates", []),
        "completion_details": raw_data.get("completion_details", {}),
        "complaints": {
            "on_promoter": raw_data.get("complaints_on_promoter", []),
            "on_project": raw_data.get("complaints_on_project", [])
        }
    }

    record["data_quality"] = validate_record(record)
    return clean_value(record)




# --------------------------------------------------------------------------- #
# CSV & Storage Helpers
# --------------------------------------------------------------------------- #

def flatten_for_csv(data: dict) -> dict:
    """Flatten nested dict/list values into JSON strings for CSV row alignment."""
    flat = {}
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            flat[k] = json.dumps(v, ensure_ascii=False)
        else:
            flat[k] = v
    return flat


def write_single_csv(row: dict, output_path: Path):
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)


class PostgresStorage:
    """Manages upserting scraped project records into a PostgreSQL database."""
    def __init__(self, db_uri: str, force_init: bool = False):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise CrawlerError("psycopg2-binary package is required for PostgreSQL support. Install with: pip install psycopg2-binary")
        
        self.db_uri = db_uri
        self._db_lock = threading.Lock()
        self.conn = psycopg2.connect(db_uri)
        self.conn.autocommit = True
        self._init_db(force=force_init)

    def _init_db(self, force: bool = False):
        with self.conn.cursor() as cur:
            if not force:
                # Fast check: skip heavy DDL if schema already exists to prevent lock contention
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = 'public' AND table_name = 'rera_projects'
                    );
                """)
                if cur.fetchone()[0]:
                    return
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rera_projects (
                    registration_no VARCHAR(255) PRIMARY KEY,
                    acknowledgement_no VARCHAR(255),
                    project_name TEXT,
                    firm_name TEXT,
                    project_id VARCHAR(100),
                    crawled_at TIMESTAMP WITH TIME ZONE,
                    data JSONB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rera_projects_name ON rera_projects (project_name);
                CREATE INDEX IF NOT EXISTS idx_rera_projects_crawled_at ON rera_projects (crawled_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_rera_ack_no_unique
                    ON rera_projects (acknowledgement_no)
                    WHERE acknowledgement_no IS NOT NULL;

                -- GIN index: accelerates containment (@>, ?) and full-document JSONB searches
                CREATE INDEX IF NOT EXISTS idx_rera_projects_jsonb ON rera_projects USING GIN (data);

                -- Expression indexes: accelerate ->> text-equality filters used by vw_rera_projects_analytics
                -- (GIN does NOT speed these up; only a dedicated B-tree expression index does)
                CREATE INDEX IF NOT EXISTS idx_rera_status
                    ON rera_projects ((COALESCE(data->>'status', data->'project_overview_details'->>'Project Status')));
                CREATE INDEX IF NOT EXISTS idx_rera_district
                    ON rera_projects ((data->'project_location'->>'district'));
                CREATE INDEX IF NOT EXISTS idx_rera_taluk
                    ON rera_projects ((data->'project_location'->>'taluk'));
                CREATE INDEX IF NOT EXISTS idx_rera_project_sub_type
                    ON rera_projects ((data->'project_overview_details'->>'Project Sub Type'));
                CREATE INDEX IF NOT EXISTS idx_rera_approving_authority
                    ON rera_projects ((data->'project_overview_details'->>'Approving Authority'));

                -- Analytical View for BI Dashboards & Visualization Tools (Metabase, Grafana, Tableau)
                CREATE OR REPLACE VIEW vw_rera_projects_analytics AS
                SELECT 
                    registration_no,
                    acknowledgement_no,
                    COALESCE(project_name, data->'project_overview_details'->>'Project Name') AS project_name,
                    COALESCE(firm_name, data->'promoter_details'->'Promoter Details'->>'Promoter Name') AS promoter_name,
                    COALESCE(data->>'status', data->'project_overview_details'->>'Project Status') AS status,
                    data->'project_overview_details'->>'Project Sub Type' AS project_sub_type,
                    data->'project_location'->>'district' AS district,
                    data->'project_location'->>'taluk' AS taluk,
                    data->'project_location'->>'pin_code' AS pin_code,
                    data->'project_location'->>'address' AS address,
                    NULLIF(data->'project_location'->>'latitude', '')::numeric AS latitude,
                    NULLIF(data->'project_location'->>'longitude', '')::numeric AS longitude,
                    data->'project_location'->>'google_maps_url' AS google_maps_url,
                    data->'project_location'->'boundaries'->>'north_schedule' AS north_schedule,
                    data->'project_location'->'boundaries'->>'south_schedule' AS south_schedule,
                    data->'project_location'->'boundaries'->>'east_schedule' AS east_schedule,
                    data->'project_location'->'boundaries'->>'west_schedule' AS west_schedule,
                    data->'project_overview_details'->>'Approving Authority' AS approving_authority,
                    data->'project_overview_details'->>'Approved Plan Number' AS approved_plan_number,
                    data->'project_overview_details'->>'Plan Approval Date' AS plan_approval_date,
                    NULLIF(data->'project_overview_details'->>'Total Area Of Land (Sq Mtr) (A1+A2)', '')::numeric AS total_land_area_sq_mtr,
                    NULLIF(data->'project_overview_details'->>'Total Built-up Area of all the Floors (Sq Mtr)', '')::numeric AS total_builtup_area_sq_mtr,
                    NULLIF(data->'project_overview_details'->>'Total Carpet Area of all the Floors (Sq Mtr)', '')::numeric AS total_carpet_area_sq_mtr,
                    NULLIF(data->'project_overview_details'->>'Total Number of Inventories/Flats/Villas', '')::integer AS total_units,
                    NULLIF(data->'project_overview_details'->>'Total Project Cost (INR) (C1+C2)', '')::numeric AS total_project_cost_inr,
                    data->>'approved_on' AS approved_on,
                    data->>'proposed_completion_date' AS proposed_completion_date,
                    data->>'certificate_url' AS certificate_url,
                    (data->'data_quality'->>'quality_score')::integer AS data_quality_score,
                    (data->'data_quality'->>'is_complete')::boolean AS is_complete,
                    crawled_at
                FROM rera_projects;

                -- Materialized views for LATERAL-unnest queries (vw_rera_documents, vw_rera_surveys)
                -- Plain views re-execute the unnest on every query — expensive at scale.
                -- These are refreshed once per crawl batch via refresh_materialized_views().
                CREATE MATERIALIZED VIEW IF NOT EXISTS vw_rera_documents AS
                SELECT 
                    p.registration_no,
                    p.project_name,
                    doc->>'document_type' AS document_type,
                    doc->>'financial_year' AS financial_year_or_context,
                    doc->>'file' AS file_name,
                    file_url.value AS file_url
                FROM rera_projects p,
                LATERAL jsonb_array_elements(p.data->'uploaded_documents') doc,
                LATERAL jsonb_array_elements_text(doc->'file_url') file_url(value)
                WHERE doc->'file_url' IS NOT NULL
                  AND jsonb_typeof(doc->'file_url') = 'array'
                WITH NO DATA;

                CREATE MATERIALIZED VIEW IF NOT EXISTS vw_rera_surveys AS
                SELECT 
                    p.registration_no,
                    p.project_name,
                    p.data->'project_location'->>'district' AS district,
                    p.data->'project_location'->>'taluk' AS taluk,
                    s.survey_no
                FROM rera_projects p,
                LATERAL jsonb_array_elements_text(p.data->'project_location'->'survey_numbers') s(survey_no)
                WITH NO DATA;

                -- Unique indexes required for REFRESH MATERIALIZED VIEW CONCURRENTLY
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vw_rera_documents_uniq
                    ON vw_rera_documents (registration_no, document_type, financial_year_or_context, file_url);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vw_rera_surveys_uniq
                    ON vw_rera_surveys (registration_no, survey_no);

                -- Initial populate: plain REFRESH (not CONCURRENTLY) is required here because
                -- CONCURRENTLY cannot run inside a multi-statement transaction block.
                -- All subsequent refreshes via refresh_materialized_views() use CONCURRENTLY.
                REFRESH MATERIALIZED VIEW vw_rera_documents;
                REFRESH MATERIALIZED VIEW vw_rera_surveys;
            """)

    def upsert_record(self, record: dict):
        reg_no = record.get("registration_no") or record.get("_registration_no") or record.get("registration_number")
        ack_no = record.get("acknowledgement_no") or record.get("_acknowledgement_no") or record.get("acknowledgement_number")
        project_name = record.get("project_name")
        firm_name = record.get("promoter_name") or record.get("_firm_name_from_listing")
        project_id = record.get("internal_project_id") or record.get("_project_id")
        crawled_at = record.get("crawled_at") or record.get("_crawled_at")


        with self._db_lock:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO rera_projects (registration_no, acknowledgement_no, project_name, firm_name, project_id, crawled_at, data)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (registration_no) DO UPDATE SET
                        acknowledgement_no = EXCLUDED.acknowledgement_no,
                        project_name = EXCLUDED.project_name,
                        firm_name = EXCLUDED.firm_name,
                        project_id = EXCLUDED.project_id,
                        crawled_at = EXCLUDED.crawled_at,
                        data = EXCLUDED.data;
                """, (reg_no, ack_no, project_name, firm_name, project_id, crawled_at, json.dumps(record, ensure_ascii=False)))

    def refresh_materialized_views(self):
        """Refresh LATERAL-unnest materialized views without blocking concurrent reads.

        Uses CONCURRENTLY so BI dashboards (Metabase/Grafana) querying vw_rera_documents
        or vw_rera_surveys are never blocked. Requires the unique indexes created in
        _init_db() (idx_vw_rera_documents_uniq, idx_vw_rera_surveys_uniq).
        """
        with self._db_lock:
            with self.conn.cursor() as cur:
                for view in ("vw_rera_documents", "vw_rera_surveys"):
                    print(f"[*] Refreshing materialized view concurrently: {view}...")
                    cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view};")
        print("[+] Materialized views refreshed.")


class CrawlState:

    """Manages crash-safe JSONL storage, progress tracking, and state resumption.

    Thread-safe: a single CrawlState instance is shared across worker threads
    in continuous_crawl, so every mutation is guarded by self._lock.

    done_regnos.json / failed_regnos.json are NOT rewritten on every single
    project anymore (that was tens of thousands of full-file rewrites on a
    large crawl). Instead they're flushed every `save_every` completions,
    plus always on program exit / Ctrl+C via flush(). project_details.jsonl
    remains the authoritative crash-safe record: it's appended immediately
    and is never rewritten, so no progress is lost between flushes even if
    the process is killed - at worst a few already-done reg_nos get re-tried
    on the next --all run, which is cheap (they just fail the "already in
    jsonl" dedupe... note: dedupe is by done_regnos, so keep save_every modest).
    """
    def __init__(self, output_dir: Path, save_every: int = 50):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = output_dir / "project_details.jsonl"
        self.done_path = output_dir / "done_regnos.json"
        self.failed_path = output_dir / "failed_regnos.json"
        self.done_regnos = set()
        self.failed = {}
        self.save_every = max(1, save_every)
        self._dirty_count = 0
        self._lock = threading.Lock()

    def load(self, resume: bool):
        if resume and self.done_path.exists():
            self.done_regnos = set(json.loads(self.done_path.read_text(encoding="utf-8")))
        if resume and self.failed_path.exists():
            self.failed = json.loads(self.failed_path.read_text(encoding="utf-8"))
        if not resume:
            self.done_regnos = set()

    def mark_done(self, reg_no: str):
        with self._lock:
            self.done_regnos.add(reg_no)
            self.failed.pop(reg_no, None)
            self._maybe_save()

    def mark_failed(self, reg_no: str, error: str):
        with self._lock:
            self.failed[reg_no] = error
            self._maybe_save()

    def _maybe_save(self):
        """Caller must hold self._lock. Writes done/failed to disk only every
        `save_every` calls, to avoid rewriting the whole JSON file per project."""
        self._dirty_count += 1
        if self._dirty_count >= self.save_every:
            self._save_locked()
            self._dirty_count = 0

    def _save_locked(self):
        self.done_path.write_text(json.dumps(sorted(self.done_regnos), indent=2), encoding="utf-8")
        self.failed_path.write_text(json.dumps(self.failed, indent=2), encoding="utf-8")

    def flush(self):
        """Force-write done/failed state regardless of the batch counter.
        Call this at the end of a crawl and from the Ctrl+C handler so the
        last partial batch is never silently lost."""
        with self._lock:
            self._save_locked()
            self._dirty_count = 0

    def append_record(self, record: dict):
        # JSONL append is the crash-safe source of truth, so it's still
        # flushed to disk immediately for every record (this is a cheap
        # append, unlike the done/failed full-file rewrites above).
        with self._lock:
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


def rebuild_csv(state: CrawlState, filename: str = "project_table.csv"):
    """Rebuild project_table.csv from project_details.jsonl using union of all keys."""
    if not state.jsonl_path.exists():
        print("[!] No project_details.jsonl found - nothing to build CSV from.", file=sys.stderr)
        return

    rows = []
    fieldnames = []
    seen_fields = set()

    with state.jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            flat = flatten_for_csv(record)
            rows.append(flat)
            for k in flat.keys():
                if k not in seen_fields:
                    seen_fields.add(k)
                    fieldnames.append(k)

    csv_path = state.output_dir / filename
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)

    print(f"[+] Rebuilt CSV ({csv_path.resolve()}): {len(rows)} projects, {len(fieldnames)} columns.")


# --------------------------------------------------------------------------- #
# Main Continuous Crawl Loop
# --------------------------------------------------------------------------- #

def _crawl_one_project(proj: dict, args, state: "CrawlState", pg_db) -> tuple[str, str, str | None]:
    """Runs in a worker thread. Fetches, parses, and persists ONE project.
    Returns (status, reg_no, error_message_or_None). Never raises."""
    reg_no = proj["registration_no"]
    name = proj["project_name"]
    try:
        s, hidden_fields = get_thread_session()  # one session per worker thread, reused across its projects

        search_html = search_projects(s, hidden_fields, reg_no=reg_no)
        project_id, candidates = resolve_project_id(search_html, reg_no=reg_no, project_name=name)
        search_listing_details = parse_search_result_row(search_html, reg_no=reg_no, project_name=name)

        if not project_id:
            raise CrawlerError(f"Could not resolve project_id for reg_no={reg_no}")

        html_text = fetch_project_detail_html(s, project_id)
        raw_data = normalize_project_detail(html_text)

        record = build_clean_structured_record(
            raw_data=raw_data,
            reg_no=reg_no,
            project_name=name,
            acknowledgement_no=proj["acknowledgement_no"],
            promoter_name=proj["firm_name"],
            project_id=project_id,
            search_listing_details=search_listing_details
        )

        state.append_record(record)
        if pg_db:
            try:
                pg_db.upsert_record(record)
            except Exception as db_err:
                state.mark_failed(reg_no, f"DB upsert error: {db_err}")
                return ("db_error", reg_no, str(db_err))

        state.mark_done(reg_no)
        return ("done", reg_no, None)

    except CrawlerError as exc:
        state.mark_failed(reg_no, str(exc))
        return ("crawler_error", reg_no, str(exc))
    except Exception as exc:
        state.mark_failed(reg_no, f"unexpected: {exc}")
        return ("error", reg_no, str(exc))
    finally:
        # Per-worker pacing: with N workers each sleeping `delay` seconds,
        # the *effective* site-wide request rate is ~N / delay req/sec -
        # tune --workers and --delay together rather than relying on delay alone.
        time.sleep(args.delay)


def continuous_crawl(args):
    output_dir = Path(args.output_dir)
    state = CrawlState(output_dir, save_every=args.state_save_every)
    state.load(resume=not args.no_resume)

    db_uri = args.db_uri or os.getenv("DATABASE_URL")
    pg_db = None
    if db_uri:
        print(f"[*] Initializing PostgreSQL connection...")
        pg_db = PostgresStorage(db_uri)

    # Handle Ctrl+C gracefully. Note: this fires on the main thread; in-flight
    # worker requests are not force-cancelled, they just won't be waited on.
    def _handle_sigint(signum, frame):
        print("\n[!] Interrupted - flushing state, rebuilding CSV, refreshing views...")
        state.flush()
        rebuild_csv(state)
        if pg_db:
            pg_db.refresh_materialized_views()
        os._exit(130)  # hard exit: don't wait on the executor's worker threads
    signal.signal(signal.SIGINT, _handle_sigint)

    # Discovery uses its own one-off session; workers each build their own
    # session lazily via get_thread_session().
    s, _hidden_fields = new_session()

    save_discovery_dir = Path(args.save_listing_pages) if args.save_listing_pages else None
    print("[*] Discovering top-level property records from /viewAllProjects...")
    projects = discover_projects(s, save_discovery_dir)
    print(f"[*] Discovered {len(projects)} total projects.")

    if args.limit:
        projects = projects[: args.limit]
        print(f"[*] --limit set: processing first {len(projects)} projects.")

    todo = [p for p in projects if p["registration_no"] not in state.done_regnos]
    skipped = len(projects) - len(todo)
    if skipped:
        print(f"[*] Skipping {skipped} already-crawled project(s) (resuming).")

    total = len(todo)
    completed = 0
    print(f"[*] Crawling {total} project(s) with {args.workers} worker(s), "
          f"{args.delay}s delay per worker...")

    # NOTE on CSV: we deliberately do NOT rebuild project_table.csv during
    # the loop anymore. JSONL (appended immediately, per record) and
    # PostgreSQL (upserted immediately, per record) are the crash-safe
    # primary stores during a bulk crawl. CSV is a derived view, rebuilt
    # once at the end here, on Ctrl+C above, or on demand via
    # `--rebuild-csv-only` - rebuilding it every 25-500 records was reading
    # + rewriting the *entire* growing JSONL/CSV on every interval for no
    # benefit during the crawl itself.
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_crawl_one_project, proj, args, state, pg_db): proj for proj in todo}
        for future in as_completed(futures):
            proj = futures[future]
            reg_no = proj["registration_no"]
            name = proj["project_name"]
            completed += 1
            try:
                status, _reg_no, err = future.result()
            except Exception as exc:  # should not happen - _crawl_one_project never raises
                status, err = "error", str(exc)

            if status == "done":
                print(f"[*] ({completed}/{total}) OK: {name!r} ({reg_no})")
            else:
                print(f"[!] ({completed}/{total}) {status} for {name!r} ({reg_no}): {err}", file=sys.stderr)

    state.flush()  # make sure the last partial batch of done/failed reg_nos hits disk
    rebuild_csv(state)
    if pg_db:
        pg_db.refresh_materialized_views()
    print(f"\n[+] Crawl complete! {len(state.done_regnos)} properties saved to {state.output_dir.resolve()}")

    if args.auto_shutdown:
        print("[*] Continuous crawl complete and --auto-shutdown specified. Initiating EC2 shutdown...")
        os.system("sudo shutdown -h now")


# --------------------------------------------------------------------------- #
# CLI Entry Point
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Unified RERA Karnataka property crawler (top-level discovery + deep v9 detailed extraction)"
    )
    # Continuous batch loop mode arguments
    parser.add_argument("--all", action="store_true", help="Run continuous loop over all discovered properties")
    parser.add_argument("--output-dir", default="rera_data", help="Output directory for jsonl/csv/state files (default: ./rera_data)")
    parser.add_argument("--workers", type=int, default=3,
                         help="Number of concurrent crawl workers (default: 3). Start at 3-5; "
                              "this is a government site making 2 requests/project, so don't jump straight to 20-50.")
    parser.add_argument("--delay", type=float, default=1.0,
                         help="Delay in seconds each WORKER sleeps between its own requests (default: 1.0). "
                              "This is per-worker, not global - effective rate scales with --workers too.")
    parser.add_argument("--state-save-every", type=int, default=50,
                         help="Rewrite done_regnos.json/failed_regnos.json every N completed projects "
                              "instead of after every single one (default: 50). project_details.jsonl is "
                              "still appended immediately regardless, so no crawl data is at risk.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of properties to crawl")
    parser.add_argument("--no-resume", action="store_true", help="Ignore done_regnos.json and re-crawl everything")
    parser.add_argument("--save-listing-pages", default=None, help="Directory to save raw discovery page HTML")
    parser.add_argument("--rebuild-csv-only", action="store_true", help="Rebuild CSV from existing project_details.jsonl without crawling")
    parser.add_argument("--db-uri", default=None, help="PostgreSQL connection string (or set DATABASE_URL env var)")
    parser.add_argument("--auto-shutdown", action="store_true", help="Automatically trigger 'sudo shutdown -h now' when batch completes (for Cloud EC2 pipelines)")


    # Single-property execution mode arguments
    parser.add_argument("--project-id", help="Directly fetch detailed info for known numeric PROJECT_ID")
    parser.add_argument("--reg-no", help="Search and extract detailed info for a single registration number")
    parser.add_argument("--project", default="", help="Single project name filter")
    parser.add_argument("--district", default="", help="District search filter")
    parser.add_argument("--subdistrict", default="", help="Sub-district search filter")
    parser.add_argument("--save-raw-html", help="Save raw project detail HTML to file")
    parser.add_argument("--from-html", help="Parse saved HTML file offline without network calls")

    args = parser.parse_args()

    if args.rebuild_csv_only:
        state = CrawlState(Path(args.output_dir))
        rebuild_csv(state)
        return

    if args.all:
        continuous_crawl(args)
        return

    # Single-property or offline mode
    if args.from_html:
        html_text = Path(args.from_html).read_text(encoding="utf-8", errors="ignore")
        raw_data = normalize_project_detail(html_text)
        record = build_clean_structured_record(raw_data=raw_data, reg_no="", project_name="")
        _write_single_outputs(record)
        return

    try:
        s, hidden_fields = new_session()
        project_id = args.project_id

        search_listing_details = {}
        if not project_id:
            print("[*] Searching rera.karnataka.gov.in...")
            search_html = search_projects(
                s, hidden_fields,
                project=args.project,
                reg_no=args.reg_no or "",
                district=args.district,
                subdistrict=args.subdistrict,
            )
            project_id, candidates = resolve_project_id(
                search_html, reg_no=args.reg_no or "", project_name=args.project
            )
            search_listing_details = parse_search_result_row(
                search_html, reg_no=args.reg_no or "", project_name=args.project
            )
            if not project_id:
                print("[!] Could not resolve PROJECT_ID. Supply --project-id directly.", file=sys.stderr)
                sys.exit(2)
            print(f"[*] Resolved PROJECT_ID: {project_id}")

        print(f"[*] Fetching project detail page for PROJECT_ID={project_id}...")
        html_text = fetch_project_detail_html(s, project_id)

        if args.save_raw_html:
            Path(args.save_raw_html).write_text(html_text, encoding="utf-8")
            print(f"[*] Raw HTML saved to: {Path(args.save_raw_html).resolve()}")

        raw_data = normalize_project_detail(html_text)
        record = build_clean_structured_record(
            raw_data=raw_data,
            reg_no=args.reg_no or "",
            project_name=args.project or "",
            project_id=project_id,
            search_listing_details=search_listing_details
        )
        _write_single_outputs(record)



    except CrawlerError as exc:
        print(f"\n[!] Crawler error: {exc}\n", file=sys.stderr)
        sys.exit(1)


def _write_single_outputs(data: dict):
    json_output = Path("project.json")
    csv_output = Path("project_table.csv")

    json_output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    write_single_csv(flatten_for_csv(data), csv_output)

    print(f"\n[+] Success! JSON saved to: {json_output.resolve()}")
    print(f"[+] Flattened CSV saved to: {csv_output.resolve()}\n")
    print(json.dumps(data, indent=2, ensure_ascii=False)[:2000], "...\n")


if __name__ == "__main__":
    main()