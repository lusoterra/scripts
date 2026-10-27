#!/usr/bin/env python3
"""
mirroropus.py

Mirrors a music library (master) to a lightweight Opus copy,
keeping the folder structure.

  - Lossy (MP3, Opus, Ogg Vorbis, AAC, WMA lossy, Musepack, ...) -> copied as-is
  - Lossless (FLAC, WAV, AIFF, ALAC, APE, WV, TTA, TAK, SHN, WMA lossless,
    Ogg-FLAC, DSD (.dsf/.dff), CAF/AIFC lossless, ...)           -> converted to .opus
  - Playlists (.cue, .m3u, .m3u8)                                -> copied and rewritten
    to point at the .opus files (e.g. FILE "track.flac" becomes FILE "track.opus"),
    keeping the original encoding and line endings
  - Everything else                                              -> copied as-is:
    accurip (.accurip), .log, .nfo, .txt, .lrc, covers (.jpg/.png),
    scans and booklets (e.g. [album]/scans/, .7z/.zip/.rar/.pdf with scans),
    and any other file or subfolder. The structure is kept identical to the source.

Encoder: opusenc (opus-tools). Copies tags and embedded covers from FLAC.
Formats opusenc cannot read go first through ffmpeg, which decodes
to FLAC on a pipe (with tags and cover), and that pipe is read by opusenc.

Incremental mode (default): only processes what is new or changed in the
source (different mtime at the destination, or different size on direct copies).
The bitrate used is kept in a state file next to the destination folder
(.NAME.mirroropus.json). If you change -b, only the converted files are
redone; direct copies are left untouched. --overwrite redoes everything.

Always ignored (junk): dotfiles/dot-folders (.DS_Store, ._*, .Trash-1000,
.Trashes, .Spotlight-V100, ...), @eaDir, .AppleDouble, Thumbs.db, desktop.ini,
$RECYCLE.BIN, "System Volume Information", etc. Exception: .ndignore (Navidrome)
is copied. Symlinks are always ignored and never followed.

--prune deletes from the destination everything that no longer has a match in the source
(files removed/renamed in the master). It deletes right away, but:
  - always reports how many files and albums will be deleted;
  - does not run without -y/--yes if more albums than
    --prune-limit are affected (default 3);
  - never runs if the source has no audio files
    (unmounted disk, wrong path, ...);
  - never runs if some source folder could not be read (permissions, I/O error),
    because its files would look "removed".
With --dry-run, it only lists what would be deleted.
Temporary *.part files never count for the prune and are cleaned up at startup.

By default the script lowers its own priority (nice 10) so it does not get in
the way of other services on the server (e.g. Navidrome). Use --nice 0 to disable.

--tiny mode (minimal catalogue, for cheap cloud backup):
  - EVERY audio file (lossy or lossless) -> mono .opus, 8 kbps by default
    (tags and durations are kept; the quality is garbage on purpose)
  - Embedded covers -> resized to at most --cover-px pixels (200 by
    default) as low-quality JPEG; --cover-px 0 strips the embedded covers
  - Loose images (folder.jpg, scans, ...) -> small JPEG (same maximum width)
  - Playlists (.cue/.m3u/.m3u8) and small texts (.log, .nfo, .txt, ...) -> copied
    (playlists are rewritten to point at the .opus files)
  - Any other file (7z/zip of scans, PDFs, .mp4 without audio such as Tidal's
    animated covers, .sfv/.md5, ...) -> copied as-is. If you want a size
    ceiling, use --tiny-max-copy MiB: anything above it stops being copied.
  Use a dedicated destination folder: --tiny mode does not mix with the normal one.

--dont-skip-lossy (normal mode): convert EVERYTHING to .opus, including lossy
sources (mp3, aac, m4a, wma, ogg, mpc, .opus, ...), so even .opus files are
decoded and re-encoded to .opus. Files with no audio track (.mp4 covers) and
non-audio files are still copied as-is, and --keep-album exceptions are still
copied intact.

Kept albums (--keep-album ALBUM), applies to both modes (including plain
-b/--bitrate runs: the listed rarities stay lossless). Repeat the flag for each
album; a value is either:
  - an album folder name, matched anywhere in the tree ("Demo Tape [2003]" keeps
    that album whoever the artist is);
  - a path: relative to the SOURCE folder, relative to the current directory,
    or absolute ("/mnt/music/Artist/Album");
  - a .txt file with one of the above per line, for long lists.
Everything inside a kept album is COPIED INTACT (the original flac, big cover,
tags, name and extension) instead of being converted/downsized. Rules:
  - blank lines and lines starting with # are ignored in .txt lists;
  - a folder (by name or by path) keeps everything inside it (subfolders included);
  - a file path keeps only that file;
  - * matches anything (including slashes) and ? matches a single character; everything
    else is literal, so [brackets] and (parentheses) in names work as-is;
  - case matters; "/" or "\\" are equivalent.
Example (rarities.txt):
    # rarities
    Demo Tape [2003]
    Artist/Rare Single
    *Live at Some Radio Session*
Patterns that match nothing produce a warning (typo).
Playlists (.cue/.m3u) keep pointing at the .flac of kept files.

Usage:
  ./mirroropus.py SOURCE DEST
  ./mirroropus.py SOURCE DEST --tiny --keep-album rarities.txt
  ./mirroropus.py SOURCE DEST --tiny
  ./mirroropus.py SOURCE DEST --tiny --cover-px 128 --prune
  ./mirroropus.py SOURCE DEST -b 160 -j 8
  ./mirroropus.py SOURCE DEST --dont-skip-lossy -b 96
  ./mirroropus.py SOURCE DEST --keep-album "Demo Tape [2003]" -b 128
  ./mirroropus.py SOURCE DEST --keep-album rarities.txt --keep-album "/mnt/music/Artist/Album"
  ./mirroropus.py SOURCE DEST --overwrite
  ./mirroropus.py SOURCE DEST --dry-run
  ./mirroropus.py SOURCE DEST --prune
  ./mirroropus.py SOURCE DEST --prune -y --prune-limit 20

Requirements: opusenc; ffmpeg + ffprobe (for ALAC/APE/WV/TTA/TAK/SHN/DSD/WMA-lossless/Ogg-FLAC/.m4a/.caf).
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

# Read directly by opusenc (PCM/AIFF/simple FLAC)
OPUSENC_NATIVE = {".flac", ".wav", ".aif", ".aiff"}
# Lossless that always go through ffmpeg (opusenc cannot read them)
VIA_FFMPEG = {
    ".ape", ".wv", ".tta", ".tak", ".shn",
    ".dsf", ".dff", ".dsd",
}
# Ambiguous containers: only converted if the actual codec is lossless (see probe_codec)
# .m4a/.mp4: alac -> convert, aac -> copy
# .wma: wmalossless -> convert, the rest (wmav1/wmav2/wmapro/wmavoice) -> copy
# .ogg/.oga: flac -> convert, the rest (vorbis/opus/...) -> copy
# .caf/.aifc: alac/pcm/flac -> convert, the rest (aac/mp3/...) -> copy
NEEDS_PROBE = {".m4a", ".mp4", ".wma", ".ogg", ".oga", ".caf", ".aifc"}
# Playlists with references to audio files (rewritten at the destination)
PLAYLIST_EXTS = {".cue", ".m3u", ".m3u8"}

CONVERT_ACTIONS = ("opusenc", "ffmpeg")
# Everything that is re-encoded (audio + images in --tiny mode)
RECODE_ACTIONS = CONVERT_ACTIONS + ("image",)

# Images that --tiny mode downsizes to a small JPEG.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

TIMESTAMP_TOLERANCE = 2.0  # seconds (filesystems with coarse mtime)

# Extensions considered "music" (to count albums and validate the source).
# Includes lossy + lossless + ambiguous containers.
AUDIO_EXTS = {
    ".opus", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".wma",
    ".flac", ".wav", ".aif", ".aiff", ".aifc", ".caf",
    ".ape", ".wv", ".tta", ".tak", ".shn", ".mpc", ".mpp", ".mp+",
    ".dsf", ".dff", ".dsd",
}

# Junk folders that are never traversed nor mirrored.
IGNORE_DIRS = {
    "@eadir", ".appledouble",
    "$recycle.bin", "system volume information",
    ".trashes", ".spotlight-v100", ".fseventsd", ".temporaryitems",
    ".documentrevisions-v100", ".pk installsandboxmanager",
    "found.000",
}
# Junk files that are never mirrored.
IGNORE_FILES = {
    ".ds_store", "thumbs.db", "desktop.ini", ".apdisk",
}
# Dotfiles that are meaningful for the music server and MUST be copied.
KEEP_DOTFILES = {".ndignore"}


def _is_junk_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in IGNORE_DIRS


def _is_junk_file(name: str) -> bool:
    n = name.lower()
    if n in KEEP_DOTFILES:
        return False
    return name.startswith(".") or n in IGNORE_FILES  # includes ._* (AppleDouble)


@dataclass
class Job:
    src: Path
    dst: Path
    action: str  # "copy" | "opusenc" | "ffmpeg" | "rewrite" | "image"
    rel: str
    # Playlists only: paths (relative to the playlist's folder, with "/")
    # of the files that become .opus at the destination.
    convert_set: set[str] | None = None


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def probe_codec(path: Path) -> str:
    """Returns the codec of the 1st audio stream (lowercase) or "" on failure."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"WARNING: ffprobe failed on {path}: {e}", file=sys.stderr, flush=True)
        return ""
    if r.returncode != 0:
        print(f"WARNING: ffprobe could not read {path}: "
              f"{r.stderr.strip()[:200]}", file=sys.stderr, flush=True)
        return ""
    return r.stdout.strip().split(",")[0].strip().lower()


