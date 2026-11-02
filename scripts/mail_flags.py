#!/usr/bin/env python3
"""Hourly Yahoo check that publishes counts only, never mail content (this repo is public).

Writes mail_flags.json: how many raffle-related messages arrived in Inbox or Spam since the
ticket confirmation. The Morning Brief reads it so the raffle check needs no browser.
"""
import imaplib
import json
import os
import sys
from datetime import datetime, timezone
from email import message_from_bytes
from email.utils import parsedate_to_datetime

HOST = "imap.mail.yahoo.com"
SINCE = "21-Oct-2026"
QUERIES = ('FROM "laddinc"', 'FROM "zeffy"', 'SUBJECT "raffle"', 'SUBJECT "LADD"')


def main():
    user, pw = os.environ.get("YAHOO_EMAIL"), os.environ.get("YAHOO_APP_PASSWORD")
    if not user or not pw:
        print("no Yahoo credentials; skipping")
        return
    M = imaplib.IMAP4_SSL(HOST, 993)
    M.login(user, pw)
    count, latest = 0, ""
    for box in ("INBOX", "Bulk"):
        typ, _ = M.select(f'"{box}"', readonly=True)
        if typ != "OK":
            print(f"cannot open {box}", file=sys.stderr)
            continue
        ids = set()
        for q in QUERIES:
            typ, data = M.search(None, f"(SINCE {SINCE} {q})")
            if typ == "OK" and data and data[0]:
                ids |= set(data[0].split())
        for i in ids:
            typ, data = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (DATE)])")
            if typ != "OK":
                continue
            count += 1
            try:
                d = parsedate_to_datetime(message_from_bytes(data[0][1])["Date"]).strftime("%Y-%m-%d")
                latest = max(latest, d)
            except (TypeError, ValueError, IndexError):
                pass
    M.logout()
    with open("mail_flags.json", "w") as f:
        json.dump({"checked": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   "raffle_messages": count, "latest": latest}, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main()
