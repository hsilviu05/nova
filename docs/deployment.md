# Deployment

One host, one domain, Docker Compose, TLS from Let's Encrypt. This is the
smallest production shape that is actually production: nothing in it is a
development setting with a different name.

**What has and has not been tested.** The compose file validates, the
application behaviour it depends on (the production guardrail, security
headers, the Host allowlist, forwarded‑address rate limiting, the body
cap) was exercised in production mode on the development machine, and
the backup script was run against a real database and its dump restored
into a fresh one. The full stack has **not** been brought up on a public
host: the development environment cannot pull container images, and a
real certificate needs a real domain. Expect the first `up` on a real
host to be the first time Caddy and the API have met.

## What runs

| service | image | published | job |
|---|---|---|---|
| `caddy` | `caddy:2.10-alpine` | 80, 443 | TLS termination, reverse proxy, the only thing on the internet |
| `api` | `ghcr.io/hsilviu05/nova-api:<tag>` | nothing | Migrations on start, then uvicorn with two workers |
| `postgres` | `pgvector/pgvector:pg17` | nothing | The database, on a named volume |
| `redis` | `redis:7.4-alpine` | nothing | Rate‑limit counters and webhook dedupe; 64 MB, LRU, no persistence |
| `backup` | `pgvector/pgvector:pg17` | nothing | `pg_dump` on an interval to a named volume, pruned by age |

Only Caddy publishes a port. An unpublished port is one that cannot be
misconfigured onto the internet, and it is why uvicorn can trust
`X-Forwarded-*` from any peer: the only peer that can reach it is Caddy.

## First deployment

1. A host with Docker, ports 80 and 443 reachable from the internet, and
   DNS for your domain pointing at it. Caddy obtains the certificate on
   first start and fails loudly if the DNS is not there yet.

2. A release image. Push a tag and the release workflow builds and
   publishes it:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

   The workflow's last step runs the image with placeholder settings in
   production mode and requires it to refuse to boot. That is the
   guardrail being tested on the artefact that will actually run.

3. Configuration:

   ```bash
   git clone https://github.com/hsilviu05/nova && cd nova
   cp deploy/env.production.example .env
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # twice
   $EDITOR .env
   ```

   Fill in `NOVA_DOMAIN`, `NOVA_API_TAG`, `NOVA_JWT__SECRET_KEY` and
   `NOVA_DATABASE__PASSWORD`. Everything else has a default. The compose
   file derives the public URL and the Host allowlist from the domain.

4. Start it:

   ```bash
   docker compose -f docker-compose.prod.yml up -d
   docker compose -f docker-compose.prod.yml logs -f api
   ```

   The API log's first lines are the migration, then the workers. If the
   API exits immediately with `refusing to start in production: ...`, the
   message names every setting it objected to; fix `.env` and `up` again.

5. Prove it:

   ```bash
   curl -sD - https://$NOVA_DOMAIN/health
   ```

   Expect a 200 with `strict-transport-security` in the headers. Then
   register a user from the app, claim a device with the simulator, and
   confirm the analytics screen fills in.

## Upgrading

```bash
sed -i 's/^NOVA_API_TAG=.*/NOVA_API_TAG=v0.2.0/' .env
docker compose -f docker-compose.prod.yml pull api
docker compose -f docker-compose.prod.yml up -d api
```

Migrations run on start. Rolling back a version whose migration changed
the schema means `alembic downgrade` first — every migration in this
repository has a tested downgrade — and then the old tag.

## Backups

The `backup` service runs `pg_dump --format=custom` every
`NOVA_BACKUP_INTERVAL_SECONDS` (default a day) into the `nova-backups`
volume and deletes dumps older than `NOVA_BACKUP_KEEP_DAYS` (default 14).
A dump that fails deletes nothing.

Copy them off the host. A backup on the same disk as the database is a
backup against `DROP TABLE`, not against the disk:

```bash
docker run --rm -v nova-prod_nova-backups:/backups:ro -v "$PWD":/out alpine \
    sh -c 'cp /backups/*.dump /out/'
```

### Restore drill

Do this once before you need it, on a scratch database:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
    createdb -U nova nova_restore_test
docker compose -f docker-compose.prod.yml exec postgres \
    pg_restore -U nova -d nova_restore_test --no-owner /backups/nova-<stamp>.dump
docker compose -f docker-compose.prod.yml exec postgres \
    psql -U nova -d nova_restore_test -c 'select count(*) from device_telemetry'
docker compose -f docker-compose.prod.yml exec postgres \
    dropdb -U nova nova_restore_test
```

A real restore is the same with the real database name, after stopping
the API. The dumps include the `vector` extension declaration, so a fresh
database restores without any preparation.

## Operating notes

- **Logs** are JSON on stdout, rotated by Docker at 20 MB × 5 per
  service. `docker compose logs` is the interface; ship them somewhere if
  you want history longer than the rotation.
- **Rate limits** are per client address and are correct behind Caddy;
  see the security review for why.
- **Ollama** is reached at `host.docker.internal:11434` if the provider
  is set to it, which means it runs on the host, not in the stack. On a
  Linux host without a GPU that is a slow chat; the offline or Anthropic
  provider is the realistic choice for a small VPS.
- **Scaling** past one host is not designed for. The migration‑on‑start
  step, the in‑memory device connection registry and the single Redis are
  all one‑host assumptions, and each is called out where it lives.
