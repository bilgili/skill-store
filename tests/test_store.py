from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from types import MappingProxyType

import pytest

from skill_store import backends
from skill_store import journal as journal_module
from skill_store.journal import JournalCommittedError, MigrationIntent, MigrationJournal
from skill_store.models import (
    BackendCommittedError,
    BackendError,
    Skill,
    SkillStoreError,
    StoreConfig,
)
from skill_store.registry import Registry
from skill_store.store import SkillStore

FILES = {
    "SKILL.md": b"---\ndescription: Diagnose a failing test.\n---\nFollow the evidence.\n",
    "references/raw.bin": bytes(range(256)),
}


async def running(tmp_path: Path) -> SkillStore:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    store = SkillStore(state)
    await store.start()
    return store


async def directory(store: SkillStore, tmp_path: Path, name: str = "local") -> Path:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    await store.add_store(StoreConfig(name, "directory", path=str(path)))
    return path


async def test_persistent_registry_skill_restart_and_removal(tmp_path):
    store = await running(tmp_path)
    root = await directory(store, tmp_path)
    await store.set_writable("local")
    await store.write_skill("debug", FILES, message="Create a skill")
    previous = store.snapshot
    assert store.use_skill("debug")["body"] == FILES["SKILL.md"].decode()
    assert previous["local/debug"].files["references/raw.bin"] == bytes(range(256))
    with pytest.raises(TypeError):
        previous["other"] = None
    with pytest.raises(TypeError):
        previous["local/debug"].files["other"] = b"no"
    state_file = tmp_path / "state" / "stores.json"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    await store.close()

    restarted = SkillStore(tmp_path / "state")
    await restarted.start()
    try:
        assert restarted.list_stores()["writable_store"] == "local"
        assert restarted.snapshot["local/debug"].files == FILES
        await restarted.remove_store("local")
        assert restarted.snapshot == {}
        assert restarted.list_stores()["writable_store"] is None
        assert (root / "debug" / "SKILL.md").read_bytes() == FILES["SKILL.md"]
        with pytest.raises(SkillStoreError, match="No writable"):
            await restarted.write_skill("new", FILES)
    finally:
        await restarted.close()


async def test_writable_switch_ambiguous_identity_and_catalog(tmp_path):
    store = await running(tmp_path)
    try:
        first = await directory(store, tmp_path, "first")
        second = await directory(store, tmp_path, "second")
        with pytest.raises(SkillStoreError, match="No writable"):
            await store.write_skill("same", FILES)
        await store.set_writable("first")
        await store.write_skill("same", FILES)
        await store.set_writable("second")
        await store.write_skill("same", {**FILES, "SKILL.md": b"Other instructions"})
        assert (first / "same" / "SKILL.md").read_bytes() != (
            second / "same" / "SKILL.md"
        ).read_bytes()
        with pytest.raises(SkillStoreError, match="first/same, second/same"):
            store.use_skill("same")
        assert store.use_skill("first/same")["files"] == ["references/raw.bin"]
        assert "first/same" in store.instructions()
        assert store.list_skills(limit=1)["next_offset"] == 1
        assert store.list_skills(offset=1, limit=1)["next_offset"] is None
    finally:
        await store.close()


async def test_process_lock_and_invalid_registry_fail_closed(tmp_path):
    store = await running(tmp_path)
    duplicate = SkillStore(tmp_path / "state")
    with pytest.raises(SkillStoreError, match="Cannot open"):
        await duplicate.start()
    await store.close()
    (tmp_path / "state" / "stores.json").write_text('{"credential":"must-not-appear"}')
    broken = SkillStore(tmp_path / "state")
    with pytest.raises(SkillStoreError) as failure:
        await broken.start()
    assert "must-not-appear" not in str(failure.value)
    # Failed startup releases its lock.
    registry = Registry(tmp_path / "state")
    (tmp_path / "state" / "stores.json").unlink()
    registry.open()
    registry.close()


async def test_start_requires_existing_state_directory(tmp_path):
    with pytest.raises(SkillStoreError, match="Cannot open"):
        await SkillStore(tmp_path / "missing").start()


