# Work Plan: Remote Skill Store

**Status:** pending approval
**Mode:** RALPLAN-DR consensus, SHORT · iteration 5 (final)
**Source spec:** `.omc/specs/deep-interview-remote-skill-store.md` — revision 4, with the Actions-page security, flat-schema, and backend-timeout constraints added 2026-09-16 after review.
**Reviews folded in:** iter 1 (A 8, C 15) · iter 2 (A 6, C 15) · iter 3 (A 4) · iter 4 (A 11, C 18), under `.omc/state/`
**Sketch:** `.omc/plans/remote-skill-store.sketch.html`
**Tooling note:** Serena did not load in this session. The author used Read, Grep, Glob, and Bash.

---

## 1. Requirements Summary

| # | Requirement | Spec ACs |
|---|-------------|----------|
| R1 | One FastMCP stdio server presents N stores as one catalog. | AC-1, AC-12 |
| R2 | The store runs as one mcpflow child under `skills`. mcpflow owns auth, address, visibility, UI. | AC-2, AC-3 |
| R3 | The gateway forwards child prompts, resources, and templates. A mute hides them. | AC-8 |
| R4 | The gateway resolves a component a child added, with no restart. | AC-9 |
| R5 | The gateway aggregates `instructions://self` per `initialize`, bounded at 2 s per child. | AC-10 |
| R6 | Three kinds read: `git` read-only, `directory` and `s3` read/write. | AC-12, AC-17 |
| R7 | Exactly one store is writable. Only a capable store may hold the flag. | AC-11, AC-17 |
| R8 | Skills are addressed `<store>/<name>`. No shadowing. | AC-13 |
| R9 | Store management is runtime. | AC-15, AC-18 |
| R10 | Migration copies, verifies, then deletes; a `git` source is copy-only. | AC-14 |
| R11 | mcpflow renders a generic per-child actions page. **Actions are admin-only on the UI and on the MCP surface.** | AC-16, AC-19 |
| R12 | All four agents hold one `mcpflow` entry. An unreachable gateway degrades. | AC-3, AC-6 |
| R13 | The import moves all **64** directories (48 from `~/.agents/skills` plus the 16 Claude-only ones from `~/.claude/skills`, per OQ-B decided 2026-09-16) with identical file trees. | AC-5, AC-7 |

**Measured, 2026-09-16.** 48 directories, 227 files, 3.3 MB. All 48 `SKILL.md` files carry a `description:` key.

Five workstreams. A, B, C, and E change mcpflow. D builds the store and runs the migration.

---

## 2. Verified Facts and Spec Corrections

1. **mcpflow forwards no child instructions today.** `ProxyMetadataMiddleware` is installed only by `FastMCPProxy.__init__` (`.venv/.../fastmcp/server/providers/proxy.py:1500`). `build_app` builds a plain `FastMCP` (`src/mcpflow/gateway.py:47-52`); `_build_provider` builds a bare `ProxyProvider` (`src/mcpflow/supervisor.py:294`).

2. **The cache gates lookups, never lists.** `_list_tools` always calls the child (`.venv/.../fastmcp/server/providers/proxy.py:921-938`). `_get_tool` (`:940-953`), `_get_resource` (`:1027-1040`), `_get_resource_template` (`:1066-1079`), `_get_prompt` (`:1105-1119`) read the cache.

3. **`Client.instructions` is handshake state.** `StdioTransport(keep_alive=True)` (`src/mcpflow/supervisor.py:271-277`); `.venv/.../fastmcp/client/transports/stdio.py:128-130`; `.venv/.../fastmcp/client/client.py:849-852`; `mcp/client/session.py:658-660`. A runtime probe confirms it.

4. **A bare resource template fails AC-8.** `SkillProvider` hides supporting files under `supporting_files="template"` (`.venv/.../fastmcp/server/providers/skills/skill_provider.py:188-196`).

5. **A provider transform marks; it never removes.** `Provider.get_resource` does not filter disabled components (`.venv/.../fastmcp/server/providers/base.py:248-262`, note at `:253-254`). `_probe_child` relies on this (`src/mcpflow/supervisor.py:951-954`). **A `ScopeFilter` removes** (`src/mcpflow/admin_mcp.py:70-99`). That difference decides workstream E.

6. **A tool-level mute matches the bare name of every kind.** `.venv/.../fastmcp/server/transforms/visibility.py:125-183` applies no kind filter when `components` is `None`; `_build_provider` passes no `components` (`supervisor.py:291`).

7. **`_meta` survives both hops.** `Namespace` copies only the name or URI (`.venv/.../fastmcp/server/transforms/namespace.py:100`, `:112`, `:122`, `:180`); `ProxyTool.from_mcp_tool` carries `meta=mcp_tool.meta` (`.venv/.../fastmcp/server/providers/proxy.py:381`). *Kept as a fact, demoted as a claim:* workstream E does not rely on it. E reads raw over `child.transport` and submits by namespaced name. It matters only for the scope filter, which sits inside `Namespace` and reads bare meta.

8. **`on_discover` enters a Context**, as `on_initialize` does: `.venv/.../fastmcp/server/low_level.py:367` and `:425`. C-5 is reachable.

9. **One `TransportOptions` profile per child transport.** `TransportOptions` carries `session_class`, `forward_incoming_headers`, `backend_mode` — never `timeout` (`.venv/.../fastmcp/client/transports/base.py:41-72`). `_make_factory` returns a **factory whose product is a plain `Client(transport, timeout=...)`** (`src/mcpflow/supervisor.py:350-356`), and `_probe_child` builds the same shape (`:956-958`). Neither passes `transport_options` nor a proxy session class, so `StdioTransport._session_options` compares equal and `.venv/.../fastmcp/client/transports/stdio.py:112-119` cannot raise. FastMCP's own proxy paths would layer `session_class=_ForwardingClientSession, forward_incoming_headers=True` (`.venv/.../fastmcp/server/providers/proxy.py:101-115`). **C-10 asserts the equality, not the word "plain".**

10. **`meta["fastmcp"]` is FastMCP's own reserved top-level namespace** (`.venv/.../fastmcp/utilities/components.py:161-179`). The spec's `_meta["mcpflow"]["action"]` follows the same one-reserved-key pattern. **`openspec/specs/mcp-gateway/spec.md:126` is a prohibition** — "No admin tool SHALL set `meta.ui.visibility`" and "Child providers SHALL carry no scope filter" — not a precedent for reading child metadata. Workstream E adds the first read-and-act `_meta` channel and must carry its own trust argument (section 3, E1).

11. **`FastMCP.call_tool` raises `NotFoundError` for a tool that is "not found or disabled"** (`.venv/.../fastmcp/server/server.py:1364-1394`).

11b. **Tool dispatch has two entry points, and the second one skips every transform.** When named resolution returns `None`, `call_tool` falls through to `parse_hashed_backend_name` and then `get_tool_by_hash` (`.venv/.../fastmcp/server/server.py:1466-1487`). That path delegates straight past the wrapper chain. `.venv/.../fastmcp/server/providers/wrapped_provider.py:70-72` states it outright: *"Delegate to inner, bypassing this wrapper's transforms."* It reads the raw tool cache (`.venv/.../fastmcp/server/providers/base.py:213-236`; `.venv/.../fastmcp/server/providers/proxy.py:955-1001`). The gate is **child-supplied** metadata only: `meta.fastmcp.tool_hash` must match the digest, and `meta.ui.visibility` must contain `"app"`. Only `tool.auth` still applies (`.venv/.../fastmcp/server/server.py:1477-1487`). **So a scope-filter transform closes the display-name path and nothing else.** mcpflow already closes this for its own admin tools by constraint rather than by code, and says why (`src/mcpflow/admin_mcp.py:152-156`).

12. **The UI routes are already admin-gated at the app level.** `src/mcpflow/web.py:3-4`; `_PUBLIC_PREFIXES` excludes the UI (`src/mcpflow/gateway.py:29`). `build_routes` takes no gateway today (`src/mcpflow/web.py:153-159`).

13. **Child tools are reachable by any token today.** `_build_provider` wraps a child in `Visibility` then `Namespace` only (`supervisor.py:290-298`). `mcp-gateway/spec.md:126` states child providers carry no scope filter. A token's default scope is `mcp` (`src/mcpflow/auth.py:172`), and `/mcp` accepts both scopes, putting the scope on the `AccessToken` (`src/mcpflow/auth.py:297-305`). **Without workstream E's filter, any agent session could call `skills_set_writable`, `skills_remove_store`, or `skills_add_store` with live S3 credentials as arguments.** This is the defect the spec's new security constraint closes.

---

## 3. RALPLAN-DR Summary

### Principles

1. **One owner per concern.**
2. **A value the gateway must re-read is a live call, never handshake state.**
3. **The seam follows the owner, not the contract.**
4. **mcpflow stays generic.** No mcpflow source file may contain the word "skill".
5. **Every call on a hot path carries its own bound** — in the gateway *and* in the store.
6. **A destructive step runs only after its constructive step succeeded.**
7. **A privileged surface is closed by default.** A capability reachable by an agent token is an agent capability, whatever the UI does.

