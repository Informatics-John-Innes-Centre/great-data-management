#!/usr/bin/env python3
"""
seed_demo_data.py — populate an EMPTY database with a fully synthetic storage
estate, so the dashboard can be explored without any real Diskover/Isilon
data (see the Docker demo in the README).

Unlike generate_mock_data.py, which clones a real snapshot forward, this
builds everything from scratch: areas across every tier, fictional group
leaders/projects/platforms/user homes, 18 monthly snapshots ending this
month, ~4 months of Isilon-style daily sizes, subdirectories, per-extension
totals, top files (with planted within-group and cross-group duplicates), and
one directory_group_rules link. All dates are relative to today, so the demo
always looks current.

Each directory's size comes from one deterministic curve (growth rate plus a
few step "events" — a cleanup, a migration to archive, a sudden surge), and
both the monthly snapshot rows and the daily Isilon rows are sampled from it,
so the two sources agree wherever charts merge them.

Refuses to run against a database that already has snapshots unless --force
is given, and even then only wipes data it can regenerate. Never point this
at a production database.

Usage
-----
    python demo/seed_demo_data.py            # seed if empty
    python demo/seed_demo_data.py --force    # wipe and reseed
"""

import argparse
import math
import os
import random
import re
import sys
import time
from datetime import date, timedelta

import mysql.connector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from import_csv import (  # noqa: E402  (path tweak above must run first)
    compute_and_store_cross_leader_duplicates,
    compute_and_store_duplicate_summaries,
)

TB = 1_000_000_000_000
GB = 1_000_000_000
MB = 1_000_000

SNAPSHOT_MONTHS = 18
ISILON_DAYS = 120
DEMO_NOTES = "[DEMO] Synthetic data — not a real crawl"

rng = random.Random(20260101)

# ── Areas ─────────────────────────────────────────────────────────────────────
# Labels exactly as diskover_complex.index_to_label() produces them, roots as
# in its INDEX_ROOT_PATHS, so short_area_label()/tier_bucket_for() in app.py
# treat them like the real thing. stale = (>1yr, >2yr, >4yr) fractions.
AREAS = {
    "legacy_groups":   ("JIC-Apricot / Primarydata Research Groups",   "/ifs/apricot/JIC/PrimaryData/RESEARCH-GROUPS",     (0.55, 0.38, 0.18)),
    "scratch_groups":  ("JIC-Apricot / Primarydata Group Scratch",     "/ifs/apricot/JIC/PrimaryData/GROUP_SCRATCH",       (0.22, 0.09, 0.02)),
    "archive_groups":  ("JIC-Apricot / Loanstorage Archive Groups",    "/ifs/apricot/JIC/LoanStorage/ARCHIVE/Groups",      (0.85, 0.66, 0.40)),
    "deep_groups":     ("JIC-Peach / Deeparchive Groups",              "/peach/deep-archive/groups",                       (0.96, 0.90, 0.72)),
    "legacy_projects": ("JIC-Apricot / Primarydata Research Projects", "/ifs/apricot/JIC/PrimaryData/RESEARCH_PROJECTS",   (0.50, 0.30, 0.12)),
    "scratch_projects":("JIC-Apricot / Primarydata Projects Scratch",  "/ifs/apricot/JIC/PrimaryData/PROJECTS_SCRATCH",    (0.18, 0.06, 0.01)),
    "main_platforms":  ("JIC-Apricot / Primarydata Platforms",         "/ifs/apricot/JIC/PrimaryData/PLATFORMS",           (0.45, 0.28, 0.10)),
    "scratch_platforms":("JIC-Apricot / Primarydata Platform Scratch", "/ifs/apricot/JIC/PrimaryData/PLATFORM_SCRATCH",    (0.20, 0.07, 0.01)),
    "archive_platforms":("JIC-Apricot / Loanstorage Archive Platforms","/ifs/apricot/JIC/LoanStorage/ARCHIVE/Platforms",   (0.88, 0.70, 0.45)),
    "instruments":     ("JIC-Apricot / Primarydata Instruments",       "/ifs/apricot/JIC/PrimaryData/INSTRUMENTS",         (0.35, 0.15, 0.03)),
    "user_homes":      ("JIC-Apricot / Primarydata User Homes",        "/ifs/apricot/JIC/PrimaryData/USER_HOMES",          (0.42, 0.27, 0.11)),
    "reference":       ("NBI-Apricot / Primarydata Reference Data",    "/ifs/apricot/NBI/PrimaryData/reference_data",      (0.30, 0.12, 0.04)),
}

