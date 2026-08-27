# Row-level isolation / project-grant manual test & demo

Demos and manually verifies per-project row-level isolation (RLS): a
`project_grants`/`idp_groups` model synced from OIDC group claims
(`src/phoenix/server/access/idp_sync.py`), enforced in Postgres via a
`phoenix_scoped` role + `USING`/`WITH CHECK` policies on 10 project-scoped
tables (`src/phoenix/server/access/resolution.py`,
`_set_db_isolation_guards` in `src/phoenix/server/app.py`), plus pre-write
project-access checks in the GraphQL/REST annotation mutations.

Reuses `local-dev/keycloak/`'s Keycloak instance and realm (see that
directory's README for the base SSO test matrix) — this just adds
Postgres and a second demo user so isolation is actually observable.

Unlike `local-dev/keycloak/`, this directory is tracked — it's kept
around as a demo rig, not just scratch scaffolding. `phoenix.env` itself
stays untracked (see `.gitignore`); only `phoenix.env.example` is
checked in, same convention as `local-dev/keycloak/`.

**Verified against a real Keycloak + Postgres + Phoenix stack
(2026-08-27)**: every non-optional item in the test matrix below passed,
plus the config-driven-grant-sync and direct-`psql` optional checks. One
real bug was caught and fixed in the process: this directory's own
`phoenix.env` (not `.example`) had a stale, pre-JMESPath-fix
`ROLE_ATTRIBUTE_PATH`, which silently resolved both `bob-user` and
`dave-user` to VIEWER instead of MEMBER — exactly the alphabetical-groups
failure mode described in step 1's note below. `phoenix.env` is
untracked precisely so this can't happen from a stale committed copy;
if you see it, `rm` and re-`cp` from `.example`.

## 1. Start Keycloak and Postgres

```sh
docker compose -f local-dev/keycloak/docker-compose.yml up -d
docker compose -f local-dev/rls-demo/docker-compose.yml up -d
```

The Keycloak realm now seeds four users (all password `password`):

| user             | groups                                | expected Phoenix role | project access  |
|------------------|-----------------------------------------|------------------------|------------------|
| `alice-admin`    | `phoenix-admins`                        | ADMIN                  | all (bypasses RLS) |
| `bob-user`       | `phoenix-users`, `demo-project-alpha`   | MEMBER                 | `demo-team-alpha` only |
| `dave-user`      | `phoenix-users`, `demo-project-beta`    | MEMBER                 | `demo-team-beta` only |
| `carol-outsider` | *(none)*                                | denied at login        | n/a              |

Project access comes from `local-dev/rls-demo/group-mapping.yaml`, synced
into `project_grants` at login time.

`bob-user`/`dave-user` each carry a second, unmapped group
(`demo-project-alpha`/`demo-project-beta`) alongside `phoenix-users`.
Keycloak returns the `groups` claim **alphabetically sorted**, and
Phoenix's role-mapping (`OAuth2Client.extract_and_map_role` in
`src/phoenix/server/oauth2.py`) only looks at the *first* array element
when `ROLE_ATTRIBUTE_PATH` is a plain claim path — so a naive
`ROLE_ATTRIBUTE_PATH=groups` + `ROLE_MAPPING=...` setup (as used in
`local-dev/keycloak`) silently resolves both of them to VIEWER once the
extra group sorts first. `local-dev/rls-demo/phoenix.env.example` works
around this with a JMESPath conditional
(`ROLE_ATTRIBUTE_PATH="contains(groups, 'phoenix-admins') && 'ADMIN' ||
(contains(groups, 'phoenix-users') && 'MEMBER' || 'VIEWER')"`) that
checks membership directly instead of relying on array order — verified
live against this rig.

## 2. Start Phoenix

```sh
make build-frontend    # first time only -- phoenix serve needs static assets on 6006
cp local-dev/rls-demo/phoenix.env.example local-dev/rls-demo/phoenix.env   # first time only
set -a && source local-dev/rls-demo/phoenix.env && set +a
env | grep PHOENIX_SQL_DATABASE_URL   # must print the postgresql:// URL above
make dev-backend
```

First boot against the fresh Postgres database auto-runs the Alembic
migrations, which create the `phoenix_scoped` role and the RLS policies.
Run natively on the host (not the root `docker-compose.yml` stack) for
the same reason as `local-dev/keycloak`: the browser and Phoenix both
need to reach Keycloak at `http://localhost:8080`.

## 3. Seed two demo projects

With `PHOENIX_ENABLE_AUTH=true`, the whole `/v1` router -- including REST
OTLP ingest -- requires a valid bearer credential
(`is_authenticated`, applied router-wide in
`src/phoenix/server/api/routers/v1/__init__.py`). Mint an admin API key
once:

```sh
curl -s -i -X POST http://localhost:6006/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@localhost","password":"admin"}'
# copy the phoenix-access-token cookie value from the response, then:
curl -s -X POST http://localhost:6006/v1/user/api_keys \
  -H "Authorization: Bearer <access token>" \
  -H "Content-Type: application/json" \
  -d '{"data":{"name":"rls-demo-seed-key"}}'
# copy the "key" field from the response
export PHOENIX_API_KEY=<key>
uv run local-dev/rls-demo/seed_projects.py
```

