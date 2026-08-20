# VersionDrift

**Find every out-of-sync Git repo on your machine, without touching your work.**

VersionDrift is a safety-first Git checkup for developers with more repositories than they can keep track of. It scans only the local directories you provide, separates safe fast-forwards from local work that must be protected, and records every decision locally.

```console
$ version-drift scan ~/code --fetch
VersionDrift scanned 17 repositories under ~/code

  ✓ 10  in sync
  ↓  3  safe to update
  !  2  local work protected
  ↑  1  ahead of upstream
  ↕  1  diverged

Safe to update
  docs                             2 commits behind
  website                          4 commits behind

Protected. VersionDrift will not touch these
  client-api                       dirty_worktree
  prototype                        diverged_from_upstream

Working files changed: 0
Remote data: refreshed now
```

The example above illustrates the output format. It is not an adoption claim or benchmark.

## Install

Until the first PyPI release is live, install the versioned GitHub Release wheel:

```bash
pipx install https://github.com/seojoonkim/version-drift/releases/download/v0.2.0/version_drift-0.2.0-py3-none-any.whl
```

After PyPI publication, the canonical commands will be:

```bash
uv tool install version-drift
# or
pipx install version-drift
```

For development:

```bash
git clone https://github.com/seojoonkim/version-drift.git
cd version-drift
python -m pip install -e .
```

## Run your first Git checkup

```bash
version-drift scan ~/code --fetch
```

`scan` never modifies working files, the index, local commits, or branches. Without `--fetch`, it compares against local remote-tracking refs. With `--fetch`, it first runs a non-destructive fetch so the comparison is current.

## Why VersionDrift?

A loop that runs `git pull` everywhere can stop on dirty work, create conflicts, or conceal work behind an automatic stash. VersionDrift takes the opposite approach:

1. Discover repositories only inside roots you provide.
2. Classify every repository before taking action.
3. Protect anything dirty, ahead, diverged, ambiguous, or missing an upstream.
4. Fast-forward only repositories proven safe at execution time.
5. Record every decision locally as JSONL.

## Safety contract

VersionDrift will never automatically:

- reset your working tree
- stash or drop changes
- clean untracked files
- merge or rebase branches
- force-pull or force-push
- guess a missing upstream

`sync --apply` is allowed only when a repository is clean, tracks an upstream, and is behind-only. VersionDrift checks the state again immediately before running exactly `git pull --ff-only`; if the snapshot changed, it aborts.

## Commands

### Scan one or more roots

```bash
version-drift scan ~/code ~/work
```

- `--fetch`: refresh remote-tracking refs first
- `--json`: emit machine-readable output
- `--check`: return exit code 1 when drift exists
- `--max-depth N`: bound discovery depth

Set repeatable default roots with your platform path separator:

```bash
export VERSION_DRIFT_ROOTS="$HOME/code:$HOME/work"
version-drift scan
```

### Inspect one repository

```bash
version-drift inspect ~/code/project --fetch --json
```

### Preview safe synchronization

```bash
version-drift sync ~/code
```

### Apply safe fast-forwards

```bash
version-drift sync ~/code --apply
```

Repositories with local work or ambiguous history remain untouched.

## JSON and local decision events

Every scan and sync decision is appended to:

```text
.version-drift/events.jsonl
```

The current schema is `version-drift/1`. Choose another state root with `--base-dir` or `VERSION_DRIFT_DIR`.

VersionDrift sends no telemetry and never uploads repository paths, remotes, or results.

## VersionDrift, Gita, and myrepos

Gita and myrepos are strong choices for broad multi-repository management or arbitrary commands. VersionDrift is deliberately narrower: fail-closed diagnosis plus clean fast-forward-only reconciliation.

Choose VersionDrift when you want a read-only first run, a fixed non-destructive policy, apply-time revalidation, machine-readable decisions, and a local audit trail.

## MemKraft integration

MemKraft can optionally use the standalone `version_drift` engine. VersionDrift remains independently installable and owns the `version-drift` command.

## Contributing

Bug reports and focused pull requests are welcome. Safety invariants are part of the public API and cannot be weakened for convenience. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