### Decision drivers (top 3)

| # | Driver | Why it decides |
|---|--------|----------------|
| DD1 | AC-1 self-trigger through aggregated instructions. | Claude Code defers the tool list, so the catalog must ride `initialize`. Forces C. |
| DD2 | Runtime store management with no child restart (AC-15). | Forces the persisted registry, the `Catalog` rebuild, and **all of workstream E**. |
| DD3 | Two owners, one seam; mcpflow never learns "skill". | Forces the generic `_meta["mcpflow"]["action"]` contract. |

### The Catalog — the store's single source of truth

*This paragraph resolves how registration, freshness, and non-blocking reads fit together.*

**The `Catalog` is the store's single in-memory source of truth.** Exactly **one `Provider`** reads it, serving `list_resources`, `get_resource`, `list_prompts`, and `get_prompt` (the precedent is the one-provider-per-child chain at `src/mcpflow/supervisor.py:290-298`, and `.venv/.../fastmcp/server/providers/skills/directory_provider.py:55-74` for the shape). **Every backend write updates the `Catalog` from the write's own result, never by re-listing** — this is what makes Risk 5 (S3 list-after-write) a non-issue. `stores.json` and the object stores are **durable state that reconciles into** the `Catalog`, never the other way round. The `Catalog` is served from the **last-built immutable snapshot**, and a rebuild swaps one object reference, so **a rebuild never blocks a read** and one handshake can never see two catalogs.

That last property is load-bearing twice over. It closes tension T2 — `cache_ttl=0` and the git poll are two freshness owners, and without snapshot swapping a poll landing between `tools/list` and `prompts/list` of one handshake would serve two generations. And it closes the in-process deadlock the Critic found: a rebuild holding the write lock while the gateway's 2-second-bounded `instructions://self` read waits would silently drop the `## skills` block and fail AC-1 with no error. **A read never takes the write lock.**

**Break-glass:** writing `stores.json` directly on the volume is supported. The store reads it at start. This is the bootstrap path when the actions **page** is unavailable. **It replaces the page, never the filter:** E stays a hard predecessor of D7, because the exposure Risk 4 names is created by registering the child, not by opening the page.

### B — where `cache_ttl` lives *(decided iteration 2)*

**B3: `Settings` default 300, plus an optional per-`ServerSpec` override, `null` = inherit. The `skills` child is registered with `0`** (spec line 71). `is_fresh(0)` is always false (`.venv/.../fastmcp/server/providers/proxy.py:845-846`).

**`cache_ttl` is caller-owned, like `env`.** `Registry.update` re-merges only six registry-owned fields (`src/mcpflow/registry.py:343-351`). Three sites must carry it: `src/mcpflow/web.py:66-79`, `src/mcpflow/web.py:85-99`, `src/mcpflow/templates/_server_form_fields.html`. No API work: `child_json` dumps the spec (`src/mcpflow/api.py:63`); `PUT` uses `spec_from_dict` (`:177`).

**The asymmetry, named.** A dropped `env` fails **loudly** — the child cannot start. A dropped `cache_ttl` fails **silently** — the catalog goes stale. `mcpflow_update_server` takes a partial dict from an agent (`src/mcpflow/admin_mcp.py:177-182`), which reintroduces the hazard. The registry-owned alternative is in the ADR.

### C — carrying a changed `instructions` string *(decided iteration 3)*

**Contract (b):** a child publishes a **concrete** resource `instructions://self`. **Caller (b2):** the supervisor opens a direct `Client(child.transport)` and reads the bare URI, mirroring `_probe_child` (`src/mcpflow/supervisor.py:944-957`, comment at `:951-954`), gated on `registry.visibility(child.spec)` → `hide_all` (`src/mcpflow/registry.py:453-461`), each child under `asyncio.wait_for(..., 2.0)`.

Rejected, with grounds:
- **(a) a second transport with `keep_alive=False`** — one store respawn per gateway session start, and a re-clone or re-fetch each time.
- **(c) restart the child on every write** — spec line 149; drops in-flight calls; invisible for a `remote` child.
- **(d) the store exits 0 after a write** — races the triggering call, kills in-flight calls, re-clones per write, and freezes `status`, `tool_count`, `started_at` while `status` still reads `running`.
- **(e) `disconnect()` then reconnect** — one subprocess, not two, but the same in-flight-call loss as (c) plus a re-fetch per session start.
- **(b1) read through the provider chain** — cache-gated `_get_resource` (`.venv/.../fastmcp/server/providers/proxy.py:1027-1040`) forces a full list under `cache_ttl=0`, and a child registering the resource late is invisible for up to 300 s.

### D1 — who registers resources and prompts

| Option | Pros | Cons |
|--------|------|------|
| D1-a Keep `SkillsDirectoryProvider(reload=True)` | Zero code for the directory case; concrete resources; `safe_join` defence (`.venv/.../fastmcp/server/providers/skills/skill_provider.py:81-99`). | **Local-filesystem only** — roots resolve through `Path(r).resolve()` (`.venv/.../fastmcp/server/providers/skills/directory_provider.py:66-68`), so it cannot reach S3. Its URIs carry no store segment, contradicting spec line 58. It rescans every list and get (`.venv/.../fastmcp/server/providers/skills/directory_provider.py:122-125`). |
| **D1-b The store owns one `Provider` over the `Catalog` (CHOSEN)** | One path for three kinds. URIs carry the store segment. Rebuild is an object swap, so it is non-blocking and generation-consistent. | The store re-implements enumeration, MIME typing, and traversal defence. |
| D1-c Hybrid | Reuses the library where it fits. | Two registration paths and two URI shapes. |

**Chosen: D1-b.** **Port, do not invent:** lift `safe_join` from `.venv/.../fastmcp/server/providers/skills/skill_provider.py:81-99`; `mimetypes.guess_type` covers MIME.

### D2 — the S3 client

| Option | Pros | Cons |
|--------|------|------|
| **boto3 + `asyncio.to_thread` (CHOSEN)** | Reference client. `endpoint_url` and `Config(s3={"addressing_style": "path"})` give MinIO support. One known dependency. | Synchronous; one thread per call. |
| aiobotocore | Async-native. | Pins tightly to a botocore range, conflicting with other boto-based dependencies. |
| minio-py | Smallest MinIO API. | Also synchronous; a second dialect; narrower for a non-MinIO endpoint. |

**Chosen: boto3 + `to_thread`**, with explicit bounds — see D5.

### D3 — write, replace, and delete semantics *(rewritten; the iteration-4 version was wrong)*

The iteration-4 plan claimed "`SKILL.md` last" made a write atomic. **It does not.** Writing `SKILL.md` last gives atomic *appearance*, never atomic *replacement*:

- Under `replace=True`, the supporting-file `PUT`s overwrite the old objects **before** the new `SKILL.md` lands. A reader in that window gets the **old `SKILL.md` over new files** — a torn read across two generations. AC-5 byte identity fails inside it.
- A `PUT` never deletes. A file dropped from the new tree survives as an orphan and is enumerated forever. The iteration-4 claim that "a later overwrite reconciles them" was false.

| Backend | Write / replace | Delete | Failure state |
|---------|-----------------|--------|---------------|
| `directory` | Stage the whole tree in a sibling temporary directory on the same filesystem, then `os.replace` the skill directory. | `os.replace` away, then remove. | Atomic. A reader never sees a mixed tree. |
| `s3` | **1.** LIST the existing keys under `<prefix>/<skill>/`. **2.** PUT every new supporting file. **3.** PUT `SKILL.md` **last**. **4.** DELETE the keys from step 1 that are absent from the new tree. | **DELETE `SKILL.md` first**, then the rest — so the skill stops existing before its files vanish. | **Torn-read window, stated honestly:** between step 2 and step 3 a concurrent reader can see the old `SKILL.md` beside new supporting files. It is bounded by the PUT of one small object. S3 offers no multi-object transaction; closing it fully would need a generation prefix and a pointer object, which is out of scope for day one. Risk 6 records it. |
| `git` | Never writes (spec lines 45, 90). `writable_capable = False`. | — | — |

**`migrate_skill` verification is `head_object` per key, never LIST.** This is forced by Risk 5: the store must not re-list after a write. `head_object` on a known key is a point read, so it is consistent where a LIST may lag. Without this, `MigrateAtomicity` has no refinement to the implementation.

**Partial `migrate_store` recovery.** It stops at the first failure and reports moved and remaining counts. **Re-running it is idempotent**, because each skill goes through copy → verify → delete independently. A skill already moved is absent from the source, so it is skipped. A skill whose copy succeeded but whose delete failed is re-verified by `head_object`, then deleted. No compensating transaction is needed.

### D4 — registry secrets

