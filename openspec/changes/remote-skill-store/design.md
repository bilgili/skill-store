# Design

## Ownership and data flow
The independent `skill_store` package owns skill semantics.
`Registry` owns validated store definitions and the writable flag.
The registry accepts at most 128 stores and one MiB of encoded configuration.
Store updates preserve name and kind. Remove and add a store to change its kind.
`Backend` owns durable file operations.
`SkillStore` serializes mutations and publishes an immutable catalog snapshot.
`SkillProvider` serves prompts and concrete resources from that snapshot.
The server publishes ordinary discovery tools and eight flat management actions.
The gateway grants actions through its trusted catalog entry.

Agent reads enter the server and read one snapshot without the mutation lock.
Agent writes enter `SkillStore`, select the writable backend, persist files, and publish a new snapshot.
Admin actions enter the same mutation owner.
Admin reads capture one immutable view of configuration, writable selection, and store errors.
Registry updates stage a mode-0600 file and synchronize it.
They atomically replace `stores.json` and synchronize its parent directory before publication.
A registry commit can complete before memory publication. Recovery loads the committed file.
If directory synchronization fails after replacement, report failure and reconcile memory with the installed file.
This case does not promise crash durability.
One process holds an operating-system lock for each state directory.
Cancellation must not release mutation ownership while a worker continues durable writes.
The owner waits for the worker after cancellation, including repeated cancellation requests.
It publishes completed changes before it releases ownership and propagates cancellation.

## Modules and interfaces
`models.py` defines immutable `StoreConfig`, `Skill`, and shared validation.
StoreConfig fields: name, kind, path, url, ref, credential, endpoint, bucket, prefix, access_key, secret_key, region.
StoreConfig is frozen. Optional location fields default to empty strings.
The default Git ref is `HEAD`. The default region is `us-east-1`.
`backends.py` exposes `make_backend(config, state_dir)` and synchronous backend operations.
Backend operations are `list_skills() -> dict[str, dict[str, bytes]]`, `read_file(skill, path) -> bytes`,
`write_tree(skill, files, replace=False)`, `delete_skill(skill)`, `verify_tree(skill, files)`, and `refresh()`.
Backends expose `writable_capable`.
`registry.py` validates and durably saves configuration.
`store.py` exposes async operations and a snapshot mapping qualified names to immutable skills.
`server.py` maps MCP tools, prompts, and resources onto `SkillStore`.

## File safety and persistence
Validate store names as `[a-z0-9][a-z0-9-]*`.
Reject absolute paths, traversal, symlinks, special files, and duplicate normalized paths.
Every skill contains a UTF-8 `SKILL.md` file.
Support UTF-8 text and explicit base64 binary inputs.
Directory writes stage complete sibling trees and atomically exchange existing trees on supported platforms.
Stage on the same filesystem. Synchronize each file, staged directory, and parent directory.
Use native directory exchange on macOS and Linux. Refuse replacement if atomic exchange is unavailable.
Exchange support depends on the filesystem. Test the failure path without modifying the public tree.
A completed exchange publishes the new tree. Cleanup of the displaced tree can follow.
S3 writes supporting objects first and `SKILL.md` last, then remove obsolete objects.
S3 deletion removes `SKILL.md` first.
S3 replacement permits mixed generations for external readers during publication.
An interrupted replacement can retain mixed generations until a successful replacement repairs it.
S3 refresh and restart can load those mixed generations. The flat layout has no durable generation marker.
Existing snapshot readers retain their previous bytes throughout backend writes.
Successful writes publish a snapshot from their exact input bytes. They do not enumerate S3 again.
One request reads one snapshot. Separate requests can observe different snapshots.
Do not claim that a complete MCP handshake pins one generation.
Migration copies and verifies every file before source deletion. Git sources survive migration.
Verification uses point reads and compares bytes or a trusted content checksum.
Object length alone does not prove content equality.
An identical target permits retry. A different target requires explicit replacement.
After partial source deletion, retain the complete target. Report partial progress without compensating deletion.
Retry migration safely after partial completion. Never overwrite differing target content without explicit replacement.
Reject migrations between aliases of overlapping storage locations.
Compare canonical directory roots and normalized S3 endpoint, bucket, and prefix values.
Reject directory stores that overlap the state directory or Git cache directories.
Directory operations use directory descriptors and reject symbolic links.
Git uses bare repositories and reads trees without checkouts or hooks.

## Bounds and secrets
Bound Git commands, S3 requests, retries, and backend scans.
Limit each file to 8 MiB and each skill to 32 MiB.
Limit each store to 256 MiB, each skill to 256 files, and each store to 1,024 skills.
Limit scans to 60 seconds and Git commands to 15 seconds.
Use S3 connection and read timeouts of 3 and 5 seconds. Permit at most two attempts.
Run blocking operations outside the event loop.
Keep the previous snapshot when one store refresh fails. Report a fixed per-store error.
Never return raw backend exceptions or configuration credentials.
Mask credentials and URL user information in every configuration result.
Action secret fields use `format: password`. Actions have flat schemas and no hash or UI visibility metadata.
Poll Git stores every 60 seconds. A refresh action supports immediate reconciliation.

## Discovery
Publish `use_skill`, `write_skill`, and paginated `list_skills`.
Publish a concrete `instructions://self` resource with an 8192-byte catalog limit.
Use qualified names and reject ambiguous short names with qualified alternatives.
Publish one prompt per skill and every supporting file as a concrete resource.
Bare resource URIs use `skill://<store>/<name>/<path>`.
The gateway namespace produces `skill://skills/<store>/<name>/<path>`.
Supporting file references returned through tools must identify gateway resource URIs explicitly.
Support qualified and double-underscore prompt lookup aliases without duplicate list entries.

## Decisions
Full immutable content snapshots simplify read consistency and isolate readers from slow backends.
This costs memory proportional to loaded skill contents. Enforce documented file and store limits.
Native directory exchange preserves existing plain directory layouts and atomic replacement.
A two-rename replacement is rejected because a crash can leave the public path missing.
Use one mutation owner instead of separate registry and backend locks.
Use the existing GitLab host and the repository name `skill-store` for delivery.
Keep local skill directories until deployed client acceptance succeeds.

## Independent design gate
The review and model results are in `design-review.md`.
The models cover registry commits, cancellation, publication, crash recovery, and migration.
They distinguish accepted S3 limitations from implementation defects.
The models abstract filesystem atomicity and storage integrity. Backend tests must establish those assumptions.

## Legacy description parsing
`Skill` owns front-matter parsing and preserves the original `SKILL.md` bytes.
The parser first uses the safe YAML loader.
One fallback accepts an unquoted colon in a single top-level plain `description` line.
The YAML scanner must identify that colon as the failure location.
The fallback replaces that line with an empty string before it validates all remaining YAML.
It then uses the complete original description text and normalizes whitespace.
The fallback rejects duplicate description lines, quoted values, structural prefixes, and other invalid YAML.
This parsing clarification has no state transitions. It requires no additional TLA+ model.
