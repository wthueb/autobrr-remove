from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterable
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class QBittorrentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    username: str
    password: str


class TrackerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    # announce hostnames that identify this tracker (e.g. tracker.example.org)
    hosts: list[str] = Field(min_length=1)
    seed_time_minutes: int = Field(ge=0)
    ratio: float = Field(default=1.0, ge=0)

    def matches(self, hostname: str) -> bool:
        hostname = hostname.lower()
        for host in self.hosts:
            host = host.lower()
            if hostname == host or hostname.endswith("." + host):
                return True
        return False


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    file: pathlib.Path | None = None
    file_count: int = Field(default=20, ge=1)

    @field_validator("level")
    @classmethod
    def validate_level(cls, value: str) -> str:
        value = value.upper()
        if value not in logging.getLevelNamesMapping():
            raise ValueError(f"unknown log level {value!r}")
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qbittorrent: QBittorrentConfig
    trackers: list[TrackerConfig] = Field(min_length=1)
    # only torrents in one of these categories are considered; null to consider all
    categories: list[str] | None = ["autobrr"]
    free_space_threshold_gibi: int = Field(ge=0)
    remove_unregistered_delay_minutes: int = Field(default=0, ge=0)
    interval_seconds: int = Field(default=60, ge=1)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def free_space_threshold_bytes(self) -> int:
        return self.free_space_threshold_gibi * 1024**3

    def match_tracker(self, tracker_urls: Iterable[str]) -> TrackerConfig | None:
        """Return the first configured tracker matching any of the torrent's tracker URLs."""
        hostnames = [h for url in tracker_urls if (h := urlparse(url).hostname)]

        for tracker in self.trackers:
            if any(tracker.matches(hostname) for hostname in hostnames):
                return tracker

        return None


def load_config(path: pathlib.Path) -> Config:
    with path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} is empty or not a mapping")

    return Config.model_validate(raw)
