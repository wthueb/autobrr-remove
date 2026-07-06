import argparse
import datetime
import logging
import logging.handlers
import os
import pathlib
import sys
import time

import qbittorrentapi
from pydantic import ValidationError

from autobrr_remove.config import (
    UNLIMITED,
    Config,
    LoggingConfig,
    QBittorrentConfig,
    RemoveUnregisteredConfig,
    load_config,
)

log = logging.getLogger("autobrr_remove")


def setup_logging(cfg: LoggingConfig) -> None:
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    handlers: list[logging.Handler] = [stream_handler]

    if cfg.file is not None:
        cfg.file.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            cfg.file,
            maxBytes=10 * 1024**2,  # 10 MiB
            backupCount=cfg.file_count,
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(level=cfg.level, handlers=handlers)


def build_client(cfg: QBittorrentConfig) -> qbittorrentapi.Client:
    client = qbittorrentapi.Client(
        host=cfg.host,
        username=cfg.username,
        password=cfg.password,
    )

    try:
        client.auth_log_in()
    except qbittorrentapi.LoginFailed as e:
        log.warning(f"failed to connect to qBittorrent: {e}")

    return client


def torrents_in_categories(
    client: qbittorrentapi.Client,
    categories: list[str] | None,
) -> list[qbittorrentapi.TorrentDictionary]:
    torrents = client.torrents_info()

    if categories is None:
        return list(torrents)

    return [t for t in torrents if t.category in categories]


def remove_unregistered(
    client: qbittorrentapi.Client,
    cfg: RemoveUnregisteredConfig,
    unregistered_first_seen: dict[str, datetime.datetime],
    dry_run: bool = False,
) -> None:
    log.debug("checking for unregistered torrents...")

    torrents = client.torrents_info()
    now = datetime.datetime.now()

    delay = datetime.timedelta(minutes=cfg.delay_minutes)

    currently_unregistered: set[str] = set()

    for torrent in torrents:
        trackers = torrent.trackers

        for tracker in trackers:
            # https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)#get-torrent-trackers
            if tracker.status in [0, 1, 3]:
                continue

            if tracker.msg == "unregistered torrent":
                # TL reports unregistered sometimes but then it goes away,
                # so we want to wait a bit before removing
                currently_unregistered.add(torrent.hash)

                if torrent.hash not in unregistered_first_seen:
                    unregistered_first_seen[torrent.hash] = now
                    log.debug(
                        f"first time seeing {torrent.hash[-6:]} as unregistered, will remove after {cfg.delay_minutes} minutes"
                    )

                first_seen = unregistered_first_seen[torrent.hash]
                time_unregistered = now - first_seen

                if time_unregistered >= delay:
                    action = "[dry-run] would remove" if dry_run else "removing"
                    log.info(
                        f"{action} unregistered torrent {torrent.hash[-6:]}: {torrent.name=} {torrent.state=} {torrent.size / 1024**3:.3f} GiB (unregistered for {time_unregistered})"
                    )
                    if not dry_run:
                        torrent.delete(delete_files=True)
                        unregistered_first_seen.pop(torrent.hash, None)
                else:
                    remaining = delay - time_unregistered
                    log.debug(
                        f"torrent {torrent.hash[-6:]} unregistered for {time_unregistered}, waiting {remaining} more before removal"
                    )
                break

    stale_hashes = set(unregistered_first_seen.keys()) - currently_unregistered
    for torrent_hash in stale_hashes:
        log.debug(f"torrent {torrent_hash[-6:]} is no longer unregistered, removing from tracking")
        unregistered_first_seen.pop(torrent_hash, None)


def set_seed_limits(
    client: qbittorrentapi.Client,
    config: Config,
    dry_run: bool = False,
) -> None:
    cfg = config.set_seed_limits

    log.debug("setting seed limits...")

    torrents = torrents_in_categories(client, cfg.categories)

    for torrent in torrents:
        # -2 means "use the global limit", i.e. the torrent has no explicit limit set.
        # leave torrents that already have a ratio or seeding-time limit untouched.
        if torrent.ratio_limit != -2 or torrent.seeding_time_limit != -2:
            log.debug(f"{torrent.hash[-6:]}: {torrent.name} already has share limits set, skipping")
            continue

        tracker = config.match_tracker(t.url for t in torrent.trackers)

        if tracker is not None:
            seed_time_minutes = tracker.seed_time_minutes
            ratio = tracker.ratio
            source = tracker.name
        elif cfg.default_seed_time_minutes is not None and cfg.default_ratio is not None:
            seed_time_minutes = cfg.default_seed_time_minutes
            ratio = cfg.default_ratio
            source = "default"
        else:
            log.debug(
                f"{torrent.hash[-6:]}: {torrent.name} has no configured tracker "
                "and no defaults, skipping"
            )
            continue

        ratio_desc = "unlimited" if ratio == UNLIMITED else ratio
        seed_desc = "unlimited" if seed_time_minutes == UNLIMITED else f"{seed_time_minutes}m"

        action = "[dry-run] would set" if dry_run else "setting"
        log.info(
            f"{action} share limits on {torrent.hash[-6:]} [{source}]: {torrent.name} -> "
            f"ratio={ratio_desc} seed_time={seed_desc} on_delete={cfg.on_delete}"
        )

        if not dry_run:
            torrent.set_share_limits(
                ratio_limit=str(ratio),
                seeding_time_limit=seed_time_minutes,
                share_limit_action=cfg.on_delete,
                share_limits_mode="MatchAny",
            )


