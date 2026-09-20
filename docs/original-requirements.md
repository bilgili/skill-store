# Deep Interview Spec: Remote Skill Store

## Metadata
- Interview ID: 3f1c2a7e-skilldb-2026-09-13
- Rounds: 20 (hard cap) plus Round 0 topology, revised once
- Final Ambiguity Score: 20%
- Type: brownfield
- Generated: 2026-09-16 (revision 4: multi-store, runtime store management, mcpflow actions page)
- Threshold: 0.2
- Threshold Source: default
- Initial Context Summarized: no
- Status: PASSED (at threshold, hard cap reached; two stated assumptions)
- Supersedes: revision 3 (single remote git repository with push)

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.85 | 0.35 | 0.298 |
| Constraint Clarity | 0.80 | 0.25 | 0.200 |
| Success Criteria | 0.70 | 0.25 | 0.175 |
| Context Clarity | 0.85 | 0.15 | 0.128 |
| **Total Clarity** | | | **0.800** |
| **Ambiguity** | | | **0.20** |

## Topology
| Component | Status | Description | Coverage / Deferral Note |
|-----------|--------|-------------|--------------------------|
| Skill store | active | FastMCP stdio server, own code repository. Reads N stores (git read-only; directory and S3 read/write). Exactly one store is writable at a time. Runs as a child of mcpflow. | AC-1, AC-4, AC-5, AC-7, AC-11, AC-12, AC-13, AC-14, AC-15 |
| Discovery surface | active | The mcpflow gateway at `/mcp`. Agents find and load skills under the `skills` namespace. Catalog reaches agents through aggregated MCP server instructions. | AC-1, AC-2, AC-6, AC-8, AC-9, AC-10 |
| Agent adapters | active | One `mcpflow` MCP entry per agent: Claude Code, Codex CLI, OpenCode, Copilot CLI. | AC-2, AC-3 |
| Authoring flow | active | Humans and agents write skills through one MCP write tool into the writable store. Store management and migration run from a generic actions page in the mcpflow admin UI. | AC-4, AC-11, AC-13, AC-14, AC-16, AC-17 |

## Goal
Build one remote skill store. The store is a FastMCP server that presents skills from several stores as one catalog. A git store is read-only. A directory store and an S3-compatible store are read/write. Exactly one store is writable at a time. Every skill is addressed as `<store>/<name>`. The store runs as a child of the mcpflow gateway under the namespace `skills`. All four agents connect to the mcpflow gateway. Agents self-trigger skills from a catalog in the aggregated MCP server instructions, and also invoke skills by name. Humans and agents write skills through one MCP write tool. The mcpflow admin UI shows a generic actions page per child; the skill store publishes its store management and migrate actions there. No agent keeps skill files on disk.

## Constraints

### Code and process
- The store code lives in its own git repository on any git host. mcpflow runs it as a child of kind `python` with `source=git+<url>` and `package=skill-store`. The URL may carry credentials; mcpflow masks user-info in `source` (`redact_source`).
- The store listens on stdio only. It has no auth layer and no HTTP listener. mcpflow owns bearer auth, visibility, the public address, and the admin UI.
- The store is not a module of mcpflow, not a separate container, and not part of hindsight.

### Stores
- A store has a `name` (one path segment), a `kind`, a location, and optional credentials. Kinds: `git` (URL, ref, credential), `directory` (absolute path, a Docker volume in deployment), `s3` (endpoint, bucket, prefix, access key, secret key, region; S3-compatible API; MinIO is the first target).
- The `git` kind is read-only. The `directory` and `s3` kinds are read/write capable.
- Exactly one store is writable at a time. Only a read/write-capable store can be writable. `write_skill` targets the writable store. When no store is writable, `write_skill` returns an error.
- The store keeps a registry of stores and the writable flag in `SKILLS_STATE_DIR` on the directory volume. `SKILLS_STATE_DIR` must exist and be writable at start. Credentials in the registry are stored as given; the store never returns them in any tool result, resource, or action result. It returns a masked form.
- Store management is runtime: `add_store`, `update_store`, `remove_store`, `set_writable`, `refresh_store`. A change takes effect without a restart of the child.
- **Assumption A-1 (stated at hard cap):** the store polls each `git` store every 60 s (`git fetch` and compare the ref) and also exposes `refresh_store(name)`. The user may override the interval or drop polling at approval.
- **Assumption A-2 (stated at hard cap):** the S3 layout is one object per file at `<prefix>/<skill>/<path>`. A skill exists when `<prefix>/<skill>/SKILL.md` exists.
- Removing the writable store clears the writable flag. Removing a store never deletes its data.