| Option | Verdict |
|--------|---------|
| **A `0600` JSON file under `SKILLS_STATE_DIR`, written with `os.replace` (CHOSEN)** | One file, one owner, crash-consistent. Open with `O_CREAT` at `0o600` — never write-then-chmod — following `src/mcpflow/config.py:55-58`, which exists precisely to avoid the `0644` window. |
| Reuse mcpflow's `CredStore` (`src/mcpflow/oauth.py:159-170`, per-namespace directories at `0o700`, `:170`) | **Rejected.** It lives in mcpflow, and the store must run without mcpflow (spec line 41). Adopting it would put store credentials on the wrong side of the seam and make the store depend on the gateway it is a child of. Its *file-mode discipline* is worth copying; its ownership is not. |
| Environment variables per store | **Rejected** with the env-config alternative in the ADR: it defeats AC-15 and puts S3 secrets into `servers.json`, which `mcpflow_get_server` exposes to any agent with the admin tool (`src/mcpflow/admin_mcp.py:177-182`). |

**Masking rule:** every tool result, action result, resource, and log line returns a credential as a **fixed** mask. Never a prefix, a length, or a suffix.
**`stores.json` is written stage-and-swap too** — the same `os.replace` discipline as a skill tree, so a crash mid-write cannot truncate the registry.
**Follow-up, not day one:** encrypt at rest with a key from the child `env`.

### D5 — bounds inside the store

Principle 5 binds the store, not only the gateway. Without this, a slow backend stalls `use_skill` on the agent's critical path, and a slow catalog rebuild makes the gateway's 2-second bound drop the `## skills` block — **AC-1 fails silently**.

- **S3:** `Config(connect_timeout=..., read_timeout=..., retries={"max_attempts": ...})`. boto3's defaults are 60 s connect and 60 s read with retries, which is far past any bound here.
- **Git:** every `git fetch` runs under an explicit timeout and is killed on expiry. A failed or timed-out fetch leaves the previous working tree readable.
- **`use_skill` carries its own bound** on the agent path, independent of the catalog rebuild.
- A backend past its bound is reported as a per-store error; the rest of the catalog still serves.

### D6 — store names and file encoding

- **Store names match `[a-z0-9][a-z0-9-]*`.** This keeps the `<store>__<name>` prompt-name fallback **injective**: without it, stores `a_b` and `a` with skill `b_c` would collide.
- **`write_tree` carries no file mode.** S3 has no mode, so the interface cannot promise one. `block-dangerous-git.sh` is `0755` while its siblings are `0644`, and **AC-7's `diff -r` does not compare modes** — so AC-7 passes while the executable bit is lost. Agents read scripts as resources and write them to their own temp path, where they set their own mode, so nothing breaks. **Stated, not hidden.**
- **Files are UTF-8 text.** A binary asset travels in a separate base64 field, so the text path never guesses an encoding. Of the 227 files measured, none is binary today.

### E1 — the trust boundary *(the critical change this iteration)*

**The defect.** Fact 13: a child's tools are reachable by any `mcp`-scope token. The eight store actions include `add_store` (which takes live S3 credentials as arguments), `remove_store`, `set_writable`, and `migrate_store`. Gating only the *form* leaves the *capability* open to every agent.

**Trust direction of child-supplied `_meta`.** Any child can tag any tool as an action, and any child can declare a `format: password` field. mcpflow reads that metadata and renders a form for an admin. So the metadata is **untrusted input that drives a privileged UI** — a credential-harvesting surface if unconstrained. Three things contain it, and all three are required:
1. **Admin-only on both surfaces**, so only an admin ever sees or calls an action.
2. **Flat schemas** (spec), so the masking rule is enforceable — a nested `credentials` object would fall to the JSON textarea and silently void `format: password`.
3. **Password masking** on render, re-render, and result.
mcpflow renders what a child declares; it never grants a child anything it did not already have.

| Option | Pros | Cons |
|--------|------|------|
| **E1-a A scope-filter transform on action-tagged child tools, plus the action contract clause (CHOSEN)** | The filter closes the **display-name** dispatch path, using the mechanism already implemented for `mcpflow_*` (`src/mcpflow/admin_mcp.py:70-99`). It **removes** rather than marks (fact 5). The **hash** path is closed separately, by the contract clause — see below. | A delta to `mcp-gateway/spec.md:126`. Two mechanisms, not one, because dispatch has two entry points. |
| E1-b Mute the action tools per tool | No new mechanism. | **Incompatible with E2-a:** `gateway.call_tool` raises `NotFoundError` for a disabled tool (`.venv/.../fastmcp/server/server.py:1364-1394`), so hiding the action from agents also kills the page. Iteration 4 presented this refusal as a virtue; it is the thing that makes muting unusable as a hiding mechanism. |
| E1-c A store-side caller-identity check | No mcpflow change. | The store cannot see the caller's scope — mcpflow owns auth (spec line 40). It would have to trust a header the gateway does not send. |
| E1-d Accept the exposure, record it in Non-Goals | Zero work. | Hands every agent session `remove_store` and `migrate_store`. Not acceptable. |

**Chosen: E1-a.** A per-tool filter keyed on `_meta["mcpflow"]["action"]`, installed by `_build_provider` (`src/mcpflow/supervisor.py:279-297`), **innermost in the child chain**. Inside `Visibility` and `Namespace` it reads bare names and bare meta. That position also stops a later mark from undoing its removal. That position and that reasoning are the ones `register_builtin` already documents for the admin chain (`src/mcpflow/supervisor.py:299-340`). Scope comes from the request `AccessToken` exactly as `ScopeFilter._is_admin` reads it (`admin_mcp.py:87-89`), and `get_access_token()` returning `None` fails closed (`admin_mcp.py:73-77`).

**Closing the hash path — the action contract clause (spec line 79).** Fact 11b shows the filter alone is not enough. An action-tagged tool **declares no `ui.visibility` and no `fastmcp.tool_hash`, and its schema is flat.** mcpflow validates the clause where it reads a child's actions, in `Supervisor.actions()`, and the filter's `get_tool` re-checks it on the call seam. A violating tool is rejected with a WARNING.

**What happens to a violating tool, stated plainly.** It **is not an action**. It is dropped from the page, carries no scope filter, and stays an ordinary agent-callable child tool — the same exposure every child tool has today. mcpflow does not silently harden it, and it does not silently hide it. A child that wants an admin-only tool must meet the clause.

*Discarded alternative — strip `ui.visibility` and `meta.fastmcp` from every child tool in `_build_provider`.* One owner, every child, no clause to violate. Rejected for now because stripping `ui.visibility` would break any MCP-Apps-capable child. It is recorded as **follow-up hardening**, and it belongs at the `ProxyProvider` conversion hook (`.venv/.../fastmcp/server/providers/proxy.py:381`), where child metadata first enters mcpflow — not at the transform layer, which the hash path skips by design.

**Spec delta.** `mcp-gateway/spec.md:126` gains: *a child provider SHALL carry a scope filter for tools its child tags as actions, and no other component.* The existing prohibition stays for every untagged component.

### E2 — where the page reads, and how the submit gets admin scope

**Read: E2-a, `Supervisor.actions(namespace)` reading direct over `child.transport`**, mirroring `_probe_child` (`supervisor.py:944-957`). It returns raw `Tool` objects, so the full schema and `meta` survive. It filters on the action key, on `running`, and on `_visible()` (`supervisor.py:166-174`). Reading direct also means the page sees actions **regardless of the new scope filter**, which is correct: the page is already behind the admin session gate.

**Submit: the page holds a session cookie, not a bearer token.** A plain in-process `gateway.call_tool` from a web route presents **no** token, so the new filter fails closed and the call raises `NotFoundError`. The submit path must supply admin scope deliberately.

**The mechanism is settled, not a spike.** The UI routes sit on the **outer** Starlette app, a sibling of the `Mount` that carries the MCP app (`src/mcpflow/gateway.py:93-99`). `RequestContextMiddleware` lives inside the mounted app, so in a web route `get_http_request()` raises (`.venv/.../fastmcp/server/dependencies.py:536-537`) and `get_access_token()` falls through to `_sdk_get_access_token()` (`.venv/.../fastmcp/server/dependencies.py:634-635`). **That SDK context variable is the seam** — `auth_context_var` holding an `AuthenticatedUser` that wraps an `AccessToken` (`.venv/.../mcp/server/auth/middleware/auth_context.py:10`, `:39-43`). Spec line 80 fixes the rule: an **in-memory** `AccessToken` object only.

| Option | Pros | Cons |
|--------|------|------|
| **E2-b An internal admin-scoped call path: set `auth_context_var` to an in-memory admin `AccessToken` for the duration of the call, then `gateway.call_tool` (CHOSEN)** | Visibility and the scope filter both apply on the display-name path. One call path for the UI and for an admin bearer token. The session gate is the authority that justifies the scope (`src/mcpflow/web.py:3-4`). Reachability is settled by source, above. | mcpflow mints an in-memory token object. **Remaining design-gate item: reset discipline** — the token must not outlive the call, must never become a `TokenStore` record (`src/mcpflow/auth.py:288-303`), and must never be rendered. |
| E2-c Call direct over `child.transport` | Symmetric with the read. | Bypasses `Visibility`, so a muted action would still execute. Contradicts spec line 76. |
| E2-d Require the admin to paste a bearer token into the page | No new mechanism. | Hostile, and it puts a long-lived admin token into a form field. |

