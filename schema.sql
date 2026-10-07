-- Diskover storage dashboard schema
-- MySQL / MariaDB
-- Run once: mysql -u root -p diskover_dashboard < schema.sql

CREATE DATABASE IF NOT EXISTS diskover_dashboard
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE diskover_dashboard;

-- ── One row per monthly scrape run ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS snapshots (
    id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_date   DATE         NOT NULL,          -- e.g. 2026-09-01 (first of month)
    imported_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes      VARCHAR(500)                    -- optional free-text comment
) ENGINE=InnoDB;

CREATE UNIQUE INDEX uq_snapshots_run_date ON snapshots (run_date);

-- ── One row per directory per snapshot ───────────────────────────────────────
-- Size-first model: stale metrics are stored in bytes.
-- Human-readable size strings (e.g. "3.2 TB") are stored alongside so we
-- never lose the original value if parsing is imperfect.
CREATE TABLE IF NOT EXISTS directory_stats (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    snapshot_id     INT UNSIGNED    NOT NULL,
    directory       VARCHAR(500)    NOT NULL,   -- leaf name  (e.g. "Haseloff")
    path            VARCHAR(1000)   NOT NULL,   -- short path (e.g. "JIC_Apricot_Group_Scratch/Haseloff")
    index_label     VARCHAR(300)    NOT NULL,   -- area label (e.g. "JIC Apricot / Group Scratch")
    dir_size_raw    VARCHAR(50),                -- original string from Diskover ("3.20 TB")
    dir_size_bytes  BIGINT UNSIGNED,            -- parsed bytes (NULL if parse failed)
    stale_1yr_bytes BIGINT UNSIGNED,            -- stale >1yr size in bytes (estimated)
    stale_2yr_bytes BIGINT UNSIGNED,            -- stale >2yr size in bytes (estimated)
    stale_4yr_bytes BIGINT UNSIGNED,            -- stale >4yr size in bytes (estimated)

    CONSTRAINT fk_ds_snapshot FOREIGN KEY (snapshot_id)
        REFERENCES snapshots (id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE INDEX idx_ds_snapshot   ON directory_stats (snapshot_id);
CREATE INDEX idx_ds_path       ON directory_stats (path(255));
CREATE INDEX idx_ds_index      ON directory_stats (index_label(100));

-- ── One row per subdirectory (one level below a group-leader directory) ─────
-- Only populated for "Groups"-type areas (Research Groups / Group Scratch /
-- Archive Groups) — see diskover_complex.py --subdirs. Size only, no stale
-- metrics yet. `directory` holds the top-level leader name (same semantics as
-- directory_stats.directory) so existing access-control queries (build_access_sql)
-- can filter this table unchanged.
CREATE TABLE IF NOT EXISTS subdirectory_stats (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    snapshot_id     INT UNSIGNED    NOT NULL,
    directory       VARCHAR(500)    NOT NULL,   -- top-level leader name (e.g. "Haseloff")
    parent_path     VARCHAR(1000)   NOT NULL,   -- leader's own short path
    subdirectory    VARCHAR(500)    NOT NULL,   -- leaf name (e.g. "raw_data")
    path            VARCHAR(1000)   NOT NULL,   -- short path incl. subdirectory
    index_label     VARCHAR(300)    NOT NULL,
    dir_size_raw    VARCHAR(50),
    dir_size_bytes  BIGINT UNSIGNED,

    CONSTRAINT fk_sds_snapshot FOREIGN KEY (snapshot_id)
        REFERENCES snapshots (id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE INDEX idx_sds_snapshot    ON subdirectory_stats (snapshot_id);
CREATE INDEX idx_sds_path        ON subdirectory_stats (path(255));
CREATE INDEX idx_sds_parent_path ON subdirectory_stats (parent_path(255));
CREATE INDEX idx_sds_directory   ON subdirectory_stats (directory);

-- ── One row per file extension per subdirectory_stats row per snapshot ──────
-- Exact size/count totals across every file under that subdirectory (not a
-- sample) — see diskover_complex.py get_dir_file_stats(). Kept as normal
-- history (cascades with its snapshot like directory_stats/subdirectory_stats,
-- no special pruning). `directory` mirrors subdirectory_stats.directory (the
-- top-level leader name) so build_access_sql's home-directory and
-- inferred-group-name clauses, which key off {alias}.directory, work here
-- unchanged.
CREATE TABLE IF NOT EXISTS dir_extension_stats (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    snapshot_id      INT UNSIGNED    NOT NULL,
    directory        VARCHAR(500)    NOT NULL,   -- top-level leader name
    path             VARCHAR(1000)   NOT NULL,   -- subdirectory_stats.path this belongs to
    index_label      VARCHAR(300)    NOT NULL,
    extension        VARCHAR(255)    NOT NULL,   -- lowercased; "(none)" if the file has none
    file_count       INT UNSIGNED    NOT NULL,
    total_size_bytes BIGINT UNSIGNED NOT NULL,

    CONSTRAINT fk_des_snapshot FOREIGN KEY (snapshot_id)
        REFERENCES snapshots (id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE INDEX idx_des_snapshot  ON dir_extension_stats (snapshot_id);
CREATE INDEX idx_des_path      ON dir_extension_stats (path(255));
CREATE INDEX idx_des_directory ON dir_extension_stats (directory);

-- ── Up to 1000 biggest individual files per subdirectory_stats row/snapshot ──
-- Unlike dir_extension_stats, this table is NOT kept as long-term history —
-- import_csv.py prunes rows belonging to snapshots older than last month right
-- after each import, since this is the bulky one (up to 1000 rows per
-- subdirectory every month). directory_stats/subdirectory_stats/
-- dir_extension_stats history is untouched by that prune.
CREATE TABLE IF NOT EXISTS dir_top_files (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    snapshot_id  INT UNSIGNED    NOT NULL,
    directory    VARCHAR(500)    NOT NULL,   -- top-level leader name
    path         VARCHAR(1000)   NOT NULL,   -- subdirectory_stats.path this belongs to
    index_label  VARCHAR(300)    NOT NULL,
    file_path    VARCHAR(1500)   NOT NULL,   -- full path of the file itself
    extension    VARCHAR(255)    NOT NULL,
    size_bytes   BIGINT UNSIGNED NOT NULL,
    mtime        VARCHAR(50),                -- raw "Date Modified" text from Diskover
    rank         SMALLINT UNSIGNED NOT NULL, -- 1..1000, descending by size

    CONSTRAINT fk_dtf_snapshot FOREIGN KEY (snapshot_id)
        REFERENCES snapshots (id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE INDEX idx_dtf_snapshot  ON dir_top_files (snapshot_id);
CREATE INDEX idx_dtf_path      ON dir_top_files (path(255));
CREATE INDEX idx_dtf_directory ON dir_top_files (directory);

-- ── Precomputed "possible duplicate files" totals per group leader ───────────
-- One row per (snapshot, leader), computed once at import time (see
-- import_csv.py's compute_and_store_duplicate_summaries(), using the same
-- clustering as app.py's live /duplicate-files page — see duplicate_finder.py)
-- rather than live on every page view, since clustering every leader's
-- dir_top_files on demand would be too slow for an on-the-fly admin page.
-- Only the leaderboard numbers are stored here; the live page still computes
-- the full per-file breakdown for one leader at a time on request.
CREATE TABLE IF NOT EXISTS leader_duplicate_summary (
    id                BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    snapshot_id       INT UNSIGNED    NOT NULL,
    directory         VARCHAR(500)    NOT NULL,   -- leader name
    tolerance_pct     DECIMAL(4,1)    NOT NULL,   -- which size tolerance this was computed at
    cluster_count     INT UNSIGNED    NOT NULL,
    reclaimable_bytes BIGINT UNSIGNED NOT NULL,

    CONSTRAINT fk_lds_snapshot FOREIGN KEY (snapshot_id)
        REFERENCES snapshots (id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE INDEX idx_lds_snapshot ON leader_duplicate_summary (snapshot_id);

-- ── Cross-group duplicate data: the same file (by name+size) found under more
-- than one group leader's directories — a signal that several groups have
-- independently downloaded/kept the same dataset, worth consolidating into a
-- shared location instead. Deliberately separate from leader_duplicate_summary
-- above, which only looks within one leader's own scope. Recomputed (deleted
-- and reinserted) each month, same as leader_duplicate_summary — no long-term
-- history kept, only the latest snapshot's view is ever shown.
CREATE TABLE IF NOT EXISTS cross_leader_duplicate_clusters (
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
) ENGINE=InnoDB;

CREATE INDEX idx_clc_snapshot ON cross_leader_duplicate_clusters (snapshot_id);

CREATE TABLE IF NOT EXISTS cross_leader_duplicate_files (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    cluster_id  BIGINT UNSIGNED NOT NULL,
    leader      VARCHAR(300)    NOT NULL,
    index_label VARCHAR(300)    NOT NULL,
    file_path   VARCHAR(1500)   NOT NULL,
    size_bytes  BIGINT UNSIGNED NOT NULL,
    mtime       DATE,

    CONSTRAINT fk_clf_cluster FOREIGN KEY (cluster_id)
        REFERENCES cross_leader_duplicate_clusters (id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE INDEX idx_clf_cluster ON cross_leader_duplicate_files (cluster_id);

-- ── One row per quota'd path per day, from Isilon's own SmartQuotas feed ─────
-- Populated by import_isilon_daily.py from /isilon_json/daily-json/YYYY/MM/
-- JIC-daily-YYYYMMDD.json (a daily dump of Isilon's Platform API quota
-- listing — no crawling involved, OneFS tracks this continuously). Size only,
-- no staleness — Diskover stays the source for atime-based stale buckets.
-- `path` reuses the exact same dashboard path convention as directory_stats
-- (`{area}/{name}`) so it can be matched against directory_stats.path/
-- subdirectory_stats.path for merged trend charts.
CREATE TABLE IF NOT EXISTS isilon_quota_stats (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    stat_date       DATE            NOT NULL,
    area_label      VARCHAR(300)    NOT NULL,
    entity_type     ENUM('directory', 'user') NOT NULL,
    path            VARCHAR(1000)   NOT NULL,   -- dashboard-style path, e.g. "JIC-Apricot___Primarydata_Research_Groups/Cristobal-Uauy"
    absolute_path   VARCHAR(1000)   NOT NULL,   -- raw Isilon path, e.g. "/ifs/apricot/JIC/PrimaryData/RESEARCH-GROUPS/Cristobal-Uauy"
    logical_bytes   BIGINT UNSIGNED,
    physical_bytes  BIGINT UNSIGNED,
    inode_count     BIGINT UNSIGNED,

    UNIQUE KEY uq_isilon_date_path (stat_date, path(255))
) ENGINE=InnoDB;

CREATE INDEX idx_isilon_stat_date ON isilon_quota_stats (stat_date);
CREATE INDEX idx_isilon_path      ON isilon_quota_stats (path(255));

CREATE TABLE IF NOT EXISTS area_settings (
    index_label VARCHAR(300) PRIMARY KEY,
    enabled     TINYINT(1) NOT NULL DEFAULT 1,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS full_access_users (
    username   VARCHAR(120) PRIMARY KEY,
    notes      VARCHAR(255),
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS directory_group_rules (
    id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    index_label   VARCHAR(300) NOT NULL,
    path_prefix   VARCHAR(1000) NOT NULL,
    ldap_group_cn VARCHAR(255) NOT NULL,
    notes         VARCHAR(255),
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_dir_group_rule (index_label, path_prefix(255), ldap_group_cn)
) ENGINE=InnoDB;

-- ── File-extension storage action plan (admin-editable at /settings/extensions) ─
-- Seeded once from EXTENSION_ACTION_PLAN_SEED in app.py when first empty;
-- admin edits from then on are the live source classify_extension() reads.
CREATE TABLE IF NOT EXISTS extension_action_plan (
    extension  VARCHAR(255) PRIMARY KEY,
    category   ENUM('red','yellow','green') NOT NULL,
    phrase     VARCHAR(1000),
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ── Convenience view: latest snapshot per directory ───────────────────────────
CREATE OR REPLACE VIEW latest_stats AS
SELECT
    ds.*,
    s.run_date
FROM directory_stats ds
JOIN snapshots s ON s.id = ds.snapshot_id
WHERE s.run_date = (
    SELECT MAX(s2.run_date)
    FROM snapshots s2
    JOIN directory_stats ds2 ON ds2.snapshot_id = s2.id
    WHERE ds2.path = ds.path
);
