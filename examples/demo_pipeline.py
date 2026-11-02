"""Runnable fake pipeline: no Claude CLI needed, events land in PostHog in about a minute.

    export POSTHOG_API_KEY=phc_...                 # project API key (Project settings)
    export POSTHOG_HOST=https://eu.i.posthog.com   # or https://us.i.posthog.com
    uv run python examples/demo_pipeline.py        # then open AI observability > Traces

Without a key, add --print to see the exact events as JSON lines.
The pipeline code lives in src/agent_observability/demo.py (also installed as `agent-obs-demo`).
"""

import sys

from agent_observability.demo import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