**Chosen: E2-b**, with the mechanism flagged for the design gate. `build_routes` must receive the gateway; it takes none today (`src/mcpflow/web.py:153-159`), so this is one wiring change in `build_app` (`src/mcpflow/gateway.py:47-60`).

### E3 — the form schema subset

Render natively: `string` (with `enum` → `<select>`, `format: password` → masked input), `integer`/`number`, `boolean`, and `array` of `string` → textarea, one per line, reusing the `_kv_lines` shape (`src/mcpflow/web.py:51-60`). Anything else falls back to a JSON textarea parsed on submit.

**The spec now requires flat action schemas**, which makes the fallback a safety net rather than a load-bearing path. This matters: a nested `credentials` object would reach the JSON textarea and **silently void the password masking**. The plan no longer claims the subset "covers the eight actions" — those schemas do not exist yet. It claims the opposite direction: **D-11 constrains the schemas to the subset**, and a schema that escapes it is a store bug, caught by an E test.

**A muted action renders marked and with its submit disabled** (tension T3). `_visible()` is already consulted at render (`supervisor.py:166-174`). Without this the admin types an S3 secret into a form whose submit is doomed, and the secret crosses into the gateway process and its logs before the refusal.

---

## 4. Acceptance Criteria

### Carried from the spec (AC-1 … AC-18), plus AC-19

- [ ] **AC-1** Fresh Claude Code, no `~/.claude/skills`: "debug this failing test" produces `skills_use_skill("<store>/diagnosing-bugs")` unprompted. *Single-trial model behaviour; record the trial count.*
- [ ] **AC-2** The `diagnosing-bugs` prompt in Claude Code and Codex CLI loads the same body; OpenCode and Copilot CLI reach it through `skills_use_skill`.
- [ ] **AC-3** Four agent configs, one `mcpflow` entry each, no config naming the store.
- [ ] **AC-4** A Codex `skills_write_skill` lands in the writable store; a session 10 s later lists and triggers it.
- [ ] **AC-5** The three named resources resolve as `skill://skills/<store>/<name>/<path>`, byte-identical.
- [ ] **AC-6** With DNS blocked, a new session starts, reports one failure, and runs.
- [ ] **AC-7** All 64 directories (48 + 16, OQ-B) exist in the import target with identical file trees. *`diff -r` compares content, not modes — see D6.*
- [ ] **AC-8** `prompts/list` holds one prompt per skill; `resources/list` **holds every** non-`SKILL.md` file. A mute empties both. *Assert presence, never exclusivity.*
- [ ] **AC-9** After a write, `prompts/get` and `resources/read` succeed in a fresh session with no restart.
- [ ] **AC-10** A fresh `initialize` carries the catalog under a `skills` header; a mute removes it; a hanging child adds at most 2 s and contributes no block.
- [ ] **AC-11** With `s3main` writable, `write_skill` creates objects under its prefix only. With none writable, it errors and nothing changes.
- [ ] **AC-12** One `git`, one `directory`, one `s3` store each hold a skill; three catalog lines; each `use_skill` returns the matching body.
- [ ] **AC-13** `git1/foo` and `dir1/foo` both list. `use_skill("foo")` errors and names both. `use_skill("dir1/foo")` returns `dir1`'s body.
- [ ] **AC-14** `migrate_skill("dir1","foo","s3main")`: target matches, source gone, catalog reflects both within 10 s, no restart. From `git1`, the source survives.
- [ ] **AC-15** `add_store` for MinIO: it appears in `list_stores`, its skills appear, no restart. `remove_store` deletes no object. The secret never appears in any result or log.
- [ ] **AC-16** The admin UI shows an actions page for `skills`, one form per action. `set_writable` works and `list_stores` confirms. A child with no action metadata shows an empty page. A non-admin session gets no page.
- [ ] **AC-17** With only a `git` store, `set_writable("git1")` and `write_skill` both error.
- [ ] **AC-18** A commit on the git ref reaches the catalog within 60 s (A-1) or after `refresh_store`.
- [ ] **AC-19 (new, from the security constraint)** **(a)** An `mcp`-scope session's `tools/list` contains **no** action-tagged tool, while still containing the child's ordinary tools. **(b)** `tools/call` on an action from an `mcp`-scope session returns the unknown-tool error and the store never runs it — proven by an empty call log. **(c)** An `admin`-scope session sees and calls the same action successfully. **(d)** Action tools consume no Risk-1 catalog budget, because agents never see them. **(e)** An `mcp`-scope `tools/call` on the **hashed** backend name `<hash>_<action>` returns the unknown-tool error, and the store's call log is empty. This exercises the second dispatch entry point of fact 11b, which the scope filter does not cover.

### Per workstream

**A — `proxy-prompts-resources`**
- [ ] A-1 `openspec validate proxy-prompts-resources --strict` passes.
- [ ] A-2 `tests/fake_mcp_server.py` gains mode `skills`: one tool, one same-named prompt, one resource, one template.
- [ ] A-3 `prompts/get` and `resources/read` reach the child and return the body.
- [ ] A-4 Muting the namespace empties all three lists; a refused read appends no line to `MCPFLOW_CALL_LOG`.
- [ ] A-5 The `tool-visibility` spec records the cross-kind reach of a tool mute.
- [ ] A-6 **Prediction, settled by A-2..A-4:** `src/mcpflow/` needs no diff.

**B — `child-catalog-freshness`**
- [ ] B-1 `Settings.child_cache_ttl`; `CHILD_CACHE_TTL` default `300`; negative raises `ConfigError`.
- [ ] B-2 `ServerSpec.cache_ttl: float | None = None`; `null` inherits; negative fails validation.
- [ ] B-3 `_build_provider` passes the resolved value.
- [ ] B-4 A test **primes the cache first**, then proves resolution under `0` and failure under a long TTL (`.venv/.../fastmcp/server/providers/proxy.py:943-946`).
- [ ] B-5 `README.md` gains the row and the override line.
- [ ] B-6 A dashboard edit of the `skills` child leaves `cache_ttl` at `0`.
- [ ] B-7 After registration, `servers.json` and `GET /api/servers` both read `cache_ttl: 0`.

**C — `child-instructions`**
- [ ] C-1 One block per running, unmuted child that answers `instructions://self`, under `## <namespace>`, in registry order.
- [ ] C-2 A `starting`, `failed`, or `stopped` child contributes no block.
- [ ] C-3 A root or namespace mute removes the block. *`hide_all` is the whole gate.*
- [ ] C-4 A missing resource, a raising read, and a read past its 2 s bound each contribute no block, log at WARNING, and never fail `initialize`. **The handler must not mark the child `failed`.** *Fixture: mode `slowres`.*
- [ ] C-5 `initialize` and `server/discover` return the same string (`.venv/.../fastmcp/server/low_level.py:367`, `:425`).
- [ ] C-6 Changing the content behind `instructions://self` shows in the next `initialize`. **The child under test inherits the 300 s default.** *Fixture: mode `instructions`.*
- [ ] C-7 With no such child, the gateway answers `instructions: null`.
- [ ] C-8 The spec states `instructions://self` generically. No mcpflow source file names a skill.
- [ ] C-9 A block past the 16 KB per-child cap is truncated, ends with one marker line, and logs at WARNING. *Fixture: mode `bigres`.*
- [ ] C-10 **Invariant test:** the supervisor's direct client and the proxy's backend client produce equal `StdioTransport._session_options` — neither passes `transport_options` nor a proxy session class. The test fails if either grows one (fact 9).

**E — `child-actions-page`**
- [ ] E-1 `Supervisor.actions(namespace)` returns only tools whose `_meta["mcpflow"]["action"]` is set, only for a `running` child. *Fixture: mode `action`.*
- [ ] E-2 The page renders one form per action over the E3 subset; an out-of-subset schema is a store bug and the test asserts the store's schemas stay in subset.
- [ ] E-3 A `format: password` field renders masked, is never pre-filled, and never appears in the re-rendered form, the result block, or a log line.
- [ ] E-4 Submitting calls `gateway.call_tool` with the namespaced name under admin scope, and the action runs.
- [ ] E-5 A child with no action metadata renders an empty page; a `stopped` child renders no actions.
- [ ] E-6 An unauthenticated request to the page is redirected by the session gate.
- [ ] E-7 **A muted action renders marked, with its submit disabled**, and no secret leaves the browser for it (T3).
- [ ] E-8 The scope filter is installed by `_build_provider` innermost in the child chain, and removes rather than marks. AC-19 (a)–(c) exercise it end to end.
- [ ] E-9 No file under `src/mcpflow/` contains the word "skill" as a result of this change. *Tension T1: this tests a word, not coupling. Genericity is intended, not proven, until a second child publishes actions.*
- [ ] E-10 **The action contract clause is enforced on both seams.** A tool tagged as an action that also carries `ui.visibility` or `fastmcp.tool_hash` is rejected by `Supervisor.actions()` with a WARNING. It is absent from the page and receives no scope filter. AC-19(e) proves the hash path stays closed for a conforming action.

