## Context
The `skills` child runs as a descendant of process 1 in the mcpflow container. Process 1 is `mcpflow serve`, not an init system.
The kernel moves an orphan process to process 1. Process 1 does not call `wait`, so each finished orphan stays as a zombie.
The skill-store process cannot reap an orphan that Git detached on purpose.

## Evidence
- Production: 1965 zombie processes, all children of process 1: 1964 `git` and 1 `git-remote-http`. They appeared at 59 to 60 each hour. The container limit is 2048.
- The production image has Git 2.47.3 (Debian 13) and no SSH client.
- Experiment in the production image, from a Python process 1, `fetch --depth=1` repeated 4 times: no flag gives 4 zombies; `gc.auto=0` gives 4; `maintenance.auto=false` gives 0; both flags give 0.
- Source review (Git 2.30, 2.39, 2.46, 2.47): Git 2.47 and later adds `--detach` to the automatic maintenance command and calls `daemonize()` before it checks any task condition. Git 2.30 to 2.46 runs maintenance in the foreground. Only `gc --auto` can detach, and only when gc is needed.

## Decisions
- **D1. The seam is the argument prefix of `GitBackend._run`.** It holds the only `Popen` of the backend, so one change covers every Git command. A guard at each caller would be wrong.
- **D2. Disable maintenance. Do not make it synchronous.** The cache is derived data. A synchronous gc in the poll loop adds latency and a second timeout path for no benefit.
- **D3. Pass both `maintenance.auto=false` and `gc.auto=0`.** The first flag stops the spawn on Git 2.30 to 2.54. The second flag is a second guard against `gc --auto` on older Git.
- **D4. Discarded: make skill-store a child subreaper.** It would cover the timeout leak (D5) too. We reject it for two reasons. A `waitpid(-1)` loop races with `Popen.wait` in the same process. The subreaper call exists only on Linux.
- **D5. Required follow-up: `init: true` on the mcpflow container (homelab repository).** On a timeout, `killpg` kills `git fetch` before it reaps its helpers. Process 1 receives two zombies for each timed-out fetch. A remote that hangs gives two zombies each minute and fills 2048 slots in about 17 hours. Only a reaper at process 1 fixes this cause. This change does not fix it.
- **D6. Accept the growth of the cache.** See Risks.

## Data flow
The poll loop calls `refresh_store`, then `GitBackend.refresh`, then `_run`. `_run` builds the argument list, starts Git in a new session, waits, and returns the output.
After a command that completes, Git has started no maintenance or gc process. A command that times out can leave helper processes. See D5.

## Risks
- Without automatic gc, each new upstream commit adds one pack with a full snapshot. Five commits of one changed file grew the cache from 28 kB to 170 kB in a probe. Packs never merge.
- The skill repository changes rarely, so we accept the growth. The upgrade path is `-c fetch.unpackLimit=<large value>`, which writes loose objects. Another path is to delete the cache while the server is stopped.
- SSH connection sharing can start a background master process when the user SSH configuration sets `ControlPersist`. The production image has no SSH client, so this change leaves it out of scope.

## Formal model
No TLA+ model: the change adds constant arguments to one command. It has no state, no ordering rule, and no concurrency change. The design review agrees.
