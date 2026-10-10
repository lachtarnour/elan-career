"""Build and smoke-test the standalone Élan macOS bundle."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = PROJECT_ROOT / "build" / "macos"
# Keep the executable ASCII-only while presenting the accented product name in macOS.
BUNDLE_NAME = "Elan"
PRODUCT_NAME = "Élan"
DIST_APP = PROJECT_ROOT / "dist" / f"{BUNDLE_NAME}.app"
RUNTIME_HOME = Path.home() / "Library" / "Application Support" / "Elan"
APP_VERSION = "0.2.0"


def _running_bundle_pids(executable: Path) -> list[int]:
    """Return processes launched from the packaged executable."""
    pattern = rf"^{re.escape(str(executable))}( |$)"
    result = subprocess.run(
        ["pgrep", "-f", pattern],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"Could not check whether {PRODUCT_NAME} is running: {detail}")
    return [int(pid) for pid in result.stdout.split()]


def _assert_bundle_not_running() -> None:
    """Prevent PyInstaller from replacing an archive used by a live process."""
    executable = DIST_APP / "Contents" / "MacOS" / BUNDLE_NAME
    pids = _running_bundle_pids(executable)
    if pids:
        joined = ", ".join(str(pid) for pid in pids)
        raise RuntimeError(
            f"{PRODUCT_NAME} is still running from {DIST_APP} (PID {joined}). "
            "Quit the application before rebuilding it. Replacing a live PyInstaller "
            "bundle corrupts deferred module imports in that process."
        )


def _render_icon(output_dir: Path = BUILD_DIR) -> Path:
    source = PROJECT_ROOT / "smartapply" / "desktop" / "resources" / "app_icon.svg"
    output_dir.mkdir(parents=True, exist_ok=True)
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise RuntimeError(f"Invalid SVG icon: {source}")
    master = output_dir / f"{BUNDLE_NAME}-1024.png"
    image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, 1024, 1024))
    painter.end()
    if not image.save(str(master)):
        raise RuntimeError("Could not write 1024px application icon")
    output = output_dir / f"{BUNDLE_NAME}.icns"
    with Image.open(master) as rendered:
        rendered.save(output, format="ICNS")
    if output.read_bytes()[:4] != b"icns":
        raise RuntimeError(f"Invalid ICNS icon generated at {output}")
    return output


def _smoke_test_env(runtime_home: Path) -> dict[str, str]:
    """Create an isolated runtime so a build check cannot mutate user data."""
    env = os.environ.copy()
    env.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "ELAN_HOME": str(runtime_home),
            "ELAN_ENV_FILE": str(runtime_home / ".env"),
            "DATABASE_URL": f"sqlite:///{runtime_home / 'smartapply.db'}",
            "PROFILE_DIR": str(PROJECT_ROOT / "smartapply" / "profile" / "mock_profile"),
            "OUTPUT_DIR": str(runtime_home / "documents"),
            "CACHE_DIR": str(runtime_home / "cache"),
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
        }
    )
    return env


def _portable_env(source: Path, destination: Path) -> None:
    replacements = {
        "DATABASE_URL": f"sqlite:///{RUNTIME_HOME / 'data' / 'smartapply.db'}",
        "PROFILE_DIR": str(RUNTIME_HOME / "profile"),
        "OUTPUT_DIR": str(RUNTIME_HOME / "documents"),
        "CACHE_DIR": str(RUNTIME_HOME / "cache"),
    }
    env_text = source.read_text(encoding="utf-8")
    for key, value in replacements.items():
        pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
        line = f"{key}={value}"
        if pattern.search(env_text):
            env_text = pattern.sub(line, env_text)
        else:
            env_text += f"\n{line}"
    destination.write_text(env_text.rstrip() + "\n", encoding="utf-8")


def _copy_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)


def provision_runtime() -> str:
    """Seed Application Support once; never overwrite the desktop user's data."""
    env_source = PROJECT_ROOT / ".env"
    env_target = RUNTIME_HOME / ".env"
    if env_target.exists():
        return f"Runtime already present at {RUNTIME_HOME} (left unchanged)."
    if not env_source.exists():
        raise RuntimeError("Missing project .env; cannot provision the desktop runtime.")

    from smartapply.config import Settings

    settings = Settings(_env_file=str(env_source))
    RUNTIME_HOME.mkdir(parents=True, exist_ok=True)
    _portable_env(env_source, env_target)
    env_target.chmod(0o600)

    database_url = make_url(settings.database_url)
    if database_url.get_backend_name() != "sqlite" or not database_url.database:
        raise RuntimeError("The macOS installer currently expects a local SQLite database.")
    source_db = Path(database_url.database).expanduser().resolve()
    if source_db.exists():
        _copy_sqlite(source_db, RUNTIME_HOME / "data" / "smartapply.db")

    profile_source = settings.profile_dir.expanduser().resolve()
    profile_target = RUNTIME_HOME / "profile"
    if profile_source.exists():
        shutil.copytree(profile_source, profile_target, dirs_exist_ok=True)

    for directory in ("documents", "cache"):
        (RUNTIME_HOME / directory).mkdir(parents=True, exist_ok=True)
    return f"Runtime provisioned at {RUNTIME_HOME}."


