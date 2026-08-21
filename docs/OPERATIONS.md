# VersionDrift operations

## State layout

Default state is `~/Library/Application Support/VersionDrift/` on macOS and `${XDG_STATE_HOME:-~/.local/state}/version-drift/` on Linux. It contains:

- `events.jsonl`: append-only scan, decision, and apply lifecycle events.
- `inbox_snapshot.json`: the atomically replaced inbox baseline.
- `locks/*.lock`: per-repository apply locks that reduce duplicate local applies.

Configuration is stored in the platform config directory. With `--base-dir` or `VERSION_DRIFT_DIR`, explicit roots use a `.version-drift/` state subdirectory for compatibility.

These files can contain repository paths, remote URLs, branches, and status facts. Keep them private, exclude them from public logs, and put no secrets in configuration. Back up state only if its history/baseline is operationally useful; repository backups remain separate and essential.

## Routine diagnosis

Run `version-drift doctor --json` for automation or `version-drift doctor` for a concise report. Doctor checks Python, Git, configuration, events, snapshot, locks, and state-directory access. It is read-only: it does not create, repair, truncate, or remove state.

During an incident, stop launching applies, preserve command output and the state directory, note repository `HEAD` and `git status`, and inspect events in chronological order. `apply_started` without `apply_verified_success`, or `apply_outcome_unknown` / reason `fast_forward_outcome_unknown`, means the result was not verified. Inspect the repository manually and run a fresh read-only VersionDrift inspection; do not assume the pull failed or succeeded.

## Corrupt or unreadable files

- Malformed `events.jsonl` lines are counted and skipped by history; doctor reports them and never repairs them.
- An unreadable events file fails cleanly without mutation.
- A corrupt `inbox_snapshot.json` is preserved and inbox fails closed rather than replacing its baseline.
- Invalid configuration is reported and preserved.

Before manual recovery, stop VersionDrift writers and make a byte-for-byte backup of the affected state directory. Correct permissions or move a corrupt file aside only after preserving it and understanding that moving an inbox snapshot resets future change comparison. Never edit a file in place while a VersionDrift process may be writing it.

## Held locks

A held lock may represent an active apply or a process that exited unexpectedly. Doctor deliberately reports it as `stale-or-active-unknown`; locks are not auto-stolen. **Never delete a lock without first establishing that no apply process is active** for that repository and that no cooperating VersionDrift process owns it. Check running processes and incident evidence, inspect the lock's diagnostic content, and verify repository state. If inactivity is certain, back up the lock, move it aside, and run doctor plus a fresh read-only inspection before considering another apply.

Locks reduce duplicate concurrency only. They are not authorization, do not coordinate other Git processes, and do not eliminate races.

## Safe manual recovery

1. Stop automated VersionDrift apply jobs and avoid concurrent Git writers.
2. Preserve state files and repository backups.
3. Run doctor and a fresh read-only `inspect` or `scan --no-fetch` equivalent where applicable (scan itself uses no fetch unless requested).
4. Review `git status`, current branch, upstream, and `HEAD` using trusted Git tooling.
5. Resolve repository-specific problems manually according to your team's Git policy; VersionDrift does not prescribe destructive reconciliation.
6. Re-run doctor and a non-applying scan/plan before any new apply.

Do not use reset, stash, or clean as generic VersionDrift recovery steps. Do not erase evidence merely to make doctor green. Escalate unknown topology, uncertain ownership, `outcome_unknown`, or unexpected worktree changes for manual review.
