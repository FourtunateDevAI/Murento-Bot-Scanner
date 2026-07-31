# Murento AI Trader — 24/7 Scanner (GitHub Actions)

This runs the same Murento MSNR SBR/RBS cascade strategy as the dashboard,
scanning all 9 pairs every ~15 minutes, forever — independent of your phone,
browser, or the dashboard being open.

## Setup (one-time)

1. **Create a new GitHub repository** (private is fine) at github.com/new.
   Name it anything, e.g. `murento-scanner`.

2. **Upload these files** to the repo, keeping the folder structure exactly:
   ```
   scanner.py
   requirements.txt
   .github/workflows/scan.yml
   ```
   Easiest way on mobile: use GitHub's web "Add file → Upload files" and drag
   all three in (make sure the workflow file lands in `.github/workflows/`,
   not the repo root).

3. **Add your secrets** — go to your repo → Settings → Secrets and variables →
   Actions → "New repository secret". Add three:
   - `TWELVEDATA_KEY` → your TwelveData API key
   - `TELEGRAM_TOKEN` → your bot token (`8073607761:AAGH...`)
   - `TELEGRAM_CHAT_ID` → `5065044887`

   These stay encrypted — never visible in logs or to anyone browsing the repo.

4. **Enable Actions** if prompted — repo → Actions tab → "I understand my
   workflows, go ahead and enable them."

5. **Test it manually** — Actions tab → "Murento AI Trader Scanner" → "Run
   workflow" button → Run. Watch it execute; check the log output for each
   pair's confidence %, and check Telegram if anything crosses 80%.

That's it — from here it runs automatically every 15 minutes, 24/7, on
GitHub's servers. Your phone can be off.

## How it works

- `scanner.py` fetches live candles from TwelveData for all 9 pairs across
  5 timeframes each (Daily, 4H, 1H, 30M, 15M), computes the exact same
  cascade signal as the dashboard, and sends a Telegram alert for anything
  at 80%+ confidence.
- Rate-limited to stay under TwelveData's free-tier ~8 calls/minute cap.
- `state.json` tracks which setups have already been alerted (per pair) so
  you don't get repeat pings for the same setup — it's committed back to
  the repo after each run so state survives between runs.
- The workflow runs on a cron schedule (`*/15 * * * *`) — every 15 minutes.
  GitHub doesn't guarantee exact timing under load, so actual runs may drift
  a few minutes late sometimes; this is normal for free scheduled Actions.

## Current configuration

Watching **XAUUSD, BTCUSD, EURUSD** — 3 pairs, all 5 timeframes, scanned every
**30 minutes**. Math: 3 pairs x 5 timeframes = 15 credits/cycle x 48 cycles/day
= 720 credits/day, under TwelveData's 800/day free cap with ~80 to spare.

To change which pairs are watched, edit the `PAIRS` list at the top of
`scanner.py`. To change frequency, edit the `cron` line in
`.github/workflows/scan.yml` — just keep pairs x 5 x cycles/day under 800.

## Limits to know about

- **TwelveData free tier (confirmed)**: 8 API credits/minute AND 800 credits/day,
  resetting at midnight UTC. This configuration (3 pairs, every 30 min) uses
  720/day, safely under that cap.
- **GitHub Actions free tier**: public repos get unlimited minutes; private
  repos get 2,000 free minutes/month. A full scan run takes a couple of
  minutes, so even every 30 minutes should stay well within free minutes.
- Scheduled runs aren't exact — GitHub can delay a cron trigger by a few
  minutes under load. That's normal for free scheduled Actions, not a bug.
- If you want more pairs and/or faster scanning without dropping timeframes,
  the real fix is a paid TwelveData plan (Grow, ~$29/month) with a much
  higher call budget.