def classify_lossless(ext: str, codec: str) -> bool:
    """Says whether an ambiguous extension should be converted (lossless codec)."""
    if ext in (".m4a", ".mp4"):
        return codec == "alac"
    if ext == ".wma":
        return codec == "wmalossless"
    if ext in (".ogg", ".oga"):
        return codec == "flac"
    if ext in (".caf", ".aifc"):
        return codec == "alac" or codec == "flac" or codec.startswith("pcm_")
    return False


def _norm_path(s: str) -> str:
    """Forward slashes, no leading "./", Unicode NFC (accents match on any FS)."""
    s = s.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return unicodedata.normalize("NFC", s)


def _keep_rx(q: str) -> re.Pattern:
    return re.compile(
        "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in q)
        + r"\Z", re.DOTALL)


def load_keep_patterns(values: list[str]) -> list[tuple[str, re.Pattern]]:
    """Builds the keep patterns from the --keep-album values.

    Each value is an album name or path, or a .txt file with one per line. A
    bare name matches any folder/file with that name anywhere in the tree; a
    path may be relative to the source folder, relative to the current
    directory, or absolute. Only * and ? are wildcards; everything else is
    literal (brackets in folder names such as "[2003] Album" do not become
    character classes).
    """
    entries: list[str] = []
    for v in values:
        p = Path(v).expanduser()
        is_list = "*" not in v and "?" not in v and p.suffix.lower() == ".txt"
        if not is_list:
            entries.append(v)
            continue
        if not p.is_file():
            sys.exit(f"--keep-album: no such list file: {v}")
        try:
            entries.extend(p.read_text(encoding="utf-8-sig").splitlines())
        except (OSError, UnicodeDecodeError) as e:
            sys.exit(f"Could not read --keep-album list file {p}: {e}")

    pats: list[tuple[str, re.Pattern]] = []
    for line in entries:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        q = _norm_path(line).rstrip("/")
        if not q:
            continue
        pats.append((line, _keep_rx(q)))
        # Absolute paths are also matched in their symlink-resolved form, in
        # case the pattern was written through a symlink (e.g. /home/... ->
        # /mnt/...).
        if q.startswith("/") and "*" not in q and "?" not in q:
            q2 = _norm_path(os.path.realpath(q)).rstrip("/")
            if q2 != q:
                pats.append((line, _keep_rx(q2)))
    return pats