def maintain_free_space(
    client: qbittorrentapi.Client,
    config: Config,
    dry_run: bool = False,
) -> None:
    log.debug("checking free space...")

    torrents = torrents_in_categories(client, config.maintain_free_space.categories)
    free_space = client.sync_maindata().server_state.free_space_on_disk
    threshold = config.maintain_free_space.free_space_threshold_bytes

    if free_space > threshold:
        log.info(
            f"{free_space / 1024**4:.3f} TiB free, nothing to do ({threshold / 1024**4} TiB threshold)"
        )
        return

    possible_to_remove: list[qbittorrentapi.TorrentDictionary] = []

    for torrent in torrents:
        tracker = config.match_tracker(t.url for t in torrent.trackers)

        if tracker is None:
            log.debug(
                f"{torrent.hash[-6:]}: {torrent.name} is not managed by any configured tracker, skipping"
            )
            continue

        seeding_time = datetime.timedelta(seconds=torrent.seeding_time)
        size = torrent.size / 1024**3
        uploaded = torrent.uploaded / 1024**3
        upload_rate = torrent.uploaded / torrent.seeding_time if torrent.seeding_time > 0 else 0

        log.debug(
            f"checking {torrent.hash[-6:]} [{tracker.name}]: {torrent.name} ({torrent.state}) {size=:.2f} GiB {uploaded=:.2f} GiB ({torrent.ratio:.2f}) {seeding_time=} ({upload_rate} B/s)"
        )

        # a torrent is eligible for removal once it has met either the seed-time or the
        # ratio minimum. a limit of -1 (unlimited) means that dimension is never met.
        seed_time_met = tracker.seed_time_minutes != UNLIMITED and (
            seeding_time > datetime.timedelta(minutes=tracker.seed_time_minutes)
        )
        ratio_met = tracker.ratio != UNLIMITED and torrent.ratio >= tracker.ratio

        if not (seed_time_met or ratio_met):
            log.debug(
                f"skipping since seeding time and ratio do not meet {tracker.name} minimums ({tracker.seed_time_minutes}m / {tracker.ratio})"
            )
            continue

        possible_to_remove.append(torrent)

    possible_to_remove.sort(
        key=lambda t: t.uploaded / t.seeding_time if t.seeding_time > 0 else float("inf")
    )

    log.debug(f"found {len(possible_to_remove)} torrents that satisfy removal criteria")

    while possible_to_remove and free_space < threshold:
        torrent = possible_to_remove.pop(0)

        seeding_time = datetime.timedelta(seconds=torrent.seeding_time)
        size = torrent.size / 1024**3
        uploaded = torrent.uploaded / 1024**3
        upload_rate = torrent.uploaded / torrent.seeding_time if torrent.seeding_time > 0 else 0

        action = "[dry-run] would remove" if dry_run else "removing"
        log.info(
            f"{action} {torrent.hash[-6:]}: {torrent.name} ({torrent.state}) {size=:.2f} GiB {uploaded=:.2f} GiB ({torrent.ratio:.2f}) {seeding_time=} ({upload_rate} B/s)"
        )

        if not dry_run:
            torrent.delete(delete_files=True)

        free_space += torrent.size


def run(
    client: qbittorrentapi.Client,
    config: Config,
    unregistered_first_seen: dict[str, datetime.datetime],
    dry_run: bool = False,
) -> None:
    log.debug("starting run...")

    if config.remove_unregistered.enabled:
        remove_unregistered(client, config.remove_unregistered, unregistered_first_seen, dry_run)

    if config.set_seed_limits.enabled:
        set_seed_limits(client, config, dry_run)

    if config.maintain_free_space.enabled:
        maintain_free_space(client, config, dry_run)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("CONFIG_FILE", "config.yaml")),
        help="path to the YAML config file (default: config.yaml, or $CONFIG_FILE)",
    )
    parser.add_argument("-d", "--daemon", action="store_true")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="log what would be removed without deleting anything",
    )

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        raise SystemExit(f"config file not found: {args.config}")
    except ValidationError as e:
        raise SystemExit(f"invalid config file {args.config}:\n{e}")

    setup_logging(config.logging)

    client = build_client(config.qbittorrent)

    if args.dry_run:
        log.info("dry-run mode: no torrents will be deleted")

    unregistered_first_seen: dict[str, datetime.datetime] = {}

    if not args.daemon:
        run(client, config, unregistered_first_seen, args.dry_run)
        return

    log.info(f"running in daemon mode, checking every {config.interval_seconds} seconds...")

    while True:
        try:
            run(client, config, unregistered_first_seen, args.dry_run)
        except Exception as e:
            log.error(f"error during run: {e}", exc_info=True)

        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
