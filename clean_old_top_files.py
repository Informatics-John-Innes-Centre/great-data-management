#!/usr/bin/env python3
"""
clean_old_top_files.py — remove old top_files.csv crawl output, locally and
(optionally) from git history.

top_files.csv is the one crawl output that's intentionally NOT kept as
long-term history (see dir_top_files' "current + previous month only"
retention in the database) — it can run 100+ MB per month, and has been
committed to git repeatedly, bloating the repo (.git was 390 MB at last
check). This mirrors that same 2-month retention policy for the CSV files
on disk and in git, since the database is the real persistent store once
import_csv.py has run — the CSV itself is disposable working output.

Usage
-----
    # Preview what local files would be removed (default — nothing deleted)
    python clean_old_top_files.py

    # Actually delete old top_files.csv from disk (current + previous month kept)
    python clean_old_top_files.py --yes

    # Also purge every historical top_files.csv from git history.
    # Operates on a FRESH CLONE in a scratch directory (filter-repo's own
    # recommended practice) — your working directory and any in-progress
    # crawl are completely untouched. Stops before pushing; you review the
    # result and push it yourself when ready (see printed instructions).
    python clean_old_top_files.py --rewrite-history
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile


def find_run_months(runs_root="runs"):
    """Return sorted [(year, month, folder_path)] for every runs/YYYY/MM/ folder."""
    months = []
    if not os.path.isdir(runs_root):
        return months
    for year_name in os.listdir(runs_root):
        year_path = os.path.join(runs_root, year_name)
        if not (year_name.isdigit() and os.path.isdir(year_path)):
            continue
        for month_name in os.listdir(year_path):
            month_path = os.path.join(year_path, month_name)
            if not (month_name.isdigit() and os.path.isdir(month_path)):
                continue
            months.append((int(year_name), int(month_name), month_path))
    months.sort()
    return months


def months_to_keep(all_months, keep):
    """The most recent `keep` (year, month) tuples — same idea as
    import_csv.py's prune_old_top_files(), just applied to files on disk."""
    return {(y, m) for y, m, _ in all_months[-keep:]}


def clean_local(runs_root, keep, do_delete):
    all_months = find_run_months(runs_root)
    keep_set = months_to_keep(all_months, keep)

    to_delete = []
    for year, month, folder in all_months:
        if (year, month) in keep_set:
            continue
        csv_path = os.path.join(folder, "top_files.csv")
        if os.path.isfile(csv_path):
            to_delete.append(csv_path)

    if not to_delete:
        print("No old top_files.csv files to clean up.")
        return

    print(f"Keeping: {sorted(keep_set)}")
    print(f"{'Deleting' if do_delete else 'Would delete'} {len(to_delete)} file(s):")
    total_bytes = 0
    for path in to_delete:
        size = os.path.getsize(path)
        total_bytes += size
        print(f"  {path}  ({size / 1e6:.1f} MB)")
        if do_delete:
            os.remove(path)
    verb = "Freed" if do_delete else "Would free"
    print(f"{verb} {total_bytes / 1e9:.2f} GB total.")
    if not do_delete:
        print("(dry run — pass --yes to actually delete)")


def run(cmd, **kwargs):
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


def du(path):
    return subprocess.run(["du", "-sh", path], capture_output=True, text=True).stdout.split()[0]


