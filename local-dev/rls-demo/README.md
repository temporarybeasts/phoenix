# Row-level isolation / project-group manual test & demo

Demos and manually verifies per-project-group row-level isolation (RLS):
projects are organized into project groups (`project_groups` table, every
project belongs to exactly one), and access to a group is granted via a raw
OIDC `groups` claim value (an "external role", persisted on
`users.idp_groups` at login, see
`src/phoenix/server/access/idp_sync.py`) mapped to a `(project group, role)`
pair in the `external_role_project_group_mappings` table -- a config table
persisted in the database (not naming convention), maintained by an
onboarding process external to Phoenix (this demo's stand-in: this
directory's `seed_projects.py`). Enforced in Postgres via a
`phoenix_scoped` role + `USING`/`WITH CHECK` policies on 10 project-scoped
tables (`_set_db_isolation_guards` in `src/phoenix/server/app.py`), plus
pre-write project-access checks in the GraphQL/REST annotation mutations.
Nothing is pre-materialized into a per-project grant table -- access is
recomputed from the held external roles + mapping table + current
`projects` table on every check (cached ~30s).

A user who belongs to more than one project group must pick which one
they're "viewing" (at login, or by switching in the UI) -- see
`phoenix.server.access.resolution` and `phoenix.server.access.active_group`.
`bob-user`, `dave-user`, and `grace-groupadmin` each hold exactly one
group, so this is their implicit active group with no picker involved;
`faye-member` and `henry-groupadmin` each hold two (at VIEWER and ADMIN
respectively) and exercise the login picker and in-UI switcher -- see the
test matrix below.

Reuses `local-dev/keycloak/`'s Keycloak instance and realm (see that
directory's README for the base SSO test matrix) — this just adds
Postgres and enough demo users to make isolation, and its interaction
with multi-group membership, actually observable.

Unlike `local-dev/keycloak/`, this directory is tracked — it's kept
around as a demo rig, not just scratch scaffolding. `phoenix.env` itself
stays untracked (see `.gitignore`); only `phoenix.env.example` is
checked in, same convention as `local-dev/keycloak/`.

## 1. Start Keycloak and Postgres

```sh
docker compose -f local-dev/keycloak/docker-compose.yml up -d
docker compose -f local-dev/rls-demo/docker-compose.yml up -d
```

The Keycloak realm now seeds eight users (all password `password`):

| user                | groups                                                              | expected Phoenix role | project access  |
|---------------------|----------------------------------------------------------------------|------------------------|------------------|
| `alice-admin`       | `phoenix-admins`                                                      | ADMIN                  | all (global account-role ADMIN bypasses RLS entirely -- `app.bypass_rls`, see `_set_db_isolation_guards`). This is a distinct mechanism from project-group membership: she holds *zero* project groups, and would see nothing without the bypass. Contrast with `grace-groupadmin` below. |
| `bob-user`          | `phoenix-users`, `demo-project-alpha`                                 | MEMBER                 | `demo-team-alpha` only (implicit active group), VIEWER-level project-group role (can annotate, cannot create a project -- see `seed_projects.py`) |
| `dave-user`         | `phoenix-users`, `demo-project-beta`                                  | MEMBER                 | `demo-team-beta` only (implicit active group), same VIEWER-level project-group role as `bob-user` |
| `carol-outsider`    | *(none)*                                                              | n/a                     | **denied at login** -- this is `local-dev/keycloak`'s own pre-existing account-level `ALLOWED_GROUPS` gate (see that directory's README), which runs *before* project-group resolution is ever reached. A truly zero-Keycloak-group user can never pass this gate while `GROUPS_ATTRIBUTE_PATH`/`ALLOWED_GROUPS` are both configured (`OAuth2Client.__init__` requires both together, and the membership check is an `any()` over the user's groups, which is vacuously false for an empty list) -- it is not possible to reuse `carol-outsider` to demonstrate the zero-*project-group* scenario below. |
| `erin-nogroup`      | `phoenix-users`                                                       | MEMBER                  | none -- logs in fine (passes the account-level gate via `phoenix-users`), sees no projects, cannot create one (holds no external role mapped to any project group). This is the actual "zero-group user" scenario from the design decision: login always succeeds, only project-group *visibility* is empty. |
| `faye-member`       | `phoenix-users`, `demo-project-alpha`, `demo-project-beta`            | MEMBER                  | **two groups**, VIEWER on each -- login shows the group-selection interstitial (`/login/choose-group`); sees only the active group's project, switches via the in-UI group switcher, cannot create in either (VIEWER). |
| `grace-groupadmin`  | `phoenix-users`, `demo-project-alpha-admin`                            | MEMBER                  | `demo-team-alpha` only, ADMIN-tier *project-group* role -- one group, implicit active group, no picker. Unlike `alice-admin`, this is an ordinary account (global role MEMBER, no RLS bypass) whose elevated tier is scoped entirely to her one group: can create projects there, sees nothing outside it. This is the "existing admin, one group" case as a project-group-scoped role rather than the global bypass. |
| `henry-groupadmin`  | `phoenix-users`, `demo-project-alpha-admin`, `demo-project-beta-admin` | MEMBER                  | **two groups**, ADMIN on each -- picker at login, sees only the active group's project, can create in whichever group is active, and a project created while viewing one group is invisible after switching to the other. |

`demo-project-alpha-admin`/`demo-project-beta-admin` are separate external
roles from `demo-project-alpha`/`demo-project-beta` -- an external role
maps to exactly one `(group, role)` pair, so granting ADMIN on the same
group `bob-user`/`dave-user` hold at VIEWER requires its own role name,
not a different tier of the same one.

`erin-nogroup`, `faye-member`, `grace-groupadmin`, and `henry-groupadmin`
aren't in `local-dev/keycloak/realm-export.json`'s *import* (Keycloak only
imports a realm once; a container recreate is needed to pick up
realm-export.json edits) -- if you're standing this rig up fresh and the
import runs, they'll be there already; otherwise create them once via the
admin console/API, matching the groups columns above (the two
`*-admin` groups need creating too, alongside the existing four).

Project access is computed live from the `external_role_project_group_mappings`
table against the requesting user's current `idp_groups` list and the
current `projects` table -- nothing is written to a per-project grant
table at login. `seed_projects.py` (step 3 below) is what actually inserts
these mapping rows; log in and check the project list *after* running it.

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

Creates `demo-team-alpha` and `demo-team-beta` (each with a couple of
sample LLM spans, landing in the default project group via OTLP
auto-creation), then reassigns each to its own dedicated project group and
inserts every mapping row in `seed_projects.py`'s `ROLE_GRANTS`: VIEWER on
`demo-project-alpha`/`demo-project-beta` (`bob-user`/`dave-user`/
`faye-member`), and ADMIN on `demo-project-alpha-admin`/
`demo-project-beta-admin` (`grace-groupadmin`/`henry-groupadmin`).
"Ingest bypasses RLS" doesn't mean ingest is unauthenticated -- it means
any authenticated caller can write to *any* project via ingest regardless
of their own project-group access, since there's no per-project write
check on this path (unlike the annotation mutations below).

## 4. Test matrix

- [x] **Admin sees everything**: log in as `alice-admin`. Project list
      shows both `demo-team-alpha` and `demo-team-beta` (ADMIN sets
      `app.bypass_rls`, see `_set_db_isolation_guards`). Verified live
      (also shows the `default` project, which has no traces).
- [x] **Member sees only their granted project**: log in as `bob-user`.
      Project list shows only `demo-team-alpha`. Verified live.
- [x] **A different member sees only theirs**: log in as `dave-user`.
      Project list shows only `demo-team-beta`. Verified live.
- [x] **In-project annotation succeeds**: as `bob-user`, open a span in
      `demo-team-alpha` and add a span annotation (UI, or GraphQL
      `createSpanAnnotations`, or `POST /v1/span_annotations?sync=true`).
      Succeeds. Verified live via `createSpanAnnotations` — note this
      only requires the caller's VIEWER-level project-group role (see the
      user table above); it is not gated the same way project *creation*
      is.
