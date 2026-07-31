import argparse
import datetime
import os
import pathlib
import time
from collections.abc import Iterable
from typing import Any, Protocol

import qbittorrentapi
import structlog
from pydantic import ValidationError
from qbittorrentapi.torrents import TorrentStatusesT
from structlog.contextvars import bound_contextvars

from autobrr_remove.config import (
    UNLIMITED,
    Config,
    QBittorrentConfig,
    RemoveStoppedConfig,
    RemoveUnregisteredConfig,
    category_is_included,
    load_config,
)
from autobrr_remove.logging import setup_logging

log = structlog.stdlib.get_logger("autobrr_remove")

STOPPED_COMPLETED_STATES = {"pausedUP", "stoppedUP"}


class TorrentsClient(Protocol):
    def torrents_info(self, status_filter: TorrentStatusesT | None = None) -> Iterable[Any]: ...


def _torrent_context(torrent: Any) -> dict[str, Any]:
    return {
        "torrent": torrent.hash,
        "torrent_name": torrent.name,
        "torrent_state": torrent.state,
        "torrent_size_bytes": torrent.size,
    }


def build_client(cfg: QBittorrentConfig) -> qbittorrentapi.Client:
    client = qbittorrentapi.Client(
        host=cfg.host,
        username=cfg.username,
        password=cfg.password,
    )

    try:
        client.auth_log_in()
    except qbittorrentapi.LoginFailed as e:
        with bound_contextvars(error=str(e)):
            log.warning("failed to connect to qBittorrent")

    return client


def torrents_in_categories(
    client: TorrentsClient,
    categories: list[str | None] | None,
    ignore_categories: list[str | None] | None = None,
    status_filter: TorrentStatusesT | None = None,
) -> list[qbittorrentapi.TorrentDictionary]:
    if status_filter is None:
        torrents = client.torrents_info()
    else:
        torrents = client.torrents_info(status_filter=status_filter)
    ignored = ignore_categories or []

    return [
        torrent
        for torrent in torrents
        if category_is_included(torrent.category, categories, ignored)
    ]


def warn_category_overlaps(config: Config) -> None:
    feature_configs = (
        ("remove_unregistered", config.remove_unregistered),
        ("remove_stopped", config.remove_stopped),
        ("maintain_free_space", config.maintain_free_space),
        ("set_seed_limits", config.set_seed_limits),
    )

    for feature_name, cfg in feature_configs:
        overlap = cfg.overlapping_categories
        if not overlap:
            continue

        with bound_contextvars(job=feature_name, overlapping_categories=overlap):
            log.warning("categories overlap with ignore_categories")


def remove_unregistered(
    client: qbittorrentapi.Client,
    cfg: RemoveUnregisteredConfig,
    unregistered_first_seen: dict[str, datetime.datetime],
    dry_run: bool = False,
) -> None:
    log.info("checking for unregistered torrents...")

    torrents = torrents_in_categories(client, cfg.categories, cfg.ignore_categories)
    now = datetime.datetime.now()

    delay = datetime.timedelta(minutes=cfg.delay_minutes)

    currently_unregistered: set[str] = set()

    for torrent in torrents:
        with bound_contextvars(**_torrent_context(torrent)):
            log.debug("checking torrent")

            trackers = torrent.trackers

            for tracker in trackers:
                # https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)#get-torrent-trackers
                if tracker.status in [0, 1, 3]:
                    continue

                msg = tracker.msg.lower()

                if (
                    "unregistered torrent" in msg
                    or "torrent does not exist on this tracker" in msg
                    or "torrent has been deleted" in msg
                ):
                    # TL reports unregistered sometimes but then it goes away,
                    # so we want to wait a bit before removing
                    currently_unregistered.add(torrent.hash)

                    if torrent.hash not in unregistered_first_seen:
                        unregistered_first_seen[torrent.hash] = now
                        with bound_contextvars(delay_minutes=cfg.delay_minutes):
                            log.debug("first observed unregistered torrent")

                    first_seen = unregistered_first_seen[torrent.hash]
                    time_unregistered = now - first_seen

                    if time_unregistered >= delay:
                        with bound_contextvars(
                            dry_run=dry_run,
                            delete_files=True,
                            unregistered_for_seconds=time_unregistered.total_seconds(),
                        ):
                            log.info("unregistered torrent eligible for removal")
                            if not dry_run:
                                torrent.delete(delete_files=True)
                                unregistered_first_seen.pop(torrent.hash, None)
                    else:
                        remaining = delay - time_unregistered
                        with bound_contextvars(
                            unregistered_for_seconds=time_unregistered.total_seconds(),
                            remaining_seconds=remaining.total_seconds(),
                        ):
                            log.debug("unregistered torrent removal delayed")
                    break

    stale_hashes = set(unregistered_first_seen.keys()) - currently_unregistered
    for torrent_hash in stale_hashes:
        with bound_contextvars(torrent=torrent_hash):
            log.debug("torrent no longer unregistered; clearing tracking state")
            unregistered_first_seen.pop(torrent_hash, None)


