# /// script
# dependencies = [
#   "opentelemetry-sdk",
#   "opentelemetry-exporter-otlp",
# ]
# ///
"""Seeds two demo projects (demo-team-alpha, demo-team-beta) with a
couple of sample LLM spans each, for local-dev/rls-demo/README.md's
manual row-level-isolation test matrix.

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
"""

import os
import sys

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


if __name__ == "__main__":
    for project_name, exchanges in DEMO_PROJECTS.items():
        seed_project(project_name, exchanges)
    print(
        "\nDone. As alice-admin both projects should be visible; as "
        "bob-user only demo-team-alpha; as dave-user only demo-team-beta."
    )
