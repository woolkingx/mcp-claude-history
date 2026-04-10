#!/usr/bin/env python3
"""
sync_release.py — Zero-config worktree sync driven by .gitignore.

Auto-detects source and target worktrees from CWD. Uses target's .gitignore
as the sole exclude mechanism — no hardcoded rules.

Usage:
    python3 sync_release.py                    # dry run, auto-detect
    python3 sync_release.py --apply            # execute sync
    python3 sync_release.py --target release   # specify target worktree
    python3 sync_release.py --ignore "*.log"   # append to target .gitignore
    python3 sync_release.py -v                 # show filtered files
    python3 sync_release.py --diff             # show content diff
"""

import argparse
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path


def find_bare_repo() -> Path:
    """Find the bare repo root from CWD."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip()).resolve()


def parse_worktrees(bare: Path) -> list[dict]:
    """Parse `git worktree list --porcelain` into structured data."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=bare, capture_output=True, text=True, check=True,
    )
    worktrees = []
    current = {}
    for line in result.stdout.splitlines():
        if not line:
            if current and not current.get("bare"):
                worktrees.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = Path(line.split(" ", 1)[1])
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1].split("/")[-1]
    if current and not current.get("bare"):
        worktrees.append(current)
    return worktrees


def detect_source_target(worktrees: list[dict], target_name: str | None) -> tuple[Path, Path]:
    """Auto-detect source (CWD) and target worktrees."""
    cwd = Path.cwd().resolve()

    # Find source: the worktree that contains CWD
    source = None
    for wt in worktrees:
        try:
            cwd.relative_to(wt["path"])
            source = wt["path"]
            break
        except ValueError:
            continue

    if not source:
        print(f"Error: CWD {cwd} is not inside any worktree", file=sys.stderr)
        sys.exit(1)

    # Find target
    candidates = [wt for wt in worktrees if wt["path"] != source]

    if not candidates:
        print("Error: only one worktree found, nothing to sync to", file=sys.stderr)
        sys.exit(1)

    if target_name:
        for wt in candidates:
            if wt["branch"] == target_name or wt["path"].name == target_name:
                return source, wt["path"]
        print(f"Error: no worktree matching '{target_name}'", file=sys.stderr)
        print(f"Available: {', '.join(wt['branch'] for wt in candidates)}", file=sys.stderr)
        sys.exit(1)

    if len(candidates) == 1:
        return source, candidates[0]["path"]

    # Multiple candidates — look for common names
    for name in ("release", "public", "main"):
        for wt in candidates:
            if wt["branch"] == name or wt["path"].name == name:
                return source, wt["path"]

    print("Error: multiple worktrees found, specify --target", file=sys.stderr)
    for wt in candidates:
        print(f"  {wt['path'].name} ({wt['branch']})", file=sys.stderr)
    sys.exit(1)


def get_tracked_files(cwd: Path) -> list[Path]:
    """Get git-tracked files from a worktree."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return sorted(Path(line) for line in result.stdout.strip().split("\n") if line)


def filter_by_target_gitignore(files: list[Path], target: Path) -> tuple[list[Path], list[Path]]:
    """Filter source files using target's .gitignore. Returns (included, excluded)."""
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        return files, []

    input_text = "\n".join(str(f) for f in files)
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input=input_text, cwd=target,
        capture_output=True, text=True,
    )
    ignored = set(Path(line) for line in result.stdout.strip().split("\n") if line)

    included = [f for f in files if f not in ignored]
    excluded = [f for f in files if f in ignored]
    return included, excluded


def append_to_gitignore(target: Path, patterns: list[str]) -> list[str]:
    """Append patterns to target's .gitignore. Returns newly added patterns."""
    gitignore = target / ".gitignore"
    existing = set()
    if gitignore.exists():
        existing = set(gitignore.read_text().splitlines())

    added = []
    with open(gitignore, "a") as f:
        for pattern in patterns:
            if pattern not in existing:
                f.write(f"\n{pattern}")
                added.append(pattern)
    return added


