# VersionDrift

VersionDrift is a standalone, safety-first Git checkout drift detector for developers and coding agents.

It detects dirty worktrees, untracked files, missing upstreams, local-ahead branches, diverged history, invalid HEADs, and clean branches that are behind their upstream. It only applies `git pull --ff-only` when the checkout is clean and fast-forwardable.

## Install

```bash
python -m pip install version-drift
```

## Use

```bash
version-drift scan --root ~/code --max-depth 4 --json
version-drift inspect /absolute/path/to/project --json
version-drift sync /absolute/path/to/project --apply --json
```

Events are written to `.version-drift/events.jsonl` under the current directory by default. Set `VERSION_DRIFT_DIR` or pass `--base-dir` to choose another state directory.

## Safety contract

VersionDrift never runs `git reset --hard`, force-push, destructive checkout, or automatic stash/drop. Dirty, untracked, ahead, diverged, and missing-upstream repositories are reported and left untouched.

MemKraft includes the same `version_drift` engine and exposes it as:

```bash
memkraft version-drift scan --root ~/code --json
```

The two commands use the same module name and behavior without sharing files, entry-point names, or state directories in a way that causes installation conflicts.

License: MIT
