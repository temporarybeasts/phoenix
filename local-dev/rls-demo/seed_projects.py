# /// script
# dependencies = [
#   "opentelemetry-sdk",
#   "opentelemetry-exporter-otlp",
#   "psycopg[binary]",
# ]
# ///
"""Seeds two demo projects (demo-team-alpha, demo-team-beta) with a
couple of sample LLM spans each, then persists the external-role ->
project-group mapping directly into the database -- standing in for the
onboarding process external to Phoenix that would do this in production
(see local-dev/rls-demo/README.md's manual row-level-isolation test
matrix).

Usage (after `make dev-backend` is up):
    export PHOENIX_API_KEY=<a key from an admin/system user>
    uv run local-dev/rls-demo/seed_projects.py

With PHOENIX_ENABLE_AUTH=true, the whole /v1 router -- including REST
OTLP ingest -- requires a valid bearer credential (see is_authenticated
in src/phoenix/server/bearer_auth.py and its use as a router-level
dependency in src/phoenix/server/api/routers/v1/__init__.py). What
"ingest bypasses RLS" actually means here: any authenticated caller can
write to *any* project via ingest, regardless of their own project
grants -- there's no per-project write check on this path, unlike the
annotation mutations. It does not mean ingest is unauthenticated.

Mint a key by logging in as the seeded local admin and hitting the
user-api-keys endpoint:
    curl -s -i -X POST http://localhost:6006/auth/login \\
      -H "Content-Type: application/json" \\
      -d '{"email":"admin@localhost","password":"admin"}'
    # copy the phoenix-access-token cookie value, then:
    curl -s -X POST http://localhost:6006/v1/user/api_keys \\
      -H "Authorization: Bearer <access token>" \\
      -H "Content-Type: application/json" \\
      -d '{"data":{"name":"rls-demo-seed-key"}}'
    # copy the "key" field from the response into PHOENIX_API_KEY

Requires PHOENIX_SQL_DATABASE_URL (the same Postgres connection Phoenix
itself uses) to be set for the direct project-group seeding step -- see
phoenix.env.example.
"""

import os
import sys
import time

import psycopg
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

ENDPOINT = "http://localhost:6006/v1/traces"

API_KEY = os.environ.get("PHOENIX_API_KEY")
if not API_KEY:
    sys.exit(
        "PHOENIX_API_KEY is not set -- see this script's module docstring "
        "for how to mint one from the seeded local admin account."
    )
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

DATABASE_URL = os.environ.get("PHOENIX_SQL_DATABASE_URL")
if not DATABASE_URL:
    sys.exit("PHOENIX_SQL_DATABASE_URL is not set -- see phoenix.env.example.")

DEMO_PROJECTS = {
    "demo-team-alpha": [
        ("What's our Q3 revenue?", "Q3 revenue was $4.2M, up 12% QoQ."),
        ("Summarize the alpha team's roadmap.", "Ship the isolation demo, then RLS for datasets."),
    ],
    "demo-team-beta": [
        ("Draft a release note for v2.1.", "v2.1 adds per-project row-level isolation."),
        ("What's blocking the beta launch?", "Waiting on the annotation-mutation isolation fix."),
    ],
}

# project name -> project group name it's reassigned to.
PROJECT_GROUPS = {
    "demo-team-alpha": "demo-group-alpha",
    "demo-team-beta": "demo-group-beta",
}

# external role -> (project group name, role granted). Matches the
# Keycloak realm's demo users (see local-dev/keycloak/realm-export.json):
#   bob-user / dave-user hold demo-project-alpha / demo-project-beta
#     (VIEWER, one group each)
#   faye-member holds both VIEWER roles (two groups -- exercises the
#     group switcher as a plain member)
#   grace-groupadmin holds only demo-project-alpha-admin (ADMIN, one
#     group -- a project-group admin distinct from alice-admin's global,
#     RLS-bypassing account role: scoped to just her one group)
#   henry-groupadmin holds both *-admin roles (ADMIN, two groups --
#     exercises the switcher at admin tier)
ROLE_GRANTS = {
    "demo-project-alpha": ("demo-group-alpha", "VIEWER"),
    "demo-project-beta": ("demo-group-beta", "VIEWER"),
    "demo-project-alpha-admin": ("demo-group-alpha", "ADMIN"),
    "demo-project-beta-admin": ("demo-group-beta", "ADMIN"),
}


