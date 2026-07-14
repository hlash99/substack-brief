#!/usr/bin/env python3
"""
Substack rolling brief — pulls the latest newsletters from the Yahoo Mail
"Substack" folder over IMAP, summarizes each into 2-3 bullets with an LLM
(same free-tier provider chain as ctcl-papers), synthesizes a cross-newsletter
rolling brief, and writes data.json for the hlash99.github.io/substack-brief page.

Runs on GitHub Actions (cron, 2x daily). Secrets:
  YAHOO_EMAIL / YAHOO_APP_PASSWORD  — Yahoo IMAP login (app password, not the
                                      account password: Yahoo Account Security →
                                      "Generate app password" → Other App)
  GROQ_API_KEY (or OPENROUTER_API_KEY / CEREBRAS_API_KEY / ANTHROPIC_API_KEY)

Without an IMAP password the run exits 0 untouched (workflow stays green until
secrets are added). Without an LLM key, items get an extractive excerpt and are
re-summarized automatically on the first run that has a key.

Bootstrap/local mode: --from-dir DIR reads emails extracted from Apple Mail as
.txt files (Subject:/From:/MessageID:/Date: header block, ======== separator, body).
"""
import argparse
import email
import email.policy
import email.utils
import html
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"
TZ = ZoneInfo("America/Los_Angeles")

IMAP_HOST = "imap.mail.yahoo.com"
FOLDER = os.environ.get("SUBSTACK_FOLDER", "Substack")
FETCH_DAYS = 9        # how far back each IMAP run looks (cache covers the rest)
WINDOW_DAYS = 7       # rolling window shown on the page
KEEP_DAYS = 30        # summaries kept in data.json so nothing is re-summarized
BRIEF_DAYS = 3        # the top rolling brief synthesizes this many days
SKIP_SENDERS = {"no-reply@substack.com"}   # platform digests/promos, not newsletters

# ---------------------------------------------------------------- LLM chain --
# Mirrors ctcl-papers scripts/fetch_papers.py: within a provider, models are
# tried in order (Groq retires model ids); across providers, the next one with
# a key set picks up an outage. All free-tier, OpenAI-compatible.
import requests

OPENLLM_PROVIDERS = [
    {"name": "groq", "key": "GROQ_API_KEY",
     "url": "https://api.groq.com/openai/v1/chat/completions",
     "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"],
     "headers": {}},
    {"name": "openrouter", "key": "OPENROUTER_API_KEY",
     "url": "https://openrouter.ai/api/v1/chat/completions",
     "models": ["meta-llama/llama-3.3-70b-instruct:free",
                "qwen/qwen-2.5-72b-instruct:free",
                "meta-llama/llama-3.1-8b-instruct:free"],
     "headers": {"HTTP-Referer": "https://hlash99.github.io/substack-brief/",
                 "X-Title": "Substack brief"}},
    {"name": "cerebras", "key": "CEREBRAS_API_KEY",
     "url": "https://api.cerebras.ai/v1/chat/completions",
     "models": ["llama-3.3-70b", "llama3.1-8b"], "headers": {}},
]


def model_body(model):
    m = model.lower()
    if "gpt-oss" in m:
        return {"reasoning_effort": "low"}
    if "qwen3" in m or "qwen-3" in m:
        return {"reasoning_format": "hidden"}
    return {}


class RateLimited(RuntimeError):
    def __init__(self, msg, retry_after):
        super().__init__(msg)
        self.retry_after = retry_after


def strip_reasoning(text):
    if not text:
        return text
    t = text
    if re.search(r"<think>", t, flags=re.I) and not re.search(r"</think>", t, flags=re.I):
        t = re.split(r"<think>", t, flags=re.I)[0]
    if re.search(r"</think>", t, flags=re.I):
        t = re.split(r"</think>", t, flags=re.I)[-1]
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S | re.I)
    t = re.sub(r"</?think>", "", t, flags=re.I)
    m = re.search(r"(?:assistantfinal|<\|channel\|>\s*final[^>]*<\|message\|>)\s*", t, flags=re.I)
    if m:
        t = t[m.end():]
    t = re.sub(r"<\|[^|]*\|>", "", t)
    return t.strip()


