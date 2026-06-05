from __future__ import annotations

import subprocess
import sys


def main() -> None:
    command = [
        sys.executable, "-m", "PyInstaller",
        "--clean", "--noconfirm", "--onefile",
        "--name", "nima-export",
        "--collect-all", "browser_cookie3",
        "--collect-all", "imageio_ffmpeg",
        "nima_exporter.py",
    ]
    print(" ".join(command))
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
