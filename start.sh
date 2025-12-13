#!/usr/bin/env bash
set -euo pipefail

# Default to PORT if provided by the platform, else fall back to 8000.
PORT="${PORT:-8000}"

# Launch the FastAPI app.
python -m uvicorn web_portal.main:app --host 0.0.0.0 --port "${PORT}"