GROUP_AREAS = ("legacy_groups", "scratch_groups", "archive_groups", "deep_groups")

# Historical plant scientists, so nobody mistakes these for real JIC groups.
# (base TB per group area, monthly growth rate)
LEADERS = {
    "Gregor-Mendel":      ({"legacy_groups": 210, "scratch_groups": 85, "archive_groups": 160, "deep_groups": 320}, 0.012),
    "Barbara-McClintock": ({"legacy_groups": 340, "scratch_groups": 60, "archive_groups": 90},  0.015),
    "Rosalind-Franklin":  ({"legacy_groups": 120, "scratch_groups": 140, "archive_groups": 40}, 0.022),
    "Charles-Darwin":     ({"legacy_groups": 260, "scratch_groups": 95, "deep_groups": 410},    0.010),
    "Nikolai-Vavilov":    ({"legacy_groups": 180, "scratch_groups": 70, "archive_groups": 220}, 0.018),
    "Carl-Linnaeus":      ({"legacy_groups": 75,  "scratch_groups": 20, "archive_groups": 30},  0.006),
    "Joseph-Hooker":      ({"legacy_groups": 150, "scratch_groups": 55},                         0.025),
    "Norman-Borlaug":     ({"legacy_groups": 290, "scratch_groups": 120, "archive_groups": 130, "deep_groups": 180}, 0.020),
    "Agnes-Arber":        ({"legacy_groups": 60,  "scratch_groups": 35, "archive_groups": 25},  0.009),
    "Jane-Colden":        ({"legacy_groups": 40,  "scratch_groups": 15},                         0.030),
}

OTHER_DIRS = {
    "legacy_projects":   {"wheat-pangenome": (310, 0.020), "field-trials-2024": (45, 0.012), "root-imaging-atlas": (95, 0.015)},
    "scratch_projects":  {"model_training": (70, 0.060), "metagenomics-survey": (40, 0.025)},
    "main_platforms":    {"Bioimaging": (180, 0.018), "Metabolomics": (65, 0.010), "Sequencing": (420, 0.022)},
    "scratch_platforms": {"Bioimaging": (55, 0.030), "Sequencing": (130, 0.035)},
    "archive_platforms": {"Sequencing": (390, 0.008), "Bioimaging": (120, 0.006)},
    "instruments":       {"Krios-CryoEM": (260, 0.035), "Lightsheet-01": (90, 0.028), "Orbitrap-Exploris": (25, 0.012)},
    "reference":         {"genomes": (35, 0.010), "blast_db": (28, 0.015), "uniprot": (6, 0.005)},
}

# Linked project directories (directory_group_rules): shows up under the
# leader's own views even though it isn't named after them.
GROUP_RULES = [
    ("scratch_projects", "model_training", "RG-Charles-Darwin", "Demo: deep-learning project owned by the Darwin group"),
    ("legacy_projects", "wheat-pangenome", "RG-Nikolai-Vavilov", "Demo: consortium project led by the Vavilov group"),
]


def _months_ago(today, n):
    y, m = today.year, today.month - n
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _safe_label(label):
    return re.sub(r'[^\w\-]', '_', label).strip('_')


def _username(r):
    letters = "abcdefghijklmnopqrstuvwxyz"
    return "".join(r.choice(letters) for _ in range(3)) + f"{r.randint(18, 25)}" + "".join(r.choice(letters) for _ in range(3))


# ── Size model ────────────────────────────────────────────────────────────────

