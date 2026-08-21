# VersionDrift 1.x compatibility contract

This document freezes the public machine-readable and safety contract for VersionDrift 1.x. It distinguishes **API promises**, which consumers may rely on throughout 1.x, from implementation details, which may change without notice.

## Public API promises

### Reports and schemas

The following schema identifiers and their established fields are public:

- `version-drift/1`: per-repository inspection reports and event records.
- `version-drift/scan/1`: scan envelope with `outcome`, `summary`, and `projects`.
- `version-drift/sync/1`: sync envelope with `outcome`, `summary`, and `projects`.
- `version-drift/plan/1`: plan envelope with generation/fetch facts, `outcome`, `summary`, repository decisions, and an authorization disclaimer.
- `version-drift/doctor/1`: read-only diagnostic envelope with ordered named checks and aggregate `ok`.

Legacy `version-drift/1` fields—including `state`, `reasons`, `actions`, `ok`, `safe_to_update`, path/ref facts, and working-file measurements—are retained. New fields such as `relation`, `eligibility`, and `reason_codes` are additive and orthogonal: they clarify independent facts rather than changing legacy field meaning. Consumers must ignore unknown fields and tolerate new reason/event values that follow existing safety semantics.

Other existing command schemas (`version-drift/config/1`, `version-drift/inbox/1`, `version-drift/explain/1`, and `version-drift/history/1`) remain command contracts, but the five schemas above define this release's frozen safety/report core.

### Outcomes

For scan, sync, and plan envelopes:

- `complete`: all required observations/operations completed. Repositories may still be policy-blocked; blocked is a safety decision, not an operational failure.
- `partial`: some observations or operations failed while others completed (for sync, at least one apply was verified).
- `failed`: required observations or operations failed with no verified applicable success, as defined by that command.

Outcome is separate from repository relation and eligibility. Consumers must not infer authorization from `complete`.

### Exit codes

- Exit code `0`: command completed under its command-specific policy.
- Exit code `1`: a reported non-operational condition or command failure, including `scan --check` drift, doctor issues, an unsuccessful inspect, a failed plan observation, or a single-repository sync policy block.
- Exit code `2`: argparse usage or command validation error.
- Exit code `3`: scan or sync operational `partial` or `failed` outcome, including an uninspectable explicit scope, pull failure, or an unverified pull outcome.

The JSON outcome and per-repository reason remain authoritative details; exit codes intentionally compress them.

### Stable reason names

The following established reason codes retain their meaning in 1.x:

- Input/inspection: `path_missing`, `not_git`, `fetch_failed`, `git_metadata_unreadable`, `worktree_status_unavailable`, `invalid_head`, `ahead_behind_unavailable`, `scope_inspection_failed`, `event_write_failed`.
- Local safety: `dirty_worktree`, `missing_upstream`, `detached_head`, `operation_in_progress`, `shallow_repository`, `linked_worktree`, `submodule_checkout`, `contains_submodules`.
- Relation: `diverged_from_upstream`, `local_ahead_of_upstream`, `local_behind_upstream`, `in_sync_no_action`.
- Sync result reasons: `only_clean_fast_forward_sync_is_allowed`, `dry_run_fast_forward_available`, `apply_lock_held`, `apply_lock_io_failed`, `state_inspection_failed`, `state_changed_before_apply`, `fast_forward_applied`, `fast_forward_failed`, `fast_forward_outcome_unknown`.

New reason names may be added in 1.x when they make an existing fail-closed decision more specific. Existing names will not be reused with incompatible meaning.

### Stable event names

Established event names are `scan`, `inspect`, `decision_recorded`, `sync_blocked`, `sync_dry_run`, `sync_state_changed`, `apply_started`, `apply_failed`, `apply_verified_success`, `apply_outcome_unknown`, `sync_applied`, and `sync_failed`. New lifecycle events may be added; consumers must not require a closed enumeration or assume one event per command.

### Plan and apply

A plan is an observation, never authorization. Plan evidence may be stale immediately. `sync --apply` independently fetches according to its own flags, acquires a local duplicate-concurrency lock, and reinspects immediately before running only `git pull --ff-only`. Any changed or unknown required fact blocks apply. A successful pull is not reported as applied until post-pull inspection verifies the synchronized state.

### JSONL diagnostics

Event history is append-only during normal recording. Read-only history and doctor diagnostics never repair it. History counts and skips malformed JSONL lines; an unreadable file produces a clean command failure and is not changed. Doctor reports malformed lines as an issue and performs no repair. Inbox snapshot corruption is preserved and fails closed rather than silently creating a new baseline.

## 1.x evolution and migration rules

- 1.x releases may add orthogonal fields, reason/event values, doctor checks, or optional commands.
- Existing fields, schema identifiers, exit-code classes, and safety meanings are not removed or semantically broken in 1.x.
- Consumers should ignore unknown object fields and unknown fail-closed reason/event values.
- Fields are not moved or renamed without retaining the legacy field for the rest of 1.x.
- Removals, incompatible type/meaning changes, weaker apply checks, or other semantic breaks require the next major version and explicit migration guidance.

## Implementation details, not API promises

Internal Python helper names, private functions, command ordering below the documented safety sequence, hashing/lock filenames, temporary-index strategy, exact prose, timestamps, Git subprocess count, and storage implementation beyond documented paths/formats are not frozen APIs. Locks reduce duplicate concurrency but are not authorization, ownership proof, or a cross-host coordination protocol.
