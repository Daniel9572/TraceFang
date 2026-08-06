from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_project_environment(project_root: Path) -> bool:
    """Load local settings without overriding explicitly injected process values."""
    return load_dotenv(dotenv_path=project_root / ".env", override=False)
