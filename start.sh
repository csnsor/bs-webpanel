#!/usr/bin/env bash
set -euo pipefail

# Launch the FastAPI app. `web_portal.run` reads PORT safely across shells/platforms.
python -m web_portal.run