async def test_failed_registry_replace_keeps_memory_and_disk(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path)
        old = (tmp_path / "state" / "stores.json").read_bytes()

        def fail(*args, **kwargs):
            raise OSError("secret-do-not-echo")

        monkeypatch.setattr("skill_store.registry.os.replace", fail)
        with pytest.raises(SkillStoreError, match="Cannot save") as failure:
            await store.set_writable("local")
        assert "secret-do-not-echo" not in str(failure.value)
        assert store.list_stores()["writable_store"] is None
        assert (tmp_path / "state" / "stores.json").read_bytes() == old
    finally:
        await store.close()


async def test_failed_directory_sync_keeps_installed_registry_in_memory(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path)
        sync = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("disk failure")
            sync(fd)

        monkeypatch.setattr("skill_store.registry.os.fsync", fail_directory)
        with pytest.raises(SkillStoreError, match="Cannot save"):
            await store.set_writable("local")
        disk = json.loads((tmp_path / "state" / "stores.json").read_text())
        assert disk["writable_store"] == store.registry.writable_store == "local"
    finally:
        await store.close()


async def test_cancelled_write_holds_ownership_and_serves_old_snapshot(tmp_path, monkeypatch):
    store = await running(tmp_path)
    root = await directory(store, tmp_path)
    await store.set_writable("local")
    await store.write_skill("old", FILES)
    backend = store._backends["local"]
    original = backend.write_tree
    entered, release = threading.Event(), threading.Event()

    def slow(name, files, replace=False):
        entered.set()
        assert release.wait(5)
        return original(name, files, replace=replace)

    monkeypatch.setattr(backend, "write_tree", slow)
    write = asyncio.create_task(store.write_skill("new", FILES))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        write.cancel()
        await asyncio.sleep(0)
        write.cancel()
        switch = asyncio.create_task(store.remove_store("local"))
        await asyncio.sleep(0.02)
        assert not write.done()
        assert not switch.done()
        assert store.use_skill("old")["qualified_name"] == "local/old"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await write
        await switch
        assert (root / "new" / "SKILL.md").read_bytes() == FILES["SKILL.md"]
        assert store.snapshot == {}
    finally:
        release.set()
        await store.close()


async def test_close_waits_for_cancelled_worker_before_releasing_process_lock(
    tmp_path, monkeypatch
):
    store = await running(tmp_path)
    await directory(store, tmp_path)
    await store.set_writable("local")
    backend = store._backends["local"]
    original = backend.write_tree
    entered, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(backend, "write_tree", slow)
    write = asyncio.create_task(store.write_skill("new", FILES))
    assert await asyncio.to_thread(entered.wait, 3)
    closing = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    duplicate = SkillStore(tmp_path / "state")
    try:
        with pytest.raises(SkillStoreError, match="Cannot open"):
            await duplicate.start()
        assert not closing.done()
        release.set()
        await write
        with pytest.raises(asyncio.CancelledError):
            await closing
        restored = SkillStore(tmp_path / "state")
        await restored.start()
        assert restored.use_skill("new")["qualified_name"] == "local/new"
        await restored.close()
    finally:
        release.set()
        await store.close()


async def test_failed_refresh_preserves_snapshot_and_masks_error(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path)
        await store.set_writable("local")
        await store.write_skill("old", FILES)
        old = store.snapshot

        def fail():
            raise RuntimeError("https://user:SECRET@private.example")

        monkeypatch.setattr(store._backends["local"], "list_skills", fail)
        with pytest.raises(SkillStoreError, match="Store refresh failed") as failure:
            await store.refresh_store("local")
        assert "SECRET" not in str(failure.value)
        assert store.snapshot is old
        assert store.list_stores()["stores"][0]["error"] == "Store refresh failed."
    finally:
        await store.close()


async def test_add_update_scan_fail_before_registry_commit(tmp_path):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path)
        original = (tmp_path / "state" / "stores.json").read_bytes()
        with pytest.raises(SkillStoreError):
            await store.update_store("local", {"path": str(tmp_path / "missing" / "nested")})
        assert (tmp_path / "state" / "stores.json").read_bytes() == original
        with pytest.raises(SkillStoreError):
            await store.update_store("local", {"kind": "git"})
        with pytest.raises(SkillStoreError):
            await store.add_store(StoreConfig("unsafe", "directory", path=str(tmp_path)))
    finally:
        await store.close()


async def test_write_publishes_input_without_backend_relist(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path)
        await store.set_writable("local")

        def forbidden():
            raise AssertionError("The mutation must not enumerate after a write")

        monkeypatch.setattr(store._backends["local"], "list_skills", forbidden)
        await store.write_skill("new", FILES)
        assert store.snapshot["local/new"].files == FILES
    finally:
        await store.close()


