#!/usr/bin/env python3
"""
Diskover Dashboard — Flask web app.

Start:
  export DISKOVER_DB_PASS=yourpassword
  python app.py
  # or with gunicorn:
  gunicorn -w 2 -b 0.0.0.0:5000 app:app
"""

import calendar
import os
from datetime import date, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, jsonify, session, redirect, url_for,
    flash, send_from_directory,
)
import mysql.connector
from ldap3 import Server, Connection, ALL
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from config import APP_SECRET_KEY, get_db_config, get_ldap_config
from duplicate_finder import cluster_duplicate_files

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    # Not forced on unconditionally — this README doesn't confirm the
    # deployment terminates TLS in front of the app, and Secure=True would
    # silently stop the session cookie being sent back (breaking login)
    # if it's ever actually served over plain HTTP. Set
    # SESSION_COOKIE_SECURE=1 in the environment once HTTPS is confirmed.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
)

DB = get_db_config()
LDAP = get_ldap_config()

DOCUMENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "documents")
DATA_MANAGEMENT_PRESENTATION = "data_management_2026.pptx"

REQUIRED_LDAP_KEYS = (
    "host",
    "base_dn",
    "bind_user_dn",
    "bind_user_password",
    "allowed_group_dn",
    "required_group_dns",
)

AREA_PREFIXES = (
    "JIC-Apricot / Primarydata ",
    "JIC Apricot / Primarydata ",
    "JIC-Apricot / Loanstorage Archive ",
    "JIC Apricot / Loanstorage Archive ",
)

# Friendly "Tier - Category" display names for the areas that fit that grid.
# Keyed on the raw index_label exactly as diskover_complex.py's index_to_label()
# generates it; other areas (User Homes, Instruments, Services, etc.) fall
# through to the generic AREA_PREFIXES stripping below, unchanged.
AREA_DISPLAY_OVERRIDES = {
    # Displayed as "Legacy" (was "Main") — this tier predates the current
    # storage layout and is no longer where new data should go, but the
    # underlying index_label/database values are untouched, display-only.
    "JIC-Apricot / Primarydata Research Groups":   "Legacy - Groups",
    "JIC Apricot / Primarydata Research Groups":   "Legacy - Groups",
    "JIC-Apricot / Primarydata Research Projects": "Legacy - Projects",
    "JIC Apricot / Primarydata Research Projects": "Legacy - Projects",

    # Exception: kept as "Main" rather than renamed to "Legacy" like the rest
    # of Primarydata above — still bucketed together with Legacy in tier
    # charts (see tier_bucket_for()'s TIER_BUCKET_ALIASES), so this is a
    # display-only difference, not a new tier in Institute Trends etc.
    "JIC-Apricot / Primarydata Platforms":         "Main - Platforms",
    "JIC Apricot / Primarydata Platforms":         "Main - Platforms",

    "JIC-Apricot / Primarydata Group Scratch":    "Scratch - Groups",
    "JIC Apricot / Primarydata Group Scratch":    "Scratch - Groups",
    "JIC-Apricot / Primarydata Platform Scratch": "Scratch - Platforms",
    "JIC Apricot / Primarydata Platform Scratch": "Scratch - Platforms",
    "JIC-Apricot / Primarydata Projects Scratch": "Scratch - Projects",
    "JIC Apricot / Primarydata Projects Scratch": "Scratch - Projects",

    "JIC-Apricot / Loanstorage Archive Groups":    "Archive - Groups",
    "JIC Apricot / Loanstorage Archive Groups":    "Archive - Groups",
    "JIC-Apricot / Loanstorage Archive Platforms": "Archive - Platforms",
    "JIC Apricot / Loanstorage Archive Platforms": "Archive - Platforms",
    "JIC-Apricot / Loanstorage Archive Projects":  "Archive - Projects",
    "JIC Apricot / Loanstorage Archive Projects":  "Archive - Projects",

    "JIC-Peach / Deeparchive Groups":    "Deep Archive - Groups",
    "JIC Peach / Deeparchive Groups":    "Deep Archive - Groups",
    "JIC-Peach / Deeparchive Platforms": "Deep Archive - Platforms",
    "JIC Peach / Deeparchive Platforms": "Deep Archive - Platforms",
    "JIC-Peach / Deeparchive Projects":  "Deep Archive - Projects",
    "JIC Peach / Deeparchive Projects":  "Deep Archive - Projects",
}

USER_HOME_PATTERNS = (
    "%User Homes%",
    "%User_Homes%",
)

GROUP_AREA_PATTERNS = (
    "%Research Groups%",
    "%Group Scratch%",
    "%Archive Groups%",
    "%Groups%",
)

# Allowlist mapping a request's ?metric= value to a real directory_stats
# column — never interpolate the raw request value into SQL directly.
LEADER_TIER_METRICS = {
    "size": "dir_size_bytes",
    "stale1": "stale_1yr_bytes",
    "stale2": "stale_2yr_bytes",
    "stale4": "stale_4yr_bytes",
}

GROWTH_ALERT_THRESHOLD_PCT = 10  # flag a directory only if it moved by at least this %...
GROWTH_ALERT_THRESHOLD_BYTES = 50_000_000_000  # ...AND at least this many bytes (50 GB) — avoids noise from tiny directories

GROUP_CN_PREFIXES = (
    "RG-",
    "RGGRANT-",
    "ARCHIVE-JIC-",
    "ARCHIVE-",
)

RG_SUGGESTION_PREFIXES = (
    "RG-",
)

# ── File-extension storage action plan (traffic light) ──────────────────────
# red    = ACT    — normally a delete/migrate/convert candidate (temporary,
#                    intermediate, redundant, generated, obsolete)
# yellow = WATCH  — may be legitimate, needs review/context before acting
# green  = KEEP   — normally legitimate research data/output, no action
#                    warranted from the extension alone
# Keyed on the lowercased extension exactly as _extension_of()/dir_extension_stats
# store it. Anything not listed here is unclassified.
#
# This dict is SEED DATA ONLY, loaded into the extension_action_plan DB table
# the first time it's empty (see seed_extension_action_plan()) — the live,
# admin-editable source of truth from then on is that table, editable at
# /settings/extensions. Editing this dict after the table's already been
# seeded has no effect; it's kept only as the original starting policy.
EXTENSION_ACTION_PLAN_SEED = {
    # ── RED — regenerable indexes / k-mer databases / build & transient junk
    "kmc_suf": ("red", "KMC k-mer counting database file — internal working file, safe to delete once the counting job is done."),
    "kmc_pre": ("red", "KMC k-mer counting database file — internal working file, safe to delete once the counting job is done."),
    "sam": ("red", "Uncompressed alignment — almost always superseded by a sorted, indexed .bam of the same alignment."),
    "bin": ("red", "Generic binary cache/index file — usually a tool's intermediate working file, not a data product."),
    "log": ("red", "Run log — useful right after a job, rarely needed months later."),
    "tmp": ("red", "Explicitly named as temporary."),
    "temp": ("red", "Explicitly named as temporary."),
    "swp": ("red", "Editor swap file — never meant to persist."),
    "pyc": ("red", "Compiled Python bytecode — regenerated automatically, never a data product."),
    "o": ("red", "Compiled object file — regenerated by the next build, never a data product."),
    "part": ("red", "Incomplete download/transfer artifact — not a usable file on its own."),
    "partial": ("red", "Incomplete download/transfer artifact — not a usable file on its own."),
    "crdownload": ("red", "Incomplete browser download artifact."),
    "lock": ("red", "Process lock file — meaningless once the process has ended."),
    "cache": ("red", "Explicitly named as a cache — regenerable by definition."),
    "bai": ("red", "Index for a .bam — regenerated from it in seconds, useless without it."),
    "csi": ("red", "Index for a .bam/.vcf — regenerated from it in seconds, useless without it."),
    "tbi": ("red", "Index for a .vcf/.bed — regenerated from it in seconds, useless without it."),
    "fai": ("red", "Index for a .fa/.fasta — regenerated from it in seconds, useless without it."),
    "idx": ("red", "Generic index file — regenerable from the file it indexes."),
    "sai": ("red", "Legacy BWA intermediate index, pre-dates the real alignment output."),
    "ht2l": ("red", "HISAT2 aligner index — regenerable from the reference FASTA."),
    "bt2l": ("red", "Bowtie2 aligner index — regenerable from the reference FASTA."),
    "ebwtl": ("red", "Bowtie (legacy) aligner index — regenerable from the reference FASTA."),
    "bwt": ("red", "BWA aligner index component — regenerable from the reference FASTA."),
    "sa": ("red", "Suffix array index component — regenerable from the file it indexes."),
    "mmi": ("red", "minimap2 aligner index — regenerable from the reference FASTA."),
    "yak": ("red", "Yak k-mer counting database — intermediate, regenerable from the raw reads."),
    "jf": ("red", "Jellyfish k-mer counting database — intermediate, regenerable from the raw reads."),
    "k31": ("red", "K-mer database (k=31) — intermediate, regenerable from the raw reads."),
    "kmers": ("red", "K-mer output — intermediate, regenerable from the raw reads."),
    "nsq": ("red", "BLAST nucleotide database index component — regenerable via makeblastdb."),
    "phr": ("red", "BLAST protein database index component — regenerable via makeblastdb."),
    "psq": ("red", "BLAST protein database index component — regenerable via makeblastdb."),
    "ref153positions": ("red", "Naming resembles a reference-genome index/lookup structure — likely regenerable; unverified, confirm the generating tool first."),
    "ref153positionsh": ("red", "Naming resembles a reference-genome index/lookup structure — likely regenerable; unverified, confirm the generating tool first."),
    "ref153offsets64strm": ("red", "Naming resembles a reference-genome index/lookup structure — likely regenerable; unverified, confirm the generating tool first."),
    "genomebits128": ("red", "Naming resembles a compact/compressed genome index — likely regenerable; unverified, confirm the generating tool first."),
    "genomecomp": ("red", "Naming resembles a compact/compressed genome index — likely regenerable; unverified, confirm the generating tool first."),

    # ── YELLOW — needs context before acting
    "gz": ("yellow", "Generic compression wrapper — could be primary raw data or a disposable intermediate; the extension alone doesn't say which."),
    "tar": ("yellow", "Generic archive bundle — contents unknown from the extension alone."),
    "tgz": ("yellow", "Generic archive bundle — contents unknown from the extension alone."),
    "zip": ("yellow", "Generic archive bundle — contents unknown from the extension alone."),
    "bz2": ("yellow", "Generic compression wrapper — contents unknown from the extension alone."),
    "xz": ("yellow", "Generic compression wrapper — contents unknown from the extension alone."),
    "zst": ("yellow", "Generic compression wrapper — contents unknown from the extension alone."),
    "fa": ("yellow", "Sequence data — a public reference/database copy is re-downloadable; a lab's own curated sequence is not."),
    "fasta": ("yellow", "Sequence data — a public reference/database copy is re-downloadable; a lab's own curated sequence is not."),
    "fna": ("yellow", "Sequence data — a public reference/database copy is re-downloadable; a lab's own curated sequence is not."),
    "faa": ("yellow", "Protein sequence data — same reasoning as .fa: public reference vs in-house."),
    "gbk": ("yellow", "GenBank reference/annotation — same reasoning as .fa: public reference vs in-house."),
    "gbff": ("yellow", "GenBank reference/annotation — same reasoning as .fa: public reference vs in-house."),
    "tif": ("yellow", "Could be original instrument output (keep) or a generated preview/thumbnail (delete)."),
    "png": ("yellow", "Could be a final figure (keep) or an auto-generated preview (delete)."),
    "jpg": ("yellow", "Could be a final figure (keep) or an auto-generated preview (delete)."),
    "jpeg": ("yellow", "Could be a final figure (keep) or an auto-generated preview (delete)."),
    "bam": ("yellow", "Often a real deliverable, but also commonly an intermediate superseded by a later filtered/merged BAM — check whether it's the final version."),
    "fq": ("yellow", "Raw reads — could be irreplaceable in-house sequencing, or a re-downloadable cached copy of public data (SRA/ENA). Check provenance."),
    "fastq": ("yellow", "Raw reads — could be irreplaceable in-house sequencing, or a re-downloadable cached copy of public data (SRA/ENA). Check provenance."),
    "bak": ("yellow", "Explicitly a backup copy — likely redundant if the primary file still exists; confirm before deleting the only surviving copy."),
    "out": ("yellow", "Ambiguous — often job-scheduler stdout (treat like .log), but some pipelines name real results .out."),
    "err": ("yellow", "Usually job-scheduler stderr — treat like .log once the job's been checked."),
    "h5": ("yellow", "Structured data container used for anything from raw instrument output to ML training caches — contents vary too much to default."),
    "hdf5": ("yellow", "Structured data container used for anything from raw instrument output to ML training caches — contents vary too much to default."),
    "rds": ("yellow", "Saved R object — could be a genuinely valuable curated dataset or just a saved workspace from a one-off script."),
    "rdata": ("yellow", "Saved R workspace — could be a genuinely valuable curated dataset or just a saved workspace from a one-off script."),
    "mzml": ("yellow", "Mass-spec format usually converted from .raw — redundant if the original .raw survives, may be the only copy if not."),
    "mzxml": ("yellow", "Mass-spec format usually converted from .raw — redundant if the original .raw survives, may be the only copy if not."),
    "sra": ("yellow", "NCBI SRA archive — almost always a downloaded copy of public data; re-downloadable."),
    "paf": ("yellow", "Alignment output (minimap2) — a real result, but usually cheap to regenerate from the reads."),
    "gaf": ("yellow", "Pangenome/graph alignment output — a real result, but often cheap to regenerate from the reads."),
    "coverage": ("yellow", "Coverage summary — a real result, but usually cheap to regenerate from the BAM if space is needed."),
    "pileup": ("yellow", "Pileup summary — a real result, but usually cheap to regenerate from the BAM if space is needed."),
    "bw": ("yellow", "BigWig signal track — a real result, but usually cheap to regenerate from the BAM."),
    "delta": ("yellow", "Whole-genome alignment output (MUMmer) — often an intermediate toward a final synteny/SNP call."),
    "sif": ("yellow", "Singularity container image — losing it means rebuilding a software environment, not losing data."),
    "txt": ("yellow", "Normally fine as plain text/results, but check volume — an unusually large number of .txt files can indicate per-sample logs or intermediate dumps rather than real documentation."),
    "db": ("yellow", "Generic database file — could be a legitimate curated database or a regenerable cache; needs a direct look."),
    "map": ("yellow", "Ambiguous mapping/lookup file — needs a direct look."),
    "rf": ("yellow", "Unrecognized — needs a direct look."),
    "mgxm": ("yellow", "Unrecognized — needs a direct look."),
    "mgxs": ("yellow", "Unrecognized — needs a direct look."),
    "x": ("yellow", "Too generic to classify — needs a direct look at real examples."),
    "mpi": ("yellow", "Unrecognized — worth checking, especially since this tends to appear as one very large file rather than many small ones."),
    "psd": ("yellow", "Usually Adobe Photoshop, but unusual in this context — could be a scientific format reusing the extension. Needs a direct look."),
    "sorted": ("yellow", "Likely a naming-convention artifact (e.g. sample.bam.sorted) rather than a true file type — the real type is probably masked."),
    "sync": ("yellow", "Most likely PoPoolation2 pooled-sequencing output — a real result if so, but moderately cheap to regenerate; confirm before deleting."),
    "(none)": ("yellow", "No extension at all — too generic to classify without looking at what these files actually are."),

    # ── GREEN — normally legitimate, no action from the extension alone
    "czi": ("green", "Zeiss microscope native raw image format — original instrument output, effectively irreplaceable."),
    "nd2": ("green", "Nikon microscope native raw image format — original instrument output, effectively irreplaceable."),
    "lif": ("green", "Leica microscope native raw image format — original instrument output, effectively irreplaceable."),
    "oib": ("green", "Olympus microscope native raw image format — original instrument output, effectively irreplaceable."),
    "oif": ("green", "Olympus microscope native raw image format — original instrument output, effectively irreplaceable."),
    "lsm": ("green", "Zeiss (legacy) microscope native raw image format — original instrument output, effectively irreplaceable."),
    "vcf": ("green", "Variant calls — usually a real, study-specific analysis output."),
    "bed": ("green", "Genomic feature/annotation file — usually a real analysis output or reference annotation."),
    "gff": ("green", "Genomic feature/annotation file — usually a real analysis output or reference annotation."),
    "gff3": ("green", "Genomic feature/annotation file — usually a real analysis output or reference annotation."),
    "gtf": ("green", "Genomic feature/annotation file — usually a real analysis output or reference annotation."),
    "cram": ("green", "Compressed alignment, the modern lower-storage alternative to .bam — usually the deliberate long-term copy."),
    "fast5": ("green", "Oxford Nanopore raw signal data — primary instrument output; regenerating means re-sequencing, not re-computing."),
    "gfa": ("green", "Genome assembly graph — a real, compute-intensive assembly output."),
    "bedcov": ("green", "Per-region coverage output — a specific analysis result, not a generic intermediate."),
    "tab": ("green", "Tabular results."),
    "raw": ("green", "Mass-spec instrument output — regenerating means re-running the physical sample, not just re-computing."),
    "arw": ("green", "Sony camera RAW — plausible specimen/phenotyping photography; primary instrument data if so. Confirm it isn't something else reusing the extension."),
    "svg": ("green", "Vector graphics — almost always a deliberately produced figure."),
    "pdf": ("green", "Report/paper — normal documentation, not a cleanup target."),
    "docx": ("green", "Document — normal documentation, not a cleanup target."),
    "doc": ("green", "Document — normal documentation, not a cleanup target."),
    "xlsx": ("green", "Spreadsheet — normal documentation, not a cleanup target."),
    "xls": ("green", "Spreadsheet — normal documentation, not a cleanup target."),
    "csv": ("green", "Structured results/metadata — small, usually meaningful."),
    "tsv": ("green", "Structured results/metadata — small, usually meaningful."),
    "json": ("green", "Structured results/metadata/config — small, usually meaningful."),
    "yaml": ("green", "Config — small, usually meaningful."),
    "yml": ("green", "Config — small, usually meaningful."),
    "py": ("green", "Source code — the analysis logic itself; keep for reproducibility."),
    "r": ("green", "Source code — the analysis logic itself; keep for reproducibility."),
    "sh": ("green", "Source code — the analysis logic itself; keep for reproducibility."),
    "ipynb": ("green", "Notebook — the analysis logic itself; keep for reproducibility."),
}


