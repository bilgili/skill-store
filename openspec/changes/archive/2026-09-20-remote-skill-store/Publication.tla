---------------------------- MODULE Publication ----------------------------
EXTENDS Naturals, TLC
CONSTANTS Backend, Fault, AllowCrash, Operation
VARIABLES tree, snapshot, phase
vars == <<tree, snapshot, phase>>
Keys == {"doc", "common", "old", "new"}
Old == [k \in Keys |-> IF k = "new" THEN 0 ELSE 1]
New == [k \in Keys |-> IF k = "old" THEN 0 ELSE 2]
Empty == [k \in Keys |-> 0]
Required(t) == IF t["doc"] = 1 THEN {"doc", "common", "old"}
               ELSE IF t["doc"] = 2 THEN {"doc", "common", "new"} ELSE {}
Complete(t) == \A k \in Required(t): t[k] # 0
Single(t) == \A k \in Required(t): t[k] = t["doc"]
Initial == IF Operation = "create" THEN Empty ELSE Old
Desired == IF Operation = "delete" THEN Empty ELSE New
Init == /\ tree = Initial /\ snapshot = Initial /\ phase = "start"
DirectoryExchange == /\ Backend = "directory" /\ phase = "start"
                     /\ tree' = Desired /\ phase' = "clean"
                     /\ UNCHANGED snapshot
Support == /\ Backend = "s3" /\ phase = "start"
           /\ tree' = [tree EXCEPT !["common"] = 2, !["new"] = 2,
                                  !["old"] = IF Fault = "delete-early" THEN 0 ELSE @]
           /\ snapshot' = IF Fault = "publish-early" THEN tree' ELSE snapshot
           /\ phase' = "support"
Document == /\ phase = "support" /\ tree' = [tree EXCEPT !["doc"] = 2]
            /\ phase' = "document" /\ UNCHANGED snapshot
Cleanup == /\ phase = "document"
           /\ tree' = IF Fault = "orphan" THEN tree ELSE [tree EXCEPT !["old"] = 0]
           /\ phase' = "clean" /\ UNCHANGED snapshot
Publish == /\ phase = "clean" /\ snapshot' = Desired /\ phase' = "done"
           /\ UNCHANGED tree
PostCommitError == /\ Backend = "directory" /\ phase = "clean"
                   /\ snapshot' = IF Fault = "stale-error" THEN snapshot ELSE Desired
                   /\ phase' = "reported-error" /\ UNCHANGED tree
Crash == /\ AllowCrash /\ phase \in {"support", "document", "clean"}
         /\ snapshot' = Empty /\ phase' = "crashed" /\ UNCHANGED tree
Reload == /\ phase = "crashed" /\ snapshot' = tree /\ phase' = "reloaded"
          /\ UNCHANGED tree
Next == DirectoryExchange \/ Support \/ Document \/ Cleanup \/ Publish \/ PostCommitError \/ Crash \/ Reload
Spec == Init /\ [][Next]_vars /\ WF_vars(Reload)
TypeOK == /\ tree \in [Keys -> 0..2] /\ snapshot \in [Keys -> 0..2]
          /\ phase \in {"start", "support", "document", "clean", "done", "crashed", "reloaded", "reported-error"}
CatalogEntryFullyResolvable == Complete(snapshot)
ExternalFullyResolvable == Complete(tree)
SnapshotSingleGeneration == Single(snapshot)
ExternalSingleGeneration == Single(tree)
CompletionNoOrphans == phase = "done" => tree = Desired
ErrorPublicationMatchesTree == phase = "reported-error" => snapshot = tree
RecoveryCompletes == (phase = "crashed") ~> (phase = "reloaded")
=============================================================================
