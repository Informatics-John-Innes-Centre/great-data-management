#!/usr/bin/env python3
"""
import_isilon_daily.py — load a daily Isilon SmartQuotas JSON dump into MySQL.

Usage
-----
    python import_isilon_daily.py --file /isilon_json/daily-json/2026/09/JIC-daily-20260916.json
    python import_isilon_daily.py --auto [--root /isilon_json/daily-json] [--days 31]
    python import_isilon_daily.py --auto --year 2026

--auto scans <root>/YYYY/MM/JIC-daily-YYYYMMDD.json for every date not already
in the database and imports each one — safe to run daily via a systemd timer
even if a run is occasionally missed. Only dates within the last --days days
are considered by default (31), since the daily-json folder can hold years of
history and there's no need to backfill all of it — pass --days 0 to disable
the limit and catch up on everything available, or --year YYYY to backfill
one specific calendar year instead (e.g. the whole current year so far).

Data source
-----------
Isilon's own SmartQuotas feature, dumped daily to
<root>/YYYY/MM/JIC-daily-YYYYMMDD.json — a raw {"quotas": [...]} listing from
Isilon's Platform API. No crawling involved; OneFS tracks this continuously.
Size only (logical/physical bytes, inode count) — no staleness. Diskover
(diskover_complex.py / import_csv.py) stays the source for atime-based stale
buckets and for subdirectory-level size, which Isilon isn't quota'd at.

Note: the area-root table and label-formatting helpers below are duplicated
from diskover_complex.py rather than imported from it, since that module
pulls in requests/beautifulsoup4 (crawler-only dependencies not installed in
the server venv that runs this importer).

Config
------
Set credentials in config.py or override them with environment variables:
  DISKOVER_DB_HOST, DISKOVER_DB_PORT, DISKOVER_DB_USER,
  DISKOVER_DB_PASS, DISKOVER_DB_NAME
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import date, timedelta

import mysql.connector


# ── Known area roots (see diskover_complex.py's INDEX_ROOT_PATHS) ───────────
INDEX_ROOT_PATHS = {
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
    "diskover-jic-apricot-loanstorage-archive-groups":      "/ifs/apricot/JIC/LoanStorage/ARCHIVE/Groups",
    "diskover-jic-apricot-loanstorage-archive-platforms":   "/ifs/apricot/JIC/LoanStorage/ARCHIVE/Platforms",
    "diskover-jic-apricot-loanstorage-archive-projects":    "/ifs/apricot/JIC/LoanStorage/ARCHIVE/Projects",
    "diskover-jic-peach-deeparchive-groups":                "/mnt/deep-archive/Groups",
    "diskover-jic-peach-deeparchive-projects":              "/mnt/deep-archive/Projects",
    "diskover-jic-peach-deeparchive-platforms":             "/mnt/deep-archive/Platforms",
    "diskover-nbi-apricot-primarydata-reference_data":      "/ifs/apricot/NBI/PrimaryData/reference_data",
}


def strip_date(index_name):
    return re.sub(r'-\d{4}-\d{2}(-\d{2})?$', '', index_name)


def index_to_label(index_name):
    s = re.sub(r'^diskover-', '', strip_date(index_name))
    parts = s.split('-', 2)
    org  = '-'.join(p.upper() if len(p) <= 3 else p.title() for p in parts[:2])
    rest = parts[2].replace('-', ' ').replace('_', ' ').title() if len(parts) > 2 else ''
    return f"{org} / {rest}" if rest else org


def _safe_label(label):
    return re.sub(r'[^\w\-]', '_', label).strip('_')


# (root_path, area_label, safe_label) tuples, longest root first so the most
# specific match wins.
_AREA_ROOTS = sorted(
    (
        (root, index_to_label(key), _safe_label(index_to_label(key)))
        for key, root in INDEX_ROOT_PATHS.items()
    ),
    key=lambda t: len(t[0]),
    reverse=True,
)


def match_area(abs_path):
    """
    Return (area_label, safe_label, relative_name) for the first known area
    root abs_path falls under, or None if it's outside all of them.

    relative_name is "" when abs_path IS the area root itself — this happens
    for every `type: "user"` quota (Isilon uses one shared quota path per
    area, differentiated only by `persona`, not by path), so the caller
    supplies the real per-entity name in that case. For `type: "directory"`
    quotas an empty relative_name means the quota sits on the area root
    itself rather than a specific directory, and the caller should skip it.
    """
    for root, area_label, safe_label in _AREA_ROOTS:
        if abs_path == root:
            return area_label, safe_label, ""
        if abs_path.startswith(root + "/"):
            return area_label, safe_label, abs_path[len(root) + 1:]
    return None


# ── Filename / date parsing ──────────────────────────────────────────────────
_FILENAME_RE = re.compile(r'JIC-daily-(\d{4})(\d{2})(\d{2})\.json$')


def parse_stat_date(filename):
    m = _FILENAME_RE.search(os.path.basename(filename))
    if not m:
        raise ValueError(
            f"Can't parse a date from filename {filename!r} "
            "(expected JIC-daily-YYYYMMDD.json)"
        )
    year, month, day = (int(g) for g in m.groups())
    return date(year, month, day)


# ── DB ────────────────────────────────────────────────────────────────────────

def connect():
    # Imported here, not at module level, so --help works without DB secrets
    # configured — config.py deliberately raises immediately if they're
    # missing (see its docstring), which would otherwise break --help too.
    from config import DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME, charset="utf8mb4",
        autocommit=False,
    )


def ensure_isilon_table(cur):
    cur.execute("SHOW TABLES LIKE 'isilon_quota_stats'")
    if cur.fetchone():
        return
    cur.execute(
        """
        CREATE TABLE isilon_quota_stats (
            id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            stat_date       DATE            NOT NULL,
            area_label      VARCHAR(300)    NOT NULL,
            entity_type     ENUM('directory', 'user') NOT NULL,
            path            VARCHAR(1000)   NOT NULL,
            absolute_path   VARCHAR(1000)   NOT NULL,
            logical_bytes   BIGINT UNSIGNED,
            physical_bytes  BIGINT UNSIGNED,
            inode_count     BIGINT UNSIGNED,
            UNIQUE KEY uq_isilon_date_path (stat_date, path(255))
        ) ENGINE=InnoDB
        """
    )
    cur.execute("CREATE INDEX idx_isilon_stat_date ON isilon_quota_stats (stat_date)")
    cur.execute("CREATE INDEX idx_isilon_path ON isilon_quota_stats (path(255))")
    print("  Created missing table isilon_quota_stats")


# ── Parsing one day's JSON ────────────────────────────────────────────────────

def build_rows(quotas, stat_date):
    """Return (rows, stats) — rows ready for insertion, stats for logging."""
    rows = []
    stats = {"skipped_type": 0, "skipped_not_ready": 0, "skipped_unmatched": 0, "unmatched_paths": []}

    for q in quotas:
        qtype = q.get("type")
        if qtype not in ("directory", "user"):
            stats["skipped_type"] += 1
            continue
        if not q.get("ready", False):
            stats["skipped_not_ready"] += 1
            continue

        abs_path = q.get("path") or ""
        matched = match_area(abs_path)
        if not matched:
            stats["skipped_unmatched"] += 1
            stats["unmatched_paths"].append(abs_path)
            continue
        area_label, safe_label, relative = matched

        if qtype == "user":
            persona = q.get("persona") or {}
            name = persona.get("name") or persona.get("id")
            if not name:
                stats["skipped_unmatched"] += 1
                stats["unmatched_paths"].append(f"{abs_path} (no persona)")
                continue
        else:
            if not relative:
                # Quota sits on the area root itself, not a specific directory.
                stats["skipped_unmatched"] += 1
                stats["unmatched_paths"].append(f"{abs_path} (area root itself)")
                continue
            name = relative

        usage = q.get("usage") or {}
        rows.append({
            "stat_date": stat_date,
            "area_label": area_label,
            "entity_type": qtype,
            "path": f"{safe_label}/{name}",
            "absolute_path": abs_path,
            "logical_bytes": usage.get("logical"),
            "physical_bytes": usage.get("physical"),
            "inode_count": usage.get("inodes"),
        })

    return rows, stats


def import_file(cur, filepath):
    stat_date = parse_stat_date(filepath)
    if os.path.getsize(filepath) == 0:
        raise ValueError(f"{filepath} is empty")
    with open(filepath, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{filepath} is not valid JSON ({exc})") from exc
    quotas = data.get("quotas") or []
    rows, stats = build_rows(quotas, stat_date)

    cur.execute("DELETE FROM isilon_quota_stats WHERE stat_date = %s", (stat_date,))
    for row in rows:
        cur.execute(
            """
            INSERT INTO isilon_quota_stats
              (stat_date, area_label, entity_type, path, absolute_path,
               logical_bytes, physical_bytes, inode_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                row["stat_date"], row["area_label"], row["entity_type"],
                row["path"], row["absolute_path"],
                row["logical_bytes"], row["physical_bytes"], row["inode_count"],
            ),
        )

    print(
        f"  {stat_date}: inserted {len(rows)} row(s) "
        f"(skipped {stats['skipped_type']} wrong-type, "
        f"{stats['skipped_not_ready']} not-ready, "
        f"{stats['skipped_unmatched']} unmatched)"
    )
    if stats["unmatched_paths"]:
        for p in stats["unmatched_paths"][:5]:
            print(f"    unmatched: {p}")
        if len(stats["unmatched_paths"]) > 5:
            print(f"    ... and {len(stats['unmatched_paths']) - 5} more")

    return len(rows)


