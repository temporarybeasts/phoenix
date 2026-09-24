---
name: rbac-fork-rebase-conflict
description: >
  Resolve a failed nightly rebase of the `rbac-fork` branch onto Arize-ai/phoenix `main`.
  Use this skill when the `RBAC fork nightly rebase` workflow
  (.github/workflows/rbac-fork-nightly-rebase.yml) has opened a "Nightly rebase conflict on
  rbac-fork" issue (or an older dated "Nightly rebase conflict: <date>" one), when the user mentions the `rbac-fork-conflict` branch, or asks to "fix the nightly
  rebase", "resolve the rebase conflict", or "rebase rbac-fork onto upstream". Rebases in an
  isolated worktree, resolves conflicts, runs the workflow's smoke check, force-pushes
  `rbac-fork` with a pinned lease after confirmation, and closes the issue.
license: Apache-2.0
metadata:
  author: amyers367@gmail.com
  version: "1.0.0"
  internal: true
---

# Resolve an rbac-fork nightly rebase conflict

The nightly workflow rebases `rbac-fork` onto `upstream/main`. On conflict it runs
`git rebase --abort`, force-pushes the **unrebased** tip to `rbac-fork-conflict`, and opens (or
comments on) an issue titled `Nightly rebase conflict on rbac-fork` on `temporarybeasts/phoenix`.
Before 2026-09-24 the title was dated (`Nightly rebase conflict: YYYY-MM-DD`), so a new issue was
opened each night. Older open issues may still carry that form.

## Never rebase the conflict branch

Older issues say to check out `rbac-fork-conflict`, rebase it, and
`git push origin HEAD:rbac-fork --force-with-lease`. That is unsafe:

- `rbac-fork-conflict` is a snapshot of `rbac-fork` from the night of the failure. If anyone
  has pushed to `rbac-fork` since, rebasing the snapshot and force-pushing it **deletes those
  commits**.
- A bare `--force-with-lease` does not protect against this: right after `git fetch`,
  the remote-tracking ref is fresh, so the lease always passes.

Always rebase the **current** `origin/rbac-fork` tip and push with a lease pinned to the exact
SHA you started from. Treat `rbac-fork-conflict` only as the signal that a conflict happened.

## Constants

```bash
FORK_REPO=temporarybeasts/phoenix
PATCH_BRANCH=rbac-fork
CONFLICT_BRANCH=rbac-fork-conflict
UPSTREAM_URL=https://github.com/Arize-ai/phoenix.git
```

## Step 1: Preflight

```bash
command -v gh && gh auth status
git remote get-url upstream || git remote add upstream "$UPSTREAM_URL"
```

If `gh` is missing, tell the user to run `brew install gh` then `! gh auth login`. Stop there.
Without `gh` you can still do Steps 3–7, but you can't do Step 8, so ask the user first.

Pass `-R temporarybeasts/phoenix` on every `gh` call. The local checkout has two remotes, and
without the flag `gh` may pick `upstream`.

## Step 2: Find the issue

```bash
gh issue list -R temporarybeasts/phoenix --state open \
  --search 'in:title "Nightly rebase conflict"' --json number,title,url,createdAt
```

Several open issues can exist, including old dated ones. Resolving the rebase fixes all of them,
so collect every number for Step 8. If there are none, go ahead anyway: the user may just want
a manual rebase.

## Step 3: Fetch and record the base

```bash
git fetch origin "$PATCH_BRANCH" "$CONFLICT_BRANCH"
git fetch upstream main
BASE=$(git rev-parse "origin/$PATCH_BRANCH")
git rev-parse "origin/$CONFLICT_BRANCH"   # compare to $BASE
git cherry upstream/main "origin/$PATCH_BRANCH" | grep -c '^+'   # fork commit count, keep for Step 7
```

If `origin/rbac-fork-conflict` ≠ `$BASE`, tell the user that `rbac-fork` has moved since the
failed run and that you're rebasing the newer tip. Also check that it's an ancestor:
`git merge-base --is-ancestor origin/$CONFLICT_BRANCH $BASE`. If it isn't, someone rewrote
`rbac-fork` in the meantime. Stop and ask.

## Step 4: Isolated worktree

The user's main checkout may have uncommitted work, so never rebase there.

```bash
WT=.claude/worktrees/rbac-rebase-$(date +%Y%m%d)
git worktree add --detach "$WT" "$BASE"
```

Run every later command inside `$WT`, with `unset VIRTUAL_ENV` so uv uses the worktree's `.venv`.

### Shell and uv gotchas

- The user's shell is zsh, which doesn't word-split unquoted variables. Build file lists as
  arrays, e.g. `F=(${(f)"$(git diff --name-only --diff-filter=U)"})`. Don't store a command in a
  variable either; define a function instead.
- Upstream bumps `[tool.uv].required-version` in `pyproject.toml` from time to time. If the local
  uv is older, run the pinned version without changing the user's install:
  `uv(){ uvx --from uv==<required> uv "$@"; }`, and pass
  `UV="uvx --from uv==<required> uv"` to `make`. Also bump the `setup-uv` `version:` pin in
  `.github/workflows/rbac-fork-nightly-rebase.yml` to match, as its own commit, or the next
  nightly smoke check fails.

## Step 5: Rebase and resolve

```bash
git rebase upstream/main
```

Each time the rebase stops:

1. List the conflicted files: `git diff --name-only --diff-filter=U`
2. Get the context for both sides:
   - The fork commit being applied: `git log -1 --stat REBASE_HEAD`
   - Why upstream changed the file: `git log --oneline "$BASE"..upstream/main -- <file>`
     (you can narrow this with `git merge-base`)
   - The upstream diff: `git diff $(git merge-base "$BASE" upstream/main) upstream/main -- <file>`