def build_bundle(*, smoke_test: bool = True) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError(f"{PRODUCT_NAME} can only be built on macOS.")
    _assert_bundle_not_running()
    os.chdir(PROJECT_ROOT)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the icon outside BUILD_DIR: concurrent or interrupted PyInstaller
    # cleanup must not remove it before the macOS BUNDLE phase consumes it.
    icon_workspace = tempfile.TemporaryDirectory(prefix="elan-macos-icon-")
    pyinstaller_workspace = tempfile.TemporaryDirectory(prefix="elan-pyinstaller-")
    icon = _render_icon(Path(icon_workspace.name))
    entry = PROJECT_ROOT / "smartapply" / "desktop" / "__main__.py"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        BUNDLE_NAME,
        "--distpath",
        str(DIST_APP.parent),
        "--specpath",
        str(BUILD_DIR),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--icon",
        str(icon),
        "--osx-bundle-identifier",
        "app.elan.career",
        "--additional-hooks-dir",
        str(PROJECT_ROOT / "smartapply" / "desktop" / "pyinstaller_hooks"),
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "pyarrow",
        "--exclude-module",
        "tiktoken",
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "weasyprint",
        "--exclude-module",
        "mypy",
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineQuick",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
        "--add-data",
        f"{PROJECT_ROOT / 'smartapply' / 'desktop' / 'resources' / '*.svg'}:smartapply/desktop/resources",
        "--add-data",
        f"{PROJECT_ROOT / 'smartapply' / 'desktop' / 'resources' / 'icons' / '*.svg'}:smartapply/desktop/resources/icons",
        "--add-data",
        f"{PROJECT_ROOT / 'smartapply' / 'desktop' / 'qml'}:smartapply/desktop/qml",
        "--hidden-import",
        "PySide6.QtQml",
        "--hidden-import",
        "PySide6.QtQuick",
        "--hidden-import",
        "PySide6.QtQuickControls2",
        "--add-data",
        f"{PROJECT_ROOT / 'smartapply' / 'cv' / 'templates'}:smartapply/cv/templates",
        "--add-data",
        f"{PROJECT_ROOT / 'smartapply' / 'cv' / 'role_contracts.json'}:smartapply/cv",
        "--add-data",
        f"{PROJECT_ROOT / 'smartapply' / 'dedup' / 'data'}:smartapply/dedup/data",
        "--add-data",
        f"{PROJECT_ROOT / 'smartapply' / 'llm' / 'prompts' / 'templates'}:smartapply/llm/prompts/templates",
        "--add-data",
        f"{PROJECT_ROOT / 'model_config.json'}:.",
        str(entry),
    ]
    try:
        build_env = os.environ.copy()
        build_env["PYINSTALLER_CONFIG_DIR"] = pyinstaller_workspace.name
        subprocess.run(command, check=True, env=build_env)
    finally:
        icon_workspace.cleanup()
        pyinstaller_workspace.cleanup()
    if not DIST_APP.exists():
        raise RuntimeError(f"PyInstaller completed without producing {BUNDLE_NAME}.app")
    _finalize_bundle_metadata()
    if smoke_test:
        executable = DIST_APP / "Contents" / "MacOS" / BUNDLE_NAME
        with tempfile.TemporaryDirectory(prefix="elan-smoke-") as runtime:
            subprocess.run(
                [str(executable), "--smoke-test"],
                check=True,
                env=_smoke_test_env(Path(runtime)),
                timeout=90,
            )
    return DIST_APP


def _finalize_bundle_metadata() -> None:
    plist_path = DIST_APP / "Contents" / "Info.plist"
    with plist_path.open("rb") as stream:
        metadata = plistlib.load(stream)
    metadata.update(
        {
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": "2",
            "CFBundleDevelopmentRegion": "fr",
            "LSApplicationCategoryType": "public.app-category.productivity",
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            "CFBundleDisplayName": PRODUCT_NAME,
            "CFBundleName": PRODUCT_NAME,
            "NSHumanReadableCopyright": f"Copyright © 2026 {PRODUCT_NAME}",
        }
    )
    with plist_path.open("wb") as stream:
        plistlib.dump(metadata, stream)
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(DIST_APP)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(DIST_APP)],
        check=True,
        capture_output=True,
        text=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-smoke-test", action="store_true")
    args = parser.parse_args(argv)
    os.chdir(PROJECT_ROOT)
    print(provision_runtime())
    bundle = build_bundle(smoke_test=not args.no_smoke_test)
    print(f"Built and verified: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
