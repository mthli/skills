#!/usr/bin/env python3
"""Manage local git sparse-checkout exclusions (non-cone mode).

Hides specified paths from the working tree without touching .gitignore or any
tracked file. State lives in .git/info/sparse-checkout, which is per-clone and
never pushed.
"""

import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        print(f"command failed: {' '.join(cmd)}", file=sys.stderr)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        sys.exit(result.returncode)
    return result


def git_root() -> Path:
    r = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if r.returncode != 0:
        print("not inside a git repository", file=sys.stderr)
        sys.exit(1)
    return Path(r.stdout.strip())


def patterns_file(root: Path) -> Path:
    return root / ".git" / "info" / "sparse-checkout"


def read_patterns(root: Path) -> list[str]:
    f = patterns_file(root)
    if not f.exists():
        return []
    return [ln.rstrip() for ln in f.read_text().splitlines() if ln.strip()]


def is_enabled() -> bool:
    r = run(["git", "config", "--get", "core.sparseCheckout"], check=False)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def normalize(path_str: str, root: Path) -> str:
    """Normalize a path for use in a sparse-checkout pattern.

    Honor an explicit trailing slash from the user (their hint that this is
    a directory, useful for paths that don't yet exist on disk or in HEAD).
    Otherwise auto-detect from the working tree and HEAD; fall back to a
    file-style pattern only if there's no evidence either way.
    """
    p = path_str.strip()
    if p.startswith("./"):
        p = p[2:]
    if not p:
        raise ValueError("empty path")

    # User-supplied trailing slash wins — they know better than we do.
    if p.endswith("/"):
        return p

    fs = root / p
    if fs.is_dir():
        return p + "/"

    r = run(["git", "ls-tree", "-d", "HEAD", p], check=False)
    if r.returncode == 0 and r.stdout.strip():
        return p + "/"

    return p


def ensure_init(root: Path) -> None:
    """Enable sparse-checkout config without running `set` (which rewrites
    patterns in surprising ways — e.g. appending !/*/ to a /* base, hiding all
    subdirectories). We manage .git/info/sparse-checkout directly and call
    `reapply` to sync the working tree.
    """
    if is_enabled():
        return
    print("→ initializing sparse-checkout (non-cone mode)")
    run(["git", "config", "core.sparseCheckout", "true"])
    run(["git", "config", "core.sparseCheckoutCone", "false"])
    f = patterns_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        # Base pattern: include everything recursively, then layer negations.
        f.write_text("/*\n")


