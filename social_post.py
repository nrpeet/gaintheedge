"""GainTheEdge social poster: a free pick of the day and a daily recap, on X and Instagram.

Runs on GitHub Actions (.github/workflows/social.yml) from a checkout of the website repository.

    python social/social_post.py pick     # posts the pick you chose on the site today (or, late in the day, a random 3-star+ one)
    python social/social_post.py need     # quick check (no browser): is there a free pick to post right now? prints yes / no
    python social/social_post.py recap    # once a day: yesterday's results in every sport + how the free pick did
    python social/social_post.py test     # builds both images and the post text from today's cards, posts nothing

How it works
  * The full cards are read from the members area (Supabase member_files, with the secret key), the same files members see.
  * Grading uses the website itself: the site is opened in a headless browser with those cards, it pulls ESPN scores and
    grades every bet exactly as members see it (teasers and player props included); this script reads window.gteGrades().
  * You choose the free pick on the site: log in, tap "Free pick" on any of today's bets (only the site owner, listed in
    the Supabase table site_admins, sees that button). It's saved in the table free_pick_choice and posted on the next run.
    If you haven't chosen one by FALLBACK_HOUR (5pm ET; GitHub variable SOCIAL_FALLBACK=none turns this off), a
    random 3-star+ pick that starts at least 30 minutes later is posted instead. It is saved to data/social/free_picks.json (public: the site shows today's free pick and the free-pick
    record), and graded in the next recap. Losses are posted like wins.
  * Images are made from HTML and saved under social/img/ in the repo; Instagram fetches them from the public repo.

Secrets (GitHub repo -> Settings -> Secrets and variables -> Actions). Each one is only ever sent to its own service:
  SUPABASE_URL, SUPABASE_SERVICE_KEY            -> your Supabase project only
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET   -> api.x.com only (leave out to skip X)
  IG_USER_ID, IG_ACCESS_TOKEN                   -> graph.instagram.com / graph.facebook.com only (leave out to skip Instagram)
The Instagram token is kept fresh automatically (it expires after 60 days otherwise) in the Supabase table social_tokens.
"""
import datetime as dt
import functools
import http.server
import json
import os
import random
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import requests

ROOT = os.getcwd()
SOCIAL = os.path.join(ROOT, "data", "social")
IMG = os.path.join(ROOT, "social", "img")
SITE_URL = os.environ.get("SITE_URL", "https://gaintheedge.io").rstrip("/")
SITE_HOST = urllib.parse.urlparse(SITE_URL).netloc or "gaintheedge.io"
SPORTS = {"cfb": ("NCAAF", "data"), "nfl": ("NFL", "data/nfl"), "nba": ("NBA", "data/nba"), "cbb": ("NCAAB", "data/cbb")}
ORDER = ["nfl", "cfb", "nba", "cbb"]
MIN_STARS = 3
LEAD_MIN = 30                         # an automatic free pick must start at least this many minutes after it's posted
CHOSEN_LEAD_MIN = 5                   # the pick you choose is posted as long as it hasn't started (5-minute margin)
FALLBACK_HOUR = 17                    # ET hour after which, with no pick chosen, a random 3-star+ pick is posted
FALLBACK = os.environ.get("SOCIAL_FALLBACK", "random").strip().lower() or "random"     # "random" or "none"
RG = "21+ · Gamble responsibly · 1-800-GAMBLER"


# ------------------------------------------------------------------ time (US Eastern, like the site)
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def et_offset(d):
    y = d.year
    mar = dt.datetime(y, 3, 8, 7, tzinfo=dt.timezone.utc)
    start = mar + dt.timedelta(days=(6 - mar.weekday()) % 7)
    nov = dt.datetime(y, 11, 1, 6, tzinfo=dt.timezone.utc)
    end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7)
    return -4 if start <= d < end else -5


def now_et():
    n = now_utc()
    return (n + dt.timedelta(hours=et_offset(n))).replace(tzinfo=None)


