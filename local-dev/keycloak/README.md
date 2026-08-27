# Stage 1: Local Keycloak + Phoenix OIDC SSO

Proves out stock Phoenix SSO (group-gated login, JIT provisioning, role
mapping/resync, revocation-on-next-login) against a real local Keycloak.
No Phoenix source changes involved -- everything here is config and a
seeded realm.

This directory is intentionally kept out of the tracked Phoenix source
tree (`src/`, `docs/`, root `docker-compose.yml`) so it never conflicts on
a future upstream rebase.

## 1. Start Keycloak

```sh
docker compose -f local-dev/keycloak/docker-compose.yml up
```

This imports `realm-export.json` on boot: a `phoenix-dev` realm, a
`phoenix` client (secret `phoenix-dev-secret`, redirect URI
`http://localhost:6006/oauth2/keycloak/tokens`), two groups
(`phoenix-admins`, `phoenix-users`), and three seeded users (all password
`password`):

| user             | group            | expected Phoenix role |
|------------------|------------------|------------------------|
| `alice-admin`    | `phoenix-admins` | ADMIN                  |
| `bob-user`        | `phoenix-users`  | MEMBER                 |
| `carol-outsider` | *(none)*         | denied at login        |

Keycloak admin console: <http://localhost:8080> (`admin` / `admin`).

## 2. Start Phoenix

Build the frontend once -- Phoenix has no built-in dev-server proxy for
this stage, so `phoenix serve` needs static assets to serve on port 6006
(without this you'll get a 404 on `/`):

```sh
make build-frontend
```

Then, in the **same shell session** you'll run `phoenix serve` from:

```sh
cp local-dev/keycloak/phoenix.env.example local-dev/keycloak/phoenix.env   # first time only
set -a && source local-dev/keycloak/phoenix.env && set +a
env | grep PHOENIX_ENABLE_AUTH   # must print PHOENIX_ENABLE_AUTH=true -- if empty, re-run the line above
make dev-backend
```

The `env | grep` check matters: `phoenix serve` inherits whatever's
exported in *that* shell, so if you open a new terminal tab, restart the
server, or re-run `make dev-backend` without re-sourcing `phoenix.env`
first, auth silently falls back to disabled and you'll land straight in
the default project with no login page.

Phoenix runs natively on the host here (not the root `docker-compose.yml`
stack) so both the browser and Phoenix reach Keycloak at the same
`http://localhost:8080` -- this sidesteps the container-network-vs-browser
hostname mismatch that a containerized Phoenix would hit against
Keycloak's discovery document.

## 3. Test matrix

Drive these through a real browser session (redirects + cookies aren't a
curl job). `tests/integration/auth/test_oidc.py` and its `conftest.py`
fixtures are a useful behavioral reference for what "correct" looks like,
but they run an in-process mock OIDC server, not real Keycloak -- don't
try to reuse them directly here.

- [ ] **Login + JIT provisioning**: sign in as `alice-admin`. Phoenix
      redirects to Keycloak, back to `/oauth2/keycloak/tokens`, and a new
      user is created with role **ADMIN**. Confirm in Settings -> Members,
      or directly in Postgres (`select email, role_id from users;`... via
      the `db` service if using the root compose, or the local sqlite file
      if not).
- [ ] **Group gating denies non-members**: sign in as `carol-outsider`.
      Login is denied (`PermissionError`) -- she never reaches Phoenix.
- [ ] **Role mapping**: sign in as `bob-user`, confirm role **MEMBER**.
- [ ] **Unmapped fallback**: temporarily remove `bob-user` from
      `phoenix-users` and add him to a new, unmapped group in the Keycloak
      admin console; sign in again and confirm he lands on **VIEWER**
      (`ROLE_ATTRIBUTE_STRICT=false`).
- [ ] **Role resync**: move `bob-user` from `phoenix-users` to
      `phoenix-admins` in Keycloak, sign out/in again in Phoenix, confirm
      his role updates to **ADMIN** without any Phoenix-side action
      (`ROLE_RESYNC=true`).
- [ ] **Revocation window**: remove `alice-admin` from `phoenix-admins`
      (or disable her user) in Keycloak. Confirm her *next* login attempt
      is denied, but a session she already had open keeps working until
      the access token expires (`PHOENIX_ACCESS_TOKEN_EXPIRY_MINUTES=10`
      above) -- this is the revocation-propagation window to keep in mind
      for production tuning.

## Notes

- `PHOENIX_DISABLE_BASIC_AUTH=false` is deliberate for this stage --
  it keeps `admin@localhost` / `admin` as a fallback login while wiring
  things up. Flip it to `true` once SSO is proven, to mirror the intended
  production posture.
- Claims must land on the **ID token** or **UserInfo** endpoint -- Phoenix
  never reads the access token. The seeded realm's `groups` protocol
  mapper has `id.token.claim` and `userinfo.token.claim` both `true` for
  exactly this reason; if you add your own groups/roles later, keep those
  toggles on.
