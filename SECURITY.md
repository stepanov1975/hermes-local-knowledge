# Security Policy

## Supported versions

This repository is a small Hermes Agent plugin. The `main` branch is the only supported development line.

## Reporting a vulnerability

Do not open a public issue for suspected credential leaks or security-sensitive behavior.

Preferred reporting path:

Use GitHub's private **[Report a vulnerability](https://github.com/stepanov1975/hermes-local-knowledge/security/advisories/new)** flow. Do not include vulnerability details in a public issue.

Please include, privately:

- the affected commit or file path;
- a concise description of the issue;
- reproduction steps when applicable;
- whether any credential, private local path, or private document content was exposed.

## Secret handling

The plugin indexes local artifacts and writes generated state under `local_knowledge.state_dir`. Do not commit generated `*.sqlite`, `*.jsonl`, `.env`, or local credential files. The `.gitignore`, Gitleaks config, security workflow, and GitHub-native secret scanning are intended to reduce accidental leaks, not replace manual review.

## Response expectations

Security fixes should be committed normally after local validation. If a real secret is committed, rotate the secret first, then remove it from Git history if needed.
