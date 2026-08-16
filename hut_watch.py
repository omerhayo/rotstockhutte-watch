#!/usr/bin/env python3
"""
Rotstockhuette availability watcher (hut-reservation.org, hutId=69).

Uses the site's public availability endpoint:
    https://www.hut-reservation.org/api/v1/reservation/getHutAvailability?hutId=69

which returns one record per day, shaped like:
    {"freeBedsPerCategory": {"101": 2, "911": 0},
     "freeBeds": 2,
     "hutStatus": "SERVICED",
     "date": "2026-08-16T00:00:00Z",
     "dateFormatted": "16.08.2026",
     "totalSleepingPlaces": 43,
     "percentage": "NEARLY FULL"}

No browser needed. Just: pip install requests

Usage
-----
    python hut_watch.py --list          # show the whole season, pick your dates
    python hut_watch.py --once          # single check, print result, exit
    python hut_watch.py                 # watch forever, notify on availability
    python hut_watch.py --test-notify   # verify WhatsApp / email are wired up
"""

from __future__ import annotations

import argparse
import json
import os
import random
import smtplib
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests

# =============================================================================
# CONFIG -- edit this block
# =============================================================================

HUT_ID = 69
HUT_NAME = "Rotstockhütte"

# The night(s) you want, as arrival dates. Format YYYY-MM-DD.
TARGET_DATES = [
    "2026-09-18"
]

# Only alert when at least this many beds are free.
MIN_BEDS = 2

# Minutes between checks. Please keep this at 10 or above.
CHECK_EVERY_MIN = 15
JITTER_MIN = 5

# Don't re-alert about the same date more often than this, unless more beds appear.
RENOTIFY_AFTER_HOURS = 12

# ---- Notifications (configure at least one) --------------------------------

# WhatsApp via CallMeBot. Setup:
#   1. Save +34 644 51 95 23 as a contact.
#   2. WhatsApp it exactly: I allow callmebot to send me messages
#   3. It replies with your API key.
CALLMEBOT_PHONE = os.getenv("CALLMEBOT_PHONE", "")     # "972501234567" (no +, no spaces)
CALLMEBOT_APIKEY = os.getenv("CALLMEBOT_APIKEY", "")

# Telegram (@BotFather -> /newbot). More reliable than CallMeBot.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# Email. Gmail requires an App Password, not your normal password.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")

# =============================================================================

API_URL = "https://www.hut-reservation.org/api/v1/reservation/getHutAvailability"
BOOKING_URL = f"https://www.hut-reservation.org/reservation/book-hut/{HUT_ID}/wizard"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": BOOKING_URL,
}


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"notified": {}}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log(f"! could not save state: {exc}")


# -----------------------------------------------------------------------------
# Fetching
# -----------------------------------------------------------------------------

def fetch_availability() -> dict[str, dict]:
    """Return {'YYYY-MM-DD': record} for every day the API knows about."""
    r = requests.get(API_URL, params={"hutId": HUT_ID}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()

    days: dict[str, dict] = {}
    for rec in data:
        if not isinstance(rec, dict):
            continue
        raw = rec.get("date") or ""
        day = str(raw)[:10]
        if len(day) == 10 and day[4] == "-":
            days[day] = rec
    return days


def describe(rec: dict) -> str:
    beds = rec.get("freeBeds")
    total = rec.get("totalSleepingPlaces")
    status = rec.get("hutStatus", "?")
    pct = rec.get("percentage", "")
    bits = f"{beds}/{total} free  [{status}"
    if pct:
        bits += f" / {pct}"
    return bits + "]"


# -----------------------------------------------------------------------------
# Notifications
# -----------------------------------------------------------------------------

def send_whatsapp(text: str) -> bool:
    if not (CALLMEBOT_PHONE and CALLMEBOT_APIKEY):
        return False
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": CALLMEBOT_PHONE, "text": text, "apikey": CALLMEBOT_APIKEY},
            timeout=30,
        )
        if r.ok:
            log("  -> WhatsApp sent")
            return True
        log(f"  ! WhatsApp failed ({r.status_code}): {r.text[:200]}")
    except Exception as exc:
        log(f"  ! WhatsApp error: {exc}")
    return False