def ensure_gitignore_entry(repo_dir):
    entry = "runs/*/*/top_files.csv"
    gitignore_path = os.path.join(repo_dir, ".gitignore")
    existing = open(gitignore_path).read() if os.path.isfile(gitignore_path) else ""
    if entry in existing:
        return False
    with open(gitignore_path, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(
            "\n# top_files.csv is intentionally not kept long-term (see dir_top_files\n"
            f"# retention in the DB) and can run 100+ MB/month — never commit it.\n{entry}\n"
        )
    run(["git", "add", ".gitignore"], cwd=repo_dir)
    run(["git", "commit", "-m", "Ignore top_files.csv going forward (large, not kept long-term)"], cwd=repo_dir)
    return True


def rewrite_git_history(scratch_dir):
    if shutil.which("git-filter-repo") is None:
        print(
            "ERROR: git-filter-repo isn't installed. Install it first:\n"
            "  pip install git-filter-repo"
        )
        sys.exit(1)

    remote_url = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
    ).stdout.strip()

    print(
        "\nThis will clone a FRESH copy of the repo and rewrite ITS history to remove\n"
        "every top_files.csv ever committed — your working directory is not touched.\n"
        "It does NOT push anything; you review the result and push it yourself after."
    )
    confirm = input("Type REWRITE HISTORY to continue: ")
    if confirm.strip() != "REWRITE HISTORY":
        print("Aborted — confirmation text didn't match.")
        sys.exit(1)

    os.makedirs(scratch_dir, exist_ok=True)
    clone_path = os.path.join(scratch_dir, "diskover-dashboard-history-rewrite")
    if os.path.exists(clone_path):
        print(f"ERROR: {clone_path} already exists — remove it first or pass a different --scratch-dir.")
        sys.exit(1)

    print(f"\nCloning fresh copy to {clone_path} …")
    run(["git", "clone", remote_url, clone_path])
    before = du(os.path.join(clone_path, ".git"))

    print("\nRewriting history to strip runs/*/*/top_files.csv …")
    run(["git", "filter-repo", "--path-glob", "runs/*/*/top_files.csv", "--invert-paths"], cwd=clone_path)

    # filter-repo removes the 'origin' remote as a safety measure, so a
    # rewritten history can't be accidentally pushed without a deliberate
    # step — re-add it now that we're doing that deliberately.
    run(["git", "remote", "add", "origin", remote_url], cwd=clone_path)

    print("\nRunning garbage collection to actually reclaim disk space…")
    run(["git", "gc", "--prune=now", "--aggressive"], cwd=clone_path)
    after = du(os.path.join(clone_path, ".git"))

    if ensure_gitignore_entry(clone_path):
        print("Added .gitignore entry (runs/*/*/top_files.csv) and committed it in the rewritten clone.")

    print("\n" + "=" * 70)
    print(f"Done. .git size in the rewritten clone: {before} -> {after}")
    print(f"Rewritten clone: {clone_path}")
    print("\nReview it before pushing anything (git log, spot-check a few commits with git show).")
    print("When you're satisfied, push it yourself:")
    print(f"\n    cd {clone_path}")
    print("    git push --force origin main")
    print("\nAfter that, your ORIGINAL working directory (and the VM's clone) have")
    print("diverged history and can't `git pull` cleanly anymore. Re-sync each with:")
    print("\n    git fetch origin")
    print("    git reset --hard origin/main")
    print("\n(this discards anything uncommitted there — for the VM that should only")
    print(" ever be this crawl's own runs/ output, which is fine once it's imported).")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runs-root", default="runs", help="Root of the runs/ folder (default: runs)")
    parser.add_argument(
        "--keep", type=int, default=2,
        help="How many most-recent months to keep locally (default: 2, matching dir_top_files' DB retention)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually delete old local top_files.csv (default: dry-run, prints what would be deleted)",
    )
    parser.add_argument(
        "--rewrite-history", action="store_true",
        help="Also purge every top_files.csv from git history, via a fresh clone. "
             "Requires typed confirmation. Stops before pushing.",
    )
    parser.add_argument(
        "--scratch-dir", default=os.path.join(tempfile.gettempdir(), "diskover-history-rewrite"),
        help="Where to put the fresh clone for --rewrite-history (default: a system temp folder)",
    )
    args = parser.parse_args()

    print("=== Local cleanup ===")
    clean_local(args.runs_root, args.keep, args.yes)

    if args.rewrite_history:
        print("\n=== Git history rewrite ===")
        rewrite_git_history(args.scratch_dir)
    else:
        print("\n(Pass --rewrite-history to also purge top_files.csv from git history.)")


if __name__ == "__main__":
    main()
