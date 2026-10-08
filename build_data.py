#!/usr/bin/env python3
"""
Pit Wall data builder.

Reads real F1 timing data through FastF1 and writes pitwall_data.json, which
the Pit Wall dashboard loads on its Insights tab.

    pip install fastf1 numpy pandas
    python build_data.py                      # Singapore history + 2026 form
    python build_data.py --year 2026 --round 17 --venue Singapore

Run it on a machine with normal internet access (your laptop or home server).
The first run downloads a lot of data and can take 10 to 20 minutes; later runs
use the local cache folder and are much faster.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

try:
    import fastf1
except ImportError:
    sys.exit("FastF1 is not installed. Run:  pip install fastf1 numpy pandas")


# ----------------------------------------------------------------- helpers

def log(msg):
    print(msg, flush=True)


def secs(td):
    """Timedelta (or NaT) to float seconds."""
    if pd.isna(td):
        return float("nan")
    return float(td.total_seconds())


def rnd(x, n=3):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), n)


def try_load(year, event, ident, telemetry=False, label=""):
    """Load one session, or return None with a short message."""
    try:
        ses = fastf1.get_session(year, event, ident)
        ses.load(laps=True, telemetry=telemetry, weather=False, messages=False)
        log(f"  loaded {label or ident} {year} {event}")
        return ses
    except Exception as exc:  # network, missing session, schema change
        log(f"  skipped {label or ident} {year} {event}: {type(exc).__name__}: {str(exc)[:90]}")
        return None


def clean_laps(laps):
    """Representative green-flag racing laps: no in/out laps, no safety car,
    timing is accurate, lap not deleted, and within 107% of the median."""
    if laps is None or len(laps) == 0:
        return pd.DataFrame()
    d = laps.copy()
    d["lt"] = d["LapTime"].apply(secs)
    ok = d["lt"].notna()
    ok &= d["PitInTime"].isna() & d["PitOutTime"].isna()
    if "IsAccurate" in d:
        ok &= d["IsAccurate"].fillna(False).astype(bool)
    if "Deleted" in d:
        ok &= ~d["Deleted"].fillna(False).astype(bool)
    if "TrackStatus" in d:
        ok &= d["TrackStatus"].astype(str).str.fullmatch("1")
    ok &= d["LapNumber"] > 1
    d = d[ok]
    if len(d):
        med = d["lt"].median()
        d = d[d["lt"] <= 1.07 * med]
    return d


# ------------------------------------------------------------- insight maths

def safety_car_stats(ses):
    """Safety car and virtual safety car deployments and laps under them."""
    sc = vsc = red = 0
    try:
        ts = ses.track_status
        st = ts["Status"].astype(str)
        sc = int((st == "4").sum())
        vsc = int((st == "6").sum())
        red = int((st == "5").sum())
    except Exception:
        pass
    sc_laps = 0
    try:
        laps = ses.laps
        mask = laps["TrackStatus"].astype(str).str.contains("4")
        sc_laps = int(laps.loc[mask, "LapNumber"].nunique())
    except Exception:
        pass
    return {"sc": sc, "vsc": vsc, "red": red, "sc_laps": sc_laps}


def pit_loss(laps):
    """Median time lost in the pit lane, from green-flag stops only.
    Loss = in-lap + out-lap minus two typical laps for that driver."""
    losses = []
    for drv, g in laps.groupby("Driver"):
        g = g.sort_values("LapNumber").reset_index(drop=True)
        base = clean_laps(g)
        if len(base) < 5:
            continue
        ref = base["lt"].median()
        for i in range(len(g) - 1):
            a, b = g.loc[i], g.loc[i + 1]
            if pd.isna(a["PitInTime"]) or pd.isna(b["PitOutTime"]):
                continue
            if str(a["TrackStatus"]) != "1" or str(b["TrackStatus"]) != "1":
                continue
            la, lb = secs(a["LapTime"]), secs(b["LapTime"])
            if math.isnan(la) or math.isnan(lb):
                continue
            loss = la + lb - 2 * ref
            if 5 < loss < 45:
                losses.append(loss)
    if not losses:
        return None, 0
    return float(np.median(losses)), len(losses)


def tyre_degradation(laps):
    """Seconds lost per lap of tyre age, per compound.

    Model on clean laps:  lap time = driver offset + compound offset
                                     + slope_compound * tyre_age + fuel * lap_number
    The lap-number term soaks up fuel burn and track evolution, so the tyre
    slope is not flattered by cars getting lighter."""
    d = clean_laps(laps)
    if len(d) < 40 or "Compound" not in d or "TyreLife" not in d:
        return []
    d = d[d["Compound"].isin(["SOFT", "MEDIUM", "HARD"])].dropna(subset=["TyreLife", "lt"])
    comps = [c for c in ["SOFT", "MEDIUM", "HARD"] if (d["Compound"] == c).sum() >= 15]
    if not comps:
        return []
    d = d[d["Compound"].isin(comps)]
    drivers = sorted(d["Driver"].unique())
    cols, names = [], []
    for drv in drivers:
        cols.append((d["Driver"] == drv).astype(float).values)
        names.append(("drv", drv))
    for c in comps[1:]:
        cols.append((d["Compound"] == c).astype(float).values)
        names.append(("off", c))
    for c in comps:
        cols.append(((d["Compound"] == c) * d["TyreLife"]).astype(float).values)
        names.append(("deg", c))
    cols.append(d["LapNumber"].astype(float).values)
    names.append(("fuel", ""))
    X = np.column_stack(cols)
    y = d["lt"].values
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    out = []
    for (kind, c), v in zip(names, coef):
        if kind == "deg":
            sub = d[d["Compound"] == c]
            stint = sub.groupby(["Driver", "Stint"])["LapNumber"].count()
            out.append({
                "compound": c,
                "deg_s_per_lap": rnd(v, 3),
                "laps": int(len(sub)),
                "median_stint_laps": rnd(stint.median(), 0) if len(stint) else None,
            })
    return out


def race_summary(ses, year):
    res = ses.results.copy()
    res["Position"] = pd.to_numeric(res["Position"], errors="coerce")
    res["GridPosition"] = pd.to_numeric(res["GridPosition"], errors="coerce")
    win = res.sort_values("Position").iloc[0]
    pole_row = res[res["GridPosition"] == 1]
    pole_won = bool(len(pole_row) and pole_row.iloc[0]["Position"] == 1)
    fin = res.dropna(subset=["Position"])
    fin = fin[(fin["GridPosition"] > 0)]
    change = (fin["GridPosition"] - fin["Position"]).abs()
    laps = ses.laps
    loss, n_stops = pit_loss(laps)
    stops = laps.groupby("Driver")["PitInTime"].apply(lambda s: s.notna().sum())
    out = {
        "year": int(year),
        "winner": str(win.get("Abbreviation", "")),
        "winner_team": str(win.get("TeamName", "")),
        "winner_grid": int(win["GridPosition"]) if not pd.isna(win["GridPosition"]) else None,
        "pole": str(pole_row.iloc[0]["Abbreviation"]) if len(pole_row) else None,
        "pole_won": pole_won,
        "avg_places_moved": rnd(change.mean(), 2),
        "pit_loss_s": rnd(loss, 1),
        "pit_loss_n": n_stops,
        "avg_stops": rnd(stops.mean(), 1),
        "laps": int(laps["LapNumber"].max()),
    }
    out.update(safety_car_stats(ses))
    out["tyres"] = tyre_degradation(laps)
    return out


def team_pace(laps):
    """Median clean-lap time per team, as a percentage gap to the best team."""
    d = clean_laps(laps)
    if len(d) == 0:
        return {}
    m = d.groupby("Team")["lt"].median()
    best = m.min()
    return {t: rnd((v / best - 1) * 100, 3) for t, v in m.items()}


def team_quali(res):
    """Best qualifying time per team as a percentage gap to pole."""
    r = res.copy()
    for c in ("Q1", "Q2", "Q3"):
        if c not in r:
            r[c] = pd.NaT
    r["best"] = r[["Q1", "Q2", "Q3"]].apply(lambda row: np.nanmin([secs(x) for x in row]) if any(
        not pd.isna(x) for x in row) else float("nan"), axis=1)
    m = r.groupby("TeamName")["best"].min().dropna()
    if m.empty:
        return {}
    best = m.min()
    return {t: rnd((v / best - 1) * 100, 3) for t, v in m.items()}


def team_best_lap(laps):
    d = laps.copy()
    d["lt"] = d["LapTime"].apply(secs)
    d = d.dropna(subset=["lt"])
    if "Deleted" in d:
        d = d[~d["Deleted"].fillna(False).astype(bool)]
    if d.empty:
        return {}
    m = d.groupby("Team")["lt"].min()
    best = m.min()
    return {t: rnd((v / best - 1) * 100, 3) for t, v in m.items()}


def long_run_pace(laps):
    """Practice long runs: per team median of clean laps from stints of 6+ laps."""
    d = clean_laps_loose(laps)
    if d.empty:
        return {}
    g = d.groupby(["Driver", "Stint"])
    keep = g["lt"].transform("count") >= 6
    d = d[keep]
    if d.empty:
        return {}
    m = d.groupby("Team")["lt"].median()
    best = m.min()
    return {t: rnd((v / best - 1) * 100, 3) for t, v in m.items()}


def clean_laps_loose(laps):
    """Like clean_laps but without the track-status filter (practice has yellows)."""
    if laps is None or len(laps) == 0:
        return pd.DataFrame()
    d = laps.copy()
    d["lt"] = d["LapTime"].apply(secs)
    ok = d["lt"].notna() & d["PitInTime"].isna() & d["PitOutTime"].isna()
    if "Deleted" in d:
        ok &= ~d["Deleted"].fillna(False).astype(bool)
    d = d[ok]
    if len(d):
        d = d[d["lt"] <= 1.07 * d["lt"].median()]
    return d


# ------------------------------------------------------- reference lap + map

def reference_lap(year, venue):
    ses = try_load(year, venue, "Q", telemetry=True, label="qualifying (telemetry)")
    if ses is None:
        return None
    lap = ses.laps.pick_fastest()
    tel = lap.get_telemetry().add_distance()
    tel = tel.dropna(subset=["X", "Y", "Speed"])
    rot = 0.0
    corners = []
    try:
        ci = ses.get_circuit_info()
        rot = float(ci.rotation)
        corners = ci.corners
    except Exception:
        pass
    ang = math.radians(rot)

    def rotate(x, y):
        return x * math.cos(ang) - y * math.sin(ang), x * math.sin(ang) + y * math.cos(ang)

    step = max(1, len(tel) // 320)
    t = tel.iloc[::step]
    pts = []
    for _, r in t.iterrows():
        x, y = rotate(float(r["X"]), float(r["Y"]))
        pts.append([rnd(x, 0), rnd(y, 0), rnd(r["Speed"], 0), rnd(r["Throttle"], 0),
                    int(bool(r["Brake"])), rnd(r["Distance"], 0)])
    cors = []
    xs = tel["X"].values
    ys = tel["Y"].values
    sp = tel["Speed"].values
    dist = tel["Distance"].values
    for _, c in (corners.iterrows() if len(corners) else []):
        i = int(np.argmin((xs - c["X"]) ** 2 + (ys - c["Y"]) ** 2))
        window = (dist > dist[i] - 90) & (dist < dist[i] + 90)
        cx, cy = rotate(float(c["X"]), float(c["Y"]))
        cors.append({"n": int(c["Number"]), "letter": str(c.get("Letter", "") or ""),
                     "x": rnd(cx, 0), "y": rnd(cy, 0), "dist": rnd(c["Distance"], 0),
                     "min_speed": rnd(sp[window].min(), 0)})
    throttle = tel["Throttle"].values
    return {
        "year": int(year), "driver": str(lap["Driver"]), "team": str(lap["Team"]),
        "lap_time_s": rnd(secs(lap["LapTime"]), 3),
        "length_m": rnd(float(dist.max()), 0),
        "top_speed": rnd(sp.max(), 0), "min_speed": rnd(sp.min(), 0),
        "full_throttle_pct": rnd((throttle >= 98).mean() * 100, 1),
        "braking_pct": rnd(tel["Brake"].astype(float).mean() * 100, 1),
        "points": pts, "corners": cors,
    }


# --------------------------------------------------------------- the build

def build(args):
    os.makedirs(args.cache, exist_ok=True)
    fastf1.Cache.enable_cache(args.cache)
    out = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fastf1": getattr(fastf1, "__version__", ""),
        "venue": args.venue,
        "notes": [],
    }

    log("Circuit reference lap")
    out["reference_lap"] = reference_lap(args.history[-1], args.venue)

    log(f"{args.venue} race history {args.history}")
    hist = []
    for y in args.history:
        ses = try_load(y, args.venue, "R", label="race")
        if ses is not None:
            try:
                hist.append(race_summary(ses, y))
            except Exception as exc:
                log(f"  could not summarise {y}: {exc}")
    out["history"] = hist
    out["notes"].append(
        "History uses seasons that ran the current 19-turn layout." if hist else "No history loaded.")

    log(f"{args.year} season form (last {args.recent} completed rounds)")
    form = {"rounds": [], "race_pace": {}, "quali": {}}
    try:
        sched = fastf1.get_event_schedule(args.year, include_testing=False)
        now = pd.Timestamp.now()
        done = sched[(sched["EventDate"] < now) & (sched["RoundNumber"] < (args.round or 99))]
        for _, ev in done.tail(args.recent).iterrows():
            name = str(ev["EventName"])
            rno = int(ev["RoundNumber"])
            r = try_load(args.year, rno, "R", label=f"race R{rno}")
            q = try_load(args.year, rno, "Q", label=f"qualifying R{rno}")
            entry = {"round": rno, "name": name}
            if r is not None:
                for team, gap in team_pace(r.laps).items():
                    form["race_pace"].setdefault(team, {})[str(rno)] = gap
            if q is not None:
                for team, gap in team_quali(q.results).items():
                    form["quali"].setdefault(team, {})[str(rno)] = gap
            form["rounds"].append(entry)
    except Exception as exc:
        log(f"  season form unavailable: {exc}")
    out["form"] = form

    if args.round:
        log(f"This weekend ({args.year} round {args.round})")
        wk = {}
        for ident, key in [("FP1", "fp1"), ("FP2", "fp2"), ("FP3", "fp3"),
                           ("SQ", "sprint_quali"), ("S", "sprint"), ("Q", "quali")]:
            ses = try_load(args.year, args.round, ident, label=key)
            if ses is None or len(ses.laps) == 0:
                continue
            entry = {"best_lap": team_best_lap(ses.laps)}
            if ident.startswith("FP"):
                entry["long_run"] = long_run_pace(ses.laps)
            wk[key] = entry
        out["weekend"] = wk
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--venue", default="Singapore", help="event name FastF1 understands")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--round", type=int, default=17, help="current round (0 to skip weekend sessions)")
    p.add_argument("--history", type=int, nargs="+", default=[2023, 2024, 2025],
                   help="past seasons to study at this venue")
    p.add_argument("--recent", type=int, default=5, help="recent rounds used for season form")
    p.add_argument("--cache", default=".f1cache")
    p.add_argument("--out", default="pitwall_data.json")
    args = p.parse_args()
    data = build(args)
    with open(args.out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    log(f"\nWrote {args.out} ({os.path.getsize(args.out) // 1024} KB). Load it on the Insights tab.")


if __name__ == "__main__":
    main()
