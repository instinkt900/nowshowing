"""nowshowing: a read-only page of what's coming up and what's recently arrived.

Data comes only from Sonarr and Radarr (one or more instances). A background thread
refreshes it every REFRESH_MINUTES; requests are served from memory. API keys never
leave the server: posters are linked from TMDB/TVDB directly.

Stdlib only. Run: python3 app.py  (reads .env in the working directory if present)
"""

import html
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo


# --- config -----------------------------------------------------------------

def load_dotenv(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_dotenv()

TZ = ZoneInfo(os.environ.get("TZ_NAME") or os.environ.get("TZ") or "Australia/Melbourne")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "14"))
DAYS_BACK = int(os.environ.get("DAYS_BACK", "14"))
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "15"))
PORT = int(os.environ.get("PORT", "8077"))
TITLE = os.environ.get("SITE_TITLE", "Now Showing")
REQUEST_URL = os.environ.get("REQUEST_URL", "")  # e.g. Seerr link shown in the header


def instances(prefix):
    """SONARR_URL/SONARR_KEY, plus any SONARR_<NAME>_URL/_KEY."""
    out = []
    for k, url in sorted(os.environ.items()):
        if k.startswith(prefix) and k.endswith("_URL"):
            key = os.environ.get(k[:-4] + "_KEY")
            if key:
                out.append({"name": k[:-4].lower(), "url": url.rstrip("/"), "key": key})
    return out


SONARRS = instances("SONARR")
RADARRS = instances("RADARR")


# --- arr client -------------------------------------------------------------

