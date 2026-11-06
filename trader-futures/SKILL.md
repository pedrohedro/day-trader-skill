---
name: trader-futures
description: >-
  Futures Analyst for a micro index future (MNQ, the Micro E-mini Nasdaq-100,
  front month such as MNQZ26). Runs the day-trader pipeline for one contract in
  one go, analysis only: the opening-range breakout on regular-session 5-minute
  bars in BOTH directions (a close above the range buys, a close below sells),
  a stop and a target on the 0.25 tick grid, 10,000 replays plus the history
  with trader-timemachine's engine, a size in whole contracts at US$ 2 a point,
  and PASS or BLOCK against a prop account's limits (Ylos 25K by default: 3
  contracts, the drawdown, the entry window, flat by 15:55). It never writes an
  order, never talks to a broker and never touches credentials. Use when the
  user says "analyze MNQ", "mini nasdaq", "MNQZ", "nasdaq futures", "micro
  futures", "is there a setup on the Nasdaq future", "how many contracts", or
  trades futures on a prop account.
argument-hint: "[analyze | contract] [--stop or-mid|or-far] [--target 1R]"
---

# trader-futures

The stock skills in this pack are long only and sized in shares. A future is
different: it goes both ways, it moves in ticks, and one contract is worth a
fixed amount per point. This skill runs the same pipeline for a micro index
future, from the signal to the risk check, and stops there. Nothing in it can
place an order. Day trading futures is leveraged and most people who do it
lose money; a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder. It needs
`trader-timemachine` installed next to it (it reuses that engine).

```bash
python3 "${CLAUDE_SKILL_DIR}/futures.py" contract                 # front month and its roll
python3 "${CLAUDE_SKILL_DIR}/futures.py" analyze                  # MNQ now: signal, plan, replays, size, risk
python3 "${CLAUDE_SKILL_DIR}/futures.py" analyze --stop or-far --target 2R --write
python3 "${CLAUDE_SKILL_DIR}/futures.py" analyze --now "2026-11-06 10:15" --offline saved.json.gz
```

Exit code 0 only when a fresh signal passed every risk check; 1 otherwise.

## The contract

| | MNQ |
| --- | --- |
| what | Micro E-mini Nasdaq-100, CME |
| a point | US$ 2.00 a contract |
| a tick | 0.25 point = US$ 0.50 |
| data | Yahoo `MNQ=F` (the front month, continuous), free and unofficial, can lag |
| months | H M U Z (Mar, Jun, Sep, Dec), expiry the third Friday, roll about 8 days before |

Only regular-session bars count (9:30 to 16:00 New York time), so the opening
range is the same 9:30, 9:35 and 9:40 bars the stock skills use. Overnight
bars are left out.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/futures-config.json` | optional, overrides the defaults below |
| `trader/futures-analysis.json` | with `--write`: status, opening range, plan, replays, size, checks |

Defaults (a Ylos 25K evaluation: US$ 1,500 max drawdown, US$ 1,500 target in
30 days; other plans and phases differ, override them in the config):

```json
{"account": "Ylos 25K", "max_contracts": 3, "drawdown_usd": 1500,
 "profit_target_usd": 1500, "evaluation_days": 30,
 "max_loss_per_trade_usd": null, "daily_loss_usd": null, "min_reward_risk": 1.0,
 "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
 "allow_short": true}
```

`max_loss_per_trade_usd: null` means 10 % of the drawdown, and the report says
it is a default to confirm.

## The rule

- **Signal.** The first completed 5-minute bar after the range that CLOSES above
  the range high (buy) or below the range low (sell). One signal a day.
- **Stop.** `or-mid` (default, the range midpoint) or `or-far` (the other side
  of the range), rounded to the tick AWAY from the entry. Refused when it is
  tighter than 4 ticks or 0.05 % of the price.
- **Target.** `--target 1R` by default, rounded to the nearest tick.
- **Replays.** trader-timemachine's engine on the contract's own regular-session
  bars, seed 19, to the flat-by time. The verdict line is the engine's.
- **Size.** Whole contracts: the smaller of `max_contracts` and max loss per
  trade divided by (risk in points x US$ 2).
- **Risk.** BLOCK when: zero contracts, over `max_contracts`, reward:risk under
  the minimum, a sell with `allow_short` false, the signal or now outside the
  entry window, a signal more than one bar old (a late entry is a different
  trade), over `daily_loss_usd`, or more than 25 % of the drawdown at the stop.

## Steps

1. Run `futures.py analyze --write`. Say the status in one line.
2. If triggered: the plan, the replays and the verdict exactly as printed, the
   seed, the size and which limit decided it, then PASS or BLOCK per check.
3. If the size is 0, say why in dollars: the stop is too far for the max loss.
   Never shrink the stop to fit a contract.
4. Say it is analysis only. The user decides and enters any order themselves.

## What it never does

It never places, writes or schedules an order, never logs in to a broker or a
trading platform, never reads or asks for credentials, never raises a limit,
and never calls a simulation a prediction.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Futures are leveraged: a fast market or a gap can fill a stop well past its price.
- Paper or evaluation accounts first. Prop-firm rules (drawdown, daily loss, consistency, news
  windows) are the user's to read and keep; this skill checks only what is in its config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- Broker keys and passwords live outside the chat, and this skill never uses them.
