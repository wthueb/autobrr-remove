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


def category_is_included(
    category: str | None,
    categories: list[str | None] | None,
    ignore_categories: list[str | None],
) -> bool:
    """Return whether a qBittorrent category passes include/exclude filters."""
    # qBittorrent reports "" for torrents without a category, configured here as null.
    category = category or None
    return (categories is None or category in categories) and category not in ignore_categories


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


class CategoryFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # if set, only torrents in these categories are considered; a null entry
    # represents torrents without a category
    categories: list[str | None] | None = None
    # torrents in these categories are never considered; a null entry represents
    # torrents without a category
    ignore_categories: list[str | None] = Field(default_factory=list)

    def includes_category(self, category: str | None) -> bool:
        return category_is_included(category, self.categories, self.ignore_categories)

    @property
    def overlapping_categories(self) -> list[str | None]:
        if self.categories is None:
            return []

        # Preserve the configured order while suppressing duplicate warnings.
        return list(dict.fromkeys(c for c in self.categories if c in self.ignore_categories))


class RemoveUnregisteredConfig(CategoryFilterConfig):
    # wait this long after a tracker first reports a torrent as "unregistered"
    # before deleting it (some trackers report it transiently)
    delay_minutes: int = Field(default=0, ge=0)


class RemoveStoppedConfig(CategoryFilterConfig):
    # wait this long after first observing a torrent as completed and stopped before deleting it
    delay_minutes: int = Field(default=0, ge=0)
    # Remove keeps downloaded content; RemoveWithContent deletes it
    on_delete: Literal["Remove", "RemoveWithContent"] = "Remove"


class MaintainFreeSpaceConfig(CategoryFilterConfig):
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


class SetSeedLimitsConfig(CategoryFilterConfig):
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
    remove_stopped: RemoveStoppedConfig = Field(default_factory=RemoveStoppedConfig)
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
