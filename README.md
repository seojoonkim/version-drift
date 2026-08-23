<div align="center">

# VersionDrift

### Know which Git repositories may advance, and which must not be touched.

A local, fail-closed safety gate for Git checkout drift. VersionDrift observes first and permits one deliberately narrow working-tree update: a verified `git pull --ff-only` for a clean, behind-only repository.

[![PyPI](https://img.shields.io/pypi/v/version-drift?style=flat-square&label=PyPI&color=52d18c)](https://pypi.org/project/version-drift/)
[![Python](https://img.shields.io/pypi/pyversions/version-drift?style=flat-square&color=81c9f3)](https://pypi.org/project/version-drift/)
[![CI](https://img.shields.io/github/actions/workflow/status/seojoonkim/version-drift/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/seojoonkim/version-drift/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/seojoonkim/version-drift?style=flat-square&color=a8b6bf)](https://github.com/seojoonkim/version-drift/blob/main/LICENSE)

<img src="https://raw.githubusercontent.com/seojoonkim/version-drift/main/docs/assets/version-drift-gate.svg" width="100%" alt="VersionDrift observes repositories locally, blocks dirty, ahead, diverged, unsupported, and unknown states, then reinspects, fast-forwards, and verifies only clean behind-only repositories.">

**OBSERVE LOCALLY** · **UNKNOWN = BLOCKED** · **FAST-FORWARD ONLY**

[Quick start](#quick-start) · [Safety contract](#safety-contract) · [Commands](#command-map) · [Agent board](#agent-integration-board) · [JSON contract](#machine-readable-contract)

</div>

## Quick start

Requires **Python 3.9+** and Git on `PATH`.

```bash
uv tool install version-drift
# or: pipx install version-drift
```

```bash
# 1. Save roots. This validates configuration; it does not scan.
version-drift init ~/code ~/work

# 2. Observe local Git state.
version-drift scan
version-drift inbox --fetch

# 3. Preview decisions. A plan is observation, never authorization.
version-drift sync ~/code --plan
# `sync --plan` never authorizes apply.

# 4. Explicitly apply only eligible fast-forwards.
version-drift sync ~/code --apply
```

> **Important:** plan evidence may be stale immediately. Apply fetches according to its own flags, takes a per-repository local lock, independently reinspects immediately before pulling, and verifies the result afterward.

VersionDrift is **local-only** in where it inspects and stores repository facts: it sends **no telemetry** and does not upload repository paths, remotes, or results. Commands using `--fetch`, and `sync` unless given `--no-fetch`, still contact the configured Git remotes.

## Safety contract

VersionDrift is a gate, not a general multi-repository command runner. Discovery remains below roots you explicitly provide or configure.

### The only passing case

An attached, ordinary checkout that:

1. tracks an upstream;
2. has a clean worktree;
3. is behind-only;
4. remains eligible when independently reinspected immediately before apply; and
5. reaches a verified synchronized state after `git pull --ff-only`.

Everything else stays blocked.

### What VersionDrift does

- **Observes** bounded local Git facts: HEAD, upstream, relation, worktree, and required metadata.
- **Fails closed** when a required fact is unknown, unreadable, changed, or unsupported.
- **Applies exactly one Git operation:** `git pull --ff-only`.
- **Verifies afterward:** a successful process exit is not enough to report an applied update.
- **Records locally** for inbox, history, and diagnosis.

### What VersionDrift never does

- reset, stash, clean, merge, or rebase;
- push or force an operation;
- guess an upstream;
- execute arbitrary user-supplied Git commands; or
- treat a scan, dry run, plan, `complete` outcome, or lock as authorization.

Apply is blocked for dirty, ahead, diverged, missing-upstream, detached, in-progress, shallow, linked-worktree, submodule, and unreadable or unknown states. This includes submodule checkouts and repositories containing tracked submodule metadata. A changed head, upstream, worktree fingerprint, eligibility result, or required metadata also blocks apply.

## Command map

Every command supports the global `--base-dir DIR`. Machine-readable commands offer `--json`.

<details open>
<summary><strong>Observe and understand</strong></summary>

### `init` · Save default roots without scanning

```bash
version-drift init ~/code ~/work [--json]
```

Later root precedence is: command-line roots, saved configuration, `VERSION_DRIFT_ROOTS`, then the current directory.

### `scan` · Run a bounded, read-only checkup

```bash
version-drift scan ~/code ~/work [--fetch] [--check] [--json]
version-drift scan ~/code --max-depth 3
```

`--check` exits 1 when drift is present. `--root` remains a repeatable compatibility alias.

### `inbox` · Show changes since the previous snapshot

```bash
version-drift inbox [~/code] [--fetch] [--json]
```

Reports states that are new, changed, or resolved. Snapshots are replaced atomically; corruption is preserved and fails closed instead of silently replacing the baseline.

### `inspect` · Inspect one repository

```bash
version-drift inspect ~/code/project [--fetch] [--json]
```

### `explain` · Get reasons and safe next actions

```bash
version-drift explain ~/code/project [--json]
```

Does not fetch, change Git state, record an event, or update the inbox snapshot.

</details>

<details>
<summary><strong>Plan and apply</strong></summary>

### `sync` · Preview or apply the narrow gate

```bash
version-drift sync ~/code --plan [--fetch | --no-fetch] [--json]
version-drift sync ~/code               # dry-run preview
version-drift sync ~/code --apply [--fetch | --no-fetch] [--json]
```

Plan and dry run never update working files or local branches. Unless `--no-fetch` is supplied, they may refresh remote-tracking refs and tags; they also record local VersionDrift events. Apply uses a per-repository local lock, reinspects, runs only `git pull --ff-only` for eligible repositories, then verifies. Locks reduce duplicate local concurrency; they are not authorization.

</details>

<details>
<summary><strong>Audit and diagnose</strong></summary>

### `history` · Read the newest-first local decision trail

```bash
version-drift history
version-drift history ~/code/project --event scan --limit 20 --json
```

Read-only: never invokes Git, records events, updates snapshots, or creates missing state directories. Malformed JSONL lines are counted and skipped; an unreadable event file fails without modification.

### `doctor` · Check runtime and local state without repair

```bash
version-drift doctor [--json]
```

Checks Python, Git, configuration, events, inbox snapshot, apply locks, and state-directory access. It reports issues but does not create, repair, truncate, or delete state. See the [operations guide](https://github.com/seojoonkim/version-drift/blob/main/docs/OPERATIONS.md).

</details>

`scan`, `inbox`, `explain`, and `sync` also accept `--max-depth N` where applicable. The default discovery depth is 5. Without `--fetch`, `scan`, `inbox`, and `inspect` compare existing local remote-tracking refs. `sync` fetches by default; select `--fetch` or `--no-fetch` explicitly when that distinction matters.

## Agent integration board

The shipped integration board is a local coordination surface for agents that never modifies Git repositories. `intent add` writes only VersionDrift's external intent state; `intent list` and `board` are strictly read-only. Together they pin refs to full commit OIDs, order dependencies deterministically, and keep stale or unknown facts blocked. Give every repository a stable, explicit `--repository-id`, then reuse it for add, list, and board operations.

```bash
# Record an immutable request with source and target pinned to current OIDs.
version-drift integrate intent add ~/code/project \
  --repository-id project-1 \
  --intent-id api-change \
  --agent-id agent-api \
  --source refs/heads/agent/api \
  --target refs/heads/main \
  --summary "Add the API endpoint"

# Record a dependent request. --depends-on is repeatable.
version-drift integrate intent add ~/code/project \
  --repository-id project-1 \
  --intent-id ui-change \
  --agent-id agent-ui \
  --source refs/heads/agent/ui \
  --target refs/heads/main \
  --summary "Use the endpoint" \
  --depends-on api-change

version-drift integrate intent list ~/code/project \
  --repository-id project-1

version-drift integrate board ~/code/project \
  --repository-id project-1 \
  --target refs/heads/main \
  --json
```

`intent add` resolves refs locally and writes only VersionDrift's external local intent state; an existing intent ID is never overwritten. `intent list` and `board` are strictly read-only. A ref moving away from its pinned OID makes an intent `STALE`; an unobservable ref or malformed store is `UNKNOWN`. **UNKNOWN = BLOCKED** for policy purposes.

Board exit codes are `0` for `READY`, `1` for `BLOCKED` or `STALE`, `2` for invalid CLI or repository arguments, and `3` for `UNKNOWN`. Intent/list operational failures follow the general exit-code contract below.

> **Current boundary:** the shipped board does not perform merge-tree analysis, propose or apply integrations, resolve conflicts, acquire leases, create sandboxes or worktrees, or invoke an LLM. Those are future ideas, not current capabilities.

## Machine-readable contract

VersionDrift 1.x freezes the established fields of its safety and report core:

- `version-drift/1` · inspection and event report
- `version-drift/scan/1` · scan envelope
- `version-drift/sync/1` · sync envelope
- `version-drift/plan/1` · plan envelope
- `version-drift/doctor/1` · doctor envelope

Additional command contracts are `version-drift/config/1`, `version-drift/inbox/1`, `version-drift/explain/1`, `version-drift/history/1`, `version-drift/integration-intent/1`, and `version-drift/integration-board/1`.

### Outcomes

- **`complete`** · required observations or operations completed; repositories may still be policy-blocked.
- **`partial`** · some observations or operations failed while others completed; for sync, at least one apply was verified.
- **`failed`** · required observations or operations failed with no verified applicable success, as defined by the command.

Outcome is separate from repository relation and eligibility. **Never infer authorization from `complete`.**

### General exit codes

- **`0`** · completed under the command-specific policy.
- **`1`** · reported non-operational condition or command failure, including `scan --check` drift, an unhealthy doctor, unsuccessful inspection, failed plan observation, or a single-repository sync policy block.
- **`2`** · command-line usage or validation error.
- **`3`** · scan or sync operational `partial` or `failed`, including an uninspectable explicit scope, pull failure, or unverified pull outcome.

JSON consumers should use envelope `outcome` and per-repository reasons as well as the intentionally compressed process exit code. In 1.x, fields may be added but are not removed, moved, renamed without retaining the legacy field, or given incompatible meaning. See the exact [VersionDrift 1.x compatibility contract](https://github.com/seojoonkim/version-drift/blob/main/COMPATIBILITY.md).

## State and privacy

Default state locations:

```text
macOS: ~/Library/Application Support/VersionDrift/events.jsonl
Linux: ${XDG_STATE_HOME:-~/.local/state}/version-drift/events.jsonl
```

`inbox_snapshot.json` and `locks/` live beside `events.jsonl`. Configuration lives at:

```text
macOS: ~/Library/Application Support/VersionDrift/config.toml
Linux: ${XDG_CONFIG_HOME:-~/.config}/version-drift/config.toml
```

`--base-dir` and `VERSION_DRIFT_DIR` select an explicit state root. For compatibility, explicit roots use `.version-drift/` beneath that root.

State can contain local paths, remote URLs, branches, and Git status facts. Treat it as private operational data, exclude it from public logs, and put no secrets in configuration. Event history is append-only during normal recording, but diagnostic rather than tamper-evident. For recovery, held locks, and `outcome_unknown`, follow the [operations guide](https://github.com/seojoonkim/version-drift/blob/main/docs/OPERATIONS.md).

## Boundaries

VersionDrift is a local safety tool, **not a security boundary**, authorization system, sandbox, malware defense, transaction manager, or complete defense against data loss. It trusts the local OS, Python runtime, selected Git executable and configuration, and relevant filesystem behavior. Same-user writers and TOCTOU races remain possible; Git hooks, filters, helpers, remotes, and network behavior are not sandboxed. Keep independent repository backups.

Discovery stays below supplied or resolved roots and does not follow directory symlinks. Apply blocks unsupported topology instead of attempting to make it safe. Review the full [threat model](https://github.com/seojoonkim/version-drift/blob/main/THREAT_MODEL.md), [compatibility contract](https://github.com/seojoonkim/version-drift/blob/main/COMPATIBILITY.md), and [operations guide](https://github.com/seojoonkim/version-drift/blob/main/docs/OPERATIONS.md) before automation.

## Development

```bash
git clone https://github.com/seojoonkim/version-drift.git
cd version-drift
python -m pip install -e . pytest
python -m pytest
```

Bug reports and focused pull requests are welcome. Read [CONTRIBUTING.md](https://github.com/seojoonkim/version-drift/blob/main/CONTRIBUTING.md), see the [changelog](https://github.com/seojoonkim/version-drift/blob/main/CHANGELOG.md), or [open an issue](https://github.com/seojoonkim/version-drift/issues).

VersionDrift is available under the [MIT License](https://github.com/seojoonkim/version-drift/blob/main/LICENSE).
