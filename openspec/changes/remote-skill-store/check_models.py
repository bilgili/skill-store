"""Run accepted models and require specific negative counterexamples."""

import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPECTED = {
    "Registry.cfg": None,
    "Registry-multi.cfg": "AtMostOneWritable",
    "Registry-git.cfg": "WritableIsCapable",
    "Registry-unpersisted.cfg": "PersistedMatchesMemory",
    "Registry-cancel.cfg": "WorkerHasOwner",
    "Publication-directory.cfg": None,
    "Publication-s3.cfg": None,
    "Publication-s3-live.cfg": None,
    "Publication-s3-external-mixed.cfg": "ExternalSingleGeneration",
    "Publication-s3-recovery-mixed.cfg": "SnapshotSingleGeneration",
    "Publication-delete-early.cfg": "ExternalFullyResolvable",
    "Publication-publish-early.cfg": "SnapshotSingleGeneration",
    "Publication-orphan.cfg": "CompletionNoOrphans",
    "Migration-directory.cfg": None,
    "Migration-git.cfg": None,
    "Migration-delete-first.cfg": "NoDataLoss",
    "Migration-bad-verification.cfg": "NoDataLoss",
    "Migration-delete-git.cfg": "GitSourceSurvives",
    "Migration-publish-early.cfg": "CatalogEntryFullyResolvable",
}


def main():
    for module in ("Registry", "Publication", "Migration"):
        result = subprocess.run(
            ["sany", f"{module}.tla"], cwd=ROOT, capture_output=True, text=True, timeout=60
        )
        output = result.stdout + result.stderr
        if result.returncode or "*** Errors:" in output or "Semantic errors:" in output:
            raise SystemExit(output)
    for config, expected in EXPECTED.items():
        module = config.split("-")[0].split(".")[0]
        with tempfile.TemporaryDirectory(prefix="skill-store-tlc-") as directory:
            result = subprocess.run(
                ["tlc", "-workers", "1", "-metadir", directory, f"{module}.tla", "-config", config],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
        output = result.stdout + result.stderr
        valid = (
            (
                result.returncode == 0
                and "Model checking completed. No error has been found." in output
            )
            if expected is None
            else (result.returncode == 12 and f"Invariant {expected} is violated." in output)
        )
        if not valid:
            raise SystemExit(f"Unexpected outcome for {config}:\n{output}")
        counts = re.findall(r"(\d+) states generated, (\d+) distinct states found", output)
        generated, distinct = counts[-1] if counts else ("?", "?")
        print(
            f"PASS {config}: {expected or 'accepted'}; {generated} generated, {distinct} distinct",
            flush=True,
        )
    print(f"All {len(EXPECTED)} model outcomes match.")


if __name__ == "__main__":
    main()
