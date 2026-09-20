----------------------------- MODULE Migration -----------------------------
EXTENDS Naturals, TLC
CONSTANTS SourceKind, Fault
VARIABLES source, target, sourceView, targetView, phase, verified
vars == <<source, target, sourceView, targetView, phase, verified>>
Keys == {"doc", "support"}
Full == [k \in Keys |-> 1]
Empty == [k \in Keys |-> 0]
Complete(t) == t["doc"] = 0 \/ (\A k \in Keys: t[k] = 1)
Init == /\ source = Full /\ target = Empty /\ sourceView = Full /\ targetView = Empty
        /\ phase = "start" /\ verified = FALSE
Start == /\ phase = "start"
         /\ source' = IF Fault = "delete-first" THEN Empty ELSE source
         /\ phase' = "copy"
         /\ UNCHANGED <<target, sourceView, targetView, verified>>
CopySupport == /\ phase = "copy"
               /\ target' = [target EXCEPT !["support"] = IF Fault = "bad-verification" THEN 0 ELSE 1]
               /\ phase' = "document"
               /\ UNCHANGED <<source, sourceView, targetView, verified>>
CopyDocument == /\ phase = "document"
                /\ target' = [target EXCEPT !["doc"] = 1]
                /\ targetView' = IF Fault = "publish-early" THEN [Empty EXCEPT !["doc"] = 1] ELSE targetView
                /\ phase' = "verify"
                /\ UNCHANGED <<source, sourceView, verified>>
Verify == /\ phase = "verify"
          /\ (target = Full \/ Fault = "bad-verification")
          /\ verified' = TRUE /\ phase' = "delete"
          /\ UNCHANGED <<source, target, sourceView, targetView>>
Delete == /\ phase = "delete"
          /\ source' = IF SourceKind = "git" /\ Fault # "delete-git" THEN source ELSE [source EXCEPT !["doc"] = 0]
          /\ phase' = "delete-files"
          /\ UNCHANGED <<target, sourceView, targetView, verified>>
DeleteFiles == /\ phase = "delete-files"
               /\ source' = IF source["doc"] = 0 THEN Empty ELSE source
               /\ phase' = "publish"
               /\ UNCHANGED <<target, sourceView, targetView, verified>>
Publish == /\ phase = "publish" /\ targetView' = target /\ sourceView' = source
           /\ phase' = "done"
           /\ UNCHANGED <<source, target, verified>>
Next == Start \/ CopySupport \/ CopyDocument \/ Verify \/ Delete \/ DeleteFiles \/ Publish
Spec == Init /\ [][Next]_vars
TypeOK == /\ source \in [Keys -> 0..1] /\ target \in [Keys -> 0..1]
          /\ sourceView \in [Keys -> 0..1] /\ targetView \in [Keys -> 0..1]
          /\ verified \in BOOLEAN
NoDataLoss == source = Full \/ target = Full
GitSourceSurvives == SourceKind = "git" => source = Full
VerifyBeforeDelete == (phase \in {"delete-files", "publish"}) => verified /\ target = Full
CatalogEntryFullyResolvable == Complete(sourceView) /\ Complete(targetView)
=============================================================================
