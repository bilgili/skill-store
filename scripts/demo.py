"""Reproduce a durable MCP write, restart, read, and migration over real stdio."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def demonstrate(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    state, source, target = (root / name for name in ("state", "source", "target"))
    for path in (state, source, target):
        path.mkdir(exist_ok=True)
    if (state / "stores.json").exists():
        raise SystemExit("Use a new demo directory. Existing state is preserved.")

    def transport():
        return StdioTransport(
            command=sys.executable,
            args=["-m", "skill_store"],
            keep_alive=False,
            env={"SKILLS_STATE_DIR": str(state), "SKILLS_NAMESPACE": ""},
        )

    body = (
        "---\nname: demo\ndescription: Use when testing a persistent remote skill.\n---\n# Demo\n"
    )
    script = "#!/bin/sh\nprintf 'remote skill works\\n'\n"
    async with Client(transport()) as client:
        for name, path in (("source", source), ("target", target)):
            await client.call_tool(
                "add_store",
                {
                    "name": name,
                    "kind": "directory",
                    "path": str(path),
                },
            )
        await client.call_tool("set_writable", {"name": "source"})
        await client.call_tool(
            "write_skill",
            {
                "name": "demo",
                "message": "demonstrate durable persistence",
                "files": {"SKILL.md": body, "scripts/run.sh": script},
            },
        )
        assert (source / "demo" / "SKILL.md").read_text() == body
    async with Client(transport()) as client:
        assert "source/demo" in client.instructions
        prompt = await client.get_prompt("source/demo")
        assert prompt.messages[0].content.text == body
        resource = await client.read_resource("skill://source/demo/scripts/run.sh")
        item = resource[0]
        content = item.text.encode() if hasattr(item, "text") else base64.b64decode(item.blob)
        assert content == script.encode()
        await client.call_tool("set_writable", {"name": "target"})
        await client.call_tool(
            "migrate_skill",
            {
                "from_store": "source",
                "name": "demo",
                "to_store": "target",
            },
        )
        assert not (source / "demo").exists()
        assert (target / "demo" / "scripts/run.sh").read_text() == script
    async with Client(transport()) as client:
        assert "target/demo" in client.instructions
        assert "source/demo" not in client.instructions
    result = {
        "status": "passed",
        "state": str(state),
        "skill": "target/demo",
        "process_starts": 3,
        "source_removed_after_verification": True,
        "files": {
            "SKILL.md": hashlib.sha256(body.encode()).hexdigest(),
            "scripts/run.sh": hashlib.sha256(script.encode()).hexdigest(),
        },
    }
    (root / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    print(json.dumps(asyncio.run(demonstrate(parser.parse_args().root.resolve())), indent=2))
