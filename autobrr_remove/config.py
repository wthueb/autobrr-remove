from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterable
from typing import Annotated, Literal
from urllib.parse import urlparse

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

# a seed-time/ratio limit of -1 means "unlimited" (qBittorrent's own convention: -1 is no limit)
UNLIMITED = -1


def _check_limit(value: float) -> float:
    if value != UNLIMITED and value < 0:
        raise ValueError(f"must be a non-negative number, or {UNLIMITED} for unlimited")
    return value


# a number of minutes, or -1 for unlimited
SeedTimeMinutes = Annotated[int, AfterValidator(_check_limit)]
# a share ratio, or -1 for unlimited
Ratio = Annotated[float, AfterValidator(_check_limit)]


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
    seed_time_minutes: SeedTimeMinutes
    ratio: Ratio

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


class RemoveUnregisteredConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # wait this long after a tracker first reports a torrent as "unregistered"
    # before deleting it (some trackers report it transiently)
    delay_minutes: int = Field(default=0, ge=0)


class MaintainFreeSpaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # only torrents in one of these categories are considered; null to consider all
    categories: list[str] | None = None
    free_space_threshold_gibi: int | None = Field(default=None, ge=0)

    @property
    def free_space_threshold_bytes(self) -> int:
        return (self.free_space_threshold_gibi or 0) * 1024**3

    @model_validator(mode="after")
    def _require_threshold(self) -> MaintainFreeSpaceConfig:
        if self.enabled and self.free_space_threshold_gibi is None:
            raise ValueError(
                "free_space_threshold_gibi is required when maintain_free_space is enabled"
            )
        return self


class SetSeedLimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # only torrents in one of these categories are considered; null to consider all
    categories: list[str] | None = None
    # applied when a torrent's tracker is not configured under `trackers`; both must
    # be non-null for the fallback to apply, otherwise such torrents are left untouched.
    # use -1 for unlimited (as with trackers).
    default_seed_time_minutes: SeedTimeMinutes | None = None
    default_ratio: Ratio | None = None
    # qBittorrent shareLimitAction applied when a share limit is reached
    on_delete: Literal["Default", "Remove", "RemoveWithContent", "Stop"] = "Default"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qbittorrent: QBittorrentConfig
    trackers: list[TrackerConfig] = Field(min_length=1)
    interval_seconds: int = Field(default=60, ge=1)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    remove_unregistered: RemoveUnregisteredConfig = Field(default_factory=RemoveUnregisteredConfig)
    maintain_free_space: MaintainFreeSpaceConfig = Field(default_factory=MaintainFreeSpaceConfig)
    set_seed_limits: SetSeedLimitsConfig = Field(default_factory=SetSeedLimitsConfig)

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