def _oai_post(url, key, model, system, user, max_tokens, extra, body=None):
    h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    h.update(extra or {})
    payload = {"model": model, "max_tokens": max_tokens, "temperature": 0.3,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    payload.update(body or {})
    r = requests.post(url, headers=h, timeout=90, json=payload)
    if r.status_code == 429:
        wait = r.headers.get("retry-after")
        try:
            wait = float(wait)
        except (TypeError, ValueError):
            m = re.search(r"in ([\d.]+)s", r.text)
            wait = float(m.group(1)) if m else 5.0
        raise RateLimited(f"{model}@{url.split('/')[2]} -> 429", wait)
    if r.status_code != 200:
        raise RuntimeError(f"{model}@{url.split('/')[2]} -> {r.status_code} {r.text[:160]}")
    return strip_reasoning((r.json()["choices"][0]["message"]["content"] or "").strip())


LAST_PROVIDER = None


def make_llm():
    """Return complete(system, user, max_tokens) or None if no key is set."""
    global LAST_PROVIDER
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic()

            def complete(system, user, max_tokens):
                global LAST_PROVIDER
                m = client.messages.create(model="claude-haiku-4-5-20251001",
                                           max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": user}])
                LAST_PROVIDER = "claude"
                return "".join(b.text for b in m.content if b.type == "text").strip()
            return complete
        except ImportError:
            print("anthropic SDK not installed — trying open-LLM providers", file=sys.stderr)

    cands = []
    for prov in OPENLLM_PROVIDERS:
        key = os.environ.get(prov["key"])
        if not key:
            continue
        for m in prov["models"]:
            cands.append((prov["name"], prov["url"], m, prov.get("headers") or {},
                          model_body(m), key))
    if not cands:
        return None
    state = {"i": 0}

    def complete(system, user, max_tokens):
        global LAST_PROVIDER
        n = len(cands)
        for attempt in range(6):
            errs, waits = [], []
            for off in range(n):
                idx = (state["i"] + off) % n
                name, url, model, extra, body, key = cands[idx]
                try:
                    out = _oai_post(url, key, model, system, user, max_tokens, extra, body)
                    if out:
                        state["i"], LAST_PROVIDER = idx, name
                        return out
                    errs.append(f"{name}/{model}: empty response")
                except RateLimited as e:
                    waits.append(e.retry_after)
                    errs.append(str(e))
                except Exception as e:
                    errs.append(str(e))
            if waits and len(waits) == n and attempt < 5:
                time.sleep(min(max(waits), 30) + 0.5)
                continue
            break
        raise RuntimeError("all open-LLM candidates failed — " + " | ".join(errs[:4]))
    return complete


ITEM_SYSTEM = (
    "You summarize one newsletter email (political / economic / cultural commentary "
    "Substacks) for a busy reader's morning brief. Given the newsletter, subject and "
    "full text, reply with ONLY a JSON object {\"bullets\": [...]} of 2-3 bullets. "
    "Each bullet is one concrete sentence (max ~28 words) capturing the actual "
    "argument, facts, names and numbers — never meta like 'the author discusses'. "
    "If the email is a podcast/video announcement or paywalled teaser, one bullet "
    "saying what it covers is enough. No markdown, no text outside the JSON."
)

BRIEF_SYSTEM = (
    "You write the rolling 'top brief' for a personal dashboard, synthesizing the "
    "last few days of Substack newsletters the reader subscribes to (The Bulwark, "
    "Zeteo, Paul Krugman, Doomberg, The Message Box, and others). Given recent items "
    "(newsletter, date, subject, bullets), reply with ONLY a JSON array of AT MOST 6 "
    "strings. Each is one plain-English sentence on a cross-cutting theme or the most "
    "consequential story — group overlapping coverage rather than listing emails, and "
    "note the newsletter(s) in parentheses. Lead with what matters most. No markdown."
)


def parse_json_loose(text, want):
    """Parse the model's JSON reply; tolerate stray prose around it."""
    try:
        v = json.loads(text)
    except Exception:
        m = re.search(r"\[.*\]" if want is list else r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            v = json.loads(m.group(0))
        except Exception:
            return None
    if want is list and isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if want is dict and isinstance(v, dict):
        return v
    return None


# ------------------------------------------------------------- text extract --
class TextExtractor(HTMLParser):
    """HTML → readable text; skips style/script; block elements become newlines."""
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.parts.append(d)

    def text(self):
        t = "".join(self.parts)
        t = re.sub(r"[ \t]+", " ", t)
        return re.sub(r"\n\s*\n+", "\n\n", t).strip()


BOILER = re.compile(
    r"^(forwarded this email|view (this post|in browser)|read in app|unsubscribe|"
    r"listen (now|on)|watch now|share( this post)?|like|comment|restack|"
    r"upgrade to (paid|a paid)|subscribe (here|now)|get the app|start writing|"
    r"©|\d+ (like|comment|restack)s?|preview|open in app|a guest post by)", re.I)


def clean_lines(text):
    out = []
    for ln in text.splitlines():
        ln = ln.replace("￼", "").strip()          # object-replacement chars from images
        if not ln or BOILER.match(ln):
            continue
        out.append(ln)
    return "\n".join(out)


def excerpt_bullets(text, subject):
    """No-LLM fallback: first substantial sentences as a placeholder excerpt.
    summary_by='excerpt' marks these for re-summarization once a key exists."""
    body = clean_lines(text)
    sents, cur = [], ""
    for ln in body.splitlines():
        if len(ln) < 40 or ln == subject:
            continue
        cur = (cur + " " + ln).strip()
        if len(cur) > 180:
            sents.append(cur[:240] + ("…" if len(cur) > 240 else ""))
            cur = ""
        if len(sents) >= 2:
            break
    if cur and len(sents) < 2:
        sents.append(cur[:240])
    return sents or [subject]


SUBSTACK_URL = re.compile(
    r"https?://(?:open\.substack\.com/pub/[\w.-]+/p/[\w.-]+|[\w.-]+\.substack\.com/p/[\w.-]+)")


def post_url(raw_text):
    m = SUBSTACK_URL.search(raw_text or "")
    return m.group(0) if m else None


# ------------------------------------------------------------------ sources --
def norm_mid(mid):
    return (mid or "").strip().strip("<>").strip()


def pub_key(addr):
    """thebulwark+thetriad@substack.com → 'thebulwark' (publication grouping key)."""
    local = (addr or "").split("@")[0]
    return local.split("+")[0].lower() or "unknown"


def from_imap():
    user = os.environ.get("YAHOO_EMAIL", "")
    pw = os.environ.get("YAHOO_APP_PASSWORD", "")
    if not (user and pw):
        return None    # caller exits 0 — secrets not configured yet
    M = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    M.login(user, pw)
    typ, _ = M.select(f'"{FOLDER}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"cannot select folder {FOLDER!r}")
    since = (datetime.now(timezone.utc) - timedelta(days=FETCH_DAYS)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f"(SINCE {since})")
    ids = data[0].split() if typ == "OK" else []
    items = []
    for num in ids:
        typ, msg_data = M.fetch(num, "(BODY.PEEK[])")   # PEEK: don't mark as read
        if typ != "OK" or not msg_data or msg_data[0] is None:
            continue
        msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
        name, addr = email.utils.parseaddr(msg.get("From", ""))
        if addr.lower() in SKIP_SENDERS:
            continue
        try:
            dt = email.utils.parsedate_to_datetime(msg.get("Date"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        plain, htm = None, None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and plain is None:
                plain = part.get_content()
            elif ct == "text/html" and htm is None:
                htm = part.get_content()
        text, raw_for_url = plain, plain or ""
        if htm:
            ex = TextExtractor()
            ex.feed(htm)
            if not text or len(text) < 200:
                text = ex.text()
            raw_for_url = (plain or "") + " " + htm
        items.append({
            "id": norm_mid(msg.get("Message-ID")),
            "newsletter": name or addr,
            "pub": pub_key(addr),
            "subject": (msg.get("Subject") or "").strip(),
            "date": dt.astimezone(TZ).isoformat(timespec="minutes"),
            "url": post_url(raw_for_url),
            "_text": text or "",
        })
    M.logout()
    return items


def from_dir(d):
    """Bootstrap: .txt files written by the Apple Mail extractor."""
    items = []
    for p in sorted(Path(d).glob("*.txt")):
        raw = p.read_text(encoding="utf-8", errors="replace")
        head, _, body = raw.partition("\n========\n")
        h = dict(re.findall(r"^(\w+): (.*)$", head, re.M))
        name, addr = email.utils.parseaddr(h.get("From", ""))
        if addr.lower() in SKIP_SENDERS:
            continue
        items.append({
            "id": norm_mid(h.get("MessageID")),
            "newsletter": name or addr,
            "pub": pub_key(addr),
            "subject": h.get("Subject", "").strip(),
            "date": h.get("Date", ""),
            "url": post_url(body),
            "_text": body,
        })
    return items


# --------------------------------------------------------------------- main --
def item_date(it):
    try:
        d = datetime.fromisoformat(it.get("date", ""))
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    except Exception:
        return datetime.now(TZ)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-dir", help="bootstrap from extracted .txt emails instead of IMAP")
    args = ap.parse_args()

    prev = {}
    if DATA.exists():
        prev = json.loads(DATA.read_text(encoding="utf-8"))
    known = {it["id"]: it for it in prev.get("items", []) if it.get("id")}

    incoming = from_dir(args.from_dir) if args.from_dir else from_imap()
    if incoming is None:
        print("YAHOO_EMAIL/YAHOO_APP_PASSWORD not set — skipping (add repo secrets to go live).")
        return
    print(f"fetched {len(incoming)} emails from {'dir' if args.from_dir else 'IMAP'}")

    llm = make_llm()
    if not llm:
        print("no LLM key set — new items get extractive excerpts (re-summarized once a key exists)")

    now = datetime.now(TZ)
    items, n_sum = [], 0
    for inc in incoming:
        old = known.get(inc["id"])
        # keep an existing real summary; re-do excerpts when an LLM is available
        if old and old.get("summary_by") not in (None, "", "excerpt"):
            old.update({k: inc[k] for k in ("newsletter", "pub", "subject", "date") })
            if inc.get("url") and not old.get("url"):
                old["url"] = inc["url"]
            items.append(old)
            continue
        it = {k: v for k, v in inc.items() if k != "_text"}
        text = clean_lines(inc.get("_text", ""))[:9000]
        done = False
        if llm and text:
            try:
                out = llm(ITEM_SYSTEM,
                          f"Newsletter: {it['newsletter']}\nSubject: {it['subject']}\n\n{text}",
                          400)
                v = parse_json_loose(out, dict)
                if v and isinstance(v.get("bullets"), list) and v["bullets"]:
                    it["bullets"] = [str(b).strip() for b in v["bullets"][:3]]
                    it["summary_by"] = LAST_PROVIDER
                    n_sum += 1
                    done = True
                    time.sleep(2)     # free-tier pacing
            except Exception as e:
                print(f"summarize failed ({it['subject'][:40]}…): {e}", file=sys.stderr)
        if not done:
            if old:                    # keep the old excerpt rather than redoing it
                items.append(old)
                continue
            it["bullets"] = excerpt_bullets(inc.get("_text", ""), it["subject"])
            it["summary_by"] = "excerpt"
        items.append(it)

    # carry cached items the fetch window no longer covers; drop past KEEP_DAYS
    seen = {it["id"] for it in items}
    for mid, old in known.items():
        if mid not in seen and (now - item_date(old)).days <= KEEP_DAYS:
            items.append(old)
    items.sort(key=item_date, reverse=True)
    print(f"{len(items)} items in store · {n_sum} newly summarized")

    # rolling top brief over the last BRIEF_DAYS
    recent = [it for it in items if (now - item_date(it)).days < BRIEF_DAYS
              and it.get("summary_by") != "excerpt"]
    brief = prev.get("brief") or {}
    new_ids = sorted(it["id"] for it in recent)
    if recent and llm and (n_sum or brief.get("ids") != new_ids or not brief.get("bullets")):
        lines = [f"- [{it['newsletter']}] {it['date'][:10]} — {it['subject']}: "
                 + " ".join(it.get("bullets", []))[:400] for it in recent]
        try:
            out = llm(BRIEF_SYSTEM, "\n".join(lines), 600)
            bl = parse_json_loose(out, list)
            if bl:
                brief = {"bullets": bl[:6], "by": LAST_PROVIDER, "ids": new_ids,
                         "updated": now.isoformat(timespec="minutes"),
                         "span_days": BRIEF_DAYS}
        except Exception as e:
            print(f"brief synthesis failed: {e}", file=sys.stderr)

    shown = [it for it in items if (now - item_date(it)).days < WINDOW_DAYS]
    out = {
        "updated": now.isoformat(timespec="minutes"),
        "window_days": WINDOW_DAYS,
        "source": f"Yahoo Mail '{FOLDER}' folder via IMAP; summarized server-side",
        "brief": brief,
        "items": items,
        "counts": {"shown": len(shown), "total": len(items)},
    }
    stable = lambda o: json.dumps({k: v for k, v in o.items() if k != "updated"},
                                  sort_keys=True, ensure_ascii=False)
    if DATA.exists() and stable(out) == stable(prev):
        print("no material change — skipping write.")
        return
    DATA.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote data.json · {len(shown)} items in {WINDOW_DAYS}-day window"
          + (f" · brief {len(brief.get('bullets', []))} bullets" if brief else ""))


if __name__ == "__main__":
    main()
