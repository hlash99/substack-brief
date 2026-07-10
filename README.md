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
