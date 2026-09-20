"""One mutation owner and lock-free immutable catalog reads."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, TypeVar

from .backends import backends_overlap, make_backend
from .models import (
    MAX_SKILLS_PER_STORE,
    MAX_STORE_BYTES,
    Skill,
    SkillStoreError,
    StoreConfig,
    validate_files,
    validate_name,
    validate_skill_name,
)
from .registry import Registry

T = TypeVar("T")
POLL_SECONDS = 60.0
INSTRUCTIONS_BYTES = 8192


class SkillStore:
    """Serialize storage changes and publish complete, immutable snapshots."""

    def __init__(self, state_dir: str | Path):
        self.registry = Registry(state_dir)
        self.snapshot: Mapping[str, Skill] = MappingProxyType({})
        self._backends: dict[str, object] = {}
        self._errors: dict[str, str] = {}
        self._admin_view = (self.registry.stores, None, MappingProxyType({}))
        self._lock = asyncio.Lock()
        self._operations: set[asyncio.Task] = set()
        self._poll_task: asyncio.Task | None = None
        self._started = False
        self._closing = False
        self._close_task: asyncio.Task | None = None

    async def _mutate(self, operation: Callable[[], T], *, opening: bool = False) -> T:
        if self._closing or (not self._started and not opening):
            raise SkillStoreError("The skill store is not running.")

        def transaction() -> T:
            try:
                return operation()
            finally:
                self._admin_view = (
                    self.registry.stores,
                    self.registry.writable_store,
                    MappingProxyType(dict(self._errors)),
                )

        async def owned() -> T:
            async with self._lock:
                try:
                    return await asyncio.to_thread(transaction)
                except SkillStoreError:
                    raise
                except Exception:
                    raise SkillStoreError("The store operation failed.") from None

        task = asyncio.create_task(owned())
        self._operations.add(task)
        try:
            return await self._join(task)
        finally:
            self._operations.discard(task)

    @staticmethod
    async def _join(task: asyncio.Task[T]) -> T:
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                if cancelled:
                    raise asyncio.CancelledError
                return result
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    with suppress(BaseException):
                        task.result()
                    raise
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise

    async def start(self) -> None:
        if self._started or self._closing:
            raise SkillStoreError("The skill store is already started or closed.")

        def load() -> None:
            self.registry.open()
            try:
                snapshot: dict[str, Skill] = {}
                for name, config in self.registry.stores.items():
                    try:
                        backend = make_backend(config, self.registry.path)
                        self._backends[name] = backend
                        snapshot.update(self._scan(config, backend))
                    except Exception:
                        self._errors[name] = "Store refresh failed."
                self.snapshot = MappingProxyType(snapshot)
                self._started = True
            except BaseException:
                self.registry.close()
                raise

        try:
            await self._mutate(load, opening=True)
        except asyncio.CancelledError:
            await self.close()
            raise
        if not self._closing:
            self._poll_task = asyncio.create_task(self._poll())

    async def close(self) -> None:
        self._closing = True

        async def finish() -> None:
            if self._poll_task is not None:
                self._poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._poll_task
                self._poll_task = None
            if self._operations:
                await asyncio.gather(*tuple(self._operations), return_exceptions=True)
            self.registry.close()
            self._started = False

        if self._close_task is None:
            self._close_task = asyncio.create_task(finish())
        await self._join(self._close_task)

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            configs = self._admin_view[0]
            names = tuple(name for name, config in configs.items() if config.kind == "git")
            for name in names:
                try:
                    await self.refresh_store(name)
                except SkillStoreError:
                    pass

    @staticmethod
    def _scan(config: StoreConfig, backend: object) -> dict[str, Skill]:
        files = backend.list_skills()
        return {
            f"{config.name}/{name}": Skill(config.name, name, tree) for name, tree in files.items()
        }

    def _require_config(self, name: str) -> StoreConfig:
        validate_name(name)
        try:
            return self.registry.stores[name]
        except KeyError:
            raise SkillStoreError("The store does not exist.") from None

    def _backend(self, name: str):
        config = self._require_config(name)
        if name not in self._backends:
            self._backends[name] = make_backend(config, self.registry.path)
        return self._backends[name]

    def _publish_store(self, name: str, skills: Mapping[str, Skill]) -> None:
        snapshot = {key: value for key, value in self.snapshot.items() if value.store != name}
        snapshot.update(skills)
        self.snapshot = MappingProxyType(snapshot)

    def _publish_skill(self, skill: Skill) -> None:
        snapshot = dict(self.snapshot)
        snapshot[skill.qualified_name] = skill
        self.snapshot = MappingProxyType(snapshot)

    def _commit_configuration(
        self,
        stores: Mapping[str, StoreConfig],
        writable: str | None,
        backends: dict[str, object],
        snapshot: Mapping[str, Skill],
        errors: dict[str, str],
    ) -> None:
        try:
            self.registry.save(stores, writable)
        finally:
            if (
                dict(self.registry.stores) == dict(stores)
                and self.registry.writable_store == writable
            ):
                self._backends = backends
                self._errors = errors
                self.snapshot = MappingProxyType(dict(snapshot))

    def list_stores(self) -> dict:
        configs, writable, errors = self._admin_view
        return {
            "stores": [
                {
                    **config.masked(),
                    "writable": name == writable,
                    "writable_capable": config.kind in {"directory", "s3"},
                    "error": errors.get(name),
                }
                for name, config in sorted(configs.items())
            ],
            "writable_store": writable,
        }

    def list_skills(self, offset: int = 0, limit: int = 100) -> dict:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise SkillStoreError("Invalid pagination.")
        snapshot = self.snapshot
        names = sorted(snapshot)
        page = names[offset : offset + limit]
        return {
            "skills": [{"name": key, "description": snapshot[key].description} for key in page],
            "total": len(names),
            "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < len(names) else None,
        }

    def instructions(self, snapshot: Mapping[str, Skill] | None = None) -> str:
        if snapshot is None:
            snapshot = self.snapshot
        names = sorted(snapshot)
        header = (
            "Use a matching skill before starting a task. "
            "Load it with use_skill and its qualified name.\n"
        )
        lines: list[str] = []
        for index, name in enumerate(names):
            line = name + " — " + snapshot[name].description[:60] + "\n"
            remainder = (
                f"… and {len(names) - index - 1} more; call list_skills.\n"
                if index + 1 < len(names)
                else ""
            )
            if (
                len((header + "".join(lines) + line + remainder).encode("utf-8"))
                > INSTRUCTIONS_BYTES
            ):
                lines.append(f"… and {len(names) - index} more; call list_skills.\n")
                break
            lines.append(line)
        if not names:
            lines.append("No skills are available.\n")
        return header + "".join(lines)

    def _resolve(self, name: str) -> Skill:
        if not isinstance(name, str):
            raise SkillStoreError("Invalid skill name.")
        snapshot = self.snapshot
        if "/" in name:
            if name.count("/") != 1:
                raise SkillStoreError("Invalid qualified skill name.")
            store_name, skill_name = name.split("/")
            validate_name(store_name)
            validate_skill_name(skill_name)
            if name in snapshot:
                return snapshot[name]
        else:
            validate_skill_name(name)
            matches = [skill for skill in snapshot.values() if skill.name == name]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                alternatives = ", ".join(sorted(skill.qualified_name for skill in matches))
                raise SkillStoreError("Ambiguous skill name. Use one of: " + alternatives)
        raise SkillStoreError("The skill does not exist.")

    def use_skill(self, name: str) -> dict:
        skill = self._resolve(name)
        return {
            "qualified_name": skill.qualified_name,
            "body": skill.body,
            "files": sorted(path for path in skill.files if path != "SKILL.md"),
        }

    async def add_store(self, config: StoreConfig) -> dict:
        def operation() -> dict:
            if not isinstance(config, StoreConfig):
                raise SkillStoreError("Invalid store configuration.")
            if config.name in self.registry.stores:
                raise SkillStoreError("The store already exists.")
            backend = make_backend(config, self.registry.path)
            scanned = self._scan(config, backend)
            stores = dict(self.registry.stores)
            stores[config.name] = config
            backends = dict(self._backends)
            backends[config.name] = backend
            snapshot = dict(self.snapshot)
            snapshot.update(scanned)
            self._commit_configuration(
                stores, self.registry.writable_store, backends, snapshot, dict(self._errors)
            )
            return {"store": config.masked()}

        return await self._mutate(operation)

    async def update_store(self, name: str, changes: dict) -> dict:
        def operation() -> dict:
            old = self._require_config(name)
            if not isinstance(changes, dict) or any(key in changes for key in ("name", "kind")):
                raise SkillStoreError("Store name and kind cannot change.")
            try:
                config = replace(old, **changes)
            except TypeError:
                raise SkillStoreError("Invalid store fields.") from None
            backend = make_backend(config, self.registry.path)
            scanned = self._scan(config, backend)
            stores = dict(self.registry.stores)
            stores[name] = config
            backends = dict(self._backends)
            backends[name] = backend
            snapshot = {key: value for key, value in self.snapshot.items() if value.store != name}
            snapshot.update(scanned)
            errors = dict(self._errors)
            errors.pop(name, None)
            self._commit_configuration(
                stores, self.registry.writable_store, backends, snapshot, errors
            )
            return {"store": config.masked()}

        return await self._mutate(operation)

    async def remove_store(self, name: str) -> dict:
        def operation() -> dict:
            self._require_config(name)
            stores = dict(self.registry.stores)
            del stores[name]
            backends = dict(self._backends)
            backends.pop(name, None)
            snapshot = {key: value for key, value in self.snapshot.items() if value.store != name}
            errors = dict(self._errors)
            errors.pop(name, None)
            writable = (
                None if self.registry.writable_store == name else self.registry.writable_store
            )
            self._commit_configuration(stores, writable, backends, snapshot, errors)
            return {"removed": name}

        return await self._mutate(operation)

    async def set_writable(self, name: str) -> dict:
        def operation() -> dict:
            config = self._require_config(name)
            if config.kind not in {"directory", "s3"}:
                raise SkillStoreError("The writable store must support writes.")
            self.registry.save(self.registry.stores, name)
            return {"writable_store": name}

        return await self._mutate(operation)

    async def refresh_store(self, name: str) -> dict:
        def operation() -> dict:
            config = self._require_config(name)
            try:
                backend = self._backend(name)
                backend.refresh()
                scanned = self._scan(config, backend)
            except Exception:
                self._errors[name] = "Store refresh failed."
                raise SkillStoreError("Store refresh failed.") from None
            self._publish_store(name, scanned)
            self._errors.pop(name, None)
            return {"store": name, "skills": len(scanned)}

        return await self._mutate(operation)

    def _writable(self, target: str | None = None) -> str:
        writable = self.registry.writable_store
        if writable is None:
            raise SkillStoreError("No writable store is selected.")
        if target is not None and target != writable:
            raise SkillStoreError("The target must be the writable store.")
        config = self._require_config(writable)
        if config.kind not in {"directory", "s3"}:
            raise SkillStoreError("The writable store must support writes.")
        return writable

    def _check_capacity(self, skill: Skill) -> None:
        skills = [
            value
            for key, value in self.snapshot.items()
            if value.store == skill.store and key != skill.qualified_name
        ]
        if len(skills) + 1 > MAX_SKILLS_PER_STORE:
            raise SkillStoreError("Store skill count limit exceeded.")
        size = sum(len(content) for entry in skills for content in entry.files.values())
        if size + sum(map(len, skill.files.values())) > MAX_STORE_BYTES:
            raise SkillStoreError("Store size limit exceeded.")

    async def write_skill(
        self, name: str, files: dict[str, bytes], replace: bool = False, message: str = ""
    ) -> dict:
        validate_skill_name(name)
        tree = validate_files(files)
        if type(replace) is not bool or not isinstance(message, str):
            raise SkillStoreError("Invalid write options.")

        def operation() -> dict:
            writable = self._writable()
            skill = Skill(writable, name, tree)
            self._check_capacity(skill)
            backend = self._backend(writable)
            backend.write_tree(name, dict(skill.files), replace=replace)
            self._publish_skill(skill)
            self._errors.pop(writable, None)
            return {"qualified_name": skill.qualified_name, "files": len(skill.files)}

        return await self._mutate(operation)

    def _migration_stores(self, from_store: str, to_store: str):
        source_config = self._require_config(from_store)
        target_config = self._require_config(to_store)
        self._writable(to_store)
        if from_store == to_store or backends_overlap(source_config, target_config):
            raise SkillStoreError("Migration stores must not overlap.")
        return self._backend(from_store), self._backend(to_store)

    def _migrate_one(self, from_store: str, name: str, to_store: str) -> dict:
        validate_skill_name(name)
        source, target = self._migration_stores(from_store, to_store)
        source_files = source.list_skills().get(name)
        if source_files is None:
            raise SkillStoreError("The source skill does not exist.")
        destination = Skill(to_store, name, source_files)
        self._check_capacity(destination)
        existing = target.list_skills().get(name)
        if existing is not None:
            if existing != dict(destination.files):
                raise SkillStoreError("The target skill has different content.")
        else:
            target.write_tree(name, dict(destination.files), replace=False)
        target.verify_tree(name, dict(destination.files))
        # A verified target remains visible even if source deletion fails.
        self._publish_skill(destination)
        copied = self.registry.stores[from_store].kind == "git"
        if not copied:
            source.verify_tree(name, source_files)
            source.delete_skill(name)
            snapshot = dict(self.snapshot)
            snapshot.pop(f"{from_store}/{name}", None)
            self.snapshot = MappingProxyType(snapshot)
        return {
            "from_store": from_store,
            "qualified_name": destination.qualified_name,
            "copied": copied,
            "moved": not copied,
        }

    async def migrate_skill(self, from_store: str, name: str, to_store: str) -> dict:
        return await self._mutate(lambda: self._migrate_one(from_store, name, to_store))

    async def migrate_store(self, from_store: str, to_store: str) -> dict:
        def operation() -> dict:
            source, _ = self._migration_stores(from_store, to_store)
            names = sorted(source.list_skills())
            moved = copied = 0
            for index, name in enumerate(names):
                try:
                    result = self._migrate_one(from_store, name, to_store)
                except Exception:
                    return {
                        "moved": moved,
                        "copied": copied,
                        "remaining": len(names) - index,
                        "error": "Migration stopped. Completed copies remain at the target.",
                    }
                moved += int(result["moved"])
                copied += int(result["copied"])
            return {"moved": moved, "copied": copied, "remaining": 0, "error": None}

        return await self._mutate(operation)
