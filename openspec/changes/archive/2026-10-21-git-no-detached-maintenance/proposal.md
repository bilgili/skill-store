# Stop detached Git maintenance

## Why
A Git store polls its remote every 60 seconds with `git fetch`.
Git 2.47 and later starts `git maintenance run --auto --detach` on each fetch. The detached process outlives the fetch.
The container runs `mcpflow serve` as process 1, and process 1 does not reap orphans. Each detached process stays as a zombie.
One zombie each minute fills the 2048 process slots of the container in about 33 hours.
Then every fork and thread creation in the container fails with `EAGAIN`.

## What Changes
- Pass `maintenance.auto=false` and `gc.auto=0` on every Git command of the Git backend.
- Add a test that fails when a fetch starts a maintenance or gc command.

## Impact
Only `GitBackend._run` changes. No registry, tool, or wire format changes.
The Git cache is a disposable shallow clone. An operator can delete the cache directory while the server is stopped.
This change does not fix the timeout leak. See design decision D5.
