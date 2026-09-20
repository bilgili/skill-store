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
Directory backends reject a skill name whose case-folded form matches another directory entry.
The backend checks this rule before create, replacement, deletion, and migration-target mutation.
Migration checks the target skill slot before it records an intent.
Backend contract tests establish this filesystem assumption on case-sensitive and case-insensitive hosts.
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

## Recoverable migration deletion

The core persists one migration intent in `migration.json` before it writes a target.
The file uses mode 0600, file synchronization, atomic replacement, and parent synchronization.
The intent identifies the source store, target store, skill name, phase, paths, sizes, and SHA-256 digests.
It contains no backend credential and no skill content.

The `copying` phase permits target construction but forbids source deletion.
The core verifies the complete target tree before it persists the `deleting` phase.
Only the `deleting` phase permits source deletion.
The core retains the intent until source cleanup finishes.
Cleanup and journal removal are separate durable transitions.
The directory backend derives one retired-tree name from the validated skill name.
The retired-tree name is stable across process restarts and contains no skill content.
The backend removes an existing retired tree before a new deletion.
After a committed deletion error, a retry removes the same retired tree before it reports completion.
The backend owns this cleanup. The core clears the intent only after the backend confirms cleanup.

S3 deletion is idempotent when `SKILL.md` is already absent.
It lists and deletes all remaining keys under the recorded skill prefix.
A retry therefore completes cleanup after a partial document-first deletion.
The core reconciles a committed deletion error by removing the source catalog entry.
It keeps the intent so the next call or restart removes remaining objects.

Startup resumes the durable intent before it starts Git polling.
The core clears volatile verification state after restart.
It verifies the target against recorded digests before any resumed source deletion.
It also completes target durability reconciliation before it persists or resumes `deleting`.
It rewrites and synchronizes a resumed `deleting` intent before source deletion.
A failed parent synchronization therefore blocks source deletion.
A mismatched target preserves the source and the durable intent.
A target mismatch during resumed deletion blocks source deletion.
A target with different content is never overwritten without explicit replacement authorization.
A matching `migrate_skill` call resumes the intent.
`migrate_store` resumes a matching intent before it enumerates visible source skills.
The core rejects another migration while an intent exists.
It rejects an update, removal, or writable-store change that can invalidate the intent.
It permits credential repair when the store identity remains unchanged.

`MigrationJournal.tla` models the durable phase across crash, reload, partial source deletion, and retry.
The model checks that deletion requires a verified target and a durable `deleting` intent.
It checks that a completed migration has no source object or journal record.
It also checks that a completed migration has no retired tree or object residue.
The liveness check assumes at most one crash and stable storage after restart.
It applies weak fairness to retry, verification, deletion, cleanup, and journal removal.
Negative configurations omit the journal, bypass mismatch checks, clear it early, or retain residue.

## Namespace commit outcomes

A backend owns the distinction between a failed operation and a committed operation with an uncertain durability result.
Directory creation commits when the staged directory becomes the public directory.
Directory replacement commits when native exchange succeeds.
Directory deletion commits when the public directory moves to its retired location.

After these commit points, synchronization or cleanup failure raises `BackendCommittedError`, a subtype of `BackendError`.
The error carries a fixed public message. It contains no paths or credentials.
The backend never rolls back a committed namespace change.
Rollback creates another visible transition and can fail independently.

The core catches this subtype while it still owns the mutation lock.
After a committed write error, it publishes the exact input bytes and reports uncertain durability.
After a committed source deletion error, it removes the source catalog entry and reports uncertain durability.
A migration target write with this error publishes the target but preserves the source.
Migration does not delete the source after an uncertain target write.
A later retry must verify and reconcile durability before deleting the source.
For a directory target, reconciliation synchronizes every public file, directory, and the store root.
For S3, successful point reads follow the service durability contract.

This rule also covers errors during cleanup of a displaced or retired tree.
The old snapshot remains valid while the worker runs. Error completion requires reconciliation with the committed namespace.
The amended publication and migration models check this requirement for accepted behavior and deliberately omitted reconciliation.
