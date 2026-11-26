#!/usr/bin/env python3
"""apt-mirror-bench - benchmark APT mirrors by real download speed and pick the fastest.

Why another tool? apt-smart (the usual recommendation) is unmaintained since
2020: it crashes on Ubuntu 22.04+ because its bundled release list predates
"jammy" and it depends on the distro-info-data package being installed, and it
only understands the old one-line sources.list format. This tool is a single
file, standard library only, detects the release from /etc/os-release (no
distro-info needed), understands both one-line sources.list and DEB822
.sources files, and measures real download throughput instead of ping.

Usage:
    apt-mirror-bench.py [--limit N] [--timeout S] [--top N] [--json]
    apt-mirror-bench.py --apply [--allow-stale] [--max-stale-hours H]

    --apply rewrites the main archive mirror in your APT sources to the
    fastest healthy mirror (a timestamped backup is made first).
    Without --apply it only prints the ranking, changing nothing.
"""

import argparse
import concurrent.futures
import datetime
import email.utils
import json
import os
import platform
import shutil
import sys
import time
import urllib.error
import urllib.request

__version__ = "0.1.0"

USER_AGENT = "apt-mirror-bench/%s" % __version__

# Well-known mirrors, only used when the online mirror list cannot be fetched.
UBUNTU_FALLBACK_MIRRORS = [
    "http://archive.ubuntu.com/ubuntu",
    "http://us.archive.ubuntu.com/ubuntu",
    "http://mirrors.kernel.org/ubuntu",
    "http://mirror.hetzner.com/ubuntu",
    "http://ubuntu.mirror.ac.ke/ubuntu",
    "http://mirror.aarnet.edu.au/pub/ubuntu/archive",
]
DEBIAN_FALLBACK_MIRRORS = [
    "http://deb.debian.org/debian",
    "http://ftp.debian.org/debian",
    "http://ftp.us.debian.org/debian",
    "http://mirror.hetzner.com/debian",
    "http://mirrors.kernel.org/debian",
    "http://ftp.uk.debian.org/debian",
]

DEFAULT_SOURCES_LIST = "/etc/apt/sources.list"
DEFAULT_SOURCES_DIR = "/etc/apt/sources.list.d"


# ---------------------------------------------------------------------------
# distro detection
# ---------------------------------------------------------------------------

