# Independent design review

Date: 2026-09-20.
Verdict: GO for implementation against the amended design.

## Ownership

The store owns registry state, mutation serialization, and immutable catalog publication.
Backends own storage operations. The MCP adapter owns protocol names and resource URIs.
The gateway owns authorization and the Actions page. This separation satisfies the original product boundary.

## Required corrections

1. Retain mutation ownership after caller cancellation until the worker and publication finish.
2. Synchronize the registry file and its parent directory before memory publication.
3. Use native directory exchange on the same filesystem. Refuse unsupported exchange without changing the public tree.
4. Publish successful writes from exact input bytes. Do not use S3 enumeration to confirm writes.
5. Verify migration content through point reads or trusted checksums. Object length alone is insufficient.
6. Reject storage aliases and directory overlap with the state directory or Git caches.
7. State the S3 recovery limit. Interrupted replacement can leave mixed generations until a later successful replacement.
8. Promise one snapshot per request. Separate MCP requests can observe different snapshots.

The amended design contains these corrections. No unresolved blocking design finding remains.

## Model evidence

SANY parses all four modules. TLC checks 35 configurations.
Eleven configurations pass. Twenty-four configurations produce the required counterexample.

| Model | Accepted configurations | Distinct states |
|---|---|---|
| Registry | Registry.cfg | 2,528 |
| Publication | directory | 6 |
| Publication | s3 | 11 |
| Publication | s3-live | 5 |
| Migration | directory | 8 |
| Migration | git | 8 |
| Migration journal | accepted | 30 |

Registry checks separate durable and memory records. Writable values are sets, which makes cardinality failures observable.
It checks capable writable stores, removal, publication order, cancellation ownership, and crash recovery.
Migration checks per-file copy, verification, document-first deletion, Git survival, publication, and retry after a crash.
Publication checks complete catalog reads, obsolete-file cleanup, and directory generation consistency.
The journal checks durable intent, target identity, resumed verification, residue cleanup, and safe refusal.

Negative configurations detect multiple writable stores, writable Git stores, unpersisted publication, and early lock release.
They also detect premature source deletion, bad verification, deleted Git sources, and premature catalog publication.
Publication faults detect missing files, obsolete files, and early snapshot publication.
Two expected counterexamples document S3 behavior: mixed external reads and mixed snapshots after crash recovery.

The final Codex review found no P0, P1, or P2 findings.
The final architecture spot check reported no blockers.

The liveness properties require fair recovery scheduling. Repeated crashes do not guarantee mutation completion.
The models assume atomic filesystem exchange and correct point reads. Backend tests must establish these implementation assumptions.
The finite models do not prove safety against arbitrary external writers or storage corruption.

## Reproduce

Run this command from the repository root:

```sh
python3 openspec/changes/remote-skill-store/check_models.py
```

The runner requires installed `sany` and `tlc` commands.
The local Java runtime requires loopback access for TLC.
The runner checks each counterexample name and process result. Unexpected failures fail the gate.
