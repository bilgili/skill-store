"""Synchronous storage adapters. The SkillStore owner serializes mutations."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .models import (
    GIT_TIMEOUT,
    MAX_FILE_BYTES,
    MAX_FILES_PER_SKILL,
    MAX_SKILL_BYTES,
    MAX_SKILLS_PER_STORE,
    MAX_STORE_BYTES,
    SCAN_TIMEOUT,
    BackendError,
    StoreConfig,
    validate_files,
    validate_relative_path,
    validate_skill_name,
)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_IO_ERRORS = (OSError, BotoCoreError, ClientError)
_MAX_OBJECTS = MAX_SKILLS_PER_STORE * MAX_FILES_PER_SKILL


class _Budget:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + SCAN_TIMEOUT
        self.bytes = 0
        self.files = 0

    def check(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BackendError("Store operation timed out.")
        return remaining

    def account(self, count: int) -> None:
        self.check()
        self.bytes += count
        self.files += 1
        if count > MAX_FILE_BYTES or self.bytes > MAX_STORE_BYTES or self.files > _MAX_OBJECTS:
            raise BackendError("Store size limit exceeded.")


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Walk from the filesystem root without following any symbolic link."""
    if not path.is_absolute() or ".." in path.parts:
        raise BackendError("Invalid directory location.")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for index, component in enumerate(path.parts[1:]):
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create or index != len(path.parts) - 2:
                    raise
                os.mkdir(component, 0o700, dir_fd=fd)
                os.fsync(fd)
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read_regular(parent: int, name: str, budget: _Budget) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_FILE_BYTES:
            raise BackendError("Unsafe file or file size limit exceeded.")
        result = bytearray()
        while True:
            budget.check()
            data = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - len(result)))
            if not data:
                break
            result.extend(data)
            if len(result) > MAX_FILE_BYTES:
                raise BackendError("File size limit exceeded.")
        budget.account(len(result))
        return bytes(result)
    finally:
        os.close(fd)


def _tree_from_fd(fd: int, budget: _Budget, prefix: str = "") -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for name in sorted(os.listdir(fd)):
        budget.check()
        path = prefix + name
        validate_relative_path(path)
        details = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode):
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            try:
                result.update(_tree_from_fd(child, budget, path + "/"))
            finally:
                os.close(child)
        elif stat.S_ISREG(details.st_mode):
            result[path] = _read_regular(fd, name, budget)
        else:
            raise BackendError("Symbolic links and special files are not supported.")
        if len(result) > MAX_FILES_PER_SKILL or sum(map(len, result.values())) > MAX_SKILL_BYTES:
            raise BackendError("Skill size limit exceeded.")
    return result


