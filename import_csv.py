#!/usr/bin/env python3
"""
import_csv.py — load a monthly Diskover scrape into MySQL.

Usage
-----
    python import_csv.py /path/to/csv_folder [--date 2026-09-01] [--notes "September run"]

If --date is omitted and the folder path contains runs/YYYY/MM, that month is
used automatically. Otherwise, the current month is used.

The folder may contain either:
    - stale_files.csv
    - one or more per-area CSV exports such as JIC-Apricot___Primarydata_User_Homes.csv

Config
------
Set credentials in config.py or override them with environment variables:
  DISKOVER_DB_HOST, DISKOVER_DB_PORT, DISKOVER_DB_USER,
  DISKOVER_DB_PASS, DISKOVER_DB_NAME
"""

import argparse
import csv
import os
import re
import sys
from collections import OrderedDict
from datetime import date
from typing import Optional
import mysql.connector

from duplicate_finder import cluster_duplicate_files


# ── Size parsing ──────────────────────────────────────────────────────────────
_SIZE_RE = re.compile(r'([\d.]+)\s*(TB|GB|MB|KB|B|BYTE|BYTES)', re.IGNORECASE)
_UNITS = {"TB": 1_000_000_000_000, "GB": 1_000_000_000,
          "MB": 1_000_000, "KB": 1_000, "B": 1,
          "BYTE": 1, "BYTES": 1}

def parse_bytes(text: str):
    """Return integer bytes from a human-readable size string, or None."""
    if not text:
        return None
    m = _SIZE_RE.search(text.strip())
    if m:
        return int(float(m.group(1)) * _UNITS[m.group(2).upper()])
    return None


def parse_int(text: str):
    """Strip commas and return int, or None."""
    if not text or text.strip().lower() in ("", "error", "n/a"):
        return None
    try:
        return int(text.replace(",", "").strip())
    except ValueError:
        return None


def parse_optional_bytes(text: str):
    """Parse bytes from either plain integer text or human-readable units."""
    if text is None:
        return None
    s = str(text).strip()
    if s == "":
        return None
    n = parse_int(s)
    if n is not None:
        return n
    return parse_bytes(s)


def label_from_filename(filename: str):
    stem = os.path.splitext(os.path.basename(filename))[0]
    return stem.replace("___", " / ").replace("_", " ")


def short_path_from_area(label: str, directory: str):
    safe_label = re.sub(r'[^\w\-]', '_', label).strip('_')
    return f"{safe_label}/{directory}"


# ── Main ──────────────────────────────────────────────────────────────────────

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


def ensure_schema_extensions(cur):
    """Ensure size-only stale schema and remove legacy count columns."""
    extensions = {
        "stale_1yr_bytes": "BIGINT UNSIGNED NULL",
        "stale_2yr_bytes": "BIGINT UNSIGNED NULL",
        "stale_4yr_bytes": "BIGINT UNSIGNED NULL",
    }
    for col, ddl in extensions.items():
        cur.execute("SHOW COLUMNS FROM directory_stats LIKE %s", (col,))
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE directory_stats ADD COLUMN {col} {ddl}")
            print(f"  Added missing column directory_stats.{col}")

    legacy_cols = ["total_files", "stale_1yr", "stale_2yr", "stale_4yr"]
    drop_cols = []
    for col in legacy_cols:
        cur.execute("SHOW COLUMNS FROM directory_stats LIKE %s", (col,))
        if cur.fetchone():
            drop_cols.append(col)

    if drop_cols:
        clauses = ", ".join([f"DROP COLUMN {c}" for c in drop_cols])
        cur.execute(f"ALTER TABLE directory_stats {clauses}")
        print("  Dropped legacy count columns: " + ", ".join(drop_cols))


def ensure_extension_column_widened(cur):
    """
    dir_extension_stats.extension / dir_top_files.extension were originally
    VARCHAR(50) — too narrow for some real filenames' trailing suffix (hit in
    production: MySQL error 1406 on a real crawl). Widen either table's
    column to VARCHAR(255) if it's still narrower than that. No-op on a
    table that was just created fresh (ensure_dir_extension_stats_table/
    ensure_dir_top_files_table already create it at the right width).
    """
    for table in ("dir_extension_stats", "dir_top_files"):
        cur.execute("SHOW TABLES LIKE %s", (table,))
        if not cur.fetchone():
            continue
        cur.execute(f"SHOW COLUMNS FROM {table} LIKE 'extension'")
        col = cur.fetchone()
        if not col:
            continue
        m = re.search(r"varchar\((\d+)\)", col[1], re.IGNORECASE)
        current_width = int(m.group(1)) if m else 0
        if current_width < 255:
            cur.execute(f"ALTER TABLE {table} MODIFY COLUMN extension VARCHAR(255) NOT NULL")
            print(f"  Widened {table}.extension from VARCHAR({current_width}) to VARCHAR(255)")