def kick_et(k):
    """The ET wall clock in a kickoff string ('2026-11-09T19:30' or with an offset); None when missing / time TBA."""
    try:
        t = dt.datetime.strptime(str(k)[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return None if (t.hour, t.minute) == (0, 0) else t


def day_name(d, short=False):
    x = dt.date.fromisoformat(d)
    return x.strftime("%a %b %-d") if short else x.strftime("%A, %B %-d")


# ------------------------------------------------------------------ data
def supa_get(path):
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not (url.startswith("https://") and key):
        return None
    hdr = {"apikey": key}
    if key.count(".") == 2:
        hdr["Authorization"] = f"Bearer {key}"
    r = requests.get(f"{url}/rest/v1/member_files", params={"select": "body", "path": f"eq.{path}"}, headers=hdr, timeout=60)
    r.raise_for_status()
    rows = r.json()
    return rows[0]["body"] if rows else None


def _supa():
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not (url.startswith("https://") and key):
        return None, None
    hdr = {"apikey": key, "Content-Type": "application/json"}
    if key.count(".") == 2:
        hdr["Authorization"] = f"Bearer {key}"
    return url, hdr


def chosen(day):
    """The free pick you picked on the site for this ET day (Supabase free_pick_choice), or None."""
    url, hdr = _supa()
    if not url:
        return None
    r = requests.get(f"{url}/rest/v1/free_pick_choice", params={"select": "*", "day": f"eq.{day}"}, headers=hdr, timeout=30)
    if not r.ok:
        print(f"   (couldn't read free_pick_choice: {r.status_code}; has supabase_freepick.sql been run?)")
        return None
    rows = r.json()
    return rows[0] if rows else None


def mark_posted(day, note=""):
    url, hdr = _supa()
    if url:
        requests.patch(f"{url}/rest/v1/free_pick_choice", params={"day": f"eq.{day}"}, headers=hdr,
                       json={"posted_at": now_utc().isoformat(), "note": note}, timeout=30)


def card(path):
    """A card's full data: the members copy when there is one, else the repo copy if it isn't a locked preview."""
    full = supa_get(path)
    if full:
        return full
    p = os.path.join(ROOT, path)
    if os.path.exists(p):
        d = json.load(open(p))
        if not d.get("members_only"):
            return d
    return None


def index_of(sp):
    p = os.path.join(ROOT, SPORTS[sp][1], "index.json")
    return json.load(open(p)) if os.path.exists(p) else None


def cards_for(day):
    """(sport, card id, path) for every card that can hold bets on this ET day."""
    out = []
    for sp in ORDER:
        idx = index_of(sp)
        if not idx:
            continue
        base = SPORTS[sp][1]
        if sp in ("nba", "cbb"):
            if any(x.get("id") == day for x in idx.get("days", [])):
                out.append((sp, day, f"{base}/{day}.json"))
        else:                                     # football: the newest two weeks; bets are filtered by kickoff day later
            for w in sorted(idx.get("weeks", []), key=lambda w: w["id"], reverse=True)[:2]:
                out.append((sp, w["id"], f"{base}/{w['id']}.json"))
    return out


def load_json(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=1)


# ------------------------------------------------------------------ grading with the site itself
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def graded(jobs, wait_day=None, timeout=120):
    """jobs: [(sport, card id, full data)]. Opens the site on a local copy holding those cards and returns
    {(sport, card id): [bet dicts from window.gteGrades()]}. With wait_day, waits until that day's bets are settled."""
    from playwright.sync_api import sync_playwright
    tmp = tempfile.mkdtemp()
    shutil.copy(os.path.join(ROOT, "index.html"), tmp)
    out = {}
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for sp, cid, d in jobs:
            base = os.path.join(tmp, SPORTS[sp][1])
            os.makedirs(base, exist_ok=True)
            json.dump(d, open(os.path.join(base, f"{cid}.json"), "w"))
            key = "days" if sp in ("nba", "cbb") else "weeks"
            json.dump({"current": cid, key: [{"id": cid}]}, open(os.path.join(base, "index.json"), "w"))
            handler = functools.partial(_Quiet, directory=tmp)
            with socketserver.TCPServer(("127.0.0.1", 0), handler) as srv:
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                ctx = br.new_context()
                ctx.add_init_script(f"try {{ localStorage.setItem('gte.sport', '{sp}'); localStorage.setItem('gte.view', 'week'); }} catch (e) {{}}")
                ctx.route("**/*supabase*", lambda r: r.abort())        # no log-in needed: the page reads the local cards
                fx = os.environ.get("SOCIAL_ESPN_FIXTURE")              # tests only: canned ESPN scores
                if fx:
                    ctx.route(lambda u: "espn.com" in u, lambda r: r.fulfill(content_type="application/json", body=open(fx).read()))
                pg = ctx.new_page()
                pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/index.html", wait_until="domcontentloaded")
                t0, res = time.time(), None
                while time.time() - t0 < timeout:
                    time.sleep(3)
                    res = pg.evaluate("window.gteGrades && window.gteGrades()")
                    if not res or res.get("id") != cid:
                        continue
                    todo = [b for b in res["bets"] if (not wait_day or str(b.get("kickoff", ""))[:10] == wait_day)
                            and b.get("stake") and b["r"] not in ("W", "L", "P")]
                    if res.get("scores") and not todo:
                        break
                    if not wait_day and res.get("bets") is not None and time.time() - t0 > 12:
                        break
                ctx.close()
                srv.shutdown()
            out[(sp, cid)] = (res or {}).get("bets", [])
        br.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return out


# ------------------------------------------------------------------ images
FONTS = ("https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@800;900&family=Archivo:wght@500;700;800"
         "&family=JetBrains+Mono:wght@700&display=swap")


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def frame(body):
    return f"""<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="{FONTS}"><style>
*{{box-sizing:border-box;margin:0}} body{{width:1080px;height:1350px;background:#090d18;color:#f2f4fa;font-family:Archivo,'Helvetica Neue',Arial,sans-serif;overflow:hidden;position:relative}}
.str{{position:absolute;inset:0 0 0 45%;background:repeating-linear-gradient(115deg,rgba(255,255,255,.045) 0 3px,transparent 3px 30px);-webkit-mask-image:linear-gradient(90deg,transparent,#000 40%)}}
.wrap{{position:relative;padding:84px 80px;height:100%;display:flex;flex-direction:column}}
.logo{{font-family:'Big Shoulders Display','Arial Narrow','Helvetica Neue',Arial,sans-serif;font-weight:900;font-style:italic;text-transform:uppercase;font-size:46px;display:flex;align-items:center}}
.logo i{{display:inline-block;width:12px;height:36px;background:#ff6b2c;transform:skewX(-18deg);margin:0 12px 0 8px}} .logo em{{color:#ff6b2c}}
.eb{{margin-top:70px;font-weight:800;letter-spacing:.16em;text-transform:uppercase;color:#ff6b2c;font-size:26px}}
h1{{font-family:'Big Shoulders Display','Arial Narrow','Helvetica Neue',Arial,sans-serif;font-weight:900;font-style:italic;text-transform:uppercase;font-size:112px;line-height:.9;margin-top:14px}}
.card{{margin-top:44px;background:#121a2d;border:2px solid rgba(255,255,255,.08);border-radius:28px;padding:40px 44px}}
.k{{font-size:22px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:#98a3bd}}
.pick{{font-family:'Big Shoulders Display','Arial Narrow','Helvetica Neue',Arial,sans-serif;font-weight:900;text-transform:uppercase;font-size:92px;line-height:.95;margin:12px 0 8px}}
.game{{font-size:30px;color:#98a3bd}} .stars{{color:#ffc53d;font-size:40px;letter-spacing:4px}} .off{{color:#2a3550}}
.row{{display:flex;gap:18px;margin-top:26px;flex-wrap:wrap}} .chip{{background:#18223a;border-radius:14px;padding:14px 20px;font-size:26px;font-weight:700}}
.mono{{font-family:'JetBrains Mono',monospace;font-weight:700;letter-spacing:-.02em}}
.tbl{{margin-top:36px;display:grid;gap:14px}} .tr{{display:grid;grid-template-columns:1fr auto auto;gap:30px;align-items:baseline;background:#121a2d;border-radius:20px;padding:26px 32px;font-size:34px}}
.tr b{{font-family:'Big Shoulders Display','Arial Narrow','Helvetica Neue',Arial,sans-serif;font-weight:900;font-size:44px;letter-spacing:.04em}} .up{{color:#2fd27a}} .down{{color:#ff4d5e}} .mut{{color:#98a3bd}}
.foot{{margin-top:auto;display:flex;justify-content:space-between;align-items:flex-end;font-size:26px;color:#98a3bd}} .foot b{{color:#f2f4fa;font-size:34px}}
.bar{{position:absolute;left:0;right:0;bottom:0;height:10px;background:linear-gradient(90deg,#ff6b2c 0 22%,transparent 22% 23%,#ff6b2c 23% 24%,transparent 24%)}}
</style></head><body><div class="str"></div><div class="wrap"><div class="logo">Gain<i></i>The<em>Edge</em></div>{body}
<div class="foot"><div><b>{esc(SITE_HOST)}</b><br>Every bet graded in public</div><div>{esc(RG)}</div></div></div><div class="bar"></div></body></html>"""


def stars_html(n):
    n = max(0, min(5, int(n or 0)))
    return "★" * n + f'<span class="off">{"★" * (5 - n)}</span>'


def pick_html(p, day):
    k = kick_et(p.get("kickoff"))
    when = k.strftime("%-I:%M %p ET") if k else "Today"
    chips = [f'<span class="chip">{esc(SPORTS[p["sport"]][0])}</span>', f'<span class="chip">{esc(when)}</span>',
             f'<span class="chip mono">{p["stake"]:g}u</span>']
    if p.get("price") not in (None, ""):
        chips.append(f'<span class="chip mono">{int(p["price"]):+d}</span>')
    su = ", ".join(p.get("setups") or [])
    return frame(f"""<div class="eb">{esc(day_name(day))}</div><h1>Free pick<br>of the day</h1>
<div class="card"><div class="k">The pick</div><div class="pick">{esc(p["pick"])}</div><div class="game">{esc(p["game"])}</div>
<div class="row"><span class="stars">{stars_html(p["stars"])}</span></div><div class="row">{"".join(chips)}</div>
{f'<div class="row"><span class="game">Setup: {esc(su)}</span></div>' if su else ""}</div>""")


def recap_html(day, rows, fp):
    tr = "".join(f'<div class="tr"><b>{esc(SPORTS[sp][0])}</b><span class="mono">{r["w"]}–{r["l"]}{"–" + str(r["p"]) if r["p"] else ""}</span>'
                 f'<span class="mono {"up" if r["u"] > 0 else "down" if r["u"] < 0 else "mut"}">{r["u"]:+.2f}u</span></div>' for sp, r in rows)
    tw, tl, tp, tu = (sum(r[k] for _, r in rows) for k in ("w", "l", "p", "u"))
    tot = (f'<div class="tr" style="background:#18223a"><b>All</b><span class="mono">{tw}–{tl}{"–" + str(tp) if tp else ""}</span>'
           f'<span class="mono {"up" if tu > 0 else "down" if tu < 0 else "mut"}">{tu:+.2f}u</span></div>') if len(rows) > 1 else ""
    f = ""
    if fp and fp.get("result") in ("W", "L", "P"):
        lab = {"W": '<span class="up">Won</span>', "L": '<span class="down">Lost</span>', "P": '<span class="mut">Push</span>'}[fp["result"]]
        f = f'<div class="card" style="margin-top:30px;padding:28px 36px"><div class="k">Free pick</div><div class="game" style="color:#f2f4fa;font-size:36px;margin-top:8px">{esc(fp["pick"])} · {lab}{(" · " + esc(fp["final"])) if fp.get("final") else ""}</div></div>'
    return frame(f"""<div class="eb">{esc(day_name(day))}</div><h1>Daily<br>recap</h1><div class="tbl">{tr}{tot}</div>{f}""")


def render(html, path):
    from playwright.sync_api import sync_playwright
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page(viewport={"width": 1080, "height": 1350})
        pg.set_content(html, wait_until="networkidle")
        pg.evaluate("document.fonts.ready.then(() => document.fonts.status)")
        pg.wait_for_timeout(300)
        pg.screenshot(path=path)
        br.close()
    return path


# ------------------------------------------------------------------ posting
def git_push(msg):
    """Commit the social files to the site repo (Instagram needs the image at a public URL). Returns the commit sha."""
    subprocess.run(["git", "add", "data/social", "social/img"], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-c", "user.name=GainTheEdge bot", "-c", "user.email=bot@users.noreply.github.com", "commit", "-qm", msg], check=True)
    for k in range(4):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            break
        subprocess.run(["git", "pull", "-q", "--rebase"], check=False)
        time.sleep(3 * (k + 1))
    else:
        raise RuntimeError("couldn't push the social files to the repo")
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def x_on():
    return all(os.environ.get(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"))


def post_x(text, img):
    from requests_oauthlib import OAuth1
    auth = OAuth1(os.environ["X_API_KEY"], os.environ["X_API_SECRET"], os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_SECRET"])
    with open(img, "rb") as f:
        r = requests.post("https://api.x.com/2/media/upload", auth=auth, files={"media": ("card.png", f, "image/png")},
                          data={"media_category": "tweet_image"}, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"X image upload failed ({r.status_code}): {r.text[:300]}")
    j = r.json()
    mid = (j.get("data") or {}).get("id") or j.get("media_id_string")
    r = requests.post("https://api.x.com/2/tweets", auth=auth, json={"text": text, "media": {"media_ids": [str(mid)]}}, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"X post failed ({r.status_code}): {r.text[:300]}")
    return r.json()["data"]["id"]


def ig_on():
    return bool(os.environ.get("IG_USER_ID") and (os.environ.get("IG_ACCESS_TOKEN") or os.environ.get("SUPABASE_SERVICE_KEY")))


def ig_token():
    """The Instagram token from Supabase (social_tokens), refreshed weekly so it never hits its 60-day expiry. The first
    time, it comes from the IG_ACCESS_TOKEN secret."""
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
    hdr = {"apikey": key, "Content-Type": "application/json"}
    if key.count(".") == 2:
        hdr["Authorization"] = f"Bearer {key}"
    row = None
    if url and key:
        r = requests.get(f"{url}/rest/v1/social_tokens", params={"select": "token,refreshed_at", "name": "eq.instagram"}, headers=hdr, timeout=30)
        if r.ok and r.json():
            row = r.json()[0]
    tok = (row or {}).get("token") or os.environ.get("IG_ACCESS_TOKEN", "")
    if not tok:
        raise RuntimeError("no Instagram token: add the IG_ACCESS_TOKEN secret")
    age = None
    if row and row.get("refreshed_at"):
        age = now_utc() - dt.datetime.fromisoformat(row["refreshed_at"].replace("Z", "+00:00"))
    if tok.startswith("IG") and (age is None or age > dt.timedelta(days=7)):
        r = requests.get("https://graph.instagram.com/refresh_access_token", params={"grant_type": "ig_refresh_token", "access_token": tok}, timeout=30)
        if r.ok and r.json().get("access_token"):
            tok = r.json()["access_token"]
            if url and key:
                requests.post(f"{url}/rest/v1/social_tokens?on_conflict=name", headers=dict(hdr, Prefer="resolution=merge-duplicates"),
                              json={"name": "instagram", "token": tok, "refreshed_at": now_utc().isoformat()}, timeout=30)
        else:
            print(f"   (Instagram token not refreshed: {r.status_code} {r.text[:200]})")
    return tok


def post_ig(caption, image_url):
    tok, uid = ig_token(), os.environ["IG_USER_ID"]
    host = "https://graph.instagram.com/v23.0" if tok.startswith("IG") else "https://graph.facebook.com/v23.0"
    r = requests.post(f"{host}/{uid}/media", data={"image_url": image_url, "caption": caption, "access_token": tok}, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Instagram upload failed ({r.status_code}): {r.text[:300]}")
    cid = r.json()["id"]
    for _ in range(20):
        s = requests.get(f"{host}/{cid}", params={"fields": "status_code", "access_token": tok}, timeout=30).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram couldn't process the image: {s}")
        time.sleep(3)
    r = requests.post(f"{host}/{uid}/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Instagram publish failed ({r.status_code}): {r.text[:300]}")
    return r.json()["id"]


def publish(text, caption, img, dry):
    """Posts to each platform that's set up. Returns {"x": id, "ig": id} (errors are printed, not raised: one platform
    failing shouldn't stop the other)."""
    out = {}
    if dry:
        print("\n--- X ---\n" + text + "\n\n--- Instagram ---\n" + caption + f"\n\n(image: {img}; test mode: nothing posted)")
        return out
    sha = git_push(f"Social: {os.path.basename(img)}")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if x_on():
        try:
            out["x"] = post_x(text, img)
            print(f"✓ posted on X: {out['x']}")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  X: {e}")
    else:
        print("   X not set up (secrets missing): skipped")
    if ig_on() and repo:
        try:
            rel = os.path.relpath(img, ROOT).replace(os.sep, "/")
            out["ig"] = post_ig(caption, f"https://raw.githubusercontent.com/{repo}/{sha}/{rel}")
            print(f"✓ posted on Instagram: {out['ig']}")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  Instagram: {e}")
    else:
        print("   Instagram not set up (secrets missing): skipped")
    return out


# ------------------------------------------------------------------ jobs
def free_record(log):
    w = sum(1 for x in log if x.get("result") == "W")
    l_ = sum(1 for x in log if x.get("result") == "L")
    return w, l_


def do_pick(dry=False):
    day, now = now_et().date().isoformat(), now_et()
    path = os.path.join(SOCIAL, "free_picks.json")
    log = load_json(path, [])
    if any(x.get("date") == day for x in log) and not dry:
        print(f"Free pick for {day} already posted.")
        return
    ch = chosen(day)
    if ch and ch.get("posted_at") and not dry:
        print(f"Your free pick for {day} was already posted.")
        return
    if ch:                                                   # the pick you chose on the site
        p = None
        d = card(f"{SPORTS[ch['sport']][1]}/{ch['card']}.json") if ch.get("sport") in SPORTS else None
        if d and isinstance(d.get("bets"), list):
            for b in graded([(ch["sport"], ch["card"], d)]).get((ch["sport"], ch["card"]), []):
                if str(b.get("id")) == str(ch["bet_id"]):
                    p = dict(b, sport=ch["sport"], card=ch["card"])
        k = kick_et((p or ch).get("kickoff"))
        if not p:
            print(f"⚠️  The free pick you chose ({ch.get('pick')}, {ch.get('game')}) isn't on today's card any more; nothing posted. Choose another on the site.")
            return
        if p.get("r") != "open" or (k and k <= now + dt.timedelta(minutes=CHOSEN_LEAD_MIN)):
            print(f"⚠️  The free pick you chose ({p['pick']}) has already started; nothing posted. Choose another on the site.")
            return
        how = "chosen"
    else:
        if FALLBACK != "random" or (now.hour < FALLBACK_HOUR and not dry):
            print(f"No free pick chosen on the site for {day} yet" + (f"; at {FALLBACK_HOUR % 12}pm ET a random {MIN_STARS}-star+ pick is posted instead."
                  if FALLBACK == "random" else " (automatic picks are off)."))
            return
        jobs = []
        for sp, cid, path_ in cards_for(day):
            d = card(path_)
            if d and isinstance(d.get("bets"), list):
                jobs.append((sp, cid, d))
        G = graded(jobs) if jobs else {}
        cands = []
        for (sp, cid), bets in G.items():
            for b in bets:
                k = kick_et(b.get("kickoff"))
                if (str(b.get("kickoff", ""))[:10] == day and k and k > now + dt.timedelta(minutes=LEAD_MIN) and (b.get("stake") or 0) > 0
                        and (b.get("stars") or 0) >= MIN_STARS and b.get("market") != "Teaser" and b.get("r") == "open"):
                    cands.append(dict(b, sport=sp, card=cid))
        if not cands:
            print(f"No pick chosen and no {MIN_STARS}-star+ pick left to start today ({day}); the next run tries again.")
            return
        cands.sort(key=lambda b: (b["sport"], b["id"]))
        p = random.Random(f"gte-free-{day}").choice(cands)  # random, but the same pick if the job runs twice
        how = "auto"
    entry = {"date": day, "sport": p["sport"], "card": p["card"], "id": p["id"], "game": p["game"], "pick": p["pick"],
             "market": p["market"], "stars": p["stars"], "stake": p["stake"], "price": p.get("price"), "setups": p.get("setups"),
             "kickoff": p["kickoff"], "result": None, "units": None, "final": "", "posted": {}, "how": how}
    img = render(pick_html(entry, day), os.path.join(IMG, f"{day}-pick.png"))
    w, l_ = free_record(log)
    k = kick_et(entry["kickoff"])
    rec = f"Free picks so far: {w}–{l_}\n" if w + l_ else ""
    px = entry.get("price")
    shown = entry["pick"] + (f" ({int(px):+d})" if px not in (None, "") and entry["market"] == "Moneyline" and str(int(px)) not in entry["pick"] else "")
    text = (f"🎯 FREE PICK · {day_name(day, True)}\n\n{SPORTS[entry['sport']][0]}: {shown}\n{entry['game']}"
            f"{' · ' + k.strftime('%-I:%M %p ET') if k else ''}\n{'★' * int(entry['stars'])} · {entry['stake']:g}u\n\n{rec}"
            f"Every pick we make is graded in public, wins and losses: {SITE_URL}/?utm_source=x\n\n{RG}")
    caption = (f"🎯 Free pick of the day: {shown} ({SPORTS[entry['sport']][0]}, {entry['game']}).\n\n{rec}"
               f"Members get every pick in NCAAF, NFL, NBA and NCAAB, each one tested on past seasons first. Every bet is graded in "
               f"public on our site: link in bio.\n\n{RG}\n\n#sportsbetting #freepick #{SPORTS[entry['sport']][0].lower()} #bettingtips")
    entry["posted"] = publish(text, caption, img, dry)
    if not dry:
        # Nov 12, 2026 (outside audit): a free pick enters the public free-pick record only once it was actually posted
        # somewhere (X and/or Instagram returned a post id). If every platform failed, nothing is recorded and the next run
        # tries again.
        if not entry["posted"]:
            print(f"⚠️  The free pick for {day} wasn't posted anywhere, so it is NOT added to the free-pick record. The next run retries.")
            return
        entry["posted_at"] = now_utc().isoformat()
        log.append(entry)
        save_json(path, log)
        git_push(f"Free pick {day}")
        if how == "chosen":
            mark_posted(day, ", ".join(f"{k}:{v}" for k, v in entry["posted"].items()))


RECAP_WAIT_UNTIL = 15        # ET hour: after this the recap posts even if a bet is still ungraded (it says how many)


def do_recap(dry=False, day=None):
    day = day or (now_et().date() - dt.timedelta(days=1)).isoformat()
    rpath, fpath = os.path.join(SOCIAL, "recaps.json"), os.path.join(SOCIAL, "free_picks.json")
    recaps, log = load_json(rpath, {}), load_json(fpath, [])
    fp0 = next((x for x in log if x.get("date") == day), None)
    if day in recaps and not dry and not (fp0 and fp0.get("result") is None):
        print(f"Recap for {day} already posted.")
        return
    jobs = []
    for sp, cid, p in cards_for(day):
        d = card(p)
        if d and isinstance(d.get("bets"), list) and any(str(b.get("kickoff", ""))[:10] == day for b in d["bets"]):
            jobs.append((sp, cid, d))
    G = graded(jobs, wait_day=day) if jobs else {}
    per, pending = {}, 0
    for (sp, cid), bets in G.items():
        for b in bets:
            if str(b.get("kickoff", ""))[:10] != day or not (b.get("stake") or 0) > 0:
                continue
            r = per.setdefault(sp, {"w": 0, "l": 0, "p": 0, "u": 0.0})
            if b["r"] in ("W", "L", "P"):
                r[b["r"].lower()] += 1
                r["u"] += float(b.get("units") or 0)
            else:
                pending += 1
    fp = next((x for x in log if x.get("date") == day), None)
    if fp and fp.get("result") is None:
        for b in G.get((fp["sport"], fp["card"]), []):
            if b["id"] == fp["id"] and b["r"] in ("W", "L", "P"):
                fp.update(result=b["r"], units=round(float(b.get("units") or 0), 2), final=b.get("final") or "")
    rows = [(sp, per[sp]) for sp in ORDER if sp in per and per[sp]["w"] + per[sp]["l"] + per[sp]["p"]]
    if day in recaps and not dry:                      # recap already out: this run only finished grading the free pick
        save_json(fpath, log)
        git_push(f"Free pick result {day}")
        return
    if pending and not dry and now_et().hour < RECAP_WAIT_UNTIL:
        # Nov 12, 2026 (outside audit): don't post a recap with games still ungraded (the record would leave them out for
        # good). Wait for the next run; after RECAP_WAIT_UNTIL (ET) post anyway and say how many are still pending.
        print(f"{pending} bet(s) from {day} not graded yet: waiting (recap posts once they are, or after {RECAP_WAIT_UNTIL}:00 ET).")
        save_json(fpath, log)
        return
    if not rows:
        print(f"No settled bets on {day}: no recap.")
        if fp and not dry:
            save_json(fpath, log)
        return
    img = render(recap_html(day, rows, fp), os.path.join(IMG, f"{day}-recap.png"))
    lines = [f"{SPORTS[sp][0]} {r['w']}–{r['l']}{'–' + str(r['p']) if r['p'] else ''} ({r['u']:+.2f}u)" for sp, r in rows]
    tw, tl, tu = sum(r["w"] for _, r in rows), sum(r["l"] for _, r in rows), sum(r["u"] for _, r in rows)
    fl = ""
    if fp and fp.get("result"):
        fl = f"\nFree pick: {fp['pick']} {'✅' if fp['result'] == 'W' else '❌' if fp['result'] == 'L' else '➖'}"
    w, l_ = free_record(log)
    text = (f"📊 {day_name(day, True)} results\n\n" + "\n".join(lines) + (f"\nAll: {tw}–{tl} ({tu:+.2f}u)" if len(rows) > 1 else "") +
            fl + (f"\nFree picks: {w}–{l_}" if w + l_ else "") + (f"\n({pending} still pending)" if pending else "") +
            f"\n\nEvery bet, wins and losses: {SITE_URL}/?utm_source=x\n\n{RG}")
    caption = (f"📊 {day_name(day)}: " + " · ".join(lines) + "." + fl + f"\n\nWe post every result, good days and bad. "
               f"Full record and today's picks: link in bio.\n\n{RG}\n\n#sportsbetting #bettingrecord #sportspicks")
    if len(text) > 280:                                # X's limit: drop the per-sport lines, keep the total
        text = (f"📊 {day_name(day, True)}: {tw}–{tl} ({tu:+.2f}u) across {len(rows)} sports{fl}\n\nEvery bet, wins and losses: {SITE_URL}/?utm_source=x\n\n{RG}")
    posted = publish(text, caption, img, dry)
    if not dry:
        recaps[day] = {"rows": {sp: dict(r, u=round(r["u"], 2)) for sp, r in rows}, "pending": pending, "posted": posted, "at": now_utc().isoformat()}
        save_json(rpath, recaps)
        save_json(fpath, log)
        git_push(f"Recap {day}")


def need_pick():
    """Without a browser: is a free pick waiting to be posted? (already posted today -> no; chosen -> yes; late -> yes)"""
    day, now = now_et().date().isoformat(), now_et()
    if any(x.get("date") == day for x in load_json(os.path.join(SOCIAL, "free_picks.json"), [])):
        return False
    ch = chosen(day)
    if ch:
        return not ch.get("posted_at")
    return FALLBACK == "random" and now.hour >= FALLBACK_HOUR


if __name__ == "__main__":
    job = sys.argv[1] if len(sys.argv) > 1 else "pick"
    if job == "need":
        yes = need_pick()
        print("yes" if yes else "no")
        if os.environ.get("GITHUB_OUTPUT"):
            open(os.environ["GITHUB_OUTPUT"], "a").write(f"run={'yes' if yes else 'no'}\n")
    elif job == "pick":
        do_pick()
    elif job == "recap":
        do_recap(day=sys.argv[2] if len(sys.argv) > 2 else None)
    elif job == "test":
        do_pick(dry=True)
        do_recap(dry=True, day=sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        sys.exit("use: pick | need | recap | test")
