"""Exercise the actual MCP adapter with durable stores, not fixture actions."""

import base64
import json

import pytest
from fastmcp import Client

from skill_store.server import create_server

ACTIONS = {
    "add_store",
    "update_store",
    "remove_store",
    "set_writable",
    "refresh_store",
    "migrate_skill",
    "migrate_store",
    "list_stores",
}
BODY = "---\nname: demo\ndescription: Use when a demonstration is requested.\n---\n# Demo\n"


def data(result):
    return result.data if result.data is not None else json.loads(result.content[0].text)


@pytest.fixture
def paths(tmp_path):
    state = tmp_path / "state"
    library = tmp_path / "library"
    state.mkdir()
    library.mkdir()
    return state, library


async def configure(client, library):
    await client.call_tool(
        "add_store", {"name": "local", "kind": "directory", "path": str(library)}
    )
    await client.call_tool("set_writable", {"name": "local"})


async def test_real_protocol_roundtrip_and_restart(paths):
    state, library = paths
    async with Client(create_server(state, namespace="")) as client:
        await configure(client, library)
        await client.call_tool(
            "write_skill",
            {
                "name": "demo",
                "files": {"SKILL.md": BODY, "scripts/run.sh": "#!/bin/sh\necho demo\n"},
                "message": "create demo",
                "binary_files": {"assets/payload.bin": base64.b64encode(b"\x00\xff\x01").decode()},
            },
        )
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == ACTIONS | {
            "use_skill",
            "write_skill",
            "list_skills",
        }
        assert "local/demo" in next(tool.description for tool in tools if tool.name == "use_skill")
        prompts = await client.list_prompts()
        assert [prompt.name for prompt in prompts] == ["local/demo"]
        assert (await client.get_prompt("local/demo")).messages[0].content.text == BODY
        assert (await client.get_prompt("local__demo")).messages[0].content.text == BODY
        resources = await client.list_resources()
        assert {str(resource.uri) for resource in resources} == {
            "instructions://self",
            "skill://local/demo/scripts/run.sh",
            "skill://local/demo/assets/payload.bin",
        }
        assert "local/demo" in (await client.read_resource("instructions://self"))[0].text
        binary = (await client.read_resource("skill://local/demo/assets/payload.bin"))[0]
        assert base64.b64decode(binary.blob) == b"\x00\xff\x01"
        skill = data(await client.call_tool("use_skill", {"name": "demo"}))
        assert skill["body"] == BODY
        assert {x["uri"] for x in skill["resources"]} == {
            "skill://local/demo/scripts/run.sh",
            "skill://local/demo/assets/payload.bin",
        }
        await client.call_tool(
            "write_skill",
            {
                "name": "demo",
                "files": {"SKILL.md": BODY + "New\n"},
                "replace": True,
            },
        )
        assert len(await client.list_resources()) == 1
    assert (library / "demo" / "SKILL.md").read_text() == BODY + "New\n"
    async with Client(create_server(state)) as client:
        assert "local/demo" in client.instructions
        assert (
            data(await client.call_tool("use_skill", {"name": "local/demo"}))["body"]
            == BODY + "New\n"
        )
        stores = data(await client.call_tool("list_stores"))
        assert "local" in json.dumps(stores)


async def test_action_contract_and_secret_masking(paths):
    state, library = paths
    async with Client(create_server(state)) as client:
        tools = await client.list_tools()
        for tool in tools:
            if tool.name not in ACTIONS:
                assert not (tool.meta or {}).get("mcpflow", {}).get("action")
                continue
            assert tool.meta["mcpflow"]["action"] is True
            assert "ui" not in tool.meta
            assert "tool_hash" not in tool.meta.get("fastmcp", {})
            for key, prop in tool.input_schema.get("properties", {}).items():
                variants = prop.get("anyOf", [prop])
                assert all(
                    v.get("type") in {"string", "number", "integer", "boolean", "null"}
                    for v in variants
                )
                if key in {"credential", "access_key", "secret_key"}:
                    assert prop.get("format") == "password"
        await configure(client, library)
        # Even credentials supplied to a directory registration must never be disclosed.
        await client.call_tool("update_store", {"name": "local", "credential": "NEVER-ECHO-123"})
        result = await client.call_tool("list_stores")
        assert "NEVER-ECHO-123" not in str(result)


async def test_invalid_inputs_leave_tree_intact(paths):
    state, library = paths
    async with Client(create_server(state)) as client:
        await configure(client, library)
        for args in [
            {"name": "../escape", "files": {"SKILL.md": BODY}},
            {"name": "demo", "files": {"SKILL.md": BODY, "../escape": "x"}},
            {"name": "demo", "files": {"SKILL.md": BODY}, "binary_files": {"x": "not base64"}},
            {"name": "demo", "files": {"SKILL.md": BODY, "x": "x"}, "binary_files": {"x": "eA=="}},
        ]:
            result = await client.call_tool("write_skill", args, raise_on_error=False)
            assert result.is_error
        assert list(library.iterdir()) == []
        assert data(await client.call_tool("list_skills"))["total"] == 0


async def test_gateway_resource_addresses(paths):
    state, library = paths
    async with Client(create_server(state)) as client:
        await configure(client, library)
        await client.call_tool(
            "write_skill",
            {
                "name": "demo",
                "files": {"SKILL.md": BODY, "references/a b.txt": "spaces"},
            },
        )
        skill = data(await client.call_tool("use_skill", {"name": "demo"}))
        assert skill["resources"][0]["uri"] == "skill://skills/local/demo/references/a%20b.txt"
