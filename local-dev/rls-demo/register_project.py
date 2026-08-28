# /// script
# dependencies = [
#   "psycopg[binary]",
# ]
# ///
"""Registers an already-ingested Phoenix project into a project group,
generalizing the group-assignment half of
local-dev/rls-demo/seed_projects.py (seed_project_groups()) into a
standalone CLI for any project name -- not just the two hardcoded demo
projects.

Stands in for the onboarding process external to Phoenix that would
otherwise populate project_groups/external_role_project_group_mappings
in production (see local-dev/rls-demo/README.md).

Usage (after the project has received at least one OTLP trace -- e.g.
from local-dev/rls-demo/chat-app -- so it already exists in the
`projects` table, auto-created into the default project group):

    export PHOENIX_SQL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
    uv run local-dev/rls-demo/register_project.py demo-team-gamma demo-group-gamma
    uv run local-dev/rls-demo/register_project.py demo-team-gamma demo-group-gamma \\
        --grant demo-project-gamma:VIEWER --grant demo-project-gamma-admin:ADMIN
"""

import argparse
import os
import sys
import time

import psycopg


def parse_grant(value: str) -> tuple[str, str]:
    try:
        external_role, role = value.split(":", 1)
    except ValueError:
        raise argparse.ArgumentTypeError(f"grant must be '<external_role>:<ROLE>', got {value!r}")
    role = role.upper()
    if role not in ("VIEWER", "MEMBER", "ADMIN"):
        raise argparse.ArgumentTypeError(f"role must be VIEWER/MEMBER/ADMIN, got {role!r}")
    return external_role, role


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_name", help="Phoenix project name, already ingested at least once")
    parser.add_argument(
        "group_name", help="project group name to create/reuse and assign the project to"
    )
    parser.add_argument(
        "--grant",
        action="append",
        default=[],
        type=parse_grant,
        metavar="EXTERNAL_ROLE:ROLE",
        help=(
            "external role -> group role mapping to upsert, e.g. "
            "demo-project-gamma:VIEWER; repeatable"
        ),
    )
    args = parser.parse_args()

    database_url = os.environ.get("PHOENIX_SQL_DATABASE_URL")
    if not database_url:
        sys.exit(
            "PHOENIX_SQL_DATABASE_URL is not set -- see local-dev/rls-demo/phoenix.env.example."
        )

    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            project_id = None
            # Ingest is queued -- see seed_projects.py's identical polling
            # loop for why this can't just do a single SELECT.
            for _ in range(50):
                cur.execute("SELECT id FROM projects WHERE name = %s", (args.project_name,))
                row = cur.fetchone()
                if row is not None:
                    project_id = row[0]
                    break
                time.sleep(0.2)
            if project_id is None:
                sys.exit(
                    f"Project '{args.project_name}' does not exist yet -- send it "
                    "at least one trace first (e.g. via local-dev/rls-demo/chat-app)."
                )

            cur.execute(
                "INSERT INTO project_groups (name) VALUES (%s) "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id",
                (args.group_name,),
            )
            (group_id,) = cur.fetchone()
            cur.execute(
                "UPDATE projects SET project_group_id = %s WHERE id = %s",
                (group_id, project_id),
            )
            print(f"Assigned project '{args.project_name}' to group '{args.group_name}'.")

            for external_role, role in args.grant:
                cur.execute(
                    "INSERT INTO external_role_project_group_mappings "
                    "(external_role, project_group_id, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT (external_role) DO UPDATE SET "
                    "project_group_id = EXCLUDED.project_group_id, role = EXCLUDED.role",
                    (external_role, group_id, role),
                )
                print(
                    f"Mapped external role '{external_role}' -> group '{args.group_name}' ({role})."
                )


if __name__ == "__main__":
    main()