- [x] **Cross-project annotation is rejected**: as `bob-user`, get a span
      id from `demo-team-beta` (e.g. from `alice-admin`'s session, or via
      Postgres) and attempt to annotate it. Rejected — GraphQL raises
      `NotFound` (RLS makes the row invisible to `bob-user`'s own
      existence-check `SELECT`, before any `WITH CHECK` is reached), REST
      `sync=true` returns 404. This is the behavior verified in commit
      `9f7f66374`. Verified live: `Could not find spans with IDs: [...]`.
- [x] **Zero-project-group user logs in fine, sees nothing, can't
      create**: log in as `erin-nogroup` (see the user table above for
      why `carol-outsider` can't actually be used for this despite the
      name). Login succeeds, project list is empty, and the "New
      Project" button itself is absent from the toolbar (the button is
      gated on `activeProjectGroup.role` being `MEMBER`/`ADMIN` via
      `useCanCreateProject`, `js/app/src/contexts/ViewerContext.tsx` —
      this was previously missing and has been fixed). Attempting
      `createProject` directly over GraphQL is separately rejected
      server-side regardless of the UI: `"You must be viewing a project
      group with write access to create a project -- select or switch to
      one first."` Both verified live.
- [x] **A project created while viewing a group lands in that group**:
      promote `bob-user`'s role on `demo-group-alpha` to `MEMBER`
      (`UPDATE external_role_project_group_mappings SET role='MEMBER'
      WHERE external_role='demo-project-alpha';`, wait out the cache
      TTL), confirm the "New Project" button now appears, then create
      one. The new row's `project_group_id` matches `demo-group-alpha`.
      Verified live (`createProject` → `SELECT project_group_id FROM
      projects` confirms). Revert the role back to `VIEWER` afterward to
      match the documented user table.
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
- [ ] **(optional) Mapping changes take effect live, without a new login**:
      delete `bob-user`'s mapping row (`DELETE FROM
      external_role_project_group_mappings WHERE external_role =
      'demo-project-alpha';` via `psql`). Without signing `bob-user` out,
      refresh the project list after ~30s (the resolution cache's TTL) —
      `demo-team-alpha` disappears. Re-run `seed_projects.py` (or
      re-insert the row) to restore access; it reappears after the same
      TTL, still with no re-login required. This demonstrates the fix for
      the old additive-only sync behavior, now via a direct DB edit
      instead of a YAML edit.
- [ ] **(optional) A newly created project in an already-held group
      appears without re-login**: as `bob-user` (mapped to
      `demo-group-alpha`), have an admin create a new project and assign
      it to `demo-group-alpha`'s id (`UPDATE projects SET
      project_group_id = <demo-group-alpha id> WHERE name =
      '<new-project-name>';`) — the new project appears in `bob-user`'s
      list after the cache TTL, with no re-login needed, since group
      membership is recomputed against the *current* `projects` table on
      every check.
- [x] **Multi-group member: picker at login, scoped visibility,
      switching**: log in as `faye-member` (VIEWER on both
      `demo-group-alpha` and `demo-group-beta`). Login lands on
      `/login/choose-group` showing both groups at VIEWER. Choosing
      `demo-group-alpha` shows only `demo-team-alpha` and no "New
      Project" button (VIEWER can't create). Switching to
      `demo-group-beta` via the in-UI group switcher updates the project
      list to show only `demo-team-beta`. Verified live.
- [x] **Single-group project-group admin, scoped like any other member**:
      log in as `grace-groupadmin` (ADMIN on `demo-group-alpha` only, one
      group). No picker (implicit active group, same as `bob-user`/
      `dave-user`). Sees only `demo-team-alpha` -- unlike `alice-admin`,
      whose *global* ADMIN role bypasses RLS regardless of group
      membership, `grace-groupadmin` is an ordinary account (global role
      MEMBER) whose elevated tier is entirely group-scoped: "New Project"
      is visible (ADMIN-tier, write-capable) but only ever creates into
      `demo-group-alpha`. Verified live.
- [x] **Multi-group project-group admin: picker, scoped visibility and
      creation, switching**: log in as `henry-groupadmin` (ADMIN on both
      `demo-group-alpha` and `demo-group-beta`). Picker shows both groups
      at ADMIN. While viewing `demo-group-alpha`, "New Project" is
      visible; creating one lands it in `demo-group-alpha` and it's
      immediately visible in that view. Switching to `demo-group-beta`
      shows only `demo-team-beta` -- neither `demo-team-alpha` nor the
      just-created project are visible, confirming creation and read
      access are both scoped to the *active* group, not the union of
      held groups. Verified live.

## 5. Fake chat app: register a new project and stream live traces

`local-dev/rls-demo/chat-app/` is a small synthetic "chat app" runnable
as a Docker container -- each instance is one fake app identity that
streams synthetic OTLP traces (`chat_app.py`) into a Phoenix project on
a loop, demonstrating both reusing an already-mapped demo project and
onboarding a brand-new one live, without re-running `seed_projects.py`.

1. Build the image once:
   ```sh
   docker build -t rls-demo-chat-app local-dev/rls-demo/chat-app
   ```
2. Mint a dedicated API key per chat-app identity, reusing the same
   admin-login + `/v1/user/api_keys` flow from step 3 above (just give
   each a distinct `name`, e.g. `"chat-app-alpha-bot"`) -- one key per
   instance, so each shows up separately in the admin UI's API keys
   list.
3. Run an instance targeting an *existing*, already-mapped project:
   ```sh
   docker run --rm --name chat-app-alpha \
     -e CHAT_APP_NAME=alpha-bot \
     -e PHOENIX_PROJECT_NAME=demo-team-alpha \
     -e PHOENIX_API_KEY=<key> \
     rls-demo-chat-app
   ```
   Traces stream straight into `demo-team-alpha` -- log in as
   `bob-user` and watch new spans keep appearing under Traces with no
   extra step.
4. Mint a second key and run a second instance under a brand-new
   name/project:
   ```sh
   docker run --rm --name chat-app-gamma \
     -e CHAT_APP_NAME=gamma-bot \
     -e PHOENIX_PROJECT_NAME=demo-team-gamma \
     -e PHOENIX_API_KEY=<key2> \
     rls-demo-chat-app
   ```
   Log in as `alice-admin` and confirm `demo-team-gamma` appears
   (global RLS bypass, default project group) while it's invisible to
   every other demo user -- nothing has mapped it to a group yet.
5. Register the new project into its own group, the same onboarding
   step `seed_projects.py` performs at setup time for the two
   pre-baked demo projects, now done live and ad hoc:
   ```sh
   uv run local-dev/rls-demo/register_project.py demo-team-gamma demo-group-gamma
   ```
6. Multiple instances just mean distinct `--name`/`CHAT_APP_NAME`/
   `PHOENIX_API_KEY` values -- e.g. run two identities both pointed at
   `PHOENIX_PROJECT_NAME=demo-team-alpha` and confirm both show up as
   separate API keys in the admin UI while interleaving spans into the
   same project.

`chat-app/docker-compose.yml` is a convenience for `docker compose
build`/running a single instance; multiple named instances are run
directly against the built image with `docker run`, as above, since
compose services aren't a natural fit for "spin up N of these with
different names."

## Notes

- Access is recomputed on every check from `users.idp_groups` (set at
  login) against `external_role_project_group_mappings` and the current
  `projects` table — there's no per-project grant table to inspect or
  clean up. Revoking access is a `DELETE`/`UPDATE` on that table (or, in
  production, whatever the external onboarding process does); no
  Phoenix-side mutation exists for it.
- There is currently **no admin UI or GraphQL mutation** to hand-edit the
  external-role -> project-group mapping — by design (the requirement:
  this table is maintained by an onboarding process external to
  Phoenix). The only path is direct DB writes, as `seed_projects.py`
  does here. Manual (non-IdP) per-user grants were considered and
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

## 6. Clean up

Stop Phoenix first (`Ctrl-C` the `make dev-backend` process), then tear
down both stacks and their state:

```sh
docker compose -f local-dev/rls-demo/docker-compose.yml down -v
docker compose -f local-dev/keycloak/docker-compose.yml down
```

If any chat-app containers from section 5 are still running, stop them
and remove the built image too:

```sh
docker rm -f $(docker ps -aq --filter "name=chat-app-") 2>/dev/null
docker rmi rls-demo-chat-app
```

- The `-v` on the `rls-demo` stack is required — it drops the named
  `rls_demo_database_data` volume, which is the only thing holding the
  Postgres data directory (and with it the `phoenix_scoped` role, the RLS
  policies, the Alembic migration history, and every seeded row —
  `project_groups`, `external_role_project_group_mappings`, the demo
  projects/spans). Without `-v` the next `up` reuses the same volume and
  skips re-running migrations against a fresh database.
- The `keycloak` stack has no named volume — it runs `start-dev` with
  only the read-only `realm-export.json` bind-mounted in, so a plain
  `down` (no `-v` needed) already discards all realm state. That includes
  the four users manually created via the admin console/API in step 1
  (`erin-nogroup`, `faye-member`, `grace-groupadmin`,
  `henry-groupadmin`) — they are **not** in `realm-export.json`, so they
  will not come back on the next `up` and must be recreated by hand
  again (or added to `realm-export.json` first, see that directory's
  README on realm-import-only-runs-once).
- Remove the untracked env file and any exported shell state from step
  2/3, since `phoenix.env` holds the seed API key and the local
  `PHOENIX_SQL_DATABASE_URL`, both now pointing at a database that no
  longer exists:
  ```sh
  rm local-dev/rls-demo/phoenix.env
  unset PHOENIX_API_KEY PHOENIX_SQL_DATABASE_URL
  ```
  (or simply start a fresh shell for the next run — `phoenix.env` was
  never sourced into anything persistent beyond the current shell's
  environment).

After this, both `docker volume ls` and `docker compose -f
local-dev/rls-demo/docker-compose.yml ps` /
`docker compose -f local-dev/keycloak/docker-compose.yml ps` should show
nothing left for either stack, and re-running step 1 starts from the
same blank-slate realm-export-only state as a first-time setup.