def ensure_subdirectory_table(cur):
    """Create subdirectory_stats if it doesn't exist yet (mirrors schema.sql)."""
    cur.execute("SHOW TABLES LIKE 'subdirectory_stats'")
    if cur.fetchone():
        return
    cur.execute(
        """
        CREATE TABLE subdirectory_stats (
            id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            snapshot_id     INT UNSIGNED    NOT NULL,
            directory       VARCHAR(500)    NOT NULL,
            parent_path     VARCHAR(1000)   NOT NULL,
            subdirectory    VARCHAR(500)    NOT NULL,
            path            VARCHAR(1000)   NOT NULL,
            index_label     VARCHAR(300)    NOT NULL,
            dir_size_raw    VARCHAR(50),
            dir_size_bytes  BIGINT UNSIGNED,
            CONSTRAINT fk_sds_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES snapshots (id) ON DELETE CASCADE
        ) ENGINE=InnoDB
        """
    )
    cur.execute("CREATE INDEX idx_sds_snapshot ON subdirectory_stats (snapshot_id)")
    cur.execute("CREATE INDEX idx_sds_path ON subdirectory_stats (path(255))")
    cur.execute("CREATE INDEX idx_sds_parent_path ON subdirectory_stats (parent_path(255))")
    cur.execute("CREATE INDEX idx_sds_directory ON subdirectory_stats (directory)")
    print("  Created missing table subdirectory_stats")


def ensure_dir_extension_stats_table(cur):
    """Create dir_extension_stats if it doesn't exist yet (mirrors schema.sql)."""
    cur.execute("SHOW TABLES LIKE 'dir_extension_stats'")
    if cur.fetchone():
        return
    cur.execute(
        """
        CREATE TABLE dir_extension_stats (
            id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            snapshot_id      INT UNSIGNED    NOT NULL,
            directory        VARCHAR(500)    NOT NULL,
            path             VARCHAR(1000)   NOT NULL,
            index_label      VARCHAR(300)    NOT NULL,
            extension        VARCHAR(255)    NOT NULL,
            file_count       INT UNSIGNED    NOT NULL,
            total_size_bytes BIGINT UNSIGNED NOT NULL,
            CONSTRAINT fk_des_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES snapshots (id) ON DELETE CASCADE
        ) ENGINE=InnoDB
        """
    )
    cur.execute("CREATE INDEX idx_des_snapshot ON dir_extension_stats (snapshot_id)")
    cur.execute("CREATE INDEX idx_des_path ON dir_extension_stats (path(255))")
    cur.execute("CREATE INDEX idx_des_directory ON dir_extension_stats (directory)")
    print("  Created missing table dir_extension_stats")


def ensure_dir_top_files_table(cur):
    """Create dir_top_files if it doesn't exist yet (mirrors schema.sql)."""
    cur.execute("SHOW TABLES LIKE 'dir_top_files'")
    if cur.fetchone():
        return
    cur.execute(
        """
        CREATE TABLE dir_top_files (
            id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            snapshot_id  INT UNSIGNED    NOT NULL,
            directory    VARCHAR(500)    NOT NULL,
            path         VARCHAR(1000)   NOT NULL,
            index_label  VARCHAR(300)    NOT NULL,
            file_path    VARCHAR(1500)   NOT NULL,
            extension    VARCHAR(255)    NOT NULL,
            size_bytes   BIGINT UNSIGNED NOT NULL,
            mtime        VARCHAR(50),
            rank         SMALLINT UNSIGNED NOT NULL,
            CONSTRAINT fk_dtf_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES snapshots (id) ON DELETE CASCADE
        ) ENGINE=InnoDB
        """
    )
    cur.execute("CREATE INDEX idx_dtf_snapshot ON dir_top_files (snapshot_id)")
    cur.execute("CREATE INDEX idx_dtf_path ON dir_top_files (path(255))")
    cur.execute("CREATE INDEX idx_dtf_directory ON dir_top_files (directory)")
    print("  Created missing table dir_top_files")


def ensure_leader_duplicate_summary_table(cur):
    """Create leader_duplicate_summary if it doesn't exist yet (mirrors schema.sql)."""
    cur.execute("SHOW TABLES LIKE 'leader_duplicate_summary'")
    if cur.fetchone():
        return
    cur.execute(
        """
        CREATE TABLE leader_duplicate_summary (
            id                BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            snapshot_id       INT UNSIGNED    NOT NULL,
            directory         VARCHAR(500)    NOT NULL,
            tolerance_pct     DECIMAL(4,1)    NOT NULL,
            cluster_count     INT UNSIGNED    NOT NULL,
            reclaimable_bytes BIGINT UNSIGNED NOT NULL,
            CONSTRAINT fk_lds_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES snapshots (id) ON DELETE CASCADE
        ) ENGINE=InnoDB
        """
    )
    cur.execute("CREATE INDEX idx_lds_snapshot ON leader_duplicate_summary (snapshot_id)")
    print("  Created missing table leader_duplicate_summary")


