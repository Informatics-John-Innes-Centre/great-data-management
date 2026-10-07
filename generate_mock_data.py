#!/usr/bin/env python3
"""
generate_mock_data.py — synthesize future monthly snapshots for testing.

Clones the latest real snapshot's directory_stats rows forward, month by
month, applying a bit of random growth to size and stale bytes so charts
(evolution lines, stale breakdowns) have a realistic trend to render. If
isilon_quota_stats has real data, also generates one mock row per day
(smaller daily growth) spanning from the latest real Isilon date through the
end of the last mocked month — so daily-resolution features (trend charts'
current-month view, Growth Alerts' 7-day/month windows) have something
realistic to render locally too, not just the monthly Diskover side.

Generated snapshots are tagged with a "[MOCK]" notes prefix so they're easy
to identify and remove later with delete_snapshot.py (which also cleans up
any mock isilon_quota_stats rows in the same months).

This is a local development/testing tool only — never run it against a
production database, since it fabricates data into real snapshot rows with
no undo beyond delete_snapshot.py.

Usage
-----
    python generate_mock_data.py                     # 3 months, default growth
    python generate_mock_data.py --months 6
    python generate_mock_data.py --months 3 --seed 7
    python generate_mock_data.py --dry-run            # preview, no DB writes

Config
------
Uses the same credentials as import_csv.py / app.py (config.py or the
DISKOVER_DB_* environment variables).
"""

import argparse
import random
import sys
from datetime import date, timedelta

import mysql.connector

MOCK_NOTES_PREFIX = "[MOCK]"


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


def next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def fetch_latest_snapshot(cur):
    cur.execute("SELECT id, run_date FROM snapshots ORDER BY run_date DESC LIMIT 1")
    return cur.fetchone()


