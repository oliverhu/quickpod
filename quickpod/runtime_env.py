"""Load `.env` from cwd or parents, then read `RUNPOD_API_KEY`."""

from __future__ import annotations

import os
import sys

from dotenv import find_dotenv, load_dotenv

_dotenv_loaded = False


def load_dotenv_if_present() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    load_dotenv(find_dotenv(usecwd=True))
    _dotenv_loaded = True


def require_runpod_api_key() -> str:
    load_dotenv_if_present()
    k = (os.environ.get("RUNPOD_API_KEY") or "").strip()
    if not k or "REPLACE" in k:
        print(
            "Set RUNPOD_API_KEY in the environment or in a `.env` file (see `.env.example` in the repo).",
            file=sys.stderr,
        )
        sys.exit(2)
    return k