# Mirrors app.py's GROUP_AREA_PATTERNS/GROUP_CN_PREFIXES — duplicated here
# rather than imported, since this is a standalone CLI script with no Flask
# dependency. Keep these two in sync with app.py's if they ever change.
DUPLICATE_SUMMARY_GROUP_AREA_PATTERNS = (
    "%Research Groups%",
    "%Group Scratch%",
    "%Archive Groups%",
    "%Groups%",
)
DUPLICATE_SUMMARY_GROUP_CN_PREFIXES = (
    "RG-",
    "RGGRANT-",
    "ARCHIVE-JIC-",
    "ARCHIVE-",
)
DUPLICATE_SUMMARY_TOLERANCE_PCT = 1.0


def _strip_duplicate_summary_cn_prefix(cn):
    raw = (cn or "").strip()
    upper_raw = raw.upper()
    for prefix in DUPLICATE_SUMMARY_GROUP_CN_PREFIXES:
        if upper_raw.startswith(prefix):
            suffix = raw[len(prefix):].strip()
            if suffix:
                return suffix
            break
    return raw


def _discover_leaders_and_linked_rules(cur, snapshot_id, area_match):
    """
    Shared by compute_and_store_duplicate_summaries() and
    compute_and_store_cross_leader_duplicates(): every group leader name for
    this snapshot (own-named Groups-type directories, plus any leader a
    directory_group_rules row resolves to via the RG-/RGGRANT-/ARCHIVE-
    prefix convention), and the linked rules keyed by lowercased leader name.
    """
    cur.execute(
        f"SELECT DISTINCT directory FROM directory_stats WHERE snapshot_id = %s AND ({area_match})",
        (snapshot_id, *DUPLICATE_SUMMARY_GROUP_AREA_PATTERNS),
    )
    leaders = {row[0].strip() for row in cur.fetchall() if row[0] and row[0].strip()}

    cur.execute("SELECT index_label, path_prefix, ldap_group_cn FROM directory_group_rules")
    rules = [{"index_label": r[0], "path_prefix": r[1], "ldap_group_cn": r[2]} for r in cur.fetchall()]

    linked_by_leader = {}
    for rule in rules:
        cn = (rule["ldap_group_cn"] or "").strip()
        bare = _strip_duplicate_summary_cn_prefix(cn)
        if not bare or bare == cn:
            continue  # no recognised RG-/RGGRANT-/ARCHIVE- prefix -> not a leader-linked rule
        leaders.add(bare)
        linked_by_leader.setdefault(bare.lower(), []).append(rule)

    return leaders, linked_by_leader


def _leader_scope_where(leader, linked_by_leader, area_match):
    """WHERE clause + params matching one leader's own-named directory plus any linked rules."""
    clauses = [f"(LOWER(directory) = %s AND ({area_match}))"]
    params = [leader.lower(), *DUPLICATE_SUMMARY_GROUP_AREA_PATTERNS]
    for rule in linked_by_leader.get(leader.lower(), []):
        clauses.append("(index_label = %s AND path LIKE %s)")
        params.extend([rule["index_label"], f"{rule['path_prefix']}%"])
    return " OR ".join(clauses), params


