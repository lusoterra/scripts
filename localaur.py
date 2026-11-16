#!/usr/bin/env python3
"""Minimal AUR overlay updater.

Scans a local collection of PKGBUILD directories, detects installed packages
that are outdated (normal version bump or advanced git source), rebuilds them
with makepkg and installs the rebuilt packages, then prints a summary.

Never overwrites local PKGBUILDs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

META_NAME = ".aur-overlay.json"
DEFAULT_ROOT = Path(os.environ.get("AUR_OVERLAY_ROOT", str(Path.home() / "aur")))

# --------------------------------------------------------------------------- #
# subprocess helpers
# --------------------------------------------------------------------------- #


def run(cmd, cwd=None, timeout=120, check=False, capture=True):
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr}")
    return r


def which(name):
    for p in os.environ.get("PATH", "").split(os.pathsep):
        c = Path(p) / name
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


# --------------------------------------------------------------------------- #
# SRCINFO parsing
# --------------------------------------------------------------------------- #


@dataclass
class SrcInfo:
    pkgbase: str
    pkgnames: list = field(default_factory=list)
    pkgver: str = ""
    pkgrel: str = "1"
    epoch: str = "0"
    sources: list = field(default_factory=list)
    arch: list = field(default_factory=list)
    raw: str = ""


def parse_srcinfo(text: str) -> Optional[SrcInfo]:
    info = SrcInfo(pkgbase="")
    cur = "base"
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, val = s.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "pkgbase":
            info.pkgbase = val
            cur = "base"
            continue
        if key == "pkgname":
            info.pkgnames.append(val)
            cur = "pkgname"
            continue
        if cur == "base":
            if key == "pkgver":
                info.pkgver = val
            elif key == "pkgrel":
                info.pkgrel = val
            elif key == "epoch":
                info.epoch = val
            elif key == "source":
                info.sources.append(val)
            elif key == "arch":
                info.arch.append(val)
    if not info.pkgbase:
        return None
    if not info.pkgnames:
        info.pkgnames = [info.pkgbase]
    if not info.pkgver:
        return None
    return info


def get_srcinfo(pkgdir: Path) -> Optional[SrcInfo]:
    r = run(["makepkg", "--printsrcinfo"], cwd=str(pkgdir), timeout=120)
    if r is None or r.returncode != 0:
        return None
    info = parse_srcinfo(r.stdout)
    if info is not None:
        info.raw = r.stdout
    return info


# --------------------------------------------------------------------------- #
# git source helpers
# --------------------------------------------------------------------------- #


def split_frag(src: str):
    # returns (display_name, url, fragment or None)
    name = None
    if "::" in src:
        name, rest = src.split("::", 1)
    else:
        rest = src
    url = rest
    frag = None
    if "#" in url:
        url, frag = url.split("#", 1)
    url = url.split("?")[0]
    if url.startswith("git+"):
        url = url[4:]
    if name is None:
        # makepkg get_filename logic for git
        base = url.rstrip("/").split("/")[-1]
        if base.endswith(".git"):
            base = base[:-4]
        name = base
    return name, url, frag


def is_git_source(src: str) -> bool:
    if "::" in src:
        src = src.split("::", 1)[1]
    url = src.split("#")[0].split("?")[0]
    if url.startswith("git+"):
        return True
    if url.startswith("git://"):
        return True
    proto = url.split("://")[0] if "://" in url else ""
    if proto in ("git", "ssh") and url.endswith(".git"):
        return True
    return False


def get_filename_makepkg(src: str) -> str:
    name, url, frag = split_frag(src)
    return name


def git_cmd(args, cwd=None, timeout=60):
    r = run(["git"] + args, cwd=cwd, timeout=timeout)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip()


def resolve_upstream_commit(url: str, frag: Optional[str]) -> Optional[str]:
    """Resolve the current upstream commit for a git source via ls-remote."""
    if frag:
        if "=" in frag:
            key, val = frag.split("=", 1)
        else:
            key, val = frag, None
        if key == "commit":
            return val
        if key == "branch":
            ref = f"refs/heads/{val}"
        elif key == "tag":
            # prefer peeled
            out = git_cmd(["ls-remote", url, f"refs/tags/{val}", f"refs/tags/{val}^{{}}"], timeout=60)
            if out:
                lines = out.splitlines()
                peeled = [l for l in lines if l.endswith("^{}")]
                chosen = peeled[-1] if peeled else (lines[-1] if lines else None)
                if chosen:
                    return chosen.split()[0]
            return None
        else:
            return None
    else:
        ref = "HEAD"

    if frag and "=" in frag and frag.split("=", 1)[0] == "branch":
        pass
    # default branch / specified ref
    if not frag or ("=" not in frag):
        # HEAD
        out = git_cmd(["ls-remote", url, "HEAD"], timeout=60)
        if out:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 1:
                    return parts[0]
        return None
    out = git_cmd(["ls-remote", url, ref], timeout=60)
    if out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 1:
                return parts[0]
    return None


def capture_mirror_commit(pkgdir: Path, src_entry: str) -> Optional[str]:
    name, url, frag = split_frag(src_entry)
    mirror = pkgdir / name
    if not mirror.is_dir():
        # maybe src working copy
        wc = pkgdir / "src" / name
        if wc.is_dir():
            c = git_cmd(["-C", str(wc), "rev-parse", "HEAD"], timeout=30)
            if c:
                return c
        return None
    # bare mirror
    if frag:
        if "=" in frag:
            key, val = frag.split("=", 1)
        else:
            key, val = frag, None
        if key == "commit":
            return val
        if key == "branch":
            c = git_cmd(["-C", str(mirror), "rev-parse", val], timeout=30)
            if c:
                return c
            # try refs/heads
            c = git_cmd(["-C", str(mirror), "rev-parse", f"refs/heads/{val}"], timeout=30)
            if c:
                return c
        if key == "tag":
            c = git_cmd(["-C", str(mirror), "rev-parse", f"{val}^{{}}"], timeout=30)
            if c:
                return c
            c = git_cmd(["-C", str(mirror), "rev-parse", val], timeout=30)
            if c:
                return c
    # default branch tip
    c = git_cmd(["-C", str(mirror), "rev-parse", "HEAD"], timeout=30)
    if c:
        return c
    # symbolic ref
    sym = git_cmd(["-C", str(mirror), "symbolic-ref", "refs/remotes/origin/HEAD"], timeout=30)
    if sym:
        c = git_cmd(["-C", str(mirror), "rev-parse", sym], timeout=30)
        if c:
            return c
    return None


# --------------------------------------------------------------------------- #
# pacman helpers
# --------------------------------------------------------------------------- #


def pacman_version(pkg: str) -> Optional[str]:
    r = run(["pacman", "-Q", pkg], timeout=30)
    if r is None or r.returncode != 0:
        return None
    parts = r.stdout.strip().split()
    if len(parts) < 2:
        return None
    # Only treat as installed if pacman reports the exact package name.
    # A "-git" (or other) variant that merely provides `pkg` must not count.
    if parts[0] != pkg:
        return None
    return parts[1]


def vercmp(a: str, b: str) -> int:
    r = run(["vercmp", a, b], timeout=30)
    if r is None:
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# metadata sidecar
# --------------------------------------------------------------------------- #


def load_meta(pkgdir: Path) -> dict:
    p = pkgdir / META_NAME
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_meta(pkgdir: Path, meta: dict):
    p = pkgdir / META_NAME
    p.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def capture_meta(pkgdir: Path, info: SrcInfo, verbose=False) -> dict:
    meta = load_meta(pkgdir)
    meta["pkgbase"] = info.pkgbase
    meta["pkgver_declared"] = f"{info.epoch}:{info.pkgver}-{info.pkgrel}" if info.epoch not in ("0", "") else f"{info.pkgver}-{info.pkgrel}"
    meta["epoch"] = info.epoch
    meta["pkgver"] = info.pkgver
    meta["pkgrel"] = info.pkgrel
    git_sources = []
    for s in info.sources:
        if is_git_source(s):
            name, url, frag = split_frag(s)
            commit = capture_mirror_commit(pkgdir, s)
            git_sources.append({
                "name": name,
                "url": url,
                "fragment": frag,
                "commit": commit,
            })
    meta["git_sources"] = git_sources
    # pkgbuild repo commit
    if (pkgdir / ".git").is_dir():
        c = git_cmd(["-C", str(pkgdir), "rev-parse", "HEAD"], timeout=30)
        if c:
            meta["pkgbuild_commit"] = c
    meta["built_at"] = None
    import datetime
    meta["built_at"] = datetime.datetime.now().isoformat()
    save_meta(pkgdir, meta)
    if verbose:
        print(f"  recorded metadata: {pkgdir / META_NAME}")
    return meta


# --------------------------------------------------------------------------- #
# local PKGBUILD git repo handling
# --------------------------------------------------------------------------- #


def pkgbuild_repo_status(pkgdir: Path, fetch=False, verbose=False):
    """Return list of status strings for the PKGBUILD git repo."""
    out = []
    if not (pkgdir / ".git").is_dir():
        return out
    if fetch:
        r = run(["git", "-C", str(pkgdir), "fetch", "--all", "-p"], timeout=120)
        if r is None or r.returncode != 0:
            out.append("pkgbuild-fetch-failed")
        elif verbose:
            print(f"  fetched PKGBUILD repo in {pkgdir}")
    # dirty
    # only tracked modifications count as "local modifications"
    r1 = run(["git", "-C", str(pkgdir), "diff", "--quiet"], timeout=30)
    r2 = run(["git", "-C", str(pkgdir), "diff", "--cached", "--quiet"], timeout=30)
    if (r1 is not None and r1.returncode != 0) or (r2 is not None and r2.returncode != 0):
        out.append("pkgbuild-dirty")
    # upstream tracking
    head = git_cmd(["-C", str(pkgdir), "rev-parse", "HEAD"], timeout=30)
    upstream = git_cmd(["-C", str(pkgdir), "rev-parse", "@{u}"], timeout=30)
    if upstream and head and head != upstream:
        # determine ahead/behind
        r = run(["git", "-C", str(pkgdir), "rev-list", "--left-right", "--count", "HEAD...@{u}"], timeout=30)
        if r is not None and r.returncode == 0:
            parts = r.stdout.strip().split()
            if len(parts) == 2:
                ahead, behind = parts
                if int(behind) > 0:
                    out.append(f"pkgbuild-behind-upstream ({behind})")
                if int(ahead) > 0:
                    out.append(f"pkgbuild-ahead ({ahead})")
        else:
            out.append("pkgbuild-differs-from-upstream")
    return out


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def discover(root: Path) -> list[Path]:
    pkgs = []
    seen = set()
    if (root / "PKGBUILD").is_file():
        pkgs.append(root)
        seen.add(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # prune .git? keep walking but skip hidden? We want package dirs; allow nested.
        # avoid huge
        if "PKGBUILD" in filenames:
            p = Path(dirpath)
            if p not in seen:
                pkgs.append(p)
                seen.add(p)
        # don't descend into VCS or build dirs
        for d in (".git", "src", "pkg"):
            if d in dirnames:
                dirnames.remove(d)
    # dedup preserve
    uniq = []
    seen2 = set()
    for p in pkgs:
        if p in seen2:
            continue
        seen2.add(p)
        uniq.append(p)
    return sorted(uniq)


# --------------------------------------------------------------------------- #
# main processing
# --------------------------------------------------------------------------- #


@dataclass
class Result:
    pkgdir: Path
    pkgbase: str
    pkgnames: list
    installed: list  # installed pkg names
    status: str  # up-to-date, rebuild, unknown, skipped, failed
    reason: str = ""
    rebuilt: bool = False
    build_ok: bool = None
    pkgbuild_repo: list = field(default_factory=list)


def process_package(pkgdir: Path, fetch: bool, dry_run: bool, verbose: bool, install: bool = False) -> Result:
    res = Result(pkgdir=pkgdir, pkgbase="", pkgnames=[], installed=[], status="skipped")
    info = get_srcinfo(pkgdir)
    if info is None:
        res.status = "error"
        res.reason = "could not parse PKGBUILD"
        return res
    res.pkgbase = info.pkgbase
    res.pkgnames = info.pkgnames

    # installed split packages
    installed_names = []
    installed_versions = {}
    for pn in info.pkgnames:
        v = pacman_version(pn)
        if v is not None:
            installed_names.append(pn)
            installed_versions[pn] = v
    res.installed = installed_names
    if not installed_names:
        res.status = "not-installed"
        res.reason = "no installed split packages"
        # still report pkgbuild repo status if fetch
        res.pkgbuild_repo = pkgbuild_repo_status(pkgdir, fetch=fetch, verbose=verbose)
        return res

    is_git = any(is_git_source(s) for s in info.sources)

    local_full = f"{info.epoch}:{info.pkgver}-{info.pkgrel}" if info.epoch not in ("", "0") else f"{info.pkgver}-{info.pkgrel}"
    # Actually always include epoch for vercmp safety:
    local_full = f"{info.epoch}:{info.pkgver}-{info.pkgrel}"

    res.pkgbuild_repo = pkgbuild_repo_status(pkgdir, fetch=fetch, verbose=verbose)

    # ---- git package handling ----
    if is_git:
        meta = load_meta(pkgdir)
        recorded_sources = {gs.get("url"): gs for gs in (meta.get("git_sources") or [])}
        # determine if we have recorded commits for all git sources
        all_recorded = bool(meta.get("git_sources")) and all(
            (gs.get("commit") is not None) for gs in meta.get("git_sources", [])
        ) and len(meta.get("git_sources", [])) == sum(1 for s in info.sources if is_git_source(s))

        if not all_recorded:
            res.status = "unknown"
            res.reason = "no recorded git commit metadata"
            return res

        # compare upstream commits
        needs_rebuild = False
        reasons = []
        for s in info.sources:
            if not is_git_source(s):
                continue
            name, url, frag = split_frag(s)
            upstream = resolve_upstream_commit(url, frag)
            # find recorded
            rec = None
            for gs in meta.get("git_sources", []):
                if gs.get("url") == url:
                    rec = gs.get("commit")
                    break
            if upstream is None:
                # network failure
                res.status = "unknown"
                res.reason = f"upstream check failed for {name}"
                return res
            if rec is None or upstream != rec:
                needs_rebuild = True
                reasons.append(f"{name}: {rec} -> {upstream}")

        # pkgbuild repo changed?
        if meta.get("pkgbuild_commit") and (pkgdir / ".git").is_dir():
            cur_pb = git_cmd(["-C", str(pkgdir), "rev-parse", "HEAD"], timeout=30)
            if cur_pb and cur_pb != meta.get("pkgbuild_commit"):
                needs_rebuild = True
                reasons.append("PKGBUILD repo changed")

        # also normal version bump?
        # For git packages, only if local declared version newer than all installed
        version_newer = False
        for pn, iv in installed_versions.items():
            if vercmp(local_full, iv) > 0:
                version_newer = True
        if version_newer:
            needs_rebuild = True
            reasons.append("declared version newer")

        if not needs_rebuild:
            res.status = "up-to-date"
            res.reason = "git sources current"
            return res

        res.status = "rebuild"
        res.reason = "; ".join(reasons) if reasons else "outdated"
        # rebuild
        if dry_run:
            res.rebuilt = False
            res.build_ok = None
            return res
        ok = do_build(pkgdir, info, verbose, install=install)
        res.rebuilt = True
        res.build_ok = ok
        if ok:
            capture_meta(pkgdir, info, verbose=verbose)
        return res

    # ---- normal package handling ----
    # rebuild if local version newer than any installed version
    needs = False
    reasons = []
    for pn, iv in installed_versions.items():
        c = vercmp(local_full, iv)
        if c > 0:
            needs = True
            reasons.append(f"{pn}: {iv} -> local {local_full}")
        elif c < 0:
            # installed newer (shouldn't happen); note
            pass
    if not needs:
        res.status = "up-to-date"
        res.reason = "version current"
        return res

    res.status = "rebuild"
    res.reason = "; ".join(reasons)
    if dry_run:
        return res
    ok = do_build(pkgdir, info, verbose, install=install)
    res.rebuilt = True
    res.build_ok = ok
    if ok:
        capture_meta(pkgdir, info, verbose=verbose)
    return res


def do_build(pkgdir: Path, info: SrcInfo, verbose: bool, install: bool = False) -> bool:
    print(f"  building {info.pkgbase} in {pkgdir} ...")
    log_path = pkgdir / "makepkg-build.log"
    cmd = ["makepkg", "-f"]
    if install:
        cmd += ["-i", "--noconfirm"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(pkgdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        print("  BUILD TIMEOUT")
        return False
    except Exception as e:
        print(f"  BUILD ERROR: {e}")
        return False
    output = proc.stdout or ""
    log_path.write_text(output)
    if output:
        sys.stdout.write(output)
        sys.stdout.flush()
    if proc.returncode != 0:
        print(f"  BUILD FAILED (rc={proc.returncode}); see {log_path}")
        lines = output.splitlines()
        for ln in lines[-20:]:
            print("    " + ln)
        return False
    print(f"  build OK; log {log_path}")
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def cmd_record(args):
    pkgdir = Path(args.record).expanduser().resolve()
    info = get_srcinfo(pkgdir)
    if info is None:
        print(f"could not parse {pkgdir}")
        return 1
    meta = capture_meta(pkgdir, info, verbose=True)
    print(json.dumps(meta, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Minimal AUR overlay updater")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="root containing package dirs")
    ap.add_argument("--dry-run", action="store_true", help="show what would be rebuilt")
    ap.add_argument("--verbose", action="store_true", help="detailed output")
    ap.add_argument("--fetch", action="store_true", help="fetch PKGBUILD git repos")
    ap.add_argument("--no-install", action="store_true", help="build only, do not install rebuilt packages")
    ap.add_argument("--record", metavar="PKGDIR", help="capture git commit metadata for a package dir")
    args = ap.parse_args(argv)

    if args.record:
        return cmd_record(args)

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"root not found: {root}")
        return 2

    pkgs = discover(root)
    print(f"root: {root}")
    print(f"discovered {len(pkgs)} package dirs")

    results = []
    for pkgdir in pkgs:
        if args.verbose:
            print(f"\n== {pkgdir} ==")
        r = process_package(pkgdir, fetch=args.fetch, dry_run=args.dry_run, verbose=args.verbose, install=not args.no_install)
        results.append(r)
        # concise line
        installed_str = ",".join(r.installed) if r.installed else "-"
        extra = (" [" + ", ".join(r.pkgbuild_repo) + "]") if r.pkgbuild_repo else ""
        print(f"{r.pkgbase or pkgdir.name}: [{installed_str}] {r.status}" + (f" ({r.reason})" if r.reason else "") + extra)

    # summary
    rebuilt = [r for r in results if r.rebuilt and r.build_ok]
    failed = [r for r in results if r.rebuilt and r.build_ok is False]
    unknown = [r for r in results if r.status == "unknown"]
    rebuild_pending = [r for r in results if r.status == "rebuild" and not r.rebuilt]
    print("\n--- summary ---")
    print(f"packages: {len(results)}")
    print(f"rebuilt: {len(rebuilt)}")
    print(f"failed: {len(failed)}")
    print(f"unknown git status: {len(unknown)}")
    print(f"pending (dry-run): {len(rebuild_pending)}")
    if unknown:
        print("unknown:")
        for r in unknown:
            print(f"  {r.pkgbase}: {r.reason}")
    if failed:
        print("failed:")
        for r in failed:
            print(f"  {r.pkgbase}: {r.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
