#!/usr/bin/env python3
"""trader-futures: the day-trader pipeline for a micro index future (MNQ), analysis only.

One run does what entry -> exit -> timemachine -> budget -> risk do for a stock, adapted
to a future: the opening-range breakout on regular-session 5-minute bars (9:30-16:00 New
York time) in BOTH directions, a stop and a target on the 0.25 tick grid, 10,000 replays
plus the history (reusing trader-timemachine's engine), a size in whole contracts at
US$ 2 a point, and a PASS / BLOCK against the account's limits (a prop account such as
Ylos 25K). It never writes an order, never talks to a broker and never reads credentials.

Standard library only. Data: Yahoo's free, unofficial chart endpoint (MNQ=F, the front
month), which can lag. Not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CONTRACTS = {
    "MNQ": {"name": "Micro E-mini Nasdaq-100", "yahoo": "MNQ=F", "point_value": 2.0, "tick": 0.25,
            "exchange": "CME"},
}
QUARTER = ((3, "H"), (6, "M"), (9, "U"), (12, "Z"))
ROLL_DAYS = 8  # CME equity index roll: about 8 days before the third-Friday expiry

DEFAULTS = {
    "account": "Ylos 25K",
    "max_contracts": 3,
    "drawdown_usd": 1500,          # Ylos 25K evaluation, as the account holder gave it
    "profit_target_usd": 1500,     # to pass the evaluation
    "evaluation_days": 30,
    "max_loss_per_trade_usd": None,  # None: 10 % of drawdown_usd, flagged as a default
    "daily_loss_usd": None,          # None: no daily limit checked (some Ylos plans have none)
    "min_reward_risk": 1.0,
    "no_entries_before": "09:45",
    "no_entries_after": "15:30",
    "flat_by": "15:55",
    "allow_short": True,
}


class FuturesError(Exception):
    pass


def _load_tm():
    for base in (os.path.join(HERE, "..", "trader-timemachine"),
                 os.path.join(os.path.expanduser("~"), ".claude", "skills", "trader-timemachine")):
        path = os.path.join(base, "timemachine.py")
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("trader_timemachine_for_futures", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FuturesError("trader-timemachine/timemachine.py not found next to this skill: install the whole pack")


tm = _load_tm()


# ---------------------------------------------------------------- contract and ticks

def third_friday(year, month):
    d = datetime.date(year, month, 15)
    return d + datetime.timedelta(days=(4 - d.weekday()) % 7)


def front_contract(root, today):
    """The quarterly contract a day trader is in: the next one whose roll date is still ahead."""
    for year in (today.year, today.year + 1):
        for month, code in QUARTER:
            expiry = third_friday(year, month)
            if today <= expiry - datetime.timedelta(days=ROLL_DAYS):
                return "%s%s%02d" % (root, code, year % 100), expiry
    raise FuturesError("no contract month found")


def to_tick(x, tick, mode):
    """Round to the tick grid: 'down', 'up' or 'near' (half up)."""
    n = x / tick
    eps = 1e-9
    if mode == "down":
        n = math.floor(n + eps)
    elif mode == "up":
        n = math.ceil(n - eps)
    else:
        n = math.floor(n + 0.5 + eps)
    return round(n * tick, 2)


def hhmm(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def clock(minutes):
    return "%d:%02d" % (minutes // 60, minutes % 60)


# ---------------------------------------------------------------- the pure steps

def find_trigger(day, n, last_slot, allow_short=True):
    """day: {slot: bar}. Opening range = slots 0..n-1. The first completed bar after the
    range that CLOSES above the high (buy) or below the low (sell). Returns
    (orng, trigger or None, missing range bars)."""
    missing = [i for i in range(n) if i not in day]
    if missing:
        return None, None, missing
    oh = max(day[i]["h"] for i in range(n))
    ol = min(day[i]["l"] for i in range(n))
    orng = {"high": oh, "low": ol, "minutes": n * 5}
    for i in range(n, last_slot + 1):
        b = day.get(i)
        if b is None:
            continue
        if b["c"] > oh:
            return orng, {"side": "buy", "slot": i, "price": b["c"], "bar": b}, []
        if allow_short and b["c"] < ol:
            return orng, {"side": "sell", "slot": i, "price": b["c"], "bar": b}, []
    return orng, None, []


def make_plan(orng, trig, tick, stop_rule="or-mid", target_r=1.0):
    long = trig["side"] == "buy"
    entry = trig["price"]
    if stop_rule == "or-mid":
        raw = (orng["high"] + orng["low"]) / 2.0
        tm_rule = "or_mid"
    elif stop_rule == "or-far":
        raw = orng["low"] if long else orng["high"]
        tm_rule = "or_low" if long else "or_high"
    else:
        raise FuturesError("--stop must be or-mid or or-far")
    stop = to_tick(raw, tick, "down" if long else "up")  # away from the entry
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        raise FuturesError("the stop %.2f is not on the losing side of the entry %.2f" % (stop, entry))
    if risk < max(4 * tick, entry * 0.0005):
        raise FuturesError("a %.2f-point stop is too tight to survive the spread and normal wiggle" % risk)
    target = to_tick(entry + target_r * risk if long else entry - target_r * risk, tick, "near")
    reward = (target - entry) if long else (entry - target)
    return {"side": trig["side"], "entry": entry, "stop": stop, "target": target,
            "risk_points": round(risk, 2), "reward_points": round(reward, 2),
            "reward_risk": round(reward / risk, 2), "stop_rule": stop_rule, "tm_stop_rule": tm_rule,
            "target_r": target_r}


def size(plan, spec, cfg):
    per = round(plan["risk_points"] * spec["point_value"], 2)
    dd = cfg.get("drawdown_usd")
    max_loss = cfg.get("max_loss_per_trade_usd")
    defaulted = max_loss is None
    if defaulted:
        max_loss = round(0.10 * dd, 2) if dd else 0
    by_loss = int(math.floor(max_loss / per + 1e-9)) if per > 0 else 0
    qty = max(0, min(int(cfg["max_contracts"]), by_loss))
    decided = "max_loss_per_trade" if by_loss < int(cfg["max_contracts"]) else "max_contracts"
    return {"risk_usd_per_contract": per, "max_loss_per_trade_usd": max_loss, "max_loss_defaulted": defaulted,
            "by_max_loss": by_loss, "max_contracts": int(cfg["max_contracts"]), "contracts": qty,
            "decided_by": decided, "risk_usd": round(qty * per, 2),
            "reward_usd": round(qty * plan["reward_points"] * spec["point_value"], 2),
            "pct_of_drawdown": round(100.0 * qty * per / dd, 1) if dd else None}


def risk_check(plan, sz, cfg, trig_close_min, now_min, bars_since):
    checks = []

    def add(name, ok, why):
        checks.append({"check": name, "result": "PASS" if ok else "BLOCK", "why": why})

    add("contracts", sz["contracts"] >= 1,
        "%d contract(s): US$ %.2f risk each, max loss per trade US$ %.2f"
        % (sz["contracts"], sz["risk_usd_per_contract"], sz["max_loss_per_trade_usd"]))
    add("max_contracts", sz["contracts"] <= sz["max_contracts"], "limit %d" % sz["max_contracts"])
    add("reward_risk", plan["reward_risk"] >= float(cfg["min_reward_risk"]),
        "%.2f vs minimum %.2f" % (plan["reward_risk"], float(cfg["min_reward_risk"])))
    add("short_allowed", plan["side"] == "buy" or bool(cfg.get("allow_short")), "sell needs allow_short")
    lo, hi = hhmm(cfg["no_entries_before"]), hhmm(cfg["no_entries_after"])
    add("entry_window_signal", lo <= trig_close_min <= hi,
        "signal at %s, window %s-%s ET" % (clock(trig_close_min), cfg["no_entries_before"], cfg["no_entries_after"]))
    add("entry_window_now", lo <= now_min <= hi,
        "now %s ET, window %s-%s ET" % (clock(now_min), cfg["no_entries_before"], cfg["no_entries_after"]))
    add("fresh_signal", bars_since <= 1,
        "the trigger is %d bar(s) old: a late entry is a different trade" % bars_since)
    if cfg.get("daily_loss_usd"):
        add("daily_loss", sz["risk_usd"] <= float(cfg["daily_loss_usd"]),
            "US$ %.2f at the stop vs daily limit US$ %.2f" % (sz["risk_usd"], float(cfg["daily_loss_usd"])))
    if cfg.get("drawdown_usd"):
        add("drawdown", sz["risk_usd"] <= 0.25 * float(cfg["drawdown_usd"]),
            "US$ %.2f at the stop is %.1f %% of the US$ %.0f drawdown (limit 25 %%)"
            % (sz["risk_usd"], 100.0 * sz["risk_usd"] / float(cfg["drawdown_usd"]), float(cfg["drawdown_usd"])))
    verdict = "PASS" if all(c["result"] == "PASS" for c in checks) else "BLOCK"
    return verdict, checks


# ---------------------------------------------------------------- the run

def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    return cfg


def analyze(text, root, now_utc, cfg, stop_rule="or-mid", target_r=1.0, replays=10000, seed=19,
            or_minutes=15, source="yahoo"):
    spec = CONTRACTS[root]
    result = tm.chart_result(text, spec["yahoo"])
    meta, bars, dropped = tm.parse_bars(result, now_utc)
    now_et = tm.to_eastern(now_utc)
    today = now_et.date()
    contract, expiry = front_contract(root, today)
    doc = {"created_by": "trader-futures", "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source": source, "root": root, "contract": contract, "expiry": expiry.isoformat(),
           "spec": spec, "now_et": now_et.strftime("%Y-%m-%d %H:%M"), "config": cfg, "orders": "none: analysis only"}
    days = tm.split_sessions(bars)
    day = days.get(today.isoformat(), {})
    n = or_minutes // 5
    if not day:
        doc.update(status="NO_SESSION", reason="no complete regular-session bars for %s yet" % today)
        return doc
    last_slot = max(day)
    if last_slot < n:
        doc.update(status="RANGE_FORMING", reason="the %d-minute opening range is still forming" % or_minutes)
        return doc
    orng, trig, missing = find_trigger(day, n, last_slot, bool(cfg.get("allow_short")))
    doc["opening_range"] = orng
    last = day[last_slot]
    doc["last_bar"] = {"start": last["start"], "close": last["c"]}
    if missing:
        doc.update(status="NO_DATA", reason="opening-range bars missing: %s" % missing)
        return doc
    now_min = now_et.hour * 60 + now_et.minute
    if trig is None:
        doc.update(status="WAITING",
                   reason="no 5-minute close outside %.2f-%.2f yet; last close %.2f (%s bar)"
                   % (orng["low"], orng["high"], last["c"], last["start"]))
        return doc
    close_min = tm.OPEN_MIN + 5 * (trig["slot"] + 1)
    bars_since = last_slot - trig["slot"]
    plan = make_plan(orng, trig, spec["tick"], stop_rule, target_r)
    plan.update(time=clock(close_min), bar=trig["slot"], bars_since=bars_since)
    doc.update(status="TRIGGERED", plan=plan)
    # the time machine, on the same bars (its symbol check wants a plain ticker)
    data = json.loads(text)
    data["chart"]["result"][0]["meta"]["symbol"] = root
    raw = {"symbol": root, "side": plan["side"], "date": today.isoformat(), "setup": "orb",
           "entry": plan["entry"], "stop": plan["stop"], "target": plan["target"], "entry_bar": trig["slot"],
           "opening_range": {"minutes": or_minutes, "high": orng["high"], "low": orng["low"]},
           "stop_rule": plan["tm_stop_rule"], "flat_by": cfg["flat_by"]}
    try:
        tplan = tm.normalize_plan(raw, "trader-futures")
        tdoc = tm.time_machine(json.dumps(data), tplan, now_utc, replays, seed, 2, cfg["flat_by"], source)
        doc["timemachine"] = {k: tdoc[k] for k in ("verdict", "verdict_notes", "history", "warnings", "note")}
        doc["timemachine"]["mc"] = {k: v for k, v in tdoc["mc"].items()
                                    if k not in ("bands", "sample_paths", "hist", "band_times")}
    except tm.TMError as e:
        doc["timemachine"] = {"error": str(e)}
    sz = size(plan, spec, cfg)
    doc["sizing"] = sz
    verdict, checks = risk_check(plan, sz, cfg, close_min, now_min, bars_since)
    doc["risk"] = {"verdict": verdict, "checks": checks}
    return doc


def report(doc):
    out = []
    s = doc["spec"]
    out.append("%s (%s, %s) %s ET. Front month %s, expires %s. US$ %.2f/point, tick %.2f. Yahoo %s, free, can lag."
               % (doc["root"], s["name"], s["exchange"], doc["now_et"], doc["contract"], doc["expiry"],
                  s["point_value"], s["tick"], s["yahoo"]))
    if "opening_range" in doc and doc["opening_range"]:
        o = doc["opening_range"]
        out.append("Opening range 9:30-%s: high %.2f, low %.2f." % (clock(tm.OPEN_MIN + o["minutes"]), o["high"], o["low"]))
    out.append("STATUS %s%s" % (doc["status"], (": " + doc["reason"]) if doc.get("reason") else ""))
    p = doc.get("plan")
    if p:
        word = "BUY (closed above the high)" if p["side"] == "buy" else "SELL (closed below the low)"
        out.append("Signal %s at %s ET, %.2f (bar %d, %d bar(s) ago)." % (word, p["time"], p["entry"], p["bar"], p["bars_since"]))
        out.append("Plan: entry %.2f  stop %.2f (%s)  target %.2f (%gR)  risk %.2f pts  reward:risk %.2f  flat by %s ET"
                   % (p["entry"], p["stop"], p["stop_rule"], p["target"], p["target_r"], p["risk_points"],
                      p["reward_risk"], doc["config"]["flat_by"]))
        t = doc.get("timemachine", {})
        if "error" in t:
            out.append("Time machine: refused: %s" % t["error"])
        elif t:
            mc = t["mc"]
            out.append("Time machine: %d replays to %s, seed %d, pool %d sessions: target first %.1f %%, stop first %.1f %%, "
                       "neither %.1f %%, green %.1f %%; close median %.2f (5-95 %%: %.2f-%.2f); average R %+.3f"
                       % (mc["replays"], mc["until"], mc["seed"], mc["pool_sessions"], 100 * mc["p_target"],
                          100 * mc["p_stop"], 100 * mc["p_neither"], 100 * mc["p_green"], mc["median_close"],
                          mc["p5_close"], mc["p95_close"], mc["avg_R"]))
            h = t["history"]
            if h.get("rule"):
                c = {"target": 0, "stop": 0, "close": 0}
                for tr in h.get("trades", []):
                    c[tr.get("outcome", "close")] = c.get(tr.get("outcome", "close"), 0) + 1
                out.append("History: the same rule on %d sessions fired %d times: %d target, %d stop, %d neither."
                           % (h["sessions"], h["triggered"], c["target"], c["stop"], c["close"]))
            out.append(t["verdict"])
            out.extend(t["verdict_notes"])
        z = doc["sizing"]
        out.append("Size: %d contract(s), decided by %s (risk US$ %.2f/contract, max loss/trade US$ %.2f%s). "
                   "At the stop: US$ %.2f%s."
                   % (z["contracts"], z["decided_by"], z["risk_usd_per_contract"], z["max_loss_per_trade_usd"],
                      " = 10 % of the drawdown, a DEFAULT to confirm" if z["max_loss_defaulted"] else "",
                      z["risk_usd"], (" (%.1f %% of the drawdown)" % z["pct_of_drawdown"]) if z["pct_of_drawdown"] is not None else ""))
        r = doc["risk"]
        out.append("RISK %s" % r["verdict"])
        for c in r["checks"]:
            out.append("  %-5s %-20s %s" % (c["result"], c["check"], c["why"]))
    c = doc["config"]
    if c.get("profit_target_usd") and doc.get("sizing"):
        z = doc["sizing"]
        out.append("Account %s: target US$ %.0f in %s days, max drawdown US$ %.0f. This trade at %d contract(s): "
                   "US$ %.2f at the stop, US$ %.2f at the target (%.1f %% of the goal)."
                   % (c.get("account"), c["profit_target_usd"], c.get("evaluation_days"), c.get("drawdown_usd") or 0,
                      z["contracts"], z["risk_usd"], z["reward_usd"], 100.0 * z["reward_usd"] / c["profit_target_usd"]))
    out.append("Analysis only: no order was written or sent. A stop is a plan, not a guarantee. Not financial advice.")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="opening-range breakout, plan, replays, size and risk, analysis only")
    a.add_argument("--root", default="MNQ", choices=sorted(CONTRACTS))
    a.add_argument("--stop", default="or-mid", choices=["or-mid", "or-far"])
    a.add_argument("--target", default="1R", help="e.g. 1R, 1.5R, 2R")
    a.add_argument("--or-minutes", type=int, default=15)
    a.add_argument("--replays", type=int, default=10000)
    a.add_argument("--seed", type=int, default=19)
    a.add_argument("--config", default=os.path.join("trader", "futures-config.json"))
    a.add_argument("--offline", help="a saved Yahoo chart JSON (.json or .json.gz) instead of the network")
    a.add_argument("--now", help='pin the clock, New York time, e.g. "2026-11-06 10:15"')
    a.add_argument("--write", action="store_true", help="save trader/futures-analysis.json")
    a.add_argument("--json", action="store_true")
    c = sub.add_parser("contract", help="the front-month contract and its roll")
    c.add_argument("--root", default="MNQ", choices=sorted(CONTRACTS))
    c.add_argument("--now")
    args = ap.parse_args(argv)
    try:
        now_utc = tm.parse_now(args.now)
        if args.cmd == "contract":
            code, exp = front_contract(args.root, tm.to_eastern(now_utc).date())
            print("%s front month: %s, expires %s (roll about %d days before)." % (args.root, code, exp, ROLL_DAYS))
            return 0
        tr = args.target.upper().rstrip("R")
        try:
            target_r = float(tr)
        except ValueError:
            raise FuturesError("--target must look like 1R or 2R")
        if target_r <= 0:
            raise FuturesError("--target must be above 0R")
        spec = CONTRACTS[args.root]
        text = tm.read_offline(args.offline, args.root) if args.offline else tm.fetch_chart(spec["yahoo"])
        cfg = load_config(args.config)
        doc = analyze(text, args.root, now_utc, cfg, args.stop, target_r, args.replays, args.seed,
                      args.or_minutes, "offline" if args.offline else "yahoo")
    except (FuturesError, tm.TMError) as e:
        print("trader-futures: %s" % e)
        return 1
    if args.write:
        os.makedirs("trader", exist_ok=True)
        with open(os.path.join("trader", "futures-analysis.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
    print(json.dumps(doc, indent=1) if args.json else report(doc))
    return 0 if doc["status"] == "TRIGGERED" and doc["risk"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
