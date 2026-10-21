# Design review: git-no-detached-maintenance

Scope: proposal.md, design.md, tasks.md, specs/skill-store/spec.md, `GitBackend` in `src/skill_store/backends.py`, and `_poll` in `src/skill_store/store.py`.

## Verdict
APPROVE WITH CHANGES.

The seam is correct. The two flags stop the steady leak on all Git versions from 2.30 to 2.54.
The design text has a wrong version model and a wrong growth model. The spec makes a claim that the timeout path breaks.
Correct the text before implementation. The code change stays as proposed.

## Findings

### F1 (High): the timeout path leaks two zombies on each timeout
Evidence: a Linux container runs Git 2.53 with Python as process identifier (PID) 1. The probe uses the exact `_run` prefix with both flags.
A local Transmission Control Protocol (TCP) peer accepts connections and never answers. Three fetches time out.
Result: 6 new zombies under PID 1. Three are `git` (the `git remote-http` wrapper) and three are `git-remote-http`.

`os.killpg` kills the whole group at the same time. The `git fetch` process dies before it reaps its helpers.
The kernel moves the helpers to PID 1, and PID 1 does not reap them.
A SIGTERM to the group leader before `killpg` gives the same 6 zombies. The backend cannot fix this. Only a reaper can fix it.

Rate: `GIT_TIMEOUT` is 15 seconds and the poll interval is 60 seconds. A remote that hangs gives 2 zombies each minute.
That fills 2048 slots in about 17 hours. This is two times faster than the original fault.
Consequence: D5 is not defense in depth. It is the only fix for this second cause.
The spec sentence "SHALL NOT start a process that outlives the command" is false on this path.

### F2 (Medium): the version model in Evidence and D3 is wrong
The Git source and the probes show two different mechanisms:
- Git 2.30 to 2.46: `run_auto_maintenance` reads `maintenance.auto`. Then it runs `git maintenance run --auto` in the foreground with `run_command`. Only `gc --auto` can detach, and only when `need_to_gc` is true (more than 50 packs or 6700 loose objects). With `gc.auto=0`, no process detaches.
- Git 2.47 and later: `prepare_auto_maintenance` adds `--detach`, because `gc.autoDetach` defaults to true. `maintenance_run_tasks` calls `daemonize()` before it checks any task condition. Thus each fetch detaches one process, whatever the state of the cache.

The production rate (one zombie for each fetch) matches Git 2.47 or later. The experiment result (`gc.auto=0` still gives 4 zombies) also matches only Git 2.47 or later.
Neither result can occur on Git 2.39. The final stage of the mcpflow Dockerfile is `python:3.12-slim`.
That image family moved to Debian 13 in 2025 (the local `python:3.13-slim` reports Debian 13.7). Debian 13 ships Git 2.47. The statement "bookworm, Git 2.39" is probably wrong.

Linux probe, Git 2.53, Python PID 1, 4 fetches for each case: no flag gives 4 zombies, `gc.auto=0` gives 4, `maintenance.auto=false` gives 0, and both flags give 0.
This result agrees with the production experiment.
macOS probe, Apple Git 2.54, real `GitBackend`, `GIT_TRACE`: the fetch logs `run_command: git maintenance run --auto --no-quiet --detach`. Trace2 shows the `maintenance` `detach` region.
With both flags, neither entry occurs.

The fix stays correct. `maintenance.auto=false` stops the spawn in all versions from 2.30 to 2.54.
Source check: v2.30.0, v2.39.0, v2.46.0, and v2.47.0. Probes: 2.53 and 2.54.
`gc.auto=0` is redundant on these versions. It is a cheap second guard. Keep it, but state the correct reason.

### F3 (Medium): the growth model in Risks is wrong
Probe with both flags, a repository of 300 files, Git 2.53 and 2.54. Ten fetches of an unchanged reference add nothing.
Each new upstream commit adds one pack that holds a full snapshot. Five commits that change one file each give 6 packs. The size goes from 28 kB to 170 kB.
Without automatic garbage collection (gc), the packs never merge. Growth is one full snapshot for each upstream commit, not "few objects".
Object lookups in `ls-tree` and `cat-file` also become slower as the pack count increases. Before this change, Git 2.47 ran gc in the background after 50 packs.

Option with no new code: add `-c fetch.unpackLimit=<large value>` to the same prefix. Git then writes loose objects and does not write an existing object again.
Probe: five commits that change one file each add 20 loose objects (4 for each commit). The size goes from 19 kB to 25 kB, with 0 packs.
The cost is one file for each object. This is acceptable for a skill repository.
The other option is to accept the growth and record it with the numbers above. Either option is acceptable. The current text is not.

### F4 (Low): "Operators can delete the cache directory at any time" is not accurate
`SkillStore._backend` keeps each backend in memory. Only `GitBackend.__init__` runs `git init --bare`.
If an operator deletes the cache while the server runs, each refresh fails until the next start. Write "while the server is stopped".

