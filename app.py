"""Watch an Island eTickets event and push to Peter's phone when it changes.

Aimed at THE ZEN CIRCUS (ZEN Halloween), 31 Oct 2026, whose only tier is
"PRE SALE TICKETS -- $150.00 TTD Sold Out". The point is to catch the moment a
second tier appears, or that first one comes off Sold Out.

Two things make this cheap to poll:

  * The event page ships an EMPTY <div class="tickets-container">. Every tier,
    price and sold-out badge is pulled in afterwards from a separate fragment
    endpoint (see TICKETS_URL). Hitting that directly means no browser, no JS.
  * That fragment is byte-identical between requests -- no CSRF token, no
    nonce, no timestamp -- so anything that differs is a real change.

Even so this compares PARSED tiers rather than a hash of the markup, because a
hash can tell you something moved but not what, and the notification has to be
readable off a lock screen.

This runs in two places and has to behave in both:

  * Locally, `py app.py` loops forever and pushes through the notify skill.
  * In GitHub Actions, `py app.py --once` runs on a cron and pushes through
    PUSH_TOKEN (a repo secret). state.json is committed back to the repo, so
    the git history of that one file IS the history of the ticket page.

Usage:
    py app.py                  # check now, then every 20 minutes
    py app.py --once           # single check (for CI / Task Scheduler)
    py app.py --once --no-push # ...without sending anything to the phone
    py app.py --reset          # forget the baseline and start over
    py app.py --test-notify    # prove the push channel still works
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

EVENT_ID = "280566"
EVENT_SLUG = "TheZENCircus"

EVENT_URL = f"https://islandetickets.com/event/{EVENT_SLUG}"
TICKETS_URL = (
    "https://islandetickets.com/event_manager/public_events"
    f"/html_tickets/{EVENT_ID}/"
)

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
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


def fetch(session, url):
    response = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


# --------------------------------------------------------------------------
# scraping
# --------------------------------------------------------------------------

def parse_tiers(html):
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


def parse_event(html):
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


def snapshot(session):
    # Deliberately no timestamp in here. state.json is committed by CI, and a
    # "last checked" field would make every single run a commit; without one,
    # `git diff --quiet state.json` means exactly "the page changed".
    return {
        "event": parse_event(fetch(session, EVENT_URL)),
        "tiers": parse_tiers(fetch(session, TICKETS_URL)),
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


def headline(old, new):
    """Title for the lock screen -- lead with the thing worth waking up for."""
    old_tiers = old.get("tiers") or {}
    new_tiers = new.get("tiers") or {}

    if set(new_tiers) - set(old_tiers):
        return "New ticket tier"

    for key in set(old_tiers) & set(new_tiers):
        if sold_out(old_tiers[key]) and not sold_out(new_tiers[key]):
            return "Tickets on sale"

    return "ZEN Circus page changed"


def summary(state):
    tiers = state.get("tiers") or {}
    if not tiers:
        return "no tiers listed"
    return "; ".join(describe(tier) for tier in tiers.values())


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"state file unreadable ({exc}); treating this run as a new baseline")
        return None


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

def check(session, allow_push=True):
    """One poll. True if a change was found and reported."""
    current = snapshot(session)

    if not current["tiers"]:
        # Not fatal on its own: an organizer can pull every tier down between
        # sales. Worth saying out loud rather than silently comparing {} to {}.
        log("warning: the fragment listed no tiers at all")

    previous = load_state()
    if previous is None:
        save_state(current)
        log(f"baseline saved -- {summary(current)}")
        return False

    changes = diff(previous, current)
    save_state(current)

    if not changes:
        log(f"no change ({len(current['tiers'])} tier(s))")
        return False

    log("CHANGE DETECTED:\n  " + "\n  ".join(changes))

    body = " / ".join(changes)
    if len(body) > 300:
        body = body[:297] + "..."
    push(headline(previous, current), f"{body} -- {EVENT_URL}", enabled=allow_push)
    return True


def watch(interval_seconds, allow_push=True):
    log(f"watching {EVENT_URL} every {interval_seconds // 60} min (Ctrl+C to stop)")
    failures = 0
    alerted_about_failures = False

    while True:
        try:
            check(SESSION, allow_push=allow_push)
            if failures and alerted_about_failures:
                push(
                    "Ticket watch recovered",
                    f"Back in contact with islandetickets.com after {failures} "
                    "failed checks.",
                    enabled=allow_push,
                )
            failures = 0
            alerted_about_failures = False
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            failures += 1
            log(f"check failed ({failures}): {exc}")
            if failures >= FAILURES_BEFORE_ALERT and not alerted_about_failures:
                push(
                    "Ticket watch is blind",
                    f"{failures} failed checks in a row on the ZEN Circus page. "
                    f"Last error: {exc}",
                    enabled=allow_push,
                )
                alerted_about_failures = True

        # A little jitter so the requests do not land on a metronome.
        time.sleep(interval_seconds + random.randint(0, 60))


def main():
    parser = argparse.ArgumentParser(
        description="Watch the ZEN Circus ticket page for changes.",
    )
    parser.add_argument("--once", action="store_true",
                        help="check once and exit (for Task Scheduler)")
    parser.add_argument("--interval", type=int, default=20,
                        help="minutes between checks (default: 20)")
    parser.add_argument("--no-push", action="store_true",
                        help="log changes but send nothing to the phone")
    parser.add_argument("--reset", action="store_true",
                        help="delete the saved baseline and re-seed it")
    parser.add_argument("--test-notify", action="store_true",
                        help="send one test push and exit")
    args = parser.parse_args()

    if args.test_notify:
        push("Ticket watch test", f"Watching {EVENT_SLUG} for new tiers.")
        return 0

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        log("baseline cleared")

    allow_push = not args.no_push

    if args.once:
        try:
            check(SESSION, allow_push=allow_push)
        except Exception as exc:
            log(f"check failed: {exc}")
            return 1
        return 0

    try:
        watch(args.interval * 60, allow_push=allow_push)
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
