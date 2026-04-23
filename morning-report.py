#!/usr/bin/env python3
"""Morning report - send daily checklist via Signal."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import LOCATION_FILE, SIGNAL_TARGET

def get_location():
    """Read current location from LOCATION.md."""
    try:
        loc = LOCATION_FILE.read_text().strip()
        if loc:
            return loc
    except Exception as e:
        print(f"Error reading location: {e}", file=sys.stderr)
    return "San Francisco, CA"

def get_local_time(location):
    """Get local time for the location."""
    try:
        localtime_script = Path(__file__).parent / "localtime.py"
        cmd = f'python3 "{localtime_script}" "{location}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            return data.get('timestamp', 'Unknown time')
    except Exception as e:
        print(f"Error getting time: {e}", file=sys.stderr)
    return "Unknown time"

def get_weather(location):
    """Get weather for the location."""
    try:
        location_formatted = location.replace(' ', '+').replace(',', '')
        cmd = f'curl -s "wttr.in/{location_formatted}?format=%l:+%c+%t+(feels+like+%f),+%w+wind,+%h+humidity"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout:
            return result.stdout.strip()
    except Exception as e:
        print(f"Error getting weather: {e}", file=sys.stderr)
    return None

def send_signal_message(message):
    """Send message via Signal using openclaw."""
    try:
        cmd = f'openclaw message send --target {SIGNAL_TARGET} --message "{message}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception as e:
        print(f"Error sending message: {e}", file=sys.stderr)
        return False

def main():
    location = get_location()
    time = get_local_time(location)
    weather = get_weather(location)

    weather_line = f"\n🌤️ {weather}\n" if weather else ""

    message = f"""Good morning! ☀️

It's {time} in {location}.{weather_line}

**Daily Checklist:**
- [ ] Take pills
- [ ] Practice German
- [ ] Write in journal
- [ ] Walk 15K steps

How are we doing?"""

    if send_signal_message(message):
        print(f"Morning report sent at {time}")
    else:
        print("Failed to send morning report", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