### F5 (Low): Secure Shell (SSH) connection sharing can leave a detached process
The backend accepts `ssh` URLs. `GIT_SSH_COMMAND` does not disable connection sharing.
If an SSH configuration file sets `ControlMaster` and `ControlPersist`, ssh moves a master process to the background in a new session.
That process is outside the group of `killpg`. It becomes a zombie under PID 1 when it exits.
The production image installs Git with `--no-install-recommends`, so it has no SSH client. Thus production has no risk today.
Add `-o ControlMaster=no -o ControlPath=none` to `GIT_SSH_COMMAND`, or limit the spec to automatic maintenance.

### F6 (Info): ownership and seam are correct
- `backends.py` line 789 is the only `Popen` in `src/` and `scripts/`. The tests call `git` only to make fixtures.
- The constructor runs `init --bare` through `_run`, so the prefix also applies to it.
- Under `GIT_TRACE`, only `fetch` starts child processes. `init`, `rev-parse`, `ls-tree`, and `cat-file` start none.
- The argument prefix is the correct seam. It already holds the constant policy keys. `_environment` holds the conditional secret key.
- A probe shows that `-c` keys and `GIT_CONFIG_COUNT` pairs load together in the command scope. Git reads the environment pairs first, so a `-c` key wins if a key collides. Today no key collides.
- Other daemons do not apply. The fsmonitor daemon needs a work tree and an index. The credential helper list is empty. Remote helpers, `index-pack`, and `unpack-objects` run in the foreground.

### F7 (Info): "No TLA+ model" is correct
Yes. The change adds constant arguments to one command. It has no state, no ordering rule, and no concurrency.

## Required changes

design.md:
1. Evidence: replace "Git 2.39" with the output of `git --version` in the production container. Record the Linux probe of F2.
2. D3: replace the rationale with the two mechanisms of F2. State that `maintenance.auto=false` alone stops the spawn on 2.30 to 2.54. State that `gc.auto=0` is a second guard.
3. D4: remove "handles only one cause", because a subreaper covers both causes. Reject it for two reasons. A `waitpid(-1)` loop races with `Popen.wait` in the same process. The subreaper call exists only on Linux.
4. D5: change "defense in depth" to "required follow-up". Name the timeout leak of F1 and its rate.
5. Data flow: limit "no Git process of that call remains" to a command that completes. State the timeout exception.
6. Risks: replace the growth text with F3. Record the decision: `fetch.unpackLimit`, or accepted growth.

proposal.md:
7. Why: state that Git 2.47 and later detach maintenance on each fetch.
8. Impact: replace "at any time" with "while the server is stopped" (F4).

spec.md:
9. Limit the requirement to commands that complete. Example: "When a Git command of the backend completes, no process that the command started SHALL remain."
10. If F5 stays open, limit the requirement to automatic maintenance and automatic gc.

tasks.md:
11. Task 2.2: use the test below.
12. Task 2.3: make it optional. Task 2.2 already fails without `maintenance.auto=false`. Keep 2.3 only as a guard for `gc.auto=0`.
13. Add a task: record `git --version` of the production image in design.md.
14. Add a task: open a homelab change for `init: true` on the mcpflow container.

## Test recommendation
Add `test_git_refresh_starts_no_maintenance` with the `git_backend` fixture. The test needs no reaping by PID 1 and no timing.
1. Set `backend._environment["GIT_TRACE"] = str(tmp_path / "trace")`. Git needs an absolute path.
2. Call `backend.refresh()`.
3. Collect the lines of the trace file that contain `run_command:`.
4. Assert that the list is not empty. A local fetch always logs `git-upload-pack`. This assertion stops a false pass when tracing does not work.
5. Assert that no line matches the pattern `run_command: git (maintenance|gc)\b`.

The parent `git fetch` writes the `run_command:` line before it starts the child. Thus the assertion is deterministic, and a detached child cannot race it.
Do not match `--detach`. Git 2.30 to 2.46 write the line without it.
Verification on Apple Git 2.54: the current code gives `run_command: git maintenance run --auto --no-quiet --detach`, so the test fails. With both flags the line is absent, so the test passes.

## Probes
The reviewer ran these probes outside the repository. The probes changed no file in the repository.
- `GitBackend` from this worktree with `GIT_TRACE` and `GIT_TRACE2_EVENT`, Apple Git 2.54. The probe injects the flags at `Popen`.
- A copy of the `_run` logic in the local `texlive/texlive` image (Debian, Git 2.53) with `python3` as PID 1 and no network. It counts zombies with parent PID 1 in `/proc`.
- The same container with a TCP peer that never answers, for the timeout path. It tests `killpg` and also SIGTERM before `killpg`.
- Repeated shallow fetches of a repository with 300 files, with and without `fetch.unpackLimit`.
- Git source at tags v2.30.0, v2.39.0, v2.46.0, and v2.47.0 (`run-command.c` and `builtin/gc.c`).
