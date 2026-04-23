#!/usr/bin/env python3
"""
localtime.py — Return the current local time for a given location.

Usage:
  python3 localtime.py "San Francisco"
  python3 localtime.py "Boulder"
  python3 localtime.py "Athens, Greece"
  python3 localtime.py "Reykjavik"           # geocoded on the fly
  python3 localtime.py --list                 # show all known locations
  python3 localtime.py "Frankfurt" --learn "Wetzlar, Germany"
                                              # resolve Frankfurt, save alias for Wetzlar

Output (JSON):
  {
    "location": "San Francisco, CA",
    "timezone": "America/Los_Angeles",
    "abbreviation": "PDT",
    "utc_offset": "-07:00",
    "datetime": "2026-03-28T15:30:00",
    "time": "15:30",
    "date": "2026-03-28",
    "timestamp": "15:30 PDT"   ← use this for journal logs
  }

Resolution order:
  1. Learned aliases (localtime-aliases.json)
  2. Hardcoded LOCATIONS table
  3. Nominatim (OpenStreetMap) geocoding + TimeAPI.io
  4. Open-Meteo geocoding + TimeAPI.io
  5. Structured "unresolved" error (agent should ask user for nearby city)
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))
from config import LOCATION_FILE as _LOCATION_FILE

ALIASES_FILE = Path(__file__).parent.parent / "localtime-aliases.json"

# ---------------------------------------------------------------------------
# Location → IANA timezone table
# Each entry: (canonical_name, iana_zone, [aliases...])
# ---------------------------------------------------------------------------

LOCATIONS = [
    # United States
    ("San Francisco, CA",   "America/Los_Angeles",  ["san francisco", "sf", "san fran", "the city", "soma"]),
    ("Los Angeles, CA",     "America/Los_Angeles",  ["los angeles", "la", "hollywood", "santa monica", "venice beach"]),
    ("Seattle, WA",         "America/Los_Angeles",  ["seattle", "bellevue", "kirkland"]),
    ("Portland, OR",        "America/Los_Angeles",  ["portland"]),
    ("Las Vegas, NV",       "America/Los_Angeles",  ["las vegas", "vegas"]),
    ("Boulder, CO",         "America/Denver",       ["boulder", "boulder co"]),
    ("Denver, CO",          "America/Denver",       ["denver"]),
    ("Salt Lake City, UT",  "America/Denver",       ["salt lake", "slc"]),
    ("Phoenix, AZ",         "America/Phoenix",      ["phoenix", "scottsdale", "tempe"]),
    ("Chicago, IL",         "America/Chicago",      ["chicago"]),
    ("Dallas, TX",          "America/Chicago",      ["dallas", "fort worth", "dfw"]),
    ("Houston, TX",         "America/Chicago",      ["houston"]),
    ("Minneapolis, MN",     "America/Chicago",      ["minneapolis", "twin cities"]),
    ("New Orleans, LA",     "America/Chicago",      ["new orleans", "nola"]),
    ("New York, NY",        "America/New_York",     ["new york", "nyc", "manhattan", "brooklyn", "queens"]),
    ("Boston, MA",          "America/New_York",     ["boston"]),
    ("Washington, DC",      "America/New_York",     ["washington dc", "dc", "washington d.c."]),
    ("Miami, FL",           "America/New_York",     ["miami"]),
    ("Atlanta, GA",         "America/New_York",     ["atlanta"]),
    ("Philadelphia, PA",    "America/New_York",     ["philadelphia", "philly"]),
    ("Honolulu, HI",        "Pacific/Honolulu",     ["honolulu", "hawaii", "maui", "oahu"]),
    ("Anchorage, AK",       "America/Anchorage",    ["anchorage", "alaska"]),

    # Europe
    ("London, UK",          "Europe/London",        ["london", "england", "uk", "united kingdom"]),
    ("Paris, France",       "Europe/Paris",         ["paris", "france"]),
    ("Berlin, Germany",     "Europe/Berlin",        ["berlin", "germany", "munich", "frankfurt", "hamburg", "wetzlar"]),
    ("Amsterdam, NL",       "Europe/Amsterdam",     ["amsterdam", "netherlands", "holland"]),
    ("Brussels, Belgium",   "Europe/Brussels",      ["brussels", "belgium"]),
    ("Zurich, Switzerland", "Europe/Zurich",        ["zurich", "zurich", "switzerland", "geneva", "bern"]),
    ("Rome, Italy",         "Europe/Rome",          ["rome", "italy", "milan", "florence", "venice"]),
    ("Madrid, Spain",       "Europe/Madrid",        ["madrid", "spain", "barcelona"]),
    ("Lisbon, Portugal",    "Europe/Lisbon",        ["lisbon", "portugal"]),
    ("Stockholm, Sweden",   "Europe/Stockholm",     ["stockholm", "sweden"]),
    ("Oslo, Norway",        "Europe/Oslo",          ["oslo", "norway"]),
    ("Copenhagen, Denmark", "Europe/Copenhagen",    ["copenhagen", "denmark"]),
    ("Helsinki, Finland",   "Europe/Helsinki",      ["helsinki", "finland"]),
    ("Warsaw, Poland",      "Europe/Warsaw",        ["warsaw", "poland"]),
    ("Prague, Czech Rep.",  "Europe/Prague",        ["prague", "czech"]),
    ("Vienna, Austria",     "Europe/Vienna",        ["vienna", "austria"]),
    ("Budapest, Hungary",   "Europe/Budapest",      ["budapest", "hungary"]),
    ("Athens, Greece",      "Europe/Athens",        ["athens", "greece", "thessaloniki", "tolo", "nafplio"]),
    ("Istanbul, Turkey",    "Europe/Istanbul",      ["istanbul", "turkey", "ankara"]),
    ("Kyiv, Ukraine",       "Europe/Kyiv",          ["kyiv", "kiev", "ukraine"]),
    ("Moscow, Russia",      "Europe/Moscow",        ["moscow", "russia"]),

    # Asia-Pacific
    ("Tokyo, Japan",        "Asia/Tokyo",           ["tokyo", "japan", "osaka", "kyoto"]),
    ("Seoul, Korea",        "Asia/Seoul",           ["seoul", "korea"]),
    ("Beijing, China",      "Asia/Shanghai",        ["beijing", "shanghai", "china", "shenzhen", "guangzhou"]),
    ("Hong Kong",           "Asia/Hong_Kong",       ["hong kong", "hk"]),
    ("Taipei, Taiwan",      "Asia/Taipei",          ["taipei", "taiwan"]),
    ("Singapore",           "Asia/Singapore",       ["singapore"]),
    ("Bangkok, Thailand",   "Asia/Bangkok",         ["bangkok", "thailand"]),
    ("Kuala Lumpur, MY",    "Asia/Kuala_Lumpur",    ["kuala lumpur", "kl", "malaysia"]),
    ("Jakarta, Indonesia",  "Asia/Jakarta",         ["jakarta", "indonesia", "bali"]),
    ("Mumbai, India",       "Asia/Kolkata",         ["mumbai", "delhi", "india", "bangalore", "kolkata", "chennai"]),
    ("Dubai, UAE",          "Asia/Dubai",           ["dubai", "uae", "abu dhabi"]),
    ("Tel Aviv, Israel",    "Asia/Jerusalem",       ["tel aviv", "israel", "jerusalem"]),
    ("Sydney, Australia",   "Australia/Sydney",     ["sydney", "australia", "melbourne", "canberra"]),
    ("Brisbane, Australia", "Australia/Brisbane",   ["brisbane", "queensland"]),
    ("Adelaide, Australia", "Australia/Adelaide",   ["adelaide"]),
    ("Perth, Australia",    "Australia/Perth",      ["perth"]),
    ("Auckland, NZ",        "Pacific/Auckland",     ["auckland", "new zealand", "wellington"]),

    # Americas (non-US)
    ("Toronto, Canada",     "America/Toronto",      ["toronto", "ottawa", "montreal"]),
    ("Vancouver, Canada",   "America/Vancouver",    ["vancouver", "canada", "bc"]),
    ("Calgary, Canada",     "America/Edmonton",     ["calgary", "edmonton", "alberta"]),
    ("Mexico City, MX",     "America/Mexico_City",  ["mexico city", "mexico", "guadalajara"]),
    ("São Paulo, Brazil",   "America/Sao_Paulo",    ["sao paulo", "são paulo", "rio", "brazil", "brasil"]),
    ("Buenos Aires, AR",    "America/Argentina/Buenos_Aires", ["buenos aires", "argentina"]),
    ("Santiago, Chile",     "America/Santiago",     ["santiago", "chile"]),
    ("Bogotá, Colombia",    "America/Bogota",       ["bogota", "colombia"]),
    ("Lima, Peru",          "America/Lima",         ["lima", "peru"]),

    # Africa
    ("Cairo, Egypt",        "Africa/Cairo",         ["cairo", "egypt"]),
    ("Lagos, Nigeria",      "Africa/Lagos",         ["lagos", "nigeria"]),
    ("Nairobi, Kenya",      "Africa/Nairobi",       ["nairobi", "kenya"]),
    ("Johannesburg, SA",    "Africa/Johannesburg",  ["johannesburg", "cape town", "south africa"]),
    ("Casablanca, Morocco", "Africa/Casablanca",    ["casablanca", "morocco"]),

    # UTC fallback
    ("UTC",                 "UTC",                  ["utc", "gmt", "universal"]),
]

# Build flat lookup: alias → (canonical_name, iana_zone)
_ALIAS_MAP = {}
for canonical, zone, aliases in LOCATIONS:
    for alias in aliases:
        _ALIAS_MAP[alias.lower()] = (canonical, zone)
    # Also index the canonical name itself
    _ALIAS_MAP[canonical.lower()] = (canonical, zone)


# ---------------------------------------------------------------------------
# Learned aliases — persisted in localtime-aliases.json
# Format: {"wetzlar, germany": {"location": "Wetzlar, Germany", "timezone": "Europe/Berlin"}}
# ---------------------------------------------------------------------------

def _load_aliases():
    try:
        return json.loads(ALIASES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_aliases(aliases):
    ALIASES_FILE.write_text(json.dumps(aliases, indent=2, ensure_ascii=False) + "\n")


def learn_alias(original_query, resolved_name, iana_zone):
    """Save a learned alias so future lookups resolve instantly."""
    aliases = _load_aliases()
    aliases[original_query.strip().lower()] = {
        "location": original_query.strip(),
        "timezone": iana_zone,
    }
    _save_aliases(aliases)


# ---------------------------------------------------------------------------
# Resolution chain
# ---------------------------------------------------------------------------

def resolve_location(query):
    """
    Resolve a location query to (display_name, iana_zone).

    Order: learned aliases → hardcoded list → Nominatim → Open-Meteo → unresolved error.
    """
    q = query.strip().lower()

    # 1. Learned aliases
    aliases = _load_aliases()
    if q in aliases:
        entry = aliases[q]
        return (entry["location"], entry["timezone"])

    # 2. Hardcoded — exact match
    if q in _ALIAS_MAP:
        return _ALIAS_MAP[q]

    # 3. Hardcoded — word-boundary substring match
    matches = []
    for alias, entry in _ALIAS_MAP.items():
        if re.search(r'(?:^|[\s,;/\-])' + re.escape(alias) + r'(?:$|[\s,;/\-])', q):
            if entry not in matches:
                matches.append(entry)
        elif re.search(r'(?:^|[\s,;/\-])' + re.escape(q) + r'(?:$|[\s,;/\-])', alias):
            if entry not in matches:
                matches.append(entry)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = [m[0] for m in matches]
        raise ValueError(f"Ambiguous location '{query}' — matches: {names}. Be more specific.")

    # 4. Geocode via Nominatim + TimeAPI.io
    result = _geocode_nominatim(query)
    if result:
        return result

    # 5. Geocode via Open-Meteo + TimeAPI.io
    result = _geocode_openmeteo(query)
    if result:
        return result

    # 6. All lookups failed — structured error for agent
    raise LocationUnresolved(query)


class LocationUnresolved(Exception):
    """Raised when no resolver could identify the location."""
    def __init__(self, query):
        self.query = query
        super().__init__(query)


def _tz_from_coords(lat, lon):
    """Look up IANA timezone from coordinates via TimeAPI.io."""
    url = f"https://timeapi.io/api/timezone/coordinate?latitude={lat}&longitude={lon}"
    try:
        resp = json.loads(urllib.request.urlopen(url, timeout=5).read())
        return resp["timeZone"]
    except (urllib.error.URLError, TimeoutError, KeyError):
        return None


def _geocode_nominatim(query):
    """Geocode via Nominatim (OpenStreetMap). Returns (display_name, iana_zone) or None."""
    encoded = urllib.request.quote(query)
    url = (
        f"https://nominatim.openstreetmap.org/search"
        f"?q={encoded}&format=json&limit=1&accept-language=en"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "localtime-script/1.0"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
    except (urllib.error.URLError, TimeoutError):
        return None

    if not resp:
        return None

    place = resp[0]
    lat, lon = place["lat"], place["lon"]
    display_name = place.get("display_name", query).split(",")[0]

    iana_zone = _tz_from_coords(lat, lon)
    if not iana_zone:
        return None

    return (display_name, iana_zone)


def _geocode_openmeteo(query):
    """Geocode via Open-Meteo. Returns (display_name, iana_zone) or None."""
    encoded = urllib.request.quote(query)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded}&count=1&language=en"
    try:
        resp = json.loads(urllib.request.urlopen(url, timeout=5).read())
    except (urllib.error.URLError, TimeoutError):
        return None

    results = resp.get("results")
    if not results:
        return None

    place = results[0]
    display_name = place.get("name", query)

    # Open-Meteo often includes timezone directly
    iana_zone = place.get("timezone")
    if iana_zone:
        return (display_name, iana_zone)

    # Fall back to coordinate-based lookup
    lat, lon = place.get("latitude"), place.get("longitude")
    if lat is not None and lon is not None:
        iana_zone = _tz_from_coords(lat, lon)
        if iana_zone:
            return (display_name, iana_zone)

    return None


# ---------------------------------------------------------------------------
# Main time lookup
# ---------------------------------------------------------------------------

def get_localtime(location=None, zone=None):
    """Return a dict with the current time for the given location or IANA zone name."""
    if zone:
        canonical = zone
        iana = zone
    else:
        canonical, iana = resolve_location(location)

    tz = ZoneInfo(iana)
    now = datetime.now(tz)

    utc_offset_secs = now.utcoffset().total_seconds()
    sign = "+" if utc_offset_secs >= 0 else "-"
    h, m = divmod(abs(int(utc_offset_secs)), 3600)
    utc_offset = f"{sign}{h:02d}:{m // 60:02d}"

    return {
        "location":     canonical,
        "timezone":     iana,
        "abbreviation": now.strftime("%Z"),
        "utc_offset":   utc_offset,
        "datetime":     now.strftime("%Y-%m-%dT%H:%M:%S"),
        "date":         now.strftime("%Y-%m-%d"),
        "time":         now.strftime("%H:%M"),
        "timestamp":    now.strftime(f"%H:%M {now.strftime('%Z')}"),
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Return current local time for a location."
    )
    parser.add_argument("location", nargs="?", help="Location name (fuzzy matched)")
    parser.add_argument("--zone", help="IANA timezone name (e.g. America/Los_Angeles)")
    parser.add_argument("--learn", metavar="ALIAS",
                        help="Save ALIAS as a learned alias for this location's timezone")
    parser.add_argument("--list", action="store_true", help="List all known locations")
    parser.add_argument("--pretty", action="store_true", help="Human-readable output")
    args = parser.parse_args()

    if args.list:
        for canonical, zone, _ in sorted(LOCATIONS, key=lambda x: x[0]):
            print(f"  {canonical:<35} {zone}")
        aliases = _load_aliases()
        if aliases:
            print("\n  Learned aliases:")
            for key, val in sorted(aliases.items()):
                print(f"    {val['location']:<33} {val['timezone']}")
        return

    if not args.location and not args.zone:
        # Default to LOCATION.md via config
        loc_file = _LOCATION_FILE
        try:
            loc = loc_file.read_text().strip()
            if loc:
                args.location = loc
        except Exception:
            pass
        if not args.location:
            parser.error("Provide a location name or --zone")

    try:
        result = get_localtime(location=args.location, zone=args.zone)
    except LocationUnresolved as e:
        err = {
            "error": "unresolved",
            "query": e.query,
            "suggestion": (
                f"Could not resolve '{e.query}'. "
                "Ask the user for a nearby larger city, then re-run with: "
                f"localtime.py \"<nearby city>\" --learn \"{e.query}\""
            ),
        }
        print(json.dumps(err), file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)

    # --learn: save alias mapping
    if args.learn:
        learn_alias(args.learn, args.learn, result["timezone"])
        result["learned_alias"] = args.learn

    if args.pretty:
        print(f"Location:  {result['location']}")
        print(f"Timezone:  {result['timezone']} ({result['abbreviation']}, UTC{result['utc_offset']})")
        print(f"Date/Time: {result['datetime']} {result['abbreviation']}")
        print(f"Timestamp: {result['timestamp']}")
        if args.learn:
            print(f"Learned:   '{args.learn}' → {result['timezone']}")
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