# ── Discovery for --auto ──────────────────────────────────────────────────────

def find_json_files(root):
    return sorted(glob.glob(os.path.join(root, "*", "*", "JIC-daily-*.json")))


def already_imported_dates(cur):
    cur.execute("SELECT DISTINCT stat_date FROM isilon_quota_stats")
    return {row[0] for row in cur.fetchall()}


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Import Isilon SmartQuotas daily JSON dump(s) into MySQL."
    )
    parser.add_argument("--file", help="Import one specific JIC-daily-YYYYMMDD.json file")
    parser.add_argument(
        "--auto", action="store_true",
        help="Scan --root for any dates not yet imported and import them",
    )
    parser.add_argument(
        "--root", default="/isilon_json/daily-json",
        help="Root folder to scan in --auto mode (default: /isilon_json/daily-json)",
    )
    parser.add_argument(
        "--days", type=int, default=31,
        help="In --auto mode, only import dates within the last N days (default: 31). "
             "Use --days 0 to catch up on all available history instead. Ignored if --year is given.",
    )
    parser.add_argument(
        "--year", type=int,
        help="In --auto mode, only import dates within this calendar year (e.g. --year 2026), "
             "instead of the --days window.",
    )
    args = parser.parse_args()

    if not args.file and not args.auto:
        parser.error("specify --file <path> or --auto")

    conn = connect()
    cur = conn.cursor()

    try:
        ensure_isilon_table(cur)
        conn.commit()

        if args.file:
            targets = [args.file]
        else:
            all_files = find_json_files(args.root)
            if not all_files:
                print(f"No JIC-daily-*.json files found under {args.root}")
                return
            imported = already_imported_dates(cur)
            targets = [f for f in all_files if parse_stat_date(f) not in imported]

            if args.year:
                targets = [f for f in targets if parse_stat_date(f).year == args.year]
                scope_desc = f" (limited to {args.year})"
            elif args.days > 0:
                cutoff = date.today() - timedelta(days=args.days)
                targets = [f for f in targets if parse_stat_date(f) >= cutoff]
                scope_desc = f" (limited to the last {args.days} days)"
            else:
                scope_desc = ""

            if not targets:
                print(f"Nothing new to import — {len(imported)} date(s) already in the database{scope_desc}.")
                return
            print(f"Found {len(targets)} new date(s) to import out of {len(all_files)} file(s) under {args.root}{scope_desc}")

        # Each file is its own transaction: a bad/corrupt file is skipped and
        # logged rather than rolling back every other day already imported
        # in this run.
        total = 0
        succeeded = 0
        failed = []
        for filepath in targets:
            try:
                total += import_file(cur, filepath)
                conn.commit()
                succeeded += 1
            except Exception as exc:
                conn.rollback()
                print(f"  ERROR importing {filepath}: {exc}", file=sys.stderr)
                failed.append(filepath)

        print(f"\nDone. Imported {total} row(s) across {succeeded} day(s).")
        if failed:
            print(f"\n{len(failed)} file(s) failed to import:", file=sys.stderr)
            for f in failed:
                print(f"  {f}", file=sys.stderr)
            sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
