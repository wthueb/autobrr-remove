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

logger = logging.getLogger(__name__)

client = qbittorrentapi.Client(
    host=QBITTORRENT_HOST,
    username=QBITTORRENT_USERNAME,
    password=QBITTORRENT_PASSWORD,
)

try:
    client.auth_log_in()
except qbittorrentapi.LoginFailed as e:
    logger.warning(f"failed to connect to qBittorrent: {e}")


def remove_unregistered():
    logger.info("checking for unregistered torrents...")

    torrents = client.torrents_info(category="autobrr")

    for torrent in torrents:
        trackers = torrent.trackers

        for tracker in trackers:
            # https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)#get-torrent-trackers
            if tracker.status in [0, 1, 3]:
                continue

            logger.debug(f"{torrent.hash[-6:]}: {torrent.name=} {tracker=}")

            if tracker.msg == "unregistered torrent":
                logger.info(
                    f"removing unregistered torrent {torrent.hash[-6:]}: {torrent.name=} {torrent.state=} {torrent.size / 1024**3:.3f} GiB"
                )
                torrent.delete(delete_files=True)
                break


def run():
    remove_unregistered()

    torrents = client.torrents_info(category="autobrr")
    free_space = client.sync_maindata().server_state.free_space_on_disk

    if free_space > FREE_SPACE_THRESHOLD:
        logger.info(
            f"{free_space / 1024**4:.3f} TiB free, nothing to do ({FREE_SPACE_THRESHOLD / 1024**4} TiB threshold)"
        )
        return

    possible_to_remove: list[qbittorrentapi.TorrentDictionary] = []

    for torrent in torrents:
        logger.debug(
            f"{torrent.hash[-6:]}: {torrent.name=} {torrent.state=} {torrent.ratio=} {torrent.seeding_time=} torrent.size={torrent.size / 1024**3:.3f} GiB"
        )

        seeding_time = datetime.timedelta(seconds=torrent.seeding_time)

        if seeding_time <= datetime.timedelta(days=8) and torrent.ratio < 1.0:
            logger.debug("skipping since seeding time and ratio do not meet minimums")
            continue

        possible_to_remove.append(torrent)

    possible_to_remove.sort(key=lambda t: t.popularity)

    while possible_to_remove and free_space < FREE_SPACE_THRESHOLD:
        torrent = possible_to_remove.pop(0)
        logger.info(
            f"removing torrent {torrent.hash[-6:]}: {torrent.name=} {torrent.state=} {torrent.ratio=} {torrent.seeding_time=} torrent.size={torrent.size / 1024**3:.3f} GiB"
        )
        torrent.delete(delete_files=True)
        free_space += torrent.size


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", logging.INFO),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout,
    )

    while True:
        try:
            run()
        except Exception as e:
            logger.error(f"error during run: {e}", exc_info=True)

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
