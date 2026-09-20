"""Validated values shared by the registry, storage, and MCP layers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

import yaml

MAX_FILE_BYTES = 8 * 1024**2
MAX_SKILL_BYTES = 32 * 1024**2
MAX_STORE_BYTES = 256 * 1024**2
MAX_FILES_PER_SKILL = 256
MAX_SKILLS_PER_STORE = 1024
SCAN_TIMEOUT = 60.0
GIT_TIMEOUT = 15.0
MASK = "********"


class SkillStoreError(ValueError):
    """An error whose message is safe to expose to a client."""


class BackendError(SkillStoreError):
    """A safe storage failure."""


class BackendCommittedError(BackendError):
    """The public namespace changed, but durability or cleanup is uncertain."""

    def __init__(self, operation: str) -> None:
        if operation not in {"write", "delete"}:
            raise ValueError("Invalid committed operation.")
        self.operation = operation
        super().__init__(
            "The storage change committed, but durability or cleanup could not be confirmed."
        )


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", name):
        raise SkillStoreError("Invalid store name.")
    return name


def validate_skill_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise SkillStoreError("Invalid skill name.")
    return name


def validate_relative_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise SkillStoreError("Invalid relative file path.")
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        raise SkillStoreError("Invalid relative file path.") from None
    if size > 1024:
        raise SkillStoreError("Invalid relative file path.")
    if "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise SkillStoreError("Invalid relative file path.")
    if unicodedata.normalize("NFC", path) != path:
        raise SkillStoreError("File paths must use normalized Unicode.")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or len(part.encode("utf-8")) > 255 for part in parts):
        raise SkillStoreError("Invalid relative file path.")
    return path


def validate_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(files, Mapping) or not files or len(files) > MAX_FILES_PER_SKILL:
        raise SkillStoreError("Invalid skill file count.")
    result: dict[str, bytes] = {}
    normalized: set[str] = set()
    directories: dict[str, str] = {}
    total = 0
    for path, content in files.items():
        validate_relative_path(path)
        key = path.casefold()
        if key in normalized:
            raise SkillStoreError("Duplicate normalized file path.")
        normalized.add(key)
        parts = path.split("/")
        for index in range(1, len(parts)):
            directory = "/".join(parts[:index])
            canonical = directory.casefold()
            if canonical in directories and directories[canonical] != directory:
                raise SkillStoreError("Duplicate normalized directory path.")
            directories[canonical] = directory
        if not isinstance(content, bytes) or len(content) > MAX_FILE_BYTES:
            raise SkillStoreError("Invalid file content or file size limit exceeded.")
        total += len(content)
        result[path] = content
    if total > MAX_SKILL_BYTES:
        raise SkillStoreError("Skill size limit exceeded.")
    for key in normalized:
        parts = key.split("/")
        if any("/".join(parts[:index]) in normalized for index in range(1, len(parts))):
            raise SkillStoreError("A file conflicts with a directory path.")
    if "SKILL.md" not in result:
        raise SkillStoreError("A skill must contain SKILL.md.")
    try:
        result["SKILL.md"].decode("utf-8")
    except UnicodeDecodeError:
        raise SkillStoreError("SKILL.md must be UTF-8 text.") from None
    return result


def _validate_url(value: str, *, git: bool = False) -> None:
    if not value or any(ord(char) < 32 for char in value):
        raise SkillStoreError("Invalid store URL.")
    if git and Path(value).is_absolute():
        return
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise SkillStoreError("Invalid store URL.") from None
    schemes = {"http", "https", "ssh", "file"} if git else {"http", "https"}
    if parsed.scheme not in schemes or parsed.query or parsed.fragment:
        raise SkillStoreError("Invalid store URL.")
    if parsed.scheme != "file" and not parsed.hostname:
        raise SkillStoreError("Invalid store URL.")
    if not git and (parsed.username is not None or parsed.password is not None):
        raise SkillStoreError("Endpoint credentials must use the credential fields.")


def mask_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.username is None and parsed.password is None:
            return value
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        return urlunsplit((parsed.scheme, MASK + "@" + host, parsed.path, "", ""))
    except ValueError:
        return MASK


@dataclass(frozen=True)
class StoreConfig:
    name: str
    kind: str
    path: str = ""
    url: str = field(default="", repr=False)
    ref: str = "HEAD"
    credential: str = field(default="", repr=False)
    endpoint: str = ""
    bucket: str = ""
    prefix: str = ""
    access_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    region: str = "us-east-1"

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) for value in asdict(self).values()):
            raise SkillStoreError("Store fields must be strings.")
        validate_name(self.name)
        if self.kind not in {"directory", "git", "s3"}:
            raise SkillStoreError("Unknown store kind.")
        if self.kind == "directory" and (
            not Path(self.path).is_absolute() or any(ord(char) < 32 for char in self.path)
        ):
            raise SkillStoreError("Directory stores require an absolute path.")
        if self.kind == "git":
            _validate_url(self.url, git=True)
            if (
                not self.ref
                or self.ref.startswith("-")
                or any(char.isspace() or ord(char) < 32 for char in self.ref)
            ):
                raise SkillStoreError("Invalid Git reference.")
        if self.kind == "s3":
            if self.endpoint:
                _validate_url(self.endpoint)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,62}", self.bucket):
                raise SkillStoreError("Invalid S3 bucket.")
            if self.prefix:
                validate_relative_path(self.prefix.rstrip("/"))
                object.__setattr__(self, "prefix", self.prefix.rstrip("/"))
            if bool(self.access_key) != bool(self.secret_key):
                raise SkillStoreError("Both S3 credential fields are required.")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def masked(self) -> dict[str, str]:
        result = self.to_dict()
        for key in ("credential", "access_key", "secret_key"):
            if result[key]:
                result[key] = MASK
        result["url"] = mask_url(result["url"])
        result["endpoint"] = mask_url(result["endpoint"])
        return result


def _front_matter_metadata(lines: list[str]) -> object:
    """Accept one legacy plain description without evaluating its YAML syntax."""
    try:
        return yaml.safe_load("\n".join(lines))
    except yaml.scanner.ScannerError as error:
        candidates = [
            (index, match.group(1))
            for index, line in enumerate(lines)
            if (match := re.fullmatch(r"description:[ \t]+(\S.*)", line))
        ]
        if len(candidates) != 1 or error.problem != "mapping values are not allowed here":
            raise
        index, description = candidates[0]
        mark = error.problem_mark
        if (
            mark is None
            or mark.line != index
            or lines[index][mark.column : mark.column + 2] not in {": ", ":\t"}
            or description[0] in "\"'[]{}|>!&*#%@`"
            or description.startswith(("- ", "? ", ": "))
        ):
            raise
        repaired = list(lines)
        repaired[index] = 'description: ""'
        metadata = yaml.safe_load("\n".join(repaired))
        if not isinstance(metadata, dict) or metadata.get("description") != "":
            raise SkillStoreError("Invalid skill front matter.")
        metadata["description"] = description
        return metadata


@dataclass(frozen=True)
class Skill:
    store: str
    name: str
    files: Mapping[str, bytes]
    body: str = field(init=False)
    description: str = field(init=False)

    def __post_init__(self) -> None:
        validate_name(self.store)
        validate_skill_name(self.name)
        files = validate_files(self.files)
        body = files["SKILL.md"].decode("utf-8")
        description = ""
        if body.startswith("---\n") or body.startswith("---\r\n"):
            parts = body.splitlines()
            try:
                end = parts.index("---", 1)
                metadata = _front_matter_metadata(parts[1:end])
            except (ValueError, yaml.YAMLError):
                raise SkillStoreError("Invalid skill front matter.") from None
            if isinstance(metadata, dict):
                value = metadata.get("description", "")
                if not isinstance(value, str):
                    raise SkillStoreError("Skill description must be text.")
                description = " ".join(value.split())[:1024]
        object.__setattr__(self, "files", MappingProxyType(files))
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "description", description)

    @property
    def qualified_name(self) -> str:
        return self.store + "/" + self.name