def compute_and_store_duplicate_summaries(cur, snapshot_id):
    """
    Precomputes each group leader's "possible duplicate files" total
    (cluster count + reclaimable bytes, at a fixed DUPLICATE_SUMMARY_
    TOLERANCE_PCT) for this snapshot and stores it in
    leader_duplicate_summary — the admin-only "biggest potential savings"
    leaderboard on Growth Alerts reads from this table instead of
    clustering every leader's dir_top_files live on every page view, which
    would be far too slow for an on-demand page. Covers every leader
    system-wide, not scoped to any one viewer's access — the live,
    per-user-filtered equivalent is app.py's /duplicate-files page.

    Leader scoping mirrors app.py's leader_scope_sql(): directories
    literally named after the leader in a Groups-type area, plus any
    directory linked via a directory_group_rules row whose LDAP group CN
    resolves to that leader (same RG-/RGGRANT-/ARCHIVE- prefix convention).
    """
    area_match = " OR ".join(["index_label LIKE %s" for _ in DUPLICATE_SUMMARY_GROUP_AREA_PATTERNS])
    leaders, linked_by_leader = _discover_leaders_and_linked_rules(cur, snapshot_id, area_match)

    cur.execute("DELETE FROM leader_duplicate_summary WHERE snapshot_id = %s", (snapshot_id,))

    stored = 0
    for leader in sorted(leaders, key=str.lower):
        where_sql, params = _leader_scope_where(leader, linked_by_leader, area_match)

        cur.execute(
            f"""
            SELECT file_path, extension, size_bytes
            FROM dir_top_files
            WHERE snapshot_id = %s AND ({where_sql})
            """,
            (snapshot_id, *params),
        )
        rows = [{"file_path": r[0], "extension": r[1], "size_bytes": r[2]} for r in cur.fetchall()]
        if not rows:
            continue

        clusters = cluster_duplicate_files(rows, DUPLICATE_SUMMARY_TOLERANCE_PCT)
        if not clusters:
            continue

        reclaimable_bytes = sum(c["reclaimable_bytes"] for c in clusters)
        cur.execute(
            """
            INSERT INTO leader_duplicate_summary
              (snapshot_id, directory, tolerance_pct, cluster_count, reclaimable_bytes)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (snapshot_id, leader, DUPLICATE_SUMMARY_TOLERANCE_PCT, len(clusters), reclaimable_bytes),
        )
        stored += 1

    return stored


# Caps how many cross-leader clusters get stored per snapshot, same spirit as
# get_duplicate_leaderboard()'s default limit — keeps the table small since
# it's fully recomputed every month anyway, and the admin UI only ever shows
# the biggest few dozen by reclaimable space.
CROSS_LEADER_DUPLICATE_MAX_CLUSTERS = 200

# Only files at least this big are considered for cross-group duplicate
# detection — this is about spotting datasets worth consolidating into a
# shared location, not every coincidentally-matching small file.
CROSS_LEADER_DUPLICATE_MIN_SIZE_BYTES = 10_000_000_000  # 10 GB


def ensure_cross_leader_duplicate_tables(cur):
    """Create cross_leader_duplicate_clusters/_files if missing yet (mirrors schema.sql)."""
    cur.execute("SHOW TABLES LIKE 'cross_leader_duplicate_clusters'")
    if not cur.fetchone():
        cur.execute(
            """
            CREATE TABLE cross_leader_duplicate_clusters (
                id                 BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                snapshot_id        INT UNSIGNED    NOT NULL,
                basename           VARCHAR(500)    NOT NULL,
                extension          VARCHAR(50)     NOT NULL,
                tolerance_pct      DECIMAL(4,1)    NOT NULL,
                leader_count       SMALLINT UNSIGNED NOT NULL,
                file_count         SMALLINT UNSIGNED NOT NULL,
                total_size_bytes   BIGINT UNSIGNED NOT NULL,
                reclaimable_bytes  BIGINT UNSIGNED NOT NULL,
                CONSTRAINT fk_clc_snapshot FOREIGN KEY (snapshot_id)
                    REFERENCES snapshots (id) ON DELETE CASCADE
            ) ENGINE=InnoDB
            """
        )
        cur.execute("CREATE INDEX idx_clc_snapshot ON cross_leader_duplicate_clusters (snapshot_id)")
        print("  Created missing table cross_leader_duplicate_clusters")

    cur.execute("SHOW TABLES LIKE 'cross_leader_duplicate_files'")
    if not cur.fetchone():
        cur.execute(
            """
            CREATE TABLE cross_leader_duplicate_files (
                id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                cluster_id  BIGINT UNSIGNED NOT NULL,
                leader      VARCHAR(300)    NOT NULL,
                index_label VARCHAR(300)    NOT NULL,
                file_path   VARCHAR(1500)   NOT NULL,
                size_bytes  BIGINT UNSIGNED NOT NULL,
                mtime       DATE,
                CONSTRAINT fk_clf_cluster FOREIGN KEY (cluster_id)
                    REFERENCES cross_leader_duplicate_clusters (id) ON DELETE CASCADE
            ) ENGINE=InnoDB
            """
        )
        cur.execute("CREATE INDEX idx_clf_cluster ON cross_leader_duplicate_files (cluster_id)")
        print("  Created missing table cross_leader_duplicate_files")


def compute_and_store_cross_leader_duplicates(cur, snapshot_id):
    """
    Finds files that look like duplicates of each other across DIFFERENT
    group leaders — e.g. two groups that each independently downloaded the
    same reference dataset — as opposed to leader_duplicate_summary above,
    which only looks for duplicates within one leader's own scope (that case
    is already visible to the leader themselves on /duplicate-files, so it's
    deliberately excluded here to keep this list focused on the one thing
    only an admin can see: the same data sitting under multiple leaders who
    likely don't know about each other's copy, and might be able to share
    one copy from a commonly-accessible location instead).

    Same heuristic and caveats as cluster_duplicate_files(): filename + size
    within tolerance, no content hash available, so this is "worth checking"
    not "confirmed". Only considers files at least
    CROSS_LEADER_DUPLICATE_MIN_SIZE_BYTES — this is about flagging datasets
    worth consolidating, not every small file that happens to share a name
    and size. Stores at most CROSS_LEADER_DUPLICATE_MAX_CLUSTERS clusters,
    ranked by reclaimable space, fully recomputed every import.
    """
    area_match = " OR ".join(["index_label LIKE %s" for _ in DUPLICATE_SUMMARY_GROUP_AREA_PATTERNS])
    leaders, linked_by_leader = _discover_leaders_and_linked_rules(cur, snapshot_id, area_match)

    all_rows = []
    # A path can legitimately match more than one leader's scope — own-named
    # directory for one leader plus an overlapping/nested directory_group_
    # rules path_prefix for another, nothing in the schema stops that. Without
    # this, the same physical file would get appended twice under two
    # different "leader" tags, then cluster with itself (identical size) and
    # get reported as a cross-group duplicate with fake reclaimable space —
    # there's only ever one copy on disk. First leader encountered wins;
    # `leaders` isn't ordered meaningfully, so which one is arbitrary, but
    # that's fine since this path only ever appears once either way.
    seen_paths = set()
    for leader in leaders:
        where_sql, params = _leader_scope_where(leader, linked_by_leader, area_match)
        cur.execute(
            f"""
            SELECT file_path, extension, size_bytes, mtime, index_label
            FROM dir_top_files
            WHERE snapshot_id = %s AND ({where_sql})
            """,
            (snapshot_id, *params),
        )
        for r in cur.fetchall():
            if (r[2] or 0) < CROSS_LEADER_DUPLICATE_MIN_SIZE_BYTES:
                continue
            if r[0] in seen_paths:
                continue
            seen_paths.add(r[0])
            all_rows.append({
                "file_path": r[0], "extension": r[1], "size_bytes": r[2],
                "mtime": r[3], "index_label": r[4], "leader": leader,
            })

    clusters = cluster_duplicate_files(all_rows, DUPLICATE_SUMMARY_TOLERANCE_PCT)
    cross_leader_clusters = [c for c in clusters if len({f["leader"] for f in c["files"]}) >= 2]
    cross_leader_clusters.sort(key=lambda c: -c["reclaimable_bytes"])
    cross_leader_clusters = cross_leader_clusters[:CROSS_LEADER_DUPLICATE_MAX_CLUSTERS]

    # Cascades to cross_leader_duplicate_files via fk_clf_cluster ON DELETE CASCADE.
    cur.execute("DELETE FROM cross_leader_duplicate_clusters WHERE snapshot_id = %s", (snapshot_id,))

    for c in cross_leader_clusters:
        basename = c["files"][0]["file_path"].rsplit("/", 1)[-1]
        leader_count = len({f["leader"] for f in c["files"]})
        cur.execute(
            """
            INSERT INTO cross_leader_duplicate_clusters
              (snapshot_id, basename, extension, tolerance_pct, leader_count,
               file_count, total_size_bytes, reclaimable_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (snapshot_id, basename, c["extension"], DUPLICATE_SUMMARY_TOLERANCE_PCT,
             leader_count, c["count"], c["total_size_bytes"], c["reclaimable_bytes"]),
        )
        cluster_id = cur.lastrowid
        cur.executemany(
            """
            INSERT INTO cross_leader_duplicate_files
              (cluster_id, leader, index_label, file_path, size_bytes, mtime)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                (cluster_id, f["leader"], f["index_label"], f["file_path"], f["size_bytes"], f["mtime"])
                for f in c["files"]
            ],
        )

    return len(cross_leader_clusters)


def estimate_stale_bytes(total_files, stale_files, size_bytes):
    if total_files in (None, 0) or stale_files is None or size_bytes is None:
        return None
    return int((stale_files / total_files) * size_bytes)


def parse_run_date(date_arg: Optional[str]):
    """Parse run date from YYYY-MM or YYYY-MM-DD, normalized to first day of month."""
    if not date_arg:
        base = date.today()
    else:
        value = date_arg.strip()
        if re.fullmatch(r"\d{4}-\d{2}", value):
            base = date.fromisoformat(f"{value}-01")
        else:
            base = date.fromisoformat(value)
    return base.replace(day=1)


def infer_run_date_from_folder(folder: str):
    """Infer run date from a folder path containing .../YYYY/MM/..."""
    if not folder:
        return None
    path = folder.replace("\\", "/")
    matches = list(re.finditer(r"(?:^|/)(\d{4})/(0[1-9]|1[0-2])(?:/|$)", path))
    if not matches:
        return None
    last = matches[-1]
    year = int(last.group(1))
    month = int(last.group(2))
    return date(year, month, 1)


def get_or_create_snapshot(cur, run_date: date, notes: str):
    cur.execute(
        """
        SELECT id, run_date
        FROM snapshots
        WHERE YEAR(run_date) = YEAR(%s) AND MONTH(run_date) = MONTH(%s)
        ORDER BY id DESC
        LIMIT 1
        """,
        (run_date, run_date),
    )
    row = cur.fetchone()
    if row:
        snap_id, existing_run_date = row
        print(
            f"  Snapshot for {run_date.strftime('%Y-%m')} already exists "
            f"(id={snap_id}, date={existing_run_date}). Rows will be replaced."
        )
        if existing_run_date != run_date:
            cur.execute("UPDATE snapshots SET run_date = %s WHERE id = %s", (run_date, snap_id))
            print(f"  Normalized snapshot date to {run_date}")
        cur.execute("DELETE FROM directory_stats WHERE snapshot_id = %s", (snap_id,))
        cur.execute("DELETE FROM subdirectory_stats WHERE snapshot_id = %s", (snap_id,))
        cur.execute("DELETE FROM dir_extension_stats WHERE snapshot_id = %s", (snap_id,))
        cur.execute("DELETE FROM dir_top_files WHERE snapshot_id = %s", (snap_id,))
        return snap_id
    cur.execute(
        "INSERT INTO snapshots (run_date, notes) VALUES (%s, %s)",
        (run_date, notes or None)
    )
    return cur.lastrowid


def load_stale_csv(cur, snapshot_id: int, csv_path: str):
    """Read stale_files.csv and insert rows into directory_stats."""
    if not os.path.isfile(csv_path):
        print(f"  WARNING: {csv_path} not found — skipping.", file=sys.stderr)
        return 0

    # Expected columns:
    #   Directory, Path, Index, Total Dir Size,
    #   Stale >1yr Bytes, Stale >2yr Bytes, Stale >4yr Bytes
    # Legacy CSV fallback still supported if only stale file counts are present.
    col_map = {
        "Directory":      "directory",
        "Path":           "path",
        "Index":          "index_label",
        "Total Dir Size": "dir_size_raw",
    }

    rows_inserted = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            mapped = {dest: raw.get(src, "").strip()
                      for src, dest in col_map.items()}
            size_raw   = mapped["dir_size_raw"]
            size_bytes = parse_bytes(size_raw)
            stale_1yr_bytes = parse_optional_bytes(raw.get("Stale >1yr Bytes"))
            stale_2yr_bytes = parse_optional_bytes(raw.get("Stale >2yr Bytes"))
            stale_4yr_bytes = parse_optional_bytes(raw.get("Stale >4yr Bytes"))

            # Backward compatibility for older stale_files.csv versions.
            total_files = parse_int(raw.get("Total Files", ""))
            stale_1yr = parse_int(raw.get("Stale >1yr", ""))
            stale_2yr = parse_int(raw.get("Stale >2yr", ""))
            stale_4yr = parse_int(raw.get("Stale >4yr", ""))

            if stale_1yr_bytes is None:
                stale_1yr_bytes = estimate_stale_bytes(total_files, stale_1yr, size_bytes)
            if stale_2yr_bytes is None:
                stale_2yr_bytes = estimate_stale_bytes(total_files, stale_2yr, size_bytes)
            if stale_4yr_bytes is None:
                stale_4yr_bytes = estimate_stale_bytes(total_files, stale_4yr, size_bytes)

            cur.execute(
                """
                INSERT INTO directory_stats
                  (snapshot_id, directory, path, index_label,
                   dir_size_raw, dir_size_bytes,
                   stale_1yr_bytes, stale_2yr_bytes, stale_4yr_bytes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    snapshot_id,
                    mapped["directory"],
                    mapped["path"],
                    mapped["index_label"],
                    size_raw or None,
                    size_bytes,
                    stale_1yr_bytes,
                    stale_2yr_bytes,
                    stale_4yr_bytes,
                )
            )
            rows_inserted += 1
    return rows_inserted