def send_telegram(text: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=30,
        )
        if r.ok:
            log("  -> Telegram sent")
            return True
        log(f"  ! Telegram failed ({r.status_code}): {r.text[:200]}")
    except Exception as exc:
        log(f"  ! Telegram error: {exc}")
    return False


def send_email(subject: str, body: str) -> bool:
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_TO
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        log("  -> Email sent")
        return True
    except Exception as exc:
        log(f"  ! Email error: {exc}")
    return False


def notify(subject: str, body: str) -> None:
    sent = False
    if send_whatsapp(f"{subject}\n\n{body}"):
        sent = True
    if send_telegram(f"{subject}\n\n{body}"):
        sent = True
    if send_email(subject, body):
        sent = True
    if not sent:
        log("  ! No notification channel configured. Alert follows:")
        log(f"  ! {subject} | {body}")


# -----------------------------------------------------------------------------
# Checking
# -----------------------------------------------------------------------------

def check(state: dict, targets: list[str]) -> None:
    days = fetch_availability()
    if not days:
        log("  ! API returned no day records")
        return

    log(f"  {len(days)} days known ({min(days)} .. {max(days)})")

    hits = []
    for d in targets:
        rec = days.get(d)
        if rec is None:
            log(f"  {d}: not in the published calendar")
            continue
        beds = rec.get("freeBeds") or 0
        log(f"  {d}: {describe(rec)}")
        if beds >= MIN_BEDS:
            hits.append((d, beds, rec))

    if not hits:
        return

    now = time.time()
    notified = state.setdefault("notified", {})
    fresh = []
    for d, beds, rec in hits:
        prev = notified.get(d)
        if prev:
            stale = (now - prev.get("ts", 0)) > RENOTIFY_AFTER_HOURS * 3600
            better = beds > prev.get("beds", 0)
            if not (stale or better):
                continue
        fresh.append((d, beds, rec))
        notified[d] = {"ts": now, "beds": beds}

    if not fresh:
        log("  (already alerted on these — staying quiet)")
        return

    save_state(state)

    lines = []
    for d, beds, rec in fresh:
        cats = rec.get("freeBedsPerCategory") or {}
        cat_txt = ", ".join(f"cat {k}: {v}" for k, v in cats.items() if v)
        line = f"{d}  —  {beds} bed(s) free"
        if cat_txt:
            line += f"  ({cat_txt})"
        lines.append(line)

    subject = f"{HUT_NAME}: space on " + ", ".join(d for d, _, _ in fresh)
    body = "\n".join(lines) + f"\n\nBook: {BOOKING_URL}"
    log("  *** AVAILABILITY FOUND ***")
    notify(subject, body)


def show_calendar(days_ahead: int) -> None:
    days = fetch_availability()
    today = datetime.now().date()
    limit = today + timedelta(days=days_ahead)
    print(f"\n{HUT_NAME} — next {days_ahead} days\n")
    for d in sorted(days):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (today <= dt <= limit):
            continue
        rec = days[d]
        beds = rec.get("freeBeds") or 0
        mark = "OPEN " if beds > 0 else "  -  "
        print(f"  {mark} {d} {dt:%a}  {describe(rec)}")
    print()


# -----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=f"Watch {HUT_NAME} for free beds.")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--list", type=int, nargs="?", const=120, metavar="DAYS",
                    help="print availability for the next N days (default 120)")
    ap.add_argument("--dates", nargs="*", metavar="YYYY-MM-DD",
                    help="override the target dates for this run")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    if args.test_notify:
        notify(f"{HUT_NAME} watcher test", "Notifications are working.")
        return

    if args.list:
        show_calendar(args.list)
        return

    targets = args.dates or TARGET_DATES
    state = load_state()

    log(f"Watching {HUT_NAME} (hut {HUT_ID}) for: {', '.join(targets)}")
    log(f"Alerting at >= {MIN_BEDS} free bed(s).")

    while True:
        try:
            check(state, targets)
        except requests.RequestException as exc:
            log(f"! network problem: {exc}")
        except Exception as exc:
            log(f"! {type(exc).__name__}: {exc}")

        if args.once:
            return

        wait = CHECK_EVERY_MIN + random.uniform(0, JITTER_MIN)
        log(f"  next check {datetime.now() + timedelta(minutes=wait):%H:%M:%S}\n")
        try:
            time.sleep(wait * 60)
        except KeyboardInterrupt:
            log("Stopped.")
            return


if __name__ == "__main__":
    main()