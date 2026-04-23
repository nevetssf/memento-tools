#!/usr/bin/env python3
"""
journal-weather.py — Fetch weather, log to journal, optionally send via Signal.

Usage:
  python3 journal-weather.py               # Current location, log + signal
  python3 journal-weather.py --no-signal   # Log only, skip Signal
  python3 journal-weather.py --location "New York, NY"
  python3 journal-weather.py --date YYYY-MM-DD
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from journal_fm import get_current_location, get_local_date, get_journal_path
from localtime import get_localtime
from config import LOCATION_FILE, SIGNAL_TARGET

WEATHER_ICONS = {
    "113": "☀️", "116": "⛅", "119": "☁️", "122": "☁️",
    "143": "🌫️", "176": "🌦️", "179": "🌨️", "182": "🌧️",
    "185": "🌧️", "200": "⛈️", "227": "🌨️", "230": "❄️",
    "248": "🌫️", "260": "🌫️", "263": "🌦️", "266": "🌧️",
    "281": "🌧️", "284": "🌧️", "293": "🌦️", "296": "🌧️",
    "299": "🌧️", "302": "🌧️", "305": "🌧️", "308": "🌧️",
    "311": "🌧️", "314": "🌧️", "317": "🌨️", "320": "🌨️",
    "323": "🌨️", "326": "🌨️", "329": "❄️", "332": "❄️",
    "335": "❄️", "338": "❄️", "350": "🌧️", "353": "🌦️",
    "356": "🌧️", "359": "🌧️", "362": "🌨️", "365": "🌨️",
    "368": "🌨️", "371": "❄️", "374": "🌨️", "377": "🌨️",
    "386": "⛈️", "389": "⛈️", "392": "⛈️", "395": "❄️",
}


def hour_label(time_str: str) -> str:
    """Convert wttr.in time string ('0','300','1500') to '12am','3am','3pm' etc."""
    h = int(time_str) // 100
    if h == 0:
        return "12am"
    elif h < 12:
        return f"{h}am"
    elif h == 12:
        return "12pm"
    else:
        return f"{h - 12}pm"


def get_weather(location: str) -> str | None:
    """Fetch detailed weather from wttr.in JSON API. Returns formatted multi-line string."""
    loc = location.replace(' ', '+').replace(',', '')
    try:
        result = subprocess.run(
            f'curl -s "wttr.in/{loc}?format=j1"',
            shell=True, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
    except Exception:
        return None

    try:
        cur = data["current_condition"][0]
        today = data["weather"][0]
        hourly = today["hourly"]

        # Current conditions
        icon = WEATHER_ICONS.get(cur["weatherCode"], "🌡️")
        desc = cur["weatherDesc"][0]["value"].strip()
        temp = cur["temp_C"]
        feels = cur["FeelsLikeC"]
        humidity = cur["humidity"]
        wind_dir = cur["winddir16Point"]
        wind_mph = cur["windspeedMiles"]

        # Today's high/low with time of occurrence
        max_c = int(today["maxtempC"])
        min_c = int(today["mintempC"])
        high_hour = max(hourly, key=lambda h: int(h["tempC"]))
        low_hour = min(hourly, key=lambda h: int(h["tempC"]))
        high_time = hour_label(high_hour["time"])
        low_time = hour_label(low_hour["time"])

        # Precipitation: max chance and total for the day
        rain_chances = [int(h["chanceofrain"]) for h in hourly]
        snow_chances = [int(h["chanceofsnow"]) for h in hourly]
        max_rain = max(rain_chances)
        max_snow = max(snow_chances)
        total_precip = sum(float(h["precipInches"]) for h in hourly)

        if max_snow > max_rain and max_snow > 10:
            precip_str = f"Snow: {max_snow}%"
        elif max_rain > 10:
            precip_str = f"Rain: {max_rain}%"
            if total_precip > 0:
                precip_str += f", {total_precip:.2f}\""
        else:
            precip_str = "Precip: <10%"

        # Wind range through the day
        wind_speeds = [int(h["windspeedMiles"]) for h in hourly]
        wind_gusts = [int(h["WindGustMiles"]) for h in hourly]
        wind_min = min(wind_speeds)
        wind_max = max(wind_speeds)
        gust_max = max(wind_gusts)
        wind_dirs = [h["winddir16Point"] for h in hourly]
        dominant_dir = max(set(wind_dirs), key=wind_dirs.count)

        if wind_min == wind_max:
            wind_range = f"{wind_max}mph"
        else:
            wind_range = f"{wind_min}–{wind_max}mph"
        if gust_max > wind_max + 5:
            wind_range += f" (gusts {gust_max}mph)"

        # Format location name from API response
        area = data.get("nearest_area", [{}])[0]
        area_name = area.get("areaName", [{}])[0].get("value", location)
        region = area.get("region", [{}])[0].get("value", "")
        loc_display = f"{area_name}, {region}" if region else area_name

        line1 = f"{icon} {loc_display}: {desc} {temp}°C (feels {feels}°C) · {humidity}% humidity · {wind_dir} {wind_mph}mph"
        line2 = f"High {max_c}°C @ {high_time} · Low {min_c}°C @ {low_time} · {precip_str} · Wind {dominant_dir} {wind_range}"

        return f"{line1}\n{line2}"

    except (KeyError, IndexError, ValueError):
        return None


def send_signal(message: str) -> bool:
    try:
        result = subprocess.run(
            f'openclaw message send --target {SIGNAL_TARGET} --message "{message}"',
            shell=True, capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Log weather to journal and optionally send via Signal")
    parser.add_argument("--location", help="Location override (default: current from LOCATION.md)")
    parser.add_argument("--date", help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--no-signal", action="store_true", dest="no_signal", help="Skip Signal message")
    args = parser.parse_args()

    location = args.location or get_current_location()
    date_str = args.date or get_local_date()
    time_info = get_localtime(location=location)
    timestamp = time_info["timestamp"]

    weather = get_weather(location)
    entry = weather if weather else f"⚠️ Weather unavailable for {location}"

    # Log to journal
    log_script = Path(__file__).parent / "journal-log.py"
    subprocess.run(
        ["python3", str(log_script), "--entry", entry, "--time", timestamp, "--date", date_str],
        capture_output=True, text=True, timeout=10
    )

    # Signal
    if not args.no_signal:
        send_signal(entry)

    print(json.dumps({"date": date_str, "weather": entry, "signal": not args.no_signal}))


if __name__ == "__main__":
    main()
