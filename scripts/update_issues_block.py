"""Refresh the auto-managed issue board in the profile README, alert Telegram.

Open issues plus anything updated in the last LOOKBACK_DAYS are listed in a
marked block of README.md. Issues that were not present in the previous block
are also sent to Telegram when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set.
The previous README block doubles as the "already announced" state, so no
extra storage is needed and a re-run after a failure cannot spam duplicates.
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

README = Path(__file__).resolve().parents[1] / "README.md"
OWNER = "Voltur792"
LOOKBACK_DAYS = 30
MAX_ALERTS = 10
TITLE_LIMIT = 70

BEGIN = "<!-- BEGIN AUTO:ISSUES -->"
END = "<!-- END AUTO:ISSUES -->"
BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
ISSUE_URL = re.compile(r"https://github\.com/Voltur792/[^/)]+/issues/\d+")
TIMESTAMP = re.compile(r"данные на \d{2}\.\d{2}\.\d{4} \d{2}:\d{2} UTC")


def signature(text: str) -> str:
    """Block content ignoring its refresh timestamp — so an empty run commits nothing."""
    return TIMESTAMP.sub("данные на <ts>", text)


def api_get(path: str, params: dict) -> list | dict:
    url = f"https://api.github.com{path}?{urlencode(params)}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{OWNER}-profile-issue-board",
    }
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def search_issues(query: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        payload = api_get("/search/issues", {"q": query, "per_page": 100, "page": page})
        if not isinstance(payload, dict) or "items" not in payload:
            raise ValueError(f"Unexpected search response for {query!r}: {payload}")
        items.extend(payload["items"])
        if len(items) >= payload["total_count"] or not payload["items"]:
            return items
        page += 1


def collect() -> list[dict]:
    since = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    found: dict[str, dict] = {}
    for query in (
        f"is:issue owner:{OWNER} is:open",
        f"is:issue owner:{OWNER} updated:>={since}",
    ):
        for item in search_issues(query):
            if item.get("pull_request"):
                continue
            repo = item["repository_url"].rsplit("/", 2)[-1]
            found[item["html_url"]] = {
                "url": item["html_url"],
                "repo": repo,
                "number": item["number"],
                "title": (item["title"] or "").strip(),
                "author": (item.get("user") or {}).get("login", "?"),
                "state": item["state"],
                "created": item["created_at"][:10],
                "updated": item["updated_at"][:10],
                "comments": item.get("comments", 0),
            }
    # newest activity on top, open issues listed before closed ones
    newest_first = sorted(found.values(), key=lambda i: i["updated"], reverse=True)
    return sorted(newest_first, key=lambda i: 0 if i["state"] == "open" else 1)


def cell(text: str, limit: int = TITLE_LIMIT) -> str:
    text = text.replace("|", "\\|").replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "—"


def render(issues: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    opened = [i for i in issues if i["state"] == "open"]
    lines = [BEGIN, "## Заявки и вопросы", ""]
    if opened:
        lines.append(f"**Открытых заявок: {len(opened)}.**")
    else:
        lines.append("**Открытых заявок нет — все разобранны.**")
    lines.append("")
    lines.append(
        f"Ниже — открытые заявки и всё, что обновлялось за {LOOKBACK_DAYS} дней. "
        f"Раздел ведёт автоматический процесс профиля: данные на {now}."
    )
    lines.append("")
    if issues:
        lines.append("| Репозиторий | Заявка | Автор | Создана | Обновлено | Ответов | Статус |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for issue in issues:
            state = "🔴 открыта" if issue["state"] == "open" else "✅ закрыта"
            lines.append(
                f"| {cell(issue['repo'], 40)} "
                f"| [{cell(issue['title'])}]({issue['url']}) "
                f"| {cell(issue['author'], 25)} "
                f"| {issue['created']} "
                f"| {issue['updated']} "
                f"| {issue['comments']} "
                f"| {state} |"
            )
    else:
        lines.append("За последние {} дней заявок не было.".format(LOOKBACK_DAYS))
    lines.append(END)
    return "\n".join(lines)


def telegram_text(new_issues: list[dict]) -> str:
    def esc(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    head = (
        f"🆕 Новые заявки на GitHub ({len(new_issues)})"
        if len(new_issues) > 1
        else "🆕 Новая заявка на GitHub"
    )
    rows = [head, ""]
    for issue in new_issues[:MAX_ALERTS]:
        state = "открыта" if issue["state"] == "open" else "закрыта"
        rows.append(
            f"• <b>{esc(issue['repo'])} #{issue['number']}</b> — {esc(cell(issue['title']))}"
            f"\n  от {esc(issue['author'])}, {state}, ответов {issue['comments']}"
            f"\n  {issue['url']}"
        )
    if len(new_issues) > MAX_ALERTS:
        rows.append(f"… и ещё {len(new_issues) - MAX_ALERTS} (см. профиль)")
    return "\n".join(rows)


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("Telegram secrets are not set — alert skipped.")
        return False
    body = urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"User-Agent": f"{OWNER}-profile-issue-alert"},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise ValueError(f"Telegram API rejected the message: {payload.get('description')}")
    print(f"Telegram message sent ({len(text)} chars).")
    return True


def main() -> int:
    original = README.read_text(encoding="utf-8")
    issues = collect()
    new_block = render(issues)
    announced = set(ISSUE_URL.findall(original)) if BLOCK.search(original) else None

    if BLOCK.search(original):
        updated = BLOCK.sub(lambda _m: new_block, original, count=1)
    else:
        for anchor in ("\n---\n", "\n[Поддержать разработку на Boosty]"):
            if anchor in original:
                updated = original.replace(anchor, f"\n{new_block}\n{anchor.lstrip()}", 1)
                break
        else:
            updated = original.rstrip() + "\n\n" + new_block + "\n"
        print("Issue block not found — inserted it; existing issues are not alerted "
              "(first run only seeds the block).")

    if signature(updated) != signature(original):
        README.write_text(updated, encoding="utf-8")
        print("README.md issue block updated.")
    else:
        print("README.md issue block unchanged.")

    pending = [i for i in issues if announced is None or i["url"] not in announced]
    if announced is None:
        print(f"Seeded {len(issues)} issues without Telegram alerts.")
    elif pending:
        send_telegram(telegram_text(pending))
    else:
        print("No new issues since the last run.")
    for issue in issues:
        print(f"  {issue['repo']}#{issue['number']} [{issue['state']}] {issue['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
