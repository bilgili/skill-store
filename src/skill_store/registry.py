"""Durable configuration and exclusive ownership of one state directory."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .models import SkillStoreError, StoreConfig


class Registry:
    """Commit configuration before publishing its immutable memory view."""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir)
        self.stores: Mapping[str, StoreConfig] = MappingProxyType({})
        self.writable_store: str | None = None
        self._lock_fd: int | None = None

    def open(self) -> None:
        if self._lock_fd is not None:
            raise SkillStoreError("The state directory is already open.")
        try:
            if not self.path.is_dir() or self.path.is_symlink():
                raise OSError("Invalid state directory")
            # A probe tests write permission for the actual process.
            probe, probe_name = tempfile.mkstemp(prefix=".probe-", dir=self.path)
            os.close(probe)
            os.unlink(probe_name)
            fd = os.open(
                self.path / ".skill-store.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("Invalid state lock")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(fd)
                raise
            self._lock_fd = fd
            self._load()
        except BaseException as exc:
            self.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise SkillStoreError("Cannot open the state directory or registry.") from None

    def close(self) -> None:
        if self._lock_fd is not None:
            fd, self._lock_fd = self._lock_fd, None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _validate(stores: Mapping[str, StoreConfig], writable: str | None) -> None:
        if len(stores) > 128:
            raise SkillStoreError("The registry exceeds the 128-store limit.")
        if any(name != config.name for name, config in stores.items()):
            raise SkillStoreError("Invalid store registry.")
        if writable is not None:
            if writable not in stores:
                raise SkillStoreError("The writable store does not exist.")
            if stores[writable].kind not in {"directory", "s3"}:
                raise SkillStoreError("The writable store must support writes.")

    def _load(self) -> None:
        location = self.path / "stores.json"
        try:
            fd = os.open(location, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            self.save({}, None)
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise SkillStoreError("Invalid store registry.")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                payload = json.load(stream)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"version", "stores", "writable_store"}
                or type(payload["version"]) is not int
                or payload["version"] != 1
                or not isinstance(payload["stores"], list)
                or len(payload["stores"]) > 128
            ):
                raise SkillStoreError("Invalid store registry.")
            configs = [StoreConfig(**entry) for entry in payload["stores"]]
            stores = {config.name: config for config in configs}
            writable = payload["writable_store"]
            if len(stores) != len(configs) or (
                writable is not None and not isinstance(writable, str)
            ):
                raise SkillStoreError("Invalid store registry.")
            self._validate(stores, writable)
            self.stores = MappingProxyType(stores)
            self.writable_store = writable
        finally:
            if fd >= 0:
                os.close(fd)

    def save(self, stores: Mapping[str, StoreConfig], writable: str | None) -> None:
        if self._lock_fd is None:
            raise SkillStoreError("The state directory is not open.")
        self._validate(stores, writable)
        payload = json.dumps(
            {
                "version": 1,
                "stores": [config.to_dict() for config in stores.values()],
                "writable_store": writable,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise SkillStoreError("The registry exceeds the size limit.")
        fd, staged = tempfile.mkstemp(prefix=".stores-", dir=self.path)
        installed = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.path / "stores.json")
            installed = True
            directory_fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.stores = MappingProxyType(dict(stores))
            self.writable_store = writable
        except OSError:
            if installed:
                # The committed name changed even if durability confirmation failed.
                self.stores = MappingProxyType(dict(stores))
                self.writable_store = writable
            raise SkillStoreError("Cannot save the store registry.") from None
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(staged)
            except FileNotFoundError:
                pass
