# autobrr-remove

Automatically remove torrents from qBittorrent based on custom, per-tracker
criteria to keep a target amount of free disk space available.

## Configuration

Configuration lives in a YAML file (validated with pydantic on startup). Copy
[`config.example.yaml`](config.example.yaml) to `config.yaml` and edit it:

```sh
cp config.example.yaml config.yaml
```

Seed time and ratio limits are defined **per tracker**. A torrent is only
managed if one of its trackers is listed under `trackers`; torrents whose
trackers are not listed are left untouched. A managed torrent becomes eligible
for removal once it has either seeded longer than `seed_time_minutes` **or**
reached `ratio`. When free space drops below `free_space_threshold_gibi`,
eligible torrents are removed (lowest upload rate first) until the threshold is
met.

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
