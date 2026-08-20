# Changelog

All notable changes to VersionDrift are documented here.

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
