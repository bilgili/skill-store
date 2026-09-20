"""Protocol adapters over the store's immutable catalog and mutation owner."""

from __future__ import annotations

import base64
import binascii
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.prompts import Prompt
from fastmcp.resources import Resource
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from pydantic import Field

from .models import MAX_FILE_BYTES, Skill, SkillStoreError, StoreConfig, validate_files
from .store import SkillStore

Password = Annotated[str, Field(json_schema_extra={"format": "password"})]
OptionalPassword = Annotated[str | None, Field(json_schema_extra={"format": "password"})]
StoreKind = Literal["directory", "git", "s3"]
ACTION_META = {"mcpflow": {"action": True}}


def qualified_resource_uri(qualified_name: str, path: str, namespace: str = "") -> str:
    parts = [*qualified_name.split("/"), *path.split("/")]
    if namespace:
        parts.insert(0, namespace)
    return "skill://" + "/".join(quote(part, safe="") for part in parts)


def resource_uri(skill: Skill, path: str, namespace: str = "") -> str:
    return qualified_resource_uri(skill.qualified_name, path, namespace)


def decode_files(files: dict[str, str], binary_files: dict[str, str] | None) -> dict[str, bytes]:
    """Decode explicit encodings before handing bytes to the persistence owner."""
    binary_files = binary_files or {}
    if files.keys() & binary_files.keys():
        raise SkillStoreError("A path cannot occur in both text and binary files.")
    decoded: dict[str, bytes] = {}
    for path, value in files.items():
        if len(value) > MAX_FILE_BYTES:
            raise SkillStoreError("A file exceeds the size limit.")
        decoded[path] = value.encode("utf-8")
    for path, value in binary_files.items():
        if len(value) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise SkillStoreError("A file exceeds the size limit.")
        try:
            decoded[path] = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise SkillStoreError("A binary file is not valid base64.") from None
    return validate_files(decoded)


