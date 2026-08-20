# tarr

`tarr` is a small automation service for qBittorrent. It applies tracker- and
category-specific retention rules, removes torrents that are no longer useful,
and keeps a configurable amount of disk space free. Run it once for a single
reconciliation pass or leave it running as a daemon.

The service talks to qBittorrent through its WebUI API and does not inspect the
download directory directly. Configuration is a strictly validated YAML file;
unknown fields and invalid limits stop startup instead of being silently ignored.

## What it manages

Each job is independent and can be enabled or disabled:

| Job | Behavior |
| --- | --- |
| `remove_unregistered` | Deletes a torrent and its content after a tracker continuously reports it as unregistered for a configured delay. |
| `remove_stopped` | Removes completed, stopped torrents after a delay, optionally keeping their downloaded content. Actively seeding torrents are untouched. |
| `set_seed_limits` | Reconciles qBittorrent ratio, seed-time, limit action, and related share-limit settings for explicitly managed categories. |
| `maintain_free_space` | When disk space is below the target, deletes eligible torrents and their content until the estimated target is reached. |

The delayed jobs keep their first-seen timestamps in memory. Restarting `tarr`
resets those timers, and delays greater than zero are only useful in daemon mode.
Use `--dry-run` to inspect planned deletions and share-limit changes without
mutating qBittorrent.

## Quick start

Requirements are Python 3.12 or newer and a reachable qBittorrent WebUI. Copy
the annotated example, enter the WebUI credentials, and start with a dry run:

```nu
cp config.example.yaml config.yaml
uv sync
uv run tarr --config config.yaml --dry-run
```

Run a real pass by removing `--dry-run`, or poll continuously:

```nu
uv run tarr --config config.yaml --daemon
```

The config path defaults to `config.yaml`. It can also be supplied with `-c` /
`--config` or through the `CONFIG_FILE` environment variable. In daemon mode,
`qbittorrent.interval_seconds` controls the polling interval and a failed pass is
logged before the next one is attempted.

## Configuration model

See [`config.example.yaml`](config.example.yaml) for the complete, documented
schema. Its three top-level sections are:

- `logging`: log level, output format, and optional rotating file output.
- `qbittorrent`: WebUI connection, polling interval, and the four job settings.
- `trackers`: tracker hostnames and their minimum seed-time and ratio rules.

### Categories

The three removal jobs share include/exclude filters. `categories: null` includes
all categories; otherwise only exact names in the list are included.
`ignore_categories` always wins over `categories`. Use YAML `null` in either list
for torrents that have no category.

`set_seed_limits` is intentionally opt-in: only exact category names listed under
that job are changed. A category named `null` manages uncategorized torrents.
Category names are case-sensitive and must be unique.

### Trackers and retention

A torrent is associated with the first configured tracker whose `hosts` entry
matches an announce hostname or its subdomain. Tracker policies provide a
`seed_time_minutes` and `ratio` value. For free-space cleanup, a torrent becomes
eligible after satisfying either value; `-1` makes that dimension unlimited, so
it can never make the torrent eligible.

When cleanup is needed, unmanaged trackers and excluded categories are skipped.
Eligible torrents are ordered by average ratio gained per second of seeding,
lowest first, and deleted with their content until enough space is estimated to
have been reclaimed.

### Share limits

For each managed category, seed time and ratio are resolved independently in
this order:

1. An explicit category value.
2. The matching tracker value when `use_tracker_limits` is enabled.
3. The category's `default_*` value.
4. The job's global `default_*` value.

An omitted value continues down that chain. An explicit `null` preserves the
torrent's current value, while `-1` sets an unlimited value. Zero and positive
numbers are applied as limits. The limit action can be `Default`, `Stop`,
`Remove`, `RemoveWithContent`, or `EnableSuperSeeding`, with an optional override
per category.

The job also sets the inactive-seeding limit to unlimited and uses `MatchAny`
semantics. qBittorrent versions before 5.3 already use those semantics implicitly.
No API update is sent when a torrent already matches the resolved state.

## Docker

The included [`compose.yaml`](compose.yaml) runs both qBittorrent and `tarr`.
Create `config.yaml`, change its qBittorrent host to
`http://qbittorrent:8080`, review the credentials and volume settings, then run:

```nu
docker compose up -d
```

The `tarr` image runs as UID/GID 999 and reads the mounted configuration from
`/config/config.yaml`. The compose file builds the local source by default;
remove `build` if you only want to use `ghcr.io/wthueb/tarr:latest`.

## Logging

Logs go to stdout in logfmt format. Set `logging.format: json` for JSON, and set
`logging.file` to additionally write 10 MiB rotating files; `file_count` controls
how many are retained. Events include stable `ts`, `level`, `logger`, `src`, and
`msg` fields plus job, torrent, limits, and deletion context where relevant.

## Development

The repository uses `uv` for dependency locking and Nix for a reproducible
development shell. `nix develop` syncs and activates the virtual environment.
The usual checks are:

```nu
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Source lives in [`tarr/`](tarr/), with configuration models in `config.py`, job
orchestration in `main.py`, and structured logging in `logging.py`. Tests live in
[`tests/`](tests/). Releases are packaged from `pyproject.toml` and the container
build is defined in [`Dockerfile`](Dockerfile).

## License

`tarr` is available under the [MIT License](LICENSE).
