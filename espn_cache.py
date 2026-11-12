"""GainTheEdge: save the ESPN data the website needs, on our own site, so pages don't depend on ESPN answering each visitor.

Runs on GitHub Actions (.github/workflows/espn_cache.yml) from a checkout of the website repository, every 2 hours.

    python tools/espn_cache.py            # every sport
    python tools/espn_cache.py nba cbb    # just these

What it saves (data/espn/, one small JSON file per ESPN request, named like the page's espnKey()):
  * each league's team list (logos, colors, ids)
  * for every game on the next few days' schedule: ESPN's game summary (injuries, leaders), both teams' schedules
    (form, last game), the last game's box score, 5 seasons of the home team's schedule (head to head), and in football
    each team's depth chart and roster (for the X/O diagram).
A file is only rewritten when ESPN's data changed, so the repo doesn't grow with copies of the same thing.
data/espn/_stamp.json records when this last ran; the page trusts saved copies for 3 hours after that, then asks ESPN
itself (and falls back to the saved copy if ESPN doesn't answer). Live scoreboards are never served from here.
No keys or secrets: these are ESPN's public endpoints.
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import time

import requests

ROOT = os.getcwd()
OUT = os.path.join(ROOT, "data", "espn")
HOSTS = ("https://site.api.espn.com", "https://site.web.api.espn.com")
UA = {"User-Agent": "Mozilla/5.0 (gaintheedge.io cache)"}
# sport: ESPN path, team-list groups, days ahead to cache, the scoreboard query's extra filters
SPORTS = {
    "cfb": ("football/college-football", ["80", "81"], 7, "&groups=80&limit=400"),
    "nfl": ("football/nfl", [""], 7, "&limit=400"),
    "nba": ("basketball/nba", [""], 2, ""),
    "cbb": ("basketball/mens-college-basketball", ["50"], 2, "&groups=50&limit=500"),
}
SUMMARY_KEEP = ("header", "boxscore", "injuries", "leaders", "gameInfo", "pickcenter", "odds", "format")
stats = {"get": 0, "fail": 0, "write": 0, "same": 0}


def key(q):
    k = re.sub(r"^/apis/site/v2/sports/", "", q)
    k = re.sub(r"[^A-Za-z0-9.\-]+", "_", k).strip("_")
    return os.path.join(OUT, k + ".json")


def get(q):
    for h in HOSTS:
        for k in range(2):
            try:
                stats["get"] += 1
                r = requests.get(h + q, headers=UA, timeout=25)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (400, 404):
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1 + k)
    stats["fail"] += 1
    return None


def save(q, j):
    """Write only when the data changed (keeps the repo small)."""
    p = key(q)
    body = json.dumps(j, separators=(",", ":"), ensure_ascii=False)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            if f.read() == body:
                stats["same"] += 1
                return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    stats["write"] += 1


def have(q):
    return os.path.exists(key(q))


# ------------------------------------------------------------------ trimming (keep what the page reads, drop the rest)
def t_team(t):
    return {k: t.get(k) for k in ("id", "location", "name", "abbreviation", "displayName", "shortDisplayName", "color", "alternateColor")
            if t.get(k) is not None} | {"logos": [{"href": l.get("href"), "rel": l.get("rel")} for l in (t.get("logos") or [])[:3]]}


def t_teams(j):
    lg = ((j.get("sports") or [{}])[0].get("leagues") or [{}])[0]
    return {"sports": [{"leagues": [{"teams": [{"team": t_team(w.get("team") or w)} for w in lg.get("teams") or []]}]}]}


def t_comp(t):
    tm = t.get("team") or {}
    sc = t.get("score")
    if isinstance(sc, dict):
        sc = {k: sc.get(k) for k in ("value", "displayValue")}
    return {"id": t.get("id") or tm.get("id"), "homeAway": t.get("homeAway"), "score": sc, "winner": t.get("winner"),
            "team": {k: tm.get(k) for k in ("id", "abbreviation", "shortDisplayName", "displayName", "location") if tm.get(k) is not None}}


def t_schedule(j):
    ev = []
    for e in j.get("events") or []:
        c = (e.get("competitions") or [{}])[0]
        st = ((c.get("status") or e.get("status") or {}).get("type") or {})
        ev.append({"id": e.get("id"), "date": e.get("date"),
                   "seasonType": {k: (e.get("seasonType") or {}).get(k) for k in ("type", "name")},
                   "competitions": [{"neutralSite": c.get("neutralSite"),
                                     "status": {"type": {"completed": st.get("completed"), "state": st.get("state")}},
                                     "competitors": [t_comp(t) for t in c.get("competitors") or []]}]})
    return {"events": ev}


def t_summary(j):
    return {k: j[k] for k in SUMMARY_KEEP if k in j}


def t_roster(j):
    def ath(a):
        p = a.get("position") or {}
        return {"id": a.get("id"), "displayName": a.get("displayName"), "position": {"abbreviation": p.get("abbreviation"), "name": p.get("name")}}
    out = []
    for g in j.get("athletes") or []:
        if isinstance(g, dict) and "items" in g:
            out.append({"position": g.get("position"), "items": [ath(a) for a in g.get("items") or []]})
        elif isinstance(g, dict):
            out.append(ath(g))
    return {"athletes": out}


# ------------------------------------------------------------------ what each sport needs
def et_today():
    n = dt.datetime.now(dt.timezone.utc)
    y = n.year
    mar = dt.datetime(y, 3, 8, 7, tzinfo=dt.timezone.utc)
    start = mar + dt.timedelta(days=(6 - mar.weekday()) % 7)
    nov = dt.datetime(y, 11, 1, 6, tzinfo=dt.timezone.utc)
    end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7)
    return (n + dt.timedelta(hours=-4 if start <= n < end else -5)).date()


def season(sp, d):
    return (d.year if d.month >= 3 else d.year - 1) if sp in ("cfb", "nfl") else (d.year + 1 if d.month >= 8 else d.year)


def fetch_save(q, trim, skip_if_have=False):
    if skip_if_have and have(q):
        return None
    j = get(q)
    if j is None:
        return None
    t = trim(j) if trim else j
    save(q, t)
    return t


def run_sport(sp, pool):
    path, groups, ahead, sbq = SPORTS[sp]
    today = et_today()
    S = season(sp, today)
    base = f"/apis/site/v2/sports/{path}"
    # team lists (rewritten only if ESPN changed something)
    for g in groups:
        fetch_save(f"{base}/teams?limit=1000" + (f"&groups={g}" if g else ""), t_teams)
    # the games: yesterday through the next few days
    games = {}
    for k in range(-1, ahead + 1):
        d = today + dt.timedelta(days=k)
        j = get(f"{base}/scoreboard?dates={d:%Y%m%d}{sbq}") or {}
        for e in j.get("events") or []:
            c = (e.get("competitions") or [{}])[0]
            side = {t.get("homeAway"): t for t in c.get("competitors") or []}
            if "home" in side and "away" in side:
                games[e["id"]] = (str(side["home"].get("id") or side["home"]["team"]["id"]), str(side["away"].get("id") or side["away"]["team"]["id"]),
                                  bool(((c.get("status") or {}).get("type") or {}).get("completed")))
    teams = sorted({t for h, a, _ in games.values() for t in (h, a)})
    homes = sorted({h for h, a, _ in games.values()})
    jobs = []
    for eid, (h, a, done) in games.items():
        jobs.append((f"{base}/summary?event={eid}", t_summary, done))          # a finished game's summary never changes
    for t in homes:                                                             # head to head: 5 past seasons (saved once) + this one
        for s_ in range(S - 5, S + 1):
            for st in (2, 3):
                jobs.append((f"{base}/teams/{t}/schedule?season={s_}&seasontype={st}", t_schedule, s_ < S))
    for t in teams:
        jobs.append((f"{base}/teams/{t}/schedule?season={S}", t_schedule, False))
        if sp in ("cfb", "nfl"):
            jobs.append((f"{base}/teams/{t}/roster", t_roster, False))
    list(pool.map(lambda x: fetch_save(x[0], x[1], x[2]), jobs))
    # each team's last finished game (box score for starters), and football depth charts
    more = []
    for t in teams:
        sch = key(f"{base}/teams/{t}/schedule?season={S}")
        if os.path.exists(sch):
            ev = [e for e in json.load(open(sch)).get("events", []) if ((e["competitions"][0].get("status") or {}).get("type") or {}).get("completed")]
            if ev:
                last = max(ev, key=lambda e: e.get("date") or "")
                more.append((f"{base}/summary?event={last['id']}", t_summary, True))
        if sp in ("cfb", "nfl"):
            more.append((f"{base}/teams/{t}/depthcharts", "depth", False))

    def depth(q):
        j = get(q)
        dc = j and (j.get("depthchart") or j.get("depthCharts") or j.get("items"))
        save(q, {"depthchart": dc} if dc else {"depthchart": [], "_none": True})   # "_none": the page skips ESPN's slow fallback
    list(pool.map(lambda x: depth(x[0]) if x[1] == "depth" else fetch_save(x[0], x[1], x[2]), more))
    print(f"{sp}: {len(games)} games, {len(teams)} teams")


def prune(days=30):
    """Drop saved game summaries for games more than a month old (schedules and team lists stay)."""
    cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    n = 0
    for f in os.listdir(OUT):
        if "_summary_event_" not in f:
            continue
        p = os.path.join(OUT, f)
        try:
            c = json.load(open(p))["header"]["competitions"][0]
            when = str(c.get("date") or "")
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
        if when and when < cut[:len(when)]:
            os.remove(p)
            n += 1
    return n


if __name__ == "__main__":
    want = [a for a in sys.argv[1:] if a in SPORTS] or list(SPORTS)
    os.makedirs(OUT, exist_ok=True)
    with cf.ThreadPoolExecutor(6) as pool:
        for sp in want:
            try:
                run_sport(sp, pool)
            except Exception as e:  # noqa: BLE001  one sport failing shouldn't stop the others
                print(f"⚠️  {sp}: {e}")
    gone = prune()
    with open(os.path.join(OUT, "_stamp.json"), "w") as f:
        json.dump({"t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}, f)
    print(f"requests {stats['get']} (failed {stats['fail']}), files written {stats['write']}, unchanged {stats['same']}, pruned {gone}")