class Directory:
    def __init__(self, area_key, name, base_tb, growth, start):
        self.area_key = area_key
        self.label, self.root, stale = AREAS[area_key]
        self.name = name
        self.path = f"{_safe_label(self.label)}/{name}"
        self.absolute_path = f"{self.root}/{name}"
        self.base = base_tb * TB * rng.uniform(0.9, 1.1)
        self.growth = growth * rng.uniform(0.7, 1.3)
        self.start = start
        self.stale = tuple(min(0.99, f * rng.uniform(0.8, 1.15)) for f in stale)
        self.events = []  # (date, multiplier applied from that date onward)
        self.phase = rng.uniform(0, 2 * math.pi)

    def size_on(self, d):
        t = (d - self.start).days / 30.44
        size = self.base * math.exp(self.growth * t)
        for when, mult in self.events:
            if d >= when:
                size *= mult
        # Small, smooth day-to-day wobble so daily charts aren't ruler-straight.
        size *= 1 + 0.004 * math.sin(t * 6.0 + self.phase)
        return int(size)

    def stale_on(self, d, size):
        drift = 1 + 0.004 * (d - self.start).days / 30.44
        s1, s2, s4 = (min(0.99, f * drift) for f in self.stale)
        s2, s4 = min(s2, s1), min(s4, s2, s1)
        return int(size * s1), int(size * s2), int(size * s4)


def build_directories(today):
    start = _months_ago(today, SNAPSHOT_MONTHS - 1)
    dirs = []
    for leader, (areas, growth) in LEADERS.items():
        for area_key, base_tb in areas.items():
            g = growth * (0.4 if area_key in ("archive_groups", "deep_groups") else 1.0)
            dirs.append(Directory(area_key, leader, base_tb, g, start))
    for area_key, entries in OTHER_DIRS.items():
        for name, (base_tb, growth) in entries.items():
            dirs.append(Directory(area_key, name, base_tb, growth, start))
    home_rng = random.Random(7)
    for user in ["demo"] + [_username(home_rng) for _ in range(30)]:
        dirs.append(Directory("user_homes", user, home_rng.lognormvariate(-1.2, 1.1), home_rng.uniform(0, 0.03), start))

    by = {(d.area_key, d.name): d for d in dirs}

    # Stories worth finding in the dashboard:
    # McClintock moved a third of Legacy into Archive four months ago (cleanup
    # shows as "reclaimed"; archive jumps by roughly the same amount).
    moved = _months_ago(today, 4) + timedelta(days=9)
    by[("legacy_groups", "Barbara-McClintock")].events.append((moved, 0.66))
    by[("archive_groups", "Barbara-McClintock")].events.append((moved, 2.25))
    # Growth Alerts compare against ~7 days ago and the start of this month,
    # so these all land in the last few days to be flagged on any date.
    # Mendel's scratch surged.
    by[("scratch_groups", "Gregor-Mendel")].events.append((today - timedelta(days=4), 1.38))
    # model_training keeps growing fast and just jumped again.
    by[("scratch_projects", "model_training")].events.append((today - timedelta(days=2), 1.30))
    # Franklin tidied up scratch (a shrink is flagged too).
    by[("scratch_groups", "Rosalind-Franklin")].events.append((today - timedelta(days=5), 0.72))
    # Cryo-EM had a big data-collection campaign two months ago.
    by[("instruments", "Krios-CryoEM")].events.append((_months_ago(today, 2) + timedelta(days=12), 1.25))
    # Sequencing scratch was purged six months ago.
    by[("scratch_platforms", "Sequencing")].events.append((_months_ago(today, 6) + timedelta(days=3), 0.55))

    return dirs, start


# ── Subdirectories, extensions and top files ──────────────────────────────────

SUBDIR_TEMPLATES = {
    "raw_data":      [("fastq.gz", 0.70, 4 * GB), ("bam", 0.20, 25 * GB), ("(none)", 0.10, 200 * MB)],
    "alignments":    [("bam", 0.55, 30 * GB), ("sam", 0.25, 60 * GB), ("bai", 0.05, 8 * MB), ("cram", 0.15, 12 * GB)],
    "assemblies":    [("fa", 0.40, 3 * GB), ("fasta.gz", 0.30, 1 * GB), ("gff3", 0.10, 200 * MB), ("kmc_suf", 0.15, 15 * GB), ("kmc_pre", 0.05, 2 * GB)],
    "imaging":       [("czi", 0.45, 6 * GB), ("tif", 0.35, 80 * MB), ("nd2", 0.20, 4 * GB)],
    "analysis":      [("h5", 0.30, 2 * GB), ("rds", 0.20, 400 * MB), ("csv", 0.15, 20 * MB), ("tsv", 0.10, 15 * MB),
                      ("ipynb", 0.02, 2 * MB), ("py", 0.01, 30_000), ("log", 0.07, 5 * MB), ("txt", 0.15, 40_000)],
    "scratch_tmp":   [("tmp", 0.25, 1 * GB), ("sam", 0.30, 50 * GB), ("bin", 0.20, 3 * GB), ("log", 0.10, 2 * MB), ("core", 0.15, 8 * GB)],
    "old_projects":  [("tar.gz", 0.50, 80 * GB), ("bam", 0.30, 20 * GB), ("zip", 0.20, 5 * GB)],
    "papers":        [("pdf", 0.30, 5 * MB), ("tif", 0.40, 50 * MB), ("xlsx", 0.10, 2 * MB), ("png", 0.20, 3 * MB)],
}

