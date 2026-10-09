"""Prepare predictable runtime paths before importing application services."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeContext:
    home: Path
    env_file: Path
    packaged: bool


def prepare_runtime_environment() -> RuntimeContext:
    """Keep packaged configuration in the user's workspace and source configuration in the repo."""
    packaged = bool(getattr(sys, "frozen", False))
    home = (
        Path.home() / "Library" / "Application Support" / "Elan"
        if packaged else Path(__file__).resolve().parents[2]
    )
    home = Path(os.environ.get("ELAN_HOME", home)).expanduser().resolve()
    env_file = Path(os.environ.get("ELAN_ENV_FILE", home / ".env")).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    if packaged:
        marker = home / ".configuration-initialized"
        model_file = env_file.with_name("model_config.json")
        if not marker.exists():
            if not env_file.exists():
                env_file.parent.mkdir(parents=True, exist_ok=True)
                env_file.write_text(
                    f"DATABASE_URL=sqlite:///{home / 'data' / 'smartapply.db'}\n"
                    f"PROFILE_DIR={home / 'profile'}\nOUTPUT_DIR={home / 'documents'}\n"
                    f"CACHE_DIR={home / 'cache'}\nOPENAI_API_KEY=\nANTHROPIC_API_KEY=\n",
                    encoding="utf-8",
                )
                env_file.chmod(0o600)
            if not model_file.exists():
                template = Path(__file__).resolve().parents[2] / "model_config.json"
                if not template.exists():
                    raise RuntimeError(f"Modèle de configuration IA absent du paquet : {template}")
                model_file.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=model_file.parent, delete=False) as output:
                    temporary = Path(output.name)
                    with template.open("rb") as source:
                        shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, model_file)
            marker.write_text("initialized\n", encoding="utf-8")
    os.environ.setdefault("ELAN_HOME", str(home))
    os.environ.setdefault("ELAN_ENV_FILE", str(env_file))
    os.chdir(home)
    return RuntimeContext(home=home, env_file=env_file, packaged=packaged)
