# VersionDrift

VersionDrift is a **local-only, fail-closed safety gate** for checking and selectively fast-forwarding Git repositories. It discovers repositories only below roots you provide, reports drift without changing working files, and blocks apply when repository state or topology is dirty, ambiguous, unsupported, or unknown.

VersionDrift does not provide general multi-repository command execution. Its only working-tree update is a verified clean fast-forward using exactly `git pull --ff-only`.

## Requirements and installation

VersionDrift requires Python 3.9 or newer and Git on `PATH`.

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

## Start with a local checkup

```bash
version-drift init ~/code ~/work
version-drift scan
version-drift inbox --fetch
```

- `init` validates and saves roots without scanning.
- `scan` inspects all discovered repositories and records local decision events.
- `inbox` reports repository states that are new, changed, or resolved since its previous snapshot.
- `history` reads the local JSONL decision trail newest-first.
- Configuration precedence is command-line roots, saved configuration, `VERSION_DRIFT_ROOTS`, then the current directory.

Without `--fetch`, observation uses existing local remote-tracking refs. With `--fetch`, `scan`, `inbox`, and `inspect` refresh refs before comparison. Sync fetches by default; select that behavior explicitly with `--fetch` or disable it with `--no-fetch`.

## Safety contract

VersionDrift applies only a clean, attached, ordinary checkout that tracks an upstream and is behind-only. Unsupported or unknown topology is blocked, including detached HEAD, an in-progress Git operation, shallow repositories, linked worktrees, submodule checkouts, repositories containing tracked submodule metadata, and unreadable required Git metadata.

VersionDrift never automatically performs:

- reset, stash, or clean operations;
- merge or rebase operations;
- push or force operations;
- upstream guessing;
- arbitrary user-supplied Git commands.

`sync --apply` independently reinspects each repository immediately before pulling. A prior scan or plan is observation, not authorization. If the head, upstream, worktree fingerprint, eligibility, or required metadata no longer agrees, apply is blocked. The only pull command is `git pull --ff-only`; success is then reinspected and must be verified.

## Commands

### Scan

```bash
version-drift scan ~/code ~/work [--fetch] [--check] [--json]
```

`--check` exits 1 when drift is present. `--max-depth N` bounds discovery.

### Inbox and configuration

```bash
version-drift init ~/code ~/work
version-drift inbox [--fetch] [--json]
```

Inbox snapshots are replaced atomically. A corrupt snapshot is preserved and causes a fail-closed error rather than silently replacing the baseline.

### History

```bash
version-drift history
version-drift history ~/code/project --event scan --limit 20 --json
```

History is read-only: it never invokes Git, records events, updates snapshots, or creates missing state directories. Malformed JSONL lines are counted and skipped; an unreadable event file fails without being modified.

### Inspect and explain

```bash
version-drift inspect ~/code/project [--fetch] [--json]
version-drift explain ~/code/project [--json]
```

`explain` converts inspection states into reasons and safe next actions. It does not fetch, change Git state, record an event, or update the inbox snapshot.

### Plan or synchronize

```bash
version-drift sync ~/code --plan [--fetch | --no-fetch] [--json]
version-drift sync ~/code               # dry-run preview
version-drift sync ~/code --apply [--fetch | --no-fetch] [--json]
```

`sync --plan` records structured decisions but changes no repository. A plan never authorizes a later apply. Dry-run sync also changes no repository. Apply takes a per-repository local lock, reinspects immediately before pull, and verifies afterward. Locks reduce duplicate local concurrency; they are not authorization.

### Doctor

```bash
version-drift doctor [--json]
```

`doctor` performs read-only checks of the Python runtime, Git executable, configuration, event history, inbox snapshot, apply locks, and state directory. It reports issues but does not repair or delete files. See [operations guidance](docs/OPERATIONS.md).

## JSON contracts and outcomes

The frozen v1 schemas are:

- inspection/event report: `version-drift/1`
- scan envelope: `version-drift/scan/1`
- sync envelope: `version-drift/sync/1`
- plan envelope: `version-drift/plan/1`
- doctor envelope: `version-drift/doctor/1`

Other command schemas include `version-drift/config/1`, `version-drift/inbox/1`, `version-drift/explain/1`, and `version-drift/history/1`.

Scan, sync, and plan envelopes use `complete`, `partial`, or `failed`. These describe whether inspection or operations completed, not whether every repository was eligible. A policy-blocked repository can occur in a `complete` run. See [COMPATIBILITY.md](COMPATIBILITY.md) for the exact 1.x API and migration rules.

## Exit codes

- `0`: command completed under its command-specific policy.
- `1`: drift requested by `scan --check`, an unhealthy `doctor`, an unsuccessful inspection, a single-repository policy block, or another reported command failure.
- `2`: command-line usage or validation error, including argparse errors.
- `3`: sync operational failure (`partial` or `failed`), distinct from an ordinary policy block.

JSON consumers should use the envelope `outcome` and per-repository reasons as well as the process exit code.

## Local state and privacy

Default state paths:

```text
macOS: ~/Library/Application Support/VersionDrift/events.jsonl
Linux: ${XDG_STATE_HOME:-~/.local/state}/version-drift/events.jsonl
```

`inbox_snapshot.json` and `locks/` live beside `events.jsonl`. Configuration is at `~/Library/Application Support/VersionDrift/config.toml` on macOS or `${XDG_CONFIG_HOME:-~/.config}/version-drift/config.toml` on Linux. `--base-dir` and `VERSION_DRIFT_DIR` select an explicit state root; for compatibility, explicit roots use `.version-drift/` beneath that root.

State contains local paths and Git metadata. VersionDrift sends no telemetry and does not upload repository paths, remotes, or results. Treat state files as private local operational data and do not put secrets in configuration. See [THREAT_MODEL.md](THREAT_MODEL.md) for boundaries and residual risks.

## Compatibility

The VersionDrift 1.x public contract preserves legacy report fields and permits only additive, orthogonal fields and values that follow the documented compatibility rules. Removals or semantic breaks require a new major version. Safety invariants cannot be weakened for convenience.

## Contributing and license

Bug reports and focused pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). VersionDrift is licensed under the [MIT License](LICENSE).