### Skill identity and shape
- A skill is a full directory in the agentskills.io layout: `SKILL.md` plus optional `scripts/`, `references/`, `assets/`.
- Every skill is addressed as `<store>/<name>`. The same `<name>` may exist in several stores. There is no shadowing.
- The catalog line is `<store>/<name> — <description>`, where `<description>` is the YAML front-matter `description` key of `SKILL.md`.
- `use_skill("<store>/<name>")` returns the `SKILL.md` body plus resource URIs. `use_skill("<name>")` without a store returns the skill when exactly one store holds `<name>`, else an error that lists the matching `<store>/<name>` values.
- Resource URI: `skill://<store>/<name>/<path>`. Through the gateway: `skill://skills/<store>/<name>/<path>`.
- Prompt per skill named `<store>/<name>`; through the gateway `skills_<store>/<name>`. If an agent rejects `/` in a prompt name, the store uses `<store>__<name>` and the spec records that choice.

### Writes and migration
- `write_skill(name, files, message, replace)` writes into the writable store. For `directory` it writes the tree atomically (stage then swap). For `s3` it puts each object and then `SKILL.md` last. A partial failure leaves the previous `SKILL.md` in place.
- `migrate_skill(from_store, name, to_store)` copies `<from_store>/<name>` into `<to_store>` and then deletes it from `from_store`. `to_store` must be the writable store. `from_store` must be read/write-capable; a `git` source is copy-only (the source stays). When the copy fails, the source stays untouched.
- `migrate_store(from_store, to_store)` runs `migrate_skill` for every skill in `from_store`, stops at the first failure, and reports how many moved.
- There is no review pipeline, no proposal state, and no git push from the store. History is the responsibility of the backing store (the git remote's own history, filesystem snapshots, or S3 versioning).

### Delivery through the gateway
- The store publishes the catalog as its MCP server `instructions` and as a concrete resource `instructions://self`. The gateway reads `instructions://self` from every running, unmuted child at each `initialize`, with a bound of 2 s per child, and concatenates one block per child. A child without the resource contributes no block.
- The store also embeds the catalog in the `use_skill` tool description. Secondary channel only; agents that defer large tool lists do not see it.
- The gateway forwards the store's prompts and resources under the `skills` namespace. A muted `skills` namespace hides them.
- The `skills` child runs with `cache_ttl=0` so a new prompt or resource resolves in the next session.
- When mcpflow is unreachable, the agent runs with zero remote skills and reports the MCP connect failure once. The agent must not fail to start.

### Actions page (mcpflow)
- mcpflow gains a generic per-child actions page in the admin UI. A child marks a tool as an action through tool metadata (`_meta["mcpflow"]["action"] = true`). The page renders each action as a form built from the tool's input schema and shows the tool result.
- The page shows only actions of a `running`, unmuted child, and only to an `admin` session.
- **Security constraint (added 2026-09-16 after review):** a tool tagged as an action is admin-only on the MCP surface too. mcpflow applies the same scope filter that hides `mcpflow_*` tools: an action appears in `tools/list` and resolves for `tools/call` only when the session's token carries the `admin` scope. An agent session with the default `mcp` scope never sees or calls an action. This is a delta to the `mcp-gateway` spec rule "child providers carry no scope filter". Discarded alternative: hide actions by muting them, which also disables the page because the gateway refuses a disabled tool.
- Action input schemas are flat: every property is a string, number, boolean, enum, or array of strings. A `format: password` property is a top-level string. This keeps the masking rule enforceable.
- **Action contract clause (added 2026-09-16 after review):** an action-tagged tool declares no `ui.visibility` and no `fastmcp.tool_hash` in its metadata, and its schema is flat. mcpflow validates the clause when it reads a child's actions; a tool that violates it is not an action, and mcpflow logs a warning. Reason: FastMCP resolves a hashed tool name without running provider transforms, so a hashed action would bypass the admin scope filter. The same rule already protects the `mcpflow_*` admin tools.
- The admin page submits an action through the gateway with an in-memory admin `AccessToken` set on the SDK auth context. The token is never stored, never rendered, and never a `TokenStore` record.
- Every backend call in the store (git fetch, S3 request) carries its own timeout. A slow backend must not stall `use_skill` or the catalog read.
- mcpflow never learns the word "skill". The skill store is the first child that publishes actions.
- The store publishes these actions: `add_store`, `update_store`, `remove_store`, `set_writable`, `refresh_store`, `migrate_skill`, `migrate_store`, `list_stores`.
- Secret fields in an action schema carry a `format: password` hint. The page masks them on input and never echoes them back.

### Agents
- Day-one agents: Claude Code, Codex CLI, OpenCode, Copilot CLI. Each needs one `mcpflow` MCP config entry.
- Slash-by-name through an MCP prompt is required in Claude Code and Codex CLI. In OpenCode and Copilot CLI, a `skills_use_skill("<store>/<name>")` call by name is enough.

## Non-Goals
- No materialized skill directories on any agent machine.
- No local cache on agents and no offline mode beyond graceful degrade.
- No review, approval, or proposal workflow.
- No web UI inside the skill store. The only UI is the generic actions page in mcpflow.
- No git push from the store. Git stores are read-only.
- No shadowing or merge of same-named skills across stores.
- No sync from `~/.agents/skills` after the one-time import.
- Plugin-managed skills stay in their plugins. The store covers the 48 directories under `~/.agents/skills` on day one. The 16 Claude-only directories in `~/.claude/skills` are an open decision (OQ-B).
- No database. Stores are git, directory, or S3. The registry is one JSON file on the directory volume.
- No auth layer in the store. mcpflow owns auth.
- No forwarding of `list_changed` notifications to live agent sessions.
- No Claude Code SessionStart hook for the catalog.

## Acceptance Criteria
- [ ] AC-1 Self-trigger in fresh Claude Code. Remove `~/.claude/skills`. Start a new session. Type "debug this failing test". The model calls `skills_use_skill("<store>/diagnosing-bugs")` without the user naming the skill.
- [ ] AC-2 Slash by name. The MCP prompt for `diagnosing-bugs` in Claude Code and Codex CLI loads the same `SKILL.md` body through the gateway. In OpenCode and Copilot CLI, "use skill diagnosing-bugs" leads to a `skills_use_skill` call that returns the same body.
- [ ] AC-3 One config entry per agent. `~/.claude.json`, `~/.codex/config.toml`, `~/.config/opencode/opencode.json`, and `~/.copilot/mcp-config.json` each hold exactly one `mcpflow` MCP server entry with a bearer token. No agent config names the store directly.
- [ ] AC-4 Agent-written skill round-trips. Codex CLI calls `skills_write_skill` with a new skill. The writable store holds it (a directory tree, or objects under the S3 prefix). A Claude Code session started 10 seconds later lists `<writable-store>/<new>` in its MCP server instructions and can trigger it.
- [ ] AC-5 Full-directory skills survive import. After import, `skill://skills/<store>/diagnosing-bugs/scripts/hitl-loop.template.sh`, `skill://skills/<store>/git-guardrails-claude-code/scripts/block-dangerous-git.sh`, and `skill://skills/<store>/hindsight-docs/references/openapi.json` resolve as MCP resources through the gateway with byte-identical content.
- [ ] AC-6 Degrade when unreachable. Block DNS to mcpflow. A new Claude Code session starts, reports one MCP connect failure, and runs.
- [ ] AC-7 Import completeness. All 48 directories under `~/.agents/skills` (measured 2026-09-16) exist in the import target store with identical file trees.
- [ ] AC-8 Gateway forwards prompts and resources. With the store registered as child `skills`, `prompts/list` on the gateway holds one prompt per skill, and `resources/list` holds every non-`SKILL.md` file. With namespace `skills` muted, both lists hold none of them.
- [ ] AC-9 Lookup freshness. After a `skills_write_skill` call, `prompts/get` and `resources/read` for the new skill succeed through the gateway in a fresh session without an admin restart.
- [ ] AC-10 Gateway aggregates instructions. A fresh gateway session's `initialize` result carries an `instructions` string with the store's catalog under a `skills` header. Muting namespace `skills` removes the block. A child that hangs on the read adds at most 2 s to `initialize` and contributes no block.
- [ ] AC-11 Write into the writable store only. With the writable flag on store `s3main`, `skills_write_skill` creates objects under `s3main`'s prefix and nothing under any other store. With no writable store, the tool returns an error and no store changes.
- [ ] AC-12 Three kinds read. One `git` store, one `directory` store, and one `s3` store (MinIO) each hold one skill. The catalog lists three `<store>/<name>` lines. Each `use_skill` returns the matching body.
- [ ] AC-13 Same name, two stores. `git1/foo` and `dir1/foo` both exist. The catalog lists both. `use_skill("foo")` returns an error that names both. `use_skill("dir1/foo")` returns `dir1`'s body.
- [ ] AC-14 Migrate. `migrate_skill("dir1", "foo", "s3main")` with `s3main` writable: `s3main/foo` exists with identical files, `dir1/foo` is gone, the catalog reflects both within 10 seconds, no restart. `migrate_skill("git1", "foo", "s3main")`: `s3main/foo` exists, `git1/foo` still exists.
- [ ] AC-15 Runtime store management. `add_store` for a MinIO bucket from the mcpflow actions page: the store appears in `list_stores`, its skills appear in the catalog, no child restart. `remove_store` removes it from the catalog and deletes no object. The secret key never appears in any action result, log line, or `list_stores` output.
- [ ] AC-16 Actions page. mcpflow's admin UI shows an actions page for child `skills` with one form per published action. Submitting `set_writable` changes the writable store; `list_stores` confirms it. A `remote` child with no action metadata shows an empty page. A non-admin session gets no page.
- [ ] AC-17 Git store is read-only. With only a `git` store registered, `set_writable("git1")` returns an error and `write_skill` returns an error.
- [ ] AC-18 Git refresh. A commit lands on the git store's ref. Within 60 s (A-1) or after `refresh_store`, the catalog reflects the change.

## Assumptions Exposed & Resolved
| Assumption | Challenge | Resolution |
|------------|-----------|------------|
| Skills must be materialized to disk for agents. | Round 0. | No files on agent disks. |
| "Automatically discovered" means slash commands. | Round 1. | Both self-trigger and by-name. |
| An existing service hosts skills. | Round 2. | New service; round 13: mcpflow child. |
| Only Claude Code consumes the store. | Round 3. | All four agents. |
| Files stay the authoring source. | Round 4 (contrarian). | Service is the source; agents write too. |
| Service outage must not lose skills. | Round 5, retracted round 10. | Degrade only. |
| A database is needed. | Round 6 (simplifier). | Git, directory, S3. |
| A skill is one markdown file. | Round 8 (ontologist). | Full directory. |
| Agent writes need a gate. | Round 9, retracted round 12. | Writes land at once. |
| Cache needs a local process. | Round 10. | Direct HTTP. |
| The store needs its own address and auth. | Round 13. | mcpflow child. |
| mcpflow forwards child instructions. | Round 13 and planner. | It forwards none today; workstream C is additive. |
| Tool descriptions reach the model. | Round 15. | Claude Code defers mcpflow's tools; catalog goes in instructions. |
| A second Client re-runs initialize on a keep-alive child. | Planner iter 1, both reviewers. | No. Gateway reads `instructions://self` live over `child.transport`. |
| One remote git repository with push per write. | Round 16.5 (user). | Retracted. Git is read-only; directory and S3 are writable; one writable at a time. |
| Same-named skills merge or shadow. | Round 17. | Store-qualified names; no shadowing. |
| The store needs its own UI for migration. | Round 18. | Generic actions page in mcpflow. |
| The writable store is static config. | Round 19. | Runtime switch; store persists the flag. |
| Stores are static config. | Round 20. | Fully runtime via the actions page; store persists its registry. |
| The store has 51 skills. | Reviewers. | 48, measured. |

## Technical Context

### Existing state
- `~/.agents/skills` holds 48 skill directories, all with a `description:` front-matter key. `~/.claude/skills` holds 36 symlinks into it plus 16 Claude-only directories.
- 3 of 48 skills contain non-markdown files.
- mcpflow (`~/projects/home-mcp-server`, FastMCP 4.0.2): one `AggregateProvider`; each child is a `ProxyProvider` wrapped in `Visibility` then `Namespace` (`src/mcpflow/supervisor.py:290-298`). The gateway sets no `instructions` (`gateway.py:47`). `_probe_child` (`supervisor.py:944-957`) is the precedent for a direct read over `child.transport`. The admin UI is server-rendered (`src/mcpflow/web.py`, `templates/`). Admin MCP tools exist (`admin_mcp.py`). The `tool-visibility` spec already reserves `meta.ui.visibility`.
- FastMCP 4.0.2: `ProxyProvider` caches lists with `cache_ttl` (default 300 s, `proxy.py:849`); `_get_*` lookups are cache-gated. `ProxyResource.read` is live. `SkillsDirectoryProvider` exists (`server/providers/skills/`) and rescans on every request under `reload=True`.
- Claude Code already points at `https://mcpflow.kephrenz.nl/mcp`. Codex CLI, OpenCode, Copilot CLI do not.

### Design (architecture)
Two owners, one seam.

The store owns the store registry, the writable flag, skill semantics, and every read and write against git, directory, and S3. It is one FastMCP stdio server. It has no auth, no address, and no UI.

mcpflow owns the public address, bearer auth, the namespace `skills`, visibility, instructions aggregation, and the generic actions page. mcpflow sees the store as one more child that happens to publish actions.

Store internals: one `Backend` interface with `list_skills()`, `read_file(skill, path)`, `write_tree(skill, files, replace)`, `delete_skill(skill)`, `writable_capable`. Three implementations: `GitBackend` (clone to `SKILLS_STATE_DIR/git/<store>`, fetch on poll or refresh, read from the working tree, never writes), `DirectoryBackend` (atomic stage-and-swap writes), `S3Backend` (boto3-compatible client against any endpoint; `SKILL.md` written last). A `Registry` holds the store list and the writable flag in `SKILLS_STATE_DIR/stores.json`. A `Catalog` unions all backends into `<store>/<name>` entries and rebuilds on any write, migrate, refresh, or registry change. `instructions`, the `use_skill` description, the prompts, and the resources all derive from the `Catalog`.

Data flow, read: agent → mcpflow → `skills_use_skill("<store>/<name>")` → store → `Backend.read_file` → body plus URIs → agent.

Data flow, write: agent → mcpflow → `skills_write_skill` → store resolves the writable store → `Backend.write_tree` → `Catalog` rebuild → `instructions://self` reflects it → next gateway `initialize` carries it.

Data flow, action: admin → mcpflow actions page → `tools/call` on the child with the form values → store executes → result rendered. Secrets travel once, page to store, and never back.

Data flow, session start: agent → mcpflow `initialize` → supervisor reads `instructions://self` from each running, unmuted child over `child.transport` with a 2 s bound → one block per child → agent system prompt.

mcpflow changes (OpenSpec proposals in `~/projects/home-mcp-server`):
1. `proxy-prompts-resources` — spec and tests; no code expected.
2. `child-catalog-freshness` — `Settings` default 300 plus per-`ServerSpec` `cache_ttl` (`null` inherits); `skills` child at 0; form, admin MCP, and registry merge list carry the field.
3. `child-instructions` — the supervisor reads `instructions://self` per `initialize` and a middleware aggregates blocks.
4. `child-actions-page` — a generic admin page that renders tools tagged as actions as forms from their input schema; admin scope only; `format: password` fields masked.

Trade-off, store-qualified names over shadowing: longer names and a two-segment prompt name, in exchange for no hidden precedence rule and a migrate that is a plain move. Discarded: writable-wins shadowing, which makes "which copy am I running" invisible.

Trade-off, generic actions page over a store UI: one more mcpflow feature, in exchange for one UI, one auth, one chrome, and a store that stays stdio-only. Discarded: an HTTP listener in the store, which would need a port, a container, and its own auth.

Trade-off, runtime store registry over static config: the store persists state and handles secrets, in exchange for add and remove without a restart. Discarded: env-based config, which needs a child restart per change and puts S3 secrets into `servers.json`.

Risk, secrets: the registry file holds S3 and git credentials in clear text on the directory volume. Mitigation: file mode 0600, never returned by any tool, and the volume is the same trust boundary as `servers.json` today. Follow-up: encrypt at rest with a key from the child env.

Risk, AC-1: self-trigger depends on the model reading the instructions block. The catalog now carries store prefixes, so lines are longer; cap at 8 KB and truncate with a trailing count.

Risk, prompt names: `/` may be rejected in prompt names by some clients. Fallback `<store>__<name>` recorded in constraints.

### Migration
1. Create the store code repository and implement it against this spec.
2. Land the four mcpflow proposals.
3. Register the store in mcpflow as child `skills` (kind `python`, git source, `env` with `SKILLS_STATE_DIR` on a volume).
4. From the actions page: `add_store` for a directory store on the volume, `set_writable` on it, import the 48 directories into it (a one-off `import_directory` action or a copy into the volume).
5. Add MinIO as an `s3` store; optionally `migrate_store` from the directory store to it and `set_writable` on it.
6. Add the `mcpflow` MCP entry to Codex CLI, OpenCode, and Copilot CLI configs.
7. Verify AC-1 through AC-18.
8. Decide OQ-B, then remove `~/.claude/skills` symlinks, `~/.codex/skills` copies, and `~/.agents/skills`.

## Open Questions (user decisions)
- OQ-A: git host and repository name for the store code. Open.
- OQ-B: **Decided 2026-09-16: import all 16** Claude-only directories from `~/.claude/skills` into the store. Day-one import is 48 + 16 = 64 skill directories. AC-1 removes `~/.claude/skills` entirely, as written. AC-7 counts 64.
- OQ-C: confirm or override assumptions A-1 (git poll 60 s) and A-2 (S3 layout). Open.
- OQ-D: **Decided 2026-09-16: keep workstream E** (runtime store management page). The restart-on-change alternative is recorded in the plan ADR and rejected.
- Plan approved 2026-09-16 for the proposal stage: OpenSpec changes A, B, C, E in `home-mcp-server`. No implementation yet.

## Ontology (Key Entities)
| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| Store | core domain | name, kind (git, directory, s3), location, credentials (masked), writable_capable, writable | Registry has many Stores; Store has many Skills |
| Registry | core domain | stores, writable_store, path (SKILLS_STATE_DIR/stores.json) | Owned by SkillStore |
| Skill | core domain | store, name, description, SKILL.md, scripts/, references/, assets/ | Store has many Skills; addressed as store/name |
| SkillFile | supporting | path, content, resource_uri skill://store/name/path | Skill has many SkillFiles |
| Catalog | supporting | entries (store/name, description) | Union over Stores; drives instructions, use_skill description, prompts, resources |
| SkillStore | core domain | code_repo_url, state_dir, backends | Serves Catalog; is one mcpflow Child; publishes Actions |
| Action | supporting | tool name, input schema, mcpflow.action meta, password fields | SkillStore publishes Actions; Gateway renders them |
| Gateway (mcpflow) | external system | url, token, namespace skills, cache_ttl, instructions aggregator, actions page | Proxies SkillStore; owns auth, visibility, UI |
| Agent | supporting | kind: claude-code, codex, opencode, copilot | Connects to Gateway; reads instructions |
| Author | supporting | kind: human, agent | Writes Skill via write_skill; admin runs Actions |

## Ontology Convergence
| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|-------|-------------|-----|---------|--------|----------------|
| 1–16 | see revision 3 | | | | 100% at 16 |
| 17 | 11 | 1 (Store) | 1 (SkillsRepo → Store) | 9 | 91% |
| 18 | 12 | 1 (Action) | 0 | 11 | 92% |
| 19 | 12 | 0 | 0 | 12 | 100% |
| 20 | 13 | 1 (Registry) | 0 | 12 | 92% |

## Interview Transcript
<details>
<summary>Rounds 17–20 (revision 4). Rounds 0–16 in revision 3.</summary>

### Round 16.5 (user-initiated)
Stores: git read-only; directory (docker volume) read/write; S3-compatible (MinIO first) read/write. One writable store. Migrate button. Retracts round 15.

### Round 17
**Q:** Same name in two stores: what does the agent see?
**A:** Store-qualified names, no collisions.

### Round 18
**Q:** Where does the migrate button live?
**A:** Page in mcpflow's admin UI.

### Round 19
**Q:** Who decides which store is writable?
**A:** Runtime switch on the mcpflow page.

### Round 20
**Q:** How are stores defined?
**A:** Fully runtime via the mcpflow page. Hard cap reached. **Ambiguity:** 20%
</details>