def _keep_targets(src: Path, src_root: Path) -> list[str]:
    """What a keep pattern may match against:
    - the single path components of the library-relative path, so a bare name
      (an album folder name) matches at any depth;
    - cumulative path prefixes (relative to the source root, relative to the
      current directory, absolute), so a folder path keeps everything inside it."""
    targets: list[str] = []
    try:
        targets.extend(_norm_path(str(src.relative_to(src_root))).split("/"))
    except ValueError:
        pass
    for base in (src_root, Path.cwd(), None):
        try:
            p = src if base is None else src.relative_to(base)
        except ValueError:
            continue
        parts = _norm_path(str(p)).split("/")
        targets.extend("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return targets


def keep_match(src: Path, src_root: Path,
               pats: list[tuple[str, re.Pattern]]) -> str | None:
    """Returns the pattern that keeps this file (itself or a parent folder)."""
    targets = _keep_targets(src, src_root)
    for text, rx in pats:
        if any(rx.match(t) for t in targets):
            return text
    return None


def build_plan(src_root: Path, dst_root: Path, jobs_hint: int,
               tiny: bool = False, max_copy: int | None = None,
               keep: list[tuple[str, re.Pattern]] | None = None,
               dont_skip_lossy: bool = False
               ) -> tuple[list[Job], dict[str, int], list[OSError]]:
    raw: list[tuple[Path, str, str]] = []  # (src, rel, ext)
    skipped = {"junk": 0, "symlink": 0, "big": 0, "kept": 0, "kept_bytes": 0}
    keep_hits: dict[str, int] = {text: 0 for text, _rx in (keep or [])}
    to_probe: list[Path] = []
    walk_errors: list[OSError] = []

    for dirpath, dirnames, filenames in os.walk(
            src_root, followlinks=False, onerror=walk_errors.append):
        # Does not descend into junk folders nor symbolic links.
        keep_dirs: list[str] = []
        for d in dirnames:
            full = Path(dirpath) / d
            if full.is_symlink():
                skipped["symlink"] += 1
                continue
            if _is_junk_dir(d):
                skipped["junk"] += 1
                continue
            keep_dirs.append(d)
        dirnames[:] = keep_dirs

        for name in filenames:
            src = Path(dirpath) / name
            if src.is_symlink():
                skipped["symlink"] += 1
                continue
            if _is_junk_file(name):
                skipped["junk"] += 1
                continue
            ext = src.suffix.lower()
            rel = str(src.relative_to(src_root))
            raw.append((src, rel, ext))
            # In --tiny mode everything goes through ffmpeg, so we only probe .mp4
            # (they can be animated covers / music videos without an audio track).
            if ext in NEEDS_PROBE and (not tiny or ext == ".mp4") and not (
                    keep and keep_match(src, src_root, keep) is not None):
                to_probe.append(src)

    # ffprobe is only mandatory if there are ambiguous files.
    codecs_by_path: dict[Path, str] = {}
    if to_probe:
        if shutil.which("ffprobe") is None:
            sys.exit("ffprobe is required to tell lossless from lossy "
                     "(.m4a/.wma/.ogg/.caf) but was not found.")
        workers = max(1, min(jobs_hint or 4, len(to_probe), (os.cpu_count() or 4) * 2))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(probe_codec, p): p for p in to_probe}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    codecs_by_path[p] = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"WARNING: error detecting the codec of {p}: {e}",
                          file=sys.stderr, flush=True)
                    codecs_by_path[p] = ""

    jobs: list[Job] = []
    for src, rel, ext in raw:
        action = "copy"
        kept = keep_match(src, src_root, keep) if keep else None
        if kept is not None:
            # Exception list: copied intact, no converting nor downsizing.
            keep_hits[kept] += 1
            skipped["kept"] += 1
            try:
                skipped["kept_bytes"] += src.stat().st_size
            except OSError:
                pass
        elif tiny:
            # .mp4 without an audio track (e.g. Tidal animated covers) is not music:
            # it falls under the "other files" rule (copied, unless above --tiny-max-copy).
            if ext in AUDIO_EXTS and not (
                    ext == ".mp4" and not codecs_by_path.get(src)):
                action = "ffmpeg"
            elif ext in IMAGE_EXTS:
                action = "image"
            elif max_copy is not None and ext not in PLAYLIST_EXTS:
                try:
                    size = src.stat().st_size
                except OSError:
                    size = 0
                if size > max_copy:
                    skipped["big"] += 1
                    continue
        elif ext in OPUSENC_NATIVE:
            action = "opusenc"
        elif ext in VIA_FFMPEG:
            action = "ffmpeg"
        elif ext in NEEDS_PROBE:
            codec = codecs_by_path.get(src, "")
            # With --dont-skip-lossy every container with audio is converted,
            # the only exception being .mp4 without an audio track.
            if classify_lossless(ext, codec) or (
                    dont_skip_lossy and not (ext == ".mp4" and not codec)):
                action = "ffmpeg"
            elif not codec:
                print(f"WARNING: no codec detected, copying as-is: {rel}",
                      file=sys.stderr, flush=True)
        elif dont_skip_lossy and ext in AUDIO_EXTS:
            # Lossy sources (mp3, aac, mpc, .opus, ...): decoded and re-encoded.
            action = "ffmpeg"

        dst = dst_root / rel
        if action in CONVERT_ACTIONS:
            dst = dst.with_suffix(".opus")
        elif action == "image":
            dst = dst.with_suffix(".jpg")
        jobs.append(Job(src, dst, action, rel))

    for text, hits in keep_hits.items():
        if hits == 0:
            print(f"WARNING: --keep-album: matched nothing: {text}",
                  file=sys.stderr, flush=True)

    # If two files collide on the same destination (e.g. track.flac and track.opus),
    # the direct copy wins; the rest is warned about and skipped.
    jobs.sort(key=lambda j: (j.action not in ("copy", "rewrite"), j.rel))
    seen: dict[Path, Job] = {}
    unique: list[Job] = []
    for j in jobs:
        if j.dst in seen:
            print(f"WARNING: destination collision, skipping {j.rel} "
                  f"({seen[j.dst].rel} already exists)", file=sys.stderr, flush=True)
            continue
        seen[j.dst] = j
        unique.append(j)
    unique.sort(key=lambda j: j.rel)

    attach_playlist_maps(unique)
    return unique, skipped, walk_errors