FILE_STEMS = {
    "fastq.gz": lambda r, i: f"S{r.randint(1, 96):03d}_L00{r.randint(1, 4)}_R{1 + i % 2}_001",
    "bam":      lambda r, i: f"sample_{r.randint(1, 99999):05d}.sorted",
    "sam":      lambda r, i: f"sample_{r.randint(1, 99999):05d}.aligned",
    "cram":     lambda r, i: f"sample_{r.randint(1, 99999):05d}",
    "fa":       lambda r, i: f"assembly_{r.randint(1, 9999)}_v{r.randint(1, 6)}",
    "fasta.gz": lambda r, i: f"contigs_run{r.randint(1, 9999)}",
    "kmc_suf":  lambda r, i: f"kmers_k{r.choice([21, 31, 51])}_{r.randint(1, 9999)}",
    "czi":      lambda r, i: f"plate{r.randint(1, 30)}_well{r.choice('ABCDEFGH')}{r.randint(1, 12)}",
    "nd2":      lambda r, i: f"timelapse_{r.randint(1, 99999):05d}",
    "h5":       lambda r, i: f"features_{r.randint(1, 9999)}",
    "tar.gz":   lambda r, i: f"project_backup_{2014 + r.randint(0, 8)}_{r.randint(1, 9999):04d}",
    "tmp":      lambda r, i: f"job_{r.randint(100000, 999999)}",
    "bin":      lambda r, i: f"index_cache_{r.randint(1, 99999)}",
    "core":     lambda r, i: f"core.{r.randint(1000, 99999)}",
}

# Planted duplicates: (leader, area_key, subdir, basename, size). Same name and
# (nearly) the same size → clustered by duplicate_finder.cluster_duplicate_files.
PLANTED_FILES = [
    # Cross-group: three groups each downloaded the same reference/database.
    ("Nikolai-Vavilov", "legacy_groups",  "assemblies", "IWGSC_RefSeq_v2.1_genome.fa", 14_620_000_000),
    ("Norman-Borlaug",  "legacy_groups",  "assemblies", "IWGSC_RefSeq_v2.1_genome.fa", 14_620_000_000),
    ("Joseph-Hooker",   "scratch_groups", "assemblies", "IWGSC_RefSeq_v2.1_genome.fa", 14_620_000_000),
    ("Rosalind-Franklin", "legacy_groups", "analysis",  "nr_2025-01.fasta.gz",         128_400_000_000),
    ("Gregor-Mendel",   "scratch_groups", "analysis",   "nr_2025-01.fasta.gz",         128_400_000_000),
    ("Agnes-Arber",     "legacy_groups",  "analysis",   "nr_2025-01.fasta.gz",         128_400_000_000),
    ("Charles-Darwin",  "legacy_groups",  "raw_data",   "uniref90.fasta",               46_900_000_000),
    ("Carl-Linnaeus",   "legacy_groups",  "raw_data",   "uniref90.fasta",               46_850_000_000),
    # Within-group: the same run kept in Legacy, Scratch and Archive.
    ("Barbara-McClintock", "legacy_groups",  "raw_data",    "maize_B73_WGS_run3.bam",  212_000_000_000),
    ("Barbara-McClintock", "archive_groups", "old_projects","maize_B73_WGS_run3.bam",  212_000_000_000),
    ("Barbara-McClintock", "scratch_groups", "alignments",  "maize_B73_WGS_run3.bam",  211_500_000_000),
    ("Norman-Borlaug",  "legacy_groups",  "raw_data",   "rust_isolates_pool.fastq.gz", 88_000_000_000),
    ("Norman-Borlaug",  "deep_groups",    "old_projects","rust_isolates_pool.fastq.gz", 88_000_000_000),
    ("Gregor-Mendel",   "legacy_groups",  "imaging",    "pea_seed_morphology_stack.czi", 34_000_000_000),
    ("Gregor-Mendel",   "scratch_groups", "imaging",    "pea_seed_morphology_stack.czi", 33_900_000_000),
]

