#!/usr/bin/env python3
"""Build nfl_epg.xml (XMLTV) from ESPN's public scoreboard + your IPTV playlist,
plus a matchup image (both team logos) for every game.

Env vars:
  M3U_URL        your provider playlist URL (keep it as a GitHub secret)
  EPG_TZ         output time zone (default America/Denver)
  EPG_DAYS       days ahead to include (default 9)
  EPG_OUT        output file (default nfl_epg.xml)
  ICON_BASE_URL  public base URL of the repo's raw files, ending in "/".
                 Auto-detected inside GitHub Actions.
"""
import io, json, os, re, shutil, sys, urllib.error, urllib.request
from datetime import datetime, timedelta
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("EPG_TZ", "America/Denver"))
DAYS_AHEAD = int(os.environ.get("EPG_DAYS", "9"))
OUT = os.environ.get("EPG_OUT", "nfl_epg.xml")
DURATION = timedelta(hours=3, minutes=30)
ESPN = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
        "scoreboard?dates={day}")
LOGO_FALLBACK = "https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png"


def fetch_bytes(url, label):
    """Fetch a URL; on failure say WHICH request failed (never print the playlist URL)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nfl-epg/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{label} request failed: HTTP {e.code} {e.reason}") from None
    except Exception as e:
        raise RuntimeError(f"{label} request failed: {type(e).__name__}") from None


def fetch(url, label):
    return fetch_bytes(url, label).decode("utf-8", "replace")


def parse_games(data):
    games, seen = [], set()
    for ev in data.get("events", []):
        key = ev.get("id") or (ev["date"], ev.get("name"))
        if key in seen:
            continue
        seen.add(key)
        comp = ev["competitions"][0]
        t = {c["homeAway"]: c["team"] for c in comp["competitors"]}
        start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        nets = [n for b in comp.get("broadcasts", []) for n in b.get("names", [])]
        g = {"start": start, "networks": nets}
        for side in ("away", "home"):
            abbr = (t[side].get("abbreviation") or t[side]["displayName"][:3]).lower()
            g[side] = t[side]["displayName"]
            g[f"{side}_abbr"] = abbr
            g[f"{side}_logo"] = t[side].get("logo") or LOGO_FALLBACK.format(abbr=abbr)
        games.append(g)
    return sorted(games, key=lambda g: g["start"])


def get_games():
    """One request per day (ESPN's documented single-date form)."""
    today = datetime.now(TZ).date()
    events = []
    for offset in range(-1, DAYS_AHEAD + 1):
        day = (today + timedelta(days=offset)).strftime("%Y%m%d")
        data = json.loads(fetch(ESPN.format(day=day), f"ESPN schedule ({day})"))
        events += data.get("events", [])
    return parse_games({"events": events})


# ---- playlist -------------------------------------------------------------
CH = re.compile(r"^(?P<prefix>.*NFL.*?):\s*(?P<a>.+?)\s+(?:vs\.?|at|@)\s+(?P<h>.+)$", re.I)


def parse_playlist(text):
    """Return provider NFL channels: id, prefix, full name, the two team names."""
    out = []
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        name = line.rsplit(",", 1)[-1].strip()
        m = CH.match(name)
        if not m:
            continue
        tid = re.search(r'tvg-id="([^"]*)"', line)
        prefix = m["prefix"].strip()
        cid = (tid.group(1).strip() if tid else "") or re.sub(r"[^A-Za-z0-9]+", ".", prefix).strip(".")
        out.append({"id": cid, "prefix": prefix, "name": name, "teams": (m["a"], m["h"])})
    return out


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def same_team(x, y):
    x, y = norm(x), norm(y)
    return bool(x and y) and (x == y or x in y or y in x or x.split()[-1] == y.split()[-1])


def channel_for(game, channels):
    for ch in channels:
        a, h = ch["teams"]
        if (same_team(a, game["away"]) and same_team(h, game["home"])) or \
           (same_team(a, game["home"]) and same_team(h, game["away"])):
            return ch
    return None


# ---- matchup images ---------------------------------------------------------
def icon_base():
    base = os.environ.get("ICON_BASE_URL", "").strip()
    if not base and os.environ.get("GITHUB_REPOSITORY"):
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        base = f"https://raw.githubusercontent.com/{os.environ['GITHUB_REPOSITORY']}/{branch}/"
    return base if not base or base.endswith("/") else base + "/"


def _font(size):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def make_matchup(g, path, cache):
    from PIL import Image, ImageDraw
    W, H, BOX = 1280, 720, 440
    img = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(img)
    for side, cx in (("away", 330), ("home", 950)):
        url = g[f"{side}_logo"]
        if url not in cache:
            cache[url] = Image.open(io.BytesIO(fetch_bytes(url, f"logo {g[side + '_abbr']}"))).convert("RGBA")
        logo = cache[url].copy()
        logo.thumbnail((BOX, BOX))
        img.paste(logo, (cx - logo.width // 2, 300 - logo.height // 2), logo)
        d.text((cx, 600), g[f"{side}_abbr"].upper(), font=_font(84), fill=(255, 255, 255), anchor="mm")
    d.text((640, 290), "@", font=_font(150), fill=(200, 205, 215), anchor="mm")
    img.save(path, "PNG", optimize=True)


def make_icons(games, channels):
    """Writes icons/matchups/*.png + icons/channels/*.png and sets g['icon'] / g['chan_icon']."""
    base = icon_base()
    if not base:
        print("No ICON_BASE_URL / GitHub repo detected: skipping images")
        return
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow not installed: skipping images")
        return
    shutil.rmtree("icons", ignore_errors=True)
    os.makedirs("icons/matchups")
    os.makedirs("icons/channels")
    cache, done = {}, set()
    for g in games:
        try:
            name = f"{g['start']:%Y%m%d}_{g['away_abbr']}_{g['home_abbr']}"
            path = f"icons/matchups/{name}.png"
            make_matchup(g, path, cache)
            g["icon"] = f"{base}{path}"
            ch = channel_for(g, channels)
            if ch and ch["id"] not in done:
                slug = re.sub(r"[^A-Za-z0-9]+", "_", ch["id"]).strip("_").lower()
                cpath = f"icons/channels/{slug}.png"
                shutil.copyfile(path, cpath)
                g["chan_icon"] = f"{base}{cpath}?v={name}"  # ?v= busts app caches each week
                done.add(ch["id"])
        except Exception as e:  # one bad logo must not break the guide
            print(f"Image skipped for {g['away']} at {g['home']}: {e}")


# ---- XMLTV ----------------------------------------------------------------
def build_xml(games, channels):
    chan_defs, chan_icons, progs, busy = {}, {}, [], []
    for g in games:
        ch = channel_for(g, channels)
        if ch:
            cid = ch["id"]
            chan_defs.setdefault(cid, [ch["prefix"], ch["name"]])
            if g.get("chan_icon"):
                chan_icons.setdefault(cid, g["chan_icon"])
        else:  # no matching provider channel: spread across generic channels
            for i, end in enumerate(busy):
                if end <= g["start"]:
                    busy[i] = g["start"] + DURATION
                    break
            else:
                busy.append(g["start"] + DURATION)
                i = len(busy) - 1
            cid = f"NFL.Game{i + 1}"
            chan_defs.setdefault(cid, [f"NFL Game {i + 1}"])
            if g.get("icon"):
                chan_icons.setdefault(cid, g["icon"])
        progs.append((cid, g))

    fmt = lambda d: d.astimezone(TZ).strftime("%Y%m%d%H%M%S %z")
    o = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
         '<tv generator-info-name="nfl-epg">']
    for cid, names in sorted(chan_defs.items()):
        dn = "".join(f"<display-name>{escape(n)}</display-name>" for n in dict.fromkeys(names))
        ic = f'<icon src="{escape(chan_icons[cid])}"/>' if cid in chan_icons else ""
        o.append(f'  <channel id="{escape(cid)}">{dn}{ic}</channel>')
    for cid, g in sorted(progs, key=lambda p: (p[1]["start"], p[0])):
        desc = f'{g["away"]} at {g["home"]}. NFL regular season.'
        if g["networks"]:
            desc += " Broadcast: " + ", ".join(g["networks"]) + "."
        o += [f'  <programme start="{fmt(g["start"])}" stop="{fmt(g["start"] + DURATION)}" channel="{escape(cid)}">',
              f'    <title lang="en">NFL Football: {escape(g["away"])} at {escape(g["home"])}</title>',
              f'    <desc lang="en">{escape(desc)}</desc>',
              '    <category lang="en">Sports</category><category lang="en">Football</category>']
        if g.get("icon"):
            o.append(f'    <icon src="{escape(g["icon"])}"/>')
        o += ['    <new/>', '  </programme>']
    o.append("</tv>")
    return "\n".join(o) + "\n"


def main():
    games = get_games()
    if not games:
        print("No games returned (offseason?). Leaving existing file untouched.")
        return
    channels = []
    url = os.environ.get("M3U_URL", "").strip().strip("\"'").strip()
    if url:
        channels = parse_playlist(fetch(url, "IPTV playlist (M3U_URL secret)"))
        print(f"Found {len(channels)} NFL channels in playlist")
    else:
        print("M3U_URL not set: using generic NFL Game channels")
    make_icons(games, channels)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build_xml(games, channels))
    print(f"Wrote {OUT}: {len(games)} games")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail loudly so the Action never commits a broken file
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
