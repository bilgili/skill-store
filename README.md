# Skill Store

Skill Store serves persistent skills through the Model Context Protocol (MCP).
It runs over standard input and output as one mcpflow child.
The store supports directory, read-only Git, and S3 backends.

## Start

Install Python 3.12 or later.
Run `uv pip install .` in your environment.
Create separate state and library directories:

```sh
mkdir -p /data/skill-state /data/skill-library
SKILLS_STATE_DIR=/data/skill-state skill-store
```

The state directory must exist and permit writes.
Run only one process per state directory.
Do not place a directory store inside the state directory.

## Gateway setup

Register the server under namespace `skills`.
Use `package: skill-store`, `actions: true`, and `cache_ttl: 0`.
Set `SKILLS_STATE_DIR` to the persistent state directory.
The production source is a pinned commit in the separate skill-store repository.
For local testing, use an installed executable as a custom child.

Open the child's Actions page.
Call `add_store` with name `local`, kind `directory`, and the absolute library path.
Call `set_writable` with name `local`.

## Agent tools

`use_skill(name)` returns the skill body and supporting resource addresses.
Use qualified names such as `local/diagnosing-bugs`.
An ambiguous short name returns all matching qualified names.
`list_skills(offset, limit)` returns a page of the catalog.

`write_skill(name, files, message, replace)` writes into the selected writable store.
The `files` object maps relative paths to UTF-8 text.
Include `SKILL.md` with YAML front matter and a description.
Use the optional `binary_files` object for base64 content.
Set `replace` to true to replace an existing skill and remove obsolete files.
The message is a caller note. The store does not provide version history.

The server exposes one prompt per skill and every supporting file as a concrete resource.
Its live catalog is available at `instructions://self`.
Gateway resource addresses start with `skill://skills/`.
Set `SKILLS_NAMESPACE` to a different gateway namespace when required.
Set it to an empty string for direct stdio clients.
Set `SKILLS_PROMPT_SEPARATOR=__` if a client rejects slash-separated prompt names.
This setting changes the listed prompt names, so gateway lookup supports the fallback.

## Administration

The eight actions are `add_store`, `update_store`, `remove_store`, `set_writable`,
`refresh_store`, `migrate_skill`, `migrate_store`, and `list_stores`.
The gateway restricts these actions to administrators.
The store itself has no authentication or HTTP listener.

Store credentials remain in `stores.json` with file mode 0600.
Protect volume snapshots because this file contains plaintext credentials.
Action results mask credential fields.
Removing a store removes its registration and preserves its contents.
Removing the writable store clears the writable selection.
Use `update_store` to change a location, ref, or credential.
Remove and add a store to change its name or kind.

Migration requires the destination to be writable.
It copies and verifies every file before deleting a writable source.
Git sources remain intact.
An identical destination supports retry after interruption.
A different destination requires explicit replacement through `write_skill` first.

## Limits and failure behavior

The store permits 8 MiB per file and 32 MiB per skill.
It permits 256 files per skill, 1,024 skills per store, and 256 MiB per store.
The registry permits 128 stores and at most 1 MiB of configuration.
Loaded skill bytes remain in memory to keep reads independent of backend latency.
Instructions use at most 8,192 bytes. Use `list_skills` for the complete catalog.

Directory replacement uses an atomic native exchange on macOS and Linux.
An unsupported filesystem refuses replacement before changing the public tree.
S3 replacement writes supporting files before `SKILL.md`, then removes obsolete objects.
External readers can observe mixed generations during a failed or interrupted S3 replacement.
Refresh or restart can import that mixed state. S3 has no transaction across objects.
Use S3 versioning or volume snapshots when recovery history is required.

Git stores refresh every 60 seconds and support immediate `refresh_store` calls.
A failed backend refresh preserves the last published catalog for that store.
Backend calls have request limits and finite scan deadlines.
No agent needs a local skill directory or an offline cache.

## Verification

```sh
uv pip install '.[dev]'
pytest
ruff check src tests
python openspec/changes/remote-skill-store/check_models.py
```

Start disposable MinIO and run the integration test:

```sh
docker compose -f compose.minio.yml up -d
SKILL_STORE_MINIO_ENDPOINT=http://127.0.0.1:19000 \
SKILL_STORE_MINIO_ACCESS_KEY=skillstoretest \
SKILL_STORE_MINIO_SECRET_KEY=skillstore-local-test-only pytest -m integration
docker compose -f compose.minio.yml down
```

These credentials are for local disposable tests only.
The service listens on loopback and stores no persistent data.

Run the stdio demonstration with a new directory:

```sh
python scripts/demo.py --root /tmp/skill-store-demo
```

The demonstration starts three real child processes.
It verifies restart persistence, prompt content, resource bytes, and migration.
It writes a report into the demonstration directory.

Import a local library without changing its source:

```sh
python scripts/import_skills.py --source ~/.agents/skills \
  --target /data/shared-skills --report /data/shared-import.json
python scripts/import_skills.py --source ~/.claude/skills --skip-linked-skills \
  --target /data/claude-skills --report /data/claude-import.json
```

Each target must be new. Register the two targets under different store names.
This preserves same-named skills without a hidden precedence rule.
The importer verifies every copied file with SHA-256.
Retain local source directories until deployed client acceptance succeeds.

The original product documents in `docs/` are historical references.
The implementation design records corrections to their persistence assumptions.
