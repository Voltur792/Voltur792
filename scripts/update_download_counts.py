"""Refresh profile badges using downloads of installable release assets."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


README = Path(__file__).resolve().parents[1] / "README.md"
INSTALLERS = {
    "detective-game": re.compile(r"\.astraplugin$"),
    "astra-sea-battle": re.compile(r"\.astraplugin$"),
    "astra-music": re.compile(r"\.astraplugin$"),
    "auto-assistant": re.compile(r"\.astraplugin$"),
    "browser-control": re.compile(r"\.astraplugin$"),
    "SPT": re.compile(r"\.astraplugin$"),
    "voice-text-input": re.compile(r"\.astraplugin$"),
    "Y-home-f-astra": re.compile(r"\.astraplugin$"),
    "VoiceTyper": re.compile(r"^VoiceTyper-v.+-windows-x64\.zip$"),
    "sleep-pause-timer": re.compile(r"^SleepPauseTimer-v.+-windows-x64\.zip$"),
}
PROJECT_LINK = re.compile(r"\]\(https://github\.com/Voltur792/([^/)]+)\)")
DOWNLOAD_BADGE = re.compile(
    r"https://img\.shields\.io/(?:"
    r"github/downloads/Voltur792/[^/)]+/total\?label=загрузки"
    r"|badge/downloads-\d+-blue\?label=[^)]*)"
)
BADGE_LABEL = quote("загрузки")
COUNTER_NOTE = re.compile(r"Счётчики показывают .*?в них не входят\.(?: Данные на \d{2}\.\d{2}\.\d{4} \(UTC\)\.)?")


def installer_downloads(repo: str, pattern: re.Pattern[str]) -> int:
    total = 0
    page = 1
    while True:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Voltur792-profile-download-counter",
        }
        if token := os.getenv("GH_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"https://api.github.com/repos/Voltur792/{repo}/releases?per_page=100&page={page}",
            headers=headers,
        )
        with urlopen(request, timeout=30) as response:
            releases = json.load(response)
        if not isinstance(releases, list):
            raise ValueError(f"Unexpected releases response for {repo}")
        for release in releases:
            if release["draft"]:
                continue
            for asset in release["assets"]:
                if pattern.search(asset["name"]):
                    total += asset["download_count"]
        if len(releases) < 100:
            return total
        page += 1


def main() -> None:
    original = README.read_text(encoding="utf-8")
    updated_lines = []
    seen = set()
    for line in original.splitlines(keepends=True):
        if "[![Загрузки]" not in line:
            updated_lines.append(line)
            continue
        project = PROJECT_LINK.search(line)
        if project is None or project.group(1) not in INSTALLERS:
            raise ValueError(f"No installer rule for badge row: {line.strip()}")
        repo = project.group(1)
        if repo in seen:
            raise ValueError(f"Duplicate download badge for {repo}")
        count = installer_downloads(repo, INSTALLERS[repo])
        badge = f"https://img.shields.io/badge/downloads-{count}-blue?label={BADGE_LABEL}"
        line, replacements = DOWNLOAD_BADGE.subn(badge, line)
        if replacements != 1:
            raise ValueError(f"Expected one download badge for {repo}, found {replacements}")
        updated_lines.append(line)
        seen.add(repo)
        print(f"{repo}: {count}")
    if seen != INSTALLERS.keys():
        raise ValueError(f"Missing download badges: {set(INSTALLERS) - seen}")
    updated = "".join(updated_lines)
    date = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    note = (
        "Счётчики показывают суммарные загрузки установочных файлов из GitHub Releases "
        "(пакеты .astraplugin и ZIP приложений); скачивания ZIP репозитория и Git-клоны "
        f"в них не входят. Данные на {date} (UTC)."
    )
    updated, replacements = COUNTER_NOTE.subn(note, updated)
    if replacements != 1:
        raise ValueError(f"Expected one counter note, found {replacements}")
    if updated != original:
        README.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
