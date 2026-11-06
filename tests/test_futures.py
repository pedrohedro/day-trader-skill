import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load

fut = load("trader-futures", "futures")
tm = fut.tm


def ts_of(day, slot):
    """UTC epoch of a regular-session 5-minute bar (New York time, either offset)."""
    local = datetime.datetime.combine(day, datetime.time(9, 30)) + datetime.timedelta(minutes=5 * slot)
    utc = tm.eastern_to_utc(local)
    return int((utc - datetime.datetime(1970, 1, 1)).total_seconds())


def chart(today, today_bars, sessions=25, base=20000.0):
    """A Yahoo chart JSON: `sessions` full flat-ish weekdays before `today`, then today's bars."""
    stamps, o, h, l, c, v = [], [], [], [], [], []
    day = today
    days = []
    while len(days) < sessions:
        day -= datetime.timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    for d in sorted(days):
        p = base
        for s in range(78):
            step = 4.0 if s % 2 else -3.75
            stamps.append(ts_of(d, s)); o.append(p); c.append(p + step)
            h.append(max(p, p + step) + 5); l.append(min(p, p + step) - 5); v.append(1000)
            p += step
    for s, (bo, bh, bl, bc) in enumerate(today_bars):
        stamps.append(ts_of(today, s)); o.append(bo); h.append(bh); l.append(bl); c.append(bc); v.append(1000)
    return json.dumps({"chart": {"result": [{"meta": {"symbol": "MNQ=F", "dataGranularity": "5m"},
                                              "timestamp": stamps,
                                              "indicators": {"quote": [{"open": o, "high": h, "low": l,
                                                                        "close": c, "volume": v}]}}],
                                 "error": None}})


RANGE = [(20000, 20020, 19990, 20010), (20010, 20030, 20000, 20015), (20015, 20025, 19995, 20005)]
DAY = datetime.date(2026, 11, 6)


class Contract(unittest.TestCase):
    def test_front_month_and_roll(self):
        self.assertEqual(fut.front_contract("MNQ", DAY)[0], "MNQZ26")
        self.assertEqual(fut.front_contract("MNQ", DAY)[1], datetime.date(2026, 12, 18))
        # inside the roll window the next quarter is the front month
        self.assertEqual(fut.front_contract("MNQ", datetime.date(2026, 12, 11))[0], "MNQH27")

    def test_ticks(self):
        self.assertEqual(fut.to_tick(20012.6, 0.25, "down"), 20012.5)
        self.assertEqual(fut.to_tick(20012.6, 0.25, "up"), 20012.75)
        self.assertEqual(fut.to_tick(20012.625, 0.25, "near"), 20012.75)
        self.assertEqual(fut.to_tick(20012.5, 0.25, "down"), 20012.5)


class Pure(unittest.TestCase):
    def day(self, bars):
        return {i: {"o": b[0], "h": b[1], "l": b[2], "c": b[3], "start": "x"} for i, b in enumerate(bars)}

    def test_buy_on_close_above_high_not_on_a_wick(self):
        d = self.day(RANGE + [(20005, 20040, 20000, 20029), (20029, 20045, 20020, 20031)])
        orng, trig, _ = fut.find_trigger(d, 3, 4)
        self.assertEqual((orng["high"], orng["low"]), (20030, 19990))
        self.assertEqual((trig["side"], trig["slot"], trig["price"]), ("buy", 4, 20031))

    def test_sell_on_close_below_low(self):
        d = self.day(RANGE + [(20005, 20010, 19980, 19985)])
        _, trig, _ = fut.find_trigger(d, 3, 3)
        self.assertEqual((trig["side"], trig["price"]), ("sell", 19985))
        _, none, _ = fut.find_trigger(d, 3, 3, allow_short=False)
        self.assertIsNone(none)

    def test_short_plan_mirrors_and_rounds_away(self):
        trig = {"side": "sell", "price": 19985.0}
        p = fut.make_plan({"high": 20030.1, "low": 19990.0}, trig, 0.25)
        self.assertEqual(p["stop"], 20010.25)            # midpoint 20010.05 rounded UP for a sell
        self.assertEqual(p["risk_points"], 25.25)
        self.assertEqual(p["target"], 19959.75)          # 19985 - 25.25, nearest tick
        self.assertLess(p["target"], p["entry"])
        self.assertLess(p["entry"], p["stop"])

    def test_tight_stop_refused(self):
        with self.assertRaises(fut.FuturesError):
            fut.make_plan({"high": 20000.5, "low": 20000.0}, {"side": "buy", "price": 20001.0}, 0.25)

    def test_size_in_contracts(self):
        cfg = dict(fut.DEFAULTS)
        plan = {"risk_points": 25.0, "reward_points": 25.0}
        z = fut.size(plan, fut.CONTRACTS["MNQ"], cfg)       # US$ 50 a contract, US$ 120 default max loss
        self.assertEqual((z["contracts"], z["decided_by"], z["risk_usd"]), (2, "max_loss_per_trade", 100.0))
        self.assertTrue(z["max_loss_defaulted"])
        cfg["max_loss_per_trade_usd"] = 500
        z = fut.size(plan, fut.CONTRACTS["MNQ"], cfg)
        self.assertEqual((z["contracts"], z["decided_by"]), (3, "max_contracts"))
        z = fut.size({"risk_points": 175.5, "reward_points": 175.5}, fut.CONTRACTS["MNQ"], dict(fut.DEFAULTS))
        self.assertEqual(z["contracts"], 0)               # a stop too far is zero contracts, not a smaller stop