3. Resolve by keeping the fork's RBAC behaviour on top of upstream's new code. Upstream's
   refactors win on structure; the fork's access-control checks, group/permission logic and RLS
   changes must still be there afterwards.
4. Handle these files specially instead of hand-merging:
   - `uv.lock`: take the version rebased so far with `git checkout HEAD -- uv.lock`, then run
     `uv lock`. If the commit only touched `uv.lock` and is now empty, `git rebase --skip` it.
     (Mid-rebase `--ours` is the rebased branch and `--theirs` is the fork commit, the reverse
     of a merge.)
   - `js/app/schema.graphql`: resolve the Python source, then run `make schema-graphql`.
   - `schemas/openapi.json`: resolve the Python source, then run `make schema-openapi`.
   - Generated Relay artifacts (`__generated__/`): take the fork side mid-rebase
     (`git checkout --theirs`), then after the rebase finishes run `make schema-graphql` and
     `make relay-build` (needs `pnpm install --frozen-lockfile` in `js/` first) and commit the
     regenerated files as `chore: regenerate Relay artifacts after rebase`.
   - Alembic migrations (`src/phoenix/db/migrations/versions/`): if both sides added a migration,
     re-point the fork migration's `down_revision` at upstream's newest head so the chain stays
     linear. Verify there is exactly one head: `cd src/phoenix/db && uv run alembic heads`
5. Make sure no markers remain: `git diff --check` and `grep -rn '^<<<<<<< \|^>>>>>>> ' <files>`
6. `git add <files>`, then `GIT_EDITOR=true git rebase --continue`

If a conflict is semantic (both sides changed behaviour and it's unclear which should win),
**stop and ask the user**. Show both hunks and the commits they came from. Don't guess on
access-control logic.

If a fork commit becomes empty because upstream already contains it, `git rebase --skip` is
fine. Record which commit you skipped for the summary.

## Step 6: Verify

A clean textual merge is not proof of a working fork. On 2026-09-24, after every conflict was
resolved, the rebase still had these breakages:
- a fork-only name (`PhoenixUser`) whose import upstream deleted in a refactor, which ruff
  reports as F821
- fork tests importing a test helper upstream had renamed (`patch_batched_caller` became
  `patch_dml_event_handler`)
- a fork test that seeded a row upstream's fixtures now seed too (UNIQUE violation)

So always lint and test the files that differ from upstream, not just the conflicted ones.

This matches the workflow's post-rebase smoke check, which normally only runs on clean rebases:

```bash
uv run python -c "import phoenix.server.app; import phoenix.db.models; print('imports OK')"

export PHOENIX_WORKING_DIR="$(mktemp -d)"
uv run phoenix serve > "$PHOENIX_WORKING_DIR/server.log" 2>&1 &
server_pid=$!
# poll up to ~60s for "Phoenix is up and running" in server.log; fail if the process exits
kill "$server_pid"; wait "$server_pid" 2>/dev/null
```

Wait for the server with the Monitor tool (until-loop on the grep). Don't use a foreground sleep.

Then lint and test the fork's surface:

```bash
F=(${(f)"$(git diff --name-only --diff-filter=AM upstream/main HEAD -- '*.py')"})
uv tool run ruff check $F && uv tool run ruff format --check $F
T=(${(f)"$(git diff --name-only --diff-filter=AM upstream/main HEAD -- 'tests/unit/**/test_*.py')"})
uv run pytest -q -n 6 $T                     # SQLite
uv run pytest -q -n 6 --db postgresql $T     # Postgres: the RLS tests only run here
```

For every lint error or test failure, compare against the pre-rebase tip (`git show $BASE:<file>`)
to confirm the rebase caused it before you fix it. Upstream's changelog usually explains the
right fix: `git log $BASE..upstream/main -S<name>`. Put fixes in separate commits on top.

If you resolved frontend files, run `make typecheck-frontend`. Errors under `js/app/evals/` come
from upstream and need the workspace packages built first, so ignore them.

If verification fails, fix it with an extra commit on top (or amend the relevant rebased
commit), then re-verify. Don't push a failing state.

## Step 7: Confirm, then push

Show the user:
- the old tip `$BASE` and the new tip `git rev-parse HEAD`
- the number of fork commits before and after (`git cherry upstream/main HEAD | grep -c '^+'`).
  The two numbers should match unless you skipped commits
- the files you resolved and how, plus any commits you skipped
- the verification results

**Ask for explicit confirmation**, then push:

```bash
git push origin HEAD:rbac-fork --force-with-lease=rbac-fork:"$BASE"
```

If the lease is rejected, `rbac-fork` moved while you were working. Fetch again, then rebase
the new commits on top: `git rebase --onto HEAD $BASE origin/rbac-fork`. Update `$BASE`,
re-verify, and ask again before pushing.

## Step 8: Close the issue(s)

```bash
gh issue comment <n> -R temporarybeasts/phoenix --body "<summary: new tip SHA, resolved files, skipped commits, verification>"
gh issue close <n> -R temporarybeasts/phoenix
```

(The workflow uses `gh api` because the bot's fine-grained PAT can't reach GraphQL. The user's
own `gh` login doesn't have that problem.)

## Step 9: Clean up

```bash
git worktree remove "$WT"
```

Ask the user whether to delete `rbac-fork-conflict` with `git push origin --delete rbac-fork-conflict`.
It's optional: the next failed run recreates it.

Remind the user that their local `rbac-fork` is now behind the rewritten remote. They can
update it with `git fetch origin && git reset --keep origin/rbac-fork`, after checking for
unpushed local commits with `git log origin/rbac-fork..rbac-fork`.