def load_area_csv(cur, snapshot_id: int, csv_path: str):
    """Read a per-area CSV export and insert rows into directory_stats."""
    label = label_from_filename(csv_path)
    rows_inserted = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            directory = (raw.get("Name") or "").strip()
            parent_path = (raw.get("Path") or "").strip()
            if not directory:
                continue

            full_path = f"{parent_path.rstrip('/')}/{directory}" if parent_path else directory
            short_path = short_path_from_area(label, directory)
            size_raw = (raw.get("Size") or "").strip()

            cur.execute(
                """
                INSERT INTO directory_stats
                  (snapshot_id, directory, path, index_label,
                   dir_size_raw, dir_size_bytes,
                   stale_1yr_bytes, stale_2yr_bytes, stale_4yr_bytes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    snapshot_id,
                    directory,
                    short_path,
                    label,
                    size_raw or None,
                    parse_bytes(size_raw),
                    None,
                    None,
                    None,
                )
            )
            rows_inserted += 1

    return rows_inserted


def load_subdirectory_csv(cur, snapshot_id: int, csv_path: str):
    """Read subdirectories.csv (from diskover_complex.py --subdirs) and insert rows."""
    rows_inserted = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            directory = (raw.get("ParentDirectory") or "").strip()
            parent_path = (raw.get("ParentPath") or "").strip()
            subdirectory = (raw.get("Name") or "").strip()
            path = (raw.get("Path") or "").strip()
            index_label = (raw.get("Area") or "").strip()
            size_raw = (raw.get("Size") or "").strip()
            if not directory or not subdirectory:
                continue

            cur.execute(
                """
                INSERT INTO subdirectory_stats
                  (snapshot_id, directory, parent_path, subdirectory, path,
                   index_label, dir_size_raw, dir_size_bytes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    snapshot_id,
                    directory,
                    parent_path,
                    subdirectory,
                    path,
                    index_label,
                    size_raw or None,
                    parse_bytes(size_raw),
                )
            )
            rows_inserted += 1

    return rows_inserted


