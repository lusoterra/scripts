# aur_overlay.py - minimal AUR overlay updater

## Files
- `aur_overlay.py` - main script
- `makepkg-aur.sh` - makepkg wrapper that records git commit metadata

## Usage
```
python3 aur_overlay.py --root ~/aur
python3 aur_overlay.py --root ~/aur --dry-run
python3 aur_overlay.py --root ~/aur --fetch --verbose
```
Environment: `AUR_OVERLAY_ROOT` sets default root.

## Commit metadata integration with manual makepkg builds
Local `-git` packages need recorded upstream commit hashes. Two options:

1. Use the wrapper instead of makepkg:
   ```
   cd ~/aur/foo-git
   /path/to/makepkg-aur.sh -f
   ```
   It runs makepkg, then records `.aur-overlay.json`.

2. Build normally, then record:
   ```
   cd ~/aur/foo-git
   makepkg -f
   python3 /path/to/aur_overlay.py --record .
   ```

The sidecar `.aur-overlay.json` stores package name, declared version, upstream git
source URL, commit hash, branch/revision, and PKGBUILD repo commit. Keep this file
alongside the PKGBUILD (it is untracked by default; add to .gitignore if desired).

If no metadata exists for an installed git package, its status is reported as
`unknown` and it is not rebuilt automatically.

## Update detection
- Normal packages: compares declared `epoch:pkgver-pkgrel` to installed using
  `vercmp`; rebuilds only when local is newer.
- Git packages: resolves current upstream commit via `git ls-remote` for each git
  source and compares to the recorded commit; rebuilds only on change.
- PKGBUILD git repos: `--fetch` fetches them without merging; reports if behind
  upstream or locally modified. Fetch never overwrites uncommitted changes.

## Limitations
- Requires network for git upstream checks.
- Commit metadata must be recorded after builds (use wrapper or --record).
- Does not install packages; built packages remain in package dirs.
- Split packages are rebuilt as a unit when any installed member is outdated.
- Offline upstream checks report unknown status rather than assuming current.
- `makepkg` must be able to fetch sources; SRCDEST defaults to package dir.
