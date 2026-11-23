# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub's
[private vulnerability reporting](https://github.com/ZachGSnow/zachcap/security/advisories/new).
Include what you found, how to reproduce it, and the impact you expect.

You can expect an acknowledgement within 7 days. Fixes are released as a patch version
and credited in the release notes unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes.

## Secrets and local data

This repository never contains real credentials, recordings or transcripts. API keys are
stored in Windows Credential Manager (or read from environment variables) and are never
written to files. The local engine listens on 127.0.0.1 only and requires a random token
that the desktop app generates at each launch.

Every push is scanned with [gitleaks](https://github.com/gitleaks/gitleaks).