EXTENSION_CATEGORIES = ("red", "yellow", "green")


def ensure_extension_action_plan_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS extension_action_plan (
            extension  VARCHAR(255) PRIMARY KEY,
            category   ENUM('red','yellow','green') NOT NULL,
            phrase     VARCHAR(1000),
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
        """
    )


def seed_extension_action_plan():
    """One-time seed from EXTENSION_ACTION_PLAN_SEED — no-ops once the table
    has any rows at all, so admin edits (including deletions) always stick."""
    ensure_extension_action_plan_table()
    count = query_value("SELECT COUNT(*) AS c FROM extension_action_plan", default=0)
    if count:
        return
    for extension, (category, phrase) in EXTENSION_ACTION_PLAN_SEED.items():
        execute(
            "INSERT IGNORE INTO extension_action_plan (extension, category, phrase) VALUES (%s, %s, %s)",
            (extension, category, phrase),
        )


def load_extension_action_plan():
    """Load the whole table once (used by /api/extension_totals so it isn't
    a DB round-trip per extension in that endpoint's loop)."""
    seed_extension_action_plan()
    rows = query("SELECT extension, category, phrase FROM extension_action_plan")
    return {r["extension"]: (r["category"], r["phrase"]) for r in rows}


def classify_extension(extension, plan=None):
    if plan is None:
        plan = load_extension_action_plan()
    return plan.get((extension or "").lower())


def _effective_stale_bytes(row, stale_bytes_key):
    explicit = row.get(stale_bytes_key)
    if explicit is not None:
        return int(explicit)
    return None


def short_area_label(label):
    if not label:
        return ""
    if label in AREA_DISPLAY_OVERRIDES:
        return AREA_DISPLAY_OVERRIDES[label]
    for prefix in AREA_PREFIXES:
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


def human_bytes(n):
    if n is None:
        return "—"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1e12:
        return f"{sign}{n / 1e12:.2f} TB"
    if n >= 1e9:
        return f"{sign}{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{sign}{n / 1e6:.0f} MB"
    if n >= 1e3:
        return f"{sign}{n / 1e3:.0f} KB"
    return f"{sign}{n:.0f} B"


def is_user_home_label(label):
    value = (label or "").lower()
    return "user homes" in value or "user_homes" in value


def is_group_area_label(label):
    value = (label or "").lower()
    return any(pattern.strip("%").lower() in value for pattern in GROUP_AREA_PATTERNS)


def normalise_area_key(label):
    return "".join(ch for ch in short_area_label(label).lower() if ch.isalnum())


def get_db():
    return mysql.connector.connect(**DB)


def query(sql, params=(), one=False):
    conn = get_db()
    cur  = conn.cursor(dictionary=True)
    cur.execute(sql, params)
    rows = cur.fetchone() if one else cur.fetchall()
    cur.close()
    conn.close()
    return rows


def execute(sql, params=()):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    cur.close()
    conn.close()


def query_value(sql, params=(), default=None):
    row = query(sql, params, one=True)
    if not row:
        return default
    return next(iter(row.values()))


def ensure_area_settings_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS area_settings (
            index_label VARCHAR(300) PRIMARY KEY,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
        """
    )


def ensure_access_control_tables():
    execute(
        """
        CREATE TABLE IF NOT EXISTS full_access_users (
            username VARCHAR(120) PRIMARY KEY,
            notes VARCHAR(255),
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS directory_group_rules (
            id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            index_label VARCHAR(300) NOT NULL,
            path_prefix VARCHAR(1000) NOT NULL,
            ldap_group_cn VARCHAR(255) NOT NULL,
            notes VARCHAR(255),
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_dir_group_rule (index_label, path_prefix(255), ldap_group_cn)
        ) ENGINE=InnoDB
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS hidden_group_leaders (
            directory VARCHAR(500) PRIMARY KEY,
            notes VARCHAR(255),
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
        """
    )


def ensure_admin_tables():
    ensure_area_settings_table()
    ensure_access_control_tables()


def sync_area_settings():
    ensure_area_settings_table()
    execute(
        """
        INSERT INTO area_settings (index_label, enabled)
        SELECT DISTINCT ds.index_label, 1
        FROM directory_stats ds
        LEFT JOIN area_settings aset ON aset.index_label = ds.index_label
        WHERE aset.index_label IS NULL
        """
    )


def get_all_area_settings():
    sync_area_settings()
    return query(
        """
        SELECT aset.index_label, aset.enabled
        FROM area_settings aset
        ORDER BY aset.index_label
        """
    )


def get_enabled_area_labels(snapshot_id=None):
    sync_area_settings()
    params = []
    sql = """
        SELECT DISTINCT ds.index_label
        FROM directory_stats ds
        JOIN area_settings aset ON aset.index_label = ds.index_label
        WHERE aset.enabled = 1
    """
    access_sql, access_params = build_access_sql("ds")
    sql += f" AND ({access_sql})"
    params.extend(access_params)
    if snapshot_id is not None:
        sql += " AND ds.snapshot_id = %s"
        params.append(snapshot_id)
    sql += " ORDER BY ds.index_label"
    rows = query(sql, tuple(params))
    return [row["index_label"] for row in rows]


def has_stale_size_columns():
    rows = query(
        """
        SELECT COUNT(*) AS col_count
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = 'directory_stats'
          AND column_name IN ('stale_1yr_bytes', 'stale_2yr_bytes', 'stale_4yr_bytes')
        """,
        (DB["database"],),
        one=True,
    )
    return int(rows["col_count"] or 0) == 3


def isilon_daily_totals(paths):
    """Return {date: total_logical_bytes} from isilon_quota_stats for the given paths."""
    if not paths:
        return {}
    placeholders = ", ".join(["%s"] * len(paths))
    rows = query(
        f"""
        SELECT stat_date, SUM(logical_bytes) AS total_bytes
        FROM isilon_quota_stats
        WHERE path IN ({placeholders})
        GROUP BY stat_date
        """,
        tuple(paths),
    )
    return {row["stat_date"]: int(row["total_bytes"] or 0) for row in rows}


def build_snapshot_timeseries(where_sql, params):
    access_sql, access_params = build_access_sql("ds")
    full_params = tuple(params) + tuple(access_params)
    rows = query(
        f"""
        SELECT s.run_date,
               SUM(ds.dir_size_bytes) AS size_bytes,
               SUM(ds.stale_1yr_bytes) AS stale_1yr_bytes,
               SUM(ds.stale_2yr_bytes) AS stale_2yr_bytes,
               SUM(ds.stale_4yr_bytes) AS stale_4yr_bytes
        FROM directory_stats ds
        JOIN snapshots s ON s.id = ds.snapshot_id
        WHERE ({where_sql})
          AND ({access_sql})
        GROUP BY s.run_date
        ORDER BY s.run_date
        """,
        full_params,
    )

    # Merge in Isilon's daily SmartQuotas size data for the same set of paths
    # this aggregate covers — it's continuously tracked by OneFS (more
    # accurate, far more frequent than the monthly Diskover crawl), though it
    # never carries staleness. `paths` is drawn from the same access_sql-
    # filtered directory_stats query, so it's never broader than what this
    # user can already see, regardless of whether `rows` itself is empty.
    paths = [
        r["path"] for r in query(
            f"SELECT DISTINCT ds.path AS path FROM directory_stats ds "
            f"WHERE ({where_sql}) AND ({access_sql})",
            full_params,
        )
    ]
    daily_totals = isilon_daily_totals(paths)
    if daily_totals:
        by_date = {r["run_date"]: r for r in rows}
        for stat_date, total_bytes in daily_totals.items():
            entry = by_date.get(stat_date)
            if entry is None:
                entry = {
                    "run_date": stat_date,
                    "size_bytes": None,
                    "stale_1yr_bytes": None,
                    "stale_2yr_bytes": None,
                    "stale_4yr_bytes": None,
                }
                by_date[stat_date] = entry
            entry["size_bytes"] = total_bytes
        rows = [by_date[k] for k in sorted(by_date)]

    for row in rows:
        row["run_date"] = row["run_date"].isoformat()
        row["size_tb"] = round(float(row["size_bytes"] or 0) / 1e12, 3)
        # Preserve a real gap (None) instead of coercing to 0 — a daily-only
        # point genuinely has no staleness data, which isn't the same as zero
        # stale bytes. Chart.js treats null data points as a gap by default.
        row["stale_1yr_tb"] = None if row["stale_1yr_bytes"] is None else round(float(row["stale_1yr_bytes"]) / 1e12, 3)
        row["stale_2yr_tb"] = None if row["stale_2yr_bytes"] is None else round(float(row["stale_2yr_bytes"]) / 1e12, 3)
        row["stale_4yr_tb"] = None if row["stale_4yr_bytes"] is None else round(float(row["stale_4yr_bytes"]) / 1e12, 3)
    return rows


def build_leader_tier_timeseries(leader_name, metric_column):
    """One leader's tiered timeseries — see build_group_tier_timeseries()."""
    return build_group_tier_timeseries(metric_column, leader_name=leader_name)


def build_all_groups_tier_timeseries(metric_column):
    """
    Every group leader combined, tiered the same way as
    build_leader_tier_timeseries() — the admin-only "All groups combined"
    chart. Still access-filtered (defense in depth): a non-admin calling
    this directly would just get their own restricted subset, same as
    everywhere else, even though the UI only ever shows it to admins.
    """
    return build_group_tier_timeseries(metric_column, leader_name=None)


def build_group_tier_timeseries(metric_column, leader_name=None):
    """
    A chosen metric (total size, or one staleness threshold) for group/PI
    directories, broken down by storage tier (Archive/Legacy/Scratch — the
    same short labels short_area_label() produces elsewhere) instead of
    summed into a single combined line. Scoped to one leader if leader_name
    is given (including any linked project directories outside the normal
    Groups-type areas — see leader_scope_sql()), or combined across every
    leader's own Groups-type directories (not their linked projects) the
    caller can see otherwise.

    metric_column must already be validated against LEADER_TIER_METRICS by
    the caller — it's interpolated directly into the SQL.
    """
    access_sql, access_params = build_access_sql("ds")
    if leader_name:
        scope_sql, scope_params = leader_scope_sql(leader_name, "ds")
        where_sql = f"({scope_sql}) AND ({access_sql})"
        where_params = (*scope_params, *access_params)
    else:
        area_match = " OR ".join([f"ds.index_label LIKE %s" for _ in GROUP_AREA_PATTERNS])
        where_sql = f"({area_match}) AND ({access_sql})"
        where_params = (*GROUP_AREA_PATTERNS, *access_params)
    return _aggregate_tier_series(metric_column, where_sql, where_params, short_area_label)


# The four tiers used by the institute-wide breakdown, collapsing category
# (Groups/Platforms/Projects all sum together within a tier) — unlike
# build_group_tier_timeseries, which keeps "Legacy - Groups" etc. intact
# since every area it covers is already "- Groups". Areas outside the Tier -
# Category grid (User Homes, Instruments, Services, ...) land in "Other"
# rather than each getting their own line, to keep the institute view
# readable.
INSTITUTE_TIERS = ("Legacy", "Scratch", "Archive", "Deep Archive")


# "Main - Platforms" (see AREA_DISPLAY_OVERRIDES) displays under its own
# name but still counts toward Legacy here — it's the same Primarydata tier
# as the rest of Legacy, just kept under its old display name for this one
# area, not a genuinely separate tier.
TIER_BUCKET_ALIASES = {"Main": "Legacy"}


def tier_bucket_for(index_label):
    short = short_area_label(index_label)
    tier = short.split(" - ", 1)[0] if " - " in short else None
    tier = TIER_BUCKET_ALIASES.get(tier, tier)
    return tier if tier in INSTITUTE_TIERS else "Other"


# Used by find_leader_duplicate_files(): when a duplicate cluster has a copy
# in one of these tiers, that's the recommended one to keep (Archive/Deep
# Archive is where data is meant to end up long-term; a copy still sitting in
# Legacy/Scratch alongside it is the more likely one to be safe to remove).
DUPLICATE_KEEP_ARCHIVE_TIERS = {"Archive", "Deep Archive"}


def build_institute_tier_timeseries(metric_column):
    """
    Every enabled area, institute-wide, broken down by tier_bucket_for()
    instead of one leader's group directories — the admin-only "Institute
    Trends" page. Still access-filtered here for defense in depth, same as
    every other tiered timeseries function.
    """
    access_sql, access_params = build_access_sql("ds")
    enabled_labels = get_enabled_area_labels()
    if not enabled_labels:
        return [], []
    placeholders = ",".join(["%s"] * len(enabled_labels))
    where_sql = f"ds.index_label IN ({placeholders}) AND ({access_sql})"
    params = (*enabled_labels, *access_params)
    return _aggregate_tier_series(
        metric_column, where_sql, params, tier_bucket_for,
        tier_order=list(INSTITUTE_TIERS) + ["Other"],
    )


def _aggregate_tier_series(metric_column, where_sql, where_params, bucket_fn, tier_order=None):
    """
    Shared core behind build_group_tier_timeseries() and
    build_institute_tier_timeseries(): sums metric_column per month (and,
    for dir_size_bytes, per day via Isilon) bucketed by bucket_fn(index_label)
    instead of into one combined total.

    Isilon's daily SmartQuotas feed only tracks total size, never staleness,
    so daily resolution is only merged in for "Total size" (dir_size_bytes)
    — the staleness metrics stay monthly-snapshot-only. Each bucket's daily
    total is computed separately (one isilon_daily_totals() call per bucket,
    keyed by which paths belong to it) rather than summed across every
    matched path into one combined total the way build_snapshot_timeseries
    does.

    A stacked area needs a value for every bucket at every point, so each
    bucket's last known reading (monthly or daily, whichever is freshest) is
    forward-filled rather than defaulting a missing point to 0 — otherwise a
    bucket with no daily feed (e.g. archive storage with no Isilon quota)
    would visibly collapse to zero on every day that isn't a snapshot date.

    metric_column must already be validated by the caller — it's
    interpolated directly into the SQL.
    """
    rows = query(
        f"""
        SELECT s.run_date, ds.index_label, SUM(ds.{metric_column}) AS value_bytes
        FROM directory_stats ds
        JOIN snapshots s ON s.id = ds.snapshot_id
        WHERE ({where_sql})
        GROUP BY s.run_date, ds.index_label
        ORDER BY s.run_date
        """,
        where_params,
    )

    by_date = {}
    buckets_seen = set()
    for row in rows:
        bucket = bucket_fn(row["index_label"])
        buckets_seen.add(bucket)
        entry = by_date.setdefault(row["run_date"], {})
        entry[bucket] = entry.get(bucket, 0) + int(row["value_bytes"] or 0)

    if metric_column == "dir_size_bytes":
        path_rows = query(
            f"""
            SELECT DISTINCT ds.path AS path, ds.index_label AS index_label
            FROM directory_stats ds
            WHERE ({where_sql})
            """,
            where_params,
        )
        bucket_paths = {}
        for row in path_rows:
            bucket = bucket_fn(row["index_label"])
            bucket_paths.setdefault(bucket, set()).add(row["path"])

        for bucket, paths in bucket_paths.items():
            buckets_seen.add(bucket)
            for stat_date, total_bytes in isilon_daily_totals(list(paths)).items():
                by_date.setdefault(stat_date, {})[bucket] = total_bytes

    if tier_order:
        buckets = [b for b in tier_order if b in buckets_seen] + sorted(buckets_seen - set(tier_order))
    else:
        buckets = sorted(buckets_seen)

    series = []
    last_known = {bucket: 0 for bucket in buckets}
    for run_date in sorted(by_date):
        point_values = by_date[run_date]
        for bucket in buckets:
            if bucket in point_values:
                last_known[bucket] = point_values[bucket]
        point = {"run_date": run_date.isoformat()}
        point.update(last_known)
        series.append(point)
    return buckets, series


def compute_institute_tier_totals(snapshot_id):
    """Total dir_size_bytes per tier_bucket_for() bucket, for one snapshot."""
    if not snapshot_id:
        return {}
    access_sql, access_params = build_access_sql("ds")
    enabled_labels = get_enabled_area_labels()
    if not enabled_labels:
        return {}
    placeholders = ",".join(["%s"] * len(enabled_labels))
    rows = query(
        f"""
        SELECT ds.index_label, SUM(ds.dir_size_bytes) AS total_bytes
        FROM directory_stats ds
        WHERE ds.snapshot_id = %s
          AND ds.index_label IN ({placeholders})
          AND ({access_sql})
        GROUP BY ds.index_label
        """,
        (snapshot_id, *enabled_labels, *access_params),
    )
    totals = {}
    for row in rows:
        bucket = tier_bucket_for(row["index_label"])
        totals[bucket] = totals.get(bucket, 0) + int(row["total_bytes"] or 0)
    return totals


def compute_reclaimed_bytes(where_sql, params):
    """Sum month-over-month size decreases per path across the full snapshot history.

    Walking each path's own history (rather than diffing an aggregated total) means
    a shrink in one directory is counted even if another directory grew the same month.
    The result only ever grows, even if a directory partially regrows later.
    """
    access_sql, access_params = build_access_sql("ds")
    rows = query(
        f"""
        SELECT ds.path, s.run_date, ds.dir_size_bytes
        FROM directory_stats ds
        JOIN snapshots s ON s.id = ds.snapshot_id
        WHERE ({where_sql})
          AND ({access_sql})
        ORDER BY ds.path, s.run_date
        """,
        tuple(params) + tuple(access_params),
    )
    reclaimed = 0
    prev_by_path = {}
    for row in rows:
        curr_size = row["dir_size_bytes"] or 0
        prev_size = prev_by_path.get(row["path"])
        if prev_size is not None and prev_size > curr_size:
            reclaimed += prev_size - curr_size
        prev_by_path[row["path"]] = curr_size
    return reclaimed


def build_enabled_areas_timeseries():
    sync_area_settings()
    return build_snapshot_timeseries(
        "ds.index_label IN (SELECT index_label FROM area_settings WHERE enabled = 1)",
        (),
    )


def normalise_group_cn(name):
    return (name or "").strip().lower()


def is_rg_group_name(name):
    upper_name = (name or "").strip().upper()
    return any(upper_name.startswith(prefix) for prefix in RG_SUGGESTION_PREFIXES)


def extract_group_cns(values):
    group_cns = []
    for raw in values or []:
        first = str(raw).split(",", 1)[0].strip()
        if first.upper().startswith("CN="):
            first = first[3:]
        if first:
            group_cns.append(first)
    return sorted(set(group_cns), key=str.lower)


def get_available_ldap_groups():
    if _missing_ldap_settings():
        return []

    bind_conn = None
    try:
        server = Server(
            LDAP["host"],
            port=LDAP["port"],
            use_ssl=LDAP["use_ssl"],
            get_info=ALL,
        )
        bind_conn = Connection(
            server,
            LDAP["bind_user_dn"],
            LDAP["bind_user_password"],
            auto_bind=True,
        )

        entries = bind_conn.extend.standard.paged_search(
            search_base=LDAP["base_dn"],
            search_filter="(&(objectClass=group)(cn=RG-*))",
            search_scope="SUBTREE",
            attributes=["cn"],
            paged_size=500,
            generator=False,
        )

        names = []
        for entry in entries:
            attributes = entry.get("attributes", {})
            cn = attributes.get("cn")
            if isinstance(cn, list):
                names.extend([str(v) for v in cn if v])
            elif cn:
                names.append(str(cn))
        return sorted({name for name in names if is_rg_group_name(name)}, key=str.lower)
    except LDAPException:
        return []
    finally:
        if bind_conn:
            bind_conn.unbind()


def get_db_group_suggestions():
    rows = query(
        """
        SELECT DISTINCT directory
        FROM directory_stats
        WHERE index_label LIKE %s
           OR index_label LIKE %s
           OR index_label LIKE %s
        ORDER BY directory
        """,
        ("%Research Groups%", "%Group Scratch%", "%Archive Groups%"),
    )
    names = set()
    for row in rows:
        directory = (row.get("directory") or "").strip()
        if not directory:
            continue
        names.add(f"RG-{directory}")
    return sorted(names, key=str.lower)


def current_username():
    return (session.get("username") or "").strip()


def current_group_cns():
    return session.get("group_cns") or []


def inferred_directory_names_from_groups():
    candidates = set()
    for group_cn in effective_group_cns():
        raw = (group_cn or "").strip()
        if not raw:
            continue

        candidates.add(raw)
        upper_raw = raw.upper()
        for prefix in GROUP_CN_PREFIXES:
            if upper_raw.startswith(prefix):
                suffix = raw[len(prefix):].strip()
                if suffix:
                    candidates.add(suffix)
                break

    return sorted({c.lower() for c in candidates if c}, key=str.lower)


def _strip_group_cn_prefix(cn):
    """Bare name from an LDAP group CN (e.g. 'RG-Yiliang-Ding' -> 'Yiliang-Ding'), or the CN unchanged if no known prefix matches."""
    raw = (cn or "").strip()
    upper_raw = raw.upper()
    for prefix in GROUP_CN_PREFIXES:
        if upper_raw.startswith(prefix):
            suffix = raw[len(prefix):].strip()
            if suffix:
                return suffix
            break
    return raw


def linked_rules_for_leader(leader_name):
    """
    directory_group_rules rows whose LDAP group CN resolves to this leader's
    name (same prefix-stripping convention as inferred_directory_names_from_
    groups()). Lets a leader's linked project directories — named
    differently, outside the normal Groups-type areas, e.g. a
    "model_training" directory under Scratch - Projects linked via an
    RG-<leader> rule — show up in their own Group Leader Evolution
    breakdown and tiered chart, not just directories literally named after
    them in Research Groups/Group Scratch/Archive Groups.
    """
    ensure_access_control_tables()
    rules = query("SELECT index_label, path_prefix, ldap_group_cn FROM directory_group_rules")
    target = normalise_group_cn(leader_name)
    return [r for r in rules if normalise_group_cn(_strip_group_cn_prefix(r.get("ldap_group_cn"))) == target]


def all_linked_leader_names():
    """Every leader name derivable from directory_group_rules' LDAP group CNs."""
    ensure_access_control_tables()
    rules = query("SELECT DISTINCT ldap_group_cn FROM directory_group_rules")
    names = set()
    for rule in rules:
        cn = rule.get("ldap_group_cn")
        if not cn:
            continue
        bare = _strip_group_cn_prefix(cn)
        if bare and bare != cn:  # only names that actually came from a known RG-style prefix
            names.add(bare)
    return names


def leader_scope_sql(leader_name, alias):
    """
    WHERE clause (and params) matching every directory belonging to a group
    leader: directories literally named after them in a Groups-type area,
    PLUS any directory linked via a directory_group_rules row that resolves
    to this leader (see linked_rules_for_leader()).
    """
    area_match = " OR ".join([f"{alias}.index_label LIKE %s" for _ in GROUP_AREA_PATTERNS])
    clauses = [f"(LOWER({alias}.directory) = %s AND ({area_match}))"]
    params = [leader_name.lower(), *GROUP_AREA_PATTERNS]
    for rule in linked_rules_for_leader(leader_name):
        clauses.append(f"({alias}.index_label = %s AND {alias}.path LIKE %s)")
        params.extend([rule["index_label"], f"{rule['path_prefix']}%"])
    return " OR ".join(clauses), params


def is_informatics_user():
    return bool(session.get("is_informatics"))


def is_explicit_full_access_user():
    username = current_username()
    if not username:
        return False
    ensure_access_control_tables()
    return bool(
        query_value(
            "SELECT 1 AS allowed FROM full_access_users WHERE LOWER(username) = LOWER(%s)",
            (username,),
            default=0,
        )
    )


def can_view_all():
    return is_informatics_user() or is_explicit_full_access_user()


def get_acting_as_group():
    """
    The RG group CN a can_view_all() user has chosen to "act as" (see
    /act-as, set from the header dropdown), for previewing the dashboard the
    way a real member of that group would see it — support/QA tool, not a
    real permission change. Returns None if not active.

    Re-checks can_view_all() here (not just at /act-as set-time) so a
    full_access_users grant revoked mid-session can't leave a stale
    "acting as" flag — though since acting-as only ever narrows access
    (see effective_can_view_all()), a stale flag would just be more
    restrictive than reality, never less; this is defense in depth, not
    the only thing standing between a user and extra access.
    """
    if not can_view_all():
        return None
    return session.get("acting_as_group") or None


def effective_can_view_all():
    """
    can_view_all(), suspended while "acting as" a group member is active —
    every data-scoping function (build_access_sql, get_group_leaders, ...)
    checks this instead of can_view_all() directly, so acting-as runs the
    exact same restricted-access code path a real member of that group
    would get, rather than the full-access bypass.
    """
    return can_view_all() and not get_acting_as_group()


def effective_group_cns():
    """current_group_cns(), or just the one simulated group while acting as a member of it."""
    acting_as = get_acting_as_group()
    if acting_as:
        return [acting_as]
    return current_group_cns()


def effective_username_for_access():
    """
    current_username(), but empty while acting as a group member — so the
    simulated view isn't contaminated by the real admin/full-access user's
    own User Homes directory always being visible on top of it. Only use
    this for the access-scoping home-directory match in build_access_sql();
    current_username() itself is unchanged everywhere else (display name,
    is_explicit_full_access_user() lookup, etc.).
    """
    if get_acting_as_group():
        return ""
    return current_username()


def is_admin_user():
    """
    Admin access (Settings / Access Control / Extension Actions / Growth
    Alerts / Institute Trends, and the "All group leaders combined" chart)
    is narrower than can_view_all(): a full_access_users grant gives someone
    outside Informatics full visibility into the data, but not the admin
    pages — those stay restricted to actual Informatics staff
    (LDAP_ALLOWED_GROUP_DN membership). can_view_all() itself is unchanged
    and still drives data access (build_access_sql, etc.) for both groups.
    """
    return is_informatics_user()


def build_access_sql(alias="ds"):
    ensure_access_control_tables()
    if effective_can_view_all():
        return "1=1", []

    username = effective_username_for_access().lower()
    clauses = []
    params = []

    if username:
        home_match = " OR ".join([f"{alias}.index_label LIKE %s" for _ in USER_HOME_PATTERNS])
        clauses.append(
            f"(LOWER({alias}.directory) = %s AND ({home_match}))"
        )
        params.append(username)
        params.extend(USER_HOME_PATTERNS)

    group_cns = effective_group_cns()
    if group_cns:
        rules = query(
            f"""
            SELECT index_label, path_prefix, ldap_group_cn
            FROM directory_group_rules
            WHERE LOWER(ldap_group_cn) IN ({','.join(['%s'] * len(group_cns))})
            ORDER BY index_label, path_prefix
            """,
            tuple(normalise_group_cn(g) for g in group_cns),
        )
        for rule in rules:
            clauses.append(f"({alias}.index_label = %s AND {alias}.path LIKE %s)")
            params.extend([rule["index_label"], f"{rule['path_prefix']}%"])

        inferred_names = inferred_directory_names_from_groups()
        if inferred_names:
            area_match = " OR ".join([f"{alias}.index_label LIKE %s" for _ in GROUP_AREA_PATTERNS])
            dir_match = ", ".join(["%s"] * len(inferred_names))
            clauses.append(
                f"(LOWER({alias}.directory) IN ({dir_match}) AND ({area_match}))"
            )
            params.extend(inferred_names)
            params.extend(GROUP_AREA_PATTERNS)

    if not clauses:
        return "1=0", []
    return " OR ".join(clauses), params


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        if not is_admin_user():
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return view_func(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_permissions():
    authenticated = bool(session.get("authenticated"))
    full_access = authenticated and can_view_all()
    acting_as = get_acting_as_group() if full_access else None
    return {
        # Suspended while acting-as is active, same as effective_can_view_all()
        # — every admin-only nav link/button/section in every template reads
        # this one flag, so hiding them all while previewing a restricted
        # view is automatic rather than something each template has to
        # remember to also check acting_as_group for. Doesn't touch
        # is_admin_user() itself or admin_required — real route protection is
        # unaffected, this only controls what templates choose to show.
        "is_admin": authenticated and is_admin_user() and not acting_as,
        "can_view_all_user": full_access,
        "acting_as_group": acting_as,
        # Cheap, local-DB-only suggestion list (see get_db_group_suggestions())
        # rather than the live LDAP lookup access_admin() uses — this runs on
        # every page view for every full-access user, so it needs to stay
        # fast, not necessarily perfectly in sync with AD at this instant.
        "act_as_group_options": get_db_group_suggestions() if full_access else [],
        "short_area_label": short_area_label,
        "human_bytes": human_bytes,
    }


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


def _safe_next_url(next_url: str):
    if next_url and next_url.startswith("/"):
        return next_url
    return url_for("index")


def _safe_referrer_path():
    """
    request.referrer's path+query if it's same-origin, else the index page.
    Unlike _safe_next_url()'s `next` query param, request.referrer is a full
    URL and a browser-set header a crafted request could spoof — used by
    routes like /act-as that redirect back to whatever page the user was on.
    """
    ref = request.referrer
    if not ref:
        return url_for("index")
    parsed = urlparse(ref)
    if parsed.netloc and parsed.netloc != request.host:
        return url_for("index")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def _ldap_authenticate(username: str, password: str):
    bind_conn = None
    user_conn = None
    try:
        server = Server(
            LDAP["host"],
            port=LDAP["port"],
            use_ssl=LDAP["use_ssl"],
            get_info=ALL,
        )
        bind_conn = Connection(
            server,
            LDAP["bind_user_dn"],
            LDAP["bind_user_password"],
            auto_bind=True,
        )

        # escape_filter_chars prevents LDAP filter injection via the login
        # form's username field (e.g. a crafted value containing ")(" could
        # otherwise alter the filter's logic).
        search_filter = f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(username)}))"

        bind_conn.search(
            LDAP["base_dn"],
            search_filter,
            attributes=["cn", "mail", "distinguishedName", "memberOf"],
        )

        if not bind_conn.entries:
            return None, "User not found in LDAP."

        user_info = bind_conn.entries[0]
        user_dn = str(user_info.entry_dn)
        user_conn = Connection(server, user_dn, password, auto_bind=False)

        if not user_conn.bind():
            return None, "Invalid username or password."

        group_dns = []
        if hasattr(user_info, "memberOf"):
            try:
                group_dns = list(user_info.memberOf.values)
            except Exception:
                group_dns = []
        group_cns = extract_group_cns(group_dns)
        normalised_group_cns = {normalise_group_cn(g) for g in group_cns}

        required_group_cns = {normalise_group_cn(cn) for cn in extract_group_cns(LDAP.get("required_group_dns") or [])}
        if required_group_cns and not (required_group_cns & normalised_group_cns):
            return None, "Your account is not authorized to access this tool."

        full_access_group_cn = extract_group_cns([LDAP["allowed_group_dn"]])[0] if LDAP.get("allowed_group_dn") else ""

        return {
            "username": username,
            "full_name": str(user_info.cn),
            "email": str(user_info.mail),
            "distinguished_name": user_dn,
            "group_cns": group_cns,
            "is_informatics": normalise_group_cn(full_access_group_cn) in normalised_group_cns,
        }, None
    except LDAPException as exc:
        return None, f"LDAP error: {exc}"
    finally:
        if bind_conn:
            bind_conn.unbind()
        if user_conn:
            user_conn.unbind()


def _missing_ldap_settings():
    missing = []
    for key in REQUIRED_LDAP_KEYS:
        value = LDAP.get(key)
        if value is None or str(value).strip() == "":
            missing.append(key)
    return missing


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            error = "Username and password are required."
        elif _missing_ldap_settings():
            missing = ", ".join(_missing_ldap_settings())
            error = f"LDAP configuration is incomplete. Missing: {missing}."
        else:
            user_data, err = _ldap_authenticate(username, password)
            if err:
                error = err
            else:
                ensure_admin_tables()
                session.clear()
                session["authenticated"] = True
                session["username"] = user_data["username"]
                session["full_name"] = user_data["full_name"]
                session["email"] = user_data["email"]
                session["distinguished_name"] = user_data["distinguished_name"]
                session["group_cns"] = user_data["group_cns"]
                session["is_informatics"] = user_data["is_informatics"]
                return redirect(_safe_next_url(request.args.get("next", "")))

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/act-as", methods=["POST"])
@login_required
def set_acting_as():
    """
    Sets or clears session["acting_as_group"] from the header dropdown — a
    can_view_all() user (admin or full_access_users grant) previewing the
    dashboard exactly as a real member of one research group would see it.
    Only ever narrows what build_access_sql()/get_group_leaders() return
    (effective_can_view_all() becomes False while this is set) — never
    grants anything beyond the real session's own access, so there's no
    privilege-escalation risk in accepting any group_cn value here, including
    one that doesn't exist (same "no matching rows" outcome as a real user
    in a group nobody's configured directory_group_rules for yet).
    """
    if not can_view_all():
        flash("You don't have permission to use that.", "error")
        return redirect(url_for("index"))

    # No flash() here — the persistent amber banner (base.html, driven by
    # acting_as_group) already communicates this continuously for as long as
    # it's active, and disappears the moment it's cleared. A one-time flash
    # on top of that was just redundant clutter, not a missing confirmation.
    group_cn = (request.form.get("group_cn") or "").strip()
    if group_cn:
        session["acting_as_group"] = group_cn
    else:
        session.pop("acting_as_group", None)

    return redirect(_safe_referrer_path())


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    snapshots = query(
        "SELECT id, run_date, notes FROM snapshots ORDER BY run_date DESC"
    )
    if not snapshots:
        return render_template("no_data.html")

    # default to the latest snapshot
    snap_id = request.args.get("snap", snapshots[0]["id"], type=int)

    enabled_labels = get_enabled_area_labels(snapshot_id=snap_id)
    areas = [{"value": label, "label": short_area_label(label)} for label in enabled_labels]
    areas.sort(key=lambda a: a["label"].lower())
    area_filter = request.args.get("area", "")
    if area_filter and area_filter not in enabled_labels:
        area_filter = ""

    access_sql, access_params = build_access_sql("ds")
    sql = """
        SELECT ds.*, s.run_date
        FROM directory_stats ds
        JOIN snapshots s ON s.id = ds.snapshot_id
        JOIN area_settings aset ON aset.index_label = ds.index_label
        WHERE ds.snapshot_id = %s
          AND aset.enabled = 1
    """
    params = [snap_id]
    sql += f" AND ({access_sql})"
    params.extend(access_params)
    if area_filter:
        sql += " AND ds.index_label = %s"
        params.append(area_filter)
    sql += " ORDER BY ds.dir_size_bytes DESC NULLS LAST"
    # MySQL doesn't support NULLS LAST — use ISNULL trick
    sql = sql.replace("DESC NULLS LAST", "IS NULL, ds.dir_size_bytes DESC")
    rows = query(sql, params)

    # month-on-month diff: find the snapshot just before snap_id
    prev = query(
        "SELECT id FROM snapshots WHERE run_date < "
        "(SELECT run_date FROM snapshots WHERE id=%s) "
        "ORDER BY run_date DESC LIMIT 1",
        (snap_id,), one=True
    )
    prev_map = {}
    prev_rows = []
    if prev:
        prev_rows = query(
            "SELECT * "
            "FROM directory_stats WHERE snapshot_id = %s",
            (prev["id"],)
        )
        prev_map = {r["path"]: r for r in prev_rows}

    packed_rows = []
    user_home_rows = []

    for r in rows:
        if is_user_home_label(r.get("index_label")):
            user_home_rows.append(r)
            continue

        p = prev_map.get(r["path"])
        r["index_label_short"] = short_area_label(r["index_label"])
        r["stale_1yr_bytes_eff"] = _effective_stale_bytes(r, "stale_1yr_bytes")
        r["stale_2yr_bytes_eff"] = _effective_stale_bytes(r, "stale_2yr_bytes")
        r["stale_4yr_bytes_eff"] = _effective_stale_bytes(r, "stale_4yr_bytes")

        prev_stale1_b = _effective_stale_bytes(p or {}, "stale_1yr_bytes") if p else None
        prev_stale2_b = _effective_stale_bytes(p or {}, "stale_2yr_bytes") if p else None
        prev_stale4_b = _effective_stale_bytes(p or {}, "stale_4yr_bytes") if p else None

        r["delta_size"]   = (r["dir_size_bytes"] or 0) - (p["dir_size_bytes"] or 0) if p else None
        r["delta_stale1_size"] = (
            r["stale_1yr_bytes_eff"] - prev_stale1_b
            if p and r["stale_1yr_bytes_eff"] is not None and prev_stale1_b is not None
            else None
        )
        r["delta_stale2_size"] = (
            r["stale_2yr_bytes_eff"] - prev_stale2_b
            if p and r["stale_2yr_bytes_eff"] is not None and prev_stale2_b is not None
            else None
        )
        r["delta_stale4_size"] = (
            r["stale_4yr_bytes_eff"] - prev_stale4_b
            if p and r["stale_4yr_bytes_eff"] is not None and prev_stale4_b is not None
            else None
        )
        packed_rows.append(r)

    if user_home_rows:
        user_home_area = short_area_label(user_home_rows[0].get("index_label"))
        user_home_size = sum(int(r.get("dir_size_bytes") or 0) for r in user_home_rows)
        user_home_stale1 = sum(int(_effective_stale_bytes(r, "stale_1yr_bytes") or 0) for r in user_home_rows)
        user_home_stale2 = sum(int(_effective_stale_bytes(r, "stale_2yr_bytes") or 0) for r in user_home_rows)
        user_home_stale4 = sum(int(_effective_stale_bytes(r, "stale_4yr_bytes") or 0) for r in user_home_rows)

        prev_user_home_rows = [r for r in prev_rows if is_user_home_label(r.get("index_label"))]
        has_prev_user_homes = bool(prev_user_home_rows)
        prev_user_home_size = sum(int(r.get("dir_size_bytes") or 0) for r in prev_user_home_rows)
        prev_user_home_stale1 = sum(int(_effective_stale_bytes(r, "stale_1yr_bytes") or 0) for r in prev_user_home_rows)
        prev_user_home_stale2 = sum(int(_effective_stale_bytes(r, "stale_2yr_bytes") or 0) for r in prev_user_home_rows)
        prev_user_home_stale4 = sum(int(_effective_stale_bytes(r, "stale_4yr_bytes") or 0) for r in prev_user_home_rows)

        packed_rows.append(
            {
                "directory": "User Homes (aggregated)",
                "index_label": user_home_rows[0].get("index_label"),
                "index_label_short": user_home_area,
                "path": "",
                "dir_size_bytes": user_home_size,
                "dir_size_raw": f"{(user_home_size / 1e12):.1f} TB",
                "stale_1yr_bytes_eff": user_home_stale1,
                "stale_2yr_bytes_eff": user_home_stale2,
                "stale_4yr_bytes_eff": user_home_stale4,
                "delta_size": (user_home_size - prev_user_home_size) if has_prev_user_homes else None,
                "delta_stale1_size": (user_home_stale1 - prev_user_home_stale1) if has_prev_user_homes else None,
                "delta_stale2_size": (user_home_stale2 - prev_user_home_stale2) if has_prev_user_homes else None,
                "delta_stale4_size": (user_home_stale4 - prev_user_home_stale4) if has_prev_user_homes else None,
            }
        )

    rows = packed_rows
    rows.sort(key=lambda r: int(r.get("dir_size_bytes") or 0), reverse=True)

    return render_template(
        "index.html",
        snapshots=snapshots,
        current_snap=snap_id,
        areas=areas,
        area_filter=area_filter,
        area_filter_label=short_area_label(area_filter),
        rows=rows,
        has_prev=bool(prev),
    )


@app.route("/evolution")
@login_required
def evolution():
    enabled_labels = get_enabled_area_labels()
    areas = [{"value": label, "label": short_area_label(label)} for label in enabled_labels]
    areas.sort(key=lambda a: a["label"].lower())
    if not areas:
        return render_template("no_data.html")

    selected_area = request.args.get("area", areas[0]["value"])
    if selected_area not in {a["value"] for a in areas}:
        selected_area = areas[0]["value"]
    access_sql, access_params = build_access_sql("directory_stats")
    directories = query(
        f"""
        SELECT DISTINCT path, directory
        FROM directory_stats
        WHERE index_label = %s
          AND ({access_sql})
        ORDER BY directory
        """,
        (selected_area, *access_params),
    )
    selected_path = request.args.get("path", "")
    valid_paths = {row["path"] for row in directories}
    if not selected_path and directories:
        selected_path = directories[0]["path"]
    elif selected_path not in valid_paths:
        selected_path = ""

    latest_snap = query("SELECT id, run_date FROM snapshots ORDER BY run_date DESC LIMIT 1", one=True)
    prev_snap = _find_previous_snapshot(latest_snap["run_date"]) if latest_snap else None

    ds_access_sql, ds_access_params = build_access_sql("ds")
    directory_rows = []
    if latest_snap:
        directory_rows = query(
            f"""
            SELECT ds.*, s.run_date
            FROM directory_stats ds
            JOIN snapshots s ON s.id = ds.snapshot_id
            WHERE ds.snapshot_id = %s
              AND ds.index_label = %s
              AND ({ds_access_sql})
            ORDER BY ds.dir_size_bytes IS NULL, ds.dir_size_bytes DESC
            """,
            (latest_snap["id"], selected_area, *ds_access_params),
        )

        prev_by_path = {}
        if prev_snap:
            prev_rows = query(
                f"""
                SELECT ds.path, ds.dir_size_bytes
                FROM directory_stats ds
                WHERE ds.snapshot_id = %s
                  AND ds.index_label = %s
                  AND ({ds_access_sql})
                """,
                (prev_snap["id"], selected_area, *ds_access_params),
            )
            prev_by_path = {row["path"]: row["dir_size_bytes"] for row in prev_rows}

        for row in directory_rows:
            row["index_label_short"] = short_area_label(row["index_label"])
            row["stale_1yr_bytes_eff"] = _effective_stale_bytes(row, "stale_1yr_bytes")
            row["stale_2yr_bytes_eff"] = _effective_stale_bytes(row, "stale_2yr_bytes")
            row["stale_4yr_bytes_eff"] = _effective_stale_bytes(row, "stale_4yr_bytes")
            prev_size = prev_by_path.get(row["path"])
            curr_size = row.get("dir_size_bytes") or 0
            row["delta_size"] = (curr_size - prev_size) if prev_size is not None else None

    return render_template(
        "evolution.html",
        areas=areas,
        current_area=selected_area,
        current_area_label=short_area_label(selected_area),
        directories=[{"value": d["path"], "label": d["directory"]} for d in directories],
        current_path=selected_path,
        directory_rows=directory_rows,
        latest_snapshot=latest_snap,
        has_prev=bool(prev_snap),
        area_has_subdirs=is_group_area_label(selected_area),
    )


@app.route("/data-management")
def data_management():
    return render_template("data_management.html")


@app.route("/help")
@login_required
def help_page():
    return render_template("help.html")


@app.route("/data-management/presentation")
def data_management_presentation():
    return send_from_directory(
        DOCUMENTS_DIR, DATA_MANAGEMENT_PRESENTATION, as_attachment=True
    )


def get_hidden_group_leaders():
    """
    Leader names an admin has hidden from the duplicate-file leaderboard on
    Storage Alerts & Insights, for any reason. Doesn't affect anything else: Big Files,
    Duplicate Files's own per-leader detail page, Group Leader Evolution,
    Area Evolution, Directory Explorer, and Overview all still show the
    leader and their data normally. Reversible at any time from Admin ->
    Access Control; this only ever hides, never deletes, anything.
    """
    ensure_access_control_tables()
    rows = query("SELECT directory FROM hidden_group_leaders")
    return {row["directory"].strip().lower() for row in rows if row.get("directory")}


def get_group_leaders():
    access_sql, access_params = build_access_sql("ds")
    area_match = " OR ".join([f"ds.index_label LIKE %s" for _ in GROUP_AREA_PATTERNS])
    rows = query(
        f"""
        SELECT DISTINCT directory
        FROM directory_stats ds
        WHERE ({area_match})
          AND ({access_sql})
        ORDER BY directory
        """,
        (*GROUP_AREA_PATTERNS, *access_params),
    )
    seen = {}
    for row in rows:
        directory = (row.get("directory") or "").strip()
        if not directory:
            continue
        seen.setdefault(directory.lower(), directory)

    # Also surface leaders who only have a *linked* project directory (via
    # directory_group_rules) and no directory literally named after them in
    # a Groups-type area. Non-admins only see linked names matching their
    # own groups — same access boundary as everywhere else; admins/full-
    # access users see every linked name.
    linked_names = all_linked_leader_names()
    if not effective_can_view_all():
        own_names = {n.lower() for n in inferred_directory_names_from_groups()}
        linked_names = {n for n in linked_names if n.lower() in own_names}
    for name in linked_names:
        seen.setdefault(name.lower(), name)

    return sorted(seen.values(), key=str.lower)


def _find_previous_snapshot(run_date):
    return query(
        "SELECT id, run_date FROM snapshots WHERE run_date < %s ORDER BY run_date DESC LIMIT 1",
        (run_date,),
        one=True,
    )


def _isilon_history_by_path(paths, since):
    """Bulk-fetch isilon_quota_stats history for `paths` from `since` onward,
    as {path: [(stat_date, logical_bytes), ...]} ascending by date."""
    history = {}
    if not paths:
        return history
    placeholders = ", ".join(["%s"] * len(paths))
    rows = query(
        f"""
        SELECT path, stat_date, logical_bytes FROM isilon_quota_stats
        WHERE path IN ({placeholders}) AND stat_date >= %s
        ORDER BY path, stat_date
        """,
        (*paths, since),
    )
    for r in rows:
        history.setdefault(r["path"], []).append((r["stat_date"], r["logical_bytes"]))
    return history


def format_delta_bytes(delta_bytes):
    """+/-1.2 TB for large deltas, +/-345.6 GB for smaller ones — raw GB gets
    unreadable once a change is actually tens of TB."""
    if abs(delta_bytes) >= 1e12:
        return f"{delta_bytes / 1e12:+.2f} TB"
    return f"{delta_bytes / 1e9:+.1f} GB"


def format_gb_threshold_label(gb):
    """≥1 TB instead of ≥1000 GB once the threshold reaches four figures."""
    return f"≥ {gb / 1000:g} TB" if gb >= 1000 else f"≥ {gb:g} GB"


def compute_growth_alerts(pct_threshold=GROWTH_ALERT_THRESHOLD_PCT, bytes_threshold=GROWTH_ALERT_THRESHOLD_BYTES, as_of=None):
    """
    Flag directories, across every enabled storage area, with a big enough
    size change (up or down) — "big enough" means at least pct_threshold%
    AND at least bytes_threshold bytes, so a tiny directory doubling in size
    doesn't drown out a huge one growing by a smaller percentage.

    `as_of` defaults to today. When it's today, two comparison windows are
    computed and shown separately, either of which can trigger the flag:
      - "week": as_of vs. ~7 days earlier, using Isilon's daily size data
        (only available where Isilon covers that path).
      - "month": as_of vs. the start of its calendar month, using Isilon
        daily data if available for that date, else falling back to the
        previous monthly Diskover snapshot.
    For a past `as_of` (the last day of an earlier month), only the "month"
    window is computed — "last 7 days" only means something relative to the
    present, not to some arbitrary past date.
    """
    as_of = as_of or date.today()
    is_current = as_of == date.today()

    # The directory universe (which paths exist, area/access filtering) always
    # comes from the single latest Diskover snapshot, regardless of `as_of` —
    # Diskover only ever has one monthly snapshot at a time (dated for
    # whenever it was last run), so requiring one dated on/before an earlier
    # requested month would wrongly find nothing whenever Isilon's daily data
    # reaches further back than Diskover's last crawl.
    period_snap = query("SELECT id, run_date FROM snapshots ORDER BY run_date DESC LIMIT 1", one=True)
    if not period_snap:
        return [], None

    # Covers every *enabled* area (same visibility toggle as Settings/Overview/
    # Area Evolution) rather than being limited to group-type areas only —
    # except User Homes, excluded here specifically: hundreds of individual
    # per-user directories would otherwise dominate the alert list with
    # routine personal-folder fluctuations.
    not_user_home = " AND ".join([f"ds.index_label NOT LIKE %s" for _ in USER_HOME_PATTERNS])
    access_sql, access_params = build_access_sql("ds")

    curr_rows = query(
        f"""
        SELECT ds.directory, ds.path, ds.index_label, ds.dir_size_bytes
        FROM directory_stats ds
        JOIN area_settings aset ON aset.index_label = ds.index_label
        WHERE ds.snapshot_id = %s
          AND aset.enabled = 1
          AND ({not_user_home})
          AND ({access_sql})
        """,
        (period_snap["id"], *USER_HOME_PATTERNS, *access_params),
    )

    prev_snap = _find_previous_snapshot(period_snap["run_date"])
    prev_by_path = {}
    if prev_snap:
        prev_rows = query(
            f"""
            SELECT ds.path, ds.dir_size_bytes
            FROM directory_stats ds
            JOIN area_settings aset ON aset.index_label = ds.index_label
            WHERE ds.snapshot_id = %s
              AND aset.enabled = 1
              AND ({not_user_home})
            """,
            (prev_snap["id"], *USER_HOME_PATTERNS),
        )
        prev_by_path = {row["path"]: row["dir_size_bytes"] for row in prev_rows}

    month_start = as_of.replace(day=1)
    week_ago = as_of - timedelta(days=7)
    paths = [row["path"] for row in curr_rows]
    isilon_history = _isilon_history_by_path(paths, since=min(month_start, week_ago) - timedelta(days=3))

    def closest_on_or_before(path, target_date):
        best = None
        for stat_date, size_bytes in isilon_history.get(path, []):
            if stat_date <= target_date:
                best = (stat_date, size_bytes)
            else:
                break
        return best

    def earliest_within(path, start_date, end_date):
        """First available entry on/after start_date (up to end_date) — used
        as a fallback baseline when Isilon's history doesn't reach back to
        the start of the requested period (e.g. the daily feed only started
        partway through a given month)."""
        for stat_date, size_bytes in isilon_history.get(path, []):
            if stat_date > end_date:
                break
            if stat_date >= start_date:
                return (stat_date, size_bytes)
        return None

    def make_window(curr_bytes, prev_pair):
        if prev_pair is None or curr_bytes is None:
            return {"available": False}
        prev_date, prev_bytes = prev_pair
        if not prev_bytes:
            return {"available": False}
        delta_bytes = curr_bytes - prev_bytes
        pct = (delta_bytes / prev_bytes) * 100
        return {
            "available": True,
            "prev_date": prev_date,
            "prev_bytes": prev_bytes,
            "delta_bytes": delta_bytes,
            "delta_display": format_delta_bytes(delta_bytes),
            "pct": pct,
            "flagged": abs(pct) >= pct_threshold and abs(delta_bytes) >= bytes_threshold,
        }

    alerts = []
    for row in curr_rows:
        path = row["path"]
        monthly_curr = row["dir_size_bytes"] or 0
        latest_i = closest_on_or_before(path, as_of)
        curr_bytes = latest_i[1] if latest_i else monthly_curr
        curr_date = latest_i[0] if latest_i else period_snap["run_date"]

        if is_current:
            week_prev = closest_on_or_before(path, week_ago) or earliest_within(path, week_ago, as_of)
            week = make_window(curr_bytes, week_prev)
        else:
            week = {"available": False}

        month_prev = closest_on_or_before(path, month_start)
        if month_prev is None:
            month_prev = earliest_within(path, month_start, as_of)
        if month_prev is None and path in prev_by_path:
            month_prev = (prev_snap["run_date"], prev_by_path[path])
        month = make_window(curr_bytes, month_prev)

        if not (week.get("flagged") or month.get("flagged")):
            continue

        alerts.append({
            "directory": row["directory"],
            "index_label_short": short_area_label(row["index_label"]),
            "path": path,
            "curr_bytes": curr_bytes,
            "curr_date": curr_date,
            "week": week,
            "month": month,
        })

    def biggest_move(alert):
        candidates = [0]
        if alert["week"].get("available"):
            candidates.append(abs(alert["week"]["delta_bytes"]))
        if alert["month"].get("available"):
            candidates.append(abs(alert["month"]["delta_bytes"]))
        return max(candidates)

    alerts.sort(key=biggest_move, reverse=True)
    return alerts, period_snap


def get_available_alert_months():
    """Distinct YYYY-MM months with either a Diskover snapshot or Isilon daily data, newest first."""
    rows = query(
        """
        SELECT DISTINCT DATE_FORMAT(run_date, '%Y-%m') AS ym FROM snapshots
        UNION
        SELECT DISTINCT DATE_FORMAT(stat_date, '%Y-%m') AS ym FROM isilon_quota_stats
        ORDER BY ym DESC
        """
    )
    return [row["ym"] for row in rows]


@app.route("/group-leader-evolution")
@login_required
def group_leader_evolution():
    leaders = get_group_leaders()
    if not leaders:
        if effective_can_view_all():
            return render_template("no_data.html")
        flash(
            "No group leader directories are mapped to your account yet. "
            "Contact an admin if you think this is wrong.",
            "error",
        )
        return redirect(url_for("index"))

    requested = request.args.get("leader", "")
    selected = next((l for l in leaders if l.lower() == requested.lower()), leaders[0])

    access_sql, access_params = build_access_sql("ds")
    scope_sql, scope_params = leader_scope_sql(selected, "ds")
    latest_snap = query("SELECT id, run_date FROM snapshots ORDER BY run_date DESC LIMIT 1", one=True)
    prev_snap = _find_previous_snapshot(latest_snap["run_date"]) if latest_snap else None

    breakdown = []
    if latest_snap:
        breakdown = query(
            f"""
            SELECT ds.*, s.run_date
            FROM directory_stats ds
            JOIN snapshots s ON s.id = ds.snapshot_id
            WHERE ds.snapshot_id = %s
              AND ({scope_sql})
              AND ({access_sql})
            ORDER BY ds.dir_size_bytes DESC
            """,
            (latest_snap["id"], *scope_params, *access_params),
        )

        prev_by_path = {}
        if prev_snap:
            prev_rows = query(
                f"""
                SELECT ds.path, ds.dir_size_bytes
                FROM directory_stats ds
                WHERE ds.snapshot_id = %s
                  AND ({scope_sql})
                  AND ({access_sql})
                """,
                (prev_snap["id"], *scope_params, *access_params),
            )
            prev_by_path = {row["path"]: row["dir_size_bytes"] for row in prev_rows}

        for row in breakdown:
            row["index_label_short"] = short_area_label(row["index_label"])
            row["stale_1yr_bytes_eff"] = _effective_stale_bytes(row, "stale_1yr_bytes")
            row["stale_2yr_bytes_eff"] = _effective_stale_bytes(row, "stale_2yr_bytes")
            row["stale_4yr_bytes_eff"] = _effective_stale_bytes(row, "stale_4yr_bytes")

            prev_size = prev_by_path.get(row["path"])
            curr_size = row.get("dir_size_bytes") or 0
            row["delta_size"] = (curr_size - prev_size) if prev_size is not None else None
            row["growth_pct"] = (row["delta_size"] / prev_size * 100) if prev_size else None
            row["growth_alert"] = (
                row["growth_pct"] is not None and row["growth_pct"] >= GROWTH_ALERT_THRESHOLD_PCT
            )

    reclaimed_bytes = compute_reclaimed_bytes(scope_sql, scope_params)

    return render_template(
        "group_leader_evolution.html",
        leaders=leaders,
        selected_leader=selected,
        latest_snapshot=latest_snap,
        breakdown=breakdown,
        has_prev=bool(prev_snap),
        reclaimed_bytes=reclaimed_bytes,
    )


@app.route("/directory-explorer")
@login_required
def directory_explorer():
    path = request.args.get("path", "").strip()
    if not path:
        flash("No directory specified.", "error")
        return redirect(url_for("index"))

    access_sql, access_params = build_access_sql("ds")
    parent = query(
        f"""
        SELECT ds.*, s.run_date
        FROM directory_stats ds
        JOIN snapshots s ON s.id = ds.snapshot_id
        WHERE ds.path = %s
          AND ({access_sql})
        ORDER BY s.run_date DESC
        LIMIT 1
        """,
        (path, *access_params),
        one=True,
    )
    if not parent:
        flash("Directory not found or you don't have access to it.", "error")
        return redirect(url_for("index"))

    if not is_group_area_label(parent.get("index_label")):
        flash("Subdirectory data isn't collected for this area.", "error")
        return redirect(url_for("index"))

    latest_snap = query("SELECT id, run_date FROM snapshots ORDER BY run_date DESC LIMIT 1", one=True)
    prev_snap = _find_previous_snapshot(latest_snap["run_date"]) if latest_snap else None

    sub_access_sql, sub_access_params = build_access_sql("sds")
    subdirs = []
    if latest_snap:
        subdirs = query(
            f"""
            SELECT sds.*
            FROM subdirectory_stats sds
            WHERE sds.snapshot_id = %s AND sds.parent_path = %s
              AND ({sub_access_sql})
            ORDER BY sds.dir_size_bytes IS NULL, sds.dir_size_bytes DESC
            """,
            (latest_snap["id"], path, *sub_access_params),
        )

        prev_by_path = {}
        if prev_snap:
            prev_rows = query(
                f"""
                SELECT sds.path, sds.dir_size_bytes
                FROM subdirectory_stats sds
                WHERE sds.snapshot_id = %s AND sds.parent_path = %s
                  AND ({sub_access_sql})
                """,
                (prev_snap["id"], path, *sub_access_params),
            )
            prev_by_path = {r["path"]: r["dir_size_bytes"] for r in prev_rows}

        for r in subdirs:
            r["index_label_short"] = short_area_label(r["index_label"])
            prev_size = prev_by_path.get(r["path"])
            curr_size = r.get("dir_size_bytes") or 0
            r["delta_size"] = (curr_size - prev_size) if prev_size is not None else None

    return render_template(
        "directory_explorer.html",
        parent=parent,
        parent_label=short_area_label(parent.get("index_label")),
        subdirs=subdirs,
        latest_snapshot=latest_snap,
        has_prev=bool(prev_snap),
    )


@app.route("/big-files")
@login_required
def big_files():
    latest_snap = query("SELECT id, run_date FROM snapshots ORDER BY run_date DESC LIMIT 1", one=True)

    rows = []
    if latest_snap:
        access_sql, access_params = build_access_sql("sds")
        rows = query(
            f"""
            SELECT sds.*
            FROM subdirectory_stats sds
            WHERE sds.snapshot_id = %s
              AND ({access_sql})
            ORDER BY sds.dir_size_bytes IS NULL, sds.dir_size_bytes DESC
            """,
            (latest_snap["id"], *access_params),
        )
        for r in rows:
            r["index_label_short"] = short_area_label(r["index_label"])

    return render_template("big_files.html", rows=rows, latest_snapshot=latest_snap)


DUPLICATE_TOLERANCE_OPTIONS = [0, 1, 2, 5, 10]


def _relative_to_leader(file_path, leader_name):
    """
    file_path with everything up to and including the leader's own folder
    segment stripped off (e.g. ".../GROUP_SCRATCH/Caroline-Dean/foo/bar.bam"
    -> "foo/bar.bam") — the area is already shown in its own column, so the
    full filesystem path down to the leader's folder is just noise. Falls
    back to the unchanged path if the leader's name isn't found as a path
    segment (a linked project directory isn't necessarily named after the
    leader at all — see leader_scope_sql()).
    """
    parts = file_path.split("/")
    target = leader_name.lower()
    for i, part in enumerate(parts):
        if part.lower() == target:
            rest = "/".join(parts[i + 1:])
            return rest or file_path
    return file_path


def find_leader_duplicate_files(leader_name, tolerance_pct):
    """
    Possible duplicate files for one group leader, across every area they
    have — their own directories plus any linked projects (leader_scope_sql()
    — same scope Group Leader Evolution uses), not just one area at a time
    like Big Files does.

    Only ever sees a leader's biggest 1000 files per subdirectory, since
    that's all dir_top_files holds (pruned to current + previous month) —
    a smaller duplicated file outside that list is invisible here by
    construction, not a bug. No content hash is available from the crawl
    either, so "duplicate" is a heuristic: the same filename, with sizes
    within tolerance_pct of each other, clustered against the largest
    not-yet-clustered file with that name. That's a plausible signal, not a
    guarantee — two unrelated files that happen to share a generic name and
    a similar size would also match, so this should read as "possible
    duplicates worth checking", not a confident, safe-to-auto-delete list.

    Returns (clusters, latest_snapshot) — clusters sorted by reclaimable
    space (total size minus the one largest copy) descending. Within each
    cluster, the recommended copy to keep is the one in Archive/Deep Archive
    if any exist (see DUPLICATE_KEEP_ARCHIVE_TIERS) — that's where data is
    meant to live long-term, so a duplicate still in Legacy/Scratch is the
    more likely candidate to remove, regardless of which copy is larger.
    Falls back to the largest copy when no archived copy exists.

    Scoped to the latest snapshot that actually has dir_top_files rows for
    this leader, not just the globally-latest snapshot — same reasoning as
    Big Files/Biggest Files: a brand new month's directory_stats can exist
    before that month's slower file-stats crawl step has reached this
    leader, and blindly using the latest snapshot would silently search a
    near-empty dataset instead of falling back to the last complete one.

    Not affected by hidden_group_leaders — a leader hidden from the Growth
    Alerts leaderboard (see get_duplicate_leaderboard()) still has their own
    full data here, so whoever's actually doing the cleanup work for that
    leader isn't blocked from it. The hide only ever applies to that
    leaderboard, nowhere else.
    """
    # latest_snapshot_with_rows()'s own subquery aliases the table as "t",
    # not "dtf" — needs its own leader_scope_sql()/build_access_sql() calls
    # matching that alias, separate from the "dtf"-aliased ones the main
    # query below uses.
    lookup_scope_sql, lookup_scope_params = leader_scope_sql(leader_name, "t")
    lookup_access_sql, lookup_access_params = build_access_sql("t")
    latest_snap = latest_snapshot_with_rows(
        "dir_top_files",
        f"({lookup_scope_sql}) AND ({lookup_access_sql})",
        [*lookup_scope_params, *lookup_access_params],
    )
    if not latest_snap:
        return [], None

    access_sql, access_params = build_access_sql("dtf")
    scope_sql, scope_params = leader_scope_sql(leader_name, "dtf")
    rows = query(
        f"""
        SELECT dtf.file_path, dtf.extension, dtf.size_bytes, dtf.mtime, dtf.index_label
        FROM dir_top_files dtf
        WHERE dtf.snapshot_id = %s
          AND ({scope_sql})
          AND ({access_sql})
        """,
        (latest_snap["id"], *scope_params, *access_params),
    )

    clusters = cluster_duplicate_files(rows, tolerance_pct)
    for c in clusters:
        for f in c["files"]:
            f["index_label_short"] = short_area_label(f["index_label"])
            f["display_path"] = _relative_to_leader(f["file_path"], leader_name)
            f["tier"] = tier_bucket_for(f["index_label"])

        archive_files = [f for f in c["files"] if f["tier"] in DUPLICATE_KEEP_ARCHIVE_TIERS]
        if archive_files:
            keeper = max(archive_files, key=lambda f: f["size_bytes"] or 0)
            c["keep_reason"] = "archive"
        else:
            keeper = max(c["files"], key=lambda f: f["size_bytes"] or 0)
            c["keep_reason"] = "largest"
        for f in c["files"]:
            f["recommended_keep"] = f is keeper
    return clusters, latest_snap


@app.route("/duplicate-files")
@login_required
def duplicate_files():
    leaders = get_group_leaders()
    if not leaders:
        flash(
            "No group leader directories are mapped to your account yet. "
            "Contact an admin if you think this is wrong.",
            "error",
        )
        return redirect(url_for("index"))

    requested = request.args.get("leader", "")
    selected = next((l for l in leaders if l.lower() == requested.lower()), leaders[0])

    tolerance_pct = request.args.get("tolerance", 1, type=float)
    if tolerance_pct not in DUPLICATE_TOLERANCE_OPTIONS:
        tolerance_pct = 1

    clusters, latest_snap = find_leader_duplicate_files(selected, tolerance_pct)

    return render_template(
        "duplicate_files.html",
        leaders=leaders,
        selected_leader=selected,
        tolerance_pct=tolerance_pct,
        tolerance_options=DUPLICATE_TOLERANCE_OPTIONS,
        clusters=clusters,
        latest_snapshot=latest_snap,
        total_reclaimable_bytes=sum(c["reclaimable_bytes"] for c in clusters),
    )


def latest_snapshot_with_rows(table, where_sql, params):
    """
    The most recent snapshot that actually has matching rows in `table`
    (dir_extension_stats or dir_top_files), scoped by the same WHERE clause
    the caller's real query uses (e.g. a specific leader or path) — not just
    the single globally-latest snapshot.

    Big Files/Biggest Files' file-stats breakdown (dir_extension_stats/
    dir_top_files) is written by a slower, separate crawl step than
    directory_stats/subdirectory_stats — importing a new month's directories
    before that step finishes would otherwise make these pages suddenly show
    empty/partial data for a scope that a prior, already-complete snapshot
    still fully covers. table must be a literal from the caller, never
    request input — it's interpolated directly into the SQL.
    """
    return query(
        f"""
        SELECT s.id, s.run_date
        FROM snapshots s
        WHERE EXISTS (
            SELECT 1 FROM {table} t
            WHERE t.snapshot_id = s.id AND ({where_sql})
        )
        ORDER BY s.run_date DESC
        LIMIT 1
        """,
        tuple(params),
        one=True,
    )


@app.route("/api/extension_totals")
@login_required
def api_extension_totals():
    """
    Storage composition by file type, aggregated across every directory the
    current user can see (optionally narrowed to one group leader and/or
    area) — backs the chart on /big-files. Same build_access_sql restriction
    as every other data endpoint: a standard user only ever aggregates their
    own accessible directories, same as the table on that page.
    """
    leader = request.args.get("leader", "").strip()
    area = request.args.get("area", "").strip()

    scope_sql, scope_params = build_access_sql("t")
    scope_where = [f"({scope_sql})"]
    scope_params = list(scope_params)
    if leader:
        scope_where.append("t.directory = %s")
        scope_params.append(leader)

    latest_snap = latest_snapshot_with_rows(
        "dir_extension_stats", " AND ".join(scope_where), scope_params,
    )
    if not latest_snap:
        return jsonify({"extensions": [], "snapshot_date": None})

    access_sql, access_params = build_access_sql("des")
    where = ["des.snapshot_id = %s", f"({access_sql})"]
    params = [latest_snap["id"], *access_params]
    if leader:
        where.append("des.directory = %s")
        params.append(leader)

    rows = query(
        f"""
        SELECT des.index_label, des.extension, des.total_size_bytes
        FROM dir_extension_stats des
        WHERE {' AND '.join(where)}
        """,
        tuple(params),
    )

    totals = {}
    for r in rows:
        if area and short_area_label(r["index_label"]) != area:
            continue
        totals[r["extension"]] = totals.get(r["extension"], 0) + (r["total_size_bytes"] or 0)

    plan = load_extension_action_plan()
    result = []
    for ext, size in totals.items():
        classification = classify_extension(ext, plan)
        category, phrase = classification if classification else (None, None)
        result.append({
            "extension": ext, "total_size_bytes": size,
            "category": category, "phrase": phrase,
        })
    result.sort(key=lambda x: -x["total_size_bytes"])
    return jsonify({"extensions": result, "snapshot_date": latest_snap["run_date"].isoformat()})


@app.route("/api/top_files")
@login_required
def api_top_files():
    """
    Biggest 1000 files across every subdirectory of one group leader within
    one area — backs the "Biggest files" table on /big-files. Requires both
    leader and area (unlike /api/extension_totals, which can aggregate a
    leader across all their areas).

    Any file that would rank in this combined top-1000 is guaranteed to
    already be present in its own subdirectory's stored dir_top_files rows
    (removing files from other subdirectories can only improve a file's
    rank, never hurt it), so this is just a re-sort of already-collected
    data — no new crawl data is needed.
    """
    leader = request.args.get("leader", "").strip()
    area = request.args.get("area", "").strip()
    if not leader or not area:
        return jsonify({"files": [], "snapshot_date": None})

    scope_sql, scope_params = build_access_sql("t")
    latest_snap = latest_snapshot_with_rows(
        "dir_top_files", f"({scope_sql}) AND t.directory = %s", [*scope_params, leader],
    )
    if not latest_snap:
        return jsonify({"files": [], "snapshot_date": None})

    access_sql, access_params = build_access_sql("dtf")
    rows = query(
        f"""
        SELECT dtf.index_label, dtf.path, dtf.file_path, dtf.extension, dtf.size_bytes, dtf.mtime
        FROM dir_top_files dtf
        WHERE dtf.snapshot_id = %s AND dtf.directory = %s
          AND ({access_sql})
        """,
        (latest_snap["id"], leader, *access_params),
    )

    filtered = [r for r in rows if short_area_label(r["index_label"]) == area]
    filtered.sort(key=lambda r: r["size_bytes"] or 0, reverse=True)
    top = filtered[:1000]
    for i, r in enumerate(top, start=1):
        r["rank"] = i

    return jsonify({"files": top, "snapshot_date": latest_snap["run_date"].isoformat()})


@app.route("/biggest-files")
@login_required
def biggest_files():
    path = request.args.get("path", "").strip()
    if not path:
        flash("No directory specified.", "error")
        return redirect(url_for("index"))

    sub_access_sql, sub_access_params = build_access_sql("sds")
    subdir = query(
        f"""
        SELECT sds.*, s.run_date
        FROM subdirectory_stats sds
        JOIN snapshots s ON s.id = sds.snapshot_id
        WHERE sds.path = %s
          AND ({sub_access_sql})
        ORDER BY s.run_date DESC
        LIMIT 1
        """,
        (path, *sub_access_params),
        one=True,
    )
    if not subdir:
        flash("Directory not found or you don't have access to it.", "error")
        return redirect(url_for("index"))

    # Scoped to this specific path, not just the globally-latest snapshot —
    # extension_stats/top_files are written by a slower, separate crawl step
    # than subdirectory_stats above, so a brand new month's directories can
    # exist before this path's file-stats breakdown has been (re-)scanned.
    # extension_stats and top_files are always written together for a given
    # path in the same crawl pass, so one lookup (via dir_extension_stats)
    # covers both queries below.
    scope_sql, scope_params = build_access_sql("t")
    latest_snap = latest_snapshot_with_rows(
        "dir_extension_stats", f"({scope_sql}) AND t.path = %s", [*scope_params, path],
    )

    extension_stats = []
    top_files = []
    if latest_snap:
        ext_access_sql, ext_access_params = build_access_sql("des")
        extension_stats = query(
            f"""
            SELECT des.*
            FROM dir_extension_stats des
            WHERE des.snapshot_id = %s AND des.path = %s
              AND ({ext_access_sql})
            ORDER BY des.total_size_bytes DESC
            """,
            (latest_snap["id"], path, *ext_access_params),
        )

        top_access_sql, top_access_params = build_access_sql("dtf")
        top_files = query(
            f"""
            SELECT dtf.*
            FROM dir_top_files dtf
            WHERE dtf.snapshot_id = %s AND dtf.path = %s
              AND ({top_access_sql})
            ORDER BY dtf.rank
            """,
            (latest_snap["id"], path, *top_access_params),
        )

    total_size_bytes = sum((r["total_size_bytes"] or 0) for r in extension_stats)
    for r in extension_stats:
        r["pct"] = (r["total_size_bytes"] / total_size_bytes * 100) if total_size_bytes else 0

    return render_template(
        "biggest_files.html",
        subdir=subdir,
        area_label=short_area_label(subdir.get("index_label")),
        extension_stats=extension_stats,
        top_files=top_files,
        total_size_bytes=total_size_bytes,
        latest_snapshot=latest_snap,
    )


GROWTH_ALERT_PCT_OPTIONS = [5, 10, 20, 30, 50]
GROWTH_ALERT_GB_OPTIONS = [10, 50, 100, 500, 1000, 2000, 5000, 10000, 20000]


@app.route("/growth-alerts")
@admin_required
def growth_alerts():
    threshold_pct = request.args.get("pct", GROWTH_ALERT_THRESHOLD_PCT, type=float)
    if threshold_pct not in GROWTH_ALERT_PCT_OPTIONS:
        threshold_pct = GROWTH_ALERT_THRESHOLD_PCT

    threshold_gb = request.args.get("gb", GROWTH_ALERT_THRESHOLD_BYTES / 1e9, type=float)
    if threshold_gb not in GROWTH_ALERT_GB_OPTIONS:
        threshold_gb = GROWTH_ALERT_THRESHOLD_BYTES / 1e9
    threshold_bytes = threshold_gb * 1_000_000_000

    available_months = get_available_alert_months()  # descending "YYYY-MM"
    today = date.today()
    current_month_str = today.strftime("%Y-%m")
    current_year_str = today.strftime("%Y")
    if current_month_str not in available_months:
        available_months = [current_month_str] + available_months

    available_years = sorted({ym[:4] for ym in available_months}, reverse=True)

    selected_year = request.args.get("year", current_year_str)
    if selected_year not in available_years:
        selected_year = available_years[0] if available_years else current_year_str

    months_in_year = [ym for ym in available_months if ym[:4] == selected_year]  # already descending

    selected_month = request.args.get("month", "")
    if selected_month not in months_in_year:
        if selected_year == current_year_str and current_month_str in months_in_year:
            selected_month = current_month_str
        elif months_in_year:
            selected_month = months_in_year[0]
        else:
            selected_month = current_month_str

    is_current_month = selected_month == current_month_str
    if is_current_month:
        as_of = today
    else:
        year, month = (int(part) for part in selected_month.split("-"))
        as_of = date(year, month, calendar.monthrange(year, month)[1])

    year_options = available_years
    month_options = [
        {"value": ym, "label": date(int(ym[:4]), int(ym[5:7]), 1).strftime("%B")}
        for ym in months_in_year
    ]
    gb_options = [{"value": g, "label": format_gb_threshold_label(g)} for g in GROWTH_ALERT_GB_OPTIONS]

    alerts, period_snap = compute_growth_alerts(
        pct_threshold=threshold_pct, bytes_threshold=threshold_bytes, as_of=as_of,
    )
    duplicate_leaders, duplicate_snap = get_duplicate_leaderboard()

    cross_leader_min_gb = request.args.get("min_gb", 25, type=int)
    if cross_leader_min_gb not in CROSS_LEADER_DUPLICATE_MIN_GB_OPTIONS:
        cross_leader_min_gb = 25
    cross_leader_clusters, cross_leader_snap = get_cross_leader_duplicate_clusters(
        min_size_gb=cross_leader_min_gb,
    )
    most_files_min_count = request.args.get("min_files", MOST_FILES_MIN_COUNT_DEFAULT, type=int)
    if most_files_min_count is None or most_files_min_count < 0:
        most_files_min_count = MOST_FILES_MIN_COUNT_DEFAULT
    most_files_rows, most_files_snap = get_most_files_leaderboard(min_file_count=most_files_min_count)
    return render_template(
        "growth_alerts.html",
        alerts=alerts,
        latest_snap=period_snap,
        threshold_pct=threshold_pct,
        threshold_bytes=threshold_bytes,
        threshold_gb=threshold_gb,
        threshold_gb_label=format_gb_threshold_label(threshold_gb),
        year_options=year_options,
        selected_year=selected_year,
        month_options=month_options,
        selected_month=selected_month,
        is_current_month=is_current_month,
        pct_options=GROWTH_ALERT_PCT_OPTIONS,
        gb_options=gb_options,
        duplicate_leaders=duplicate_leaders,
        duplicate_snap=duplicate_snap,
        cross_leader_clusters=cross_leader_clusters,
        cross_leader_snap=cross_leader_snap,
        cross_leader_min_gb=cross_leader_min_gb,
        cross_leader_min_gb_options=CROSS_LEADER_DUPLICATE_MIN_GB_OPTIONS,
        most_files_rows=most_files_rows,
        most_files_snap=most_files_snap,
        most_files_min_count=most_files_min_count,
        most_files_min_count_step=MOST_FILES_MIN_COUNT_STEP,
    )


def get_duplicate_leaderboard(limit=20):
    """
    Precomputed "possible duplicate files" totals per group leader (see
    import_csv.py's compute_and_store_duplicate_summaries()), from the
    latest snapshot that actually has leader_duplicate_summary rows — not
    necessarily the globally-latest snapshot, in case this month's crawl
    hasn't reached the file-stats step yet. Sorted by reclaimable space
    descending. System-wide, admin-only view — not scoped to any one
    viewer's access, same as the rest of Storage Alerts & Insights.
    """
    latest_snap = query(
        """
        SELECT s.id, s.run_date
        FROM snapshots s
        WHERE EXISTS (SELECT 1 FROM leader_duplicate_summary t WHERE t.snapshot_id = s.id)
        ORDER BY s.run_date DESC
        LIMIT 1
        """,
        one=True,
    )
    if not latest_snap:
        return [], None

    rows = query(
        """
        SELECT directory, tolerance_pct, cluster_count, reclaimable_bytes
        FROM leader_duplicate_summary
        WHERE snapshot_id = %s
        ORDER BY reclaimable_bytes DESC
        """,
        (latest_snap["id"],),
    )
    # Filtered here (not baked into compute_and_store_duplicate_summaries())
    # so hiding/unhiding a leader from Admin -> Access Control takes effect
    # immediately, without waiting for the next monthly import to re-run.
    hidden = get_hidden_group_leaders()
    rows = [r for r in rows if (r.get("directory") or "").strip().lower() not in hidden]
    return rows[:limit], latest_snap


CROSS_LEADER_DUPLICATE_MIN_GB_OPTIONS = [10, 25, 50]


def get_cross_leader_duplicate_clusters(limit=30, min_size_gb=10):
    """
    Precomputed cross-group duplicate-file clusters — the same file (by name
    + size) found under more than one group leader, which the per-leader
    Duplicate Files page and the leaderboard above can never surface since
    each only ever looks within one leader's own scope at a time. See
    import_csv.py's compute_and_store_cross_leader_duplicates(). Same
    "latest snapshot that actually has rows" reasoning as
    get_duplicate_leaderboard(). System-wide, admin-only, not scoped to any
    one viewer's access. Not filtered by get_hidden_group_leaders() — that
    feature is scoped specifically to the per-leader leaderboard above, not
    this cross-group view.

    min_size_gb raises the bar above the 10 GB floor import_csv.py already
    precomputes at (CROSS_LEADER_DUPLICATE_MIN_SIZE_BYTES) — applied here,
    at display time, against each cluster's smallest member file, rather
    than needing a separate precomputed table per threshold. Safe to do
    after the fact because clustering only ever groups files within 1% of
    each other's size, so a cluster's members are never far enough apart for
    this to misclassify one as meeting a higher threshold it doesn't.
    """
    latest_snap = query(
        """
        SELECT s.id, s.run_date
        FROM snapshots s
        WHERE EXISTS (SELECT 1 FROM cross_leader_duplicate_clusters t WHERE t.snapshot_id = s.id)
        ORDER BY s.run_date DESC
        LIMIT 1
        """,
        one=True,
    )
    if not latest_snap:
        return [], None

    clusters = query(
        """
        SELECT id, basename, extension, leader_count, file_count, total_size_bytes, reclaimable_bytes
        FROM cross_leader_duplicate_clusters
        WHERE snapshot_id = %s
        ORDER BY reclaimable_bytes DESC
        """,
        (latest_snap["id"],),
    )
    if not clusters:
        return [], latest_snap

    cluster_ids = [c["id"] for c in clusters]
    placeholders = ", ".join(["%s"] * len(cluster_ids))
    files = query(
        f"""
        SELECT cluster_id, leader, index_label, file_path, size_bytes, mtime
        FROM cross_leader_duplicate_files
        WHERE cluster_id IN ({placeholders})
        ORDER BY size_bytes DESC
        """,
        cluster_ids,
    )
    files_by_cluster = {}
    for f in files:
        f["index_label_short"] = short_area_label(f["index_label"])
        files_by_cluster.setdefault(f["cluster_id"], []).append(f)

    min_size_bytes = min_size_gb * 1_000_000_000
    filtered = []
    for c in clusters:
        c["files"] = files_by_cluster.get(c["id"], [])
        if not c["files"]:
            continue
        if min(f["size_bytes"] or 0 for f in c["files"]) < min_size_bytes:
            continue
        filtered.append(c)

    return filtered[:limit], latest_snap


MOST_FILES_MIN_COUNT_DEFAULT = 20000
MOST_FILES_MIN_COUNT_STEP = 5000


def get_most_files_leaderboard(min_file_count=MOST_FILES_MIN_COUNT_DEFAULT, limit=500):
    """
    Subdirectories with the most individual files, system-wide, ranked by
    total file count — for spotting directories with an unreasonable number
    of files (e.g. millions of tiny files), which is its own kind of storage
    problem independent of total size. A quick aggregate over
    dir_extension_stats (SUM(file_count) per path) rather than a precomputed
    table — no clustering/heuristic involved here, unlike the duplicate-file
    leaderboards, so this is cheap enough to just query live.

    min_file_count is a floor, not a row cap — lower it (the page steps it by
    MOST_FILES_MIN_COUNT_STEP via ▲/▼ buttons) to surface more directories,
    same idea as get_cross_leader_duplicate_clusters()'s min_size_gb, just
    free-form instead of a fixed option list. limit is just a sanity cap so
    a very low floor can't return an unbounded page.

    Same scope limitation as everything built on dir_extension_stats: only
    ever covers the Groups-type leader subdirectories the crawler collects
    file-stats for (the same ones Big Files/Duplicate Files cover), not
    every area in the dashboard. Latest snapshot that actually has
    dir_extension_stats rows, same reasoning as the other leaderboards here.
    System-wide, admin-only — not scoped to any one viewer's access, and not
    filtered by get_hidden_group_leaders() (that's scoped specifically to
    the duplicate-file leaderboard, not this one).
    """
    latest_snap = query(
        """
        SELECT s.id, s.run_date
        FROM snapshots s
        WHERE EXISTS (SELECT 1 FROM dir_extension_stats t WHERE t.snapshot_id = s.id)
        ORDER BY s.run_date DESC
        LIMIT 1
        """,
        one=True,
    )
    if not latest_snap:
        return [], None

    rows = query(
        """
        SELECT directory, path, index_label, SUM(file_count) AS total_files,
               SUM(total_size_bytes) AS total_size_bytes
        FROM dir_extension_stats
        WHERE snapshot_id = %s
        GROUP BY directory, path, index_label
        HAVING SUM(file_count) >= %s
        ORDER BY total_files DESC
        LIMIT %s
        """,
        (latest_snap["id"], min_file_count, limit),
    )
    for r in rows:
        r["index_label_short"] = short_area_label(r["index_label"])
    return rows, latest_snap


@app.route("/api/leader_evolution_by_tier")
@login_required
def api_leader_evolution_by_tier():
    name = request.args.get("name", "").strip()
    metric = request.args.get("metric", "size").strip()
    if not name or metric not in LEADER_TIER_METRICS:
        return jsonify({"tiers": [], "series": []})
    tiers, series = build_leader_tier_timeseries(name, LEADER_TIER_METRICS[metric])
    return jsonify({"tiers": tiers, "series": series})


@app.route("/api/all_groups_evolution_by_tier")
@admin_required
def api_all_groups_evolution_by_tier():
    metric = request.args.get("metric", "size").strip()
    if metric not in LEADER_TIER_METRICS:
        return jsonify({"tiers": [], "series": []})
    tiers, series = build_all_groups_tier_timeseries(LEADER_TIER_METRICS[metric])
    return jsonify({"tiers": tiers, "series": series})


@app.route("/api/institute_evolution_by_tier")
@admin_required
def api_institute_evolution_by_tier():
    metric = request.args.get("metric", "size").strip()
    if metric not in LEADER_TIER_METRICS:
        return jsonify({"tiers": [], "series": []})
    tiers, series = build_institute_tier_timeseries(LEADER_TIER_METRICS[metric])
    return jsonify({"tiers": tiers, "series": series})


# Display-only, this page alone: the tiles and the chart's legend/tooltip
# (see institute_trends.html's TIER_CHART_OPTS.tierDisplayNames) show
# "Legacy" as "Core" here. Everywhere else that tier is still called
# "Legacy" — this doesn't touch INSTITUTE_TIERS, tier_bucket_for(), or
# AREA_DISPLAY_OVERRIDES, so no other page/export is affected.
INSTITUTE_TRENDS_TIER_DISPLAY_OVERRIDES = {"Legacy": "Core"}


@app.route("/institute-trends")
@admin_required
def institute_trends():
    latest_snap = query("SELECT id, run_date FROM snapshots ORDER BY run_date DESC LIMIT 1", one=True)
    prev_snap = _find_previous_snapshot(latest_snap["run_date"]) if latest_snap else None

    latest_totals = compute_institute_tier_totals(latest_snap["id"] if latest_snap else None)
    prev_totals = compute_institute_tier_totals(prev_snap["id"] if prev_snap else None)

    tier_tiles = []
    for tier in list(INSTITUTE_TIERS) + ["Other"]:
        curr = latest_totals.get(tier)
        if curr is None:
            continue
        prev = prev_totals.get(tier)
        delta = (curr - prev) if prev is not None else None
        tier_tiles.append({
            "tier": INSTITUTE_TRENDS_TIER_DISPLAY_OVERRIDES.get(tier, tier),
            "size_bytes": curr,
            "delta_bytes": delta,
        })

    if tier_tiles:
        total_size = sum(t["size_bytes"] for t in tier_tiles)
        # None (not 0) whenever ANY tier's own delta is None — a tier with no
        # prior-snapshot data contributes an unknown change, not a known-zero
        # one, so treating it as 0 here would silently understate (or
        # overstate) the real total change. The per-tier tile already shows
        # "no prior snapshot" in that case; the Total should be equally
        # honest rather than implying a precise number it doesn't have.
        if prev_snap and all(t["delta_bytes"] is not None for t in tier_tiles):
            total_delta = sum(t["delta_bytes"] for t in tier_tiles)
        else:
            total_delta = None
        tier_tiles.append({"tier": "Total", "size_bytes": total_size, "delta_bytes": total_delta})

    return render_template(
        "institute_trends.html",
        latest_snapshot=latest_snap,
        has_prev=bool(prev_snap),
        tier_tiles=tier_tiles,
    )


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    sync_area_settings()

    if request.method == "POST":
        selected = set(request.form.getlist("enabled_areas"))
        all_rows = get_all_area_settings()
        for row in all_rows:
            enabled = 1 if row["index_label"] in selected else 0
            execute(
                "UPDATE area_settings SET enabled = %s WHERE index_label = %s",
                (enabled, row["index_label"]),
            )
        flash("Area visibility updated.", "info")
        return redirect(url_for("settings"))

    area_rows = get_all_area_settings()
    areas = [
        {
            "value": row["index_label"],
            "label": short_area_label(row["index_label"]),
            "enabled": bool(row["enabled"]),
        }
        for row in area_rows
    ]
    areas.sort(key=lambda a: a["label"].lower())
    return render_template("settings.html", areas=areas, active_tab="settings")


@app.route("/access", methods=["GET", "POST"])
@admin_required
def access_admin():
    ensure_admin_tables()

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "add_full_access":
            username = (request.form.get("username") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            if username:
                execute(
                    "INSERT INTO full_access_users (username, notes) VALUES (%s, %s) ON DUPLICATE KEY UPDATE notes = VALUES(notes)",
                    (username, notes or None),
                )
                flash("Full-access user saved.", "info")
        elif action == "remove_full_access":
            username = (request.form.get("username") or "").strip()
            if username:
                execute("DELETE FROM full_access_users WHERE username = %s", (username,))
                flash("Full-access user removed.", "info")
        elif action == "add_rule":
            index_label = (request.form.get("index_label") or "").strip()
            path_prefix = (request.form.get("path_prefix") or "").strip()
            ldap_group_cn = (request.form.get("ldap_group_cn") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            if index_label and path_prefix and ldap_group_cn:
                execute(
                    """
                    INSERT INTO directory_group_rules (index_label, path_prefix, ldap_group_cn, notes)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE notes = VALUES(notes)
                    """,
                    (index_label, path_prefix, ldap_group_cn, notes or None),
                )
                flash("Directory access rule saved.", "info")
        elif action == "remove_rule":
            rule_id = request.form.get("rule_id", type=int)
            if rule_id:
                execute("DELETE FROM directory_group_rules WHERE id = %s", (rule_id,))
                flash("Directory access rule removed.", "info")
        elif action == "add_hidden_leader":
            directory = (request.form.get("directory") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            if directory:
                execute(
                    "INSERT INTO hidden_group_leaders (directory, notes) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE notes = VALUES(notes)",
                    (directory, notes or None),
                )
                flash(f"{directory} hidden from the Storage Alerts & Insights duplicate-file leaderboard.", "info")
        elif action == "remove_hidden_leader":
            directory = (request.form.get("directory") or "").strip()
            if directory:
                execute("DELETE FROM hidden_group_leaders WHERE directory = %s", (directory,))
                flash(f"{directory} is back on the Storage Alerts & Insights duplicate-file leaderboard.", "info")

        return redirect(url_for("access_admin"))

    areas = [
        {"value": row["index_label"], "label": short_area_label(row["index_label"])}
        for row in query("SELECT DISTINCT index_label FROM directory_stats ORDER BY index_label")
    ]
    areas.sort(key=lambda a: a["label"].lower())
    latest_snap = query("SELECT id FROM snapshots ORDER BY run_date DESC LIMIT 1", one=True)
    sample_paths = query(
        """
        SELECT index_label, path, directory
        FROM directory_stats
        WHERE snapshot_id = %s
        ORDER BY index_label, directory
        """,
        (latest_snap["id"],),
    ) if latest_snap else []
    for row in sample_paths:
        row["index_label_short"] = short_area_label(row["index_label"])
    full_access_users = query(
        "SELECT username, notes, updated_at FROM full_access_users ORDER BY username"
    )
    rules = query(
        """
        SELECT id, index_label, path_prefix, ldap_group_cn, notes, updated_at
        FROM directory_group_rules
        ORDER BY index_label, path_prefix, ldap_group_cn
        """
    )
    for row in rules:
        row["index_label_short"] = short_area_label(row["index_label"])

    ldap_groups = get_available_ldap_groups()
    if not ldap_groups:
        ldap_groups = get_db_group_suggestions()
    if not ldap_groups:
        ldap_groups = sorted(
            {
                group_name
                for group_name in (set(current_group_cns()) | {row["ldap_group_cn"] for row in rules})
                if is_rg_group_name(group_name)
            },
            key=str.lower,
        )

    hidden_leaders = query(
        "SELECT directory, notes, updated_at FROM hidden_group_leaders ORDER BY directory"
    )

    return render_template(
        "access_admin.html",
        areas=areas,
        sample_paths=sample_paths,
        full_access_users=full_access_users,
        rules=rules,
        ldap_groups=ldap_groups,
        all_leaders=get_group_leaders(),
        hidden_leaders=hidden_leaders,
        active_tab="access",
    )


@app.route("/settings/extensions", methods=["GET", "POST"])
@admin_required
def extension_actions():
    ensure_extension_action_plan_table()

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "save":
            extension = (request.form.get("extension") or "").strip().lower().lstrip(".")
            category = (request.form.get("category") or "").strip().lower()
            phrase = (request.form.get("phrase") or "").strip()
            if extension and category in EXTENSION_CATEGORIES:
                execute(
                    """
                    INSERT INTO extension_action_plan (extension, category, phrase)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE category = VALUES(category), phrase = VALUES(phrase)
                    """,
                    (extension, category, phrase or None),
                )
                flash(f"Saved .{extension}.", "info")
            else:
                flash("An extension and a valid category are required.", "error")
        elif action == "delete":
            extension = (request.form.get("extension") or "").strip().lower()
            if extension:
                execute("DELETE FROM extension_action_plan WHERE extension = %s", (extension,))
                flash(f".{extension} removed — back to unclassified.", "info")

        return redirect(url_for("extension_actions"))

    seed_extension_action_plan()
    rows = query(
        "SELECT extension, category, phrase FROM extension_action_plan ORDER BY category, extension"
    )
    return render_template(
        "extension_actions.html", rows=rows, categories=EXTENSION_CATEGORIES, active_tab="extensions"
    )


@app.route("/api/trend")
@login_required
def api_trend():
    """Return stale-size and total-size time-series data for one directory path."""
    path = request.args.get("path", "")
    directory = request.args.get("directory", "").strip()
    index_label = request.args.get("area", "").strip()
    area_short = request.args.get("area_short", "").strip()
    if not path and not (directory and (index_label or area_short)):
        return jsonify([])
    access_sql, access_params = build_access_sql("ds")
    if has_stale_size_columns():
        select_cols = "ds.stale_1yr_bytes, ds.stale_2yr_bytes, ds.stale_4yr_bytes"
    else:
        select_cols = "NULL AS stale_1yr_bytes, NULL AS stale_2yr_bytes, NULL AS stale_4yr_bytes"

    if directory and (index_label or area_short):
        sql = f"""
            SELECT s.run_date, ds.index_label, ds.path, ds.dir_size_bytes, ds.dir_size_raw,
                   {select_cols}
            FROM directory_stats ds
            JOIN snapshots s ON s.id = ds.snapshot_id
            WHERE LOWER(ds.directory) = %s
              AND ({access_sql})
            ORDER BY s.run_date
        """
        candidate_rows = query(sql, (directory.lower(), *access_params))

        target_area_key = normalise_area_key(area_short or index_label)
        rows_by_date = {}
        matched_paths = set()
        for row in candidate_rows:
            if index_label and row.get("index_label") == index_label:
                matches_area = True
            else:
                row_area_key = normalise_area_key(row.get("index_label"))
                matches_area = bool(target_area_key) and target_area_key in row_area_key
            if not matches_area:
                continue
            matched_paths.add(row["path"])

            run_date = row["run_date"]
            existing = rows_by_date.get(run_date)
            stale_1yr = _effective_stale_bytes(row, "stale_1yr_bytes") or 0
            stale_2yr = _effective_stale_bytes(row, "stale_2yr_bytes") or 0
            stale_4yr = _effective_stale_bytes(row, "stale_4yr_bytes") or 0
            if existing is None:
                rows_by_date[run_date] = {
                    "run_date": run_date,
                    "dir_size_bytes": int(row.get("dir_size_bytes") or 0),
                    "dir_size_raw": row.get("dir_size_raw"),
                    "stale_1yr_bytes": stale_1yr,
                    "stale_2yr_bytes": stale_2yr,
                    "stale_4yr_bytes": stale_4yr,
                }
                continue

            existing["dir_size_bytes"] += int(row.get("dir_size_bytes") or 0)
            existing["stale_1yr_bytes"] += stale_1yr
            existing["stale_2yr_bytes"] += stale_2yr
            existing["stale_4yr_bytes"] += stale_4yr
            existing["dir_size_raw"] = f"{existing['dir_size_bytes'] / 1e12:.1f} TB"

        # Merge in Isilon's daily SmartQuotas size data across the same set
        # of paths this leader+area aggregate covers (see build_snapshot_
        # timeseries for the same pattern / rationale).
        daily_totals = isilon_daily_totals(list(matched_paths))
        if daily_totals:
            for stat_date, total_bytes in daily_totals.items():
                entry = rows_by_date.get(stat_date)
                if entry is None:
                    entry = {
                        "run_date": stat_date,
                        "dir_size_bytes": None,
                        "dir_size_raw": None,
                        "stale_1yr_bytes": None,
                        "stale_2yr_bytes": None,
                        "stale_4yr_bytes": None,
                    }
                    rows_by_date[stat_date] = entry
                entry["dir_size_bytes"] = total_bytes
                entry["dir_size_raw"] = None

        rows = [rows_by_date[run_date] for run_date in sorted(rows_by_date)]
    else:
        sql = f"""
            SELECT s.run_date, ds.dir_size_bytes, ds.dir_size_raw,
                   {select_cols}
            FROM directory_stats ds
            JOIN snapshots s ON s.id = ds.snapshot_id
            WHERE ds.path = %s
              AND ({access_sql})
            ORDER BY s.run_date
        """
        rows = query(sql, (path, *access_params))

        if not rows and path:
            # Not a top-level directory — check subdirectory_stats instead
            # (no stale-size data is collected for subdirectories yet).
            sub_access_sql, sub_access_params = build_access_sql("sds")
            sql = f"""
                SELECT s.run_date, sds.dir_size_bytes, sds.dir_size_raw,
                       NULL AS stale_1yr_bytes, NULL AS stale_2yr_bytes, NULL AS stale_4yr_bytes
                FROM subdirectory_stats sds
                JOIN snapshots s ON s.id = sds.snapshot_id
                WHERE sds.path = %s
                  AND ({sub_access_sql})
                ORDER BY s.run_date
            """
            rows = query(sql, (path, *sub_access_params))

        # Merge in Isilon's daily SmartQuotas size data for this exact path,
        # where available — it's continuously tracked by OneFS (more accurate,
        # far more frequent than the monthly Diskover crawl), though it never
        # carries staleness. Only done once the monthly query above already
        # returned rows, since that's what established this path is one the
        # current user has access to — isilon_quota_stats has no directory/
        # index_label columns of its own for build_access_sql to filter on.
        if path and rows:
            daily_rows = query(
                "SELECT stat_date, logical_bytes FROM isilon_quota_stats "
                "WHERE path = %s ORDER BY stat_date",
                (path,),
            )
            if daily_rows:
                by_date = {r["run_date"]: r for r in rows}
                for d in daily_rows:
                    entry = by_date.get(d["stat_date"])
                    if entry is None:
                        entry = {
                            "run_date": d["stat_date"],
                            "dir_size_bytes": None,
                            "dir_size_raw": None,
                            "stale_1yr_bytes": None,
                            "stale_2yr_bytes": None,
                            "stale_4yr_bytes": None,
                        }
                        by_date[d["stat_date"]] = entry
                    entry["dir_size_bytes"] = d["logical_bytes"]
                    entry["dir_size_raw"] = None
                rows = [by_date[k] for k in sorted(by_date)]

    # make dates JSON-serialisable
    for r in rows:
        r["stale_1yr_bytes_eff"] = _effective_stale_bytes(r, "stale_1yr_bytes")
        r["stale_2yr_bytes_eff"] = _effective_stale_bytes(r, "stale_2yr_bytes")
        r["stale_4yr_bytes_eff"] = _effective_stale_bytes(r, "stale_4yr_bytes")
        r["size_tb"] = round(float(r.get("dir_size_bytes") or 0) / 1e12, 3)
        # Preserve a real gap (None) instead of coercing to 0 — a daily-only
        # point genuinely has no staleness data, which isn't the same as zero
        # stale bytes. The frontend's Chart.js line charts already treat null
        # data points as a gap by default (no spanGaps set anywhere).
        r["stale_1yr_tb"] = None if r["stale_1yr_bytes_eff"] is None else round(float(r["stale_1yr_bytes_eff"]) / 1e12, 3)
        r["stale_2yr_tb"] = None if r["stale_2yr_bytes_eff"] is None else round(float(r["stale_2yr_bytes_eff"]) / 1e12, 3)
        r["stale_4yr_tb"] = None if r["stale_4yr_bytes_eff"] is None else round(float(r["stale_4yr_bytes_eff"]) / 1e12, 3)
        r["run_date"] = r["run_date"].isoformat()
    return jsonify(rows)


@app.route("/api/area_summary")
@login_required
def api_area_summary():
    """Total size and stale-size totals per area for a snapshot."""
    snap_id = request.args.get("snap", type=int)
    if not snap_id:
        return jsonify([])
    access_sql, access_params = build_access_sql("ds")
    if has_stale_size_columns():
        sql = """
             SELECT ds.index_label,
                 SUM(ds.dir_size_bytes) AS size_bytes,
                 SUM(ds.stale_1yr_bytes) AS stale_1yr_bytes,
                 SUM(ds.stale_2yr_bytes) AS stale_2yr_bytes,
                 SUM(ds.stale_4yr_bytes) AS stale_4yr_bytes
             FROM directory_stats ds
             JOIN area_settings aset ON aset.index_label = ds.index_label
             WHERE ds.snapshot_id = %s
                             AND aset.enabled = 1
             GROUP BY ds.index_label
            ORDER BY size_bytes DESC
        """
    else:
        sql = """
             SELECT ds.index_label,
                 SUM(ds.dir_size_bytes) AS size_bytes,
                   NULL AS stale_1yr_bytes,
                   NULL AS stale_2yr_bytes,
                   NULL AS stale_4yr_bytes
             FROM directory_stats ds
             JOIN area_settings aset ON aset.index_label = ds.index_label
             WHERE ds.snapshot_id = %s
               AND aset.enabled = 1
             GROUP BY ds.index_label
            ORDER BY size_bytes DESC
        """

    sql = sql.replace("GROUP BY ds.index_label", f"AND ({access_sql})\n             GROUP BY ds.index_label")

    rows = query(sql, (snap_id, *access_params))
    for r in rows:
        r["display_label"] = short_area_label(r["index_label"])
        size_bytes = int(r["size_bytes"] or 0)
        stale1_bytes = int(r["stale_1yr_bytes"] or 0)
        stale2_bytes = int(r["stale_2yr_bytes"] or 0)
        stale4_bytes = int(r["stale_4yr_bytes"] or 0)

        r["size_bytes"] = size_bytes
        r["stale_1yr_bytes"] = stale1_bytes
        r["stale_2yr_bytes"] = stale2_bytes
        r["stale_4yr_bytes"] = stale4_bytes
        r["size_tb"] = round(size_bytes / 1e12, 2)
        r["stale_1yr_tb"] = round(r["stale_1yr_bytes"] / 1e12, 2)
        r["stale_2yr_tb"] = round(r["stale_2yr_bytes"] / 1e12, 2)
        r["stale_4yr_tb"] = round(r["stale_4yr_bytes"] / 1e12, 2)
    return jsonify(rows)


@app.route("/api/evolution")
@login_required
def api_evolution():
    area = request.args.get("area", "")
    path = request.args.get("path", "")

    if path:
        rows = build_snapshot_timeseries("ds.path = %s", (path,))
    elif area:
        rows = build_snapshot_timeseries("ds.index_label = %s", (area,))
    else:
        rows = build_enabled_areas_timeseries()

    return jsonify(rows)


if __name__ == "__main__":
    # Production runs under gunicorn (see deploy/), which never executes this
    # block — this is only for ad-hoc local runs (`python app.py`), so debug
    # defaults OFF: Werkzeug's debugger shows full stack traces and an
    # interactive Python console on any unhandled exception, which is fine on
    # a developer's own machine but not something to risk leaving on during a
    # live demo. Opt in explicitly with FLASK_DEBUG=1 for local development.
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
