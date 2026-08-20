# Security Policy

## Supported versions

The latest released version receives security fixes.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could destroy or expose local work. Use GitHub's private vulnerability reporting for this repository. Include the affected command, repository state, expected safety invariant, and a minimal reproduction if safe to share.

VersionDrift does not execute shell strings, upload scan data, or collect telemetry. Its mutation boundary is intentionally limited to `git pull --ff-only` after apply-time state revalidation.
