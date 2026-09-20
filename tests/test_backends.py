import os
import subprocess
from dataclasses import FrozenInstanceError

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from skill_store import backends
from skill_store.backends import (
    DirectoryBackend,
    GitBackend,
    S3Backend,
    backends_overlap,
    make_backend,
)
from skill_store.models import BackendError, Skill, SkillStoreError, StoreConfig, validate_files

FILES = {
    "SKILL.md": b"---\nname: sample\ndescription: A useful skill.\n---\n# Sample\n",
    "scripts/run.sh": b"#!/bin/sh\nprintf hello\n",
    "assets/raw.bin": bytes(range(256)),
}


@pytest.fixture
def directory(tmp_path):
    root = tmp_path / "library"
    return DirectoryBackend(StoreConfig("local", "directory", path=str(root)))


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="skills-test")
        yield S3Backend(
            StoreConfig(
                "objects",
                "s3",
                bucket="skills-test",
                prefix="library",
                access_key="testing",
                secret_key="testing",
            )
        )


@pytest.fixture(params=["directory", "s3"])
def writable(request):
    return request.getfixturevalue(request.param)


def test_backend_complete_roundtrip_replace_and_delete(writable):
    writable.write_tree("sample", FILES)
    assert writable.list_skills() == {"sample": FILES}
    assert writable.read_file("sample", "assets/raw.bin") == FILES["assets/raw.bin"]
    writable.verify_tree("sample", FILES)
    with pytest.raises(BackendError, match="already exists"):
        writable.write_tree("sample", FILES)
    replacement = {"SKILL.md": b"# New\n", "other.txt": b"new"}
    writable.write_tree("sample", replacement, replace=True)
    assert writable.list_skills() == {"sample": replacement}
    writable.verify_tree("sample", replacement)
    writable.delete_skill("sample")
    writable.delete_skill("sample")
    assert writable.list_skills() == {}


def test_verification_rejects_same_size_different_content(writable):
    writable.write_tree("sample", FILES)
    changed = dict(FILES, **{"SKILL.md": b"x" * len(FILES["SKILL.md"])})
    with pytest.raises(BackendError, match="verification"):
        writable.verify_tree("sample", changed)


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "a/../escape",
        "a//b",
        "./a",
        "a\\b",
        "a/./b",
        "a/\x00b",
        "a/\nb",
        "a/" + "b" * 256,
    ],
)
def test_invalid_paths_rejected_before_mutation(writable, path):
    with pytest.raises(SkillStoreError):
        writable.write_tree("sample", {"SKILL.md": b"# Test", path: b"unsafe"})
    assert writable.list_skills() == {}


@pytest.mark.parametrize(
    "files",
    [
        {"SKILL.md": b"ok", "A": b"a", "a": b"b"},
        {"SKILL.md": b"ok", "A/first": b"a", "a/second": b"b"},
        {"SKILL.md": b"ok", "path": b"a", "path/file": b"b"},
        {"SKILL.md": b"\xff"},
        {"other.md": b"missing"},
    ],
)
def test_invalid_trees(files):
    with pytest.raises(SkillStoreError):
        validate_files(files)


def test_skill_snapshot_is_immutable():
    supplied = dict(FILES)
    skill = Skill("local", "sample", supplied)
    supplied["SKILL.md"] = b"changed"
    assert skill.body == FILES["SKILL.md"].decode()
    assert skill.description == "A useful skill."
    assert skill.qualified_name == "local/sample"
    with pytest.raises(TypeError):
        skill.files["x"] = b"no"
    with pytest.raises(FrozenInstanceError):
        skill.name = "other"


def test_config_secrets_masked_and_values_validated():
    config = StoreConfig(
        "remote", "git", url="https://user:password@example.test/repo", credential="secret"
    )
    result = config.masked()
    assert "password" not in repr(result)
    assert "secret" not in result["credential"]
    assert config.to_dict()["credential"] == "secret"
    with pytest.raises(SkillStoreError):
        StoreConfig("bad/name", "directory", path="/tmp/store")
    with pytest.raises(SkillStoreError):
        StoreConfig("bad", "directory", path="relative")
    with pytest.raises(SkillStoreError):
        StoreConfig("bad", "git", url="ext::evil")
    with pytest.raises(SkillStoreError):
        StoreConfig("bad", "git", url="https://example.test/repo?token=secret")