TOP_FILES_PER_SUBDIR = 25


def subdirs_for(directory):
    r = random.Random(f"{directory.area_key}/{directory.name}")
    names = list(SUBDIR_TEMPLATES)
    if directory.area_key in ("archive_groups", "deep_groups"):
        pool = ["old_projects", "raw_data", "imaging", "assemblies", "papers"]
    elif directory.area_key == "scratch_groups":
        pool = ["scratch_tmp", "alignments", "analysis", "assemblies", "raw_data", "imaging"]
    else:
        pool = names
    chosen = r.sample(pool, k=min(len(pool), r.randint(4, 6)))
    for leader, area_key, subdir, _, _ in PLANTED_FILES:
        if leader == directory.name and area_key == directory.area_key and subdir not in chosen:
            chosen.append(subdir)
    weights = [r.uniform(0.5, 3.0) for _ in chosen]
    total = sum(weights) / 0.96  # leave ~4% as loose files at the leader root
    return [(name, w / total) for name, w in zip(chosen, weights)]


def _extension_of(name):
    """Same convention as diskover_complex._extension_of(): "x.fastq.gz" -> "fastq.gz"."""
    parts = name.split(".")
    last = parts[-1].lower()
    if last in ("gz", "bz2", "xz", "zst") and len(parts) >= 3:
        return f"{parts[-2].lower()}.{last}"
    return last


def _old_date(r, today, stale_fraction):
    if r.random() < stale_fraction:
        return today - timedelta(days=r.randint(400, 3000))
    return today - timedelta(days=r.randint(3, 360))


