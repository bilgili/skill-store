----------------------------- MODULE Registry -----------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANT Fault
VARIABLES disk, memory, staged, phase, owner, cancelled
vars == <<disk, memory, staged, phase, owner, cancelled>>
Stores == {"directory", "s3", "git"}
Capable == {"directory", "s3"}
Configs == [stores : SUBSET Stores, writable : SUBSET Stores]
Valid(c) == /\ c.writable \subseteq c.stores
            /\ (Fault = "multi" \/ Cardinality(c.writable) <= 1)
            /\ (Fault = "git" \/ c.writable \subseteq Capable)
Empty == [stores |-> {}, writable |-> {}]
Init == /\ disk = Empty /\ memory = Empty /\ staged = Empty
        /\ phase = "ready" /\ owner = FALSE /\ cancelled = FALSE
Begin == /\ phase = "ready"
         /\ \E c \in Configs: /\ Valid(c) /\ staged' = c
         /\ phase' = "writing" /\ owner' = TRUE /\ cancelled' = FALSE
         /\ UNCHANGED <<disk, memory>>
Persist == /\ phase = "writing"
           /\ disk' = IF Fault = "unpersisted" THEN disk ELSE staged
           /\ phase' = "committed"
           /\ UNCHANGED <<memory, staged, owner, cancelled>>
Publish == /\ phase = "committed"
           /\ memory' = staged /\ phase' = "ready" /\ owner' = FALSE
           /\ UNCHANGED <<disk, staged, cancelled>>
Cancel == /\ phase \in {"writing", "committed"} /\ ~cancelled
          /\ cancelled' = TRUE
          /\ owner' = IF Fault = "cancel" THEN FALSE ELSE owner
          /\ UNCHANGED <<disk, memory, staged, phase>>
Crash == /\ phase \in {"writing", "committed"}
         /\ phase' = "crashed" /\ owner' = FALSE
         /\ UNCHANGED <<disk, memory, staged, cancelled>>
Reload == /\ phase = "crashed"
          /\ memory' = disk /\ phase' = "ready"
          /\ UNCHANGED <<disk, staged, owner, cancelled>>
Next == Begin \/ Persist \/ Publish \/ Cancel \/ Crash \/ Reload
Spec == Init /\ [][Next]_vars /\ WF_vars(Reload)
TypeOK == /\ disk \in Configs /\ memory \in Configs /\ staged \in Configs
          /\ phase \in {"ready", "writing", "committed", "crashed"}
          /\ owner \in BOOLEAN /\ cancelled \in BOOLEAN
AtMostOneWritable == /\ Cardinality(disk.writable) <= 1
                     /\ Cardinality(memory.writable) <= 1
WritableIsCapable == /\ disk.writable \subseteq Capable
                     /\ memory.writable \subseteq Capable
RemoveWritableClearsFlag == /\ disk.writable \subseteq disk.stores
                           /\ memory.writable \subseteq memory.stores
PersistedMatchesMemory == phase = "ready" => memory = disk
WorkerHasOwner == phase \in {"writing", "committed"} => owner
RecoveryCompletes == (phase = "crashed") ~> (phase = "ready")
=============================================================================
