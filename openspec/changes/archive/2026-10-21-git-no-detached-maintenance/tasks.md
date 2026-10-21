## 1. Design gate
- [x] 1.1 Review ownership, seam, and evidence independently (`design-review.md`).
- [x] 1.2 Record `git --version` of the production image in `design.md`.

## 2. Implementation
- [x] 2.1 Add the two `-c` pairs to the argument prefix in `GitBackend._run`.
- [x] 2.2 Add `test_git_refresh_starts_no_maintenance`: set `backend._environment["GIT_TRACE"]` to an absolute path, call `refresh()`, collect the `run_command:` lines, assert the list is not empty, and assert that no line matches `run_command: git (maintenance|gc)\b`.
- [x] 2.3 Add a test that the argument prefix holds `maintenance.auto=false` and `gc.auto=0` (guard for `gc.auto=0`).

## 3. Verification
- [x] 3.1 Remove the flags and see test 2.2 fail.
- [x] 3.2 Run the full suite.
- [x] 3.3 Run an independent review and resolve findings.
- [x] 3.4 Validate the change with `openspec validate` and archive it.