async def test_committed_write_publishes_namespace_truth(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path)
        await store.set_writable("local")
        backend = store._backends["local"]
        original = backend.write_tree

        def committed(name, files, replace=False):
            original(name, files, replace=replace)
            raise BackendCommittedError("write")

        monkeypatch.setattr(backend, "write_tree", committed)
        result = await store.write_skill("new", FILES)
        assert "warning" in result
        assert store.snapshot["local/new"].files == FILES
        assert (tmp_path / "local" / "new" / "SKILL.md").exists()
    finally:
        await store.close()


def test_journal_clear_retry_reconfirms_absence(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    journal = MigrationJournal(state)
    journal.save(MigrationIntent.create("source", "target", "same", FILES))

    def fail():
        raise OSError("parent sync")

    monkeypatch.setattr(journal, "_sync_parent", fail)
    with pytest.raises(SkillStoreError):
        journal.clear()
    assert not journal.path.exists()
    calls = 0

    def observe():
        nonlocal calls
        calls += 1

    monkeypatch.setattr(journal, "_sync_parent", observe)
    journal.clear()
    assert calls == 1


def test_journal_stage_creation_failure_is_safe(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    journal = MigrationJournal(state)
    monkeypatch.setattr(
        journal_module.tempfile,
        "mkstemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(SkillStoreError, match="Cannot save"):
        journal.save(MigrationIntent.create("source", "target", "same", FILES))


async def test_migration_retry_after_delete_failure_and_differing_target(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        source_root = await directory(store, tmp_path, "source")
        target_root = await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("same", FILES)
        await store.set_writable("target")
        source = store._backends["source"]
        original = source.delete_skill

        def fail(name):
            raise BackendError("Deletion failed.")

        monkeypatch.setattr(source, "delete_skill", fail)
        with pytest.raises(SkillStoreError):
            await store.migrate_skill("source", "same", "target")
        journal = tmp_path / "state" / "migration.json"
        assert stat.S_IMODE(journal.stat().st_mode) == 0o600
        assert json.loads(journal.read_text())["phase"] == "deleting"
        with pytest.raises(SkillStoreError, match="recovery requires"):
            await store.set_writable("source")
        assert (source_root / "same" / "SKILL.md").exists()
        assert store.snapshot["target/same"].files == FILES
        monkeypatch.setattr(source, "delete_skill", original)
        await store.migrate_skill("source", "same", "target")
        assert not journal.exists()
        assert not (source_root / "same").exists()
        assert (target_root / "same" / "SKILL.md").read_bytes() == FILES["SKILL.md"]
        await store.set_writable("source")
        await store.write_skill("same", {"SKILL.md": b"different"})
        await store.set_writable("target")
        with pytest.raises(SkillStoreError, match="different content"):
            await store.migrate_skill("source", "same", "target")
        assert (source_root / "same").exists()
    finally:
        await store.close()


async def test_restart_finishes_committed_directory_retirement(tmp_path, monkeypatch):
    store = await running(tmp_path)
    source_root = await directory(store, tmp_path, "source")
    await directory(store, tmp_path, "target")
    await store.set_writable("source")
    await store.write_skill("same", FILES)
    await store.set_writable("target")
    original = backends.shutil.rmtree

    def fail_retired(path, *args, **kwargs):
        if str(path).startswith(".skill-store-retired-"):
            raise OSError("cleanup")
        return original(path, *args, **kwargs)

    monkeypatch.setattr("skill_store.backends.shutil.rmtree", fail_retired)
    with pytest.raises(SkillStoreError, match="cleanup is pending"):
        await store.migrate_skill("source", "same", "target")
    assert not (source_root / "same").exists()
    assert (tmp_path / "state" / "migration.json").exists()
    await store.close()
    monkeypatch.setattr("skill_store.backends.shutil.rmtree", original)

    restarted = SkillStore(tmp_path / "state")
    await restarted.start()
    try:
        assert restarted._intent is None
        assert not (tmp_path / "state" / "migration.json").exists()
        assert "source/same" not in restarted.snapshot
        assert "target/same" in restarted.snapshot
        assert not list(source_root.glob(".skill-store-retired-*"))
    finally:
        await restarted.close()


async def test_restart_blocks_delete_when_target_changed(tmp_path, monkeypatch):
    store = await running(tmp_path)
    source_root = await directory(store, tmp_path, "source")
    target_root = await directory(store, tmp_path, "target")
    await store.set_writable("source")
    await store.write_skill("same", FILES)
    await store.set_writable("target")
    source = store._backends["source"]

    def fail(_name):
        raise BackendError("Deletion failed.")

    monkeypatch.setattr(source, "delete_skill", fail)
    with pytest.raises(SkillStoreError):
        await store.migrate_skill("source", "same", "target")
    await store.close()
    (target_root / "same" / "SKILL.md").write_bytes(b"changed")

    restarted = SkillStore(tmp_path / "state")
    await restarted.start()
    try:
        assert restarted._intent is not None
        assert (source_root / "same" / "SKILL.md").exists()
        with pytest.raises(SkillStoreError, match="different content"):
            await restarted.migrate_skill("source", "same", "target")
        assert (source_root / "same" / "SKILL.md").exists()
    finally:
        await restarted.close()


async def test_recovery_rejects_hidden_changed_source(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        source_root = await directory(store, tmp_path, "source")
        await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("same", FILES)
        await store.set_writable("target")
        source = store._backends["source"]
        original = source.delete_skill
        monkeypatch.setattr(
            source, "delete_skill", lambda _name: (_ for _ in ()).throw(BackendError("fail"))
        )
        with pytest.raises(SkillStoreError):
            await store.migrate_skill("source", "same", "target")
        (source_root / "same" / "SKILL.md").unlink()
        (source_root / "same" / "references" / "raw.bin").write_bytes(b"changed")
        monkeypatch.setattr(source, "delete_skill", original)
        with pytest.raises(SkillStoreError, match="source skill changed"):
            await store.migrate_skill("source", "same", "target")
        assert (source_root / "same" / "references" / "raw.bin").exists()
    finally:
        await store.close()


async def test_target_publication_precedes_deleting_journal_commit(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path, "source")
        await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("same", FILES)
        await store.set_writable("target")
        original = store._journal.save
        calls = 0

        def fail_second(intent):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SkillStoreError("journal failed")
            original(intent)

        monkeypatch.setattr(store._journal, "save", fail_second)
        with pytest.raises(SkillStoreError, match="journal failed"):
            await store.migrate_skill("source", "same", "target")
        assert store.snapshot["target/same"].files == FILES
        assert store.snapshot["source/same"].files == FILES
        assert store._intent is not None and store._intent.phase == "copying"
    finally:
        await store.close()


async def test_target_publication_precedes_reconciliation_failure(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path, "source")
        await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("same", FILES)
        await store.set_writable("target")
        target = store._backends["target"]
        monkeypatch.setattr(
            target,
            "reconcile_tree",
            lambda *_args: (_ for _ in ()).throw(BackendError("sync failed")),
        )
        with pytest.raises(SkillStoreError, match="sync failed"):
            await store.migrate_skill("source", "same", "target")
        assert store.snapshot["target/same"].files == FILES
        assert store.snapshot["source/same"].files == FILES
    finally:
        await store.close()


async def test_failed_startup_recovery_disables_mutations(tmp_path, monkeypatch):
    store = await running(tmp_path)
    await directory(store, tmp_path, "source")
    await directory(store, tmp_path, "target")
    await store.set_writable("source")
    await store.write_skill("same", FILES)
    await store.set_writable("target")
    source = store._backends["source"]
    monkeypatch.setattr(
        source, "delete_skill", lambda _name: (_ for _ in ()).throw(BackendError("fail"))
    )
    with pytest.raises(SkillStoreError):
        await store.migrate_skill("source", "same", "target")
    await store.close()

    restarted = SkillStore(tmp_path / "state")
    entered, release = threading.Event(), threading.Event()

    def fail_recovery():
        entered.set()
        assert release.wait(5)
        raise OSError("recovery failed")

    monkeypatch.setattr(restarted, "_resume_intent", fail_recovery)
    startup = asyncio.create_task(restarted.start())
    assert await asyncio.to_thread(entered.wait, 5)
    with pytest.raises(SkillStoreError, match="not running"):
        await restarted.write_skill("queued", FILES)
    release.set()
    with pytest.raises(SkillStoreError):
        await startup
    assert restarted._started is False
    with pytest.raises(SkillStoreError, match="not running"):
        await restarted.write_skill("other", FILES)


async def test_resumed_delete_requires_confirmed_journal(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        source_root = await directory(store, tmp_path, "source")
        await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("same", FILES)
        await store.set_writable("target")
        source = store._backends["source"]
        original_delete = source.delete_skill
        monkeypatch.setattr(
            source, "delete_skill", lambda _name: (_ for _ in ()).throw(BackendError("fail"))
        )
        with pytest.raises(SkillStoreError):
            await store.migrate_skill("source", "same", "target")
        monkeypatch.setattr(source, "delete_skill", original_delete)
        monkeypatch.setattr(
            store._journal,
            "save",
            lambda _intent: (_ for _ in ()).throw(JournalCommittedError("uncertain")),
        )
        with pytest.raises(JournalCommittedError):
            await store.migrate_skill("source", "same", "target")
        assert (source_root / "same" / "SKILL.md").exists()
    finally:
        await store.close()


async def test_recovery_rechecks_target_capacity_before_write(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path, "source")
        target_root = await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("same", FILES)
        await store.set_writable("target")
        await store.write_skill("other", FILES)
        store._save_intent(MigrationIntent.create("source", "target", "same", FILES))
        monkeypatch.setattr("skill_store.store.MAX_SKILLS_PER_STORE", 1)
        with pytest.raises(SkillStoreError, match="count limit"):
            await store.migrate_skill("source", "same", "target")
        assert not (target_root / "same").exists()
        assert "source/same" in store.snapshot
    finally:
        await store.close()


async def test_case_alias_rejection_does_not_create_journal(tmp_path):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path, "source")
        await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("SAMPLE", FILES)
        await store.set_writable("target")
        await store.write_skill("sample", FILES)
        with pytest.raises(SkillStoreError, match="case-insensitive"):
            await store.migrate_skill("source", "SAMPLE", "target")
        assert store._intent is None
        assert not (tmp_path / "state" / "migration.json").exists()
    finally:
        await store.close()


async def test_occupied_target_slot_does_not_create_journal(tmp_path):
    store = await running(tmp_path)
    try:
        await directory(store, tmp_path, "source")
        target_root = await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("occupied", FILES)
        (target_root / "occupied").write_bytes(b"unmanaged")
        await store.set_writable("target")
        with pytest.raises(SkillStoreError, match="slot is occupied"):
            await store.migrate_skill("source", "occupied", "target")
        assert store._intent is None
        assert not (tmp_path / "state" / "migration.json").exists()
    finally:
        await store.close()


async def test_pending_migration_allows_credential_repair_only(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        source_root = await directory(store, tmp_path, "source")
        await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("same", FILES)
        await store.set_writable("target")
        source = store._backends["source"]
        monkeypatch.setattr(
            source, "delete_skill", lambda _name: (_ for _ in ()).throw(BackendError("fail"))
        )
        with pytest.raises(SkillStoreError):
            await store.migrate_skill("source", "same", "target")
        result = await store.update_store("source", {"credential": "replacement"})
        assert result["store"]["credential"] != "replacement"
        with pytest.raises(SkillStoreError, match="storage identity"):
            await store.update_store("source", {"path": str(tmp_path / "other")})
        assert (source_root / "same" / "SKILL.md").exists()
    finally:
        await store.close()


async def test_migrate_store_reports_partial_progress_and_rejects_alias(tmp_path):
    store = await running(tmp_path)
    try:
        source = await directory(store, tmp_path, "source")
        await directory(store, tmp_path, "target")
        await store.set_writable("source")
        await store.write_skill("a", FILES)
        await store.write_skill("b", FILES)
        await store.set_writable("target")
        await store.write_skill("b", {"SKILL.md": b"different"})
        result = await store.migrate_store("source", "target")
        assert result["moved"] == 1
        assert result["remaining"] == 1
        assert result["error"]
        assert not (source / "a").exists()
        assert (source / "b").exists()
        await store.add_store(StoreConfig("alias", "directory", path=str(source)))
        await store.set_writable("alias")
        with pytest.raises(SkillStoreError, match="overlap"):
            await store.migrate_skill("source", "b", "alias")
    finally:
        await store.close()


async def test_startup_failed_store_does_not_hide_healthy_store(tmp_path):
    store = await running(tmp_path)
    await directory(store, tmp_path, "healthy")
    broken = await directory(store, tmp_path, "broken")
    await store.set_writable("healthy")
    await store.write_skill("old", FILES)
    await store.close()
    broken.rmdir()
    broken.write_text("not a directory")
    restart = SkillStore(tmp_path / "state")
    await restart.start()
    try:
        assert restart.use_skill("old")["qualified_name"] == "healthy/old"
        assert restart.list_stores()["stores"][0]["error"] == "Store refresh failed."
    finally:
        await restart.close()


def test_instruction_utf8_budget_and_paginated_fallback(tmp_path):
    store = SkillStore(tmp_path)
    files = {"SKILL.md": ("---\ndescription: " + "界" * 60 + "\n---\nbody").encode()}
    store.snapshot = MappingProxyType(
        {f"store/skill-{index}": Skill("store", f"skill-{index}", files) for index in range(150)}
    )
    instructions = store.instructions()
    assert len(instructions.encode()) <= 8192
    assert "more; call list_skills" in instructions
    assert store.list_skills(offset=100, limit=100)["total"] == 150
    assert len(store.list_skills(offset=100, limit=100)["skills"]) == 50
    with pytest.raises(SkillStoreError):
        store.list_skills(limit=True)


async def test_admin_read_view_stays_coherent_during_durable_commit(tmp_path, monkeypatch):
    store = await running(tmp_path)
    await directory(store, tmp_path)
    original = store.registry.save
    entered, release = threading.Event(), threading.Event()

    def pause_after_commit(stores, writable):
        original(stores, writable)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(store.registry, "save", pause_after_commit)
    change = asyncio.create_task(store.set_writable("local"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        view = store.list_stores()
        assert view["writable_store"] is None
        assert view["stores"][0]["writable"] is False
        release.set()
        await change
        view = store.list_stores()
        assert view["writable_store"] == "local"
        assert view["stores"][0]["writable"] is True
    finally:
        release.set()
        await store.close()


async def test_capacity_rejection_happens_before_storage_write(tmp_path, monkeypatch):
    store = await running(tmp_path)
    try:
        root = await directory(store, tmp_path)
        await store.set_writable("local")
        await store.write_skill("one", FILES)
        monkeypatch.setattr("skill_store.store.MAX_SKILLS_PER_STORE", 1)
        with pytest.raises(SkillStoreError, match="count limit"):
            await store.write_skill("two", FILES)
        assert not (root / "two").exists()
        # A replacement consumes the existing slot.
        await store.write_skill("one", {"SKILL.md": b"updated"}, replace=True)
        assert store.use_skill("one")["body"] == "updated"
    finally:
        await store.close()


def git_commit(path, content):
    (path / "example").mkdir(exist_ok=True)
    (path / "example" / "SKILL.md").write_bytes(content)
    for args in (
        ["add", "."],
        [
            "-c",
            "user.name=Skill Test",
            "-c",
            "user.email=skill@example.invalid",
            "commit",
            "-qm",
            "Update test skill",
        ],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


async def test_git_store_is_copy_only_and_poll_refreshes_content(tmp_path, monkeypatch):
    root = tmp_path / "upstream"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    git_commit(root, b"initial")
    monkeypatch.setattr("skill_store.store.POLL_SECONDS", 0.02)
    store = await running(tmp_path)
    try:
        await store.add_store(StoreConfig("git", "git", url=str(root)))
        await directory(store, tmp_path, "target")
        with pytest.raises(SkillStoreError, match="support writes"):
            await store.set_writable("git")
        await store.set_writable("target")
        result = await store.migrate_store("git", "target")
        assert result == {"moved": 0, "copied": 1, "remaining": 0, "error": None}
        assert (root / "example" / "SKILL.md").read_bytes() == b"initial"
        assert store.use_skill("git/example")["body"] == "initial"
        git_commit(root, b"refreshed")
        async with asyncio.timeout(5):
            while store.use_skill("git/example")["body"] != "refreshed":
                await asyncio.sleep(0.02)
        assert store.use_skill("target/example")["body"] == "initial"
    finally:
        await store.close()


async def test_credentials_never_leave_registry_as_plaintext(tmp_path):
    store = await running(tmp_path)
    path = tmp_path / "local"
    path.mkdir()
    config = StoreConfig("local", "directory", path=str(path), credential="very-secret")
    try:
        result = await store.add_store(config)
        assert "very-secret" not in json.dumps(result)
        assert "very-secret" not in json.dumps(store.list_stores())
        assert "very-secret" in (tmp_path / "state" / "stores.json").read_text()
    finally:
        await store.close()