class EndToEnd(unittest.TestCase):
    def run_at(self, bars, now, **kw):
        cfg = dict(fut.DEFAULTS, max_loss_per_trade_usd=200)
        return fut.analyze(chart(DAY, bars), "MNQ", tm.parse_now(now), cfg, replays=500, **kw)

    def test_fresh_buy_passes_and_writes_no_order(self):
        doc = self.run_at(RANGE + [(20005, 20040, 20000, 20035)], "2026-11-06 09:50")
        self.assertEqual(doc["status"], "TRIGGERED")
        self.assertEqual(doc["plan"]["side"], "buy")
        self.assertEqual(doc["plan"]["time"], "9:50")
        self.assertNotIn("error", doc["timemachine"])
        self.assertEqual(doc["timemachine"]["mc"]["pool_sessions"], 25)
        self.assertEqual(doc["risk"]["verdict"], "PASS", doc["risk"]["checks"])
        self.assertEqual(doc["orders"], "none: analysis only")
        self.assertIn("no order was written or sent", fut.report(doc))

    def test_fresh_sell_runs_the_short_side(self):
        doc = self.run_at(RANGE + [(20005, 20010, 19980, 19982)], "2026-11-06 09:50")
        self.assertEqual(doc["plan"]["side"], "sell")
        self.assertGreater(doc["plan"]["stop"], doc["plan"]["entry"])
        self.assertNotIn("error", doc["timemachine"])

    def test_old_signal_is_blocked(self):
        later = [(20030, 20034, 20026, 20030)] * 6
        doc = self.run_at(RANGE + [(20005, 20040, 20000, 20035)] + later, "2026-11-06 10:20")
        self.assertEqual(doc["risk"]["verdict"], "BLOCK")
        failed = [c["check"] for c in doc["risk"]["checks"] if c["result"] == "BLOCK"]
        self.assertEqual(failed, ["fresh_signal"])

    def test_waiting_and_forming(self):
        self.assertEqual(self.run_at(RANGE + [(20005, 20025, 20000, 20020)], "2026-11-06 09:50")["status"], "WAITING")
        self.assertEqual(self.run_at(RANGE[:2], "2026-11-06 09:40")["status"], "RANGE_FORMING")

    def test_cli_offline_writes_analysis_only(self):
        tmp = tempfile.mkdtemp()
        cwd = os.getcwd()
        try:
            path = os.path.join(tmp, "mnq.json")
            with open(path, "w") as fh:
                fh.write(chart(DAY, RANGE + [(20005, 20040, 20000, 20035)]))
            os.chdir(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                code = fut.main(["analyze", "--offline", path, "--now", "2026-11-06 09:50", "--replays", "200",
                                 "--write", "--json"])
            self.assertIn(code, (0, 1))
            self.assertEqual(sorted(os.listdir(os.path.join(tmp, "trader"))), ["futures-analysis.json"])
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
