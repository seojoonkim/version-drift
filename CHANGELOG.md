# Changelog

All notable changes to VersionDrift are documented here.

## [1.1.0] - 2026-08-23

### Added

- A read-only agent integration board with immutable intent records, pinned source and target object IDs, deterministic dependency ordering, conflict detection, and fail-closed readiness evaluation.
- `version-drift integrate intent` and `version-drift integrate board` commands for creating intents and evaluating local integration plans without modifying Git repositories.
- Machine-readable integration contracts for agent and automation workflows, including explicit status and reason fields plus structured unknown outcomes.

### Changed

- The project README now documents the integration workflow, safety boundary, and local board model.

### Compatibility

- VersionDrift 1.1 is additive within the 1.x contract. Integration commands are read-only and do not authorize merge, rebase, checkout, reset, fetch, or apply operations.

## [1.0.0] - 2026-08-22

### Added

- Frozen v1 JSON contracts for inspection/events, scan, sync, plan, and doctor, with explicit `complete`, `partial`, and `failed` outcomes.
- `sync --plan` structured observations with explicit fetch controls and no authorization of later apply.
- Read-only `doctor` diagnostics for the runtime, Git, configuration, event history, inbox snapshot, apply locks, and state directory.
- Additive `relation`, `eligibility`, and `reason_codes` facts while retaining legacy report fields.
- Apply lifecycle events, per-repository duplicate-concurrency locks, and post-pull verification including explicit unknown outcomes.
- Compatibility, threat-model, and operations documentation for the supported 1.x contract.

### Changed

- Apply now independently reinspects immediately before execution, blocks unsupported or unknown repository topology, runs only `git pull --ff-only`, and verifies the resulting state.
- Scan and sync envelopes distinguish policy blocks from operationally partial or failed runs.
- Package and runtime versions are synchronized at `1.0.0`; Python 3.9 remains the minimum supported runtime.

### Fixed

- Fail closed for uninspectable explicit scopes, Gitlink-only submodule trees, state/lock/event I/O failures, and successful pulls whose final state cannot be verified; operational scan failures now return a nonzero exit without requiring `--check`.

### Compatibility

- VersionDrift 1.x retains established report fields and safety meanings. Changes within 1.x are additive; removals or semantic breaks require the next major version.

## [0.5.0] - 2026-08-22

### Added

- `version-drift history` for bounded, newest-first, read-only access to the local decision trail.
- Machine-readable `version-drift/history/1` output with path and event filters plus source, malformed, matched, and returned counts.

### Fixed

- Reject negative discovery depths consistently in the CLI and public APIs.
- Refuse explicit symlink roots and avoid dereferencing directory symlinks, external file symlinks, FIFOs, and other special untracked files.
- Treat malformed or negative Git ahead/behind counts as unavailable and fail closed.
- Preserve the real Git index bytes and timestamp during inspection by calculating worktree snapshots against an isolated temporary index.
- Decode unusual Git path bytes without crashing the inspection process.

### Changed

- Shared Git execution and depth validation now enforce the same read-only and fail-closed behavior across scan, inbox, explain, and sync observation paths.

## [0.4.0] - 2026-08-21

### Added

- `version-drift explain` for deterministic, read-only reasons and safe next actions per repository.
- Machine-readable `version-drift/explain/1` output with state counts and shared sync eligibility.

### Changed

- The clean fast-forward predicate is shared by `explain` and `sync`, preventing policy drift.

## [0.3.0] - 2026-08-21

### Added

- `version-drift init` for deterministic platform-local root configuration without scanning repositories.
- `version-drift inbox` for new, changed, and resolved repository drift since the previous checkup.
- Atomic inbox snapshots and machine-readable inbox JSON.

### Changed

- Argument-free scans resolve roots from saved configuration before `VERSION_DRIFT_ROOTS` and the current directory.

## [0.2.1] - 2026-08-21

### Fixed

- Store default decision events in the platform user-state directory instead of the current working directory, preventing VersionDrift from dirtying the repository that launched it.
- Measure and report working-file changes from before/after Git snapshots instead of hardcoding zero.
- Read the CLI version from installed package metadata.
- Replace pre-publication installation instructions with canonical PyPI commands.

## [0.2.0] - 2026-08-21

### Added

- Read-only Git Checkup output with explicit `Working files changed: 0` reporting.
- Positional multi-root scans and `VERSION_DRIFT_ROOTS` defaults.
- `--check` for automation that requires drift-sensitive exit codes.
- Multi-repository dry-run and safe fast-forward application.
- Apply-time repository snapshot revalidation.
- Safety coverage for synced, behind-only, dirty/untracked, ahead, diverged, and state-change cases.

### Changed

- Standalone configuration no longer reads MemKraft-specific environment variables.
- Repository states use user-facing safety categories rather than a generic blocked state.

## [0.1.0] - 2026-08-20

- Initial standalone drift detector and clean fast-forward sync engine.
- Local JSONL event trail using schema `version-drift/1`.
