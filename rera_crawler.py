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

    matches = _DISCOVERY_PATTERN.findall(html_text)
    if not matches:
        raise CrawlerError(
            "Discovery found 0 projects in /viewAllProjects page. Markup pattern may have changed."
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
                        row_data[f"{clean_header}_url"] = links[0] if len(links) == 1 else links
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
                current_record[f"{label} URL"] = (
                    value_urls[0] if len(value_urls) == 1 else value_urls
                )

    flush()

    result = {}
    for section, records in records_by_section.items():
        result[section] = records[0] if len(records) == 1 else records
    return result


def parse_tables(container) -> list[dict]:
    """Parse html tables into rows of dicts, including column URLs."""
    all_tables = []
    for table in container.find_all("table"):
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        rows = []
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            values = [td.get_text(" ", strip=True) for td in tds]
            links = [extract_cell_links(td) for td in tds]

            if headers and len(headers) == len(values):
                row = dict(zip(headers, values))
                for header, url_list in zip(headers, links):
                    if url_list:
                        row[f"{header} URL"] = url_list[0] if len(url_list) == 1 else url_list
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
                        entry["file_url"] = urls[0] if len(urls) == 1 else urls
                    out.append(entry)
    return out


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
    print(f"[!] Warning: could not find <div id=\"{tab_id}\"> for '{tab_key}'", file=sys.stderr)


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

    # Promoter Details & Project Details tabs
    for key in ("promoter_details", "project_details"):
        div = soup.find("div", id=TAB_IDS[key])
        if div is None:
            _warn_missing(key, TAB_IDS[key])
            result[key] = {}
        else:
            result[key] = parse_label_value_sections(div)

    # Uploaded Documents tab
    div = soup.find("div", id=TAB_IDS["uploaded_documents"])
    if div is None:
        _warn_missing("uploaded_documents", TAB_IDS["uploaded_documents"])
        result["uploaded_documents"] = []
    else:
        result["uploaded_documents"] = parse_documents_matrix(div)

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
    def __init__(self, db_uri: str):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise CrawlerError("psycopg2-binary package is required for PostgreSQL support. Install with: pip install psycopg2-binary")
        
        self.db_uri = db_uri
        self.conn = psycopg2.connect(db_uri)
        self.conn.autocommit = True
        self._init_db()

    def _init_db(self):
        with self.conn.cursor() as cur:
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
            """)

    def upsert_record(self, record: dict):
        reg_no = record.get("_registration_no") or record.get("registration_number")
        ack_no = record.get("_acknowledgement_no") or record.get("acknowledgement_number")
        project_name = record.get("project_name")
        firm_name = record.get("_firm_name_from_listing")
        project_id = record.get("_project_id")
        crawled_at = record.get("_crawled_at")

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


class CrawlState:

    """Manages crash-safe JSONL storage, progress tracking, and state resumption."""
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = output_dir / "project_details.jsonl"
        self.done_path = output_dir / "done_regnos.json"
        self.failed_path = output_dir / "failed_regnos.json"
        self.done_regnos = set()
        self.failed = {}

    def load(self, resume: bool):
        if resume and self.done_path.exists():
            self.done_regnos = set(json.loads(self.done_path.read_text(encoding="utf-8")))
        if resume and self.failed_path.exists():
            self.failed = json.loads(self.failed_path.read_text(encoding="utf-8"))
        if not resume:
            self.done_regnos = set()

    def mark_done(self, reg_no: str):
        self.done_regnos.add(reg_no)
        self.failed.pop(reg_no, None)
        self._save()

    def mark_failed(self, reg_no: str, error: str):
        self.failed[reg_no] = error
        self._save()

    def _save(self):
        self.done_path.write_text(json.dumps(sorted(self.done_regnos), indent=2), encoding="utf-8")
        self.failed_path.write_text(json.dumps(self.failed, indent=2), encoding="utf-8")

    def append_record(self, record: dict):
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

def continuous_crawl(args):
    output_dir = Path(args.output_dir)
    state = CrawlState(output_dir)
    state.load(resume=not args.no_resume)

    db_uri = args.db_uri or os.getenv("DATABASE_URL")
    pg_db = None
    if db_uri:
        print(f"[*] Initializing PostgreSQL connection...")
        pg_db = PostgresStorage(db_uri)

    # Handle Ctrl+C gracefully
    def _handle_sigint(signum, frame):
        print("\n[!] Interrupted - rebuilding CSV from crawled data so far...")
        rebuild_csv(state)
        sys.exit(130)
    signal.signal(signal.SIGINT, _handle_sigint)

    s, hidden_fields = new_session()

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

    new_since_rebuild = 0
    for i, proj in enumerate(todo, 1):
        reg_no = proj["registration_no"]
        name = proj["project_name"]
        print(f"[*] ({i}/{len(todo)}) Crawling detailed info for: {name!r} ({reg_no})...")

        try:
            search_html = search_projects(s, hidden_fields, reg_no=reg_no)
            project_id, candidates = resolve_project_id(search_html, reg_no=reg_no, project_name=name)
            search_listing_details = parse_search_result_row(search_html, reg_no=reg_no, project_name=name)

            if not project_id:
                raise CrawlerError(f"Could not resolve project_id for reg_no={reg_no}")

            html_text = fetch_project_detail_html(s, project_id)
            data = normalize_project_detail(html_text)

            # Attach search result table data (status, district, taluk, dates, certificate URLs)
            data["search_listing_details"] = search_listing_details

            # Attach top-level metadata from discovery
            data["_project_id"] = project_id
            data["_registration_no"] = reg_no
            data["_acknowledgement_no"] = proj["acknowledgement_no"]
            data["_firm_name_from_listing"] = proj["firm_name"]
            data["_crawled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


            state.append_record(data)
            if pg_db:
                try:
                    pg_db.upsert_record(data)
                except Exception as db_err:
                    print(f"[!] DB upsert error for {reg_no}: {db_err}", file=sys.stderr)
            state.mark_done(reg_no)

        except CrawlerError as exc:
            print(f"[!] {reg_no} crawler error: {exc}", file=sys.stderr)
            state.mark_failed(reg_no, str(exc))
        except Exception as exc:
            print(f"[!] {reg_no} unexpected error: {exc}", file=sys.stderr)
            state.mark_failed(reg_no, f"unexpected: {exc}")

        new_since_rebuild += 1
        if new_since_rebuild >= REBUILD_EVERY:
            rebuild_csv(state)
            new_since_rebuild = 0

        time.sleep(args.delay)

    rebuild_csv(state)
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
    parser.add_argument("--delay", type=float, default=2.0, help="Delay in seconds between requests (default: 2.0)")
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
        data = normalize_project_detail(html_text)
        _write_single_outputs(data)
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

        data = normalize_project_detail(html_text)
        if search_listing_details:
            data["search_listing_details"] = search_listing_details
        _write_single_outputs(data)


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