def remove_stopped(
    client: TorrentsClient,
    cfg: RemoveStoppedConfig,
    stopped_first_seen: dict[str, datetime.datetime],
    dry_run: bool = False,
) -> None:
    log.info("checking for stopped torrents...")

    completed_torrents = torrents_in_categories(
        client,
        cfg.categories,
        cfg.ignore_categories,
        status_filter="completed",
    )
    torrents = [
        torrent for torrent in completed_torrents if torrent.state in STOPPED_COMPLETED_STATES
    ]
    now = datetime.datetime.now()
    delay = datetime.timedelta(minutes=cfg.delay_minutes)
    currently_stopped = {torrent.hash for torrent in torrents}

    for torrent in torrents:
        with bound_contextvars(**_torrent_context(torrent)):
            log.debug("checking torrent")

            if torrent.hash not in stopped_first_seen:
                stopped_first_seen[torrent.hash] = now
                with bound_contextvars(delay_minutes=cfg.delay_minutes):
                    log.debug("first observed completed and stopped torrent")

            first_seen = stopped_first_seen[torrent.hash]
            time_stopped = now - first_seen

            if time_stopped < delay:
                remaining = delay - time_stopped
                with bound_contextvars(
                    stopped_for_seconds=time_stopped.total_seconds(),
                    remaining_seconds=remaining.total_seconds(),
                ):
                    log.debug("stopped torrent removal delayed")
                continue

            delete_files = cfg.on_delete == "RemoveWithContent"
            with bound_contextvars(
                dry_run=dry_run,
                delete_files=delete_files,
                on_delete=cfg.on_delete,
                stopped_for_seconds=time_stopped.total_seconds(),
            ):
                log.info("stopped torrent eligible for removal")

                if not dry_run:
                    torrent.delete(delete_files=delete_files)
                    stopped_first_seen.pop(torrent.hash, None)

    stale_hashes = set(stopped_first_seen) - currently_stopped
    for torrent_hash in stale_hashes:
        with bound_contextvars(torrent=torrent_hash):
            log.debug("torrent no longer completed and stopped; clearing tracking state")
            stopped_first_seen.pop(torrent_hash, None)


def set_seed_limits(
    client: qbittorrentapi.Client,
    config: Config,
    dry_run: bool = False,
) -> None:
    cfg = config.set_seed_limits

    log.info("setting seed limits...")

    torrents = torrents_in_categories(client, cfg.categories, cfg.ignore_categories)

    for torrent in torrents:
        with bound_contextvars(**_torrent_context(torrent)):
            # -2 means "use the global limit", i.e. the torrent has no explicit limit set.
            # leave torrents that already have a ratio or seeding-time limit untouched.
            if torrent.ratio_limit != -2 or torrent.seeding_time_limit != -2:
                with bound_contextvars(
                    ratio_limit=torrent.ratio_limit,
                    seeding_time_limit=torrent.seeding_time_limit,
                ):
                    log.debug("torrent already has share limits; skipping")
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
                log.debug("torrent has no matching share-limit configuration; skipping")
                continue

            with bound_contextvars(
                dry_run=dry_run,
                limit_source=source,
                ratio_limit=ratio,
                seeding_time_limit_minutes=seed_time_minutes,
                on_delete=cfg.on_delete,
            ):
                log.info("torrent share limits resolved")

                if not dry_run:
                    torrent.set_share_limits(
                        ratio_limit=str(ratio),
                        seeding_time_limit=seed_time_minutes,
                        inactive_seeding_time_limit=-2,
                        share_limit_action=cfg.on_delete,
                        share_limits_mode="MatchAny",
                    )


