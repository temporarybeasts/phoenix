# /// script
# dependencies = [
#   "opentelemetry-sdk",
#   "opentelemetry-exporter-otlp",
#   "faker",
# ]
# ///
"""Fake chat app that streams synthetic OTLP traces into a Phoenix
project on a loop -- run as a Docker container (see ./Dockerfile), one
instance per fake app identity, to demo registering a new project and
watching it receive live traces (see local-dev/rls-demo/README.md,
section 5).

Requires a pre-minted Phoenix API key -- mint one per
instance/identity the same way local-dev/rls-demo/seed_projects.py's
docstring already documents (admin login + POST /v1/user/api_keys), so
each instance shows up as a distinct key in the admin UI.

Env vars:
    CHAT_APP_NAME           required -- identity label, used in logging only
    PHOENIX_API_KEY          required -- pre-minted API key for this identity
    PHOENIX_PROJECT_NAME     default: CHAT_APP_NAME -- Phoenix project traces land in
    PHOENIX_BASE_URL         default: http://host.docker.internal:6006
    TRACE_INTERVAL_SECONDS   default: 5
"""

import os
import random
import signal
import sys
import time

from faker import Faker
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

CHAT_APP_NAME = os.environ.get("CHAT_APP_NAME")
if not CHAT_APP_NAME:
    sys.exit("CHAT_APP_NAME is not set -- see this script's module docstring.")

API_KEY = os.environ.get("PHOENIX_API_KEY")
if not API_KEY:
    sys.exit(
        "PHOENIX_API_KEY is not set -- mint one per instance the same way "
        "local-dev/rls-demo/seed_projects.py's docstring documents, then "
        "pass it in via -e PHOENIX_API_KEY=<key>. See "
        "local-dev/rls-demo/README.md section 5."
    )

PROJECT_NAME = os.environ.get("PHOENIX_PROJECT_NAME", CHAT_APP_NAME)
BASE_URL = os.environ.get("PHOENIX_BASE_URL", "http://host.docker.internal:6006")
INTERVAL_SECONDS = float(os.environ.get("TRACE_INTERVAL_SECONDS", "5"))

MODEL_NAMES = ["gpt-4.1", "gpt-4o-mini", "claude-3-5-sonnet", "gemini-1.5-pro"]

fake = Faker()
running = True


def _handle_shutdown_signal(signum, frame) -> None:
    global running
    running = False


signal.signal(signal.SIGTERM, _handle_shutdown_signal)
signal.signal(signal.SIGINT, _handle_shutdown_signal)


def send_fake_chat_trace(tracer) -> None:
    query = fake.sentence()
    with tracer.start_as_current_span("chat") as chat_span:
        chat_span.set_attribute("openinference.span.kind", "LLM")
        chat_span.set_attribute("llm.model_name", random.choice(MODEL_NAMES))
        chat_span.set_attribute("input.value", query)

        with tracer.start_as_current_span("retrieve_context") as retrieve_span:
            retrieve_span.set_attribute("openinference.span.kind", "RETRIEVER")
            retrieve_span.set_attribute("input.value", query)
            retrieve_span.set_attribute("output.value", " ".join(fake.sentences(nb=2)))

        chat_span.set_attribute("output.value", fake.paragraph())

    print(f"[{CHAT_APP_NAME}] sent trace to project {PROJECT_NAME!r}: {query!r}", flush=True)


def main() -> None:
    tracer_provider = TracerProvider(
        resource=Resource({"openinference.project.name": PROJECT_NAME})
    )
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                f"{BASE_URL}/v1/traces",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
        )
    )
    tracer = tracer_provider.get_tracer(__name__)

    print(
        f"[{CHAT_APP_NAME}] streaming synthetic traces to project "
        f"{PROJECT_NAME!r} at {BASE_URL} every {INTERVAL_SECONDS}s.",
        flush=True,
    )
    while running:
        send_fake_chat_trace(tracer)
        time.sleep(INTERVAL_SECONDS)

    tracer_provider.shutdown()
    print(f"[{CHAT_APP_NAME}] shut down.", flush=True)


if __name__ == "__main__":
    main()
