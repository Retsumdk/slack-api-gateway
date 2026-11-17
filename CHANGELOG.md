# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-17

First release. The gateway is the implementation, not a wrapper: every layer below is testable
without a network and without a token.

### Added

- `SlackGatewayClient` — one chokepoint for every call, with pacing, retries, typed errors,
  read caching and an idempotency ledger.
- Token-bucket pacing per API family (`chat`, `conversations.read`, `conversations.list`,
  `users`, `files`, `reactions`, `auth`, `other`) at Slack's documented per-minute budgets.
- Retry policy with full-jitter exponential backoff, hard `Retry-After` support, and a closed
  classification of retryable HTTP statuses and Slack error codes.
- Exception taxonomy: `RateLimitError`, `TransportError`, `AuthenticationError`,
  `AuthorizationError`, `NotFoundError`, `ValidationError`, `PaginationError`, `ConfigError`,
  all deriving from `SlackGatewayError`, each mapped from Slack's error codes.
- Cursor pagination with loop protection (`PaginationError` on a repeated cursor or the page cap).
- Typed helpers: `auth_test`, `get_channel`, `list_conversations`, `history`, `replies`,
  `post_message`, `update_message`, `delete_message`, `add_reaction`, `remove_reaction`,
  `get_user`, `find_user_by_email`, `list_users`, `upload_file`, plus `call`/`raw` escape hatches.
- File upload through the current external-upload flow
  (`files.getUploadURLExternal` → presigned POST → `files.completeUploadExternal`), with stdlib
  multipart encoding.
- `slack-gateway` command line: `whoami`, `channels`, `history`, `users`, `user`, `post`, `react`,
  `upload`, `raw`, `demo`; stable exit codes (`0`, `1`, `2`, `130`).
- Deterministic offline demo (`slack-gateway demo`) driven by a scripted transport and a virtual
  clock, with its transcript committed as `demo-output.txt` and byte-compared in CI.
- Test suite: 177 tests, 94% statement+branch coverage, no network access, no credentials.
- GitHub Actions CI on Python 3.11/3.12/3.13 plus a wheel build and install-from-wheel smoke test.

### Design decisions worth knowing

- **No runtime dependencies.** HTTP, JSON, form encoding and multipart are standard library, so
  installation cannot fail on a transitive dependency and CI stays deterministic.
- **Time and transport are injected.** Clock, sleep, jitter and transport are constructor
  arguments; that is what makes exact retry assertions possible.
- **The server is the authority.** A Slack `Retry-After` always overrides the local budget.
- **Answers are not hiccups.** Permanent Slack errors are raised, never retried.
- **The idempotency ledger is in-process.** It protects against duplicate delivery inside one
  worker (a retried job step, a repeated webhook); a new process starts with an empty ledger.
