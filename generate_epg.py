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
import hashlib, io, json, os, re, shutil, sys, urllib.error, urllib.request
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
PFX = r"(?P<prefix>[^:]*?\bNFL\s+(?:Sunday|Monday|Thursday|Saturday)(?:\s+(?:\d+|Night))?)"
CH = re.compile(PFX + r"\s*:\s*(?P<a>.+?)\s+(?:vs\.?|at|@)\s+(?P<h>.+)$", re.I)
EMPTY = re.compile("^" + PFX + r"\s*:?\s*$", re.I)
TIME_SUFFIX = re.compile(r"(?:\s*@)+\s*\d{1,2}:\d{2}\s*[AP]M(?:\s+[A-Z]{2,4})?\s*$", re.I)
SLOT = re.compile(r"(Monday|Thursday|Sunday)\s+Night", re.I)


def parse_playlist(text):
    """Provider NFL channels (USA NFL Sunday 7xx / Monday Night / Thursday Night / Sunday Night).
    Named with a matchup -> matched by teams; empty ("USA NFL Monday Night:") -> by day.
    Every spelling of the channel name goes into the guide as a display-name, because
    these channels have no tvg-id and the app can only match on the name."""
    out = []
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        raw = line.rsplit(",", 1)[-1].strip()
        name = TIME_SUFFIX.sub("", raw).strip()
        m = CH.match(name)
        if m:
            prefix, teams = m["prefix"].strip(), (m["a"].strip(), m["h"].strip())
        else:
            e = EMPTY.match(name)
            if not e:
                continue
            prefix, teams = e["prefix"].strip(), None
        sm = SLOT.search(prefix)
        slot = f"{sm.group(1).lower()} night" if sm else None
        tid = re.search(r'tvg-id="([^"]*)"', line)
        tname = re.search(r'tvg-name="([^"]*)"', line)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", prefix).strip("_").lower()
        cid = (tid.group(1).strip() if tid else "") or re.sub(r"[^A-Za-z0-9]+", ".", prefix).strip(".")
        names = [prefix, prefix + ":", name, raw] + ([tname.group(1).strip()] if tname else [])
        out.append({"id": cid, "prefix": prefix, "name": name, "teams": teams, "slot": slot,
                    "slug": slug, "names": [n for n in dict.fromkeys(names) if n]})
    return out


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def same_team(x, y):
    x, y = norm(x), norm(y)
    return bool(x and y) and (x == y or x in y or y in x or x.split()[-1] == y.split()[-1])


def prime_slot(game):
    """'monday night' / 'thursday night' / 'sunday night' for 7pm+ ET kickoffs."""
    et = game["start"].astimezone(ZoneInfo("America/New_York"))
    if et.hour < 19:
        return None
    return {0: "monday night", 3: "thursday night", 6: "sunday night"}.get(et.weekday())


def teams_match(ch, game):
    """Matchup in the channel name equals the game (and, for Night channels, the right night)."""
    if not ch["teams"]:
        return False
    if ch["slot"] and prime_slot(game) != ch["slot"]:
        return False
    a, h = ch["teams"]
    return (same_team(a, game["away"]) and same_team(h, game["home"])) or \
           (same_team(a, game["home"]) and same_team(h, game["away"]))


def mark_stale(channels, games):
    """A Night channel whose name shows a matchup that isn't on that night's schedule
    (provider left an old name on it) is 'stale' and gets matched by day instead."""
    for ch in channels:
        ch["stale"] = bool(ch["teams"] and ch["slot"]) and not any(teams_match(ch, g) for g in games)


def channels_for(game, channels):
    """Primetime games (Thursday/Sunday/Monday night) go ONLY on that night's channel,
    even if the provider has put the matchup in some other channel's name.
    Other games go on the channel whose name shows the matchup."""
    slot = prime_slot(game)
    if slot:
        night = [ch for ch in channels if ch["slot"] == slot
                 and (not ch["teams"] or ch.get("stale") or teams_match(ch, game))]
        if night:
            return night
    return [ch for ch in channels if teams_match(ch, game)]


