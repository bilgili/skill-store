------------------------ MODULE MigrationJournal ------------------------
EXTENDS Naturals, TLC
CONSTANT Fault
VARIABLES source, target, sourceView, targetView, intent, phase, verified,
          targetDurable, residue, crashedOnce
vars == <<source, target, sourceView, targetView, intent, phase, verified,
          targetDurable, residue, crashedOnce>>
Keys == {"doc", "support"}
Full == [k \in Keys |-> 1]
Different == [k \in Keys |-> 2]
Empty == [k \in Keys |-> 0]
Visible(tree) == tree["doc"] = 1

Init == /\ source = Full
        /\ target = IF Fault \in {"existing-different", "overwrite-different"}
                    THEN Different ELSE Empty
        /\ sourceView = TRUE /\ targetView = FALSE
        /\ intent = "none" /\ phase = "start" /\ verified = FALSE
        /\ targetDurable = FALSE /\ residue = Empty /\ crashedOnce = FALSE
Record == /\ phase = "start"
          /\ intent' = "copying" /\ phase' = "copy"
          /\ UNCHANGED <<source, target, sourceView, targetView, verified,
                         targetDurable, residue, crashedOnce>>
RejectDifferent == /\ phase = "copy" /\ target = Different
                   /\ Fault = "existing-different" /\ phase' = "blocked"
                   /\ UNCHANGED <<source, target, sourceView, targetView, intent,
                                  verified, targetDurable, residue, crashedOnce>>
Copy == /\ phase = "copy"
        /\ (target \in {Empty, Full} \/ Fault = "overwrite-different")
        /\ target' = Full
        /\ targetDurable' = (Fault \notin {"uncertain-target", "skip-durability"})
        /\ phase' = IF Fault = "skip-durability" THEN "verify" ELSE "reconcile"
        /\ UNCHANGED <<source, sourceView, targetView, intent, verified, residue, crashedOnce>>
Reconcile == /\ phase = "reconcile" /\ target = Full
             /\ targetDurable' = TRUE /\ phase' = "verify"
             /\ UNCHANGED <<source, sourceView, target, targetView, intent,
                            verified, residue, crashedOnce>>
Verify == /\ phase \in {"verify", "reverify"} /\ target = Full
          /\ (targetDurable \/ Fault = "skip-durability")
          /\ verified' = TRUE
          /\ intent' = IF Fault = "no-journal" THEN "none" ELSE "deleting"
          /\ targetView' = TRUE /\ phase' = "delete-public"
          /\ UNCHANGED <<source, sourceView, target, targetDurable, residue, crashedOnce>>
SkipReverify == /\ Fault = "skip-reverify" /\ phase = "reverify"
                /\ verified' = FALSE /\ phase' = "delete-public"
                /\ UNCHANGED <<source, target, sourceView, targetView, intent,
                               targetDurable, residue, crashedOnce>>
BypassMismatch == /\ Fault = "bypass-resume-mismatch" /\ phase = "reverify"
                  /\ target = Different /\ verified' = FALSE
                  /\ phase' = "delete-public"
                  /\ UNCHANGED <<source, target, sourceView, targetView, intent,
                                 targetDurable, residue, crashedOnce>>
DeletePublic == /\ phase = "delete-public"
                /\ source' = Empty /\ residue' = Full
                /\ sourceView' = IF Fault = "stale-source-view" THEN sourceView ELSE FALSE
                /\ intent' = IF Fault = "clear-early" THEN "none" ELSE intent
                /\ phase' = "cleanup"
                /\ UNCHANGED <<target, targetView, verified, targetDurable, crashedOnce>>
Cleanup == /\ phase = "cleanup"
           /\ residue' = IF Fault \in {"partial-delete", "clear-residue"} THEN residue ELSE Empty
           /\ phase' = IF Fault = "partial-delete" THEN "retry-cleanup" ELSE "clear"
           /\ UNCHANGED <<source, target, sourceView, targetView, intent, verified,
                          targetDurable, crashedOnce>>
RetryCleanup == /\ phase = "retry-cleanup" /\ intent = "deleting"
                /\ residue' = Empty /\ phase' = "clear"
                /\ UNCHANGED <<source, target, sourceView, targetView, intent,
                               verified, targetDurable, crashedOnce>>
ClearJournal == /\ phase = "clear"
                /\ (residue = Empty \/ Fault = "clear-residue")
                /\ intent' = "none" /\ phase' = "done"
                /\ UNCHANGED <<source, target, sourceView, targetView, verified,
                               targetDurable, residue, crashedOnce>>
Crash == /\ ~crashedOnce
         /\ phase \in {"copy", "reconcile", "verify", "delete-public", "cleanup",
                        "retry-cleanup", "clear"}
         /\ target' = IF Fault \in {"resume-mismatch", "bypass-resume-mismatch"}
                          /\ intent = "deleting" /\ source = Full THEN Different ELSE target
         /\ verified' = FALSE /\ sourceView' = FALSE /\ targetView' = FALSE
         /\ phase' = "crashed" /\ crashedOnce' = TRUE
         /\ UNCHANGED <<source, intent, targetDurable, residue>>
Reload == /\ phase = "crashed"
          /\ sourceView' = Visible(source) /\ targetView' = Visible(target)
          /\ phase' = IF intent = "deleting" THEN "reverify" ELSE "copy"
          /\ UNCHANGED <<source, target, intent, verified, targetDurable, residue, crashedOnce>>
Next == Record \/ RejectDifferent \/ Copy \/ Reconcile \/ Verify \/ SkipReverify
        \/ BypassMismatch \/ DeletePublic \/ Cleanup \/ RetryCleanup
        \/ ClearJournal \/ Crash \/ Reload
Spec == Init /\ [][Next]_vars
       /\ WF_vars(Record) /\ WF_vars(RejectDifferent) /\ WF_vars(Copy)
       /\ WF_vars(Reconcile) /\ WF_vars(Verify) /\ WF_vars(DeletePublic)
       /\ WF_vars(Cleanup) /\ WF_vars(RetryCleanup) /\ WF_vars(ClearJournal)
       /\ WF_vars(Reload)
TypeOK == /\ source \in [Keys -> 0..2] /\ target \in [Keys -> 0..2]
          /\ sourceView \in BOOLEAN /\ targetView \in BOOLEAN
          /\ intent \in {"none", "copying", "deleting"} /\ verified \in BOOLEAN
          /\ targetDurable \in BOOLEAN /\ residue \in [Keys -> 0..1]
          /\ crashedOnce \in BOOLEAN
          /\ phase \in {"start", "copy", "blocked", "reconcile", "verify", "reverify",
                         "delete-public", "cleanup", "retry-cleanup", "clear", "done", "crashed"}
JournalBeforeDelete == phase \in {"delete-public", "cleanup", "retry-cleanup", "clear"}
                       => intent = "deleting"
VerifiedBeforeDelete == phase \in {"delete-public", "cleanup", "retry-cleanup", "clear"}
                        => verified /\ target = Full
DurableBeforeDelete == phase \in {"delete-public", "cleanup", "retry-cleanup", "clear"}
                       => targetDurable
CatalogNoGhost == (sourceView => Visible(source)) /\ (targetView => Visible(target))
MismatchPreservesSource == phase = "reverify" /\ target = Different => source = Full
DifferentTargetPreserved == Fault \in {"existing-different", "overwrite-different"}
                            => target = Different /\ source = Full
CompletionClearsState == phase = "done" => source = Empty /\ residue = Empty /\ intent = "none"
StableRetryCompletes == intent # "none" /\ Fault \in {"none", "partial-delete"} ~> phase = "done"
=============================================================================
