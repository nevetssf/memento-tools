#!/usr/bin/env python3
"""
weather_query.py — Build wttr.in location queries that geocode to the right place.

wttr.in reads a trailing two-letter token as an ISO country code before trying it
as a US state, so "San Francisco, CA" resolves to Charlesbourg, Quebec rather than
California. Appending an explicit country disambiguates it without disturbing the
comma, which wttr.in needs in order to pick the exact city (dropping it turned
"Boulder, CO" into Springdale, CO — see commit 9d215ff).
"""

# US states plus DC and the territories that appear in mailing addresses.
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
}

# Trailing tokens that already pin the country; don't append another one.
_COUNTRY_TOKENS = {"USA", "US", "U.S.", "U.S.A.", "UNITED STATES", "UNITED STATES OF AMERICA"}


def qualify_location(location: str) -> str:
    """Add an explicit country when a location ends in a bare US state code.

    "Boulder, CO"            -> "Boulder, CO, USA"
    "San Francisco, CA, USA" -> unchanged (already qualified)
    "Thessaloniki, Greece"   -> unchanged (no state code)
    "Boulder"                -> unchanged (nothing to disambiguate)
    """
    if not location or "," not in location:
        return location

    parts = [p.strip() for p in location.split(",")]
    if parts[-1].upper() in _COUNTRY_TOKENS:
        return location
    if parts[-1].upper() in US_STATE_CODES:
        return f"{location.rstrip().rstrip(',')}, USA"
    return location


def wttr_location(location: str) -> str:
    """Return a URL-ready wttr.in path segment for `location`."""
    return qualify_location(location).replace(" ", "+")