def _atomic_rename(parent: int, source: str, target: str, *, exchange: bool) -> None:
    """Use a native operation; never emulate exchange with two renames."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        flags = 2 if exchange else 4  # RENAME_SWAP, RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        flags = 2 if exchange else 1  # RENAME_EXCHANGE, RENAME_NOREPLACE
    else:
        rename = None
        flags = 0
    if rename is None:
        raise BackendError("Atomic directory publication is unavailable.")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    result = rename(parent, os.fsencode(source), parent, os.fsencode(target), flags)
    if result != 0:
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
            raise BackendError("Atomic directory publication is unavailable.")
        raise OSError(code, "Atomic directory publication failed.")


def _write_staged(fd: int, files: Mapping[str, bytes], budget: _Budget) -> None:
    for path, content in files.items():
        budget.check()
        parts = path.split("/")
        parent = os.dup(fd)
        try:
            for component in parts[:-1]:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(component, _DIR_FLAGS, dir_fd=parent)
                os.close(parent)
                parent = child
            output = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                view = memoryview(content)
                while view:
                    budget.check()
                    count = os.write(output, view)
                    view = view[count:]
                os.fsync(output)
            finally:
                os.close(output)
        finally:
            os.close(parent)

    def sync_tree(directory: int) -> None:
        for name in os.listdir(directory):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _DIR_FLAGS, dir_fd=directory)
                try:
                    sync_tree(child)
                finally:
                    os.close(child)
        os.fsync(directory)

    sync_tree(fd)


class Backend:
    writable_capable = False

    def __init__(self, config: StoreConfig) -> None:
        self.config = config

    def list_skills(self) -> dict[str, dict[str, bytes]]:
        raise NotImplementedError

    def read_file(self, skill: str, path: str) -> bytes:
        validate_skill_name(skill)
        validate_relative_path(path)
        try:
            return self.list_skills()[skill][path]
        except KeyError:
            raise BackendError("Skill file not found.") from None

    def write_tree(self, skill: str, files: Mapping[str, bytes], replace: bool = False) -> None:
        raise BackendError("This store is read-only.")

    def delete_skill(self, skill: str) -> None:
        raise BackendError("This store is read-only.")

    def verify_tree(self, skill: str, files: Mapping[str, bytes]) -> None:
        expected = validate_files(files)
        if self.list_skills().get(skill) != expected:
            raise BackendError("Skill verification failed.")

    def refresh(self) -> None:
        return None


class DirectoryBackend(Backend):
    writable_capable = True

    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        self.root = Path(config.path)
        try:
            with _directory(self.root, create=True):
                pass
        except OSError:
            raise BackendError("Directory store is unavailable or unsafe.") from None

    def list_skills(self) -> dict[str, dict[str, bytes]]:
        budget = _Budget()
        result = {}
        try:
            with _directory(self.root) as root:
                for name in sorted(os.listdir(root)):
                    budget.check()
                    if name.startswith("."):
                        continue
                    info = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or not (
                        stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                    ):
                        raise BackendError("Symbolic links and special files are not supported.")
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    validate_skill_name(name)
                    fd = os.open(name, _DIR_FLAGS, dir_fd=root)
                    try:
                        try:
                            manifest = os.stat("SKILL.md", dir_fd=fd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(manifest.st_mode):
                            raise BackendError("SKILL.md must be a regular file.")
                        result[name] = validate_files(_tree_from_fd(fd, budget))
                    finally:
                        os.close(fd)
                    if len(result) > MAX_SKILLS_PER_STORE:
                        raise BackendError("Store skill count limit exceeded.")
        except OSError:
            raise BackendError("Directory scan failed.") from None
        return result

    def read_file(self, skill: str, path: str) -> bytes:
        validate_skill_name(skill)
        validate_relative_path(path)
        try:
            with _directory(self.root) as root:
                parent = os.open(skill, _DIR_FLAGS, dir_fd=root)
                try:
                    parts = path.split("/")
                    for component in parts[:-1]:
                        child = os.open(component, _DIR_FLAGS, dir_fd=parent)
                        os.close(parent)
                        parent = child
                    return _read_regular(parent, parts[-1], _Budget())
                finally:
                    os.close(parent)
        except OSError:
            raise BackendError("Skill file is unavailable or unsafe.") from None

    def write_tree(self, skill: str, files: Mapping[str, bytes], replace: bool = False) -> None:
        validate_skill_name(skill)
        content = validate_files(files)
        stage = ".skill-store-stage-" + uuid.uuid4().hex
        try:
            with _directory(self.root) as root:
                existing = False
                try:
                    info = os.stat(skill, dir_fd=root, follow_symlinks=False)
                    existing = True
                    if not stat.S_ISDIR(info.st_mode):
                        raise BackendError("Skill path is not a safe directory.")
                except FileNotFoundError:
                    pass
                if existing and not replace:
                    raise BackendError("Skill already exists; replacement is required.")
                os.mkdir(stage, 0o700, dir_fd=root)
                try:
                    fd = os.open(stage, _DIR_FLAGS, dir_fd=root)
                    try:
                        _write_staged(fd, content, _Budget())
                    finally:
                        os.close(fd)
                    _atomic_rename(root, stage, skill, exchange=existing)
                    os.fsync(root)
                finally:
                    try:
                        shutil.rmtree(stage, dir_fd=root)
                        os.fsync(root)
                    except OSError:
                        pass
        except OSError:
            raise BackendError("Directory write failed.") from None

    def delete_skill(self, skill: str) -> None:
        validate_skill_name(skill)
        retired = ".skill-store-deleted-" + uuid.uuid4().hex
        try:
            with _directory(self.root) as root:
                try:
                    info = os.stat(skill, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if not stat.S_ISDIR(info.st_mode):
                    raise BackendError("Skill path is not a safe directory.")
                _atomic_rename(root, skill, retired, exchange=False)
                os.fsync(root)
                try:
                    shutil.rmtree(retired, dir_fd=root)
                    os.fsync(root)
                except OSError:
                    pass
        except OSError:
            raise BackendError("Directory deletion failed.") from None

    def verify_tree(self, skill: str, files: Mapping[str, bytes]) -> None:
        validate_skill_name(skill)
        expected = validate_files(files)
        try:
            with _directory(self.root) as root:
                fd = os.open(skill, _DIR_FLAGS, dir_fd=root)
                try:
                    actual = _tree_from_fd(fd, _Budget())
                finally:
                    os.close(fd)
        except OSError:
            raise BackendError("Skill verification failed.") from None
        if actual != expected:
            raise BackendError("Skill verification failed.")


class S3Backend(Backend):
    writable_capable = True

    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        options = {
            "endpoint_url": config.endpoint or None,
            "region_name": config.region,
            "config": Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"total_max_attempts": 2, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        }
        if config.access_key:
            options.update(
                aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key
            )
        try:
            self.client = boto3.client("s3", **options)
        except _IO_ERRORS:
            raise BackendError("S3 client initialization failed.") from None
        self.prefix = config.prefix + "/" if config.prefix else ""

    def _key(self, skill: str, path: str = "") -> str:
        validate_skill_name(skill)
        if path:
            validate_relative_path(path)
        return self.prefix + skill + "/" + path

    def _keys(self, prefix: str, budget: _Budget) -> dict[str, int]:
        result: dict[str, int] = {}
        token = None
        while True:
            budget.check()
            arguments = {"Bucket": self.config.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                arguments["ContinuationToken"] = token
            page = self.client.list_objects_v2(**arguments)
            budget.check()
            for item in page.get("Contents", []):
                key = item["Key"]
                if not key.startswith(prefix):
                    raise BackendError("Invalid S3 listing.")
                result[key] = item["Size"]
                if len(result) > _MAX_OBJECTS:
                    raise BackendError("Store file count limit exceeded.")
            if not page.get("IsTruncated"):
                return result
            next_token = page.get("NextContinuationToken")
            if not next_token or next_token == token:
                raise BackendError("Invalid S3 pagination.")
            token = next_token

    def _get(self, key: str, budget: _Budget) -> bytes:
        budget.check()
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        body = response["Body"]
        try:
            if response.get("ContentLength", 0) > MAX_FILE_BYTES:
                raise BackendError("File size limit exceeded.")
            result = bytearray()
            for chunk in body.iter_chunks(chunk_size=65536):
                budget.check()
                result.extend(chunk)
                if len(result) > MAX_FILE_BYTES:
                    raise BackendError("File size limit exceeded.")
            budget.account(len(result))
            return bytes(result)
        finally:
            body.close()

    def _exists(self, key: str, budget: _Budget) -> bool:
        budget.check()
        try:
            self.client.head_object(Bucket=self.config.bucket, Key=key)
            budget.check()
            return True
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise

    def list_skills(self) -> dict[str, dict[str, bytes]]:
        budget = _Budget()
        try:
            keys = self._keys(self.prefix, budget)
            grouped: dict[str, dict[str, str]] = {}
            for key, size in keys.items():
                relative = key[len(self.prefix) :]
                if relative.endswith("/"):
                    continue
                if "/" not in relative:
                    continue
                skill, path = relative.split("/", 1)
                validate_skill_name(skill)
                validate_relative_path(path)
                if size > MAX_FILE_BYTES:
                    raise BackendError("File size limit exceeded.")
                grouped.setdefault(skill, {})[path] = key
            result = {}
            for skill, paths in grouped.items():
                if "SKILL.md" not in paths:
                    continue
                if len(paths) > MAX_FILES_PER_SKILL:
                    raise BackendError("Skill file count limit exceeded.")
                result[skill] = validate_files(
                    {path: self._get(key, budget) for path, key in sorted(paths.items())}
                )
                if len(result) > MAX_SKILLS_PER_STORE:
                    raise BackendError("Store skill count limit exceeded.")
            return result
        except _IO_ERRORS:
            raise BackendError("S3 scan failed.") from None

    def read_file(self, skill: str, path: str) -> bytes:
        try:
            return self._get(self._key(skill, path), _Budget())
        except _IO_ERRORS:
            raise BackendError("S3 file read failed.") from None

    def write_tree(self, skill: str, files: Mapping[str, bytes], replace: bool = False) -> None:
        content = validate_files(files)
        budget = _Budget()
        prefix = self._key(skill)
        try:
            if self._exists(self._key(skill, "SKILL.md"), budget) and not replace:
                raise BackendError("Skill already exists; replacement is required.")
            previous = self._keys(prefix, budget)
            for path in sorted(content, key=lambda value: (value == "SKILL.md", value)):
                budget.check()
                self.client.put_object(
                    Bucket=self.config.bucket,
                    Key=self._key(skill, path),
                    Body=content[path],
                )
                budget.check()
            current = {self._key(skill, path) for path in content}
            for key in sorted(previous.keys() - current):
                budget.check()
                self.client.delete_object(Bucket=self.config.bucket, Key=key)
                budget.check()
        except _IO_ERRORS:
            raise BackendError("S3 write failed.") from None

    def delete_skill(self, skill: str) -> None:
        budget = _Budget()
        prefix = self._key(skill)
        try:
            previous = self._keys(prefix, budget)
            manifest = self._key(skill, "SKILL.md")
            budget.check()
            self.client.delete_object(Bucket=self.config.bucket, Key=manifest)
            budget.check()
            for key in sorted(previous.keys() - {manifest}):
                budget.check()
                self.client.delete_object(Bucket=self.config.bucket, Key=key)
                budget.check()
        except _IO_ERRORS:
            raise BackendError("S3 deletion failed.") from None

    def verify_tree(self, skill: str, files: Mapping[str, bytes]) -> None:
        content = validate_files(files)
        budget = _Budget()
        try:
            for path, expected in content.items():
                key = self._key(skill, path)
                budget.check()
                details = self.client.head_object(Bucket=self.config.bucket, Key=key)
                if details.get("ContentLength") != len(expected):
                    raise BackendError("Skill verification failed.")
                actual = self._get(key, budget)
                if hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
                    raise BackendError("Skill verification failed.")
        except _IO_ERRORS:
            raise BackendError("Skill verification failed.") from None


class GitBackend(Backend):
    def __init__(self, config: StoreConfig, state_dir: Path) -> None:
        super().__init__(config)
        self.cache = state_dir / "git-cache" / config.name
        self.commit: str | None = None
        self._url, self._environment = self._git_environment()
        try:
            self.cache.parent.mkdir(mode=0o700, exist_ok=True)
            if self.cache.is_symlink() or self.cache.parent.is_symlink():
                raise BackendError("Unsafe Git cache.")
            if not self.cache.exists():
                self.cache.mkdir(mode=0o700)
                self._run(["init", "--bare", str(self.cache)], _Budget(), bare=False)
        except OSError:
            raise BackendError("Git cache initialization failed.") from None

    def _git_environment(self) -> tuple[str, dict[str, str]]:
        env = os.environ.copy()
        for name in list(env):
            if name.startswith("GIT_"):
                del env[name]
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=yes",
        )
        url = self.config.url
        parsed = urlsplit(url)
        credential = self.config.credential
        if parsed.scheme in {"http", "https"}:
            if not credential and parsed.username is not None:
                credential = unquote(parsed.username) + ":" + unquote(parsed.password or "")
            host = parsed.hostname or ""
            if ":" in host:
                host = "[" + host + "]"
            if parsed.port:
                host += ":" + str(parsed.port)
            url = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        if credential:
            if parsed.scheme not in {"http", "https"}:
                raise BackendError("Git credentials require HTTP or HTTPS.")
            if ":" not in credential:
                credential = "oauth2:" + credential
            encoded = base64.b64encode(credential.encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraHeader",
                GIT_CONFIG_VALUE_0="Authorization: Basic " + encoded,
            )
        return url, env

    def _run(
        self,
        arguments: list[str],
        budget: _Budget,
        *,
        bare: bool = True,
        output_limit: int = MAX_FILE_BYTES + 1,
    ) -> bytes:
        timeout = min(GIT_TIMEOUT, budget.check())
        command = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "init.templateDir=",
        ]
        if bare:
            command.append("--git-dir=" + str(self.cache))
        command.extend(arguments)
        try:
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(
                    command,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    env=self._environment,
                    start_new_session=True,
                )
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise BackendError("Git operation timed out.") from None
                if process.returncode:
                    raise BackendError("Git operation failed.")
                if output.tell() > output_limit:
                    raise BackendError("Git output size limit exceeded.")
                output.seek(0)
                result = output.read(output_limit + 1)
                budget.check()
                return result
        except OSError:
            raise BackendError("Git operation failed.") from None

    def refresh(self) -> None:
        budget = _Budget()
        self._run(
            [
                "fetch",
                "--depth=1",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                self._url,
                self.config.ref,
            ],
            budget,
        )
        value = (
            self._run(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], budget)
            .decode("ascii")
            .strip()
        )
        if len(value) not in {40, 64} or any(char not in "0123456789abcdef" for char in value):
            raise BackendError("Invalid Git revision.")
        self.commit = value

    def list_skills(self) -> dict[str, dict[str, bytes]]:
        if self.commit is None:
            self.refresh()
        budget = _Budget()
        records = self._run(
            ["ls-tree", "-rz", "--full-tree", self.commit],
            budget,
            output_limit=32 * 1024**2,
        )
        grouped: dict[str, dict[str, str]] = {}
        for record in records.split(b"\0"):
            if not record:
                continue
            budget.check()
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            try:
                full_path = raw_path.decode("utf-8")
            except UnicodeDecodeError:
                raise BackendError("Git file paths must be UTF-8.") from None
            if "/" not in full_path or full_path.startswith("."):
                continue
            skill, path = full_path.split("/", 1)
            validate_skill_name(skill)
            validate_relative_path(path)
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise BackendError("Git links and special entries are not supported.")
            grouped.setdefault(skill, {})[path] = oid.decode("ascii")
            if sum(map(len, grouped.values())) > _MAX_OBJECTS:
                raise BackendError("Store file count limit exceeded.")
        result = {}
        for skill, files in grouped.items():
            if "SKILL.md" not in files:
                continue
            if len(files) > MAX_FILES_PER_SKILL:
                raise BackendError("Skill file count limit exceeded.")
            content = {}
            for path, oid in files.items():
                size = int(self._run(["cat-file", "-s", oid], budget))
                if size > MAX_FILE_BYTES:
                    raise BackendError("File size limit exceeded.")
                data = self._run(["cat-file", "blob", oid], budget)
                budget.account(len(data))
                content[path] = data
            result[skill] = validate_files(content)
            if len(result) > MAX_SKILLS_PER_STORE:
                raise BackendError("Store skill count limit exceeded.")
        return result


def _nested(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _endpoint(value: str) -> tuple[str, str, int | None, str]:
    if not value:
        return ("aws", "", None, "")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host == "s3.amazonaws.com" or (host.startswith("s3.") and host.endswith(".amazonaws.com")):
        return ("aws", "", None, "")
    port = parsed.port
    if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
        port = None
    return (parsed.scheme, host, port, parsed.path.rstrip("/"))


def backends_overlap(left: StoreConfig, right: StoreConfig) -> bool:
    if left.kind == right.kind == "directory":
        return _nested(Path(left.path), Path(right.path))
    if left.kind == right.kind == "s3":
        if left.bucket != right.bucket or _endpoint(left.endpoint) != _endpoint(right.endpoint):
            return False
        first = left.prefix.rstrip("/")
        second = right.prefix.rstrip("/")
        return (
            first == second
            or not first
            or not second
            or first.startswith(second + "/")
            or second.startswith(first + "/")
        )
    return False


def make_backend(config: StoreConfig, state_dir: str | Path) -> Backend:
    state = Path(state_dir)
    if config.kind == "directory":
        if _nested(Path(config.path), state):
            raise BackendError("Directory stores must not overlap the state directory.")
        return DirectoryBackend(config)
    if config.kind == "s3":
        return S3Backend(config)
    return GitBackend(config, state)