**D — `skill-store`**
- [ ] D-1 Starts over stdio. `SKILLS_STATE_DIR` must exist and be writable, else the store fails at start with a clear error.
- [ ] D-2 `Backend` has `list_skills`, `read_file`, `write_tree`, `delete_skill`, `writable_capable`. Three implementations pass one shared contract suite **including the D3 replace and delete ordering**.
- [ ] D-3 `Registry` persists to `SKILLS_STATE_DIR/stores.json` at mode `0600`, written with `os.replace`. A seeded file is read at start (break-glass).
- [ ] D-4 One `Provider` serves resources and prompts from the `Catalog`. A rebuild swaps an immutable snapshot and never blocks a read.
- [ ] D-5 `instructions://self` is a **concrete resource, never a template**. Ground: `_list_resources` enumerates concrete resources only, and templates resolve through `_get_resource_template` (`.venv/.../fastmcp/server/providers/proxy.py:1066-1079`); workstream C reads the bare URI over `child.transport`, which never touches the template path.
- [ ] D-6 `set_writable` refuses a store whose `writable_capable` is false; at most one store holds the flag; removing it clears the flag.
- [ ] D-7 A `directory` write stages and swaps; an interrupted write leaves the previous tree whole.
- [ ] D-8 An `s3` replace lists, puts, puts `SKILL.md` last, then deletes absent keys — **no orphan survives a replace**. `delete_skill` removes `SKILL.md` first.
- [ ] D-9 `migrate_skill` verifies with `head_object` per key and deletes the source only after that. A `git` source is never deleted. A re-run after a partial `migrate_store` is idempotent.
- [ ] D-10 No credential appears in any result, resource, or log. `list_stores` returns a fixed mask.
- [ ] D-11 The store publishes all eight actions with `_meta["mcpflow"]["action"]`, **flat schemas only**, and `format: password` on every secret field.
- [ ] D-12 **Bounds:** every S3 call carries `connect_timeout`, `read_timeout`, and a retry cap; every `git fetch` runs under a timeout; `use_skill` carries its own bound. A store past its bound yields a per-store error while the rest of the catalog serves.
- [ ] D-13 The catalog line format keeps the `skills` block under 8 KB for 64 skills across at least two stores, and truncates with `… and N more; call list_skills`.
- [ ] D-14 Store names match `[a-z0-9][a-z0-9-]*`.

---

## 5. Implementation Steps

### Shared test fixtures (prerequisite for C and E)

`tests/fake_mcp_server.py:36-70` selects on `_MODE`; today the modes are `good`, `two`, `slow`, `fail`, `hang` (docstring `:5-14`), and every handler records through `_record` (`:29-33`). Add five:

| Mode | Serves | Used by |
|------|--------|---------|
| `skills` | one tool, one same-named prompt, one resource, one template | A-2 |
| `instructions` | a concrete `instructions://self` backed by a file the test rewrites | C-6 |
| `slowres` | hangs on the resource read only, so the 2 s bound is exercised without a dead child | C-4 |
| `bigres` | returns more than 16 KB from `instructions://self` | C-9 |
| `action` | a tool carrying `_meta["mcpflow"]["action"]` with a `format: password` field; one untagged tool; **and one variant tool that is action-tagged but also carries `ui.visibility: ["app"]` and `fastmcp.tool_hash`**, to exercise the contract clause and AC-19(e) | E-1, E-8, E-10, AC-19 |

### Workstream A — `proxy-prompts-resources`

1. Confirm the no-code prediction from facts 5 and 6.
2. Extend `Only running enabled children are visible` (`openspec/specs/mcp-gateway/spec.md:60`) and `Visibility resolution` (`openspec/specs/tool-visibility/spec.md:21`) to all four kinds; leave the `mcpflow_*` exemption at `:24` untouched.
3. Add mode `skills`.
4. Add `tests/test_proxy_prompts_resources.py`, reusing `fake_child_spec` (`tests/conftest.py:26-39`), `server_factory` (`:100`), `seed_registry` (`:148`).
5. TLA+ `specs/tla/proxy_prompts_resources.tla` — **required**.

### Workstream B — `child-catalog-freshness`

1. Re-point the draft's justification at AC-9.
2. `Settings.child_cache_ttl` (`src/mcpflow/config.py:28-38`); parse beside `:89`, default `"300"` (`.venv/.../fastmcp/server/providers/proxy.py:849`).
3. `ServerSpec.cache_ttl` (`src/mcpflow/registry.py:205-229`), validated in `_check_kind_fields` (`:236-245`); back-compat holds (`:494-501`).
4. Three form sites: `src/mcpflow/web.py:66-79`, `:85-99`, `src/mcpflow/templates/_server_form_fields.html`. **Do not touch `api.py`, `admin_mcp.py`, `importer.py`.**
5. Resolve at `src/mcpflow/supervisor.py:293-297`.
6. Tests including B-6.
7. **No TLA+ model — correct by construction.**

### Workstream C — `child-instructions`

1. Proposal and design; state facts 1, 3, 5, 9.
2. Read seam beside `tools()` (`supervisor.py:899-909`): running children (`:904`); drop `hide_all` (`registry.py:453-461`); direct `Client(child.transport)` per child reading the bare URI, mirroring `_probe_child` (`:944-957`); `asyncio.wait_for(..., 2.0)` inside one gather (`:905`); swallow at WARNING **without** the status transition of `:980-995`; truncate at 16 KB; join under `## <namespace>`.
3. Middleware with `on_initialize` and `on_discover` (`.venv/.../fastmcp/server/middleware/middleware.py:225`, `:232`), returning `result.model_copy(update={"instructions": text})` as `ProxyMetadataMiddleware` does at `.venv/.../fastmcp/server/providers/proxy.py:1272` and `:1289`. Wire with `gateway.add_middleware(...)` (`.venv/.../fastmcp/server/server.py:598`).
4. Tests C-1..C-10 using modes `instructions`, `slowres`, `bigres`.
5. TLA+ `specs/tla/child_instructions.tla` — **required**.

### Workstream E — `child-actions-page`

1. **Proposal and design.** State the contract (`_meta["mcpflow"]["action"]`), the trust argument of E1, and the **spec delta to `mcp-gateway/spec.md:126`**.
2. **Add the action filter.** A transform keyed on the action meta, installed by `_build_provider` (`supervisor.py:279-297`) **innermost** in the child chain. It **removes** like `ScopeFilter` (`admin_mcp.py:70-99`), reads scope as `_is_admin` does (`:87-89`), and fails closed with no token (`:73-77`). The position argument is the one `register_builtin` already documents (`supervisor.py:299-340`).

   > **Do not copy `ScopeFilter` literally.** `ScopeFilter` guards a provider where *every* tool is admin-only, so its `list_tools` returns `[]` and its `get_tool` returns `None` for a non-admin (`admin_mcp.py:91-99`). The action filter guards a child where **most tools are ordinary**. Its `list_tools` must drop **only the tagged tools**, and its `get_tool` must `await call_next(name)` first and then inspect the returned tool's meta. A literal copy would hide every child tool from every `mcp` session and fail AC-19(a).
3. **Add the read seam.** `Supervisor.actions(namespace)` beside `tools()`, following `_probe_child` (`:944-957`), returning raw `Tool` objects, filtered on the action key, `running`, and `_visible()` (`:166-174`).
4. **Add the route and template.** `Route("/servers/{ns}/actions", ...)` beside the other server routes (`src/mcpflow/web.py:941-955`), plus `src/mcpflow/templates/_server_actions.html`, following `_server_detail.html`. The session gate already covers it (fact 12).
5. **Render the E3 subset**, marking a muted action and disabling its submit (E-7).
6. **Submit under admin scope (E2-b).** Pass the gateway into `build_routes` from `build_app` (`src/mcpflow/gateway.py:47-60`); `build_routes` takes none today (`web.py:153-159`). Set `auth_context_var` to an in-memory admin `AccessToken` around the call. **Design-gate item, narrowed to reset discipline:** the token must not outlive the call, must never reach the `TokenStore`, and must never be rendered.
7. **Mask passwords** on render, re-render, and result, as `web.py:600` does for OAuth secrets.
8. Tests E-1..E-9 and AC-19 using mode `action`.
9. **No TLA+ model — stateless render.** The page holds no persistent state and no ordering property. Its safety property is now the **scope filter**, whose predicate is the one `admin_mcp_provider.tla` already models for `mcpflow_*`; extend that model's rejected-variant set rather than writing a second one.

### Workstream D — `skill-store`

