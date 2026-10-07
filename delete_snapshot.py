#!/usr/bin/env python3
"""
delete_snapshot.py — remove one or more monthly snapshots from the database.

Deleting a snapshot cascades (ON DELETE CASCADE) and removes all of its
directory_stats/subdirectory_stats rows automatically. Since isilon_quota_stats
is a separate, independently-dated (daily) table with no link to snapshots,
this also deletes any of its rows falling within the same month(s) — so
"delete this bad month" cleans up both the monthly Diskover data and the
daily Isilon data for that month in one go.

Usage
-----
    python delete_snapshot.py 2026-07                  # preview only (dry run)
    python delete_snapshot.py 2026-07 --yes             # actually delete
    python delete_snapshot.py 2026-07 2026-08 --yes      # delete multiple months
    python delete_snapshot.py --list                    # show all snapshots + Isilon coverage

Config
------
Uses the same credentials as import_csv.py / app.py (config.py or the
DISKOVER_DB_* environment variables).
"""

import argparse
import re
import sys
from datetime import date

import mysql.connector


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


def parse_month(value: str) -> date:
    value = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}")
    return date.fromisoformat(f"{value}-01")


def isilon_table_exists(cur):
    cur.execute("SHOW TABLES LIKE 'isilon_quota_stats'")
    return bool(cur.fetchone())


def month_where(column, months):
    clause = " OR ".join([f"(YEAR({column}) = %s AND MONTH({column}) = %s)"] * len(months))
    params = [part for m in months for part in (m.year, m.month)]
    return clause, params


def list_snapshots(cur):
    cur.execute(
        """
        SELECT s.id, s.run_date, s.notes, COUNT(ds.id) AS row_count
        FROM snapshots s
        LEFT JOIN directory_stats ds ON ds.snapshot_id = s.id
        GROUP BY s.id, s.run_date, s.notes
        ORDER BY s.run_date
        """
    )
    rows = cur.fetchall()
    if not rows:
        print("No snapshots found.")
    else:
        for snap_id, run_date, notes, row_count in rows:
            note_suffix = f" — {notes}" if notes else ""
            print(f"  id={snap_id:<4} {run_date}  ({row_count} directory rows){note_suffix}")

    if not isilon_table_exists(cur):
        return
    cur.execute(
        """
        SELECT DATE_FORMAT(stat_date, '%Y-%m') AS ym, COUNT(*), COUNT(DISTINCT stat_date)
        FROM isilon_quota_stats
        GROUP BY ym
        ORDER BY ym
        """
    )
    isilon_rows = cur.fetchall()
    print("\nIsilon daily data (isilon_quota_stats):")
    if not isilon_rows:
        print("  No rows found.")
    else:
        for ym, row_count, day_count in isilon_rows:
            print(f"  {ym}  ({row_count} rows across {day_count} day(s))")


def find_snapshots(cur, months):
    where, params = month_where("s.run_date", months)
    cur.execute(
        f"""
        SELECT s.id, s.run_date, s.notes, COUNT(ds.id) AS row_count
        FROM snapshots s
        LEFT JOIN directory_stats ds ON ds.snapshot_id = s.id
        WHERE {where}
        GROUP BY s.id, s.run_date, s.notes
        ORDER BY s.run_date
        """,
        params,
    )
    return cur.fetchall()


def count_isilon_rows(cur, months):
    if not isilon_table_exists(cur):
        return 0
    where, params = month_where("stat_date", months)
    cur.execute(f"SELECT COUNT(*) FROM isilon_quota_stats WHERE {where}", params)
    return cur.fetchone()[0]


def delete_isilon_rows(cur, months):
    if not isilon_table_exists(cur):
        return 0
    where, params = month_where("stat_date", months)
    cur.execute(f"DELETE FROM isilon_quota_stats WHERE {where}", params)
    return cur.rowcount


def main():
    parser = argparse.ArgumentParser(description="Delete one or more monthly snapshots.")
    parser.add_argument("months", nargs="*", type=parse_month, help="Month(s) to delete, as YYYY-MM")
    parser.add_argument("--yes", action="store_true", help="Actually delete (default is a dry run / preview)")
    parser.add_argument("--list", action="store_true", help="List all snapshots and Isilon coverage, then exit")
    args = parser.parse_args()

    conn = connect()
    cur = conn.cursor()

    try:
        if args.list:
            list_snapshots(cur)
            return

        if not args.months:
            parser.error("provide at least one month (YYYY-MM), or use --list")

        matches = find_snapshots(cur, args.months)
        isilon_count = count_isilon_rows(cur, args.months)
        if not matches and not isilon_count:
            print("No matching snapshots or Isilon data found.")
            return

        print("To delete:" if args.yes else "Would delete (dry run):")
        for snap_id, run_date, notes, row_count in matches:
            note_suffix = f" — {notes}" if notes else ""
            print(f"  snapshot id={snap_id:<4} {run_date}  ({row_count} directory rows){note_suffix}")
        if isilon_count:
            print(f"  isilon_quota_stats: {isilon_count} row(s) across the selected month(s)")

        if not args.yes:
            print("\nRe-run with --yes to actually delete these.")
            return

        ids = [row[0] for row in matches]
        if ids:
            cur.execute(
                f"DELETE FROM snapshots WHERE id IN ({','.join(['%s'] * len(ids))})",
                ids,
            )
        deleted_isilon = delete_isilon_rows(cur, args.months)
        conn.commit()
        print(
            f"\nDeleted {len(ids)} snapshot(s) (and their directory_stats/subdirectory_stats rows) "
            f"and {deleted_isilon} isilon_quota_stats row(s)."
        )
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
