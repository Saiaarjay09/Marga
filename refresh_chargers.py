"""
Self-updating charger registry refresh.

Pulls the full national Open Charge Map listing for India and overwrites
every_charger_india.json with a fresh snapshot (chargers open/close and get
added constantly, so a full re-pull rather than an incremental patch keeps
the file honest). This script is meant to be run on a schedule (daily is
plenty -- new charging sites don't appear hourly): a cron job, a GitHub
Actions workflow, or a manually-triggered run all work the same way.

Requires the OCM_API_KEY environment variable (get a free key at
https://openchargemap.org/site/developerinfo). It is intentionally never
hardcoded here -- an earlier version of this script had a real key
committed to git, which is a leaked secret the moment a repo is public.

The Marga server calls run_refresh() itself once a day, so you normally never
run this by hand. Manual run:  python refresh_chargers.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REGISTRY_PATH = "every_charger_india.json"
OCM_BASE_URL = "https://api.openchargemap.io/v3/poi"


def fetch_live_india_chargers(api_key: str) -> list:
    print("Connecting to Open Charge Map...")
    params = {
        "output": "json",
        "countrycode": "IN",
        "maxresults": 50000,
        "compact": "true",
        "verbose": "false",
        "key": api_key,
    }
    headers = {"User-Agent": "EVCopilotApp/1.0", "Accept": "application/json"}

    response = requests.get(OCM_BASE_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    raw_data = response.json()
    print(f"Fetched {len(raw_data)} raw records.")

    master_list = []
    for item in raw_data:
        addr = item.get("AddressInfo", {})
        if not (addr.get("Latitude") and addr.get("Longitude")):
            continue

        connections = item.get("Connections", []) or []
        power_kw = 50
        if connections:
            power_kw = max((c.get("PowerKW") or 0) for c in connections) or 50

        master_list.append({
            "ID": item.get("ID"),
            "AddressInfo": {
                "Title": addr.get("Title", "Public EV Charger"),
                "Latitude": float(addr["Latitude"]),
                "Longitude": float(addr["Longitude"]),
                "AddressLine1": addr.get("AddressLine1", "Highway Access Corridor"),
            },
            "Connections": connections or [{"PowerKW": power_kw}],
            "OperatorInfo": item.get("OperatorInfo", {}),
            "SubmissionStatus": item.get("SubmissionStatus", {}),
            "StatusType": item.get("StatusType", {}),
        })

    return master_list


def run_refresh(api_key: str | None = None, path: str = REGISTRY_PATH) -> bool:
    """Re-pull the national registry. Returns True if the file was rewritten."""
    api_key = api_key or os.environ.get("OCM_API_KEY")
    if not api_key:
        print("OCM_API_KEY is not set. Get a free key at https://openchargemap.org/site/developerinfo "
              "and put it in .env (see .env.example).")
        return False

    try:
        master_list = fetch_live_india_chargers(api_key)
    except requests.exceptions.RequestException as e:
        print(f"Network error while refreshing chargers: {e}")
        return False

    if not master_list:
        print("Refresh returned zero valid records; keeping the existing file untouched.")
        return False

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "count": len(master_list),
        "chargers": master_list,
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp_path, path)  # atomic, so a reader never sees a half-written file

    print(f"Wrote {len(master_list)} chargers to {path}.")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if run_refresh() else 1)
