"""Refresh the auto-managed issue board in the profile README, alert Telegram.

Open issues plus anything updated in the last LOOKBACK_DAYS are listed in a
marked block of README.md. That block doubles as the alert ledger: the run
compares the stored "comments" and status columns against live GitHub data and
sends Telegram alerts for new issues, new comments and status changes. No extra
state file is needed; the trade-off is at-least-once delivery (if the commit
fails after a message was sent, the next run repeats it).
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
TIMESTAMP = re.compile(r"данные на \d{2}\.\d{2}\.\d{4} \d{2}:\d{2} UTC")
ROW_URL = re.compile(r"\((?P<url>https://github\.com/Voltur792/[^/)]+/issues/\d+)\)")
CELL_SPLIT = re.compile(r"(?<!\\)\|")


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
        lines.append(f"За последние {LOOKBACK_DAYS} дней заявок не было.")
    lines.append(END)
    return "\n".join(lines)


def parse_seen(text: str) -> dict | None:
    """Read the previous block: issue URL -> {"comments": n, "closed": bool}. None = no block."""
    match = BLOCK.search(text)
    if not match:
        return None
    seen: dict[str, dict] = {}
    for line in match.group(0).splitlines():
        url = ROW_URL.search(line)
        if not url:
            continue
        cells = [c.strip() for c in CELL_SPLIT.split(line) if c.strip()]
        if len(cells) < 7:
            print(f"Ignoring unreadable block row: {line[:80]}")
            continue
        try:
            comments = int(cells[5])
        except ValueError:
            print(f"Ignoring row with a non-numeric answer count: {line[:80]}")
            continue
        seen[url.group("url")] = {"comments": comments, "closed": "закрыта" in cells[-1]}
    return seen


def classify(issues: list[dict], seen: dict | None) -> tuple[list, list, list]:
    new: list[dict] = []
    commented: list[tuple[dict, int]] = []
    state_changed: list[dict] = []
    if seen is None:
        return new, commented, state_changed
    for issue in issues:
        previous = seen.get(issue["url"])
        if previous is None:
            new.append(issue)
            continue
        if issue["comments"] > previous["comments"]:
            commented.append((issue, issue["comments"] - previous["comments"]))
        if (issue["state"] == "closed") != previous["closed"]:
            state_changed.append(issue)
    return new, commented, state_changed


def comment_anchor(issue: dict) -> str:
    """Deep link to the newest comment; fall back to the plain issue link."""
    page = max(1, -(-issue["comments"] // 100))
    try:
        payload = api_get(
            f"/repos/{OWNER}/{issue['repo']}/issues/{issue['number']}/comments",
            {"per_page": 100, "page": page},
        )
        if isinstance(payload, list) and payload:
            return f"{issue['url']}#issuecomment-{payload[-1]['id']}"
    except Exception as error:  # the deep link is only a convenience
        print(f"Comment anchor lookup failed for {issue['repo']}#{issue['number']}: {error}")
    return issue["url"]


def esc(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def alert_text(new: list, commented: list, state_changed: list) -> str:
    sections: list[str] = []

    if new:
        head = f"🆕 Новые заявки ({len(new)})" if len(new) > 1 else "🆕 Новая заявка"
        rows = [head]
        for issue in new[:MAX_ALERTS]:
            state = "открыта" if issue["state"] == "open" else "закрыта"
            rows.append(
                f"• <b>{esc(issue['repo'])} #{issue['number']}</b> — {esc(cell(issue['title']))}"
                f"\n  от {esc(issue['author'])}, {state}, ответов {issue['comments']}"
                f"\n  {issue['url']}"
            )
        if len(new) > MAX_ALERTS:
            rows.append(f"… и ещё {len(new) - MAX_ALERTS} (см. профиль)")
        sections.append("\n".join(rows))

    if commented:
        total = sum(delta for _issue, delta in commented)
        rows = [f"💬 Новые комментарии ({total})"]
        for issue, delta in commented[:MAX_ALERTS]:
            rows.append(
                f"• <b>{esc(issue['repo'])} #{issue['number']}</b> — "
                f"<a href=\"{comment_anchor(issue)}\">{esc(cell(issue['title']))}</a>"
                f"\n  +{delta} новых ответов, всего {issue['comments']}"
                f"\n  заявка от {esc(issue['author'])}, "
                f"{'открыта' if issue['state'] == 'open' else 'закрыта'}"
            )
        if len(commented) > MAX_ALERTS:
            rows.append(f"… и ещё {len(commented) - MAX_ALERTS} (см. профиль)")
        sections.append("\n".join(rows))

    if state_changed:
        rows = ["🔒 Изменился статус заявок"]
        for issue in state_changed[:MAX_ALERTS]:
            verb = "закрыта" if issue["state"] == "closed" else "снова открыта"
            rows.append(
                f"• <b>{esc(issue['repo'])} #{issue['number']}</b> — {esc(cell(issue['title']))}"
                f"\n  {verb}, ответов {issue['comments']}"
                f"\n  {issue['url']}"
            )
        sections.append("\n".join(rows))

    return "\n\n".join(sections)


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("Telegram secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set — "
              "message NOT sent. It would look like this:")
        print(re.sub(r"<[^>]+>", "", text))
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
    seen = parse_seen(original)
    issues = collect()
    new_block = render(issues)

    if seen is not None:
        updated = BLOCK.sub(lambda _m: new_block, original, count=1)
    else:
        for anchor in ("\n---\n", "\n[Поддержать разработку на Boosty]"):
            if anchor in original:
                updated = original.replace(anchor, f"\n{new_block}\n{anchor.lstrip()}", 1)
                break
        else:
            updated = original.rstrip() + "\n\n" + new_block + "\n"
        print("Issue block not found — inserting it; existing issues are not alerted "
              "(the first run only seeds the ledger).")

    if signature(updated) != signature(original):
        README.write_text(updated, encoding="utf-8")
        print("README.md issue block updated.")
    else:
        print("README.md issue block unchanged.")

    new, commented, state_changed = classify(issues, seen)
    if seen is None:
        print(f"Seeded {len(issues)} issues without Telegram alerts.")
    elif new or commented or state_changed:
        print(f"Alerts: {len(new)} new, {len(commented)} with new comments, "
              f"{len(state_changed)} status changes.")
        send_telegram(alert_text(new, commented, state_changed))
    else:
        print("No new issues, comments or status changes since the last run.")

    for issue in issues:
        print(f"  {issue['repo']}#{issue['number']} [{issue['state']}] "
              f"{issue['comments']} отв. — {issue['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
