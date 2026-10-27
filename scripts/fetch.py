#!/usr/bin/env python3
"""Powder Desk fetcher.

Runs on a schedule in GitHub Actions (standard library only, nothing to install).
Pulls forecasts from NOAA/NWS, five weather models via Open-Meteo, avalanche.org,
the Utah Avalanche Center, the NWS forecast discussion and snow blogs, then writes
docs/data.json for the dashboard and sends phone alerts through ntfy when a big
storm shows up. If nothing crosses a threshold, it stays quiet.
"""
import datetime as dt
import html
import json
import os
import re
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
OUT = ROOT / "docs" / "data.json"
STATE_PATH = ROOT / "state" / "alerts.json"
TZ = ZoneInfo(CONFIG.get("timezone", "America/Denver"))
UA = f"PowderDesk/1.0 (personal snow dashboard; {CONFIG.get('contact_email', 'unknown')})"
NOW = dt.datetime.now(TZ)
ERRORS = []


# ---------------------------------------------------------------- helpers

def get(url, headers=None, data=None, method=None, timeout=30, tries=2, raw=False):
    h = {"User-Agent": UA, "Accept": "application/geo+json, application/ld+json, application/json"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return body if raw else json.loads(body)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def note_error(source, err):
    msg = f"{source}: {type(err).__name__}: {str(err)[:160]}"
    ERRORS.append(msg)
    print("WARN", msg)


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<(br|p|/p|li|div)[^>]*>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def excerpt(text, limit=220):
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.") + "…"


def med(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"median": round(statistics.median(vals), 1), "min": round(min(vals), 1),
            "max": round(max(vals), 1), "n": len(vals)}


def iso(t):
    return t.isoformat(timespec="minutes") if t else None


# ---------------------------------------------------------------- weather models (Open-Meteo)

def fetch_models(spots):
    """One request per model, all spots at once. Returns {model: [per-spot result]}."""
    base = "https://api.open-meteo.com/v1/forecast"
    out = {}
    for model in CONFIG["models"]:
        params = {
            "latitude": ",".join(f"{s['lat']:.4f}" for s in spots),
            "longitude": ",".join(f"{s['lon']:.4f}" for s in spots),
            "elevation": ",".join(str(round(s["elev_ft"] * 0.3048)) for s in spots),
            "hourly": "snowfall",
            "daily": "snowfall_sum,temperature_2m_max,temperature_2m_min",
            "forecast_days": 7,
            "timezone": CONFIG["timezone"],
            "precipitation_unit": "inch",
            "temperature_unit": "fahrenheit",
            "models": model,
        }
        try:
            res = get(base + "?" + urllib.parse.urlencode(params, safe=","))
            out[model] = res if isinstance(res, list) else [res]
        except Exception as e:  # noqa: BLE001
            note_error(f"Open-Meteo {model}", e)
    return out


def snow_scale(units):
    # Open-Meteo derives snowfall at ~7:1 snow-to-liquid. Rescale to the configured ratio.
    ratio = CONFIG.get("snow_ratio", 10) / 7.0
    to_in = 1 / 2.54 if units and "cm" in units else 1.0
    return ratio * to_in


def model_series(item):
    d, h = item["daily"], item["hourly"]
    k_day = snow_scale(item.get("daily_units", {}).get("snowfall_sum"))
    k_hr = snow_scale(item.get("hourly_units", {}).get("snowfall"))
    days = {}
    for i, date in enumerate(d["time"]):
        v = d["snowfall_sum"][i]
        days[date] = {
            "snow": None if v is None else v * k_day,
            "hi": d["temperature_2m_max"][i],
            "lo": d["temperature_2m_min"][i],
        }
    start = NOW.strftime("%Y-%m-%dT%H:00")
    idx = next((i for i, t in enumerate(h["time"]) if t >= start), None)
    h72 = None
    if idx is not None:
        window = [v for v in h["snowfall"][idx: idx + 72] if v is not None]
        if len(window) >= 60:  # model reaches far enough out
            h72 = sum(window) * k_hr
    complete = [x["snow"] for x in days.values()]
    d7 = sum(complete) if all(v is not None for v in complete) else None
    return {"days": days, "h72": h72, "d7": d7}


# ---------------------------------------------------------------- NWS

def nws_points(lat, lon):
    return get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")["properties"]


def nws_week(points):
    periods = get(points["forecast"])["properties"]["periods"]
    days = {}
    for p in periods:
        date = p["startTime"][:10]
        day = days.setdefault(date, {"date": date})
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        if p["isDaytime"]:
            day.update(name=p["name"], hi=p["temperature"], short=p["shortForecast"],
                       detailed=p["detailedForecast"], pop=pop)
        else:
            day.setdefault("name", p["name"])
            day.setdefault("short", p["shortForecast"])
            day["lo"] = p["temperature"]
            day["night_short"] = p["shortForecast"]
            day["pop_night"] = pop
    return [days[k] for k in sorted(days)][:7]


DUR = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")


def nws_snow(points):
    """NWS's own snowfall grid (their forecasters' numbers), summed per local day, inches."""
    grid = get(points["forecastGridData"])["properties"]["snowfallAmount"]
    per_day, h72 = {}, 0.0
    end72 = NOW + dt.timedelta(hours=72)
    for v in grid["values"]:
        if not v["value"]:
            continue
        start_s, dur_s = v["validTime"].split("/")
        start = dt.datetime.fromisoformat(start_s).astimezone(TZ)
        m = DUR.fullmatch(dur_s)
        hours = int(m.group(1) or 0) * 24 + int(m.group(2) or 0) or 1
        inch_per_hr = v["value"] / 25.4 / hours
        for k in range(hours):
            t = start + dt.timedelta(hours=k)
            per_day[t.strftime("%Y-%m-%d")] = per_day.get(t.strftime("%Y-%m-%d"), 0) + inch_per_hr
            if NOW <= t < end72:
                h72 += inch_per_hr
    today = NOW.date()
    dates = [(today + dt.timedelta(days=i)).isoformat() for i in range(7)]
    covered = [d for d in dates if d in per_day]
    return {
        "days": {d: round(per_day.get(d, 0), 1) for d in dates},
        "h72": round(h72, 1),
        # NWS grids usually run ~7 days; days past the grid read as zero, so flag coverage
        "d7": round(sum(per_day.get(d, 0) for d in dates), 1),
        "days_covered": len(covered),
    }


def nws_alerts(lat, lon):
    feats = get(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}")["features"]
    out = []
    for f in feats:
        p = f["properties"]
        out.append({"id": p["id"], "event": p["event"], "headline": p.get("headline"),
                    "severity": p.get("severity"), "ends": p.get("ends") or p.get("expires"),
                    "description": excerpt(p.get("description") or "", 600),
                    "url": "https://forecast.weather.gov/MapClick.php?" + urllib.parse.urlencode({"lat": lat, "lon": lon})})
    return out


def area_discussion(office):
    listing = get(f"https://api.weather.gov/products/types/AFD/locations/{office}")
    latest = listing["@graph"][0]
    prod = get(f"https://api.weather.gov/products/{latest['id']}")
    text = prod["productText"]
    sections = {}
    for m in re.finditer(r"^\.([A-Z][A-Z /\-]+?)\.\.\.(.*?)(?=^\.[A-Z][A-Z /\-]+?\.\.\.|^&&|\Z)", text, re.M | re.S):
        name = m.group(1).strip()
        body = re.sub(r"\s*\n\s*", " ", m.group(2)).replace("&&", "").strip()
        sections[name] = body
    def find(*keys):
        for k in sections:
            if any(key in k for key in keys):
                return sections[k]
        return None
    return {
        "office": office,
        "issued": prod.get("issuanceTime"),
        "key_messages": find("KEY MESSAGES"),
        "synopsis": find("SYNOPSIS", "OVERVIEW"),
        "short_term": find("SHORT TERM"),
        "long_term": find("LONG TERM", "EXTENDED"),
        "url": f"https://forecast.weather.gov/product.php?site={office}&issuedby={office}&product=AFD",
    }


# ---------------------------------------------------------------- avalanche

def point_in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def feature_contains(feat, lon, lat):
    g = feat.get("geometry") or {}
    polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
    for poly in polys:
        if poly and point_in_ring(lon, lat, poly[0]) and not any(point_in_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


def avy_lookup(features, lat, lon):
    """Properties of the forecast zone containing the point, if any."""
    for f in features:
        if feature_contains(f, lon, lat):
            return f["properties"]
    return None


def avy_summary(props):
    if not props:
        return None
    end = props.get("end_date")
    expired = False
    if end:
        try:
            end_dt = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:  # avalanche.org sends center-local times without an offset
                end_dt = end_dt.replace(tzinfo=ZoneInfo(props.get("timezone") or CONFIG["timezone"]))
            expired = end_dt < NOW
        except (ValueError, TypeError):
            pass
    level = props.get("danger_level")
    if expired:
        level = -1
    return {
        "zone": props.get("name"), "center": props.get("center"), "center_id": props.get("center_id"),
        "danger": "no rating" if expired else props.get("danger"), "level": level,
        "color": props.get("color"), "font_color": props.get("font_color"),
        "travel_advice": props.get("travel_advice"), "link": props.get("link") or props.get("center_link"),
        "start": props.get("start_date"), "end": end, "expired": expired,
        "warning": bool((props.get("warning") or {}).get("product")),
    }


def uac_detail(slug):
    """Best effort: UAC's JSON feed for the zone's full bottom line. Skipped quietly if it changes."""
    res = get(f"https://utahavalanchecenter.org/forecast/{slug}/json")
    adv = res["advisories"][0]["advisory"]
    return {
        "bottom_line": strip_html(adv.get("bottom_line")),
        "issued_text": strip_html(adv.get("date_issued")),
        "current_conditions": excerpt(strip_html(adv.get("current_conditions")), 700),
        "forecaster": strip_html(adv.get("forecaster")) or None,
    }


# ---------------------------------------------------------------- blogs

def blog_posts(feed):
    res = get(feed["url"])
    posts = []
    for e in res.get("feed", {}).get("entry", [])[:2]:
        link = next((l["href"] for l in e.get("link", []) if l.get("rel") == "alternate"), None)
        posts.append({
            "source": feed["name"], "author": feed.get("author"),
            "title": e.get("title", {}).get("$t"), "published": e.get("published", {}).get("$t"),
            "excerpt": excerpt(strip_html(e.get("content", {}).get("$t") or e.get("summary", {}).get("$t", ""))),
            "url": link,
        })
    return posts


# ---------------------------------------------------------------- Claude's read

def claude_take(digest, state):
    key = os.environ.get("ANTHROPIC_API_KEY")
    prev = state.get("take")
    if prev:
        age_h = (NOW - dt.datetime.fromisoformat(prev["generated_at"])).total_seconds() / 3600
        moved = abs((prev.get("anchor") or 0) - (digest.get("anchor") or 0))
        if age_h < 5.5 and moved < 2:
            return prev
    if not key:
        return None
    system = (
        "You write a short, neutral snow-forecast read for a backcountry skier and resort rider who lives in "
        "Logan, Utah. Use only the data provided. Write 3 to 5 sentences of plain text, no headings or lists. "
        "Cover: the likely range for the local area and when it falls; how well the forecasts agree and what "
        "that means for confidence; the best-looking spot in the region for the next 72 hours and the week; "
        "and one sentence on avalanche conditions that defers to the avalanche center's rating and never "
        "contradicts it. Note when the NWS forecasters' numbers differ meaningfully from the models. "
        "No hype. If little snow is coming, say so plainly. Snow totals are inches."
    )
    body = json.dumps({
        "model": CONFIG.get("claude_model", "claude-haiku-5-5"),
        "max_tokens": 450,
        "system": system,
        "messages": [{"role": "user", "content": json.dumps(digest)}],
    }).encode()
    try:
        res = get("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, timeout=60, tries=1)
        text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text").strip()
        return {"text": text, "generated_at": iso(NOW), "anchor": digest.get("anchor"), "by": "claude"}
    except Exception as e:  # noqa: BLE001
        note_error("Claude", e)
        return prev


def rules_take(primary, nws_snow_primary):
    d7, h72 = primary.get("d7"), primary.get("h72")
    if not d7:
        return "Not enough model data to summarize."
    spread = d7["max"] - d7["min"]
    agree = "agree well" if spread <= max(3, d7["median"] * 0.5) else \
        "roughly agree" if spread <= max(6, d7["median"]) else "disagree a lot"
    s = (f"{primary['name']}: the forecasts put {fmt(d7['min'])}–{fmt(d7['max'])} in. over the next 7 days "
         f"(middle {fmt(d7['median'])} in.), and they {agree}.")
    if h72:
        s += f" About {fmt(h72['median'])} in. of that lands in the next 72 hours."
    if nws_snow_primary and nws_snow_primary.get("d7") is not None:
        s += f" The NWS forecasters have {fmt(nws_snow_primary['d7'])} in. for the week."
    return s


def fmt(x):
    if x is None:
        return "–"
    return f"{x:.0f}" if x >= 1 else ("<1" if x > 0.05 else "0")


# ---------------------------------------------------------------- alerts (ntfy)

def should_send(state, key, amount):
    sent = state.setdefault("sent", {}).get(key)
    cfg = CONFIG["alerts"]
    if not sent:
        return True
    age_h = (NOW - dt.datetime.fromisoformat(sent["at"])).total_seconds() / 3600
    if age_h >= cfg["cooldown_hours"]:
        return True
    return amount is not None and sent.get("amount") and amount >= sent["amount"] * cfg["regrow_factor"]


def mark_sent(state, key, amount):
    state.setdefault("sent", {})[key] = {"at": iso(NOW), "amount": amount}


def ntfy(title, body, priority=4, tags="snowflake"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("ALERT (no NTFY_TOPIC set):", title, "|", body)
        return
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": str(priority), "Tags": tags}
    if os.environ.get("DASHBOARD_URL"):
        headers["Click"] = os.environ["DASHBOARD_URL"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        get(f"{server}/{topic}", data=body.encode(), method="POST", headers=headers, raw=True)
    except Exception as e:  # noqa: BLE001
        note_error("ntfy", e)


def day_name(date):
    return dt.date.fromisoformat(date).strftime("%a")


def evaluate_alerts(spots, nws_alert_list, local_avy, state):
    cfg = CONFIG["alerts"]
    active = []

    local_hits = []
    for s in (x for x in spots if x["group"] == "local"):
        h72, d7 = s.get("h72"), s.get("d7")
        peak = max((d for d in s["days"] if d.get("median") is not None), key=lambda d: d["median"], default=None)
        hit = (h72 and h72["median"] >= cfg["local_72h_in"]) or \
              (peak and peak["median"] >= cfg["local_day_in"]) or \
              (d7 and d7["median"] >= cfg["local_7d_in"])
        if not hit:
            continue
        parts = []
        if d7:
            parts.append(f"{fmt(d7['min'])}-{fmt(d7['max'])} in. over 7 days (middle {fmt(d7['median'])}).")
        if h72:
            parts.append(f"Next 72 h: {fmt(h72['median'])} in.")
        if peak:
            parts.append(f"Heaviest day: {day_name(peak['date'])} ({fmt(peak['median'])} in.).")
        local_hits.append((s, " ".join(parts), d7["median"] if d7 else 0))
    if local_hits:
        local_hits.sort(key=lambda x: -x[2])
        top = local_hits[0][0]
        title = f"Big snow possible at {top['name']}"
        if len(local_hits) > 1:
            title = f"Big snow possible around {CONFIG['home']['name']}"
        body = "\n".join(f"{s['name']}: {txt}" for s, txt, _ in local_hits)
        active.append({"kind": "local", "title": title, "body": body})
        amount = local_hits[0][2]
        if should_send(state, "local", amount):
            ntfy(title, body, priority=4)
            mark_sent(state, "local", amount)

    regional = [s for s in spots if s["group"] != "local" and s.get("h72")
                and s["h72"]["median"] >= cfg["regional_72h_in"]]
    if regional:
        regional.sort(key=lambda s: -s["h72"]["median"])
        lines = [f"{s['name']} ({s['drive']}): {fmt(s['h72']['median'])} in." for s in regional[:5]]
        title = "Big totals nearby in the next 72 h"
        active.append({"kind": "regional", "title": title, "body": "; ".join(lines)})
        top = regional[0]["h72"]["median"]
        if should_send(state, "regional", top):
            ntfy(title, "\n".join(lines), priority=3, tags="ski")
            mark_sent(state, "regional", top)

    for a in nws_alert_list:
        if a["event"] in cfg["nws_events"]:
            active.append({"kind": "nws", "title": a["event"], "body": a.get("headline") or ""})
            if should_send(state, f"nws:{a['id']}", None):
                ntfy(f"NWS: {a['event']}", a.get("headline") or a["event"], priority=4, tags="warning")
                mark_sent(state, f"nws:{a['id']}", None)

    if local_avy and (local_avy.get("warning") or (local_avy.get("level") or 0) >= cfg["avy_danger_level"]):
        title = f"Avalanche danger {local_avy['danger'].title()} in the {local_avy['zone']} zone"
        if local_avy.get("warning"):
            title = f"Avalanche warning: {local_avy['zone']} zone"
        active.append({"kind": "avy", "title": title, "body": local_avy.get("travel_advice") or ""})
        key = f"avy:{local_avy['zone']}:{local_avy.get('start')}"
        if should_send(state, key, None):
            ntfy(title, local_avy.get("travel_advice") or "", priority=5, tags="warning")
            mark_sent(state, key, None)

    # forget stale keys
    cutoff = NOW - dt.timedelta(days=10)
    state["sent"] = {k: v for k, v in state.get("sent", {}).items()
                     if dt.datetime.fromisoformat(v["at"]) > cutoff}
    return active


# ---------------------------------------------------------------- main

def main():
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    spots_cfg = CONFIG["spots"]
    home = CONFIG["home"]
    today = NOW.date()
    dates = [(today + dt.timedelta(days=i)).isoformat() for i in range(7)]

    # models
    raw = fetch_models(spots_cfg)
    model_ids = [m for m in CONFIG["models"] if m in raw]

    # avalanche zones (all centers, so Teton spots get Bridger-Teton)
    avy_features = []
    try:
        avy_features = get("https://api.avalanche.org/v2/public/products/map-layer")["features"]
    except Exception as e:  # noqa: BLE001
        note_error("avalanche.org", e)

    spots = []
    for i, s in enumerate(spots_cfg):
        series = {}
        for m in model_ids:
            try:
                series[m] = model_series(raw[m][i])
            except Exception as e:  # noqa: BLE001
                note_error(f"{m} @ {s['id']}", e)
        days = []
        for d in dates:
            vals = {m: series[m]["days"].get(d, {}).get("snow") for m in series}
            c = med(vals.values()) or {}
            temps_hi = med(series[m]["days"].get(d, {}).get("hi") for m in series)
            temps_lo = med(series[m]["days"].get(d, {}).get("lo") for m in series)
            days.append({"date": d, **c,
                         "models": {m: (None if v is None else round(v, 1)) for m, v in vals.items()},
                         "hi": temps_hi and round(temps_hi["median"]), "lo": temps_lo and round(temps_lo["median"])})
        entry = {
            **{k: s[k] for k in ("id", "name", "group", "kind", "drive", "elev_ft", "lat", "lon")},
            "days": days,
            "h72": med(series[m]["h72"] for m in series),
            "d7": med(series[m]["d7"] for m in series),
            "by_model": {m: {"h72": series[m]["h72"] and round(series[m]["h72"], 1),
                             "d7": series[m]["d7"] and round(series[m]["d7"], 1)} for m in series},
            "avy": avy_summary(avy_lookup(avy_features, s["lat"], s["lon"])) if avy_features else None,
        }
        spots.append(entry)

    # NWS: valley forecast for home, snow grid + alerts for local spots
    week, alerts = [], []
    try:
        hp = nws_points(home["lat"], home["lon"])
        week = nws_week(hp)
        alerts += nws_alerts(home["lat"], home["lon"])
    except Exception as e:  # noqa: BLE001
        note_error("NWS home forecast", e)
    for s in (x for x in spots if x["group"] == "local"):
        try:
            p = nws_points(s["lat"], s["lon"])
            s["nws"] = nws_snow(p)
            for a in nws_alerts(s["lat"], s["lon"]):
                if a["id"] not in {x["id"] for x in alerts}:
                    alerts.append(a)
        except Exception as e:  # noqa: BLE001
            note_error(f"NWS snow grid @ {s['id']}", e)

    # local avalanche forecast
    primary = next(s for s in spots if s["id"] == CONFIG["primary_local"])
    local_avy = primary.get("avy") or next((s["avy"] for s in spots if s["group"] == "local" and s.get("avy")), None)
    if local_avy is not None:
        try:
            local_avy.update(uac_detail(CONFIG["uac_zone_slug"]))
        except Exception as e:  # noqa: BLE001
            note_error("UAC detail (optional)", e)

    # forecaster voices
    discussion = None
    try:
        discussion = area_discussion(CONFIG["afd_office"])
    except Exception as e:  # noqa: BLE001
        note_error("NWS forecast discussion", e)
    posts = []
    for f in CONFIG.get("feeds", []):
        try:
            posts += blog_posts(f)
        except Exception as e:  # noqa: BLE001
            note_error(f"Feed {f['name']}", e)

    # takes
    ranked = sorted((s for s in spots if s.get("d7")), key=lambda s: -s["d7"]["median"])
    digest = {
        "today": today.isoformat(),
        "anchor": primary["d7"]["median"] if primary.get("d7") else None,
        "local": [{"name": s["name"], "kind": s["kind"],
                   "days": [{"date": d["date"], "median": d.get("median"), "min": d.get("min"), "max": d.get("max")} for d in s["days"]],
                   "next_72h": s["h72"], "next_7d": s["d7"], "by_model": s["by_model"],
                   "nws_forecasters": s.get("nws")}
                  for s in spots if s["group"] == "local"],
        "region_top": [{"name": s["name"], "drive": s["drive"], "kind": s["kind"],
                        "next_72h": s["h72"], "next_7d": s["d7"]} for s in ranked[:6]],
        "avalanche": local_avy and {k: local_avy.get(k) for k in ("zone", "danger", "level", "bottom_line", "warning")},
        "nws_discussion": discussion and {k: (discussion.get(k) or "")[:1500] for k in ("key_messages", "synopsis", "short_term", "long_term")},
        "nws_alerts": [a["event"] for a in alerts],
        "snow_ratio_note": f"Model snow uses a {CONFIG.get('snow_ratio', 10)}:1 snow-to-liquid ratio.",
    }
    take = claude_take(digest, state)
    if take:
        state["take"] = take

    active = evaluate_alerts(spots, alerts, local_avy, state)

    data = {
        "generated_at": iso(NOW),
        "timezone": CONFIG["timezone"],
        "home": {"name": home["name"], "week": week},
        "primary_local": CONFIG["primary_local"],
        "models": {m: CONFIG["models"][m] for m in model_ids},
        "snow_ratio": CONFIG.get("snow_ratio", 10),
        "spots": spots,
        "avalanche": local_avy,
        "nws_alerts": alerts,
        "discussion": discussion,
        "posts": posts,
        "take": take,
        "rules_take": rules_take(primary, primary.get("nws")),
        "alerts_active": active,
        "thresholds": CONFIG["alerts"],
        "errors": ERRORS,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1))
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))
    print(f"Wrote {OUT} with {len(spots)} spots, {len(model_ids)} models, {len(ERRORS)} warnings, {len(active)} active alerts.")


if __name__ == "__main__":
    main()