def test_directory_replacement_is_one_native_exchange(directory, monkeypatch):
    directory.write_tree("sample", FILES)
    original = backends._atomic_rename
    calls = []

    def observe(parent, source, target, *, exchange):
        calls.append(exchange)
        assert directory.read_file("sample", "SKILL.md") == FILES["SKILL.md"]
        return original(parent, source, target, exchange=exchange)

    monkeypatch.setattr(backends, "_atomic_rename", observe)
    directory.write_tree("sample", {"SKILL.md": b"replacement"}, replace=True)
    assert calls == [True]
    assert directory.read_file("sample", "SKILL.md") == b"replacement"
    assert not list(directory.root.glob(".skill-store-*"))


def test_unavailable_exchange_preserves_original_tree(directory, monkeypatch):
    directory.write_tree("sample", FILES)

    def unavailable(*args, **kwargs):
        raise BackendError("Atomic directory publication is unavailable.")

    monkeypatch.setattr(backends, "_atomic_rename", unavailable)
    with pytest.raises(BackendError, match="unavailable"):
        directory.write_tree("sample", {"SKILL.md": b"replacement"}, replace=True)
    assert directory.list_skills() == {"sample": FILES}
    assert not list(directory.root.glob(".skill-store-*"))


def test_stage_fsync_failure_preserves_original(directory, monkeypatch):
    directory.write_tree("sample", FILES)

    def fail(_):
        raise OSError("sensitive-path")

    monkeypatch.setattr(backends.os, "fsync", fail)
    with pytest.raises(BackendError, match="Directory write failed") as caught:
        directory.write_tree("sample", {"SKILL.md": b"replacement"}, replace=True)
    assert "sensitive" not in str(caught.value)
    assert directory.list_skills() == {"sample": FILES}


def test_directory_verification_rejects_extra_files(directory):
    directory.write_tree("sample", FILES)
    with pytest.raises(BackendError, match="verification"):
        directory.verify_tree("sample", {"SKILL.md": FILES["SKILL.md"]})


@pytest.mark.parametrize("target_kind", ["file", "directory", "fifo"])
def test_directory_rejects_symlinks_and_special_files(directory, tmp_path, target_kind):
    directory.write_tree("sample", FILES)
    target = directory.root / "sample" / "unsafe"
    outside = tmp_path / "outside"
    if target_kind == "file":
        outside.write_bytes(b"SECRET")
        target.symlink_to(outside)
    elif target_kind == "directory":
        outside.mkdir()
        (outside / "secret").write_bytes(b"SECRET")
        target.symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(target)
    with pytest.raises(BackendError, match="links and special"):
        directory.list_skills()
    with pytest.raises(BackendError):
        directory.read_file("sample", "unsafe")
    if outside.is_file():
        assert outside.read_bytes() == b"SECRET"


def test_directory_rejects_root_and_skill_symlinks(tmp_path, directory):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BackendError):
        DirectoryBackend(StoreConfig("local", "directory", path=str(alias)))
    (directory.root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BackendError):
        directory.list_skills()
    with pytest.raises(BackendError):
        directory.write_tree("linked", FILES, replace=True)
    assert list(outside.iterdir()) == []


def test_open_file_survives_native_exchange(directory):
    directory.write_tree("sample", FILES)
    old = os.open(directory.root / "sample" / "SKILL.md", os.O_RDONLY)
    try:
        directory.write_tree("sample", {"SKILL.md": b"new"}, replace=True)
        assert os.read(old, 4096) == FILES["SKILL.md"]
    finally:
        os.close(old)
    assert directory.list_skills() == {"sample": {"SKILL.md": b"new"}}


