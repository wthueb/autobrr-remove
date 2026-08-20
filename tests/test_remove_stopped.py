import datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
import qbittorrentapi
from pydantic import ValidationError
from structlog.contextvars import get_contextvars

from tarr.config import Config, RemoveStoppedConfig
from tarr.main import remove_stopped, run


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents
        self.status_filters = []

    def torrents_info(self, status_filter=None):
        self.status_filters.append(status_filter)
        return self.torrents


def make_torrent(category="movies", state="stoppedUP"):
    return SimpleNamespace(
        hash="1234567890abcdef",
        name="completed torrent",
        state=state,
        size=1024**3,
        category=category,
        delete=Mock(),
    )


def test_first_observation_starts_delay_without_removing():
    torrent = make_torrent()
    client = FakeClient([torrent])
    first_seen = {}

    remove_stopped(client, RemoveStoppedConfig(delay_minutes=10), first_seen)

    assert client.status_filters == ["completed"]
    assert torrent.hash in first_seen
    torrent.delete.assert_not_called()


def test_zero_delay_removes_on_first_observation():
    torrent = make_torrent()
    client = FakeClient([torrent])
    first_seen = {}

    remove_stopped(client, RemoveStoppedConfig(), first_seen)

    torrent.delete.assert_called_once_with(delete_files=False)
    assert torrent.hash not in first_seen


@pytest.mark.parametrize("state", ["uploading", "stalledUP", "forcedUP", "queuedUP"])
def test_seeding_torrent_is_not_tracked_or_removed(state):
    torrent = make_torrent(state=state)
    client = FakeClient([torrent])
    first_seen = {}

    remove_stopped(client, RemoveStoppedConfig(), first_seen)

    assert first_seen == {}
    torrent.delete.assert_not_called()


def test_resumed_seeding_torrent_resets_stopped_delay():
    torrent = make_torrent(state="uploading")
    client = FakeClient([torrent])
    first_seen = {torrent.hash: datetime.datetime.now() - datetime.timedelta(minutes=11)}

    remove_stopped(client, RemoveStoppedConfig(delay_minutes=10), first_seen)

    assert first_seen == {}
    torrent.delete.assert_not_called()


@pytest.mark.parametrize("state", ["pausedUP", "stoppedUP"])
def test_qbittorrent_stopped_states_are_removed(state):
    torrent = make_torrent(state=state)
    client = FakeClient([torrent])
    first_seen = {}

    remove_stopped(client, RemoveStoppedConfig(), first_seen)

    torrent.delete.assert_called_once_with(delete_files=False)


@pytest.mark.parametrize(
    ("on_delete", "delete_files"),
    [("Remove", False), ("RemoveWithContent", True)],
)
def test_removes_after_delay_with_configured_delete_action(on_delete, delete_files):
    torrent = make_torrent()
    client = FakeClient([torrent])
    first_seen = {torrent.hash: datetime.datetime.now() - datetime.timedelta(minutes=11)}
    config = RemoveStoppedConfig(delay_minutes=10, on_delete=on_delete)

    remove_stopped(client, config, first_seen)

    torrent.delete.assert_called_once_with(delete_files=delete_files)
    assert torrent.hash not in first_seen


def test_dry_run_does_not_remove_torrent_or_tracking_state():
    torrent = make_torrent()
    client = FakeClient([torrent])
    observed_at = datetime.datetime.now() - datetime.timedelta(minutes=11)
    first_seen = {torrent.hash: observed_at}

    remove_stopped(
        client,
        RemoveStoppedConfig(delay_minutes=10, on_delete="RemoveWithContent"),
        first_seen,
        dry_run=True,
    )

    torrent.delete.assert_not_called()
    assert first_seen == {torrent.hash: observed_at}


def test_category_filters_apply_before_tracking():
    torrent = make_torrent(category="upload")
    client = FakeClient([torrent])
    first_seen = {}
    config = RemoveStoppedConfig(categories=["movies"], ignore_categories=["upload"])

    remove_stopped(client, config, first_seen)

    assert first_seen == {}
    torrent.delete.assert_not_called()


def test_torrent_no_longer_stopped_is_removed_from_tracking():
    client = FakeClient([])
    first_seen = {"1234567890abcdef": datetime.datetime.now()}

    remove_stopped(client, RemoveStoppedConfig(delay_minutes=10), first_seen)

    assert first_seen == {}


@pytest.mark.parametrize("on_delete", ["Default", "Stop", "EnableSuperSeeding"])
def test_on_delete_rejects_unsupported_actions(on_delete):
    with pytest.raises(ValidationError):
        RemoveStoppedConfig(on_delete=on_delete)


def test_run_binds_job_and_torrent_context_during_removal():
    torrent = make_torrent()
    observed_context = {}
    torrent.delete.side_effect = lambda **_kwargs: observed_context.update(get_contextvars())
    client = FakeClient([torrent])
    config = Config.model_validate(
        {
            "qbittorrent": {
                "host": "localhost",
                "username": "user",
                "password": "pass",
                "remove_stopped": {"enabled": True},
            },
            "trackers": [
                {
                    "name": "example",
                    "hosts": ["tracker.example.com"],
                    "seed_time_minutes": 1,
                    "ratio": 1,
                }
            ],
        }
    )

    run(cast(qbittorrentapi.Client, client), config, {})

    assert observed_context == {
        "delete_files": False,
        "dry_run": False,
        "job": "remove_stopped",
        "on_delete": "Remove",
        "stopped_for_seconds": 0.0,
        "torrent": torrent.hash,
        "torrent_name": torrent.name,
        "torrent_size_bytes": torrent.size,
        "torrent_state": torrent.state,
    }
    assert get_contextvars() == {}
