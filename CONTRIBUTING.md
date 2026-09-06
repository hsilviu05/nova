# Contributing

## Commits

Conventional Commits, scoped by component:

```
feat(api): initialize FastAPI architecture
feat(auth): implement JWT authentication with refresh rotation
fix(auth): commit family revocation before raising on token reuse
feat(device): add device registration
feat(memory): add semantic memory retrieval
test(auth): cover deactivated accounts and expired tokens
docs(adr): record the temporal-split decision
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `build`,
`ci`.

Keep commits small and independently understandable. A reviewer should be able
to read one commit and know what changed and why. If the body needs to explain
*why*, write it — the diff already covers *what*.

## Branches

```
feat/device-websocket-protocol
fix/refresh-token-rollback
docs/architecture-diagrams
```

## Before opening a pull request

```bash
cd services/api
ruff check . && ruff format --check .
mypy
pytest
alembic check
```

All of these run in CI. Running them locally is faster than waiting.

## Code standards

**Follow what is already there.** Match the surrounding naming, structure, and
comment density. New patterns need a reason, and a big enough one to write
down.

**Respect the layering.** Routes translate HTTP. Services decide. Repositories
query. A query in a route handler or a business rule in a repository will be
sent back.

**Handle failure.** Nulls, edge cases, and error paths are part of the
feature, not a follow-up.

**Comment the non-obvious only.** Explain *why*, never restate *what*. A
comment saying `# increment the counter` above `count += 1` is noise; a
comment explaining why a commit must precede a raise is essential.

**No secrets, ever.** Not in code, tests, fixtures, or comments.

**No placeholder code.** No `TODO` stubs in place of core functionality
without agreement first.

## Tests

Non-trivial changes need tests. Failure paths especially: this codebase tests
wrong passwords, expired tokens, replayed tokens, deactivated accounts, forged
signatures, malformed payloads, races, and unreachable dependencies — and that
is the standard for new work.

Name tests for the behaviour they pin down:

```python
def test_replay_revokes_the_whole_family(): ...
def test_unknown_email_is_indistinguishable_from_wrong_password(): ...
```

not `test_refresh_2`.

When a test protects a security property, say so in a docstring. The next
person to read it should know that loosening it is not a refactor.

Prefer a real dependency to a mock where one is cheap. The suite runs against
real PostgreSQL and Redis on purpose.

## Dependencies

Before adding one, say in the pull request:

1. What it does that the standard library does not.
2. How actively it is maintained.
3. Its licence.

Prefer widely used, well-maintained packages. Pin a compatible range.

## Architecture decisions

Anything structural — a new datastore, a protocol change, a framework swap —
gets an ADR in `docs/decisions/`, numbered sequentially, following the
existing format. Record the alternatives and why they lost; that is the part
worth reading in a year.

## Documentation

Update the docs the change touches:

- New endpoint → `README.md` API table
- Structural change → `ARCHITECTURE.md`
- Security-relevant change → `SECURITY.md`
- New setup step or a footgun you hit → `DEVELOPMENT.md`

## Review

Reviews look for correctness first, then architecture, then security, then
tests, then clarity. Feature count is last, deliberately. A smaller system
implemented well beats a larger one implemented approximately.