def test_directory_state_overlap_rejected(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    for path in (state, tmp_path, state / "nested"):
        with pytest.raises(BackendError, match="overlap"):
            make_backend(StoreConfig("local", "directory", path=str(path)), state)
    make_backend(StoreConfig("local", "directory", path=str(tmp_path / "library")), state)


def test_overlap_checks_directory_and_s3_prefix_boundaries(tmp_path):
    def directory(path):
        return StoreConfig("local", "directory", path=str(path))

    assert backends_overlap(directory(tmp_path), directory(tmp_path / "nested"))
    assert not backends_overlap(directory(tmp_path / "a"), directory(tmp_path / "b"))

    def objects(prefix, endpoint="http://example.test:80/"):
        return StoreConfig("objects", "s3", endpoint=endpoint, bucket="skills-test", prefix=prefix)

    assert backends_overlap(objects("a"), objects("a/b", "http://example.test"))
    assert not backends_overlap(objects("a"), objects("ab"))
    assert backends_overlap(objects(""), objects("any"))
    assert not backends_overlap(objects("a"), objects("a", "http://other.test"))


def test_s3_manifest_publication_and_deletion_order(s3, monkeypatch):
    puts = []
    deletes = []
    original_put = s3.client.put_object
    original_delete = s3.client.delete_object

    def put(**arguments):
        puts.append(arguments["Key"])
        return original_put(**arguments)

    def delete(**arguments):
        deletes.append(arguments["Key"])
        return original_delete(**arguments)

    monkeypatch.setattr(s3.client, "put_object", put)
    monkeypatch.setattr(s3.client, "delete_object", delete)
    s3.write_tree("sample", FILES)
    assert puts[-1] == "library/sample/SKILL.md"
    s3.delete_skill("sample")
    assert deletes[0] == "library/sample/SKILL.md"


def test_s3_failed_support_write_preserves_manifest(s3, monkeypatch):
    s3.write_tree("sample", FILES)
    original = s3.client.put_object

    def fail(**arguments):
        if arguments["Key"].endswith("broken.txt"):
            raise ClientError({"Error": {"Code": "Denied", "Message": "SECRET"}}, "PutObject")
        return original(**arguments)

    monkeypatch.setattr(s3.client, "put_object", fail)
    with pytest.raises(BackendError, match="S3 write failed") as caught:
        s3.write_tree("sample", {"SKILL.md": b"new", "broken.txt": b"fail"}, replace=True)
    assert "SECRET" not in str(caught.value)
    assert s3.read_file("sample", "SKILL.md") == FILES["SKILL.md"]


def test_s3_verifies_with_head_and_get_never_list(s3, monkeypatch):
    s3.write_tree("sample", FILES)
    head_keys = []
    original = s3.client.head_object

    def head(**arguments):
        head_keys.append(arguments["Key"])
        return original(**arguments)

    def no_list(**arguments):
        raise AssertionError("Verification must not depend on LIST.")

    monkeypatch.setattr(s3.client, "head_object", head)
    monkeypatch.setattr(s3.client, "list_objects_v2", no_list)
    s3.verify_tree("sample", FILES)
    assert set(head_keys) == {"library/sample/" + path for path in FILES}


def test_s3_sdk_timeouts_and_scan_deadline(s3, monkeypatch):
    config = s3.client.meta.config
    assert config.connect_timeout == 3
    assert config.read_timeout == 5
    assert config.retries["total_max_attempts"] == 2
    monkeypatch.setattr(backends, "SCAN_TIMEOUT", -1)
    with pytest.raises(BackendError, match="timed out"):
        s3.list_skills()


def git(repository, *arguments):
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.test",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.test",
        },
    )
    return result.stdout


@pytest.fixture
def git_backend(tmp_path):
    repository = tmp_path / "remote"
    repository.mkdir()
    git(repository, "init")
    (repository / "sample").mkdir()
    for path, content in FILES.items():
        target = repository / "sample" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Initial")
    state = tmp_path / "state"
    state.mkdir()
    return GitBackend(StoreConfig("git", "git", url=str(repository)), state), repository