def compute_actions(
    source: Path, target: Path, public_files: list[Path],
) -> tuple[list[Path], list[Path], list[Path]]:
    """Compare source vs target, return (add, update, delete)."""
    add, update = [], []
    should_exist = set(public_files)

    for rel in public_files:
        if rel == Path(".gitignore"):
            continue
        src = source / rel
        dst = target / rel
        if not dst.exists():
            add.append(rel)
        elif not filecmp.cmp(src, dst, shallow=False):
            update.append(rel)

    target_files = set(get_tracked_files(target))
    target_files.discard(Path(".gitignore"))
    delete = sorted(target_files - should_exist)

    return add, update, delete


def apply_actions(
    source: Path, target: Path,
    add: list[Path], update: list[Path], delete: list[Path],
) -> None:
    """Execute the sync actions."""
    for rel in add + update:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dst)

    for rel in delete:
        dst = target / rel
        if dst.exists():
            dst.unlink()
        parent = dst.parent
        while parent != target and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def print_report(add, update, delete, excluded, verbose=False):
    """Print sync report."""
    if add:
        print(f"\n  ADD ({len(add)}):")
        for f in add:
            print(f"    + {f}")
    if update:
        print(f"\n  UPDATE ({len(update)}):")
        for f in update:
            print(f"    ~ {f}")
    if delete:
        print(f"\n  DELETE ({len(delete)}):")
        for f in delete:
            print(f"    - {f}")
    if verbose and excluded:
        print(f"\n  FILTERED ({len(excluded)}):")
        for f in excluded:
            print(f"    x {f}")

    total = len(add) + len(update) + len(delete)
    if total == 0:
        print("\n  Already in sync.")
    else:
        print(f"\n  Total: +{len(add)} ~{len(update)} -{len(delete)}")


def main():
    parser = argparse.ArgumentParser(
        description="Zero-config worktree sync driven by .gitignore"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Execute sync (default: dry run)"
    )
    parser.add_argument(
        "--target", "-t", type=str, default=None,
        help="Target worktree name or branch (auto-detected if omitted)"
    )
    parser.add_argument(
        "--ignore", type=str, default=None,
        help="Comma-separated patterns to append to target .gitignore"
    )
    parser.add_argument(
        "--diff", action="store_true", help="Show content diff for updates"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show filtered files"
    )
    args = parser.parse_args()

    bare = find_bare_repo()
    worktrees = parse_worktrees(bare)
    source, target = detect_source_target(worktrees, args.target)

    print(f"Source: {source}")
    print(f"Target: {target}")

    # --ignore: append to target .gitignore
    if args.ignore:
        patterns = [p.strip() for p in args.ignore.split(",") if p.strip()]
        added = append_to_gitignore(target, patterns)
        if added:
            print(f"\n  Added to {target.name}/.gitignore: {', '.join(added)}")

    # Get source files, filter by target's .gitignore
    source_files = get_tracked_files(source)
    public_files, excluded = filter_by_target_gitignore(source_files, target)

    add, update, delete = compute_actions(source, target, public_files)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"\nMode: {mode}")

    print_report(add, update, delete, excluded, verbose=args.verbose)

    if args.diff and update:
        print("\n  DIFF SUMMARY:")
        for rel in update:
            result = subprocess.run(
                ["diff", "--brief", str(source / rel), str(target / rel)],
                capture_output=True, text=True,
            )
            if result.stdout.strip():
                print(f"    {rel}")

    if args.apply:
        if len(add) + len(update) + len(delete) == 0:
            return
        apply_actions(source, target, add, update, delete)
        print(f"\n  Done. Applied +{len(add)} ~{len(update)} -{len(delete)}")
    elif len(add) + len(update) + len(delete) > 0:
        print("\n  Run with --apply to execute.")


if __name__ == "__main__":
    main()