def apply_patterns(patterns: list[str]) -> None:
    """Write patterns and reapply to the working tree, with rollback on failure.

    Bypasses `git sparse-checkout set`, which auto-injects extra patterns
    (notably !/*/ alongside /*) that invert the include/exclude semantics.

    Reapply can fail when the changed exclusions would discard uncommitted
    work. In that case the patterns file would otherwise be left ahead of
    reality — `list` would lie about what's hidden, and a subsequent checkout
    could silently drop those uncommitted changes. So: snapshot first, restore
    on failure.
    """
    root = git_root()
    f = patterns_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    previous = f.read_text() if f.exists() else None
    f.write_text("\n".join(patterns) + "\n")
    proc = subprocess.run(
        ["git", "sparse-checkout", "reapply"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        if previous is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(previous)
        print("git sparse-checkout reapply failed; rolled back patterns file:",
              file=sys.stderr)
        if proc.stderr:
            print(proc.stderr.rstrip(), file=sys.stderr)
        sys.exit(proc.returncode)


def print_state(root: Path) -> None:
    if not is_enabled():
        print("sparse-checkout: disabled")
        return
    pats = read_patterns(root)
    excluded = [p[1:] for p in pats if p.startswith("!")]
    if excluded:
        print("currently hidden:")
        for e in excluded:
            print(f"  - {e}")
    else:
        print("currently hidden: (nothing)")


def uncommitted_under(path: str) -> str:
    """Return porcelain status for tracked-file changes under path, or empty
    if clean. Untracked files are deliberately ignored — they're documented
    to remain on disk after hiding, which is fine. The danger we're guarding
    against is staged/unstaged modifications to tracked files, which `git
    sparse-checkout reapply` will warn about and silently leave on disk,
    leaving the patterns file ahead of reality.
    """
    r = run(
        ["git", "status", "--porcelain", "--untracked-files=no", "--", path],
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def cmd_hide(raw_paths: list[str]) -> None:
    root = git_root()
    normalized = [normalize(p, root) for p in raw_paths]

    # Pre-flight: refuse to hide paths with uncommitted tracked-file changes.
    # If we proceeded, reapply would warn-but-succeed, the patterns file would
    # commit to hiding the path, but the dirty files would remain on disk —
    # vulnerable to silent loss on the next checkout-like operation.
    dirty = [(p, uncommitted_under(p)) for p in normalized]
    dirty = [(p, s) for p, s in dirty if s]
    if dirty:
        print("refusing to hide — uncommitted changes under target paths:",
              file=sys.stderr)
        for p, status in dirty:
            print(f"  {p}", file=sys.stderr)
            for line in status.splitlines():
                print(f"    {line}", file=sys.stderr)
        print("commit or stash these first, then retry.", file=sys.stderr)
        sys.exit(1)

    ensure_init(root)
    pats = read_patterns(root)
    if "/*" not in pats:
        pats.insert(0, "/*")
    added, already = [], []
    for p in normalized:
        neg = "!" + p
        if neg in pats:
            already.append(p)
        else:
            pats.append(neg)
            added.append(p)
    if added:
        apply_patterns(pats)
        print(f"✓ hidden: {', '.join(added)}")
    for s in already:
        print(f"  (already hidden: {s})")
    print_state(root)


def cmd_restore(raw_paths: list[str]) -> None:
    root = git_root()
    if not is_enabled():
        print("sparse-checkout not enabled; nothing to restore")
        return
    pats = read_patterns(root)
    restored, missing = [], []
    for raw in raw_paths:
        p = normalize(raw, root)
        neg = "!" + p
        if neg in pats:
            pats.remove(neg)
            restored.append(p)
        else:
            missing.append(p)
    if restored:
        remaining_neg = [p for p in pats if p.startswith("!")]
        if not remaining_neg:
            # No exclusions left; clean up by disabling entirely
            run(["git", "sparse-checkout", "disable"])
            print(f"✓ restored: {', '.join(restored)}")
            print("→ no exclusions left, sparse-checkout disabled")
            return
        apply_patterns(pats)
        print(f"✓ restored: {', '.join(restored)}")
    for m in missing:
        print(f"  (not hidden: {m})")
    print_state(root)


def cmd_list() -> None:
    print_state(git_root())


def cmd_status() -> None:
    root = git_root()
    print(f"repo: {root}")
    print_state(root)


def cmd_disable() -> None:
    git_root()  # just to verify we're in a repo
    if not is_enabled():
        print("sparse-checkout already disabled")
        return
    run(["git", "sparse-checkout", "disable"])
    print("✓ sparse-checkout disabled; all files restored")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sparse_checkout",
        description="manage local git sparse-checkout exclusions",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_hide = sub.add_parser("hide", help="hide one or more paths locally")
    p_hide.add_argument("paths", nargs="+")

    p_restore = sub.add_parser(
        "restore", help="restore one or more hidden paths")
    p_restore.add_argument("paths", nargs="+")

    sub.add_parser("list", help="list currently hidden paths")
    sub.add_parser("status", help="show repo and sparse-checkout state")
    sub.add_parser("disable", help="disable sparse-checkout entirely")

    args = parser.parse_args()
    {
        "hide": lambda: cmd_hide(args.paths),
        "restore": lambda: cmd_restore(args.paths),
        "list": cmd_list,
        "status": cmd_status,
        "disable": cmd_disable,
    }[args.cmd]()


if __name__ == "__main__":
    main()