1. **Create the code repository.** Fail at start when `SKILLS_STATE_DIR` is missing or read-only.
2. **Build `Backend` and three implementations** against one shared contract suite that includes the D3 orderings. Port `safe_join` from `.venv/.../fastmcp/server/providers/skills/skill_provider.py:81-99`. Apply the D5 bounds.
3. **Build `Registry` and `Catalog`** per the Catalog paragraph in section 3: one `Provider`, immutable snapshot swap, updates from the write's own result, `os.replace` for `stores.json`.
4. **Own registration.** URIs `skill://<store>/<name>/<path>`; prompts `<store>/<name>` with the `<store>__<name>` fallback; store names constrained per D6.
5. **Publish the eight actions** with flat schemas, the action meta, and `format: password`.
6. **Land the four mcpflow changes.**
7. **Register the child**: `kind=python`, `namespace=skills`, `cache_ttl=0`, `env` with `SKILLS_STATE_DIR`. Registration uses the existing route (`src/mcpflow/web.py:941-947`).
8. **Import (AC-7): copy the 64 directories (48 from `~/.agents/skills`, 16 from `~/.claude/skills`) into the directory volume, then `add_store` and `set_writable` on it.** This is chosen over a ninth `import_directory` action for two reasons. It adds **zero new surface**, and it composes with the break-glass path. The eight actions stay frozen as the spec fixes them (spec line 78). The MinIO integration test runs before this step.
9. **Add the three agent entries.**
10. **Verify AC-1..AC-19**, then act on OQ-B and clean up.

### Ordering and dependencies

```
A ─┐
B ─┤
C ─┼─► D6 (land the four changes) ─► D7 (register child) ─► D8 (import + stores) ─► D9 (agents) ─► D10 (verify) ─► cleanup
E ─┘                                      ▲                     ▲
D1..D5 (store code) ──────────────────────┘                     │
MinIO integration test ─────────────────────────────────────────┘
```

- **{A, B, C, E} → D6.** All four land together.
- **D1..D5 → D7**, not D6: the store code must exist before the child is registered.
- **The MinIO integration test gates D8**, because D8 is the first step that writes real objects.
- **E is a hard predecessor of D7**, the step that registers the child. Risk 4 is the reason: until the action filter exists, registering a child that publishes actions hands every agent session `remove_store` and `migrate_store`. **Break-glass replaces the page, not the filter.** Two things remove the need for the *page* at D8: seeding `stores.json` on the volume, and calling actions from an admin-scoped `/mcp` session. Neither removes the need for the *filter* at D7.
- C stays a hard blocker for AC-1, AC-4, and AC-10.

---

## 6. Risks and Mitigations

| # | Risk | Mitigation |
|---|------|------------|
| 1 | The catalog outgrows the agent budget. | **Owner: the store's `Catalog`.** Cap the block at 8 KB; truncate with `… and N more; call list_skills`. **Shorten the line format now, not as a contingency:** cap each description at 60 characters, giving roughly 6 KB for 64 skills with prefixes (re-measure at D-13), against the ~6.7 KB the unshortened format already reaches with one store. **Growth trigger:** re-measure whenever a store is added. **AC D-13 covers it.** A `list_skills` tool is the paginated fallback if the cap ever binds. |
| 2 | One child returns a very large block. | Gateway-wide 16 KB per-child cap (C-9), a different cap with a different owner from Risk 1. |
| 3 | A hung child stalls session start. | Explicit 2 s per child replaces the inherited 60 s (`src/mcpflow/config.py:89`). **Metric:** `initialize` p95. |
| 4 | **An agent calls a store action.** | **The scope filter (E1-a) and AC-19.** Until E lands, the store must not be registered with actions exposed. |
| 5 | **Child `_meta` drives a privileged admin UI.** | Admin-only on both surfaces, flat schemas, password masking — all three together (E1). mcpflow grants a child nothing it did not already have. |
| 6 | **S3 torn read and orphaned keys.** | The D3 ordering deletes absent keys after the new `SKILL.md`. The torn-read window between the supporting PUTs and the `SKILL.md` PUT is **real and stated**, bounded by one small PUT. Closing it fully needs a generation prefix and a pointer object — out of scope; recorded as a follow-up. |
| 7 | S3 list-after-write lag. MinIO is strongly consistent on a single node. Consistency can lag across a distributed setup while it heals. | Never re-list to confirm a write. Update the `Catalog` from the write's own result; `refresh_store` reconciles. `migrate_skill` verifies with `head_object`, a point read. |
| 8 | **A slow backend stalls the agent path or silently drops the catalog block.** | D5 bounds on every S3 call, every `git fetch`, and `use_skill`. Without them the gateway's 2 s bound drops `## skills` and AC-1 fails with no error. |
| 9 | **A rebuild blocks a read.** | The `Catalog` serves from the last-built immutable snapshot; a read never takes the write lock. |
| 10 | Credentials in clear text on the volume. | Mode `0600` opened `O_CREAT` (`src/mcpflow/config.py:55-58`), `os.replace` writes, fixed mask, never returned. **Follow-up: encrypt at rest.** A volume snapshot copies the secrets — state this in the deploy note. |
| 11 | A prompt name containing `/` is rejected by a client. | The `<store>__<name>` fallback, kept injective by the D6 store-name rule. Decide per client during AC-2; test both forms. |
| 12 | A dashboard edit silently resets `cache_ttl`. | The ownership rule, three form sites, AC B-6. |
| 13 | The transport-profile invariant breaks silently. | Fact 9 and the C-10 equality test. |
| 14 | Two store processes race on one `stores.json`. | One `asyncio.Lock` inside one process; `os.replace` keeps the file whole. Run one store process per state directory. |
| 15 | `SKILLS_STATE_DIR` missing or read-only at start. | Fail loudly (D-1). |
| 16 | The executable bit is lost on import. | Stated in D6, not hidden. AC-7's `diff -r` does not compare modes; agents set their own mode on the temp copy. |
| 17 | A `git` poll every 60 s costs a fetch per store. | Assumption A-1, unresolved (OQ-C). Bounded by D5. `refresh_store` exists regardless. |

---

## 7. Verification Steps

**Per mcpflow change (A, B, C, E):**

```
uv run pytest
openspec validate <change-id> --strict
```

**TLA+**, following this repository's convention — `specs/tla/<name>_check.sh` plus one `.cfg` per variant, as `specs/tla/admin_mcp_provider_check.sh` does with five.

```
~/.claude/scripts/install-tlaplus.sh      # only when `tlc` is missing
sany specs/tla/<name>.tla
sh   specs/tla/<name>_check.sh
```

| Workstream | Model | Reason |
|-----------|-------|--------|
| A | `proxy_prompts_resources.tla` — **required** | A resolution predicate over four kinds and three mute levels. |
| B | **no model** | `cache_ttl=0` makes `is_fresh` always false (`.venv/.../fastmcp/server/providers/proxy.py:845-846`). |
| C | `child_instructions.tla` — **required** | `BlockIffRunningAndUnmuted`, `FreshAfterWrite`, plus a rejected configuration reproducing the frozen handshake. |
| E | **no new model** — extend `admin_mcp_provider.tla` | The scope-filter predicate is the one that model already checks for `mcpflow_*`. Add a rejected variant in which a child's action-tagged tool carries no filter, and assert an `mcp`-scope session can reach it. |
| D | `store_registry.tla` — **required** | See below. |

**`store_registry.tla` — corrections from review.** The iteration-4 sketch modelled `writable ∈ {none} ∪ stores`, a scalar, which makes `AtMostOneWritable` unfalsifiable. Instead:

- **`writable ⊆ stores`** with the invariant `Cardinality(writable) ≤ 1`, so a `set_writable` that forgets to clear the previous flag is a **reachable** state the checker can find.
- **`stores.json` is a second variable**, so `PersistedMatchesMemory` is checkable across a crash and reload.
- **`MigrateAtomicity` gets a refinement mapping to the D3 per-object ordering** — copy, `head_object` verify, delete — rather than being an abstract claim with no implementation counterpart.
- **Two properties, not one.** The iteration-5 draft had a single invariant, "the catalog never advertises a skill a reader cannot fully resolve", and claimed it modelled the torn read. It does not: inside the D3 step-2-to-step-3 window every key resolves, because only the *generations* are mixed. Split it:
  - **`CatalogEntryFullyResolvable` — holds.** Every advertised skill has every one of its keys present. This earns its place: it is what fails under delete-before-copy and under an orphan left by a replace.
  - **`SingleGenerationRead` — deliberately violated for `s3`.** Every key a reader resolves belongs to one generation. The D3 window is a **permitted** state, so this property is listed under the rejected configurations with the torn read as its **expected counterexample**. The model then documents the accepted weakness instead of hiding it, and the `directory` backend satisfies both.
- Also: `WritableIsCapable` (AC-17), `RemoveWritableClearsFlag` (spec line 51), `GitSourceSurvives` (AC-14).
- Rejected configurations: the flag on a `git` store; delete-before-copy; a scalar `writable`.

**Store repository:**