Creates `demo-team-alpha` and `demo-team-beta`, each with a couple of
sample LLM spans. "Ingest bypasses RLS" doesn't mean ingest is
unauthenticated -- it means any authenticated caller can write to *any*
project via ingest regardless of their own project grants, since there's
no per-project write check on this path (unlike the annotation
mutations below).

## 4. Test matrix

- [x] **Admin sees everything**: log in as `alice-admin`. Project list
      shows both `demo-team-alpha` and `demo-team-beta` (ADMIN sets
      `app.bypass_rls`, see `_set_db_isolation_guards`).
- [x] **Member sees only their granted project**: log in as `bob-user`.
      Project list shows only `demo-team-alpha`.
- [x] **A different member sees only theirs**: log in as `dave-user`.
      Project list shows only `demo-team-beta`.
- [x] **In-project annotation succeeds**: as `bob-user`, open a span in
      `demo-team-alpha` and add a span annotation (UI, or GraphQL
      `createSpanAnnotations`, or `POST /v1/span_annotations?sync=true`).
      Succeeds.
- [x] **Cross-project annotation is rejected**: as `bob-user`, get a span
      id from `demo-team-beta` (e.g. from `alice-admin`'s session, or via
      Postgres) and attempt to annotate it. Rejected — GraphQL raises
      `NotFound` (RLS makes the row invisible to `bob-user`'s own
      existence-check `SELECT`, before any `WITH CHECK` is reached), REST
      `sync=true` returns 404. This is the behavior verified in commit
      `9f7f66374`.
- [x] **(optional) Direct-DB verification** — via `psql
      postgresql://postgres:postgres@localhost:5432/postgres`. The
      `set_config(..., true)` third argument makes the setting
      transaction-local, so wrap each check in an explicit transaction or
      it resets before the next statement (psql defaults to autocommit,
      one statement per implicit transaction):
  ```sql
  BEGIN;
  SET ROLE phoenix_scoped;
  SELECT set_config('app.readable_project_ids', '<alpha project id>', true);
  SELECT name FROM projects;             -- only demo-team-alpha
  COMMIT;

  BEGIN;
  SET ROLE phoenix_scoped;
  SELECT set_config('app.bypass_rls', 'true', true);
  SELECT name FROM projects;             -- all three projects
  COMMIT;
  ```
- [ ] **(optional, not run manually)** **MCP SQL isolation** — authenticate
      an MCP client as `bob-user` and run `SELECT name FROM projects;`
      through the MCP SQL tool (`src/phoenix/server/mcp/sql/execute.py`).
      Expect only `demo-team-alpha` back — it has no project-scoping code
      of its own, it just rides the same RLS-guarded DB session as
      everything else. Skipped in the 2026-08-27 manual pass since it
      needs a real MCP client completing OAuth2 dynamic client
      registration + the authorization-code flow scoped to `/mcp`
      (RFC 8707) — already covered end-to-end by the automated
      `tests/integration/auth/test_mcp_sql_project_isolation.py`, which
      passes for member/admin/member-granted-both.
- [x] **(optional) Config-driven grant sync is additive, and the mapping
      file is only read once per process**: edit
      `local-dev/rls-demo/group-mapping.yaml` to add `demo-team-beta` to
      the `demo-project-alpha` entry's `projects` list. Signing
      `bob-user` out and back in **has no effect** —
      `_load_group_mapping` in `idp_sync.py` caches the parsed file at
      module level for the process lifetime ("this is fork-only,
      low-churn config, not something that needs live-reload"). Restart
      `make dev-backend`, *then* sign bob back in: `demo-team-beta` now
      appears in his project list. Revert the file back afterwards (it
      ships with `demo-project-alpha` → `demo-team-alpha` only) and
      restart again.

## Notes

- `sync_config_driven_project_grants` (`idp_sync.py`) is **additive-only**:
  narrowing or removing a `group-mapping.yaml` entry does not revoke a
  grant a user already picked up from it. Removing access requires a
  direct `DELETE FROM project_grants ...`. The mapping file itself is
  also **cached per-process** (`_load_group_mapping`), so editing it
  needs a `make dev-backend` restart before any login will see the
  change — see the optional config-sync test above.
- There is currently **no admin UI or GraphQL mutation** to hand-grant a
  project to a user — the only paths are the OIDC-group + mapping-file
  route demoed here, or a direct `INSERT INTO project_grants (project_id,
  user_id, permission, source) VALUES (..., 'manual')` for local/basic-auth
  users who aren't going through an IdP at all.
- Ingest (`/v1/traces`) still requires authentication like every other
  `/v1` route, but performs **no per-project write check** — any valid
  credential can write to any project. This is an accepted precedent
  from earlier in this fork's design (see the plan doc), not a gap
  specific to this demo.
- Keycloak's `groups` claim comes back alphabetically sorted, and
  Phoenix's plain-path role mapping only reads the first element — see
  the note in step 1 above if you add more groups to a user and their
  role unexpectedly resets to VIEWER.
