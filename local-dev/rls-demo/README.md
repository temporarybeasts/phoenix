# Row-level isolation / project-grant manual test & demo

Demos and manually verifies per-project row-level isolation (RLS): project
access is computed **live** from a user's raw OIDC `groups` claim
(persisted on `users.idp_groups` at login, see
`src/phoenix/server/access/idp_sync.py`) against the declarative
group->project mapping config (`src/phoenix/server/access/resolution.py`),
enforced in Postgres via a `phoenix_scoped` role + `USING`/`WITH CHECK`
policies on 10 project-scoped tables (`_set_db_isolation_guards` in
`src/phoenix/server/app.py`), plus pre-write project-access checks in the
GraphQL/REST annotation mutations. Nothing is pre-materialized into its own
grant table -- access is recomputed from the group list + config + current
`projects` table on every check (cached ~30s).

Reuses `local-dev/keycloak/`'s Keycloak instance and realm (see that
directory's README for the base SSO test matrix) — this just adds
Postgres and a second demo user so isolation is actually observable.

Unlike `local-dev/keycloak/`, this directory is tracked — it's kept
around as a demo rig, not just scratch scaffolding. `phoenix.env` itself
stays untracked (see `.gitignore`); only `phoenix.env.example` is
checked in, same convention as `local-dev/keycloak/`.

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

Project access is computed live from `local-dev/rls-demo/group-mapping.yaml`
against the requesting user's current `idp_groups` list and the current
`projects` table -- nothing is written to a separate grant table at login.

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
```
```shell
cp local-dev/rls-demo/phoenix.env.example local-dev/rls-demo/phoenix.env
```
```shell
set -a && source local-dev/rls-demo/phoenix.env && set +a
env | grep PHOENIX_SQL_DATABASE_URL
```
```shell
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
  ```
``` shell
token=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJBY2Nlc3NUb2tlbjo5In0.at8VyhPlKYVIco0c1SXNFWUoMWnvxQcSrDAso7sNjg8
# copy the phoenix-access-token cookie value from the response, then:
curl -s -X POST http://localhost:6006/v1/user/api_keys \
  -H "Authorization: Bearer ${token}" \
  -H "Content-Type: application/json" \
  -d '{"data":{"name":"rls-demo-seed-key"}}'
  ```
``` shell
key=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJBcGlLZXk6MiJ9.jnufpAiwMYFwS2LJEnUikOScEeNeMCq8jzIo-hYd6-o
# copy the "key" field from the response
export PHOENIX_API_KEY=${key}
uv run local-dev/rls-demo/seed_projects.py
```

Creates `demo-team-alpha` and `demo-team-beta`, each with a couple of
sample LLM spans. "Ingest bypasses RLS" doesn't mean ingest is
unauthenticated -- it means any authenticated caller can write to *any*
project via ingest regardless of their own project grants, since there's
no per-project write check on this path (unlike the annotation
mutations below).

## 4. Test matrix

- [ ] **Admin sees everything**: log in as `alice-admin`. Project list
      shows both `demo-team-alpha` and `demo-team-beta` (ADMIN sets
      `app.bypass_rls`, see `_set_db_isolation_guards`).
- [ ] **Member sees only their granted project**: log in as `bob-user`.
      Project list shows only `demo-team-alpha`.
- [ ] **A different member sees only theirs**: log in as `dave-user`.
      Project list shows only `demo-team-beta`.
- [ ] **In-project annotation succeeds**: as `bob-user`, open a span in
      `demo-team-alpha` and add a span annotation (UI, or GraphQL
      `createSpanAnnotations`, or `POST /v1/span_annotations?sync=true`).
      Succeeds.
- [ ] **Cross-project annotation is rejected**: as `bob-user`, get a span
      id from `demo-team-beta` (e.g. from `alice-admin`'s session, or via
      Postgres) and attempt to annotate it. Rejected — GraphQL raises
      `NotFound` (RLS makes the row invisible to `bob-user`'s own
      existence-check `SELECT`, before any `WITH CHECK` is reached), REST
      `sync=true` returns 404. This is the behavior verified in commit
      `9f7f66374`.
- [ ] **(optional) Direct-DB verification** — via `psql
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
      everything else. Already covered end-to-end by the automated
      `tests/integration/auth/test_mcp_sql_project_isolation.py`, which
      passes for member/admin/member-granted-both.
- [ ] **(optional) Config changes take effect live, without a new login**:
      edit `local-dev/rls-demo/group-mapping.yaml` to remove
      `demo-team-alpha` from the `demo-project-alpha` entry's `projects`
      list (or narrow the glob). Without signing `bob-user` out, refresh
      the project list after ~30s (the resolution cache's TTL) —
      `demo-team-alpha` disappears. Revert the file to restore access; it
      reappears after the same TTL, still with no re-login required. This
      demonstrates the fix for the old additive-only sync behavior. Note
      the mapping *file* itself is still cached per-process
      (`_load_group_mapping` in `resolution.py`) — an edit needs a `make
      dev-backend` restart before any request will see it, only the
      group-membership and project-existence sides of resolution are
      live.
- [ ] **(optional) A newly created project matching an already-held
      group's glob appears without re-login**: as `bob-user` (mapped to
      `demo-team-alpha` via an exact-match glob today), have an admin
      create a new project whose name would match a broader glob you've
      configured for `demo-project-alpha` (e.g. temporarily set
      `projects: ["demo-team-*"]`) — the new project appears in `bob-user`'s
      list after the cache TTL, with no re-login needed, since project
      matching is recomputed against the *current* `projects` table on
      every check.

## Notes

- Access is recomputed on every check from `users.idp_groups` (set at
  login) against `group-mapping.yaml` and the current `projects` table —
  there's no `project_grants` table to inspect or clean up. Revoking
  access is just editing the YAML; no direct DB write needed.
- There is currently **no admin UI or GraphQL mutation** to hand-grant a
  project to a user — the only path is the OIDC-group + mapping-file
  route demoed here. Manual (non-IdP) per-user grants were considered and
  intentionally dropped: all project access flows through IdP group
  claims.
- Ingest (`/v1/traces`) still requires authentication like every other
  `/v1` route, but performs **no per-project write check** — any valid
  credential can write to any project. This is an accepted precedent
  from earlier in this fork's design (see the plan doc), not a gap
  specific to this demo.
- Keycloak's `groups` claim comes back alphabetically sorted, and
  Phoenix's plain-path role mapping only reads the first element — see
  the note in step 1 above if you add more groups to a user and their
  role unexpectedly resets to VIEWER.
