# autobrr-remove

Automatically remove torrents from qBittorrent based on custom, per-tracker
criteria to keep a target amount of free disk space available.

## Configuration

Configuration lives in a YAML file (validated with pydantic on startup). Copy
[`config.example.yaml`](config.example.yaml) to `config.yaml` and edit it:

```sh
cp config.example.yaml config.yaml
```

The config is organized into four independent features, each toggled with its
own `enabled` flag:

- **`remove_unregistered`** — deletes torrents whose tracker reports them as
  "unregistered", subject to its category filters.
  Waits `delay_minutes` after first seeing the status before deleting (some
  trackers report it transiently).
- **`remove_stopped`** — removes torrents after they have continuously appeared
  as completed and stopped for `delay_minutes`; actively seeding torrents are left
  alone. `on_delete` controls whether downloaded files are kept (`Remove`) or
  deleted (`RemoveWithContent`). First-seen times are held in memory and reset when
  autobrr-remove restarts.
- **`maintain_free_space`** — once free space drops below
  `free_space_threshold_gibi`, removes eligible torrents matching its category filters
  (lowest upload rate first) until the threshold is met.
- **`set_seed_limits`** — reconciles qBittorrent share limits for explicitly
  configured category policies. A policy can use tracker limits, override either
  limit, provide category-specific fallbacks, and override the action qBittorrent
  takes when a limit is reached.

`remove_unregistered`, `remove_stopped`, and `maintain_free_space` support the same
optional category filters. With only `categories`, only the listed categories are
checked. With only `ignore_categories`, every category except the listed ones is
checked. When both are set, `ignore_categories` takes precedence. Omit `categories`
or set it to `null` to include every category; a `null` entry represents torrents
with no category.

Seed time and ratio limits are defined **per tracker** under `trackers`; a
torrent is matched to a tracker by its announce hostname. A torrent has met a
tracker's requirements once it has seeded longer than `seed_time_minutes` **or**
reached `ratio` — this drives removal eligibility for `maintain_free_space` and
can supply limits to `set_seed_limits`. Set either tracker value to `-1` for
**unlimited**; that dimension never triggers removal.

### Seed-limit category policies

Only categories listed under `set_seed_limits.categories` are managed. Category
names are exact and case-sensitive; use `name: null` for uncategorized torrents.
Each name may appear only once. An omitted or empty list is a valid no-op.

```yaml
set_seed_limits:
  enabled: true
  default_seed_time_minutes: null
  default_ratio: null
  action: RemoveWithContent

  categories:
    - name: cross
      use_tracker_limits: true

    - name: upload
      seed_time_minutes: -1
      ratio: -1
      action: EnableSuperSeeding

    - name: dontcare
      seed_time_minutes: 0
      use_tracker_limits: true
      default_ratio: 0.5
```

Seed time and ratio are resolved independently in this order: an explicit category
limit, a matching tracker limit when `use_tracker_limits` is true (the default), a
category `default_*`, then the global `default_*`. Omitted category fields continue
to the next source. An explicitly configured `null` stops resolution for that
dimension and preserves the torrent's current value. A final global `null` also
preserves the current value. `-1` means unlimited, `0` and positive values are
explicit limits, and `-2` is not accepted in configuration.

The global `action`, optionally overridden per category, accepts `Default`, `Stop`,
`Remove`, `RemoveWithContent`, or `EnableSuperSeeding`. Trackers cannot override the
action. The feature also enforces `MatchAny` mode when supported (qBittorrent 5.3+;
earlier versions use that behavior implicitly) and an unlimited inactive-seeding
limit. It calls qBittorrent only when the resolved state differs from the torrent's
current state.

## Running

```sh
# single pass
autobrr-remove --config config.yaml

# run continuously, checking every `interval_seconds`
autobrr-remove --config config.yaml --daemon

# log what would be removed without deleting anything
autobrr-remove --config config.yaml --dry-run
```

The config path defaults to `config.yaml` in the working directory, or the
`CONFIG_FILE` environment variable if set.

### Logging

Logs are written to stdout as logfmt by default. Set `logging.format: json` for
JSON output. Both formats use the same core fields as wi1-bot: `ts`, `level`,
`logger`, `src` (`func_name:lineno`), and `msg`.

Log messages are static event names. Variable data is carried in scoped context
fields: each feature run binds `job`, torrent operations bind `torrent`,
`torrent_name`, `torrent_state`, and `torrent_size_bytes`, and individual
actions add fields such as durations, limits, and `dry_run`. Context is
automatically restored when that scope finishes. The optional rotating
`logging.file` output uses the same selected format.

### Docker

`compose.yaml` mounts `./config.yaml` into the container at
`/config/config.yaml`. Edit `config.yaml`, then:

```sh
docker compose up -d
```
