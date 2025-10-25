import argparse
import datetime
import logging
import os
import sys
import time

import qbittorrentapi

try:
    FREE_SPACE_THRESHOLD = int(os.environ["FREE_SPACE_THRESHOLD_GIBI"]) * 1024**3
except KeyError:
    raise RuntimeError("FREE_SPACE_THRESHOLD_GIBI environment variable is required")

try:
    QBITTORRENT_HOST = os.environ["QBITTORRENT_HOST"]
except KeyError:
    raise RuntimeError("QBITTORRENT_HOST environment variable is required")

try:
    QBITTORRENT_USERNAME = os.environ["QBITTORRENT_USERNAME"]
except KeyError:
    raise RuntimeError("QBITTORRENT_USERNAME environment variable is required")

try:
    QBITTORRENT_PASSWORD = os.environ["QBITTORRENT_PASSWORD"]
except KeyError:
    raise RuntimeError("QBITTORRENT_PASSWORD environment variable is required")

try:
    INTERVAL_SECONDS = int(os.environ["INTERVAL_SECONDS"])
except KeyError:
    raise RuntimeError("INTERVAL_SECONDS environment variable is required")

log = logging.getLogger(__name__)

client = qbittorrentapi.Client(
    host=QBITTORRENT_HOST,
    username=QBITTORRENT_USERNAME,
    password=QBITTORRENT_PASSWORD,
)

try:
    client.auth_log_in()
except qbittorrentapi.LoginFailed as e:
    log.warning(f"failed to connect to qBittorrent: {e}")


def remove_unregistered():
    log.debug("checking for unregistered torrents...")

    torrents = client.torrents_info(category="autobrr")

    for torrent in torrents:
        trackers = torrent.trackers

        for tracker in trackers:
            # https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)#get-torrent-trackers
            if tracker.status in [0, 1, 3]:
                continue

            log.debug(f"{torrent.hash[-6:]}: {torrent.name=} {tracker=}")

            if tracker.msg == "unregistered torrent":
                log.info(
                    f"removing unregistered torrent {torrent.hash[-6:]}: {torrent.name=} {torrent.state=} {torrent.size / 1024**3:.3f} GiB"
                )
                torrent.delete(delete_files=True)
                break


def run():
    log.debug("starting removal run...")

    remove_unregistered()

    torrents = client.torrents_info(category="autobrr")
    free_space = client.sync_maindata().server_state.free_space_on_disk

    if free_space > FREE_SPACE_THRESHOLD:
        log.info(
            f"{free_space / 1024**4:.3f} TiB free, nothing to do ({FREE_SPACE_THRESHOLD / 1024**4} TiB threshold)"
        )
        return

    possible_to_remove: list[qbittorrentapi.TorrentDictionary] = []

    for torrent in torrents:
        log.debug(
            f"checking {torrent.hash[-6:]}: {torrent.name} ({torrent.state}) size={torrent.size / 1024**3:.2f} GiB uploaded={torrent.uploaded / 1024**3:.2f} GiB ({torrent.ratio:.2f}) seeding_time={torrent.seeding_time} ({torrent.uploaded / torrent.seeding_time if torrent.seeding_time > 0 else 0} B/s)"
        )

        seeding_time = datetime.timedelta(seconds=torrent.seeding_time)

        if seeding_time <= datetime.timedelta(days=8) and torrent.ratio < 1.0:
            log.debug("skipping since seeding time and ratio do not meet minimums")
            continue

        possible_to_remove.append(torrent)

    possible_to_remove.sort(key=lambda t: t.uploaded / t.seeding_time)

    log.debug(f"found {len(possible_to_remove)} torrents that satisfy removal criteria")

    while possible_to_remove and free_space < FREE_SPACE_THRESHOLD:
        torrent = possible_to_remove.pop(0)
        log.info(
            f"removing {torrent.hash[-6:]}: {torrent.name} ({torrent.state}) size={torrent.size / 1024**3:.2f} GiB uploaded={torrent.uploaded / 1024**3:.2f} GiB ({torrent.ratio:.2f}) seeding_time={torrent.seeding_time} ({torrent.uploaded / torrent.seeding_time if torrent.seeding_time > 0 else 0} B/s)"
        )
        torrent.delete(delete_files=True)
        free_space += torrent.size


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", logging.INFO),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser()

    parser.add_argument("-d", "--daemon", action="store_true")

    args = parser.parse_args()

    if not args.daemon:
        run()
        return

    log.info(f"running in daemon mode, checking every {INTERVAL_SECONDS} seconds...")

    while True:
        try:
            run()
        except Exception as e:
            log.error(f"error during run: {e}", exc_info=True)

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
