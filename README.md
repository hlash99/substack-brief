# 📬 Substack Brief

Rolling brief of the latest newsletters from the Yahoo Mail **Substack** folder —
a cross-newsletter synthesis ("The Brief") plus 2–3 bullets per email, published at
**https://hlash99.github.io/substack-brief/**.

## How it works

- `.github/workflows/refresh.yml` runs twice a day (~7:20am / 4:20pm Pacific).
- `scripts/build_brief.py` logs into Yahoo Mail over IMAP (read-only — messages are
  never marked read), pulls the last ~9 days of the `Substack` folder, and summarizes
  each new email into bullets with a free-tier LLM chain (Groq → OpenRouter → Cerebras,
  or Anthropic if a key is set). Summaries are cached in `data.json` by Message-ID, so
  each email is only summarized once.
- A rolling **Brief** (≤6 bullets) is re-synthesized across the last 3 days whenever
  new emails arrive.
- The page shows a 7-day window with per-publication filter chips.

## Failure behaviour

- IMAP/login failure, a corrupt `data.json`, or a store that would shrink by >40% in one
  run: the run fails and `data.json` is left exactly as it was.
- One undecodable email is skipped (logged) instead of failing the run.
- A model reply that is cut off or oddly shaped is salvaged (complete bullets kept). If
  fewer than 3 brief bullets survive, the previous brief stays and `brief_status`
  (`ok` / `tried` / `note`) records why; the page shows when the brief was actually written.
- An email whose summary reply is unusable 3 times keeps its excerpt (no more LLM calls).
- The page marks emails newer than your last visit on that device (localStorage only) and
  folds days older than the newest three behind their header.

## Setup (repo secrets)

| Secret | Value |
|---|---|
| `YAHOO_EMAIL` | the Yahoo address |
| `YAHOO_APP_PASSWORD` | Yahoo → Account Security → **Generate app password** (not the login password) |
| `GROQ_API_KEY` | console.groq.com key (free) — same one ctcl-papers uses |

Until the Yahoo secrets exist, the workflow exits cleanly without touching data.
Without an LLM key, new items get a raw excerpt and are re-summarized automatically
on the first run that has one.

Local bootstrap (Apple Mail): extract emails to .txt files, then
`python3 scripts/build_brief.py --from-dir <dir>`.
