# RERA Karnataka Unified Project Detail Crawler (v9)

A continuous, crash-safe Python web crawler designed to scrape top-level discovery data and deep multi-tab project details from the [RERA Karnataka Portal](https://rera.karnataka.gov.in).

---

## Features

- **Top-Level Discovery**: Automatically extracts summary listings (*Acknowledgement No*, *Registration No*, *Project Name*, and *Firm Name*) from `/viewAllProjects`.
- **Deep Multi-Tab Extraction**: Parses complete detailed pages (`/projectDetails`), capturing:
  - **Header Info**: Project Name, Registration Number, Acknowledgement Number.
  - **Promoter Details**: Registered promoter/firm information.
  - **Project Details**: Location, address, project type, and status details.
  - **Uploaded Documents**: Transposed financial-year matrix mapping document types to download URLs.
  - **Enquired Documents**: Tables with embedded file download URLs.
  - **Quarterly Updates & Completion Details**: Construction progress, building counts, and embedded image/document URLs.
  - **Complaints**: Complaints against promoter and complaints against project.
- **Crash-Safe & Resumable Loop**:
  - Appends records incrementally to `project_details.jsonl`.
  - Tracks completed registrations in `done_regnos.json` and failed attempts in `failed_regnos.json`.
  - Resumes automatically from where it left off if interrupted (`Ctrl+C`).
  - Periodically flattens all collected data into `project_table.csv`.
- **Direct PostgreSQL Database Upsert**: Supports saving/upserting scraped project records directly into PostgreSQL (`JSONB` + indexed columns) stored on the EC2 instance's **EBS volume** (or remote DB) via `--db-uri` or `DATABASE_URL`.
- **Automated Cloud Pipeline & Self-Shutdown**: Includes `--auto-shutdown` flag, setup script `setup_local_postgres_ec2.sh`, and `systemd` unit (`rera-crawler.service`) to automatically start PostgreSQL on boot, run the crawler, and power off EC2 upon job completion (keeping weekday compute costs at ₹0).

- **Multiple Execution Modes**:
  - Bulk continuous crawl over all properties.
  - Single-property lookup by Registration Number or `PROJECT_ID`.
  - Offline re-parsing of saved raw HTML files without network requests.
  - Standalone CSV rebuilding from existing JSONL data.


---

## Installation

### Prerequisites

- Python 3.10 or higher

### Install Dependencies

```bash
pip install -r requirements.txt
```

---

## EC2 Deployment & Foreground Execution Quick Reference

### 1. EC2 One-Command Setup
```bash
chmod +x setup_local_postgres_ec2.sh
./setup_local_postgres_ec2.sh
```

### 2. Run Crawler in Foreground (PostgreSQL DB Enabled)
```bash
DATABASE_URL="postgresql://rera_user:rera_password@localhost:5432/rera_db" /home/ubuntu/rera_crawler/venv/bin/python3 rera_crawler.py --all
```

Run with limit (e.g. 10 properties test run):
```bash
DATABASE_URL="postgresql://rera_user:rera_password@localhost:5432/rera_db" /home/ubuntu/rera_crawler/venv/bin/python3 rera_crawler.py --all --limit 10
```

Retry ONLY previously failed properties (recorded in `failed_regnos.json`):
```bash
DATABASE_URL="postgresql://rera_user:rera_password@localhost:5432/rera_db" /home/ubuntu/rera_crawler/venv/bin/python3 rera_crawler.py --all --retry-failed
```

### 3. Run Crawler in Background (`tmux` session)
```bash
tmux new -s crawler
DATABASE_URL="postgresql://rera_user:rera_password@localhost:5432/rera_db" /home/ubuntu/rera_crawler/venv/bin/python3 rera_crawler.py --all
# Detach with: Ctrl+B then D
# Re-attach anytime with: tmux attach -t crawler
```

---

## Usage Examples

### 1. Bulk / Continuous Crawl

Crawl all discovered properties in continuous, resumable mode:

```bash
python rera_crawler.py --all
```

Crawl the first 20 properties with a custom request delay (e.g., 1.5s) and output directory:

```bash
python rera_crawler.py --all --limit 20 --delay 1.5 --output-dir ./rera_data
```

### 2. Single Property Lookup

Lookup and extract detailed information for a single property by registration number:

```bash
python rera_crawler.py --reg-no PRM/KA/RERA/1251/310/PR/170915/000001
```

Lookup directly by numeric `PROJECT_ID`:

```bash
python rera_crawler.py --project-id 6
```

### 3. Offline Parsing

Parse a saved raw HTML file offline without network calls:

```bash
python rera_crawler.py --from-html raw_detail.html
```

Save raw HTML during a network fetch:

```bash
python rera_crawler.py --reg-no PRM/KA/RERA/1251/310/PR/170915/000001 --save-raw-html detail.html
```

### 4. Rebuild CSV Only

Rebuild `project_table.csv` from an existing `project_details.jsonl` file without making network requests:

```bash
python rera_crawler.py --rebuild-csv-only --output-dir ./rera_data
```

---

## Output Files

The crawler generates the following output files in the specified `--output-dir` (default: `./rera_data` or root for single mode):

| File | Description |
| --- | --- |
| `project_details.jsonl` | Append-only file containing full structured JSON records for each project. |
| `project_table.csv` | Unified CSV file flattest across all parsed fields and keys. |
| `done_regnos.json` | List of successfully crawled registration numbers (used for resuming). |
| `failed_regnos.json` | Map of failed registration numbers and their error reasons. |

---

## Command Line Arguments Reference

| Argument | Description |
| --- | --- |
| `--all` | Run continuous loop over all discovered properties (automatically skips already crawled properties and fetches newly added ones) |
| `--retry-failed` | Target and retry ONLY properties listed in `failed_regnos.json` |
| `--output-dir` | Directory to store JSONL, CSV, and state files (default: `rera_data`) |
| `--workers` | Number of concurrent worker threads (default: `3`) |
| `--delay` | Delay in seconds per worker between requests (default: `1.0`) |
| `--limit` | Maximum number of properties to process |
| `--no-resume` | Ignore state files and re-crawl everything from scratch |
| `--rebuild-csv-only` | Rebuild CSV from existing JSONL without network requests |
| `--db-uri` | PostgreSQL connection string (or set `DATABASE_URL` environment variable) |
| `--auto-shutdown` | Trigger automatic cloud shutdown (`sudo shutdown -h now`) upon completion |
| `--reg-no` | Single property search by Registration Number |
| `--project-id` | Direct fetch by numeric internal `PROJECT_ID` |
| `--from-html` | Parse a saved HTML file offline |
| `--save-raw-html` | Save raw project detail HTML to a specified path |