def maintain_free_space(
    client: qbittorrentapi.Client,
    config: Config,
    dry_run: bool = False,
) -> None:
    log.info("maintaining free space...")

    cfg = config.maintain_free_space
    torrents = torrents_in_categories(client, cfg.categories, cfg.ignore_categories)
    free_space = client.sync_maindata().server_state.free_space_on_disk
    threshold = cfg.free_space_threshold_bytes

    if free_space > threshold:
        with bound_contextvars(free_space_bytes=free_space, threshold_bytes=threshold):
            log.info("free space above threshold; nothing to do")
        return

    possible_to_remove: list[qbittorrentapi.TorrentDictionary] = []

    for torrent in torrents:
        with bound_contextvars(**_torrent_context(torrent)):
            tracker = config.match_tracker(t.url for t in torrent.trackers)

            if tracker is None:
                log.debug("torrent is not managed by a configured tracker; skipping")
                continue

            seeding_time = datetime.timedelta(seconds=torrent.seeding_time)
            upload_rate = torrent.uploaded / torrent.seeding_time if torrent.seeding_time > 0 else 0

            with bound_contextvars(
                tracker=tracker.name,
                torrent_ratio=torrent.ratio,
                uploaded_bytes=torrent.uploaded,
                seeding_time_seconds=torrent.seeding_time,
                upload_rate_bytes_per_second=upload_rate,
                minimum_seed_time_minutes=tracker.seed_time_minutes,
                minimum_ratio=tracker.ratio,
            ):
                log.debug("checking torrent removal eligibility")

                # a torrent is eligible for removal once it has met either the seed-time or the
                # ratio minimum. a limit of -1 (unlimited) means that dimension is never met.
                seed_time_met = tracker.seed_time_minutes != UNLIMITED and (
                    seeding_time > datetime.timedelta(minutes=tracker.seed_time_minutes)
                )
                ratio_met = tracker.ratio != UNLIMITED and torrent.ratio >= tracker.ratio

                if not (seed_time_met or ratio_met):
                    log.debug("torrent does not meet removal criteria")
                    continue

            possible_to_remove.append(torrent)

    possible_to_remove.sort(
        key=lambda t: t.uploaded / t.seeding_time if t.seeding_time > 0 else float("inf")
    )

    with bound_contextvars(eligible_torrent_count=len(possible_to_remove)):
        log.debug("identified torrents meeting removal criteria")

    while possible_to_remove and free_space < threshold:
        torrent = possible_to_remove.pop(0)

        with bound_contextvars(**_torrent_context(torrent)):
            upload_rate = torrent.uploaded / torrent.seeding_time if torrent.seeding_time > 0 else 0

            with bound_contextvars(
                dry_run=dry_run,
                delete_files=True,
                torrent_ratio=torrent.ratio,
                uploaded_bytes=torrent.uploaded,
                seeding_time_seconds=torrent.seeding_time,
                upload_rate_bytes_per_second=upload_rate,
            ):
                log.info("torrent selected to reclaim free space")

                if not dry_run:
                    torrent.delete(delete_files=True)

            free_space += torrent.size


def run(
    client: qbittorrentapi.Client,
    config: Config,
    unregistered_first_seen: dict[str, datetime.datetime],
    dry_run: bool = False,
    stopped_first_seen: dict[str, datetime.datetime] | None = None,
) -> None:
    log.info("starting run...")

    if stopped_first_seen is None:
        stopped_first_seen = {}

    if config.remove_unregistered.enabled:
        with bound_contextvars(job="remove_unregistered"):
            remove_unregistered(
                client,
                config.remove_unregistered,
                unregistered_first_seen,
                dry_run,
            )

    if config.remove_stopped.enabled:
        with bound_contextvars(job="remove_stopped"):
            remove_stopped(client, config.remove_stopped, stopped_first_seen, dry_run)

    if config.set_seed_limits.enabled:
        with bound_contextvars(job="set_seed_limits"):
            set_seed_limits(client, config, dry_run)

    if config.maintain_free_space.enabled:
        with bound_contextvars(job="maintain_free_space"):
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
    warn_category_overlaps(config)

    client = build_client(config.qbittorrent)

    if args.dry_run:
        log.info("dry-run mode: no torrents will be deleted")

    unregistered_first_seen: dict[str, datetime.datetime] = {}
    stopped_first_seen: dict[str, datetime.datetime] = {}

    if not args.daemon:
        run(
            client,
            config,
            unregistered_first_seen,
            dry_run=args.dry_run,
            stopped_first_seen=stopped_first_seen,
        )
        return

    with bound_contextvars(interval_seconds=config.interval_seconds):
        log.info("running in daemon mode")

    while True:
        try:
            run(
                client,
                config,
                unregistered_first_seen,
                dry_run=args.dry_run,
                stopped_first_seen=stopped_first_seen,
            )
        except Exception:
            log.exception("run failed")

        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
