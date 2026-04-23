#!/usr/bin/env python3
"""Check for important emails and weather, then notify via Signal."""

import subprocess
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import LOCATION_FILE

ACCOUNTS = [
    ("steven@stevenkasapi.net", "--client mail"),
    ("steven.j.kasapi@gmail.com", "")
]

def get_location():
    """Read current location from LOCATION.md."""
    location_file = str(LOCATION_FILE)
    try:
        with open(location_file, 'r') as f:
            return f.read().strip()
    except Exception as e:
        print(f"Error reading location: {e}", file=sys.stderr)
        return "San Francisco, CA"

def get_weather(location):
    """Get weather for the current location."""
    try:
        # Format location for URL (replace spaces with +)
        location_formatted = location.replace(' ', '+').replace(',', '')
        cmd = f'curl -s "wttr.in/{location_formatted}?format=%l:+%c+%t+(feels+like+%f),+%w+wind,+%h+humidity"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout:
            return result.stdout.strip()
    except Exception as e:
        print(f"Error getting weather: {e}", file=sys.stderr)
    return None

def search_important_emails():
    """Search for unread/important emails in inbox."""
    important_emails = []

    for account, client_flag in ACCOUNTS:
        cmd = f'GOG_KEYRING_PASSWORD="" gog gmail messages search "in:inbox is:unread" --max 20 --no-input --account {account} {client_flag} --json'
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout:
                try:
                    emails = json.loads(result.stdout)
                    if isinstance(emails, list):
                        for email in emails:
                            if isinstance(email, dict):
                                important_emails.append({
                                    'from': email.get('from', 'Unknown'),
                                    'subject': email.get('subject', '(no subject)'),
                                    'date': email.get('date', ''),
                                    'account': account
                                })
                except json.JSONDecodeError:
                    # Not valid JSON, might be empty or error
                    pass
        except Exception as e:
            print(f"Error checking {account}: {e}", file=sys.stderr)

    return important_emails

def main():
    # Get location and weather
    location = get_location()
    weather = get_weather(location)

    # Print weather first
    if weather:
        print(f"🌤️ Weather in {location}:")
        print(f"   {weather}")
        print()

    # Check emails
    emails = search_important_emails()

    if emails:
        count = len(emails)
        print(f"📧 You have {count} unread email(s) in your inbox:")
        for email in emails[:5]:  # Show first 5
            print(f"   • {email['from']}: {email['subject']}")
        if count > 5:
            print(f"   ... and {count - 5} more")
    else:
        print("📧 No unread emails in your inbox.")

if __name__ == "__main__":
    main()
