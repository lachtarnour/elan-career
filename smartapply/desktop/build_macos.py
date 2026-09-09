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
APP_VERSION = "0.1.0"


def _render_icon(output_dir: Path = BUILD_DIR) -> Path:
    source = PROJECT_ROOT / "smartapply" / "desktop" / "resources" / "app_icon.svg"
    output_dir.mkdir(parents=True, exist_ok=True)
    iconset = output_dir / f"{BUNDLE_NAME}.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True, exist_ok=True)
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise RuntimeError(f"Invalid SVG icon: {source}")
    targets = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for name, size in targets.items():
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        if not image.save(str(iconset / name)):
            raise RuntimeError(f"Could not write icon size {size}")
    output = output_dir / f"{BUNDLE_NAME}.icns"
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(output)],
            check=True,
        )
    except subprocess.CalledProcessError:
        # Some macOS SDK/iconutil versions reject Qt-generated iconsets even
        # when all required sizes are present. Reuse the last valid bundle
        # icon so an application rebuild is not blocked by presentation-only
        # metadata.
        previous = DIST_APP / "Contents" / "Resources" / f"{BUNDLE_NAME}.icns"
        if not previous.exists():
            raise
        shutil.copy2(previous, output)
    return output


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
    os.chdir(PROJECT_ROOT)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the icon outside BUILD_DIR: concurrent or interrupted PyInstaller
    # cleanup must not remove it before the macOS BUNDLE phase consumes it.
    icon_workspace = tempfile.TemporaryDirectory(prefix="elan-macos-icon-")
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
        f"{PROJECT_ROOT / 'smartapply' / 'llm' / 'prompts' / 'templates'}:smartapply/llm/prompts/templates",
        str(entry),
    ]
    try:
        subprocess.run(command, check=True)
    finally:
        icon_workspace.cleanup()
    if not DIST_APP.exists():
        raise RuntimeError(f"PyInstaller completed without producing {BUNDLE_NAME}.app")
    _finalize_bundle_metadata()
    if smoke_test:
        executable = DIST_APP / "Contents" / "MacOS" / BUNDLE_NAME
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        subprocess.run([str(executable), "--smoke-test"], check=True, env=env, timeout=90)
    return DIST_APP


def _finalize_bundle_metadata() -> None:
    plist_path = DIST_APP / "Contents" / "Info.plist"
    with plist_path.open("rb") as stream:
        metadata = plistlib.load(stream)
    metadata.update(
        {
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": "1",
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