def fetch_directory_rows(cur, snapshot_id):
    cur.execute(
        """
        SELECT directory, path, index_label, dir_size_bytes,
               stale_1yr_bytes, stale_2yr_bytes, stale_4yr_bytes
        FROM directory_stats
        WHERE snapshot_id = %s
        """,
        (snapshot_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def has_subdirectory_table(cur):
    cur.execute("SHOW TABLES LIKE 'subdirectory_stats'")
    return bool(cur.fetchone())


def fetch_subdirectory_rows(cur, snapshot_id):
    cur.execute(
        """
        SELECT directory, parent_path, subdirectory, path, index_label, dir_size_bytes
        FROM subdirectory_stats
        WHERE snapshot_id = %s
        """,
        (snapshot_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def has_isilon_table(cur):
    cur.execute("SHOW TABLES LIKE 'isilon_quota_stats'")
    return bool(cur.fetchone())


def fetch_latest_isilon_rows(cur):
    """Return (latest_stat_date, rows) for the most recent real Isilon day, or (None, [])."""
    cur.execute("SELECT MAX(stat_date) FROM isilon_quota_stats")
    latest_date = cur.fetchone()[0]
    if not latest_date:
        return None, []
    cur.execute(
        """
        SELECT area_label, entity_type, path, absolute_path,
               logical_bytes, physical_bytes, inode_count
        FROM isilon_quota_stats
        WHERE stat_date = %s
        """,
        (latest_date,),
    )
    cols = [d[0] for d in cur.description]
    return latest_date, [dict(zip(cols, row)) for row in cur.fetchall()]


def grow(value, low, high, rng):
    if value is None:
        return None
    return int(value * (1 + rng.uniform(low, high)))


def evolve_row(row, rng, size_growth, stale_drift):
    """Return a new row dict with grown size/stale values, clamped sensibly."""
    new_size = grow(row["dir_size_bytes"], *size_growth, rng)

    def evolve_stale(key):
        base = row[key]
        if base is None:
            return None
        grown = grow(base, *stale_drift, rng)
        # stale bytes can never exceed the directory's total size
        if new_size is not None:
            grown = min(grown, new_size)
        return grown

    new_row = dict(row)
    new_row["dir_size_bytes"] = new_size
    new_row["stale_1yr_bytes"] = evolve_stale("stale_1yr_bytes")
    new_row["stale_2yr_bytes"] = evolve_stale("stale_2yr_bytes")
    new_row["stale_4yr_bytes"] = evolve_stale("stale_4yr_bytes")
    return new_row


def evolve_subdirectory_row(row, rng, size_growth):
    """Same growth model as evolve_row(), but for a subdirectory_stats row."""
    new_row = dict(row)
    new_row["dir_size_bytes"] = grow(row["dir_size_bytes"], *size_growth, rng)
    return new_row


def evolve_isilon_row(row, rng, daily_growth):
    """Same growth model as evolve_row(), scaled down to a daily fraction."""
    new_row = dict(row)
    new_row["logical_bytes"] = grow(row["logical_bytes"], *daily_growth, rng)
    new_row["physical_bytes"] = grow(row["physical_bytes"], *daily_growth, rng)
    return new_row


def last_day_of_month(d: date) -> date:
    return next_month(d) - timedelta(days=1)


def format_size(bytes_value):
    if bytes_value is None:
        return None
    tb = bytes_value / 1e12
    return f"{tb:.2f} TB"


def get_or_create_snapshot(cur, run_date: date, notes: str, has_subdirs: bool):
    cur.execute(
        "SELECT id FROM snapshots WHERE YEAR(run_date) = YEAR(%s) AND MONTH(run_date) = MONTH(%s)",
        (run_date, run_date),
    )
    row = cur.fetchone()
    if row:
        snap_id = row[0]
        cur.execute("DELETE FROM directory_stats WHERE snapshot_id = %s", (snap_id,))
        if has_subdirs:
            cur.execute("DELETE FROM subdirectory_stats WHERE snapshot_id = %s", (snap_id,))
        cur.execute("UPDATE snapshots SET notes = %s WHERE id = %s", (notes, snap_id))
        return snap_id, True
    cur.execute("INSERT INTO snapshots (run_date, notes) VALUES (%s, %s)", (run_date, notes))
    return cur.lastrowid, False


def insert_rows(cur, snapshot_id, rows):
    for row in rows:
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
                row["directory"],
                row["path"],
                row["index_label"],
                format_size(row["dir_size_bytes"]),
                row["dir_size_bytes"],
                row["stale_1yr_bytes"],
                row["stale_2yr_bytes"],
                row["stale_4yr_bytes"],
            ),
        )


def insert_subdirectory_rows(cur, snapshot_id, rows):
    for row in rows:
        cur.execute(
            """
            INSERT INTO subdirectory_stats
              (snapshot_id, directory, parent_path, subdirectory, path,
               index_label, dir_size_raw, dir_size_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                snapshot_id,
                row["directory"],
                row["parent_path"],
                row["subdirectory"],
                row["path"],
                row["index_label"],
                format_size(row["dir_size_bytes"]),
                row["dir_size_bytes"],
            ),
        )


def delete_isilon_day(cur, stat_date):
    cur.execute("DELETE FROM isilon_quota_stats WHERE stat_date = %s", (stat_date,))


def insert_isilon_rows(cur, stat_date, rows):
    cur.executemany(
        """
        INSERT INTO isilon_quota_stats
          (stat_date, area_label, entity_type, path, absolute_path,
           logical_bytes, physical_bytes, inode_count)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        [
            (
                stat_date,
                row["area_label"],
                row["entity_type"],
                row["path"],
                row["absolute_path"],
                row["logical_bytes"],
                row["physical_bytes"],
                row["inode_count"],
            )
            for row in rows
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Generate mock future snapshots for testing.")
    parser.add_argument("--months", type=int, default=3, help="Number of future months to generate (default: 3)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible growth (default: 42)")
    parser.add_argument("--size-growth", nargs=2, type=float, default=[0.005, 0.03],
                         metavar=("MIN", "MAX"), help="Monthly size growth range as fractions (default: 0.005 0.03)")
    parser.add_argument("--stale-drift", nargs=2, type=float, default=[0.01, 0.05],
                         metavar=("MIN", "MAX"), help="Monthly stale-bytes growth range as fractions (default: 0.01 0.05)")
    parser.add_argument("--no-isilon", action="store_true",
                         help="Skip generating mock daily isilon_quota_stats rows")
    parser.add_argument("--dry-run", action="store_true", help="Preview months/row counts without writing to the DB")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    conn = connect()
    cur = conn.cursor()

    try:
        latest = fetch_latest_snapshot(cur)
        if not latest:
            print("ERROR: no snapshots found — import at least one real month first.", file=sys.stderr)
            sys.exit(1)
        latest_id, latest_run_date = latest
        baseline_rows = fetch_directory_rows(cur, latest_id)
        if not baseline_rows:
            print("ERROR: latest snapshot has no directory_stats rows.", file=sys.stderr)
            sys.exit(1)

        has_subdirs = has_subdirectory_table(cur)
        baseline_subdir_rows = fetch_subdirectory_rows(cur, latest_id) if has_subdirs else []

        print(f"Baseline: snapshot id={latest_id} ({latest_run_date}), {len(baseline_rows)} directory rows"
              + (f", {len(baseline_subdir_rows)} subdirectory rows" if baseline_subdir_rows else ""))

        current_rows = baseline_rows
        current_subdir_rows = baseline_subdir_rows
        run_date = latest_run_date
        for i in range(args.months):
            run_date = next_month(run_date)
            current_rows = [
                evolve_row(row, rng, args.size_growth, args.stale_drift)
                for row in current_rows
            ]
            current_subdir_rows = [
                evolve_subdirectory_row(row, rng, args.size_growth)
                for row in current_subdir_rows
            ]
            notes = f"{MOCK_NOTES_PREFIX} generated test data (seed={args.seed})"

            if args.dry_run:
                total_tb = sum(r["dir_size_bytes"] or 0 for r in current_rows) / 1e12
                print(f"  [dry-run] would create snapshot for {run_date}: "
                      f"{len(current_rows)} rows, {total_tb:.1f} TB total")
                continue

            snap_id, replaced = get_or_create_snapshot(cur, run_date, notes, has_subdirs)
            insert_rows(cur, snap_id, current_rows)
            if has_subdirs:
                insert_subdirectory_rows(cur, snap_id, current_subdir_rows)
            action = "replaced" if replaced else "created"
            total_tb = sum(r["dir_size_bytes"] or 0 for r in current_rows) / 1e12
            print(f"  {action} snapshot id={snap_id} for {run_date}: "
                  f"{len(current_rows)} rows, {total_tb:.1f} TB total")

        if not args.no_isilon and has_isilon_table(cur):
            latest_isilon_date, isilon_baseline = fetch_latest_isilon_rows(cur)
            if not isilon_baseline:
                print("\nNo isilon_quota_stats data found — skipping mock daily Isilon generation.")
            else:
                daily_growth = (args.size_growth[0] / 30, args.size_growth[1] / 30)
                final_day = last_day_of_month(run_date)
                num_days = (final_day - latest_isilon_date).days
                print(f"\nIsilon baseline: {latest_isilon_date} ({len(isilon_baseline)} rows). "
                      f"Generating {max(num_days, 0)} mock day(s) through {final_day}.")

                if args.dry_run:
                    print(f"  [dry-run] would create ~{max(num_days, 0) * len(isilon_baseline)} "
                          f"isilon_quota_stats rows ({latest_isilon_date + timedelta(days=1)} .. {final_day})")
                else:
                    current_isilon_rows = isilon_baseline
                    day = latest_isilon_date
                    for _ in range(num_days):
                        day = day + timedelta(days=1)
                        current_isilon_rows = [
                            evolve_isilon_row(row, rng, daily_growth)
                            for row in current_isilon_rows
                        ]
                        delete_isilon_day(cur, day)
                        insert_isilon_rows(cur, day, current_isilon_rows)
                    print(f"  generated {num_days} day(s) of mock isilon_quota_stats "
                          f"({len(isilon_baseline)} rows/day)")

        if args.dry_run:
            print("\nDry run only — no changes written. Re-run without --dry-run to apply.")
        else:
            conn.commit()
            print(f"\nDone. Generated {args.months} mock month(s). "
                  f"Remove them later with: python delete_snapshot.py --list")
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