def channel_for(game, channels):
    hits = channels_for(game, channels)
    return hits[0] if hits else None


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
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for side, cx in (("away", 330), ("home", 950)):
        url = g[f"{side}_logo"]
        if url not in cache:
            cache[url] = Image.open(io.BytesIO(fetch_bytes(url, f"logo {g[side + '_abbr']}"))).convert("RGBA")
        logo = cache[url].copy()
        logo.thumbnail((BOX, BOX))
        img.paste(logo, (cx - logo.width // 2, 300 - logo.height // 2), logo)
        d.text((cx, 600), g[f"{side}_abbr"].upper(), font=_font(84), fill=(20, 24, 32), anchor="mm")
    d.text((640, 290), "@", font=_font(150), fill=(110, 115, 125), anchor="mm")
    img.save(path, "PNG", optimize=True)


def file_hash(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()[:8]


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
            h = file_hash(path)  # ?v=<hash> makes apps reload an image whenever it changes
            g["icon"] = f"{base}{path}?v={h}"
            g["chan_icons"] = {}
            for ch in channels_for(g, channels):
                custom = f"custom/{ch['slug']}.png"
                if os.path.exists(custom):   # your own image wins
                    g["icon"] = f"{base}{custom}?v={file_hash(custom)}"
                    g["chan_icons"][ch["id"]] = g["icon"]
                elif ch["id"] not in done:
                    cpath = f"icons/channels/{ch['slug']}.png"
                    shutil.copyfile(path, cpath)
                    g["chan_icons"][ch["id"]] = f"{base}{cpath}?v={h}"
                    done.add(ch["id"])
        except Exception as e:  # one bad logo must not break the guide
            print(f"Image skipped for {g['away']} at {g['home']}: {e}")
    # channels whose provider name shows a matchup get that matchup's image as their logo,
    # even when the game itself is carried on a Night channel
    for ch in channels:
        if not ch["teams"] or ch["id"] in done:
            continue
        for g in games:
            a, h = ch["teams"]
            if (same_team(a, g["away"]) and same_team(h, g["home"])) or \
               (same_team(a, g["home"]) and same_team(h, g["away"])):
                src = f"icons/matchups/{g['start']:%Y%m%d}_{g['away_abbr']}_{g['home_abbr']}.png"
                if os.path.exists(src):
                    cpath = f"icons/channels/{ch['slug']}.png"
                    shutil.copyfile(src, cpath)
                    ch["icon_url"] = f"{base}{cpath}?v={file_hash(cpath)}"
                    done.add(ch["id"])
                break


# ---- XMLTV ----------------------------------------------------------------
def build_xml(games, channels):
    chan_defs, chan_icons, progs, busy = {}, {}, [], []
    for ch in channels:   # keep every provider NFL channel in the guide, even with no game today
        chan_defs.setdefault(ch["id"], ch.get("names") or [ch["prefix"], ch["name"]])
    for ch in channels:
        if ch.get("icon_url"):
            chan_icons.setdefault(ch["id"], ch["icon_url"])
    for g in games:
        hits = channels_for(g, channels)
        if hits:
            for ch in hits:
                cid = ch["id"]
                if g.get("chan_icons", {}).get(cid):
                    chan_icons.setdefault(cid, g["chan_icons"][cid])
                progs.append((cid, g))
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
        mark_stale(channels, games)
        print(f"Found {len(channels)} NFL channels in playlist")
        for ch in channels:
            hits = [g for g in games if ch in channels_for(g, channels)]
            tag = ", ".join(f"{g['away']} at {g['home']}" for g in hits) or "no game"
            print(f"  {ch['name']!r} (tvg-id {'set' if ch['id'] != re.sub(r'[^A-Za-z0-9]+', '.', ch['prefix']).strip('.') else 'none'}) -> {tag}")
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
