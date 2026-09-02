"""Watch ticket pages and push to Peter's phone when they change.

Two events, two very different sites:

  zen        THE ZEN CIRCUS (ZEN Halloween), 31 Oct 2026, on islandetickets.
             Server-rendered. The event page ships an EMPTY
             <div class="tickets-container"> and pulls the tiers in afterwards
             from a fragment endpoint, so hitting that endpoint directly gets
             the real data with no browser at all.

  wickedjab  Wicked Jab -- Bad Beez, 8 Nov 2026, on Jouvert Jumbeez.
             A Next.js app backed by Firebase. NOTHING about the tickets is in
             the HTML -- the server sends only title/date/venue and the tiers
             arrive client-side after the app authenticates itself. Its
             /api/events/<id> answers 401 and Firestore answers
             PERMISSION_DENIED to anonymous requests, so this one genuinely
             needs a browser. Playwright renders the page exactly as a visitor
             would and we read the result.

The point of the wickedjab watch is MALE tickets: every male tier is currently
sold out, so `male_became_available` is the thing worth waking up for and it
gets its own headline.

This runs in two places and has to behave in both:

  * Locally, `py app.py` loops forever and pushes through the notify skill.
  * In GitHub Actions, `py app.py --once` runs on a cron and pushes through
    PUSH_TOKEN (a repo secret). state/*.json is committed back to the repo, so
    the git history of those files IS the history of the ticket pages.

Usage:
    py app.py                        # check both, then every 20 minutes
    py app.py --once                 # single check (for CI)
    py app.py --once --source zen    # just one source
    py app.py --once --no-push       # ...without sending anything to the phone
    py app.py --reset                # forget the baselines and start over
    py app.py --test-notify          # prove the push channel still works
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
LOG_FILE = HERE / "watch.log"

# Locally the push sender is reused from the notify skill rather than
# reimplemented. That file does not exist on a CI runner, hence the fallback
# below: same endpoint, token from the environment. The token is deliberately
# NOT hardcoded here so this repo stays safe to make public.
NOTIFY_PY = Path(r"C:\Users\Peter\.claude\skills\notify\notify.py")
PUSH_URL = os.environ.get("PUSH_URL", "https://pushnotifapp.netlify.app/api/publish")
PUSH_TOKEN = os.environ.get("PUSH_TOKEN")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TIMEOUT = 30

# A single 502 at 3am is not worth a buzz; an hour of them is.
FAILURES_BEFORE_ALERT = 3

# "Female" must not count as male. There is no word boundary between the "e"
# and the "m" of "Female", so \bmale\b matches "Male Early Bird" and skips
# "Female Early Bird" -- which is the entire point of this watch.
MALE_RE = re.compile(r"\bmale\b", re.IGNORECASE)

SESSION = requests.Session()


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def log(message):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


_notifier = None


def _load_notifier():
    """The notify skill's sender if it is on this machine, else an env-driven
    equivalent. Returns None when neither is available."""
    if NOTIFY_PY.exists():
        try:
            spec = importlib.util.spec_from_file_location("notify_skill", NOTIFY_PY)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.notify
        except Exception as exc:
            log(f"could not load the notify skill ({exc}); trying PUSH_TOKEN")

    if not PUSH_TOKEN:
        return None

    def send(title, message, priority="high"):
        response = requests.post(
            PUSH_URL,
            headers={"Authorization": f"Bearer {PUSH_TOKEN}"},
            json={"title": title, "message": message, "priority": priority},
            timeout=TIMEOUT,
        )
        print(f"HTTP {response.status_code}: {response.text.strip()}")
        if response.status_code >= 400:
            return 1
        # The endpoint answers 200 whether it fanned the message out to a live
        # subscriber or found nobody at all; the difference is only in the
        # body. Reading it is the whole point.
        try:
            sent = response.json().get("sent")
        except Exception:
            sent = None
        return 3 if sent == 0 else 0

    return send


def push(title, message, enabled=True):
    """Send to the phone. Never let a push failure kill the watch loop."""
    global _notifier

    if not enabled:
        log(f"(push suppressed) {title}: {message}")
        return

    if _notifier is None:
        _notifier = _load_notifier()
    if _notifier is None:
        log(f"NO PUSH CHANNEL (set PUSH_TOKEN) -- {title}: {message}")
        return

    try:
        code = _notifier(title, message, priority="high")
    except Exception as exc:
        log(f"PUSH FAILED ({exc}) -- {title}: {message}")
        return

    if code != 0:
        # State is saved either way, so a change is reported once and once
        # only. If the phone was unsubscribed, the log line above is the
        # record of what you missed.
        log(f"PUSH NOT DELIVERED (exit {code}) -- {title}: {message}")


def squash(text):
    return re.sub(r"\s+", " ", text or "").strip()


def fetch(url):
    response = SESSION.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


# --------------------------------------------------------------------------
# source 1: islandetickets (server-rendered, no browser needed)
# --------------------------------------------------------------------------

ZEN_ID = "280566"
ZEN_URL = "https://islandetickets.com/event/TheZENCircus"
ZEN_TICKETS_URL = (
    "https://islandetickets.com/event_manager/public_events"
    f"/html_tickets/{ZEN_ID}/"
)


def parse_zen_tiers(html):
    """Pull the ticket tiers out of the fragment.

    Each tier is <li class="row ... price-holder price-52525"> holding a
    .price-name (with a padlock icon marking private/committee-only tiers), a
    .price-details carrying price and status, and zero or more trailing
    .secondary columns for notes like "Unisex".
    """
    soup = BeautifulSoup(html, "html.parser")
    tiers = {}

    for item in soup.select("ul.event-prices li.price-holder"):
        classes = item.get("class") or []
        tier_id = next(
            (c[len("price-"):] for c in classes if re.fullmatch(r"price-\d+", c)),
            None,
        )

        name_el = item.select_one(".price-name")
        detail_el = item.select_one(".price-details")

        # Notes sit in sibling columns of the tier, not inside .price-details,
        # so walk direct children only and skip the name/detail columns.
        notes = [
            squash(child.get_text())
            for child in item.find_all("div", recursive=False)
            if "secondary" in (child.get("class") or [])
        ]

        icon = item.select_one(".price-name i")
        icon_classes = (icon.get("class") or []) if icon else []

        name = squash(name_el.get_text()) if name_el else "(unnamed tier)"
        tiers[tier_id or name] = {
            "name": name,
            "detail": squash(detail_el.get_text()) if detail_el else "",
            "notes": [n for n in notes if n],
            # fa-lock (not fa-lock-open) means you have to request it from a
            # committee member -- that is what /request/280566 is for.
            "private": "fa-lock" in icon_classes,
        }

    return tiers


def parse_zen_event(html):
    """Title, organizer and the date/time/venue lines from the page header."""
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one(".event-header")
    if header is None:
        return {}

    title_el = header.select_one("h1")
    organizer_el = header.select_one(".small")

    lines = []
    for para in header.select("p"):
        text = squash(para.get_text())
        # The share row is a button, not content, and only renders on mobile.
        if text and text.lower() != "share":
            lines.append(text)

    return {
        "title": squash(title_el.get_text()) if title_el else "",
        "organizer": squash(organizer_el.get_text()) if organizer_el else "",
        "lines": lines,
    }


def snapshot_zen():
    # Deliberately no timestamp in here. The state files are committed by CI,
    # and a "last checked" field would make every single run a commit; without
    # one, `git diff --quiet state/` means exactly "a page changed".
    return {
        "event": parse_zen_event(fetch(ZEN_URL)),
        "tiers": parse_zen_tiers(fetch(ZEN_TICKETS_URL)),
    }


# --------------------------------------------------------------------------
# source 2: Jouvert Jumbeez (client-rendered, needs a real browser)
# --------------------------------------------------------------------------

JUMBEEZ_URL = "https://tickets.jouvertjumbeez.com/events/HvYHkQH1HbIYnr5Wp2ag"

# Prices render as "TTD 350.00". This doubles as the anchor for finding the
# ticket cards at all.
JUMBEEZ_PRICE_RE = re.compile(r"\bTTD\s*([\d,]+(?:\.\d{2})?)")


def parse_jumbeez_tiers(html):
    """Read the ticket cards out of the RENDERED page.

    The markup is Tailwind, so every class name here
    (group/relative/rounded-sm/...) is a build artefact that can churn on any
    deploy. Rather than depend on that, this finds each price and walks up to
    the largest ancestor still describing exactly ONE ticket -- the card
    boundary is defined by shape, which survives a restyle.
    """
    soup = BeautifulSoup(html, "html.parser")
    tiers = {}

    for node in soup.find_all(string=JUMBEEZ_PRICE_RE):
        card = node.parent
        while card is not None and card.parent is not None:
            parent_text = card.parent.get_text(" ", strip=True)
            if len(JUMBEEZ_PRICE_RE.findall(parent_text)) != 1:
                break
            card = card.parent

        text = squash(card.get_text(" ", strip=True))
        match = JUMBEEZ_PRICE_RE.search(text)
        if not match:
            continue

        name = squash(text[: match.start()])
        if not name:
            continue

        sold_out_now = "sold out" in text.lower()
        # Rebuilt rather than sliced so the quantity stepper ("Quantity 0")
        # never lands in the detail and masquerades as a change.
        detail = f"TTD {match.group(1)}" + (" Sold Out" if sold_out_now else "")

        # Keyed by name: these cards carry no stable id in the DOM, and the
        # names are what you would actually recognise in a notification.
        tiers[name] = {
            "name": name,
            "detail": detail,
            "notes": [],
            "private": False,
        }

    return tiers


def snapshot_jumbeez():
    # Imported lazily so `--source zen` still runs on a machine with no
    # browser installed.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            # NOT wait_until="networkidle": Firestore holds a live listener
            # open, so the network never goes idle and that wait always times
            # out. Waiting for a price to appear is the real readiness signal.
            page.goto(JUMBEEZ_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("text=TTD", timeout=60000)
            page.wait_for_timeout(1500)
            html = page.content()
            title = squash(page.title())
        finally:
            browser.close()

    # Only the title is tracked alongside the tiers. The date/venue block is
    # duplicated across responsive variants of the layout, so diffing it
    # produces noise rather than news.
    return {"event": {"title": title}, "tiers": parse_jumbeez_tiers(html)}


SOURCES = {
    "zen": {
        "label": "ZEN Circus",
        "url": ZEN_URL,
        "snapshot": snapshot_zen,
    },
    "wickedjab": {
        "label": "Wicked Jab",
        "url": JUMBEEZ_URL,
        "snapshot": snapshot_jumbeez,
    },
}


# --------------------------------------------------------------------------
# comparing
# --------------------------------------------------------------------------

def describe(tier):
    parts = [tier.get("name") or "(unnamed tier)"]
    if tier.get("detail"):
        parts.append(tier["detail"])
    if tier.get("notes"):
        parts.append(", ".join(tier["notes"]))
    if tier.get("private"):
        parts.append("private/committee")
    return " | ".join(parts)


def sold_out(tier):
    return "sold out" in (tier.get("detail") or "").lower()


def available_male_tiers(tiers):
    return {
        key for key, tier in (tiers or {}).items()
        if MALE_RE.search(tier.get("name") or "") and not sold_out(tier)
    }


def diff(old, new):
    """Human-readable change lines. Empty list means nothing moved."""
    changes = []

    old_event = old.get("event") or {}
    new_event = new.get("event") or {}

    for field in ("title", "organizer"):
        if old_event.get(field) != new_event.get(field):
            changes.append(
                f"{field.capitalize()}: '{old_event.get(field)}'"
                f" -> '{new_event.get(field)}'"
            )

    old_lines = old_event.get("lines") or []
    new_lines = new_event.get("lines") or []
    if old_lines != new_lines:
        for line in old_lines:
            if line not in new_lines:
                changes.append(f"Detail removed: {line}")
        for line in new_lines:
            if line not in old_lines:
                changes.append(f"Detail added: {line}")

    old_tiers = old.get("tiers") or {}
    new_tiers = new.get("tiers") or {}

    for key in sorted(set(new_tiers) - set(old_tiers)):
        changes.append(f"NEW TIER: {describe(new_tiers[key])}")
    for key in sorted(set(old_tiers) - set(new_tiers)):
        changes.append(f"TIER REMOVED: {describe(old_tiers[key])}")
    for key in sorted(set(old_tiers) & set(new_tiers)):
        before, after = old_tiers[key], new_tiers[key]
        if before != after:
            changes.append(f"CHANGED: {describe(before)} -> {describe(after)}")

    return changes


def headline(label, old, new):
    """Title for the lock screen -- lead with the thing worth waking up for."""
    old_tiers = old.get("tiers") or {}
    new_tiers = new.get("tiers") or {}

    # The whole reason the Wicked Jab watch exists.
    new_males = available_male_tiers(new_tiers) - available_male_tiers(old_tiers)
    if new_males:
        return f"MALE TICKETS: {', '.join(sorted(new_males))}"

    if set(new_tiers) - set(old_tiers):
        return f"{label}: new ticket tier"

    for key in set(old_tiers) & set(new_tiers):
        if sold_out(old_tiers[key]) and not sold_out(new_tiers[key]):
            return f"{label}: tickets on sale"

    return f"{label}: page changed"


def summary(state):
    tiers = state.get("tiers") or {}
    if not tiers:
        return "no tiers listed"
    return "; ".join(describe(tier) for tier in tiers.values())


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def state_path(key):
    return STATE_DIR / f"{key}.json"


def load_state(key):
    path = state_path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"[{key}] state file unreadable ({exc}); re-baselining")
        return None


def save_state(key, state):
    STATE_DIR.mkdir(exist_ok=True)
    state_path(key).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

def check_source(key, allow_push=True):
    """One poll of one source. True if a change was found and reported."""
    source = SOURCES[key]
    current = source["snapshot"]()

    if not current.get("tiers"):
        # A page that renders no tiers at all is far more likely to be a
        # failed render or a site outage than an organizer deleting every
        # tier. Raising means it counts as a failure instead of wiping the
        # baseline and reporting a bogus "TIER REMOVED" for everything.
        raise RuntimeError("no tiers found on the page")

    previous = load_state(key)
    if previous is None:
        save_state(key, current)
        log(f"[{key}] baseline saved -- {summary(current)}")
        return False

    changes = diff(previous, current)
    save_state(key, current)

    if not changes:
        log(f"[{key}] no change ({len(current['tiers'])} tier(s))")
        return False

    log(f"[{key}] CHANGE DETECTED:\n  " + "\n  ".join(changes))

    body = " / ".join(changes)
    if len(body) > 300:
        body = body[:297] + "..."
    push(
        headline(source["label"], previous, current),
        f"{body} -- {source['url']}",
        enabled=allow_push,
    )
    return True


def check_all(keys, allow_push=True, failures=None):
    """Poll every source. One source failing must not stop the others."""
    failures = {} if failures is None else failures

    for key in keys:
        try:
            check_source(key, allow_push=allow_push)
            if failures.get(key, 0) >= FAILURES_BEFORE_ALERT:
                push(
                    f"{SOURCES[key]['label']} watch recovered",
                    f"Back in contact after {failures[key]} failed checks.",
                    enabled=allow_push,
                )
            failures[key] = 0
        except Exception as exc:
            failures[key] = failures.get(key, 0) + 1
            log(f"[{key}] check failed ({failures[key]}): {exc}")
            if failures[key] == FAILURES_BEFORE_ALERT:
                push(
                    f"{SOURCES[key]['label']} watch is blind",
                    f"{failures[key]} failed checks in a row. Last error: {exc}",
                    enabled=allow_push,
                )

    return failures


def watch(keys, interval_seconds, allow_push=True):
    log(f"watching {', '.join(keys)} every {interval_seconds // 60} min "
        "(Ctrl+C to stop)")
    failures = {}
    while True:
        failures = check_all(keys, allow_push=allow_push, failures=failures)
        # A little jitter so the requests do not land on a metronome.
        time.sleep(interval_seconds + random.randint(0, 60))


def main():
    parser = argparse.ArgumentParser(
        description="Watch ticket pages for new tiers and price changes.",
    )
    parser.add_argument("--once", action="store_true",
                        help="check once and exit (for CI)")
    parser.add_argument("--source", choices=sorted(SOURCES), action="append",
                        help="only check this source (repeatable)")
    parser.add_argument("--interval", type=int, default=20,
                        help="minutes between checks (default: 20)")
    parser.add_argument("--no-push", action="store_true",
                        help="log changes but send nothing to the phone")
    parser.add_argument("--reset", action="store_true",
                        help="delete the saved baselines and re-seed them")
    parser.add_argument("--test-notify", action="store_true",
                        help="send one test push and exit")
    args = parser.parse_args()

    if args.test_notify:
        push("Ticket watch test", "Watching ZEN Circus and Wicked Jab.")
        return 0

    keys = args.source or sorted(SOURCES)

    if args.reset:
        for key in keys:
            if state_path(key).exists():
                state_path(key).unlink()
                log(f"[{key}] baseline cleared")

    allow_push = not args.no_push

    if args.once:
        failures = check_all(keys, allow_push=allow_push)
        return 1 if all(failures.get(k) for k in keys) else 0

    try:
        watch(keys, args.interval * 60, allow_push=allow_push)
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
