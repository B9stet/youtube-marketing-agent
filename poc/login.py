"""Open ordinary Chrome for the operator to sign in to the dedicated profile."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess

from .config import PROFILES, profile_directory


def chrome_command(profile: str = "youtube1") -> list[str]:
    directory = profile_directory(profile)
    directory.mkdir(parents=True, exist_ok=True)
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    ]
    chrome = next((str(path) for path in candidates if path.is_file()), None)
    chrome = chrome or shutil.which("chrome")
    if not chrome:
        raise RuntimeError("Chrome을 찾을 수 없습니다.")
    return [chrome, f"--user-data-dir={directory}", "--lang=ko-KR", "--no-first-run", "--new-window", "https://www.youtube.com/"]


def open_profile(profile: str = "youtube1") -> None:
    """Open ordinary, visible Chrome so the user can sign in manually."""
    subprocess.Popen(chrome_command(profile), close_fds=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, default="youtube1")
    args = parser.parse_args()
    print("일반 Chrome에서 YouTube에 로그인한 뒤 창을 닫아 주세요.", flush=True)
    subprocess.run(chrome_command(args.profile), check=False)


if __name__ == "__main__":
    main()