def attach_playlist_maps(jobs: list[Job]) -> None:
    """Playlists that reference converted files become "rewrite".

    Each playlist gets the set of paths (relative to its folder, including
    subfolders like CD1/01.flac) that become .opus. If the playlist references
    nothing converted in that tree, it stays a plain copy.
    """
    playlists = [j for j in jobs
                 if j.action == "copy" and j.src.suffix.lower() in PLAYLIST_EXTS]
    if not playlists:
        return
    play_dirs = {Path(j.rel).parent for j in playlists}
    by_dir: dict[Path, set[str]] = defaultdict(set)
    for j in jobs:
        if j.action not in CONVERT_ACTIONS:
            continue
        rel = Path(j.rel)
        d = rel.parent
        while True:
            if d in play_dirs:
                by_dir[d].add(rel.relative_to(d).as_posix())
            if d == d.parent:
                break
            d = d.parent
    for j in playlists:
        conv = by_dir.get(Path(j.rel).parent)
        if conv:
            j.action = "rewrite"
            j.convert_set = conv


_CUE_FILE_QUOTED = re.compile(r'^(\s*FILE\s+")([^"]*)(".*)$', re.IGNORECASE)
_CUE_FILE_BARE = re.compile(r'^(\s*FILE\s+)(\S+)(\s+\S+\s*)$', re.IGNORECASE)


def _swap_ext(ref: str, convert_set: set[str]) -> str:
    """Swaps the extension of ref to .opus if ref points at a converted file."""
    norm = ref.replace("\\", "/")
    key = norm[2:] if norm.startswith("./") else norm
    if key in convert_set:
        base, _dot, _ext = ref.rpartition(".")
        return base + ".opus"
    return ref