def detect_distro(codename_arg=None, distro_arg=None):
    """Return (distro_id, codename).

    distro_id is 'ubuntu' or 'debian'. The codename comes from
    /etc/os-release first, so no distro-info-data package is needed.
    """
    distro_id = (distro_arg or "").lower() or None
    codename = codename_arg or None
    os_release = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    os_release[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    if distro_id is None:
        distro_id = os_release.get("ID", "").lower()
        if distro_id not in ("ubuntu", "debian"):
            # ID_LIKE hints at the family (e.g. Linux Mint -> ubuntu).
            like = os_release.get("ID_LIKE", "").lower()
            if "ubuntu" in like or "debian" in like:
                distro_id = "ubuntu" if "ubuntu" in like else "debian"
    if codename is None:
        codename = os_release.get("VERSION_CODENAME") or None
    if codename is None:
        # lsb_release fallback (needs the lsb-release package).
        import subprocess
        try:
            out = subprocess.run(["lsb_release", "-cs"], capture_output=True,
                                 text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                codename = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if codename is None:
        codename = _codename_from_sources(distro_id)
    if distro_id not in ("ubuntu", "debian") or not codename:
        raise SystemExit(
            "error: could not detect distro/codename "
            "(use --distro ubuntu|debian and --codename <name>)")
    return distro_id, codename.lower()


def _codename_from_sources(distro_id):
    """Last-resort codename guess: first dist token in the APT sources."""
    roots = []
    if os.path.isfile(DEFAULT_SOURCES_LIST):
        roots.append(DEFAULT_SOURCES_LIST)
    if os.path.isdir(DEFAULT_SOURCES_DIR):
        roots += [os.path.join(DEFAULT_SOURCES_DIR, f)
                  for f in sorted(os.listdir(DEFAULT_SOURCES_DIR))
                  if f.endswith(".list") or f.endswith(".sources")]
    for path in roots:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if line.startswith(("deb ", "deb-src ")):
                tokens = line.split()
                if len(tokens) >= 3:
                    return tokens[2]
            if line.startswith("Suites:"):
                suites = line.split(":", 1)[1].split()
                if suites:
                    return suites[0]
    return None


def debian_arch():
    """Return the Debian architecture name (amd64, arm64, ...)."""
    import subprocess
    try:
        out = subprocess.run(["dpkg", "--print-architecture"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)


# ---------------------------------------------------------------------------
# mirror discovery
# ---------------------------------------------------------------------------

def _http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def discover_mirrors(distro_id):
    """Return (mirrors, source_note). Mirrors are archive-root URLs."""
    if distro_id == "ubuntu":
        try:
            raw = _http_get("https://mirrors.ubuntu.com/mirrors.txt", timeout=30)
            mirrors = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines()
                       if ln.strip().startswith(("http://", "https://"))]
            if mirrors:
                return mirrors, "mirrors.ubuntu.com (nearby mirrors)"
        except Exception as exc:  # noqa: BLE001 - offline is expected sometimes
            print("warning: could not fetch mirrors.ubuntu.com (%s), "
                  "using built-in list" % exc, file=sys.stderr)
        return list(UBUNTU_FALLBACK_MIRRORS), "built-in fallback list"
    try:
        raw = _http_get("https://www.debian.org/mirror/list", timeout=15)
        import re
        mirrors = sorted(set(
            m.rstrip("/") for m in
            re.findall(r'https?://[A-Za-z0-9.\-:/]+/debian/?', raw.decode("utf-8", "replace"))
        ))
        if mirrors:
            return mirrors, "debian.org mirror list"
    except Exception as exc:  # noqa: BLE001
        print("warning: could not fetch debian.org mirror list (%s), "
              "using built-in list" % exc, file=sys.stderr)
    return list(DEBIAN_FALLBACK_MIRRORS), "built-in fallback list"


def current_mirror_roots(distro_id):
    """Archive-root URLs currently configured in the APT sources."""
    roots = []
    paths = []
    if os.path.isfile(DEFAULT_SOURCES_LIST):
        paths.append(DEFAULT_SOURCES_LIST)
    if os.path.isdir(DEFAULT_SOURCES_DIR):
        paths += [os.path.join(DEFAULT_SOURCES_DIR, f)
                  for f in sorted(os.listdir(DEFAULT_SOURCES_DIR))
                  if f.endswith(".list") or f.endswith(".sources")]
    suffix = "/ubuntu" if distro_id == "ubuntu" else "/debian"
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        import re
        for url in re.findall(r'https?://[A-Za-z0-9.\-:/]+', text):
            url = url.rstrip("/")
            if url.endswith(suffix):
                roots.append(url)
            elif url.endswith(suffix + "/"):
                roots.append(url.rstrip("/"))
    return roots


def normalize_mirrors(mirrors, distro_id):
    suffix = "/ubuntu" if distro_id == "ubuntu" else "/debian"
    seen = set()
    out = []
    for mirror in mirrors:
        url = mirror.strip().rstrip("/")
        if not url.endswith(suffix):
            url += suffix
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ---------------------------------------------------------------------------
# benchmarking
# ---------------------------------------------------------------------------

class MirrorResult:
    def __init__(self, url):
        self.url = url
        self.ok = False
        self.speed_bps = 0.0      # bytes per second on the Packages file
        self.release_age_h = None  # hours since the Release file's Date:
        self.error = ""


def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _release_age_hours(release_bytes):
    for line in release_bytes.decode("utf-8", "replace").splitlines():
        if line.lower().startswith("date:"):
            try:
                dt = email.utils.parsedate_to_datetime(line.split(":", 1)[1].strip())
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    delta = datetime.datetime.now(datetime.timezone.utc) - dt
                    return max(0.0, delta.total_seconds() / 3600.0)
            except (ValueError, TypeError, OverflowError):
                return None
    return None


def bench_one(url, codename, arch, timeout):
    """Benchmark one mirror. Returns a MirrorResult."""
    res = MirrorResult(url)
    base = url.rstrip("/")
    # Freshness comes from the -updates pocket, whose Release file is
    # regenerated daily. The base pocket's Release is frozen at release
    # day (e.g. noble's still says April 2024) and says nothing about
    # how recently the mirror synced.
    release = None
    last_exc = None
    for suite in ("%s-updates" % codename, codename):
        try:
            release = _fetch("%s/dists/%s/Release" % (base, suite), timeout=timeout)
            break
        except Exception as exc:  # noqa: BLE001 - per-mirror failure is data
            last_exc = exc
    if release is None:
        res.error = "Release: %s" % last_exc
        return res
    res.release_age_h = _release_age_hours(release)
    packages_url = "%s/dists/%s/main/binary-%s/Packages.gz" % (base, codename, arch)
    try:
        start = time.monotonic()
        data = _fetch(packages_url, timeout=timeout)
        elapsed = time.monotonic() - start
        if not data or elapsed <= 0:
            res.error = "empty Packages file"
            return res
        res.speed_bps = len(data) / elapsed
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "Packages: %s" % exc
    return res


def benchmark(mirrors, codename, arch, timeout, workers, limit):
    tested = mirrors[:limit] if limit else mirrors
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(bench_one, url, codename, arch, timeout): url
                   for url in tested}
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    return results


# ---------------------------------------------------------------------------
# ranking / display
# ---------------------------------------------------------------------------

def human_speed(bps):
    if bps >= 1024 * 1024:
        return "%.2f MB/s" % (bps / (1024 * 1024))
    if bps >= 1024:
        return "%.0f KB/s" % (bps / 1024)
    return "%.0f B/s" % bps


def rank_results(results, max_stale_h):
    """Return (ranked_ok, stale, failed). Ranked by speed, fastest first."""
    ok = [r for r in results if r.ok]
    stale = [r for r in ok
             if r.release_age_h is not None and r.release_age_h > max_stale_h]
    fresh = [r for r in ok if r not in stale]
    fresh.sort(key=lambda r: r.speed_bps, reverse=True)
    stale.sort(key=lambda r: r.speed_bps, reverse=True)
    failed = [r for r in results if not r.ok]
    return fresh, stale, failed


def print_table(fresh, stale, failed, top):
    rows = fresh + stale
    print("%-4s %-55s %-12s %-12s %s" % ("#", "Mirror", "Speed", "Release age", "Status"))
    print("-" * 110)
    shown = 0
    for i, res in enumerate(rows, 1):
        if top and shown >= top:
            break
        age = ("%.0fh" % res.release_age_h) if res.release_age_h is not None else "?"
        status = "OK" if res in fresh else "STALE"
        print("%-4d %-55s %-12s %-12s %s" % (
            i, res.url[:55], human_speed(res.speed_bps), age, status))
        shown += 1
    if failed:
        print("\nUnreachable (%d):" % len(failed))
        for res in failed[:10]:
            print("  - %s (%s)" % (res.url, res.error[:100]))
        if len(failed) > 10:
            print("  ... and %d more" % (len(failed) - 10))


def results_json(fresh, stale, failed):
    def dump(res, status):
        return {"url": res.url,
                "speed_bps": round(res.speed_bps, 1),
                "release_age_hours": (round(res.release_age_h, 1)
                                      if res.release_age_h is not None else None),
                "status": status,
                "error": res.error}
    return {"fresh": [dump(r, "ok") for r in fresh],
            "stale": [dump(r, "stale") for r in stale],
            "failed": [dump(r, "failed") for r in failed]}


# ---------------------------------------------------------------------------
# --apply: rewrite the APT sources to the winning mirror
# ---------------------------------------------------------------------------

def _host_of(url):
    from urllib.parse import urlparse
    return urlparse(url).netloc.lower()


def rewrite_one_line_sources(path, winner_url, current_hosts):
    """Rewrite deb/deb-src lines in a one-line sources.list file.

    Only lines pointing at the archive (current main mirror hosts or
    archive.ubuntu.com / deb.debian.org) are rewritten; PPAs, security
    and third-party repos are left untouched. Returns (changed, new_text).
    """
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    changed = False
    out_lines = []
    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        tokens = line.split()
        if len(tokens) < 3 or tokens[0] not in ("deb", "deb-src"):
            out_lines.append(line)
            continue
        # tokens[1] is the URL, unless [options] are present.
        url_idx = 1
        if len(tokens) > 1 and tokens[1].startswith("["):
            url_idx = 2
        if url_idx >= len(tokens):
            out_lines.append(line)
            continue
        url = tokens[url_idx]
        if _host_of(url) in current_hosts:
            tokens[url_idx] = winner_url
            out_lines.append(" ".join(tokens))
            changed = True
        else:
            out_lines.append(line)
    return changed, "\n".join(out_lines) + ("\n" if original.endswith("\n") else "")


def rewrite_deb822_sources(path, winner_url, current_hosts, codename):
    """Rewrite URIs: in a DEB822 .sources file (Ubuntu 24.04+).

    Only stanzas whose Suites include the current codename and whose URIs
    point at the current main mirror are rewritten.
    Returns (changed, new_text).
    """
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    stanzas = original.split("\n\n")
    changed = False
    new_stanzas = []
    for stanza in stanzas:
        lines = stanza.splitlines()
        uris_idx = None
        suites = []
        for i, ln in enumerate(lines):
            if ln.startswith("URIs:"):
                uris_idx = i
            elif ln.startswith("Suites:"):
                suites = ln.split(":", 1)[1].split()
        if uris_idx is not None and codename in suites:
            uris = lines[uris_idx].split(":", 1)[1].split()
            new_uris = []
            for uri in uris:
                if _host_of(uri) in current_hosts:
                    new_uris.append(winner_url)
                    changed = True
                else:
                    new_uris.append(uri)
            lines[uris_idx] = "URIs: " + " ".join(new_uris)
        new_stanzas.append("\n".join(lines))
    return changed, "\n\n".join(new_stanzas)


def apply_winner(winner_url, distro_id, codename, sources_list, sources_dir):
    """Rewrite APT sources to winner_url. Returns list of changed files."""
    current_roots = current_mirror_roots(distro_id)
    current_hosts = {_host_of(u) for u in current_roots}
    # The default archive host is always fair game to replace.
    current_hosts.add("archive.ubuntu.com" if distro_id == "ubuntu"
                      else "deb.debian.org")
    changed_files = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    targets = []
    if sources_list and os.path.isfile(sources_list):
        targets.append((sources_list, "one-line"))
    if sources_dir and os.path.isdir(sources_dir):
        for name in sorted(os.listdir(sources_dir)):
            path = os.path.join(sources_dir, name)
            if name.endswith(".list"):
                targets.append((path, "one-line"))
            elif name.endswith(".sources"):
                targets.append((path, "deb822"))
    for path, kind in targets:
        if kind == "one-line":
            changed, new_text = rewrite_one_line_sources(path, winner_url, current_hosts)
        else:
            changed, new_text = rewrite_deb822_sources(path, winner_url,
                                                       current_hosts, codename)
        if not changed:
            continue
        backup = "%s.bak-%s" % (path, stamp)
        shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        changed_files.append((path, backup))
    return changed_files


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="apt-mirror-bench",
        description="Benchmark APT mirrors by real download speed and "
                    "optionally switch to the fastest one.")
    parser.add_argument("--version", action="version",
                        version="apt-mirror-bench %s" % __version__)
    parser.add_argument("--distro", choices=["ubuntu", "debian"],
                        help="override distro detection")
    parser.add_argument("--codename",
                        help="override release codename (e.g. jammy, noble)")
    parser.add_argument("--limit", type=int, default=40,
                        help="max mirrors to test (default: 40, 0 = all)")
    parser.add_argument("--timeout", type=int, default=15,
                        help="seconds per download (default: 15)")
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel downloads (default: 8)")
    parser.add_argument("--top", type=int, default=10,
                        help="rows shown in the table (default: 10)")
    parser.add_argument("--max-stale-hours", type=float, default=48,
                        help="Release older than this is stale (default: 48)")
    parser.add_argument("--allow-stale", action="store_true",
                        help="allow a stale mirror to win")
    parser.add_argument("--json", action="store_true",
                        help="print results as JSON")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite APT sources to the fastest mirror "
                             "(backup is made first)")
    parser.add_argument("--sources-list", default=None,
                        help="path to sources.list (default: /etc/apt/sources.list)")
    parser.add_argument("--sources-dir", default=None,
                        help="path to sources.list.d "
                             "(default: /etc/apt/sources.list.d)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    distro_id, codename = detect_distro(args.codename, args.distro)
    arch = debian_arch()
    using_system_paths = args.sources_list is None and args.sources_dir is None
    if args.apply and using_system_paths and os.geteuid() != 0:
        print("error: --apply needs root (it rewrites /etc/apt/sources.*)",
              file=sys.stderr)
        return 2

    mirrors, note = discover_mirrors(distro_id)
    current = current_mirror_roots(distro_id)
    for root in current:
        if root not in mirrors:
            mirrors.append(root)
    mirrors = normalize_mirrors(mirrors, distro_id)
    print("Distro: %s %s (%s) | mirrors from: %s | testing %d mirror(s)"
          % (distro_id, codename, arch, note,
             len(mirrors[:args.limit]) if args.limit else len(mirrors)),
          file=sys.stderr if args.json else sys.stdout)

    results = benchmark(mirrors, codename, arch, args.timeout,
                        args.workers, args.limit)
    fresh, stale, failed = rank_results(results, args.max_stale_hours)

    if args.json:
        print(json.dumps(results_json(fresh, stale, failed), indent=2))
    else:
        print_table(fresh, stale, failed, args.top)

    # In JSON mode stdout must stay pure JSON; human notes go to stderr.
    note = (lambda *a, **k: print(*a, file=sys.stderr, **k)) if args.json else print

    candidates = fresh if fresh else []
    if not candidates and args.allow_stale and stale:
        candidates = stale
    if not candidates:
        if stale and not args.allow_stale:
            print("\nAll reachable mirrors are stale (Release older than %.0fh). "
                  "Re-run with --allow-stale to use one anyway."
                  % args.max_stale_hours, file=sys.stderr)
        else:
            print("\nNo usable mirrors found.", file=sys.stderr)
        return 1

    winner = candidates[0]
    note("\nFastest%s: %s (%s)"
         % (" (stale)" if winner in stale else "",
            winner.url, human_speed(winner.speed_bps)))

    if not args.apply:
        note("Nothing changed. Re-run with --apply to switch to it.")
        return 0

    sources_list = args.sources_list or DEFAULT_SOURCES_LIST
    sources_dir = args.sources_dir or DEFAULT_SOURCES_DIR
    changed = apply_winner(winner.url, distro_id, codename,
                           sources_list, sources_dir)
    if not changed:
        note("No sources file needed changing (already on %s?)" % winner.url)
        return 0
    for path, backup in changed:
        note("Updated %s (backup: %s)" % (path, backup))
    note("Run `apt-get update` to use the new mirror.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