def test_git_reads_refreshes_and_stays_read_only(git_backend):
    backend, repository = git_backend
    assert backend.list_skills() == {"sample": FILES}
    assert not backend.writable_capable
    with pytest.raises(BackendError, match="read-only"):
        backend.write_tree("sample", FILES)
    with pytest.raises(BackendError, match="read-only"):
        backend.delete_skill("sample")
    (repository / "sample" / "SKILL.md").write_bytes(b"# Changed")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Changed")
    assert backend.list_skills()["sample"] == FILES
    backend.refresh()
    assert backend.list_skills()["sample"]["SKILL.md"] == b"# Changed"


def test_git_rejects_symlink_blobs(git_backend):
    backend, repository = git_backend
    (repository / "sample" / "escape").symlink_to("/etc/passwd")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Unsafe")
    with pytest.raises(BackendError, match="links"):
        backend.list_skills()


def test_git_credentials_stay_out_of_command_and_cache(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    config = StoreConfig("git", "git", url="https://user:secret@example.test/repository")
    backend = GitBackend(config, state)
    assert backend._url == "https://example.test/repository"
    assert "secret" not in (backend.cache / "config").read_text()
    assert "secret" not in backend._environment["GIT_CONFIG_VALUE_0"]
    assert backend._environment["GIT_TERMINAL_PROMPT"] == "0"


def test_git_command_deadline_kills_process_group(git_backend, monkeypatch):
    backend, _ = git_backend
    killed = []

    class HangingProcess:
        pid = 12345678
        returncode = None

        def __init__(self, *args, **kwargs):
            assert kwargs["start_new_session"]
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                assert timeout <= backends.GIT_TIMEOUT
                raise subprocess.TimeoutExpired("redacted", timeout)
            self.returncode = -9

    monkeypatch.setattr(backends.subprocess, "Popen", HangingProcess)
    monkeypatch.setattr(backends.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(BackendError, match="timed out"):
        backend.refresh()
    assert killed == [(12345678, 9)]


def test_store_size_bound(directory, monkeypatch):
    directory.write_tree("sample", FILES)
    monkeypatch.setattr(backends, "MAX_STORE_BYTES", 1)
    with pytest.raises(BackendError, match="size limit"):
        directory.list_skills()


@pytest.mark.parametrize(
    "description",
    [
        "Install a spec-driven workflow. Cross-platform: macOS, Linux, Windows.",
        'Use when the user asks for "OpenSpec sub-agents". Cross-platform: macOS, Linux, Windows.',
    ],
)
def test_legacy_plain_description_preserves_original_bytes(description):
    source = (
        "---\nname: spec-driven-tla\ndescription: " + description + "\n---\n# Original body\n"
    ).encode()
    skill = Skill("shared", "spec-driven-tla", {"SKILL.md": source})
    assert skill.description == description
    assert skill.files["SKILL.md"] == source
    assert skill.body.encode() == source


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ('description: "Quoted: description"', "Quoted: description"),
        (
            "description: >\n  Folded description\n  with a second line.",
            "Folded description with a second line.",
        ),
    ],
)
def test_standard_yaml_descriptions_still_parse(header, expected):
    skill = Skill("shared", "sample", {"SKILL.md": ("---\n" + header + "\n---\n").encode()})
    assert skill.description == expected


@pytest.mark.parametrize(
    "header",
    [
        "name: invalid: value",
        "description: valid: legacy\nname: invalid: value",
        "description: valid: legacy\nother: !!python/object/apply:os.system ['false']",
        "description: [invalid: value",
        'description: "unterminated: value',
        "description: !unknown value: other",
        "description: value: legacy\ndescription: other: legacy",
        "description: value: legacy\n  unexpected: nested",
    ],
)
def test_legacy_fallback_rejects_other_invalid_front_matter(header):
    with pytest.raises(SkillStoreError, match="Invalid skill front matter"):
        Skill("shared", "sample", {"SKILL.md": ("---\n" + header + "\n---\n").encode()})
