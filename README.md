# autobrr-remove

Automatically remove torrents from qBittorrent based on custom, per-tracker
criteria to keep a target amount of free disk space available.

## Configuration

Configuration lives in a YAML file (validated with pydantic on startup). Copy
[`config.example.yaml`](config.example.yaml) to `config.yaml` and edit it:

```sh
cp config.example.yaml config.yaml
```

The config is organized into three independent features, each toggled with its
own `enabled` flag:

- **`remove_unregistered`** — deletes torrents whose tracker reports them as
  "unregistered", across all categories. Waits `delay_minutes` after first
  seeing the status before deleting (some trackers report it transiently).
- **`maintain_free_space`** — once free space drops below
  `free_space_threshold_gibi`, removes eligible torrents in `categories`
  (lowest upload rate first) until the threshold is met.
- **`set_seed_limits`** — sets qBittorrent share limits on torrents in
  `categories` that don't have any yet, so qBittorrent removes them once a limit
  is reached (`on_delete`: `Remove` keeps files, `RemoveWithContent` deletes
  them, `Stop` pauses).

Seed time and ratio limits are defined **per tracker** under `trackers`; a
torrent is matched to a tracker by its announce hostname. A torrent has met a
tracker's requirements once it has seeded longer than `seed_time_minutes` **or**
reached `ratio` — this drives both removal eligibility (`maintain_free_space`)
and the limits applied by `set_seed_limits`. Set either `seed_time_minutes` or
`ratio` (or the `default_*` values) to `-1` for **unlimited**: that dimension
never triggers removal, and `set_seed_limits` sets it to "no limit" in
qBittorrent. Torrents whose tracker is not listed are left untouched, unless
`set_seed_limits`'s `default_*` values apply.

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

### Docker

`compose.yaml` mounts `./config.yaml` into the container at
`/config/config.yaml`. Edit `config.yaml`, then:

```sh
docker compose up -d
```