def _dedupe_extension_stats_rows(rows):
    """
    Some crawls have produced duplicate output for a subset of directories —
    the same directory's full extension breakdown written more than once
    into extension_stats.csv (seen in production, September 2026: 259 of
    1,035 directories affected, 2-4 duplicate passes each — root cause never
    pinned down, so this guards against a recurrence rather than a known,
    fixed bug). Detected by the same extension re-appearing for a path,
    which can only happen if a new pass has started — get_dir_file_stats()'s
    ext_counts/ext_sizes dicts can only ever hold one entry per extension
    within a single real pass. Keeps only the LAST complete pass per
    directory: every case checked during the original cleanup had the later
    pass being the more complete one (more extensions, more bytes
    accounted for), never the reverse.

    Returns (kept_rows, dropped_count, affected_directory_count).
    """
    by_path = OrderedDict()
    for r in rows:
        by_path.setdefault(r.get("Path"), []).append(r)

    kept = []
    dropped = 0
    affected = 0
    for path, group in by_path.items():
        chunks = []
        current = []
        seen_extensions = set()
        for r in group:
            extension = r.get("Extension")
            if extension in seen_extensions:
                chunks.append(current)
                current = []
                seen_extensions = set()
            current.append(r)
            seen_extensions.add(extension)
        if current:
            chunks.append(current)
        if len(chunks) > 1:
            affected += 1
            dropped += sum(len(c) for c in chunks[:-1])
        kept.extend(chunks[-1])
    return kept, dropped, affected


