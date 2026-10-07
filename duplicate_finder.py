#!/usr/bin/env python3
"""
Shared "possible duplicate files" clustering logic.

Used by both app.py (live, per-leader detail view on /duplicate-files) and
import_csv.py (precomputed, cross-leader leaderboard stored in
leader_duplicate_summary — see that table in schema.sql). Pure Python, no
Flask/DB dependency, so a web app and a standalone CLI import script can
both import it directly without pulling in each other's machinery.
"""


def cluster_duplicate_files(rows, tolerance_pct):
    """
    rows: iterable of dicts, each with at least "file_path", "extension",
    "size_bytes". Groups by (basename, extension), then within each group
    clusters files whose size is within tolerance_pct of the largest
    not-yet-clustered file with that name.

    No content hash is available from the crawl, so this is a heuristic:
    same filename + a similar size is a plausible signal of duplication,
    not a guarantee — present results as "possible duplicates worth
    checking", never as a safe-to-auto-delete list.

    Returns clusters sorted by reclaimable space (total size minus the one
    largest copy) descending. Each cluster: {basename, extension, files,
    count, total_size_bytes, reclaimable_bytes} — files is the original row
    dicts passed in (grouped, not copied), so callers can attach their own
    extra display fields to each row afterward.
    """
    groups = {}
    for r in rows:
        basename = r["file_path"].rsplit("/", 1)[-1]
        groups.setdefault((basename, r["extension"]), []).append(r)

    tol = tolerance_pct / 100.0
    clusters = []
    for (basename, extension), files in groups.items():
        if len(files) < 2:
            continue
        files = sorted(files, key=lambda f: f["size_bytes"] or 0, reverse=True)
        used = [False] * len(files)
        for i, f in enumerate(files):
            if used[i]:
                continue
            cluster = [f]
            used[i] = True
            f_size = f["size_bytes"] or 0
            for j in range(i + 1, len(files)):
                if used[j]:
                    continue
                g_size = files[j]["size_bytes"] or 0
                if abs(f_size - g_size) <= f_size * tol:
                    cluster.append(files[j])
                    used[j] = True
            if len(cluster) < 2:
                continue
            total_size = sum(c["size_bytes"] or 0 for c in cluster)
            max_size = max(c["size_bytes"] or 0 for c in cluster)
            clusters.append({
                "basename": basename,
                "extension": extension,
                "files": cluster,
                "count": len(cluster),
                "total_size_bytes": total_size,
                "reclaimable_bytes": total_size - max_size,
            })

    clusters.sort(key=lambda c: -c["reclaimable_bytes"])
    return clusters
