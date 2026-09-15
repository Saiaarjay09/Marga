"""
Daily charger health check.

There's no way to physically probe a public charging station's plug from a
script, so this uses the next best signal Open Charge Map exposes:
  - the operator-reported StatusType.IsOperational flag
  - whether the POI is still a verified SubmissionStatus (ID 100) record
  - recent driver check-in comments (CheckinStatusType.IsPositive), which
    is literally other drivers reporting "this worked" / "this was broken"

Chargers are checked in batches of up to 100 IDs per Open Charge Map
request (the API's `chargepointid` filter). Results are written to
charger_health.json, keyed by charger ID, and merged with yesterday's
results so a station that keeps failing accumulates a consecutive_failures
streak instead of resetting every run.

The router (core/simulator.py) reads this file through
core/charger_registry.py to skip stations currently reported down, and
falls back to them only if nothing else is reachable.

Run daily via cron / GitHub Actions / a scheduled task:
    OCM_API_KEY=xxxx python check_charger_health.py
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from core.charger_registry import HEALTH_PATH, REGISTRY_PATH, load_health, load_registry

OCM_BASE_URL = "https://api.openchargemap.io/v3/poi"
BATCH_SIZE = 100
RECENT_COMMENT_WINDOW_DAYS = 30
FAULT_REPORT_WINDOW_DAYS = 14


def _fetch_batch_status(ids: List[Any], api_key: str) -> List[Dict[str, Any]]:
    params = {
        "output": "json",
        "chargepointid": ",".join(str(i) for i in ids),
        "compact": "false",
        "verbose": "true",
        "maxresults": len(ids),
    }
    if api_key:
        params["key"] = api_key
    headers = {"User-Agent": "EVCopilotApp/1.0", "Accept": "application/json"}
    resp = requests.get(OCM_BASE_URL, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _assess(poi: Dict[str, Any]) -> Dict[str, Any]:
    confidence = 50
    reasons = []

    status_type = poi.get("StatusType") or {}
    is_operational = status_type.get("IsOperational")
    if is_operational is True:
        confidence += 20
        reasons.append("reported operational")
    elif is_operational is False:
        confidence -= 45
        reasons.append("reported NOT operational by operator")

    submission = poi.get("SubmissionStatus") or {}
    if submission.get("ID") == 100:
        confidence += 10
    else:
        confidence -= 15
        reasons.append("not a verified submission")

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=RECENT_COMMENT_WINDOW_DAYS)
    fault_cutoff = now - timedelta(days=FAULT_REPORT_WINDOW_DAYS)

    recent_fault_report = False
    positive_checkins = 0
    for comment in (poi.get("UserComments") or []):
        date_str = comment.get("DateCreated")
        if not date_str:
            continue
        try:
            created_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created_at < recent_cutoff:
            continue

        checkin_status = comment.get("CheckinStatusType") or {}
        is_positive = checkin_status.get("IsPositive")
        if is_positive is True:
            confidence += 8
            positive_checkins += 1
        elif is_positive is False:
            confidence -= 20
            if created_at >= fault_cutoff:
                recent_fault_report = True

    if positive_checkins:
        reasons.append(f"{positive_checkins} positive check-in(s) in last {RECENT_COMMENT_WINDOW_DAYS}d")
    if recent_fault_report:
        reasons.append(f"driver-reported fault in last {FAULT_REPORT_WINDOW_DAYS}d")

    confidence = max(0, min(100, confidence))
    is_working = confidence >= 40 and is_operational is not False and not recent_fault_report

    if not reasons:
        reasons.append("no recent signal, defaulting to baseline confidence")

    return {
        "is_working": is_working,
        "confidence": confidence,
        "reason": "; ".join(reasons),
    }


def run_health_check(registry_path: str = REGISTRY_PATH, health_path: str = HEALTH_PATH) -> None:
    api_key = os.environ.get("OCM_API_KEY", "")
    chargers, _ = load_registry(registry_path)
    if not chargers:
        print(f"No chargers found in {registry_path}; nothing to check.")
        return

    previous_health = load_health(health_path)
    ids = [c.get("ID") for c in chargers if c.get("ID") is not None]
    now_iso = datetime.now(timezone.utc).isoformat()

    new_health: Dict[str, Dict[str, Any]] = {}
    checked = 0
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i:i + BATCH_SIZE]
        try:
            pois = _fetch_batch_status(batch, api_key)
        except requests.exceptions.RequestException as e:
            print(f"Batch {i // BATCH_SIZE + 1} failed: {e}")
            continue

        returned_ids = set()
        for poi in pois:
            poi_id = poi.get("ID")
            if poi_id is None:
                continue
            returned_ids.add(poi_id)
            assessment = _assess(poi)
            prev = previous_health.get(str(poi_id), {})
            streak = prev.get("consecutive_failures", 0)
            assessment["consecutive_failures"] = (streak + 1) if not assessment["is_working"] else 0
            assessment["last_checked"] = now_iso
            new_health[str(poi_id)] = assessment
            checked += 1

        # IDs that no longer come back from OCM at all (delisted/removed) -- mark down.
        for missing_id in set(batch) - returned_ids:
            prev = previous_health.get(str(missing_id), {})
            new_health[str(missing_id)] = {
                "is_working": False,
                "confidence": 10,
                "reason": "no longer listed on Open Charge Map",
                "consecutive_failures": prev.get("consecutive_failures", 0) + 1,
                "last_checked": now_iso,
            }

        time.sleep(0.5)  # be polite to the free API tier

    with open(health_path, "w", encoding="utf-8") as f:
        json.dump(new_health, f, indent=2)

    down_count = sum(1 for v in new_health.values() if not v["is_working"])
    print(f"Health-checked {checked} chargers. {down_count} currently flagged as not working. "
          f"Written to {health_path}.")


if __name__ == "__main__":
    run_health_check()
