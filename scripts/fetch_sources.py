"""Fetch only pinned public repositories into ignored, disposable build contexts."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]


def checkout(dest, commit):
    # Clean contexts follow sources.json when a pin changes; local edits are never discarded.
    subprocess.run(["git", "-C", str(dest), "fetch", "--depth=1", "origin", commit], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", "--no-overwrite-ignore", "--detach", commit], check=True)


for name, source in json.loads((root / "sources.json").read_text()).items():
    dest = root / ".sources" / name
    if dest.exists():
        if subprocess.check_output(["git", "-C", str(dest), "status", "--porcelain", "--untracked-files=all"], text=True):
            raise SystemExit(f"{dest} has local edits. Commit or discard them, "
                             "or build with docker compose directly while editing.")
        actual = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
        if actual == source["commit"]:
            continue
        # Keep any local commit on the old detached HEAD reachable after the move.
        subprocess.run(["git", "-C", str(dest), "update-ref", f"refs/sandbox/previous/{actual}", actual], check=True)
        print(f"Updating {name} from {actual[:12]} to pinned commit {source['commit'][:12]}", flush=True)
        checkout(dest, source["commit"])
        continue
    subprocess.run(["git", "clone", "--no-checkout", "--filter=blob:none", source["url"], str(dest)], check=True)
    checkout(dest, source["commit"])