```
uv run pytest                      # Backend contract suite across all three kinds
uv run pytest -m integration       # MinIO, requires docker; gates D8
uv run ruff check .
```

**S3 test strategy — both.** `moto` runs in process and needs no docker, so it carries the `Backend` contract suite and keeps CI runnable anywhere. But the target is MinIO, and the two differ exactly where this design is exposed: path-style addressing, `endpoint_url` handling, and list-after-write behaviour. So one docker-compose MinIO test, marked `@pytest.mark.integration`, covers `add_store`, a write, a replace with an orphan, a catalog rebuild, and `migrate_skill`. moto alone would let a MinIO-only defect reach production; MinIO alone would make the unit suite need docker.

**Manual acceptance run (D10):**

1. Register the child with `cache_ttl=0`; confirm B-7.
2. **AC-19 first, before any credential is typed:** with an `mcp`-scope token, confirm `tools/list` shows no action and `tools/call` on one is refused with an empty call log. Then confirm an `admin`-scope token sees and calls it.
3. AC-16: open the actions page; one form per action; masked secret field; a muted action marked and its submit disabled; an empty page for a `remote` child.
4. AC-15: `add_store` a MinIO bucket; confirm `list_stores` masks the secret; grep the log for it.
5. AC-12, AC-13: all three kinds; three catalog lines; the ambiguous and qualified `use_skill` forms.
6. AC-17: `set_writable("git1")` and `write_skill` both error.
7. AC-11: `set_writable("s3main")`; write; objects under its prefix only.
8. D-8: replace a skill that drops a file; confirm no orphan survives.
9. AC-8, AC-5: list prompts and resources; read the three named files byte-for-byte.
10. AC-10: fresh session; confirm the `## skills` block and the other children's blocks. AC-13/D-13: measure the block size.
11. AC-2: load one skill by prompt in all four agents; record whether `/` or `__` was needed.
12. AC-4, AC-9: write from Codex CLI; start a session after 10 s.
13. AC-14: migrate from `dir1` and from `git1`; confirm the delete and the survival; interrupt a `migrate_store` and re-run it.
14. AC-18: push a commit to the git ref.
15. B-6: edit the `skills` child in the dashboard.
16. AC-1: remove `~/.claude/skills` per OQ-B; fresh session; record the trial count.
17. AC-6, AC-3, AC-7.

---

## 8. Open Questions and Stated Assumptions

**Carried from the spec. Do not close these without the user.**

- **OQ-A.** Git host and repository name for the store code. mcpflow's own host is `https://gitlab.kephrenz.nl`. Needed before step D1.
- **OQ-B — decided 2026-09-16: import all 16.** The 16 Claude-only directories in `~/.claude/skills` join the 48 from `~/.agents/skills`; day-one import is 64. AC-1 removes `~/.claude/skills` entirely, as written.
- **OQ-D — decided 2026-09-16: keep workstream E.** The restart-on-registry-change alternative stays recorded in §9 and is rejected.
- **Approval 2026-09-16: proposal stage only.** OpenSpec changes A, B, C, E proceed; implementation is paused until a separate explicit approval. Workstream E's `design.md` MUST bind a UI sketch (`openspec/changes/child-actions-page/sketch.html` + `sketch.png`, on the product stylesheet as `2026-09-12-unify-dashboard-servers` did) and `tasks.md` MUST carry a compare-against-sketch step, so the implementer and verifier work to that design.
- **OQ-C.** Confirm or override **A-1** (git poll every 60 s) and **A-2** (S3 layout: one object per file; a skill exists when its `SKILL.md` exists). A-1 shapes `GitBackend` and AC-18; A-2 shapes the `S3Backend` layout and the D3 ordering.

**Raised by review, unresolved:**
- Is 2 s the right per-child gateway bound, or a fraction of `child_start_timeout`?
- Should the eight actions live on a **separate admin-scoped child** rather than beside the read tools? That would close Risk 4 by construction and make the scope filter per child rather than per tool, at the cost of a second process and a split registry owner. The plan keeps one child and one filter; the design gate may revisit.

- **Should workstream E exist at all?** A third option was never evaluated: keep `stores.json` and the break-glass path, **restart the child on a registry change**, and drop E entirely. That deletes the scope filter, the `mcp-gateway` spec delta, the synthetic admin token, the form renderer, the masking rule, the hash-path contract clause, and roughly nine tests — **the largest new trust surface in this plan**. The ADR's counter-argument (env config leaks S3 credentials into `servers.json`, which `mcpflow_get_server` exposes, `src/mcpflow/admin_mcp.py:177-182`) argues against **env config**, not against restart-per-change: `stores.json` would still hold the credentials on the volume. The real cost is AC-15's no-restart requirement, a sub-second stdio respawn (`src/mcpflow/web.py:948`), and discarded warm git clones. **This is a user decision, not a design-gate one,** because it trades a stated acceptance criterion against a security surface.

---

## 9. ADR

**Decision.** Build the skill store as a FastMCP stdio server, registered as one mcpflow child under `skills`, presenting N stores as one store-qualified catalog held in a single in-memory `Catalog` served by one `Provider` from an immutable snapshot. A `git` store is read-only; `directory` and `s3` are writable-capable; exactly one store holds the writable flag, switched at runtime. mcpflow gains a generic actions page for any child's `_meta["mcpflow"]["action"]` tools, and — new this iteration — **a scope filter that makes those tools admin-only on the MCP surface**. The gateway reads `instructions://self` from each running, unmuted child over `child.transport` per `initialize`, bounded at 2 s.

**Drivers.** DD1 the self-trigger must ride `initialize`; DD2 store management must take effect with no restart; DD3 two owners, one seam, mcpflow never learns "skill".

**Alternatives considered.**
- *Action exposure:* mute action tools to hide them — rejected, because `gateway.call_tool` refuses a disabled tool (`.venv/.../fastmcp/server/server.py:1364-1394`), so hiding them from agents also kills the page. A store-side caller check — rejected, the store cannot see the caller's scope. Accepting the exposure in Non-Goals — rejected outright.
- *Store configuration:* **environment variables instead of a runtime registry.** This would delete `stores.json`, its crash-consistency, the secrets file, the bootstrap path, `store_registry.tla`, **and all of workstream E**. The "restart per change" cost is only a sub-second stdio respawn (`src/mcpflow/web.py:948`). It is rejected on three grounds. AC-15 fixes no-restart as a requirement. A restart discards warm clones. And env config puts store credentials into `servers.json`, which `mcpflow_get_server` exposes to any agent holding the admin tool (`src/mcpflow/admin_mcp.py:177-182`). **E is a consequence of DD2: if AC-15 were relaxed, workstream E could be deleted entirely.**
- *Secrets store:* mcpflow's `CredStore` (`src/mcpflow/oauth.py:159-170`) — rejected, it sits on the wrong side of the seam; its file-mode discipline is copied, its ownership is not.
- *Store registration:* `SkillsDirectoryProvider`, which cannot reach S3 (`.venv/.../fastmcp/server/providers/skills/directory_provider.py:66-68`); a hybrid, which forks the URI shape.
- *S3 client:* aiobotocore (botocore pinning); minio-py (a second dialect, still synchronous).
- *Skill identity:* writable-wins shadowing — rejected at spec round 17.
- *Migration UI:* an HTTP listener in the store — rejected at spec round 18.
- *Instructions carrier:* options (a), (c), (d), (e), and (b1) in section 3.
- *`cache_ttl` ownership:* registry-owned via the merge list (`src/mcpflow/registry.py:343-351`) — closes every caller including the agent's partial dict, but then no caller could clear back to inherit.
- *Import mechanism:* a ninth `import_directory` action — rejected for a copy into the volume, which adds zero new surface and keeps the eight actions frozen as spec line 78 fixes them.

**Why chosen.** Store-qualified names remove a hidden precedence rule. A generic actions page gives one UI and one auth, and keeps the store stdio-only. Owning registration is forced by S3, and the snapshot swap is what makes the catalog both non-blocking and generation-consistent. A live read of `instructions://self` is the only carrier that is current and cheap. Copy-verify-delete is the single ordering rule behind both `write_skill` and `migrate_skill`. The scope filter is the only option that closes the agent-callable hole without disabling the page.

**Consequences.**
- mcpflow gains a middleware, two supervisor methods, a scope-filter transform in the child chain, one `Settings` field, one `ServerSpec` field, three form sites, one route, one template — and **a delta to `mcp-gateway/spec.md:126`**.
- mcpflow now reads child-supplied metadata and acts on it. That is a new trust direction, contained by admin-only, flat schemas, and masking.
- The store holds credentials on a volume at mode `0600`. Encryption at rest is a named follow-up.
- The S3 torn-read window is accepted and stated for day one.
- The executable bit is not preserved across import.
- `cache_ttl` is caller-owned and fails silently when dropped.
- One store process per state directory.
- B needs no TLA+ model (correct by construction). A, C, D, and E each carry one; §7 is authoritative on the obligations.

