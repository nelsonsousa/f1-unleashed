"""Central runtime configuration loaded from .env (see .env.example).

Loading happens here, independently of any single service, so settings are
available regardless of import order. F1 auth is NOT configured here — the
F1 site has anti-robot measures, so login is always manual.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Keep the transient scratch DB (and other ephemeral processing/analysis
# artefacts) after use, for inspection, instead of deleting them. Default off.
REPLAY_DEBUG = os.getenv("REPLAY_DEBUG", "0") == "1"