def insert_group_detail(cur, snapshot_id, directory, dir_size, run_date, today, with_files):
    planted = [p for p in PLANTED_FILES if p[0] == directory.name and p[1] == directory.area_key]
    for subdir, share in subdirs_for(directory):
        sub_size = int(dir_size * share)
        sub_path = f"{directory.path}/{subdir}"
        cur.execute(
            """
            INSERT INTO subdirectory_stats
              (snapshot_id, directory, parent_path, subdirectory, path, index_label, dir_size_raw, dir_size_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (snapshot_id, directory.name, directory.path, subdir, sub_path, directory.label,
             f"{sub_size / TB:.2f} TB", sub_size),
        )

        r = random.Random(f"{sub_path}/{run_date}")
        ext_rows = []
        for ext, frac, avg in SUBDIR_TEMPLATES[subdir]:
            ext_size = int(sub_size * frac * r.uniform(0.8, 1.2))
            count = max(1, int(ext_size / (avg * r.uniform(0.6, 1.4))))
            ext_rows.append((snapshot_id, directory.name, sub_path, directory.label, ext, count, ext_size))
        # A handful of directories with a huge number of tiny files, for the
        # "most files" leaderboard.
        if r.random() < 0.15:
            ext_rows.append((snapshot_id, directory.name, sub_path, directory.label, "txt",
                             r.randint(1_500_000, 6_000_000), r.randint(20, 120) * GB))
        cur.executemany(
            """
            INSERT INTO dir_extension_stats
              (snapshot_id, directory, path, index_label, extension, file_count, total_size_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            ext_rows,
        )

        if not with_files:
            continue
        files = [(basename, _extension_of(basename), size)
                 for _, _, sd, basename, size in planted if sd == subdir]
        big_exts = [(e, avg) for e, _, avg in SUBDIR_TEMPLATES[subdir] if avg >= 100 * MB] or \
                   [(e, avg) for e, _, avg in SUBDIR_TEMPLATES[subdir]]
        for i in range(TOP_FILES_PER_SUBDIR - len(files)):
            ext, avg = r.choice(big_exts)
            stem = FILE_STEMS.get(ext, lambda rr, ii: f"file_{rr.randint(1, 999999):06d}")(r, i)
            basename = stem if ext == "(none)" else f"{stem}.{ext}"
            files.append((basename, ext, int(avg * r.lognormvariate(0.6, 0.7))))
        files.sort(key=lambda f: -f[2])
        cur.executemany(
            """
            INSERT INTO dir_top_files
              (snapshot_id, directory, path, index_label, file_path, extension, size_bytes, mtime, `rank`)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                (snapshot_id, directory.name, sub_path, directory.label,
                 f"{directory.absolute_path}/{subdir}/{basename}", ext, size,
                 _old_date(r, today, directory.stale[0]).isoformat(), rank)
                for rank, (basename, ext, size) in enumerate(files, start=1)
            ],
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def connect(retries=60):
    from config import DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME
    for attempt in range(retries):
        try:
            return mysql.connector.connect(
                host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
                database=DB_NAME, charset="utf8mb4", autocommit=False,
            )
        except mysql.connector.Error as exc:
            if attempt == retries - 1:
                raise
            print(f"  waiting for database ({exc.msg}) …", flush=True)
            time.sleep(2)


def wipe(cur):
    for table in ("cross_leader_duplicate_clusters", "leader_duplicate_summary", "dir_top_files",
                  "dir_extension_stats", "subdirectory_stats", "directory_stats", "snapshots",
                  "isilon_quota_stats", "directory_group_rules"):
        cur.execute(f"DELETE FROM {table}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="wipe existing data and reseed")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM snapshots")
    if cur.fetchone()[0] and not args.force:
        print("Database already has snapshots — skipping demo seed (use --force to reseed).")
        return
    wipe(cur)

    today = date.today()
    dirs, start = build_directories(today)
    run_dates = [_months_ago(today, n) for n in range(SNAPSHOT_MONTHS - 1, -1, -1)]

    print(f"Seeding {len(dirs)} directories × {len(run_dates)} monthly snapshots …", flush=True)
    for i, run_date in enumerate(run_dates):
        cur.execute("INSERT INTO snapshots (run_date, notes) VALUES (%s, %s)", (run_date, DEMO_NOTES))
        snapshot_id = cur.lastrowid
        is_recent = i >= len(run_dates) - 2  # subdir detail only for the two latest, like a fresh install
        is_latest = i == len(run_dates) - 1
        rows = []
        for d in dirs:
            size = d.size_on(run_date)
            s1, s2, s4 = d.stale_on(run_date, size)
            rows.append((snapshot_id, d.name, d.path, d.label, f"{size / TB:.2f} TB", size, s1, s2, s4))
            if is_recent and d.area_key in GROUP_AREAS:
                insert_group_detail(cur, snapshot_id, d, size, run_date, today, with_files=is_latest)
        cur.executemany(
            """
            INSERT INTO directory_stats
              (snapshot_id, directory, path, index_label, dir_size_raw, dir_size_bytes,
               stale_1yr_bytes, stale_2yr_bytes, stale_4yr_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            rows,
        )

    print(f"Seeding {ISILON_DAYS} days of Isilon daily sizes …", flush=True)
    for offset in range(ISILON_DAYS, -1, -1):
        d = today - timedelta(days=offset)
        rows = []
        for dr in dirs:
            size = dr.size_on(d)
            is_user = dr.area_key == "user_homes"
            rows.append((
                d, dr.label, "user" if is_user else "directory", dr.path,
                dr.root if is_user else dr.absolute_path,
                size, int(size * 1.28), max(1, size // (40 * MB)),
            ))
        cur.executemany(
            """
            INSERT INTO isilon_quota_stats
              (stat_date, area_label, entity_type, path, absolute_path, logical_bytes, physical_bytes, inode_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            rows,
        )

    for area_key, name, group_cn, notes in GROUP_RULES:
        label = AREAS[area_key][0]
        cur.execute(
            "INSERT INTO directory_group_rules (index_label, path_prefix, ldap_group_cn, notes) VALUES (%s,%s,%s,%s)",
            (label, f"{_safe_label(label)}/{name}", group_cn, notes),
        )

    print("Computing duplicate summaries …", flush=True)
    cur.execute("SELECT id FROM snapshots ORDER BY run_date DESC LIMIT 1")
    latest_id = cur.fetchone()[0]
    compute_and_store_duplicate_summaries(cur, latest_id)
    compute_and_store_cross_leader_duplicates(cur, latest_id)

    conn.commit()
    print("Demo data ready.", flush=True)


if __name__ == "__main__":
    main()