def load_extension_stats_csv(cur, snapshot_id: int, csv_path: str):
    """Read extension_stats.csv (from diskover_complex.py --subdirs) and insert rows."""
    rows_inserted = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        raw_rows = list(csv.DictReader(f))

    rows, dropped, affected = _dedupe_extension_stats_rows(raw_rows)
    if dropped:
        print(f"    WARNING: dropped {dropped} duplicate-pass row(s) across {affected} "
              f"director(y/ies) in {os.path.basename(csv_path)} — kept only the last "
              f"complete pass per directory.")

    for raw in rows:
        directory = (raw.get("ParentDirectory") or "").strip()
        path = (raw.get("Path") or "").strip()
        index_label = (raw.get("Area") or "").strip()
        extension = (raw.get("Extension") or "").strip()[:255]
        file_count = parse_int(raw.get("FileCount"))
        total_size_bytes = parse_int(raw.get("TotalSizeBytes"))
        if not directory or not path or not extension or file_count is None or total_size_bytes is None:
            continue

        cur.execute(
            """
            INSERT INTO dir_extension_stats
              (snapshot_id, directory, path, index_label,
               extension, file_count, total_size_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (snapshot_id, directory, path, index_label, extension, file_count, total_size_bytes)
        )
        rows_inserted += 1

    return rows_inserted


def _dedupe_top_files_rows(rows):
    """
    Same duplicate-pass problem as _dedupe_extension_stats_rows(), but
    detected via Rank resetting to "1" — top_files.csv's unambiguous signal
    that a new pass has started for that directory (get_dir_file_stats()
    always numbers a single pass 1..len(top_files) with no gaps or repeats).
    Keeps only the last complete pass per directory.

    Returns (kept_rows, dropped_count, affected_directory_count).
    """
    by_path = OrderedDict()
    for r in rows:
        by_path.setdefault(r.get("Path"), []).append(r)

    kept = []
    dropped = 0
    affected = 0
    for path, group in by_path.items():
        chunks = []
        current = []
        for r in group:
            if r.get("Rank") == "1" and current:
                chunks.append(current)
                current = []
            current.append(r)
        if current:
            chunks.append(current)
        if len(chunks) > 1:
            affected += 1
            dropped += sum(len(c) for c in chunks[:-1])
        kept.extend(chunks[-1])
    return kept, dropped, affected


def load_top_files_csv(cur, snapshot_id: int, csv_path: str):
    """Read top_files.csv (from diskover_complex.py --subdirs) and insert rows."""
    rows_inserted = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        raw_rows = list(csv.DictReader(f))

    rows, dropped, affected = _dedupe_top_files_rows(raw_rows)
    if dropped:
        print(f"    WARNING: dropped {dropped} duplicate-pass row(s) across {affected} "
              f"director(y/ies) in {os.path.basename(csv_path)} — kept only the last "
              f"complete pass per directory.")

    for raw in rows:
        directory = (raw.get("ParentDirectory") or "").strip()
        path = (raw.get("Path") or "").strip()
        index_label = (raw.get("Area") or "").strip()
        file_path = (raw.get("FilePath") or "").strip()
        extension = (raw.get("Extension") or "").strip()[:255]
        size_bytes = parse_int(raw.get("SizeBytes"))
        mtime = (raw.get("Mtime") or "").strip()
        rank = parse_int(raw.get("Rank"))
        if not directory or not path or not file_path or size_bytes is None or rank is None:
            continue

        cur.execute(
            """
            INSERT INTO dir_top_files
              (snapshot_id, directory, path, index_label,
               file_path, extension, size_bytes, mtime, rank)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (snapshot_id, directory, path, index_label,
             file_path, extension, size_bytes, mtime or None, rank)
        )
        rows_inserted += 1

    return rows_inserted


