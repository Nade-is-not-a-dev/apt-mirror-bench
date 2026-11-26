# apt-mirror-bench

Benchmark APT mirrors by **real download speed** and switch to the fastest one.
Single file, Python standard library only — nothing to `pip install`.

## Why not apt-smart?

`apt-smart` is the tool usually recommended for this, but it is unmaintained
since 2020 and breaks on modern systems:

- It crashes on Ubuntu 22.04+ (`Failed to determine the distribution
  codename`) because its bundled release list predates `jammy` and it depends
  on the `distro-info-data` package being installed.
- It only understands the old one-line `sources.list` format, not the DEB822
  `.sources` files Ubuntu uses since 24.04.
- It ranks partly by ping/latency, not by actual download throughput.

`apt-mirror-bench` fixes all three: it detects the release from
`/etc/os-release`, understands both formats, and measures real throughput by
downloading the `Packages.gz` index from each mirror in parallel.

## Install

```bash
git clone https://github.com/Nade-is-not-a-dev/apt-mirror-bench.git
cd apt-mirror-bench
sudo install -m 755 apt-mirror-bench.py /usr/local/bin/apt-mirror-bench
```

Requires Python 3.8+ and internet access. No third-party dependencies.

## Usage

Rank mirrors without changing anything (default, safe to run as a normal user):

```bash
apt-mirror-bench
```

Show the ranking as JSON (for scripting):

```bash
apt-mirror-bench --json
```

Switch to the fastest healthy mirror (needs root, backs up your sources first):

```bash
sudo apt-mirror-bench --apply
sudo apt-get update
```

## Options

| Option | Default | What it does |
|---|---|---|
| `--limit N` | 40 | Max mirrors to test (`0` = all discovered) |
| `--timeout S` | 15 | Seconds allowed per download |
| `--workers N` | 8 | Parallel downloads |
| `--top N` | 10 | Rows shown in the table |
| `--max-stale-hours H` | 48 | A mirror whose `Release` file is older than this is treated as stale and won't win |
| `--allow-stale` | off | Allow a stale mirror to win if nothing fresh is available |
| `--distro ubuntu\|debian` | auto | Override distro detection |
| `--codename NAME` | auto | Override release codename (e.g. `jammy`, `noble`, `bookworm`) |
| `--json` | off | Print results as JSON instead of a table |
| `--apply` | off | Rewrite the APT sources to the fastest mirror |
| `--sources-list PATH` | `/etc/apt/sources.list` | One-line sources file to rewrite |
| `--sources-dir PATH` | `/etc/apt/sources.list.d` | Directory with `.list` / `.sources` files to rewrite |

## How it works

1. **Detect** the distro and codename from `/etc/os-release` (falls back to
   `lsb_release`, then to the codename already in your APT sources).
2. **Discover** mirrors: Ubuntu from `mirrors.ubuntu.com/mirrors.txt`
   (mirrors near you), Debian from the debian.org mirror list, plus whatever
   mirror you currently use. A built-in list is used if the network fetch
   fails.
3. **Benchmark** each mirror in parallel: fetch `dists/<codename>-updates/Release`
   (the `-updates` pocket is regenerated daily, so its `Date:` tells how
   recently the mirror synced — the base pocket's `Release` is frozen at
   release day and useless for this), then download
   `dists/<codename>/main/binary-<arch>/Packages.gz` and measure real
   bytes/second.
4. **Rank** by speed. Stale mirrors (Release older than `--max-stale-hours`)
   are listed but never win unless `--allow-stale` is given.
5. **`--apply`** rewrites only the main archive lines to the winner —
   PPAs, `security.ubuntu.com` and other third-party repos are left alone —
   and saves a timestamped backup (`sources.list.bak-YYYYMMDD-HHMMSS`)
   next to each file it touches.

## Exit codes

- `0` — a winner was found (and applied, if `--apply` was given)
- `1` — no usable mirrors found
- `2` — `--apply` without root on the system paths

## License

MIT. See [LICENSE](LICENSE).