def rewrite_playlist_text(text: str, convert_set: set[str], is_cue: bool) -> str:
    """Rewrites references to converted files, line by line.

    Only touches references that match a converted file exactly
    (the whole name, not a fragment of it), so "01.flac" does not break "101.flac".
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        if is_cue:
            m = _CUE_FILE_QUOTED.match(body)
            if m:
                body = m.group(1) + _swap_ext(m.group(2), convert_set) + m.group(3)
            else:
                m = _CUE_FILE_BARE.match(body)
                if m:
                    body = m.group(1) + _swap_ext(m.group(2), convert_set) + m.group(3)
        elif body.strip() and not body.lstrip().startswith("#"):
            lead = body[:len(body) - len(body.lstrip())]
            tail = body[len(body.rstrip()):]
            body = lead + _swap_ext(body.strip(), convert_set) + tail
        out.append(body + eol)
    return "".join(out)


def detect_text_encoding(data: bytes) -> str:
    """BOM -> the BOM's encoding; otherwise UTF-8 if valid; otherwise latin-1."""
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if data[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return "utf-16"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def up_to_date(job: Job) -> bool:
    """Was the destination made from this version of the source?

    The destination's mtime is copied from the source, so it must be (almost)
    equal: if the source is replaced by an older version (backup restore) that
    is detected too. For direct copies the size must match as well.
    """
    try:
        d = job.dst.stat()
        s = job.src.stat()
    except FileNotFoundError:
        return False
    if d.st_size == 0:
        return False
    if job.action == "copy" and d.st_size != s.st_size:
        return False
    return abs(d.st_mtime - s.st_mtime) <= TIMESTAMP_TOLERANCE


def is_current(job: Job, args: argparse.Namespace) -> bool:
    if args.overwrite:
        return False
    if args.redo_converted and job.action in RECODE_ACTIONS:
        return False
    return up_to_date(job)


# --------------------------------------------------------------------------
# State (bitrate used on the last complete run)
# --------------------------------------------------------------------------
def state_path(dst_root: Path) -> Path:
    # Lives OUTSIDE the destination folder so Navidrome never sees this file.
    return dst_root.parent / f".{dst_root.name}.mirroropus.json"


def load_state(dst_root: Path) -> dict:
    try:
        data = json.loads(state_path(dst_root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(dst_root: Path, data: dict) -> None:
    try:
        state_path(dst_root).write_text(
            json.dumps(data) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"WARNING: could not save the bitrate state "
              f"({state_path(dst_root)}): {e}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Prune
# --------------------------------------------------------------------------
def find_orphans(dst_root: Path, expected: set[Path]) -> list[Path]:
    """Files at the destination that match nothing in the source."""
    if not dst_root.is_dir():
        return []
    orphans: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(dst_root):
        for name in filenames:
            if name.endswith(".part"):
                continue  # temporaries from interrupted runs; cleaned up separately
            p = Path(dirpath) / name
            if p not in expected:
                orphans.append(p)
    return sorted(orphans)


def clean_stale_parts(dst_root: Path, dry_run: bool) -> int:
    """Deletes *.part from interrupted runs. Returns how many were found."""
    if not dst_root.is_dir():
        return 0
    parts = list(dst_root.rglob("*.part"))
    if not parts:
        return 0
    if dry_run:
        print(f"Found {len(parts)} temporary *.part file(s) "
              f"(they would be removed).", flush=True)
        return len(parts)
    n = 0
    for p in parts:
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    if n:
        print(f"Removed {n} temporary *.part file(s) from a previous run.",
              flush=True)
    return n


def remove_empty_dirs(dst_root: Path) -> int:
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(dst_root, topdown=False):
        p = Path(dirpath)
        if p == dst_root:
            continue
        try:
            p.rmdir()  # only works if it is empty
            removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------
def encode_opusenc(src: Path, tmp: Path, bitrate: int) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["opusenc", "--quiet", "--bitrate", str(bitrate), str(src), str(tmp)],
            capture_output=True, text=True, timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, r.stderr.strip()


def _run_pipeline(ff_cmd: list[str], enc_cmd: list[str]) -> tuple[bool, str]:
    ff = enc = None
    try:
        ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        enc = subprocess.Popen(enc_cmd, stdin=ff.stdout, stderr=subprocess.PIPE)
        ff.stdout.close()  # SIGPIPE goes to ffmpeg if opusenc exits first
        _, enc_err = enc.communicate(timeout=3600)
        _, ff_err = ff.communicate(timeout=60)
        ok = ff.returncode == 0 and enc.returncode == 0
        msg = ((ff_err or b"") + (enc_err or b"")).decode(errors="replace").strip()
        return ok, msg
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    finally:
        for p in (enc, ff):
            if p is not None and p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass


def encode_ffmpeg(src: Path, tmp: Path, bitrate: int) -> tuple[bool, str]:
    base = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(src),
        "-map", "0:a:0", "-map_metadata", "0",
    ]
    audio = ["-c:a", "flac", "-compression_level", "0", "-f", "flac", "pipe:1"]
    cover = ["-map", "0:v:0?", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    enc_cmd = ["opusenc", "--quiet", "--bitrate", str(bitrate), "-", str(tmp)]

    # 1st attempt with embedded cover; on failure, 2nd without cover
    ok, msg = _run_pipeline(base + cover + audio, enc_cmd)
    if ok:
        return True, ""
    ok2, msg2 = _run_pipeline(base + audio, enc_cmd)
    return ok2, msg2 if not ok2 else ""


def encode_tiny(src: Path, tmp: Path, bitrate: int, cover_px: int) -> tuple[bool, str]:
    """Any audio -> tiny mono Opus, with tags and a downsized cover.

    ffmpeg decodes, downmixes to mono and delivers FLAC on a pipe (with tags and
    the already resized cover) to opusenc, which writes the tags and the cover
    into the .opus.
    """
    base = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(src),
        "-map", "0:a:0", "-map_metadata", "0",
    ]
    audio = ["-ac", "1", "-c:a", "flac", "-compression_level", "0",
             "-f", "flac", "pipe:1"]
    # 60 ms framesize: less header overhead at such low bitrates
    enc_cmd = ["opusenc", "--quiet", "--bitrate", str(bitrate),
               "--framesize", "60", "-", str(tmp)]

    if cover_px > 0:
        cover = [
            "-map", "0:v:0?", "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-q:v", "12",
            # only 1 frame: in a music-video .mp4 the "video" is not a cover
            "-frames:v", "1",
            "-vf", f"scale='min({cover_px},iw)':-2",
            "-disposition:v:0", "attached_pic",
        ]
        ok, _msg = _run_pipeline(base + cover + audio, enc_cmd)
        if ok:
            return True, ""
        tmp.unlink(missing_ok=True)
    # no cover (cover_px == 0, or the cover caused trouble)
    ok2, msg2 = _run_pipeline(base + audio, enc_cmd)
    return ok2, msg2 if not ok2 else ""


def encode_image(src: Path, tmp: Path, max_px: int) -> tuple[bool, str]:
    """Image -> small JPEG (max width max_px, low quality)."""
    px = max_px if max_px > 0 else 200
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-i", str(src),
                "-frames:v", "1", "-vf", f"scale='min({px},iw)':-2",
                "-pix_fmt", "yuvj420p", "-q:v", "12", "-f", "mjpeg", "-y", str(tmp),
            ],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, r.stderr.strip()


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
def run_job(job: Job, args: argparse.Namespace) -> tuple[str, str]:
    if is_current(job, args):
        return "skipped", ""

    job.dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = job.dst.with_name(job.dst.name + ".part")
    try:
        if job.action == "copy":
            shutil.copy2(job.src, tmp)
        elif job.action == "rewrite":
            data = job.src.read_bytes()
            enc = detect_text_encoding(data)
            text = data.decode(enc)
            out = rewrite_playlist_text(
                text, job.convert_set or set(), job.src.suffix.lower() == ".cue")
            tmp.write_bytes(out.encode(enc))
            st = job.src.stat()
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        else:
            if job.action == "image":
                ok, err = encode_image(job.src, tmp, args.cover_px)
            elif args.tiny:
                ok, err = encode_tiny(job.src, tmp, args.bitrate, args.cover_px)
            else:
                encoder = encode_opusenc if job.action == "opusenc" else encode_ffmpeg
                ok, err = encoder(job.src, tmp, args.bitrate)
                if (not ok or not tmp.exists() or tmp.stat().st_size == 0) \
                        and job.action == "opusenc":
                    # Fallback: exotic WAV/AIFF that opusenc cannot read
                    # (e.g. WAV with MP3 inside) try via ffmpeg.
                    tmp.unlink(missing_ok=True)
                    ok, err = encode_ffmpeg(job.src, tmp, args.bitrate)
            if not ok or not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError(err or "unknown failure")
            st = job.src.stat()
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, job.dst)
        if job.action == "copy":
            return "copied", ""
        if job.action == "rewrite":
            return "rewritten", ""
        return "converted", ""
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return "failed", str(e)


def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def size_summary(jobs: list[Job]) -> str | None:
    src_total = dst_total = 0
    for j in jobs:
        try:
            s = j.src.stat().st_size
            d = j.dst.stat().st_size
        except OSError:
            continue  # failed or does not exist yet: left out, to be fair
        src_total += s
        dst_total += d
    if not src_total:
        return None
    return (f"Size: source {human_size(src_total)} -> dest "
            f"{human_size(dst_total)} ({dst_total / src_total * 100:.0f}%).")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mirrors a music library: lossless -> Opus, everything else copied.",
        epilog="Note: changing --bitrate only redoes the converted files. "
               "Use --overwrite to redo absolutely everything.",
    )
    ap.add_argument("source", type=Path, help="master library folder")
    ap.add_argument("dest", type=Path, help="destination folder")
    ap.add_argument("-b", "--bitrate", type=int, default=None,
                    help="Opus bitrate in kbps (default: 128; 8 with --tiny)")
    ap.add_argument("--tiny", action="store_true",
                    help="minimal catalogue: ALL audio becomes very low quality "
                         "mono Opus (8 kbps), covers and images become tiny, "
                         "everything else (7z, logs, ...) is copied as-is")
    ap.add_argument("--dont-skip-lossy", action="store_true",
                    help="convert everything to Opus, including lossy sources "
                         "(mp3, aac, m4a, wma, ogg, opus, ...), even .opus -> .opus")
    ap.add_argument("--cover-px", type=int, default=200, metavar="N",
                    help="with --tiny: maximum width of covers/images in pixels "
                         "(default: 200; 0 strips the embedded covers)")
    ap.add_argument("--tiny-max-copy", type=float, default=None, metavar="MiB",
                    help="with --tiny: optional ceiling, in MiB, for other files "
                         "(7z, pdf, ...): the larger ones are not copied (default: "
                         "no ceiling, everything is copied)")
    ap.add_argument("--keep-album", "--keep-file", dest="keep_album",
                    action="append", default=None, metavar="ALBUM",
                    help="album(s) to keep intact (rare flac releases, ...), not "
                         "converted: repeat the flag per album; each value is an "
                         "album folder name (matched anywhere in the tree), a path "
                         "(relative to the source folder or to the current "
                         "directory, or absolute), or a .txt file with one per "
                         "line; works in --tiny mode and in normal -b/--bitrate "
                         "runs (--keep-file is an old alias)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                    help="parallel conversions (default: number of CPUs)")
    ap.add_argument("--nice", type=int, default=10, metavar="N",
                    help="process priority, 0-19 (default: 10; 0 disables)")
    ap.add_argument("--overwrite", action="store_true",
                    help="redo everything, even what already exists at the destination")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be done, without writing anything")
    ap.add_argument("--prune", action="store_true",
                    help="delete from the destination what no longer exists in the source")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="with --prune: allow deleting more albums than the limit")
    ap.add_argument("--prune-limit", type=int, default=3, metavar="N",
                    help="with --prune: maximum number of albums affected without -y (default: 3)")
    args = ap.parse_args()

    if args.bitrate is None:
        args.bitrate = 8 if args.tiny else 128
    if not 6 <= args.bitrate <= 512:
        sys.exit("Invalid bitrate: use 6-512 kbps.")
    if args.cover_px < 0:
        sys.exit("--cover-px must be >= 0.")
    if args.tiny_max_copy is not None and args.tiny_max_copy < 0:
        sys.exit("--tiny-max-copy must be >= 0.")
    if args.jobs < 1:
        sys.exit("--jobs must be >= 1.")
    if not 0 <= args.nice <= 19:
        sys.exit("--nice must be between 0 and 19.")

    src_root = args.source.resolve()
    dst_root = args.dest.resolve()

    if not src_root.is_dir():
        sys.exit(f"Source is not a folder: {src_root}")
    if dst_root == src_root or src_root in dst_root.parents:
        sys.exit("The destination cannot be inside the source.")
    if dst_root in src_root.parents:
        sys.exit("The source cannot be inside the destination.")

    if args.nice and hasattr(os, "nice"):
        try:
            os.nice(args.nice)  # opusenc/ffmpeg inherit it
        except OSError:
            pass

    prev_state = load_state(dst_root)
    if prev_state and bool(prev_state.get("tiny", False)) != args.tiny:
        was = "--tiny" if prev_state.get("tiny") else "normal"
        sys.exit(f"This destination was already used in {was} mode and the two "
                 f"modes do not mix. Use a different destination folder.")

    max_copy = (None if args.tiny_max_copy is None
                else int(args.tiny_max_copy * 1024 * 1024))
    keep = load_keep_patterns(args.keep_album) if args.keep_album else None
    jobs, skipped, walk_errors = build_plan(
        src_root, dst_root, args.jobs, tiny=args.tiny, max_copy=max_copy, keep=keep,
        dont_skip_lossy=args.dont_skip_lossy)

    if any(j.action in CONVERT_ACTIONS for j in jobs) and shutil.which("opusenc") is None:
        sys.exit("opusenc not found (install the opus-tools package).")
    if any(j.action in ("ffmpeg", "image") for j in jobs) and shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is required (ALAC/APE/WV/TTA/TAK/SHN/DSD/WMA-lossless, "
                 "--tiny mode, ...) but was not found.")

    # Settings changed since the last complete run? Then redo only the
    # re-encoded files (direct copies are left untouched).
    prev_bitrate = prev_state.get("bitrate")
    prev_cover = prev_state.get("cover_px")
    bitrate_changed = isinstance(prev_bitrate, int) and prev_bitrate != args.bitrate
    cover_changed = (args.tiny and isinstance(prev_cover, int)
                     and prev_cover != args.cover_px)
    settings_changed = bitrate_changed or cover_changed
    args.redo_converted = settings_changed and not args.overwrite

    total = len(jobs)
    n_convert = sum(1 for j in jobs if j.action in RECODE_ACTIONS)
    print(f"{total} files in the source ({n_convert} to convert). "
          f"Bitrate: {args.bitrate} kbps"
          + (f", mono, covers up to {args.cover_px}px. " if args.tiny else ". ")
          + f"Mode: {'overwrite' if args.overwrite else 'incremental'}"
          + (" + tiny" if args.tiny else "")
          + (" + lossy->opus" if args.dont_skip_lossy else "")
          + ".", flush=True)
    if skipped["junk"] or skipped["symlink"]:
        print(f"Ignored: {skipped['junk']} junk, "
              f"{skipped['symlink']} symlinks.", flush=True)
    if skipped["big"]:
        print(f"Ignored (--tiny): {skipped['big']} file(s) that are not audio/"
              f"image and are over {args.tiny_max_copy:g} MiB (they will not be copied).",
              flush=True)
    if skipped["kept"]:
        print(f"Kept intact (--keep-album): {skipped['kept']} file(s), "
              f"{human_size(skipped['kept_bytes'])}.", flush=True)
    if args.redo_converted:
        print("Settings changed (bitrate/covers): "
              "the converted files will be redone.", flush=True)
    if walk_errors:
        print(f"WARNING: {len(walk_errors)} source folder(s) could not be read "
              f"(their files were NOT mirrored):", file=sys.stderr, flush=True)
        for e in walk_errors[:10]:
            print(f"  {e.filename}: {e.strerror or e}", file=sys.stderr)
        if len(walk_errors) > 10:
            print(f"  ... and {len(walk_errors) - 10} more", file=sys.stderr)

    # ---- Prune: plan and validate BEFORE doing anything ----
    orphans: list[Path] = []
    if args.prune:
        if walk_errors:
            sys.exit("--prune refused: some source folders could not be read "
                     "and their files would look removed. Nothing was deleted.")
        if not any(j.src.suffix.lower() in AUDIO_EXTS for j in jobs):
            sys.exit("--prune refused: the source has no audio files "
                     "(unmounted disk or wrong path?). Nothing was deleted.")

        expected = {j.dst for j in jobs}
        orphans = find_orphans(dst_root, expected)
        albums = sorted({p.parent for p in orphans if p.suffix.lower() in AUDIO_EXTS})
        too_many = len(albums) > args.prune_limit

        if orphans:
            print(f"\nWARNING: --prune will delete {len(orphans)} file(s) from the "
                  f"destination, affecting {len(albums)} album(s).", file=sys.stderr, flush=True)

            if args.dry_run:
                for p in orphans:
                    print(f"[delete] {p.relative_to(dst_root)}")
                if too_many and not args.yes:
                    print(f"(without -y this would be refused: {len(albums)} albums > "
                          f"the limit of {args.prune_limit})")
            elif too_many and not args.yes:
                print(f"\nREFUSED: {len(albums)} album(s) would be affected, the limit "
                      f"without -y is {args.prune_limit}. Nothing was done (no conversions, "
                      f"no deletions).",
                      file=sys.stderr, flush=True)
                print("Affected albums:", file=sys.stderr)
                for a in albums[:30]:
                    print(f"  {a.relative_to(dst_root)}", file=sys.stderr)
                if len(albums) > 30:
                    print(f"  ... and {len(albums) - 30} more", file=sys.stderr)
                print("If that is really what you want, run again with -y "
                      "(or use --dry-run to see the full list).", file=sys.stderr)
                return 2
        else:
            print("\n--prune: nothing to delete at the destination.", flush=True)

    clean_stale_parts(dst_root, args.dry_run)

    if args.dry_run:
        counts = {"skipped": 0, "copy": 0, "convert": 0, "rewrite": 0}
        for j in jobs:
            if is_current(j, args):
                counts["skipped"] += 1
                continue
            if j.action in RECODE_ACTIONS:
                counts["convert"] += 1
                print(f"[convert] {j.rel}")
            elif j.action == "rewrite":
                counts["rewrite"] += 1
                print(f"[rewrite] {j.rel}")
            else:
                counts["copy"] += 1
                print(f"[copy] {j.rel}")
        print(f"\nDry-run: {counts['convert']} to convert, {counts['copy']} to copy, "
              f"{counts['rewrite']} playlist(s) to rewrite, "
              f"{counts['skipped']} already up to date"
              + (f", {len(orphans)} to delete." if args.prune else "."), flush=True)
        return 1 if walk_errors else 0

    stats = {"skipped": 0, "copied": 0, "rewritten": 0, "converted": 0, "failed": 0}
    failures: list[tuple[str, str]] = []
    done = 0

    # ---- Prune: delete now (validation passed) ----
    pruned = 0
    if orphans:
        for p in orphans:
            try:
                p.unlink()
                pruned += 1
                print(f"[delete] {p.relative_to(dst_root)}", flush=True)
            except OSError as e:
                failures.append((f"(delete) {p.relative_to(dst_root)}", str(e)))
        remove_empty_dirs(dst_root)

    # Manual executor (not `with`): on Ctrl-C a `with` would wait for ALL
    # pending jobs to finish, i.e. the whole library.
    ex = ThreadPoolExecutor(max_workers=args.jobs)
    interrupted = False
    try:
        futures = {ex.submit(run_job, j, args): j for j in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                status, err = fut.result()
            except Exception as e:  # noqa: BLE001
                status, err = "failed", str(e)
            stats[status] += 1
            done += 1
            if status == "failed":
                failures.append((job.rel, err))
                print(f"[{done}/{total}] ERROR: {job.rel}", file=sys.stderr, flush=True)
            elif status != "skipped":
                print(f"[{done}/{total}] {status}: {job.rel}", flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted: cancelling what has not started yet...",
              file=sys.stderr, flush=True)
        ex.shutdown(wait=True, cancel_futures=True)
    else:
        ex.shutdown(wait=True)

    if interrupted:
        print("You can run again to continue where you left off.",
              file=sys.stderr, flush=True)
        return 130

    print(f"\nDone: {stats['converted']} converted, {stats['copied']} copied, "
          f"{stats['rewritten']} playlist(s) rewritten, "
          f"{stats['skipped']} already up to date, {stats['failed']} failed"
          + (f", {pruned} deleted from the destination." if args.prune else "."), flush=True)
    summary = size_summary(jobs)
    if summary:
        print(summary, flush=True)

    # Only save the new bitrate if nothing failed: a file that failed keeps the
    # old bitrate and, without this, would count as up to date forever.
    if not (settings_changed and failures):
        state = {"bitrate": args.bitrate, "tiny": args.tiny}
        if args.tiny:
            state["cover_px"] = args.cover_px
        save_state(dst_root, state)
    elif failures:
        print("The new settings were NOT saved to the state because there were "
              "failures; the next run will redo the converted files again.",
              file=sys.stderr, flush=True)

    if failures:
        log = Path.cwd() / "mirroropus_errors.log"
        with log.open("w", encoding="utf-8") as f:
            for rel, err in failures:
                f.write(f"{rel}\n    {err}\n")
        print(f"Error details in: {log}", file=sys.stderr, flush=True)
        return 1
    return 1 if walk_errors else 0


if __name__ == "__main__":
    sys.exit(main())
