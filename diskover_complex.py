import requests
import getpass
import csv
import heapq
import os
import re
import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import unquote
from bs4 import BeautifulSoup

BASE = "https://diskover.researchcomputing.nbi.ac.uk"
REQUEST_TIMEOUT = 30  # seconds — every session.get()/post() call in this file uses this

# Every request failure (timeout, connection error, non-200 status) anywhere
# in a real crawl run gets appended here instead of just printed, so main()
# can write a full list to timeouts.csv at the end — a single bad request no
# longer just scrolls off the top of a multi-hour run's console output.
# Plain list.append() is safe to call from multiple threads (the file-stats
# scans run concurrently) — CPython guarantees it's atomic.
FAILURE_LOG = []
FAILURE_CSV_FIELDNAMES = ["step", "reason", "area", "directory", "short_path", "child_path", "index", "path", "page", "query"]


def log(msg):
    """print() with an [HH:MM:SS] prefix — lets a long unattended crawl run
    (hours, with interleaved concurrent file-stats scans) show it's still
    making progress rather than looking stalled."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def log_failure(step, reason, **context):
    entry = {"step": step, "reason": reason, **context}
    FAILURE_LOG.append(entry)
    log(f"    ! {step} failed ({reason}): {context}")

# ── Output layout ──────────────────────────────────────────────────────────────
# Each run is written to runs/<year>/<month>/ so the whole folder can be
# rsync'd straight to the remote VM and picked up by import_csv.py.
RUNS_ROOT = "runs"


def make_run_dir(run_date=None):
    run_date = run_date or date.today()
    run_dir = os.path.join(RUNS_ROOT, f"{run_date.year:04d}", f"{run_date.month:02d}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir

# ── Known root paths derived from /etc/auto.jic ───────────────────────────────
# Key = index name with date suffix stripped (diskover-jic-apricot-primarydata-group_scratch)
# Value = filesystem root path for that index
INDEX_ROOT_PATHS = {
    # JIC HPC — Primary Data
    "diskover-jic-apricot-primarydata-group_scratch":       "/ifs/apricot/JIC/PrimaryData/GROUP_SCRATCH",
    "diskover-jic-apricot-primarydata-research-groups":     "/ifs/apricot/JIC/PrimaryData/RESEARCH-GROUPS",
    "diskover-jic-apricot-primarydata-research_projects":   "/ifs/apricot/JIC/PrimaryData/RESEARCH_PROJECTS",
    "diskover-jic-apricot-primarydata-projects_scratch":    "/ifs/apricot/JIC/PrimaryData/PROJECTS_SCRATCH",
    "diskover-jic-apricot-primarydata-platform_scratch":    "/ifs/apricot/JIC/PrimaryData/PLATFORM_SCRATCH",
    "diskover-jic-apricot-primarydata-platforms":           "/ifs/apricot/JIC/PrimaryData/PLATFORMS",
    "diskover-jic-apricot-primarydata-instruments":         "/ifs/apricot/JIC/PrimaryData/INSTRUMENTS",
    "diskover-jic-apricot-primarydata-informatics_common":  "/ifs/apricot/JIC/PrimaryData/INFORMATICS_COMMON",
    "diskover-jic-apricot-primarydata-hpc_software":        "/ifs/apricot/JIC/PrimaryData/HPC_SOFTWARE",
    "diskover-jic-apricot-primarydata-services":            "/ifs/apricot/JIC/PrimaryData/SERVICES",
    "diskover-jic-apricot-primarydata-user_homes":          "/ifs/apricot/JIC/PrimaryData/USER_HOMES",
    # JIC Archive (LoanStorage)
    "diskover-jic-apricot-loanstorage-archive-groups":      "/ifs/apricot/JIC/LoanStorage/ARCHIVE/Groups",
    "diskover-jic-apricot-loanstorage-archive-platforms":   "/ifs/apricot/JIC/LoanStorage/ARCHIVE/Platforms",
    "diskover-jic-apricot-loanstorage-archive-projects":    "/ifs/apricot/JIC/LoanStorage/ARCHIVE/Projects",
    # JIC Deep Archive (peach servers) — confirmed against Diskover's own
    # search query for this index (parent_path:/peach/deep-archive/groups);
    # the previous /mnt/deep-archive/Groups guess never matched anything,
    # so this area silently crawled zero directories every run.
    "diskover-jic-peach-deeparchive-groups":                "/peach/deep-archive/groups",
    "diskover-jic-peach-deeparchive-projects":              "/peach/deep-archive/projects",
    "diskover-jic-peach-deeparchive-platforms":             "/peach/deep-archive/platforms",
    # NBI
    "diskover-nbi-apricot-primarydata-reference_data":      "/ifs/apricot/NBI/PrimaryData/reference_data",
    # CryoEM / mango — path unknown, will be discovered automatically
    # "diskover-jic-mango": None,
}


def strip_date(index_name):
    """Remove trailing -YYYY-MM or -YYYY-MM-DD date suffix."""
    return re.sub(r'-\d{4}-\d{2}(-\d{2})?$', '', index_name)


def index_to_label(index_name):
    """Human-readable label from index name, e.g. 'JIC Apricot / group scratch'."""
    s = re.sub(r'^diskover-', '', strip_date(index_name))
    parts = s.split('-', 2)
    org  = '-'.join(p.upper() if len(p) <= 3 else p.title() for p in parts[:2])
    rest = parts[2].replace('-', ' ').replace('_', ' ').title() if len(parts) > 2 else ''
    return f"{org} / {rest}" if rest else org


def extract_index_month(index_name):
    """Return (year, month) from trailing index date suffix, else None."""
    m = re.search(r'-(\d{4})-(\d{2})(?:-\d{2})?$', index_name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def parse_target_month(month_text):
    """Parse YYYY-MM into (year, month)."""
    if not month_text:
        return None
    m = re.fullmatch(r'(\d{4})-(\d{2})', month_text.strip())
    if not m:
        raise ValueError("Month must be in YYYY-MM format, e.g. 2026-07")
    year = int(m.group(1))
    month = int(m.group(2))
    if month < 1 or month > 12:
        raise ValueError("Month must be between 01 and 12")
    return year, month


# ── helpers ────────────────────────────────────────────────────────────────────

def login(username, password):
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    r = s.post(f"{BASE}/login.php",
               data={"username": username, "password": password, "refer": "/"},
               allow_redirects=True, timeout=REQUEST_TIMEOUT)
    if "login.php" in r.url:
        raise Exception("Login failed")
    log("Logged in successfully.")
    return s


# Lucene query_string special characters (Diskover's search.php passes q=
# straight through to Elasticsearch's query parser) — any of these in a
# directory/leader name breaks the query silently rather than erroring, most
# commonly a bare space, which splits the query into two separate terms
# instead of one path (confirmed live: "Sarah-O'Connor/Thuy Dang" failed
# while every other, space-free Sarah-O'Connor subdirectory scanned fine).
_LUCENE_SPECIAL_RE = re.compile(r'([+\-!(){}\[\]^"~*?:\\/&|\s])')


def escape_path(path):
    return _LUCENE_SPECIAL_RE.sub(r'\\\1', path)


def bytes_to_human(raw_bytes):
    if raw_bytes >= 1e12:
        return f"{raw_bytes / 1e12:.2f} TB"
    elif raw_bytes >= 1e9:
        return f"{raw_bytes / 1e9:.1f} GB"
    elif raw_bytes >= 1e6:
        return f"{raw_bytes / 1e6:.0f} MB"
    else:
        return f"{raw_bytes:.0f} B"


def parse_size_to_bytes(size_text):
    m = re.match(r'([\d.]+)\s*(TB|GB|MB|KB|B|BYTE|BYTES)', size_text.strip(), re.IGNORECASE)
    if m:
        val, unit = float(m.group(1)), m.group(2).upper()
        return val * {"TB": 1e12, "GB": 1e9, "MB": 1e6, "KB": 1e3, "B": 1, "BYTE": 1, "BYTES": 1}[unit]
    return None


def clean_name(td):
    for s in td.stripped_strings:
        if any(x in s for x in ["directory info", "file info", "copy path",
                                 "find similar", "Add Power"]):
            break
        return s
    return ""


def clean_path(td):
    for s in td.stripped_strings:
        if s.startswith("/"):
            return s
    return ""


# ── group-area detection ────────────────────────────────────────────────────────
# Subdirectory crawling (one level deeper than the normal area listing) is only
# done for "Groups"-type areas, to avoid multiplying requests across every
# user-home folder. Mirrors GROUP_AREA_PATTERNS in app.py.
GROUP_AREA_HINTS = ("research groups", "group scratch", "archive groups", "groups")


def is_group_area_label(label):
    value = (label or "").lower()
    return any(hint in value for hint in GROUP_AREA_HINTS)


# Deep Archive's Projects/Platforms indices get the same per-subdirectory
# file-level scan as a Groups-type area, even though their labels don't
# match GROUP_AREA_HINTS ("Deeparchive Projects"/"Deeparchive Platforms")
# and every other area's Projects/Platforms never gets this treatment.
# Deliberately scoped to just these two index keys, not a general widening
# of is_group_area_label() — the whole point of crawling Deep Archive is to
# catalog what's sitting on it, which the Groups area already gets for free
# but Projects/Platforms wouldn't under the normal rule.
DEEP_ARCHIVE_FORCE_GROUP_INDEX_KEYS = {
    "diskover-jic-peach-deeparchive-projects",
    "diskover-jic-peach-deeparchive-platforms",
}


def is_forced_group_index(index_name):
    return strip_date(index_name) in DEEP_ARCHIVE_FORCE_GROUP_INDEX_KEYS


# ── index discovery ────────────────────────────────────────────────────────────

def get_available_indices(session):
    """
    Return [(label, index_name)] for each individual index the user has access to.
    Reads the 'index' session cookie set at login, then falls back to page scraping.
    """
    individual = set()

    # Primary: session cookie (set by Diskover at login)
    index_cookie = session.cookies.get('index', '')
    log(f"    (debug) raw 'index' cookie: {index_cookie!r}")
    if index_cookie:
        for idx in unquote(index_cookie).split(','):
            idx = idx.strip()
            if idx.startswith('diskover-'):
                individual.add(idx)

    # Fallback: scrape main page
    if not individual:
        log("    (debug) cookie had no usable indices — falling back to page scraping")
        try:
            r = session.get(f"{BASE}/", allow_redirects=False, timeout=REQUEST_TIMEOUT)
            if r.status_code == 302:
                loc = r.headers.get("Location", "")
                if loc:
                    url = loc if loc.startswith("http") else BASE + loc
                    r = session.get(url, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            log_failure("get_available_indices", str(exc))
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        raw = set()
        for a in soup.find_all("a", href=True):
            m = re.search(r'[?&]index=([^&"\']+)', a["href"])
            if m:
                raw.add(unquote(m.group(1)))
        for el in soup.find_all(attrs={"data-index": True}):
            raw.add(unquote(el["data-index"]))
        for script in soup.find_all("script"):
            for m in re.finditer(r'"(diskover-[^",\s]+)"', script.string or ""):
                raw.add(m.group(1))
        log(f"    (debug) raw candidates scraped from page: {sorted(raw)!r}")
        for blob in raw:
            for idx in blob.split(','):
                idx = idx.strip()
                if idx.startswith('diskover-'):
                    individual.add(idx)

    return [(index_to_label(idx), idx) for idx in sorted(individual, reverse=True)]


def get_index_crawl_status(session):
    """
    Fetch selectindices.php and return {index_name: finish_datetime_or_None}.

    Diskover registers/names an index (e.g. "...-group_scratch-2026-10") as
    soon as its OWN crawl of the filesystem starts, not once it finishes — so
    get_available_indices() discovering an index by name is no guarantee
    search.php is querying complete data. A None value here means that
    index's "Finish Time" column is blank (still crawling) or couldn't be
    parsed; an index missing from the returned dict entirely means it wasn't
    listed on this page at all. Either case should be treated as "can't
    confirm this is safe to scrape yet" by the caller.

    Returns None (not {}) if the page itself couldn't be fetched/parsed at
    all, so the caller can tell "fetched, nothing matched" apart from
    "couldn't check" — both should still be surfaced as a warning, per
    get_index_crawl_status()'s caller in main(), rather than silently
    assuming safe.
    """
    try:
        r = session.get(f"{BASE}/selectindices.php", timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        log_failure("get_index_crawl_status", str(exc))
        return None
    if r.status_code != 200:
        log_failure("get_index_crawl_status", f"HTTP {r.status_code}")
        return None

    # Temporary debug dump — every area came back "unconfirmed" on a real run
    # with no fetch error logged, suggesting the table is populated via a
    # separate AJAX call (common for DataTables-style admin pages) rather
    # than present in this plain GET's HTML. Saved unconditionally so it can
    # be inspected after the fact instead of needing to reproduce live.
    try:
        with open("selectindices_debug.html", "w", encoding="utf-8") as f:
            f.write(r.text)
        log(f"    (debug) saved raw selectindices.php response to selectindices_debug.html ({len(r.text)} bytes)")
    except OSError:
        pass

    soup = BeautifulSoup(r.text, "html.parser")
    # The page has (at least) two <table>s — a small "helptable" legend
    # comes first in page order, before the real results table — so this
    # must be found by its id, not just the first <table> on the page (that
    # pattern works fine for search.php etc. since those pages only have one).
    table = soup.find("table", id="indices-table")
    if not table:
        log_failure("get_index_crawl_status", "no <table id='indices-table'> found on selectindices.php")
        return None
    log(f"    (debug) table found with {len(table.find_all('tr'))} <tr> total, "
        f"{len(table.find_all('th'))} <th> headers")

    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    name_idx = next((i for i, h in enumerate(headers) if h == "Index Name"), 2)
    finish_idx = next((i for i, h in enumerate(headers) if h == "Finish Time"), 5)

    status = {}
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) <= max(name_idx, finish_idx):
            continue
        name = tds[name_idx].get_text(strip=True)
        if not name or not name.startswith("diskover-") or name == "diskover-latest":
            continue  # "diskover-latest" is a combined alias over every real
                       # index, not an index with its own start/finish time
        finish_text = tds[finish_idx].get_text(strip=True)
        finish_dt = None
        if finish_text:
            try:
                finish_dt = datetime.strptime(finish_text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                finish_dt = None  # unexpected format -- treat as unconfirmed, not as "finished"
        status[name] = finish_dt
    return status


def get_root_path(session, index_name):
    """
    Return the filesystem root path for this index.
    Uses the hardcoded map first; falls back to sampling search results.
    """
    key = strip_date(index_name)
    if key in INDEX_ROOT_PATHS:
        return INDEX_ROOT_PATHS[key]

    # Unknown index — sample search results to find common prefix
    log(f"    (root path unknown for {key}, auto-detecting …)")
    for query in ["type:directory", "size:>=0"]:
        try:
            r = session.get(f"{BASE}/search.php", params={
                "q": query, "submitted": "true", "p": 1,
                "resultsize": 25, "userinput": "true",
                "index": index_name,
            }, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            log_failure("get_root_path", str(exc), index=index_name, query=query)
            continue
        if r.status_code != 200:
            log_failure("get_root_path", f"HTTP {r.status_code}", index=index_name, query=query)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            continue
        headers  = [th.get_text(strip=True) for th in table.find_all("th")]
        path_idx = next((i for i, h in enumerate(headers) if h == "Path"), 3)
        paths = []
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if not tds:
                continue
            p = clean_path(tds[path_idx]) if path_idx < len(tds) else ""
            if p:
                paths.append(p)
        if not paths:
            continue
        prefix = paths[0]
        for p in paths[1:]:
            while prefix and not p.startswith(prefix):
                prefix = prefix.rsplit("/", 1)[0]
        if prefix and prefix != "/":
            return prefix

    return None


# ── directory listing ──────────────────────────────────────────────────────────

def list_dirs_at(session, parent_path, index_name):
    """
    Return [(name, full_path, size_str)] for directories directly under parent_path.

    Note: Diskover's 'Path' column shows the PARENT directory of each result,
    not the result's own path. So we construct full_path as parent_path/name
    and deduplicate by name (not by the path column).
    """
    escaped = escape_path(parent_path)
    query   = f"parent_path:{escaped}"
    dirs    = []
    seen    = set()   # deduplicate by name
    page    = 1

    while True:
        try:
            r = session.get(f"{BASE}/search.php", params={
                "q": query, "submitted": "true", "p": page,
                "resultsize": 100, "path": parent_path,
                "userinput": "true", "index": index_name,
            }, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            log_failure("list_dirs_at", str(exc), path=parent_path, index=index_name, page=page)
            break
        if r.status_code != 200:
            log_failure("list_dirs_at", f"HTTP {r.status_code}", path=parent_path, index=index_name, page=page)
            break

        soup  = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            break

        headers  = [th.get_text(strip=True) for th in table.find_all("th")]
        name_idx = next((i for i, h in enumerate(headers) if h == "Name"), 1)
        size_idx = next((i for i, h in enumerate(headers) if h == "Size"), 4)
        type_idx = next((i for i, h in enumerate(headers) if h == "Type"), None)

        rows_found = 0
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if not tds or any("No results" in td.get_text() for td in tds):
                continue
            if type_idx is not None and type_idx < len(tds):
                t = tds[type_idx].get_text(strip=True).lower()
                if t and t not in ("directory", "dir", "d"):
                    continue
            name = clean_name(tds[name_idx]) if name_idx < len(tds) else ""
            size = tds[size_idx].get_text(strip=True) if size_idx < len(tds) else ""
            if name and name not in seen:
                seen.add(name)
                full_path = f"{parent_path}/{name}"
                dirs.append((name, full_path, size))
                rows_found += 1

        if rows_found < 100:
            break
        page += 1

    return dirs


# Deep Archive's three root paths each hold a single numbered "shard"
# directory (e.g. /peach/deep-archive/groups/1) that transparently contains
# every real leader/group directory one level deeper — confirmed directly
# against the October run's own area CSVs: each root's only immediate child
# is named just a bare digit ("1"/"2"/"3") and its file/folder counts match
# the WHOLE area's totals exactly (e.g. Groups/1 has all 117,786,869 files
# Diskover reports for diskover-jic-peach-deeparchive-groups). Without this,
# the crawler treats that single numbered directory as if it were the only
# "leader" in the whole area, and never sees the real directories beneath it.
#
# Gated on the root PATH, not the index name — this is a fact about the
# filesystem layout, true regardless of whether it's being queried through
# its own dated per-area index or (in combined_mode) a single shared index
# covering every area, so path-based detection covers both correctly.
DEEP_ARCHIVE_ROOT_PREFIX = "/peach/deep-archive/"
DEEP_ARCHIVE_SHARD_DESCEND_MAX_DEPTH = 3  # safety cap, not expected to ever need more than 1


def resolve_deep_archive_shard_root(session, root, index_name):
    """
    For Deep Archive areas only: repeatedly descends into a root path while
    it has exactly one immediate child directory (a pass-through numbered
    shard), stopping once there's zero or more-than-one child — i.e. once
    real content is reached. A no-op for every other area.
    """
    if not root.startswith(DEEP_ARCHIVE_ROOT_PREFIX):
        return root

    current = root
    for _ in range(DEEP_ARCHIVE_SHARD_DESCEND_MAX_DEPTH):
        children = list_dirs_at(session, current, index_name)
        if len(children) != 1:
            break
        shard_name, shard_path, _ = children[0]
        log(f"  Deep Archive root has a single shard directory ('{shard_name}') — descending: {shard_path}")
        current = shard_path
    return current


# ── CSV export ─────────────────────────────────────────────────────────────────

def save_area_csv(session, label, root_path, index_name, outfile):
    keep = ["Name", "Path", "Size", "Allocated", "Date Modified",
            "Last Accessed", "Files", "Folders", "Owner", "Group", "Type"]
    escaped = escape_path(root_path)
    query   = f"parent_path:{escaped}"
    all_rows, headers, seen_names = [], None, set()
    page = 1

    while True:
        try:
            r = session.get(f"{BASE}/search.php", params={
                "q": query, "submitted": "true", "p": page,
                "resultsize": 100, "path": root_path,
                "userinput": "true", "index": index_name,
            }, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            log_failure("save_area_csv", str(exc), area=label, path=root_path, page=page)
            break
        if r.status_code != 200:
            log_failure("save_area_csv", f"HTTP {r.status_code}", area=label, path=root_path, page=page)
            break
        soup  = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            break
        if headers is None:
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
        rows_found = 0
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if not tds or any("No results" in td.get_text() for td in tds):
                continue
            row = []
            for i, td in enumerate(tds):
                if i == 1:
                    row.append(clean_name(td))
                elif i == 3:
                    full = clean_path(td)
                    if full and full.startswith(root_path + "/"):
                        row.append(f"{label}/{full[len(root_path)+1:]}")
                    else:
                        row.append(full)
                else:
                    row.append(td.get_text(strip=True))
            name = row[1] if len(row) > 1 else ""
            if name and name not in seen_names:
                seen_names.add(name)
                all_rows.append(row)
                rows_found += 1
        if rows_found < 100:
            break
        page += 1

    if not all_rows or headers is None:
        return
    idx = [i for i, h in enumerate(headers) if h in keep]
    with open(outfile, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([headers[i] for i in idx])
        for row in all_rows:
            w.writerow([row[i] if i < len(row) else "" for i in idx])
    log(f"    {len(all_rows)} rows → {outfile}")


# ── subdirectory listing (one level deeper, group areas only) ──────────────────

def save_subdirectory_csv(session, all_dirs, outfile, file_stats=True,
                           extension_outfile=None, top_files_outfile=None,
                           file_stats_workers=6):
    """
    For every group-leader directory in all_dirs, list its immediate children
    and write them to outfile.

    When file_stats is True (default), also recursively scans every file
    under each child directory once to compute a per-extension size
    breakdown and the 1000 biggest individual files, written to
    extension_outfile / top_files_outfile. See get_dir_file_stats().

    That per-child scan is entirely network-bound (waiting on Diskover, not
    CPU), so it runs file_stats_workers at a time via a thread pool — the
    scans themselves are read-only and independent of each other, only the
    CSV writes need to stay on the main thread (they happen as each scan
    completes, never concurrently, so no locking is needed).

    all_dirs: [(dirname, dirpath, label, short_path, index_name)] — same shape
    produced in the main() Step 2 loop.
    """
    fieldnames     = ["Area", "ParentDirectory", "ParentPath", "Name", "Path", "Size"]
    ext_fieldnames = ["Area", "ParentDirectory", "ParentPath", "Path", "Extension", "FileCount", "TotalSizeBytes"]
    top_fieldnames = ["Area", "ParentDirectory", "ParentPath", "Path", "FilePath", "Extension", "SizeBytes", "Mtime", "Rank"]
    rows_written = 0
    tasks = []  # (label, dirname, short_path, child_path, full_path, index_name)

    with open(outfile, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for dirname, dirpath, label, short_path, index_name in all_dirs:
            if not is_group_area_label(label) and not is_forced_group_index(index_name):
                continue
            children = list_dirs_at(session, dirpath, index_name)
            for name, full_path, size in children:
                child_path = f"{short_path}/{name}"
                w.writerow({
                    "Area": label,
                    "ParentDirectory": dirname,
                    "ParentPath": short_path,
                    "Name": name,
                    "Path": child_path,
                    "Size": size,
                })
                rows_written += 1
                if file_stats:
                    tasks.append((label, dirname, short_path, child_path, full_path, index_name))

    log(f"    {rows_written} subdirectory row(s) → {outfile}")

    if not file_stats:
        return

    log(f"    Scanning {len(tasks)} subdirectory(ies) for file stats "
          f"({file_stats_workers} at a time)…")

    with open(extension_outfile, "w", newline="") as ext_f, \
         open(top_files_outfile, "w", newline="") as top_f:
        ext_w = csv.DictWriter(ext_f, fieldnames=ext_fieldnames)
        ext_w.writeheader()
        top_w = csv.DictWriter(top_f, fieldnames=top_fieldnames)
        top_w.writeheader()

        with ThreadPoolExecutor(max_workers=file_stats_workers) as pool:
            # Must be taken before submit() — a worker can finish (and call
            # log_failure()) before the main thread reaches a snapshot taken
            # after submission, which would make failed_now below miss it.
            snapshot_len = len(FAILURE_LOG)
            future_to_task = {
                pool.submit(get_dir_file_stats, session, full_path, index_name,
                            label=label, dirname=dirname, short_path=short_path, child_path=child_path):
                    (label, dirname, short_path, child_path, full_path, index_name)
                for label, dirname, short_path, child_path, full_path, index_name in tasks
            }
            done = 0
            for future in as_completed(future_to_task):
                label, dirname, short_path, child_path, full_path, index_name = future_to_task[future]
                try:
                    extension_stats, top_files = future.result()
                except Exception as exc:
                    log(f"      {child_path}: scan failed ({exc}) — skipping")
                    extension_stats, top_files = [], []

                # get_dir_file_stats() returns whatever it collected before a
                # page failure, not an empty result — so a directory that
                # failed partway through still has non-empty (but incomplete)
                # extension_stats/top_files here. Writing that would silently
                # persist an undercounted breakdown as if it were complete,
                # and — since this directory will also be retried later via
                # --retry-failures, which appends rather than replacing —
                # each retry attempt would stack another (possibly still
                # partial) copy on top, exactly the duplicate-data bug fixed
                # in import_csv.py's _dedupe_*_rows(). Only write once this
                # scan is confirmed complete (no failure logged for it).
                failed_now = any(
                    e.get("path") == full_path and e.get("index") == index_name
                    for e in FAILURE_LOG[snapshot_len:]
                )
                if not failed_now:
                    for stat in extension_stats:
                        ext_w.writerow({
                            "Area": label, "ParentDirectory": dirname,
                            "ParentPath": short_path, "Path": child_path,
                            "Extension": stat["extension"], "FileCount": stat["file_count"],
                            "TotalSizeBytes": stat["total_size_bytes"],
                        })
                    for tf in top_files:
                        top_w.writerow({
                            "Area": label, "ParentDirectory": dirname,
                            "ParentPath": short_path, "Path": child_path,
                            "FilePath": tf["path"], "Extension": tf["extension"],
                            "SizeBytes": tf["size_bytes"], "Mtime": tf["mtime"], "Rank": tf["rank"],
                        })

                done += 1
                if failed_now:
                    log(f"    [{done}/{len(tasks)}] {child_path}: incomplete scan (see failures.csv) — "
                          f"not written, retry with --retry-failures")
                else:
                    log(f"    [{done}/{len(tasks)}] {child_path}: {len(extension_stats)} extension(s), "
                          f"{len(top_files)} file(s) in top list")


# ── per-file stats (extension breakdown + biggest files, group areas only) ─────

EXTENSION_MAX_LEN = 255  # must match dir_extension_stats/dir_top_files.extension in schema.sql


# Generic compression wrappers whose own suffix alone is nearly meaningless —
# "sample.fq.gz" and "logs.tar.gz" being both just "gz" is exactly why .gz
# was the single biggest, most ambiguous category (28.7 TB of everything
# lumped together). When one of these is the final suffix, pull in the
# suffix before it too, so "fq.gz"/"fastq.gz"/"fq1.gz"/"fq2.gz"/"tar.gz"/
# "vcf.gz"/etc. become their own classifiable extensions instead.
COMPRESSION_WRAPPER_EXTENSIONS = {"gz", "bz2", "xz", "zst"}


def _extension_of(name):
    """"report.txt" -> "txt"; "sample.fq.gz" -> "fq.gz"; "archive.gz" (no
    inner extension) -> "gz"; ".bashrc" (leading dot only) -> "(none)"."""
    if "." not in name[1:]:
        return "(none)"
    parts = name.split(".")
    last = parts[-1].lower()
    if last in COMPRESSION_WRAPPER_EXTENSIONS and len(parts) >= 3:
        return f"{parts[-2].lower()}.{last}"[:EXTENSION_MAX_LEN]
    return last[:EXTENSION_MAX_LEN]


FILE_STATS_RETRY_ATTEMPTS = 3  # total tries per page, including the first
FILE_STATS_RETRY_DELAY = 3     # seconds between retries


def _fetch_file_stats_page(session, full_path, index_name, query, page,
                            label=None, dirname=None, short_path=None, child_path=None):
    """
    Fetch and validate one page of get_dir_file_stats()'s scan, retrying the
    same page up to FILE_STATS_RETRY_ATTEMPTS times (a short delay between
    tries) before giving up. Absorbs transient blips — a brief server hiccup,
    a burst of concurrent requests landing badly together — that would
    otherwise show up as a permanent gap in a real, non-empty directory's
    data (confirmed live: several definitely-non-empty directories under the
    same leader failed back-to-back with no shared cause like a special
    character in the path, pointing at something transient rather than a
    real per-directory problem).

    Returns the parsed <table> BeautifulSoup element on success, or None if
    every attempt failed (the failure is already logged via log_failure()
    by the time this returns None).
    """
    last_reason = None
    for attempt in range(1, FILE_STATS_RETRY_ATTEMPTS + 1):
        try:
            r = session.get(f"{BASE}/search.php", params={
                "q": query, "submitted": "true", "p": page,
                "resultsize": 100, "path": full_path,
                "userinput": "true", "show_files": "1",
                "index": index_name,
            }, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            last_reason = str(exc)
        else:
            if r.status_code != 200:
                last_reason = f"HTTP {r.status_code}"
            else:
                soup = BeautifulSoup(r.text, "html.parser")
                table = soup.find("table")
                if table:
                    return table
                # A genuinely empty directory still renders a table with a
                # "No results" row (handled by the caller) — a completely
                # missing table is more likely a malformed response,
                # redirect, or session hiccup than a real empty directory.
                last_reason = f"no results table in response (page {page})"

        if attempt < FILE_STATS_RETRY_ATTEMPTS:
            time.sleep(FILE_STATS_RETRY_DELAY)

    log_failure("get_dir_file_stats", f"{last_reason} (after {FILE_STATS_RETRY_ATTEMPTS} attempts)",
                path=full_path, index=index_name, page=page,
                area=label, directory=dirname, short_path=short_path, child_path=child_path)
    return None


def get_dir_file_stats(session, full_path, index_name, top_n=1000,
                        label=None, dirname=None, short_path=None, child_path=None):
    """
    Recursively scan every file under full_path (one search.php pass, paginated
    the same way as save_area_csv/list_dirs_at) and return:
      - extension_stats: [{"extension", "file_count", "total_size_bytes"}, ...]
      - top_files: [{"path", "size_bytes", "extension", "mtime", "rank"}, ...],
        the top_n biggest files, descending by size.

    Diskover exposes no sort-by-size or size-aggregation-by-extension endpoint
    that this codebase has ever used (only unsorted listings + count-only
    pagination text), so both outputs are computed client-side from a single
    full listing rather than depending on an unverified API param.

    label/dirname/short_path/child_path aren't used for scanning — they're
    only threaded through to log_failure() so a failure here carries enough
    context (Area/ParentDirectory/ParentPath/Path) for --retry-failures to
    reconstruct a correct CSV row without needing the original crawl state.
    """
    escaped = escape_path(full_path)
    query   = f"parent_path:{escaped}* AND type:file"
    ext_counts = {}
    ext_sizes  = {}
    heap = []   # min-heap of (size_bytes, seq, entry) capped at top_n
    seq  = 0
    page = 1

    while True:
        table = _fetch_file_stats_page(session, full_path, index_name, query, page,
                                        label, dirname, short_path, child_path)
        if table is None:
            log(f"      (page {page} failed after {FILE_STATS_RETRY_ATTEMPTS} attempts for "
                  f"{full_path} — stopping scan, keeping what was collected so far)")
            break

        if page == 1 or page % 20 == 0:
            log(f"      …scanning {full_path} (page {page}, {seq} file(s) so far)")

        headers   = [th.get_text(strip=True) for th in table.find_all("th")]
        name_idx  = next((i for i, h in enumerate(headers) if h == "Name"), 1)
        size_idx  = next((i for i, h in enumerate(headers) if h == "Size"), 4)
        path_idx  = next((i for i, h in enumerate(headers) if h == "Path"), 3)
        mtime_idx = next((i for i, h in enumerate(headers) if h == "Date Modified"), None)

        rows_found = 0
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if not tds or any("No results" in td.get_text() for td in tds):
                continue
            name = clean_name(tds[name_idx]) if name_idx < len(tds) else ""
            if not name:
                continue
            size_text  = tds[size_idx].get_text(strip=True) if size_idx < len(tds) else ""
            size_bytes = parse_size_to_bytes(size_text)
            if size_bytes is None:
                continue
            size_bytes = int(size_bytes)
            parent = clean_path(tds[path_idx]) if path_idx < len(tds) else ""
            path   = f"{parent}/{name}" if parent else name
            mtime  = (tds[mtime_idx].get_text(strip=True)
                      if mtime_idx is not None and mtime_idx < len(tds) else "")

            ext = _extension_of(name)
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            ext_sizes[ext]  = ext_sizes.get(ext, 0) + size_bytes

            entry = {"path": path, "size_bytes": size_bytes, "extension": ext, "mtime": mtime}
            seq += 1
            if len(heap) < top_n:
                heapq.heappush(heap, (size_bytes, seq, entry))
            elif size_bytes > heap[0][0]:
                heapq.heapreplace(heap, (size_bytes, seq, entry))
            rows_found += 1

        if rows_found < 100:
            break
        page += 1

    extension_stats = [
        {"extension": ext, "file_count": ext_counts[ext], "total_size_bytes": ext_sizes[ext]}
        for ext in ext_counts
    ]
    top_files = [entry for _, _, entry in sorted(heap, key=lambda t: t[0], reverse=True)]
    for rank, entry in enumerate(top_files, start=1):
        entry["rank"] = rank

    return extension_stats, top_files


# ── sort-param probe (diagnostic only, not used by the real crawl) ─────────────

SORT_PARAM_CANDIDATES = [
    # "sortby"/"sortdir" is confirmed to actually sort server-side (returns
    # numerically ascending by size) — these are direction-value guesses to
    # find whatever flips it to descending.
    {"sortby": "size", "sortdir": "desc"},
    {"sortby": "size", "sortdir": "DESC"},
    {"sortby": "size", "sortdir": "Desc"},
    {"sortby": "size", "sortdir": "descending"},
    {"sortby": "size", "sortdir": "reverse"},
    {"sortby": "size", "sortdir": "dsc"},
    {"sortby": "size", "sortdir": "-1"},
    {"sortby": "size", "sortdir": "0"},
    {"sortby": "size", "sortdir": "1"},
    {"sortby": "size", "order": "desc"},
    {"sortby": "-size"},
    {"sortby": "size", "reverse": "true"},
    {"sortby": "size", "reverse": "1"},
    {"sortby": "size"},  # no direction param at all — see what the default is
]


def probe_sort_params(session, full_path, index_name):
    """
    Diagnostic helper — NOT used by the real crawl. Tries a handful of
    candidate sort-by-size query params against search.php for full_path and
    prints the Name/Size of the first 10 rows returned by each, so a human
    can check by eye whether any of them actually come back sorted
    descending by size (vs. the unmodified baseline order).

    Diskover's "Size" column header in the browser may only sort
    client-side (e.g. a DataTables-style JS table over whatever page of
    results already loaded) with no server-side equivalent at all — if
    every candidate below comes back in the same order as the baseline,
    that's almost certainly what's going on, and there's no cheap
    server-side "give me the top N by size" to use.
    """
    escaped = escape_path(full_path)
    base_params = {
        "q": f"parent_path:{escaped}* AND type:file", "submitted": "true", "p": 1,
        "resultsize": 10, "path": full_path, "userinput": "true",
        "show_files": "1", "index": index_name,
    }

    log(f"\nProbing sort params against {full_path}\n{'=' * 70}")
    for candidate in [{}] + SORT_PARAM_CANDIDATES:
        params = {**base_params, **candidate}
        label = "baseline (no sort param)" if not candidate else str(candidate)
        try:
            r = session.get(f"{BASE}/search.php", params=params,
                             allow_redirects=False, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            log(f"\n{label}: request failed ({exc})")
            continue
        if r.status_code != 200:
            log(f"\n{label}: HTTP {r.status_code}")
            continue

        soup  = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            log(f"\n{label}: no results table")
            continue
        headers  = [th.get_text(strip=True) for th in table.find_all("th")]
        name_idx = next((i for i, h in enumerate(headers) if h == "Name"), 1)
        size_idx = next((i for i, h in enumerate(headers) if h == "Size"), 4)

        rows = []
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if not tds or any("No results" in td.get_text() for td in tds):
                continue
            name = clean_name(tds[name_idx]) if name_idx < len(tds) else ""
            size = tds[size_idx].get_text(strip=True) if size_idx < len(tds) else ""
            if name:
                rows.append((name, size))

        log(f"\n{label}:")
        if not rows:
            log("    (no rows returned)")
        for name, size in rows:
            log(f"    {size:>12}  {name}")


# ── stale stats ────────────────────────────────────────────────────────────────

def query_file_count(session, base_query, dirpath, index_name):
    """Run a search and return the total hit count from the pagination text."""
    try:
        r = session.get(f"{BASE}/search.php", params={
            "q": base_query, "submitted": "true", "p": 1,
            "resultsize": 1, "path": dirpath,
            "userinput": "true", "show_files": "1",
            "index": index_name,
        }, allow_redirects=False, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        log_failure("query_file_count", str(exc), path=dirpath, index=index_name)
        return "error"
    if r.status_code != 200:
        log_failure("query_file_count", f"HTTP {r.status_code}", path=dirpath, index=index_name)
        return "error"
    soup = BeautifulSoup(r.text, "html.parser")
    for div in soup.find_all("div"):
        m = re.search(r'per page\s+of\s+([\d,]+)', div.get_text(" ", strip=True))
        if m:
            return m.group(1)
    return "0"


def parse_count(text):
    if not text or str(text).strip().lower() in ("", "error", "n/a"):
        return None
    try:
        return int(str(text).replace(",", "").strip())
    except ValueError:
        return None


def estimate_stale_bytes(total_files, stale_files, dir_size_bytes):
    if total_files in (None, 0) or stale_files is None or dir_size_bytes is None:
        return None
    return int((stale_files / total_files) * dir_size_bytes)


def get_stale_stats(session, all_dirs, dir_size_map, outfile="stale_files.csv"):
    """
    all_dirs: [(dirname, dirpath, label, short_path, index_name)]
    Outputs size-first stale columns for >1yr, >2yr, and >4yr windows.
    """
    PERIODS = [
        ("Total Files",   None),      # no atime filter → all files
        ("Stale >1yr",    "1y"),
        ("Stale >2yr",    "2y"),
        ("Stale >4yr",    "4y"),
    ]

    results = []

    for dirname, dirpath, label, short_path, index_name in all_dirs:
        escaped  = escape_path(dirpath)
        base_path_filter = f"parent_path:{escaped}* AND size:>=1 AND type:file"

        counts = {}
        for col, period in PERIODS:
            if period is None:
                query = base_path_filter
            else:
                query = f"atime:[* TO now\\/m-{period}\\/d}} AND {base_path_filter}"
            counts[col] = query_file_count(session, query, dirpath, index_name)

        dir_size = dir_size_map.get((label, dirname), "")
        dir_size_bytes = parse_size_to_bytes(dir_size)
        total_files = parse_count(counts["Total Files"])
        stale1 = parse_count(counts["Stale >1yr"])
        stale2 = parse_count(counts["Stale >2yr"])
        stale4 = parse_count(counts["Stale >4yr"])
        stale1_bytes = estimate_stale_bytes(total_files, stale1, dir_size_bytes)
        stale2_bytes = estimate_stale_bytes(total_files, stale2, dir_size_bytes)
        stale4_bytes = estimate_stale_bytes(total_files, stale4, dir_size_bytes)

        log(
            f"  {short_path}: "
            f"stale>1yr size={bytes_to_human(stale1_bytes) if stale1_bytes is not None else 'n/a'} | "
            f"stale>2yr size={bytes_to_human(stale2_bytes) if stale2_bytes is not None else 'n/a'} | "
            f"stale>4yr size={bytes_to_human(stale4_bytes) if stale4_bytes is not None else 'n/a'} | "
            f"dir size={dir_size} | "
            f"index={label}"
        )
        results.append({
            "Directory":     dirname,
            "Path":          short_path,
            "Index":         label,
            "Total Dir Size": dir_size,
            "Stale >1yr Bytes": stale1_bytes if stale1_bytes is not None else "",
            "Stale >2yr Bytes": stale2_bytes if stale2_bytes is not None else "",
            "Stale >4yr Bytes": stale4_bytes if stale4_bytes is not None else "",
        })

    fieldnames = ["Directory", "Path", "Index", "Total Dir Size",
                  "Stale >1yr Bytes", "Stale >2yr Bytes", "Stale >4yr Bytes"]
    with open(outfile, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(results)
    log(f"\nSaved {len(results)} rows → {outfile}")


# ── resuming a partial run ──────────────────────────────────────────────────────
# Lets a re-run skip whatever a previous (possibly interrupted, or simply
# still-fresh) run already wrote to run_dir, instead of re-crawling
# everything from scratch — e.g. re-running just for updated extension/
# top-files data without re-scraping every area's directory listing again.

def confirm_reuse(prompt_label, path, force_fresh=False):
    """Ask whether to reuse an existing output file instead of re-crawling it.
    Defaults to reuse (bare Enter = yes) since that's the point of resuming.
    --fresh skips the prompt entirely and always re-crawls."""
    if force_fresh:
        return False
    answer = input(
        f"  {prompt_label} already exists ({path}) — reuse it instead of re-crawling? [Y/n]: "
    ).strip().lower()
    return answer in ("", "y", "yes")


def load_area_csv_for_resume(csv_path, root):
    """
    Reconstruct the [(name, full_path, size_str)] list list_dirs_at() would
    have returned, from a per-area CSV save_area_csv() wrote in a prior run —
    used to skip re-crawling an area whose CSV already exists on disk.
    """
    subdirs = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            size = (row.get("Size") or "").strip()
            subdirs.append((name, f"{root}/{name}", size))
    return subdirs


def retry_failed_dir_file_stats(session, failures_csv_path, file_stats_workers=6):
    """
    Re-scan just the subdirectories whose get_dir_file_stats() call failed in
    a previous run (recorded in that run's failures.csv), appending
    successful results onto that run's existing extension_stats.csv/
    top_files.csv rather than re-running the whole crawl. Failure rows of
    other step types (list_dirs_at, save_area_csv, etc.) are left alone —
    those aren't per-subdirectory in the same way, and a normal re-run's
    per-area reuse prompts (see confirm_reuse) already handle retrying them.
    Rewrites failures.csv afterward with whatever's still unresolved.
    """
    run_dir = os.path.dirname(os.path.abspath(failures_csv_path))
    ext_outfile = os.path.join(run_dir, "extension_stats.csv")
    top_outfile = os.path.join(run_dir, "top_files.csv")

    with open(failures_csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    other_failures = [r for r in rows if r.get("step") != "get_dir_file_stats"]
    targets = {}  # (path, index) -> row (dedup: our own logging only ever emits one per dir anyway)
    for row in rows:
        if row.get("step") == "get_dir_file_stats":
            targets[(row.get("path"), row.get("index"))] = row

    if not targets:
        log("No get_dir_file_stats failures found in this file — nothing to retry.")
        if other_failures:
            log(f"({len(other_failures)} failure(s) of other step types are in this file — "
                  f"those aren't handled by --retry-failures; a normal re-run's reuse prompts "
                  f"will retry those areas instead.)")
        return

    if not (os.path.isfile(ext_outfile) and os.path.isfile(top_outfile)):
        log(f"ERROR: expected {ext_outfile} and {top_outfile} to already exist next to "
              f"{failures_csv_path} (this appends to them, it doesn't start fresh).")
        raise SystemExit(1)

    log(f"Retrying {len(targets)} failed subdirectory scan(s) from {failures_csv_path} "
          f"({file_stats_workers} at a time)…")

    # Written incrementally, as each directory resolves — not batched until
    # the whole retry finishes. Large directories can take a long time (see
    # get_dir_file_stats' no-resume pagination), so batching everything until
    # the end meant a crash/interrupt partway through lost all completed work
    # and gave no visibility into progress from the output files. Now
    # extension_stats.csv/top_files.csv gain rows as each directory finishes,
    # and failures.csv is rewritten after every single resolution so it
    # always reflects "what's actually still left to retry".
    resolved_keys = set()
    snapshot_len = len(FAILURE_LOG)

    def write_remaining_failures():
        remaining = other_failures + [row for key, row in targets.items() if key not in resolved_keys]
        with open(failures_csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FAILURE_CSV_FIELDNAMES)
            w.writeheader()
            for row in remaining:
                w.writerow(row)
        return remaining

    with open(ext_outfile, "a", newline="") as ext_f, open(top_outfile, "a", newline="") as top_f:
        ext_w = csv.DictWriter(ext_f, fieldnames=[
            "Area", "ParentDirectory", "ParentPath", "Path", "Extension", "FileCount", "TotalSizeBytes"])
        top_w = csv.DictWriter(top_f, fieldnames=[
            "Area", "ParentDirectory", "ParentPath", "Path", "FilePath", "Extension", "SizeBytes", "Mtime", "Rank"])

        with ThreadPoolExecutor(max_workers=file_stats_workers) as pool:
            future_to_key = {
                pool.submit(
                    get_dir_file_stats, session, path, index,
                    label=row.get("area"), dirname=row.get("directory"),
                    short_path=row.get("short_path"), child_path=row.get("child_path"),
                ): (path, index)
                for (path, index), row in targets.items()
            }

            done = 0
            for future in as_completed(future_to_key):
                path, index = future_to_key[future]
                row = targets[(path, index)]
                area, dirname = row.get("area", ""), row.get("directory", "")
                short_path, child_path = row.get("short_path", ""), row.get("child_path", "")
                extension_stats, top_files = future.result()  # get_dir_file_stats never raises

                # Safe to check the instant this one future completes — unlike
                # a plain before/after len(FAILURE_LOG) diff over the whole
                # batch, matching by this exact (path, index) key isn't
                # affected by other still-running futures logging their own
                # failures concurrently in between.
                failed_now = any(
                    e.get("path") == path and e.get("index") == index
                    for e in FAILURE_LOG[snapshot_len:]
                )

                # get_dir_file_stats() returns whatever it collected before a
                # page failure, not an empty result — a still-failing retry
                # has non-empty but incomplete extension_stats/top_files.
                # Writing that here would append another (possibly still
                # partial) copy on top of anything already in these files for
                # this directory, since this function only ever appends,
                # never replaces — the exact mechanism behind the duplicate-
                # data bug fixed by import_csv.py's _dedupe_*_rows(). Only
                # write once the directory is confirmed fully resolved.
                if not failed_now:
                    for stat in extension_stats:
                        ext_w.writerow({
                            "Area": area, "ParentDirectory": dirname,
                            "ParentPath": short_path, "Path": child_path,
                            "Extension": stat["extension"], "FileCount": stat["file_count"],
                            "TotalSizeBytes": stat["total_size_bytes"],
                        })
                    for tf in top_files:
                        top_w.writerow({
                            "Area": area, "ParentDirectory": dirname,
                            "ParentPath": short_path, "Path": child_path,
                            "FilePath": tf["path"], "Extension": tf["extension"],
                            "SizeBytes": tf["size_bytes"], "Mtime": tf["mtime"], "Rank": tf["rank"],
                        })
                    ext_f.flush()
                    top_f.flush()

                done += 1
                if failed_now:
                    log(f"    [{done}/{len(targets)}] {child_path}: still failing, left in failures.csv")
                else:
                    resolved_keys.add((path, index))
                    log(f"    [{done}/{len(targets)}] {child_path}: {len(extension_stats)} extension(s), "
                          f"{len(top_files)} file(s) — resolved")

                write_remaining_failures()

    remaining = other_failures + [row for key, row in targets.items() if key not in resolved_keys]
    log(f"\n{len(resolved_keys)}/{len(targets)} resolved. {len(remaining)} row(s) remain in {failures_csv_path}.")


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Export Diskover directory CSVs and stale-size stats"
    )
    parser.add_argument(
        "--month",
        help="Use indices from a specific month in YYYY-MM (example: 2026-07). Default: latest available indices.",
    )
    parser.add_argument(
        "--subdirs",
        dest="subdirs",
        action="store_true",
        help=argparse.SUPPRESS,  # kept for backward compatibility — subdirs are on by default now
    )
    parser.add_argument(
        "--no-subdirs",
        dest="subdirs",
        action="store_false",
        help="Skip the extra one-level-deeper subdirectory crawl into each group-leader directory "
             "(Research Groups / Group Scratch / Archive Groups / Groups areas, plus Deep Archive's "
             "Projects/Platforms — see DEEP_ARCHIVE_FORCE_GROUP_INDEX_KEYS). On by default; "
             "use this to speed up a run when you don't need subdirectory-level data.",
    )
    parser.add_argument(
        "--no-file-stats",
        dest="file_stats",
        action="store_false",
        help="Skip the per-extension size breakdown and top-1000-biggest-files scan for each "
             "group-leader subdirectory (one extra recursive file scan per subdirectory, on top "
             "of --subdirs). On by default; use this to speed up a run when you only need "
             "subdirectory sizes.",
    )
    parser.add_argument(
        "--file-stats-workers",
        type=int,
        default=6,
        help="How many subdirectory file-stats scans to run concurrently (default: 6). "
             "Each scan is network-bound (waiting on Diskover), so this is the main lever for "
             "speeding up a run — higher values finish faster but put more concurrent load on "
             "the Diskover server.",
    )
    parser.add_argument(
        "--probe-sort",
        metavar="FULL_PATH",
        help="Diagnostic only, does not run the real crawl: log in, then try a handful of "
             "candidate sort-by-size query params against search.php for FULL_PATH (a real "
             "filesystem path, e.g. /ifs/apricot/JIC/PrimaryData/GROUP_SCRATCH/Some-Leader/Some-Folder) "
             "and print each candidate's first 10 results so you can check by eye whether any of "
             "them come back sorted by size. Requires --probe-sort-index.",
    )
    parser.add_argument(
        "--probe-sort-index",
        metavar="INDEX_NAME",
        help="Diskover index name to use with --probe-sort (e.g. "
             "diskover-jic-apricot-primarydata-group_scratch-2026-09).",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Always re-crawl everything, even if this run's output folder already has files "
             "from an earlier attempt — skips the 'reuse existing output?' prompts entirely. "
             "Without this, an area/step whose output file already exists asks whether to "
             "reuse it (default: yes) instead of re-crawling.",
    )
    parser.add_argument(
        "--retry-failures",
        metavar="FAILURES_CSV",
        help="Instead of a normal crawl: log in, then re-scan just the subdirectories whose "
             "file-stats scan failed in a previous run (the get_dir_file_stats rows in that "
             "run's failures.csv), appending successful results onto that run's existing "
             "extension_stats.csv/top_files.csv. Skips everything else — indices, areas, "
             "stale stats, the normal Step 4 pass.",
    )
    parser.set_defaults(subdirs=True, file_stats=True)
    args = parser.parse_args()

    if args.probe_sort and not args.probe_sort_index:
        log("ERROR: --probe-sort requires --probe-sort-index <index name>")
        raise SystemExit(2)

    try:
        target_month = parse_target_month(args.month)
    except ValueError as exc:
        log(f"Invalid --month value: {exc}")
        raise SystemExit(2)

    run_date = date(target_month[0], target_month[1], 1) if target_month else None
    run_dir = make_run_dir(run_date)
    log(f"Output folder for this run: {run_dir}\n")

    username = input("Username: ")
    password = getpass.getpass("Password: ")
    session  = login(username, password)

    if args.probe_sort:
        probe_sort_params(session, args.probe_sort, args.probe_sort_index)
        raise SystemExit(0)

    if args.retry_failures:
        retry_failed_dir_file_stats(session, args.retry_failures, args.file_stats_workers)
        raise SystemExit(0)

    # ── Step 1: all indices the user can access ────────────────────────────────
    log("\nDiscovering indices …")
    indices = get_available_indices(session)

    if not indices:
        log("No indices found. Check login or Diskover access.")
        raise SystemExit(1)

    # Diskover historically exposed one dated index per area (e.g.
    # "diskover-jic-apricot-primarydata-research-groups-2026-09"), each
    # resolvable via INDEX_ROOT_PATHS. Some deployments now instead expose a
    # single combined/alias index (e.g. "diskover-latest") covering every area.
    # Detect that specific case — exactly one discovered index that doesn't
    # match any known area — and fall back to crawling every known area root
    # path directly against it, since a single index can still be filtered by
    # `parent_path` regardless of which area actually holds the data.
    combined_mode = len(indices) == 1 and strip_date(indices[0][1]) not in INDEX_ROOT_PATHS

    if combined_mode:
        combined_index_name = indices[0][1]
        log(
            f"Only one index found ('{combined_index_name}'), not a known per-area index — "
            "treating it as a single combined index and crawling every known area root path against it."
        )
        areas = [(index_to_label(key), combined_index_name, root)
                 for key, root in INDEX_ROOT_PATHS.items()]
        log(f"Using {len(areas)} known area root(s):")
        for label, _, _ in areas:
            log(f"  {label}")
    else:
        if target_month:
            filtered = [
                (label, name)
                for label, name in indices
                if extract_index_month(name) == target_month
            ]
            if not filtered:
                log(
                    "No indices found for "
                    f"{target_month[0]:04d}-{target_month[1]:02d}. "
                    "Try another month or run without --month for latest."
                )
                raise SystemExit(1)
            indices = filtered
            log(f"Using indices for {target_month[0]:04d}-{target_month[1]:02d}")

        log(f"Found {len(indices)} index(es):")
        for label, name in indices:
            log(f"  {label}")
        areas = [(label, name, None) for label, name in indices]  # root resolved per-area below

    # ── Step 1.5: confirm each index's own crawl has actually finished ──────────
    # Scraping an index while Diskover is still mid-crawl on it silently
    # returns partial/incomplete results (no error) — this bit the October run:
    # group_scratch was scraped a full ~23 hours before its own index finished
    # building. Not meaningful in combined_mode (one shared index, not a
    # per-area finish time), so skipped there.
    if not combined_mode:
        log("Checking each index's own crawl-completion status (selectindices.php) …")
        index_status = get_index_crawl_status(session)
        unconfirmed = []
        for label, index_name, _ in areas:
            finish_dt = index_status.get(index_name) if index_status is not None else None
            if finish_dt is None:
                unconfirmed.append((label, index_name))
        if unconfirmed:
            log(
                f"WARNING: {len(unconfirmed)} of {len(areas)} area(s) could NOT be confirmed as "
                "finished indexing yet (blank/missing Finish Time, or selectindices.php couldn't "
                "be read at all) — scraping now risks silently incomplete data for these:"
            )
            for label, index_name in unconfirmed:
                log(f"    ! {label} ({index_name})")
            answer = input("Continue anyway? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                log("Aborted — re-run once the index(es) above have finished.")
                raise SystemExit(1)
        else:
            log("All area indices confirmed finished — safe to proceed.")

    # ── Step 2: per-area directory listing ─────────────────────────────────────
    all_dirs     = []    # (dirname, dirpath, label, short_path, index_name)
    dir_size_map = {}    # (label, dirname) -> size_str
    seen_keys    = set()

    for label, index_name, known_root in areas:
        safe_label = re.sub(r'[^\w\-]', '_', label).strip('_')
        log(f"\n{'='*55}\n{label}\n  index: {index_name}")

        root = known_root or get_root_path(session, index_name)
        if not root:
            log("  Root path unknown — skipping.")
            continue
        root = resolve_deep_archive_shard_root(session, root, index_name)
        log(f"  root:  {root}")

        area_outfile = os.path.join(run_dir, f"{safe_label}.csv")
        if os.path.isfile(area_outfile) and confirm_reuse("Area listing", area_outfile, args.fresh):
            subdirs = load_area_csv_for_resume(area_outfile, root)
            log(f"  Reusing {len(subdirs)} subdirectory(ies) from existing {area_outfile}")
        else:
            subdirs = list_dirs_at(session, root, index_name)
            log(f"  Found {len(subdirs)} subdirectory(ies)")
            if not subdirs:
                continue
            save_area_csv(session, safe_label, root, index_name, outfile=area_outfile)

        for dirname, dirpath, size in subdirs:
            key = (label, dirname)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            short_path = f"{safe_label}/{dirname}"
            all_dirs.append((dirname, dirpath, label, short_path, index_name))
            dir_size_map[(label, dirname)] = size

    # ── Step 3: stale file stats ───────────────────────────────────────────────
    stale_outfile = os.path.join(run_dir, "stale_files.csv")
    if os.path.isfile(stale_outfile) and confirm_reuse("Stale-file stats", stale_outfile, args.fresh):
        log(f"\nReusing existing {stale_outfile} — skipping stale-file check.")
    else:
        log(f"\n{'='*55}")
        log(f"Stale-file check for {len(all_dirs)} unique directories")
        print('='*55)
        get_stale_stats(session, all_dirs, dir_size_map, outfile=stale_outfile)

    # ── Step 4: subdirectory listing (on by default; --no-subdirs to skip) ─────
    if args.subdirs:
        subdirs_outfile = os.path.join(run_dir, "subdirectories.csv")
        ext_outfile = os.path.join(run_dir, "extension_stats.csv")
        top_outfile = os.path.join(run_dir, "top_files.csv")
        existing = [p for p in (subdirs_outfile, ext_outfile, top_outfile) if os.path.isfile(p)]

        if existing and confirm_reuse("Subdirectory/file-stats output", ", ".join(existing), args.fresh):
            log(f"\nReusing existing subdirectory/file-stats CSVs — skipping this step.")
        else:
            log(f"\n{'='*55}")
            log("Subdirectory listing for group-leader directories")
            print('='*55)
            save_subdirectory_csv(
                session, all_dirs,
                outfile=subdirs_outfile,
                file_stats=args.file_stats,
                extension_outfile=ext_outfile,
                top_files_outfile=top_outfile,
                file_stats_workers=args.file_stats_workers,
            )
    else:
        log("\nSkipping subdirectory listing (--no-subdirs).")

    if FAILURE_LOG:
        failures_path = os.path.join(run_dir, "failures.csv")
        with open(failures_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FAILURE_CSV_FIELDNAMES)
            w.writeheader()
            for entry in FAILURE_LOG:
                w.writerow(entry)
        log(
            f"\n{len(FAILURE_LOG)} request(s) failed during this run (timeouts, connection "
            f"errors, or non-200 responses) — each was skipped and the run continued, but "
            f"whatever they would have collected is missing. Full list → {failures_path}\n"
            f"Review it and decide whether to re-run the affected paths."
        )

    log(f"\nAll done! Files written to {run_dir}")
