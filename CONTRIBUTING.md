# Contributing

Thanks for helping make VersionDrift safer and more useful.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest build
python -m pytest -q
python -m build
```

## Safety invariants

Changes must preserve these contracts:

- `scan` changes no working files, index entries, commits, or branches.
- Dirty, untracked, ahead, diverged, detached, invalid, and missing-upstream repositories remain untouched.
- Apply supports only clean, behind-only repositories.
- State is revalidated immediately before `git pull --ff-only`.
- No reset, automatic stash/drop, clean, merge, rebase, checkout, force-pull, or push command may be introduced.
- Machine-readable output remains compatible with schema `version-drift/1` unless a documented schema migration is included.

Every behavior change needs a focused test. Pull requests should explain which state transition they add or alter.
