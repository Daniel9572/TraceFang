from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_project_environment(project_root: Path) -> bool:
    """Load process > .env.local > .env without exposing local secrets to Git."""
    loaded_local = load_dotenv(dotenv_path=project_root / ".env.local", override=False)
    loaded_base = load_dotenv(dotenv_path=project_root / ".env", override=False)
    return loaded_local or loaded_base
