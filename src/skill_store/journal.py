"""Durable migration intent owned by the SkillStore mutation boundary."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .models import (
    SkillStoreError,
    validate_files,
    validate_name,
    validate_relative_path,
    validate_skill_name,
)


class JournalCommittedError(SkillStoreError):
    """The journal name changed, but parent durability is uncertain."""


@dataclass(frozen=True)
class MigrationIntent:
    from_store: str
    to_store: str
    skill: str
    phase: str
    files: tuple[tuple[str, int, str], ...]

    @classmethod
    def create(
        cls, from_store: str, to_store: str, skill: str, files: Mapping[str, bytes]
    ) -> "MigrationIntent":
        content = validate_files(files)
        return cls(
            validate_name(from_store),
            validate_name(to_store),
            validate_skill_name(skill),
            "copying",
            tuple(
                (path, len(value), hashlib.sha256(value).hexdigest())
                for path, value in sorted(content.items())
            ),
        )

    def deleting(self) -> "MigrationIntent":
        return replace(self, phase="deleting")

    def matches(self, from_store: str, to_store: str, skill: str) -> bool:
        return (self.from_store, self.to_store, self.skill) == (
            from_store,
            to_store,
            skill,
        )

    def verifies(self, files: Mapping[str, bytes]) -> bool:
        try:
            candidate = validate_files(files)
        except SkillStoreError:
            return False
        actual = tuple(
            (path, len(value), hashlib.sha256(value).hexdigest())
            for path, value in sorted(candidate.items())
        )
        return actual == self.files

    def payload(self) -> dict:
        return {
            "version": 1,
            "from_store": self.from_store,
            "to_store": self.to_store,
            "skill": self.skill,
            "phase": self.phase,
            "files": [
                {"path": path, "size": size, "sha256": digest} for path, size, digest in self.files
            ],
        }


class MigrationJournal:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "migration.json"
        self.state_dir = state_dir

    def load(self) -> MigrationIntent | None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise SkillStoreError("Invalid migration journal.")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                payload = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise SkillStoreError("Invalid migration journal.") from None
        finally:
            if fd >= 0:
                os.close(fd)
        try:
            if (
                set(payload)
                != {
                    "version",
                    "from_store",
                    "to_store",
                    "skill",
                    "phase",
                    "files",
                }
                or payload["version"] != 1
            ):
                raise ValueError
            if payload["phase"] not in {"copying", "deleting"}:
                raise ValueError
            files = []
            for entry in payload["files"]:
                if set(entry) != {"path", "size", "sha256"}:
                    raise ValueError
                path = validate_relative_path(entry["path"])
                size, digest = entry["size"], entry["sha256"]
                if type(size) is not int or size < 0 or not isinstance(digest, str):
                    raise ValueError
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ValueError
                files.append((path, size, digest))
            if not files or len({path.casefold() for path, _, _ in files}) != len(files):
                raise ValueError
            intent = MigrationIntent(
                validate_name(payload["from_store"]),
                validate_name(payload["to_store"]),
                validate_skill_name(payload["skill"]),
                payload["phase"],
                tuple(files),
            )
            if not any(path == "SKILL.md" for path, _, _ in intent.files):
                raise ValueError
            return intent
        except (KeyError, TypeError, ValueError, SkillStoreError):
            raise SkillStoreError("Invalid migration journal.") from None

    def save(self, intent: MigrationIntent) -> None:
        payload = json.dumps(intent.payload(), sort_keys=True, separators=(",", ":")).encode()
        fd = -1
        staged = ""
        installed = False
        try:
            fd, staged = tempfile.mkstemp(prefix=".migration-", dir=self.state_dir)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.path)
            installed = True
            self._sync_parent()
        except OSError:
            if installed:
                raise JournalCommittedError(
                    "The migration journal committed with uncertain durability."
                ) from None
            raise SkillStoreError("Cannot save the migration journal.") from None
        finally:
            if fd >= 0:
                os.close(fd)
            if staged and not installed:
                try:
                    os.unlink(staged)
                except FileNotFoundError:
                    pass

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            raise SkillStoreError("Cannot clear the migration journal.") from None
        try:
            self._sync_parent()
        except OSError:
            raise SkillStoreError("Cannot clear the migration journal.") from None

    def _sync_parent(self) -> None:
        fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
