# VersionDrift

VersionDrift is a standalone, safety-first Git checkout drift detector for developers and coding agents.

It detects dirty worktrees, untracked files, missing upstreams, local-ahead branches, diverged history, invalid HEADs, and clean branches that are behind their upstream. It only applies `git pull --ff-only` when the checkout is clean and fast-forwardable.

## Install

```bash
python -m pip install version-drift
```

For isolated CLI use:

```bash
pipx install version-drift
```

For local development:

```bash
git clone https://github.com/seojoonkim/version-drift.git
cd version-drift
python -m pip install -e .
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

## Relation to MemKraft

MemKraft can reuse the same `version_drift` engine when it is installed, and it also keeps a fallback path so the MemKraft CLI still works if the standalone package is absent.

```bash
memkraft version-drift scan --root ~/code --json
```

The standalone package and the MemKraft integration do not share entry-point names or runtime state directories, so they can be installed together without collisions.

License: MIT