def seed_project(project_name: str, exchanges: list[tuple[str, str]]) -> None:
    tracer_provider = TracerProvider(
        resource=Resource({"openinference.project.name": project_name})
    )
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(ENDPOINT, headers=HEADERS))
    )
    tracer = tracer_provider.get_tracer(__name__)

    for user_input, assistant_output in exchanges:
        with tracer.start_as_current_span("chat") as span:
            span.set_attribute("openinference.span.kind", "LLM")
            span.set_attribute("llm.model_name", "gpt-4.1")
            span.set_attribute("input.value", user_input)
            span.set_attribute("output.value", assistant_output)

    tracer_provider.shutdown()
    print(f"Seeded {len(exchanges)} span(s) into project '{project_name}'.")


def seed_project_groups() -> None:
    """OTLP ingest has no group-selection mechanism of its own -- every
    project it auto-creates lands in the well-known default project group
    (see phoenix.db.insertion.span.insert_span). Reassigns each demo
    project to its own dedicated group, then persists every external-role
    mapping in ROLE_GRANTS -- this is the database-table config the
    requirement calls for, not a naming convention.
    """
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            group_ids: dict[str, int] = {}
            for project_name, group_name in PROJECT_GROUPS.items():
                # Ingest is queued (see `enqueue_span` /
                # `background_tasks.add_task` in
                # src/phoenix/server/api/routers/v1/traces.py) -- the OTLP
                # export call returning does not mean the project row has
                # been inserted yet. Poll for it instead of racing ahead.
                project_id = None
                for _ in range(50):
                    cur.execute("SELECT id FROM projects WHERE name = %s", (project_name,))
                    row = cur.fetchone()
                    if row is not None:
                        project_id = row[0]
                        break
                    time.sleep(0.2)
                if project_id is None:
                    sys.exit(
                        f"Project '{project_name}' was never auto-created by ingest -- "
                        "check the Phoenix server log."
                    )

                cur.execute(
                    "INSERT INTO project_groups (name) VALUES (%s) "
                    "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                    "RETURNING id",
                    (group_name,),
                )
                (group_id,) = cur.fetchone()
                group_ids[group_name] = group_id
                cur.execute(
                    "UPDATE projects SET project_group_id = %s WHERE id = %s",
                    (group_id, project_id),
                )

            for external_role, (group_name, role) in ROLE_GRANTS.items():
                group_id = group_ids[group_name]
                cur.execute(
                    "INSERT INTO external_role_project_group_mappings "
                    "(external_role, project_group_id, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT (external_role) DO UPDATE SET "
                    "project_group_id = EXCLUDED.project_group_id, role = EXCLUDED.role",
                    (external_role, group_id, role),
                )
                print(f"Mapped external role '{external_role}' -> group '{group_name}' ({role}).")


if __name__ == "__main__":
    for project_name, exchanges in DEMO_PROJECTS.items():
        seed_project(project_name, exchanges)
    seed_project_groups()
    print(
        "\nDone. As alice-admin both projects should be visible (global "
        "ADMIN bypasses RLS). As bob-user/dave-user only demo-team-alpha/"
        "demo-team-beta (VIEWER, one group each). As grace-groupadmin only "
        "demo-team-alpha (ADMIN, one group -- scoped, unlike alice-admin). "
        "As faye-member/henry-groupadmin, whichever of demo-team-alpha/"
        "demo-team-beta is the active group (VIEWER/ADMIN, two groups each "
        "-- both require picking a group at login and can switch)."
    )
