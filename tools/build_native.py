"""Build the native Windows executable or macOS application for this host."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / "tools" / "build_icons.py")], check=True, cwd=root)
    build_root = root / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system not in {"Windows", "Darwin"}:
        raise SystemExit("Native release builds are supported on Windows and macOS hosts.")
    name = "MatElabConnector" if system == "Windows" else "MatElab Connector"
    icon = (
        root
        / "build-assets"
        / ("matelab-connector.ico" if system == "Windows" else "matelab-connector.icns")
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--specpath",
        str(build_root),
        "--workpath",
        str(build_root / "pyinstaller"),
        "--windowed",
        "--name",
        name,
        "--icon",
        str(icon),
        "--collect-all",
        "matelab_bridge",
        "--collect-submodules",
        "keyring.backends",
        "--collect-submodules",
        "uvicorn",
    ]
    if system == "Windows":
        command.append("--onefile")
    else:
        command.extend(["--osx-bundle-identifier", "cn.ac.iphy.matelab.connector"])
    command.append(str(root / "tools" / "native_entry.py"))
    subprocess.run(command, check=True, cwd=root)


if __name__ == "__main__":
    main()