def get(inst, path, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{inst['url']}/api/v3/{path}?{q}",
                                 headers={"X-Api-Key": inst["key"], "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


def poster(images):
    for img in images or []:
        if img.get("coverType") == "poster" and img.get("remoteUrl"):
            u = img["remoteUrl"]
            if "image.tmdb.org/t/p/original/" in u:
                return u.replace("/original/", "/w342/")
            if "artworks.thetvdb.com" in u and "_t." not in u:
                base, _, ext = u.rpartition(".")
                return f"{base}_t.{ext}"
            return u
    return None


def is_4k(name):
    return any(t in (name or "").lower() for t in ("2160", "4k", "uhd"))


def movie_info(m):
    links = []
    if m.get("imdbId"):
        links.append(("IMDb", f"https://www.imdb.com/title/{m['imdbId']}/"))
    if m.get("tmdbId"):
        links.append(("Letterboxd", f"https://letterboxd.com/tmdb/{m['tmdbId']}/"))
        links.append(("TMDB", f"https://www.themoviedb.org/movie/{m['tmdbId']}"))
    return {
        "kind": "movie", "title": m.get("title"), "year": m.get("year"), "poster": poster(m.get("images")),
        "overview": m.get("overview"), "runtime": m.get("runtime"), "genres": m.get("genres") or [],
        "cert": m.get("certification"), "links": links,
    }


def series_info(s):
    links = []
    if s.get("imdbId"):
        links.append(("IMDb", f"https://www.imdb.com/title/{s['imdbId']}/"))
    if s.get("tvdbId"):
        links.append(("TVDB", f"https://thetvdb.com/dereferrer/series/{s['tvdbId']}"))
    if s.get("tmdbId"):
        links.append(("TMDB", f"https://www.themoviedb.org/tv/{s['tmdbId']}"))
    return {
        "kind": "tv", "title": s.get("title"), "year": s.get("year"), "poster": poster(s.get("images")),
        "overview": s.get("overview"), "network": s.get("network"), "genres": s.get("genres") or [],
        "cert": s.get("certification"), "status": s.get("status"), "links": links,
    }


def episode(e, when=None):
    return {"season": e["seasonNumber"], "ep": e["episodeNumber"], "title": e.get("title"),
            "overview": e.get("overview"), "when": when or parse_dt(e.get("airDateUtc")),
            "finale": e.get("finaleType")}


# --- data gathering ---------------------------------------------------------

def gather():
    now = datetime.now(TZ)
    since = now - timedelta(days=DAYS_BACK)
    since_utc = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    start, end = now.date() - timedelta(days=1), now.date() + timedelta(days=DAYS_AHEAD + 1)
    pool = ThreadPoolExecutor(8)

    # --- upcoming episodes, same show + same day merged into one item
    upcoming, by_key = [], {}
    for inst in SONARRS:
        for e in get(inst, "calendar", start=start.isoformat(), end=end.isoformat(), includeSeries="true"):
            when = parse_dt(e.get("airDateUtc"))
            if not when or when < now - timedelta(hours=3) or when.date() > end \
                    or e.get("hasFile") or not e.get("monitored"):
                continue
            s = e.get("series") or {}
            k = (inst["name"], s.get("id"), when.date())
            if k not in by_key:
                by_key[k] = {**series_info(s), "when": when, "episodes": []}
                upcoming.append(by_key[k])
            by_key[k]["episodes"].append(episode(e, when))

    # --- upcoming movies (digital / physical release only: that's when it can arrive)
    movies_up = {}
    for inst in RADARRS:
        for m in get(inst, "calendar", start=start.isoformat(), end=end.isoformat()):
            if m.get("hasFile") or not m.get("monitored"):
                continue
            for field, label in (("digitalRelease", "Digital release"), ("physicalRelease", "Disc release")):
                when = parse_dt(m.get(field))
                if when and now.date() <= when.date() <= end:
                    movies_up.setdefault(m["tmdbId"], {**movie_info(m), "when": when, "release": label})
                    break
    upcoming += movies_up.values()

    # --- recently imported episodes; drop upgrades (episode first imported before the window)
    recent_eps, shows = {}, {}
    for inst in SONARRS:
        for h in get(inst, "history/since", date=since_utc, eventType="downloadFolderImported",
                     includeSeries="true", includeEpisode="true"):
            s, e = h.get("series") or {}, h.get("episode") or {}
            if not s or not e:
                continue
            shows[(inst["name"], s["id"])] = (inst, s)
            k = (inst["name"], s["id"], e["id"])
            when = parse_dt(h["date"])
            if k not in recent_eps or when < recent_eps[k]["imported"]:
                recent_eps[k] = {**episode(e), "imported": when}

    def first_imports(item):
        (iname, sid), (inst, _) = item
        first = {}
        for r in get(inst, "history/series", seriesId=sid, eventType=3):
            when = parse_dt(r["date"])
            if r["episodeId"] not in first or when < first[r["episodeId"]]:
                first[r["episodeId"]] = when
        return iname, sid, first

    firsts = {}
    for iname, sid, first in pool.map(first_imports, shows.items()):
        for eid, when in first.items():
            firsts[(iname, sid, eid)] = when

    tv_recent = {}
    for (iname, sid, eid), r in recent_eps.items():
        if firsts.get((iname, sid, eid), r["imported"]) < since:
            continue  # upgrade of something we already had
        g = tv_recent.setdefault((iname, sid), {**series_info(shows[(iname, sid)][1]), "episodes": []})
        g["episodes"].append(r)
    for g in tv_recent.values():
        g["episodes"].sort(key=lambda r: (r["season"], r["ep"]))
        g["when"] = max(r["imported"] for r in g["episodes"])

    # --- recently imported movies, merged across instances, drop upgrades
    mov_recent = {}
    for inst in RADARRS:
        for h in get(inst, "history/since", date=since_utc, eventType="downloadFolderImported", includeMovie="true"):
            m = h.get("movie") or {}
            if not m:
                continue
            rec = mov_recent.setdefault(m["tmdbId"], {**movie_info(m), "when": None, "qualities": set()})
            when = parse_dt(h["date"])
            rec["when"] = min(filter(None, [rec["when"], when]))
            rec["qualities"].add("4K" if is_4k(h["quality"]["quality"]["name"]) else "HD")

    def earliest_anywhere(tmdb):
        best = None
        for inst in RADARRS:
            found = get(inst, "movie", tmdbId=tmdb)
            if found:
                for r in get(inst, "history/movie", movieId=found[0]["id"], eventType=3):
                    when = parse_dt(r["date"])
                    best = when if best is None or when < best else best
        return tmdb, best

    for tmdb, first in pool.map(earliest_anywhere, list(mov_recent)):
        if first and first < since:
            del mov_recent[tmdb]

    pool.shutdown()
    upcoming.sort(key=lambda x: x["when"])
    recent = sorted([*tv_recent.values(), *mov_recent.values()], key=lambda x: x["when"], reverse=True)
    return {"upcoming": upcoming, "recent": recent, "updated": now}


# --- rendering --------------------------------------------------------------

e = html.escape


def ep_code(season, ep):
    return f"S{season:02d}E{ep:02d}"


def eps_summary(eps):
    by_season = {}
    for r in eps:
        by_season.setdefault(r["season"], []).append(r["ep"])
    seasons = sorted(by_season)
    if len(eps) > 12:  # whole-season / whole-series drops: keep it short
        if len(seasons) == 1:
            return f"Season {seasons[0]}"
        if seasons == list(range(seasons[0], seasons[-1] + 1)):
            return f"Seasons {seasons[0]}–{seasons[-1]}"
        return f"{len(seasons)} seasons"
    parts = []
    for s in seasons:
        nums = sorted(set(by_season[s]))
        if len(nums) == 1:
            parts.append(ep_code(s, nums[0]))
        elif nums == list(range(nums[0], nums[-1] + 1)):
            parts.append(f"{ep_code(s, nums[0])}–E{nums[-1]:02d}")
        else:
            parts.append(f"S{s:02d} · {len(nums)} episodes")
    return ", ".join(parts)


def fmt_time(dt):
    return dt.strftime("%-I:%M %p").lower()


def fmt_runtime(m):
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def day_label(d, today):
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if 1 < delta < 7:
        return d.strftime("%A")
    return "Next " + d.strftime("%A") if delta < 14 else d.strftime("%A")


def ago_bucket(d, today):
    delta = (today - d).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return "This week"
    return "Last week" if delta < 14 else "Earlier"


def badges_for(it, mode):
    out = []
    if it["kind"] == "movie":
        if mode == "rec" and "4K" in it.get("qualities", ()):
            out.append(("4K", "k4"))
        return out
    eps = it["episodes"]
    if any(r["ep"] == 1 for r in eps):
        out.append(("New series" if any(r["season"] == 1 and r["ep"] == 1 for r in eps)
                    else "New season", "new"))
    elif mode == "up":
        fin = next((r["finale"] for r in eps if r.get("finale")), None)
        if fin:
            out.append(({"season": "Season finale", "series": "Series finale",
                         "midseason": "Midseason finale"}.get(fin, "Finale"), "fin"))
    return out


def badge_html(badges):
    return "".join(f'<span class="badge {c}">{e(b)}</span>' for b, c in badges)


def img_html(it):
    return (f'<img loading="lazy" src="{e(it["poster"])}" alt="" onerror="this.remove()">'
            if it.get("poster") else "")


def detail(it, mode):
    """The popup body, pre-rendered into a <template> on each tile."""
    kind = it["kind"]
    facts = [str(it["year"])] if it.get("year") else []
    if kind == "movie" and it.get("runtime"):
        facts.append(fmt_runtime(it["runtime"]))
    if kind == "tv" and it.get("network"):
        facts.append(it["network"])
    if it.get("cert"):
        facts.append(it["cert"])
    if it.get("genres"):
        facts.append(", ".join(it["genres"][:3]))

    status = ""
    if kind == "movie":
        status = (f'{e(it["release"])} {it["when"].strftime("%A %-d %B")}' if mode == "up"
                  else f'Added {it["when"].strftime("%A %-d %B")}')
    else:
        n = len(it["episodes"])
        status = (f'Airs {it["when"].strftime("%A %-d %B")}, {fmt_time(it["when"])}' if mode == "up"
                  else f'{n} new episode{"s" if n != 1 else ""} · {eps_summary(it["episodes"])}')

    body = ""
    if it.get("overview"):
        body += f'<h4>{"About the show" if kind == "tv" else "Plot"}</h4><p>{e(it["overview"])}</p>'

    if kind == "tv":
        eps = it["episodes"]
        if len(eps) <= 4:
            body += f'<h4>{"Episode" if len(eps) == 1 else "Episodes"}</h4>'
            for r in eps:
                t = f' · {e(r["title"])}' if r.get("title") and r["title"] != "TBA" else ""
                ov = e(r.get("overview") or "No description yet.")
                body += f'<div class="ep"><div class="ept"><b>{ep_code(r["season"], r["ep"])}</b>{t}</div><p>{ov}</p></div>'
        else:
            shown = eps[:40]
            items = "".join(
                f'<li><b>{ep_code(r["season"], r["ep"])}</b> {e(r.get("title") or "")}</li>' for r in shown)
            more = f'<li class="more">and {len(eps) - len(shown)} more</li>' if len(eps) > len(shown) else ""
            body += f'<h4>Episodes</h4><ul class="eplist">{items}{more}</ul>'

    links = "".join(f'<a href="{e(u)}" target="_blank" rel="noopener">{e(n)} ↗</a>' for n, u in it["links"])
    return f"""<div class="dposter">{img_html(it)}</div>
<div class="dmain">
  <h3>{e(it["title"] or "?")}</h3>
  <div class="facts">{" · ".join(e(f) for f in facts)}</div>
  <div class="dbadges">{badge_html(badges_for(it, mode))}</div>
  <div class="status">{e(status)}</div>
  {body}
  <div class="links">{links}</div>
</div>"""


def tile(it, *, mode):
    kind, title = it["kind"], it["title"] or "?"
    if kind == "movie":
        sub = str(it.get("year") or "")
        meta = it["release"] if mode == "up" else (fmt_runtime(it["runtime"]) if it.get("runtime") else "")
    else:
        eps = it["episodes"]
        sub = eps_summary(eps)
        if mode == "up":
            meta = fmt_time(it["when"]) + (f" · {it['network']}" if it.get("network") else "")
        else:
            meta = f"{len(eps)} new episode{'s' if len(eps) != 1 else ''}"
    ept = ""
    if kind == "tv" and mode == "up" and len(it["episodes"]) == 1:
        t = it["episodes"][0].get("title")
        if t and t != "TBA":
            ept = f'<div class="ept">“{e(t)}”</div>'
    if mode == "up":  # rows have no room on the poster; show badges next to the title instead
        b = badges_for(it, mode)
        if b:
            ept = f'<div class="rbadges">{badge_html(b)}</div>' + ept
    cls = f"tile {kind} {'row' if mode == 'up' else 'card'}"
    chip = "Movie" if kind == "movie" else "TV"
    return f"""<article class="{cls}" tabindex="0" role="button" aria-haspopup="dialog">
  <div class="poster"><div class="ph">{e(title)}</div>{img_html(it)}<div class="badges">{badge_html(badges_for(it, mode))}</div>
    <span class="kind-chip">{chip}</span></div>
  <div class="cap"><h3>{e(title)}</h3><div class="sub">{e(sub)}</div>{ept}<div class="meta">{e(meta)}</div></div>
  <template>{detail(it, mode)}</template>
</article>"""


def grouped(items, label_fn):
    groups = {}
    for it in items:
        groups.setdefault(label_fn(it), []).append(it)
    return groups


JS = """
const dlg = document.getElementById('dlg'), body = dlg.querySelector('.dbody');
function open(t) { body.replaceChildren(t.querySelector('template').content.cloneNode(true)); dlg.showModal(); dlg.scrollTop = 0; }
document.addEventListener('click', ev => { const t = ev.target.closest('.tile'); if (t) open(t); });
document.addEventListener('keydown', ev => { const t = ev.target.closest && ev.target.closest('.tile');
  if (t && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); open(t); } });
dlg.addEventListener('click', ev => { if (ev.target === dlg || ev.target.closest('.close')) dlg.close(); });
"""


def render(data):
    today = datetime.now(TZ).date()
    if data is None:
        body = '<p class="empty">Loading… refresh in a few seconds.</p>'
        updated = "never"
    else:
        up = grouped(data["upcoming"], lambda it: (it["when"].date(), day_label(it["when"].date(), today)))
        rec = grouped(data["recent"], lambda it: ago_bucket(it["when"].date(), today))
        up_html = "".join(
            f'<section class="day"><h2>{e(label)}<small>{d.strftime("%-d %b")}</small></h2>'
            f'<div class="rows">{"".join(tile(i, mode="up") for i in its)}</div></section>'
            for (d, label), its in up.items()) or '<p class="empty">Nothing scheduled.</p>'
        rec_html = "".join(
            f'<section class="day"><h2>{e(label)}</h2>'
            f'<div class="grid">{"".join(tile(i, mode="rec") for i in its)}</div></section>'
            for label, its in rec.items()) or '<p class="empty">Nothing new yet.</p>'
        n_up, n_rec = len(data["upcoming"]), len(data["recent"])
        body = f"""
<input type="radio" name="tab" id="t-rec" checked><input type="radio" name="tab" id="t-up">
<input type="radio" name="f" id="f-all" checked><input type="radio" name="f" id="f-movie"><input type="radio" name="f" id="f-tv">
<nav>
  <div class="tabs"><label for="t-rec">Just added <span>{n_rec}</span></label><label for="t-up">Coming up <span>{n_up}</span></label></div>
  <div class="filters"><label for="f-all">All</label><label for="f-movie">Movies</label><label for="f-tv">TV</label></div>
</nav>
<div class="pane rec">{rec_html}</div>
<div class="pane up">{up_html}<p class="note">These are release dates. New episodes and movies usually arrive here within a few hours of release.</p></div>"""
        updated = data["updated"].strftime("%-I:%M %p, %a %-d %b").replace("AM", "am").replace("PM", "pm")
    req = f'<a class="req" href="{e(REQUEST_URL)}">Request something →</a>' if REQUEST_URL else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(TITLE)}</title><link rel="stylesheet" href="/style.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🎬</text></svg>">
</head><body>
<header><h1>{e(TITLE)}</h1>{req}</header>
<main>{body}</main>
<footer>Updated {e(updated)}</footer>
<dialog id="dlg"><button class="close" aria-label="Close">×</button><div class="dbody"></div></dialog>
<script>{JS}</script>
</body></html>"""


# --- server -----------------------------------------------------------------

STATE = {"data": None, "error": None}


def refresher():
    while True:
        try:
            t = time.time()
            STATE["data"] = gather()
            STATE["error"] = None
            print(f"refreshed in {time.time() - t:.1f}s", flush=True)
        except Exception:
            STATE["error"] = traceback.format_exc()
            print(STATE["error"], file=sys.stderr, flush=True)
        time.sleep(REFRESH_MINUTES * 60)


CSS_PATH = Path(__file__).parent / "style.css"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send(200, "text/html; charset=utf-8", render(STATE["data"]).encode())
        elif path == "/style.css":
            self.send(200, "text/css", CSS_PATH.read_bytes())
        elif path == "/healthz":
            ok = STATE["data"] is not None and STATE["error"] is None
            self.send(200 if ok else 503, "text/plain", b"ok" if ok else (STATE["error"] or "loading").encode())
        else:
            self.send(404, "text/plain", b"not found")

    def send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not SONARRS and not RADARRS:
        sys.exit("No SONARR_*/RADARR_* URL+KEY pairs configured")
    if "--once" in sys.argv:  # render to stdout, for testing
        print(render(gather()))
        sys.exit()
    threading.Thread(target=refresher, daemon=True).start()
    print(f"listening on :{PORT}  sonarr={[i['name'] for i in SONARRS]} radarr={[i['name'] for i in RADARRS]}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