def prune_old_top_files(cur):
    """
    Keep dir_top_files rows for only the 2 most recent snapshot dates that
    have any — deletes anything older. Unlike directory_stats/subdirectory_stats/
    dir_extension_stats, this table is not kept as long-term history (see
    schema.sql).
    """
    cur.execute(
        """
        SELECT DISTINCT s.id
        FROM dir_top_files dtf
        JOIN snapshots s ON s.id = dtf.snapshot_id
        ORDER BY s.run_date DESC
        """
    )
    snapshot_ids = [row[0] for row in cur.fetchall()]
    prune_ids = snapshot_ids[2:]
    if not prune_ids:
        return 0
    placeholders = ",".join(["%s"] * len(prune_ids))
    cur.execute(f"DELETE FROM dir_top_files WHERE snapshot_id IN ({placeholders})", prune_ids)
    return cur.rowcount


def find_area_csvs(folder: str):
    csvs = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if not name.lower().endswith(".csv"):
            continue
        if name.lower() == "stale_files.csv":
            continue
        csvs.append(path)
    return csvs


def main():
    parser = argparse.ArgumentParser(description="Import Diskover CSV scrape into MySQL.")
    parser.add_argument("folder",  help="Folder containing stale_files.csv or per-area CSV files")
    parser.add_argument("--date",  help="Run date YYYY-MM or YYYY-MM-DD (default: infer from folder path, else current month)")
    parser.add_argument("--notes", help="Optional description for this snapshot")
    args = parser.parse_args()

    if args.date:
        run_date = parse_run_date(args.date)
    else:
        inferred = infer_run_date_from_folder(args.folder)
        run_date = inferred or parse_run_date(None)
        if inferred:
            print(f"Inferred snapshot month from folder: {inferred.strftime('%Y-%m')}")

    csv_path = os.path.join(args.folder, "stale_files.csv")
    area_csvs = find_area_csvs(args.folder)
    if not os.path.isfile(csv_path) and not area_csvs:
        print(
            f"ERROR: {csv_path} not found and no per-area CSV files were found in {args.folder}.",
            file=sys.stderr,
        )
        sys.exit(1)

    from config import DB_HOST, DB_PORT, DB_USER, DB_NAME
    print(f"Connecting to {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME} …")
    conn = connect()
    cur  = conn.cursor()

    try:
        ensure_schema_extensions(cur)
        ensure_subdirectory_table(cur)
        ensure_dir_extension_stats_table(cur)
        ensure_dir_top_files_table(cur)
        ensure_leader_duplicate_summary_table(cur)
        ensure_cross_leader_duplicate_tables(cur)
        ensure_extension_column_widened(cur)
        print(f"Snapshot date: {run_date}")
        snap_id = get_or_create_snapshot(cur, run_date, args.notes)
        print(f"  Snapshot id: {snap_id}")

        if os.path.isfile(csv_path):
            n = load_stale_csv(cur, snap_id, csv_path)
            print(f"  Inserted {n} directory rows from stale_files.csv")
        else:
            total_rows = 0
            print(f"  Importing {len(area_csvs)} area CSV file(s)")
            for area_csv in area_csvs:
                n = load_area_csv(cur, snap_id, area_csv)
                total_rows += n
                print(f"    Inserted {n} directory rows from {os.path.basename(area_csv)}")
            print(f"  Inserted {total_rows} directory rows from area CSV files")

        subdirs_csv_path = os.path.join(args.folder, "subdirectories.csv")
        if os.path.isfile(subdirs_csv_path):
            n = load_subdirectory_csv(cur, snap_id, subdirs_csv_path)
            print(f"  Inserted {n} subdirectory rows from subdirectories.csv")

        extension_csv_path = os.path.join(args.folder, "extension_stats.csv")
        if os.path.isfile(extension_csv_path):
            n = load_extension_stats_csv(cur, snap_id, extension_csv_path)
            print(f"  Inserted {n} extension-stat rows from extension_stats.csv")

        top_files_csv_path = os.path.join(args.folder, "top_files.csv")
        if os.path.isfile(top_files_csv_path):
            n = load_top_files_csv(cur, snap_id, top_files_csv_path)
            print(f"  Inserted {n} top-file rows from top_files.csv")
            pruned = prune_old_top_files(cur)
            if pruned:
                print(f"  Pruned {pruned} dir_top_files row(s) from older than the last 2 months")

            n = compute_and_store_duplicate_summaries(cur, snap_id)
            print(f"  Computed possible-duplicate-file summaries for {n} group leader(s)")

            n = compute_and_store_cross_leader_duplicates(cur, snap_id)
            print(f"  Computed {n} cross-group possible-duplicate-file cluster(s)")

        conn.commit()
        print("Done.")
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
