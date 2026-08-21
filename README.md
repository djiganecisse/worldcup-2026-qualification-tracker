# 🇸🇳 World Cup 2026 — Round-of-32 Qualification Tracker

A single-file Python agent that estimates, **in real time**, a team's probability of
reaching the 2026 FIFA World Cup round of 32 — and pushes a Telegram alert whenever
that probability moves.

Built originally to track **Senegal**, but the tracked team is configurable, so it
works for any of the 48 sides.

> ⚠️ This is a modelling toy for fun and learning. It is **not** a betting tool.
> Team strengths are estimates and the model is deliberately simple. See *Caveats*.

## What it does

- Pulls every group-stage result from the free, no-key
  [openfootball/worldcup.json](https://github.com/openfootball/worldcup.json) dataset.
- Runs a **Monte Carlo** simulation (default 12,000 runs) of all remaining group matches.
- Rates all 48 teams on **real Elo** scraped daily from
  [eloratings.net](https://www.eloratings.net) (cached; embedded fallback if offline).
- Applies the **actual 2026 FIFA rules**: top two per group **plus the eight best
  third-placed teams** across the 12 groups, with full tiebreakers
  (points → goal difference → goals for → head-to-head → drawing of lots).
- Optional **live overlay** via [api-sports.io](https://www.api-football.com): while a
  match is in play, the current score is locked in and only the remaining minutes are
  simulated. Without a key it runs in "after-match" mode.
- Sends a **Telegram** message on meaningful change: ±5 points, crossing 25/50/75 %,
  flipping alive/eliminated, kickoff of the tracked team, or its match ending.
- Extras in every message: **outcome conditionals** for the tracked team's next match
  (win / draw / loss → probability), **win-margin scenarios** ("win by 2 → 96 %") and a
  **sensitivity analysis** naming the other groups' matches that move your number most.

## Model

- Goals are drawn from a **bivariate Poisson** (a shared component induces score
  correlation, giving realistic draw rates). Expected supremacy comes from the Elo gap;
  host nations (USA/Canada/Mexico) get a home-advantage bump.
- Exact ties left after points/GD/GF/head-to-head are broken by a random draw (the real
  final FIFA step), re-rolled each simulation so tie uncertainty propagates into the odds.

## Requirements

Python 3.8+. **Standard library only** — no `pip install` needed.

## Quick start

```bash
# one cycle, compute and print, send nothing
python3 senegal_wc_tracker.py --dry-run

# track a different team
WC_TRACK_TEAM="Morocco" python3 senegal_wc_tracker.py --dry-run

# enable Telegram notifications
export TELEGRAM_BOT_TOKEN="123456:abc..."
export TELEGRAM_CHAT_ID="987654321"
python3 senegal_wc_tracker.py --force        # recompute and notify now

# enable live goal-by-goal (free api-sports.io key)
export FOOTBALL_API_KEY="your_key"
python3 senegal_wc_tracker.py
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `WC_TRACK_TEAM` | `Senegal` | Team to track (openfootball spelling) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Telegram notifications (omit → log only) |
| `FOOTBALL_API_KEY` | — | api-sports.io live overlay (omit → after-match mode) |
| `WC_TRACKER_HOME` | `./data` | Where state, logs and the Elo cache live |

### Flags

`--dry-run` compute and print, never notify · `--force` recompute and notify ·
`--refresh-elo` force an Elo refresh · `--sims N` simulation count.

## Run it on a schedule

The agent self-throttles: heavy recompute only inside match windows, and live API calls
are capped (~1 every few minutes) to respect the free tier's 100 requests/day.

**macOS (launchd):** see [`examples/com.example.wc-tracker.plist`](examples/com.example.wc-tracker.plist).
**Linux (cron):** `*/2 * * * * FOOTBALL_API_KEY=... /usr/bin/python3 /path/senegal_wc_tracker.py`

## Caveats (read before trusting a number)

- Elo ratings are approximate proxies for strength, not a market.
- Fair-play (disciplinary) tiebreak is replaced by the random draw, since the data source
  carries no cards.
- No modelling of rotation in dead-rubber matches, altitude, injuries, or motivation.
- Monte Carlo noise is ~±1 point at 12k sims.

## Ideas to improve (PRs welcome)

- Knockout bracket: extend past the group stage to round-of-32/16 path probabilities.
- Plug a stronger match model (xG-based, Dixon-Coles with fitted ρ, market odds blend).
- Pull live cards to implement the real fair-play tiebreak.
- A small web dashboard / probability-over-time chart from the stored history.
- Better team-name normalization across data sources.

## License

MIT — see [LICENSE](LICENSE).
