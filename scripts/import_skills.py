"""Copy skill trees into a new library and verify every file hash."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def import_skills(source: Path, target: Path, skip_linked_skills: bool = False) -> dict:
    if target.exists():
        raise ValueError("The target must not exist. Existing data is preserved.")
    selected = []
    for skill in sorted(source.iterdir()):
        if skip_linked_skills and skill.is_symlink():
            continue
        if not (skill / "SKILL.md").is_file():
            continue
        if skill.is_symlink():
            raise ValueError("The source contains a linked skill. Select skip-linked-skills.")
        for path in skill.rglob("*"):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise ValueError("A skill contains a symbolic link or special file.")
        selected.append(skill)
    target.mkdir(parents=True)
    hashes = {}
    for skill in selected:
        shutil.copytree(skill, target / skill.name)
        for path in sorted(skill.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source)
                original = hashlib.sha256(path.read_bytes()).hexdigest()
                copied = hashlib.sha256((target / relative).read_bytes()).hexdigest()
                if original != copied:
                    raise ValueError("The imported file does not match its source.")
                hashes[str(relative)] = copied
    return {"skills": len(selected), "files": len(hashes), "sha256": hashes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--skip-linked-skills", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = import_skills(args.source, args.target, args.skip_linked_skills)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"skills": report["skills"], "files": report["files"]}))
