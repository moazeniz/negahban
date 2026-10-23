# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Repository skeleton: README, deployment / architecture / operations / security docs, Makefile, CI workflow,
  `.env.example`, `liara.json`, ignore files.
- `tools/liara_cli.py`: stdlib operator CLI for the Liara API (`get`, `post`, `delete`, `deploy`, `wait`,
  `releases`, `rollback`); credentials from env vars or a 600-mode env file.
- `tools/openapi/`: Liara PaaS and DNS OpenAPI specs; `docs/LIARA_API_REFERENCE.md`: verified API reference.

### Pending
- Application code (`negahban/`, `static/`, `demo-shop/`, `deploy/`, `tests/`, `Dockerfile`, `requirements.txt`)
  to be synced from the build host; first tagged release `1.0.0` after the first Liara deployment of `negahban-sre`.