**Follow-ups.**
1. Encrypt `stores.json` at rest.
2. Close the S3 torn-read window with a generation prefix and a pointer object.
3. A second action-publishing child, to make E's genericity proven rather than intended (T1).
4. Surface on the dashboard which children publish `instructions://self` and which publish actions.
5. `list_skills` as the paginated catalog fallback if the 8 KB cap binds.
6. Widen the E3 subset only if a later child needs it — and never at the cost of the masking rule.
7. Preserve file modes if a skill ever ships something that needs one.

---

## 10. Changelog — iteration 4 to iteration 5

### Spec deltas (added 2026-09-16 after review)

| Delta | Where |
|-------|-------|
| Action-tagged tools are admin-only on the MCP surface via the existing scope filter; delta to `mcp-gateway:126` | §2 fact 13, §3 E1, AC-19, step E-2, Risk 4 |
| `_meta` shape is `_meta["mcpflow"]["action"]` | §2 fact 10, §3 E1, D-11 |
| Action schemas are flat | §3 E3, D-11, Risk 5 |
| Every store backend call carries its own timeout | §3 D5, D-12, Risk 8 |
| **Action contract clause (spec line 79):** no `ui.visibility`, no `fastmcp.tool_hash`, flat schema, validated by mcpflow on read | §2 fact 11b, §3 E1, AC-19(e), E-10, fixture mode `action` |
| **In-memory admin `AccessToken` for the page's submit (spec line 80)** | §3 E2, step E-6 |

### Architect iteration-4 items 1–11

| # | Item | Disposition |
|---|------|-------------|
| 1 | D1-b names owner not mechanism — one `Provider` over the live `Catalog` | **Applied** — the Catalog paragraph, §3; D-4 |
| 2 | **(blocking)** "SKILL.md last" over-claims; needs delete-set; `delete_skill` removes `SKILL.md` first | **Applied** — D3 rewritten; D-8; Risk 6 |
| 3 | `migrate_skill` verify by `head_object`, not LIST | **Applied** — D3; D-9 |
| 4 | **(blocking)** `AtMostOneWritable` vacuous; model `writable` as a set; `stores.json` as a second variable; `os.replace` for it | **Applied** — §7 `store_registry.tla`; D4; D-3 |
| 5 | Bootstrap single-path; break-glass seeding | **Applied** — the Catalog paragraph; D-3; ordering graph |
| 6 | **(blocking)** Import has no surface; pick the copy | **Applied** — step D8; the actions stay frozen at eight |
| 7 | E has no test fixture | **Applied** — the fixtures table, mode `action` |
| 8 | Correction 7 true but not what E depends on | **Applied** — fact 7 demoted |
| 9 | Use `meta["mcpflow"]["action"]`, one reserved top-level key | **Applied** — fact 10; now also a spec constraint |
| 10 | Correction 9 facts wrong, conclusion right; restate C-10 | **Applied** — fact 9; C-10 asserts option equality |
| 11 | Ordering graph edges wrong | **Applied** — §5 graph corrected |

### Critic iteration-4 items 1–18

| # | Item | Disposition |
|---|------|-------------|
| 1a | **CRITICAL** action tools agent-callable | **Applied** — fact 13, E1-a, AC-19, Risk 4 |
| 1b | **CRITICAL** mute-to-hide incompatible with E2-a | **Applied** — E1-b rejected with the mechanism named |
| 1c | D has no bounds | **Applied** — D5, D-12, Risk 8 |
| 1d | Child `_meta` drives a privileged UI; correction 10 mischaracterised `:126` | **Applied** — fact 10 restated as a prohibition; the E1 trust argument; Risk 5 |
| 2 | D3 fail: torn read, orphans | **Applied** — D3 table, D-8, Risk 6 |
| 3 | D4 has zero alternatives | **Applied** — the D4 table with `CredStore` rejected |
| 4 | E3 coverage claim unfounded; require flat schemas | **Applied** — E3 reversed to constrain D-11 |
| 5 | Risk 1 has no owner, no AC, no trigger | **Applied** — owner, cap, trigger, AC D-13, shortened format, `list_skills` |
| 6 | No torn-read/orphan risk row; in-process rebuild deadlock | **Applied** — Risks 6 and 9; the Catalog paragraph |
| 7 | No fixtures for C or E | **Applied** — four new modes |
| 8 | AC-7 import not executable | **Applied** — step D8 |
| 9 | D-6 true by construction; TLA+ gaps | **Applied** — §7 `store_registry.tla` |
| 10 | `write_tree` file modes | **Applied** — D6; Risk 16; AC-7 note |
| 11 | Ordering graph false edges | **Applied** — §5 |
| 12 | Citation `.venv/.../fastmcp/server/providers/proxy.py:1236-1255` → `:1272`/`:1289` | **Applied** — step C-3 |
| 13 | Bare `visibility.py` path | **Applied** — fact 6 fully qualified |
| 14 | C-10 wording | **Applied** — fact 9 |
| 15 | STE trims | **Applied** — long sentences split; "at all" and "for free" dropped; the erasure-coding noun cluster broken up in Risk 7 |
| 16 | Restore C option rejection grounds | **Applied** — §3 C |
| 17 | Gaps: action tools in the agent list; encoding; partial `migrate_store`; `refresh_store` | **Applied** — AC-19(d); D6 encoding; D3 recovery paragraph; `refresh_store` in Risk 7 |
| 18 | Verified-landed list | Acknowledged; no action |

### Tensions recorded

- **T1** E's genericity is **intended, not proven**. E-9 tests a word, not coupling. Follow-up 3 is a second action-publishing child.
- **T2** Two freshness owners — closed by the immutable snapshot swap.
- **T3** A muted action must render marked with its submit disabled, so no secret crosses the wire for a doomed call. AC E-7.

**Declined: none this iteration.** The iteration-2 decline (Critic C13) stands, superseded by the direct-transport caller.

**Open question promoted, not resolved:** the Critic's "separate admin-scoped child for the eight actions" is recorded in §8 rather than decided. It would close Risk 4 by construction, at the cost of a second process and a split registry owner.

---

## 11. Changelog — iteration 6 (improvements pass)

The consensus loop reached its five-iteration cap. Both reviewers converged on the same two blocking items, so this was a paragraph-scale pass, not a redesign.

### Architect iteration-5 items R1–R6

| # | Item | Disposition |
|---|------|-------------|
| R1 | **(blocking)** The "structural" claim overstated: `get_tool_by_hash` never walks transforms. | **Applied** — fact 11b; E1-a restated as display-name only; the contract clause added; AC-19(e); E-10; fixture variant. |
| R2 | **(blocking)** The catalog invariant does not model the torn read. | **Applied** — split into `CatalogEntryFullyResolvable` (holds) and `SingleGenerationRead` (deliberately violated, listed under rejected configurations). |
| R3 | The E2-b spike is closable from source. | **Applied** — §3 E2 now closes it; only reset discipline stays a gate item. |
| R4 | The action filter is not a `ScopeFilter` copy. | **Applied** — the blockquote warning in step E-2. |
| R5 | E is hard before D7, and the prose disagreed with the graph. | **Applied** — ordering prose and the break-glass paragraph. |
| R6 | D-5 cites a line that does not carry the claim. | **Applied** — re-grounded on `_list_resources` and `_get_resource_template`. |

### Critic iteration-5 items R1–R8

| # | Item | Disposition |
|---|------|-------------|
| R1 | **(blocking)** Same as Architect R1, plus the ownership defect: the control sits in the transform layer while dispatch has two entry points. | **Applied** — option (b), spec-as-written, chosen; option (a) recorded as discarded with its MCP-Apps cost and re-sited at the conversion hook. |
| R2 | **(blocking)** Same as Architect R2. | **Applied** — both properties named; the window is a permitted state. |
| R3 | E2-b answerable now. | **Applied.** |
| R4 | E soft in prose, hard in graph. | **Applied.** |
| R5 | Risk 1 cites AC-13, the shadowing criterion. | **Applied** — now cites AC D-13. |
| R6 | Citation drifts: `proxy.py:713`, `dependencies.py:606-618`, eight bare vendored cites. | **Applied** — all vendored cites now carry their full `.venv/.../` path, verified by audit. |
| R7 | STE regressed; 32 sentences over 25 words. | **Applied in part** — every sentence over 33 words in the new prose is split. Some 26-to-30-word lines remain; their word counts are inflated by inline code tokens, and splitting them further would hurt readability. **Partial, deliberately.** |
| R8 | "Add four" above a five-row table. | **Applied** — "Add five". |

### What changed in substance

The plan's central security claim was **wrong**, and both reviewers caught the same thing independently. A scope filter on the transform chain closes one of two dispatch paths. The hash path reads the raw tool cache and is gated only on metadata the child itself supplies. The fix is the spec's contract clause, validated on both seams. Its outcome is stated plainly: **a clause-violating tool is not an action, gets no filter, and stays an ordinary agent-callable tool.** That is a real residual exposure, written down rather than papered over.

**Declined: nothing outright.** Critic R7 is applied in part, with the reason stated above.
