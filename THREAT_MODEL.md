# VersionDrift threat model

VersionDrift is a local safety tool, not a security boundary. This model describes what its fail-closed policy protects, the trust boundaries it crosses, and risks it does not eliminate.

## Assets

- Uncommitted working-tree and index content, untracked files, local commits, branches, and repository metadata.
- Correct knowledge of whether a checkout is safe for a clean fast-forward.
- Local event history, inbox baseline, configuration, and diagnostic integrity.
- Privacy of local repository paths, remotes, and results.

## Trust boundaries

VersionDrift crosses boundaries between its Python process, the local filesystem, the selected Git executable and Git configuration, repository metadata/worktrees, remote/network behavior during fetch or pull, and other concurrent local processes. State files and repository files are writable local inputs, not authenticated evidence.

## Protected failure modes and mitigations

- **Destructive reconciliation:** the product has no reset, stash, clean, merge, rebase, push, force, or arbitrary-command path. Apply invokes only `git pull --ff-only`.
- **Unsafe or ambiguous checkout:** dirty, ahead, diverged, missing-upstream, detached, in-progress, shallow, linked-worktree, submodule, and unreadable/unknown topologies are blocked.
- **Stale plan or inspection:** a plan grants no authorization; apply independently reinspects immediately before pull and checks head, upstream, eligibility, and worktree evidence.
- **Duplicate local applies:** per-repository atomic lock files reduce duplicate concurrency. A held lock blocks apply. Locks are not authorization and are not auto-stolen.
- **Unverified pull:** apply records success only after post-pull inspection; otherwise it reports `outcome_unknown`/operational failure.
- **Corrupt diagnostics:** malformed event lines are counted rather than executed; corrupt inbox snapshots are preserved and fail closed; doctor is read-only.
- **Unintended discovery:** scanning is bounded to explicit/resolved roots and depth and does not follow directory symlinks.

## Assumptions

- The operating system, Python runtime, and installed VersionDrift code behave correctly.
- The selected Git executable, its configuration, aliases/exec behavior, hooks, credential helpers, filters, and remote helpers are benign enough to execute requested Git operations faithfully.
- Repository and state permissions appropriately restrict other users; the invoking user intends to inspect the supplied roots.
- Filesystem primitives used for exclusive creation, rename, reads, and writes have their documented local semantics.
- Users review diagnostics and preserve backups appropriate to the value of their repositories.

## Residual risks and out of scope

- A local process with same-user write access can alter repositories, state, locks, configuration, executables, or environment. Lock files do not defend against such a process and can be deleted or forged.
- A malicious or replaced Git executable, Git configuration, hooks, filters, credential helpers, remote helpers, or repository-controlled Git behavior can violate expectations. VersionDrift does not sandbox Git.
- Filesystem or OS compromise, privileged attackers, hardware failure, and hostile backup/synchronization software are out of scope.
- TOCTOU remains possible after the final inspection and during `git pull --ff-only`; locks only coordinate cooperating VersionDrift processes.
- Network endpoints and remotes are untrusted inputs. Fetch/pull may fail, stall until timeout, change tracking refs, expose credentials through Git's configured mechanisms, or deliver malicious repository content.
- Submodule and linked-worktree complexity is blocked for apply but not made safe; unusual worktree, filesystem, alternates, sparse-checkout, filter, and platform behavior may remain residual or out of scope.
- Event and snapshot files are local diagnostics, not tamper-evident audit logs. They may contain sensitive paths and remote URLs.
- Post-pull verification narrows uncertainty but cannot prove absence of side effects from Git, hooks, filesystem races, or another process.

No claim here makes VersionDrift an authorization system, malware defense, sandbox, transaction manager, or complete defense against data loss. Maintain independent backups and use repository access controls.