class LiveInstructions(Middleware):
    """Initialization reads the same catalog owner as instructions://self."""

    def __init__(self, store: SkillStore):
        self.store = store

    async def on_initialize(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        if result is not None:
            result.instructions = self.store.instructions()
        return result

    async def on_discover(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        if result is not None:
            result.instructions = self.store.instructions()
        return result


class SkillProvider(Provider):
    """Capture a catalog once per request; resource objects retain their bytes."""

    def __init__(self, store: SkillStore, ordinary_tools: list[Tool], prompt_separator: str = "/"):
        super().__init__()
        self.store = store
        self.ordinary_tools = ordinary_tools
        self.prompt_separator = prompt_separator

    def _resource(self, skill: Skill, path: str) -> Resource:
        content = skill.files[path]

        def read() -> bytes:
            return content

        return Resource.from_function(
            read,
            uri=resource_uri(skill, path),
            name=f"{skill.qualified_name}/{path}",
            mime_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
        )

    def _prompt(self, skill: Skill, name: str | None = None) -> Prompt:
        body = skill.body

        def render() -> str:
            return body

        return Prompt.from_function(
            render,
            name=name or f"{skill.store}{self.prompt_separator}{skill.name}",
            description=skill.description,
        )

    def _instructions_resource(self, snapshot=None) -> Resource:
        content = self.store.instructions(snapshot)

        def read() -> str:
            return content

        return Resource.from_function(
            read, uri="instructions://self", name="instructions", mime_type="text/plain"
        )

    async def _list_tools(self):
        catalog = self.store.instructions()
        return [
            tool.model_copy(
                update={"description": f"Load a skill and its supporting resources.\n\n{catalog}"}
            )
            if tool.name == "use_skill"
            else tool
            for tool in self.ordinary_tools
        ]

    async def _get_tool(self, name, version=None):
        return next((tool for tool in await self._list_tools() if tool.name == name), None)

    async def _list_resources(self):
        snapshot = self.store.snapshot
        return [self._instructions_resource(snapshot)] + [
            self._resource(skill, path)
            for skill in snapshot.values()
            for path in sorted(skill.files)
            if path != "SKILL.md"
        ]

    async def _get_resource(self, uri, version=None):
        requested = str(uri)
        if requested == "instructions://self":
            return self._instructions_resource()
        snapshot = self.store.snapshot
        for skill in snapshot.values():
            for path in skill.files:
                if path != "SKILL.md" and resource_uri(skill, path) == requested:
                    return self._resource(skill, path)
        return None

    async def _list_prompts(self):
        snapshot = self.store.snapshot
        return [self._prompt(skill) for skill in snapshot.values()]

    async def _get_prompt(self, name, version=None):
        snapshot = self.store.snapshot
        skill = snapshot.get(name)
        if skill is None and "__" in name:
            store, skill_name = name.split("__", 1)
            skill = snapshot.get(f"{store}/{skill_name}")
        return self._prompt(skill, name) if skill is not None else None


def create_server(
    state_dir: Path, namespace: str = "skills", prompt_separator: str = "/"
) -> FastMCP:
    """Build the stdio server; lifespan owns the process lock and poll task."""
    if namespace and not all(c.isalnum() or c in "-_" for c in namespace):
        raise ValueError("Invalid gateway namespace.")
    if prompt_separator not in {"/", "__"}:
        raise ValueError("The prompt separator must be / or __.")
    store = SkillStore(Path(state_dir))

    @asynccontextmanager
    async def lifespan(server):
        await store.start()
        try:
            yield {"store": store}
        finally:
            await store.close()

    server = FastMCP(
        "Skill Store",
        instructions=" ".join(
            (
                "Load remote skills with use_skill.",
                "Read instructions://self for the live catalog.",
            )
        ),
        lifespan=lifespan,
        middleware=[LiveInstructions(store)],
        mask_error_details=True,
    )

    async def use_skill(name: str) -> dict[str, Any]:
        """Load a qualified skill or an unambiguous short name."""
        try:
            # Consume one resolved snapshot; a worker can publish while this function runs.
            result = store.use_skill(name)
            return {
                "name": result["qualified_name"],
                "body": result["body"],
                "resources": [
                    {
                        "path": path,
                        "uri": qualified_resource_uri(result["qualified_name"], path, namespace),
                        "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                    }
                    for path in sorted(result["files"])
                ],
            }
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    async def list_skills(offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """List qualified names and descriptions with pagination."""
        try:
            return store.list_skills(offset=offset, limit=limit)
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    async def write_skill(
        name: str,
        files: dict[str, str],
        message: str = "",
        replace: bool = False,
        binary_files: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Write a complete skill into the writable store; binary files use base64."""
        try:
            return await store.write_skill(
                name, decode_files(files, binary_files), replace=replace, message=message
            )
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    ordinary = [Tool.from_function(fn) for fn in (use_skill, list_skills, write_skill)]
    server.add_provider(SkillProvider(store, ordinary, prompt_separator))

    @server.tool(meta=ACTION_META)
    async def add_store(
        name: str,
        kind: StoreKind,
        path: str = "",
        url: str = "",
        ref: str = "HEAD",
        credential: Password = "",
        endpoint: str = "",
        bucket: str = "",
        prefix: str = "",
        access_key: Password = "",
        secret_key: Password = "",
        region: str = "us-east-1",
    ) -> dict[str, Any]:
        """Register a directory, Git, or S3 store without a restart."""
        try:
            return await store.add_store(
                StoreConfig(
                    name=name,
                    kind=kind,
                    path=path,
                    url=url,
                    ref=ref,
                    credential=credential,
                    endpoint=endpoint,
                    bucket=bucket,
                    prefix=prefix,
                    access_key=access_key,
                    secret_key=secret_key,
                    region=region,
                )
            )
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(meta=ACTION_META)
    async def update_store(
        name: str,
        path: str | None = None,
        url: str | None = None,
        ref: str | None = None,
        credential: OptionalPassword = None,
        endpoint: str | None = None,
        bucket: str | None = None,
        prefix: str | None = None,
        access_key: OptionalPassword = None,
        secret_key: OptionalPassword = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        """Update specified fields. Omitted fields retain their existing values."""
        changes = {
            key: value
            for key, value in {
                "path": path,
                "url": url,
                "ref": ref,
                "credential": credential,
                "endpoint": endpoint,
                "bucket": bucket,
                "prefix": prefix,
                "access_key": access_key,
                "secret_key": secret_key,
                "region": region,
            }.items()
            if value is not None
        }
        try:
            return await store.update_store(name, changes)
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(meta=ACTION_META)
    async def remove_store(name: str) -> dict[str, Any]:
        """Remove a registration without deleting backend data."""
        try:
            return await store.remove_store(name)
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(meta=ACTION_META)
    async def set_writable(name: str) -> dict[str, Any]:
        """Select one writable-capable store. Git stores are read-only."""
        try:
            return await store.set_writable(name)
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(meta=ACTION_META)
    async def refresh_store(name: str) -> dict[str, Any]:
        """Refresh a store and atomically publish the rebuilt catalog."""
        try:
            return await store.refresh_store(name)
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(meta=ACTION_META)
    async def migrate_skill(from_store: str, name: str, to_store: str) -> dict[str, Any]:
        """Copy and verify a skill before removing a writable source."""
        try:
            return await store.migrate_skill(from_store, name, to_store)
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(meta=ACTION_META)
    async def migrate_store(from_store: str, to_store: str) -> dict[str, Any]:
        """Migrate each skill, stopping at the first error with progress counts."""
        try:
            return await store.migrate_store(from_store, to_store)
        except SkillStoreError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(meta=ACTION_META)
    async def list_stores() -> dict[str, Any]:
        """List store configuration with credentials masked."""
        return store.list_stores()

    return server
