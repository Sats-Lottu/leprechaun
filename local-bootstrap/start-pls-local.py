"""Development only: load local PLS credentials. Never run in production."""

import json
import os
from pathlib import Path

os.environ.update(json.loads(Path("/local/lnbits.json").read_text()))
os.execvp("sh", ["sh", "/app/entrypoint.sh"])
