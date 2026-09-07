"""Fill a local device with SYNTHETIC telemetry so the insight engine has something to chew on.

    ┌───────────────────────────────────────────────────────────────────┐
    │  EVERYTHING THIS SCRIPT WRITES IS INVENTED.                       │
    │                                                                   │
    │  No person was observed. No sensor produced any of it. It exists  │
    │  so the analytics screens can be looked at before hardware        │
    │  arrives, and for no other reason.                                │
    └───────────────────────────────────────────────────────────────────┘

Three things keep that from becoming a lie told later:

* **It refuses to run outside a local environment.** The API reports its own
  environment at ``/health``; anything but ``local`` and this exits. Seeded
  rows in a real database would be indistinguishable from observations after
  a week, and every insight drawn from them would be a fabrication with a
  confidence score attached.
* **Every row is marked.** Each event carries ``data.synthetic = true`` and
  the name of this generator, so a row's origin survives in the database
  rather than living in whoever remembers running it.
* **It goes through the real device protocol.** The events are sent over the
  WebSocket a real NOVA uses, validated by the same Pydantic schema. That
  makes this a test of the ingest path as well as a seeder -- and it means
  the seeder cannot write something a real device could not.

What it does *not* do is tune the data until the insights look impressive.
The pattern below is one plausible desk worker. Some insights will fire and
some will not, and which ones do is the interesting part: the engine's
thresholds are what decide, not this file.

Usage:

    python scripts/seed_insights.py
    python scripts/seed_insights.py --timezone Europe/Bucharest --days 21
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import websockets

HEALTH = "http://127.0.0.1:8000/health"
BASE = "http://127.0.0.1:8000/api/v1"
WS = "ws://127.0.0.1:8000/api/v1/devices/ws"

# The protocol caps a batch two ways: 100 events, and 16 KiB of frame. Which
# one binds depends on how much each event carries, and these events carry a
# `data` marker that a real device's do not -- so 100 of them come to about
# 23 KiB and the byte cap is the one that bites. The batch is sized against
# the bytes rather than a guessed count.
MAX_FRAME_BYTES = 16384
# Leave room for the envelope: type, version, id, and the array brackets.
FRAME_OVERHEAD = 256
MAX_BATCH_EVENTS = 100

# Stamped into every row. Grep-able, and it survives a database dump.
MARK = {"synthetic": True, "generator": "seed_insights"}

# A seed, so two runs produce the same desk worker and a change in the
# insights means the engine changed rather than the dice.
SEED = 20260907


def show(label: str, value: object = "") -> None:
    print(f"  {label:<46} {value}")


# ---------------------------------------------------------------------------
# The invented human
# ---------------------------------------------------------------------------

# Relative weight of presence detections by local hour. Zero means NOVA sees
# nobody. The shape is a desk: arrive at nine, a mid-morning peak, thinner
# over lunch, a second afternoon peak, gone by seven.
WEEKDAY_SHAPE = {
    8: 1, 9: 4, 10: 6, 11: 5, 12: 2, 13: 1,
    14: 4, 15: 6, 16: 5, 17: 3, 18: 1,
}

# Weekends: someone passes the desk in the afternoon and that is all.
WEEKEND_SHAPE = {11: 1, 12: 1, 15: 2, 16: 2, 17: 1}

# Wednesday is the busiest day, Friday the lightest. Multipliers on the day's
# total, not on the shape -- the hours somebody keeps do not change because
# it is Wednesday, only how much they are at the desk.
WEEKDAY_WEIGHT = {0: 1.0, 1: 1.1, 2: 1.4, 3: 1.0, 4: 0.7, 5: 0.35, 6: 0.35}

# The most recent week is busier than the one before it. Deadline week.
RECENT_WEEK_MULTIPLIER = 1.4


def presence_events(
    *, days: int, tz: ZoneInfo, rng: random.Random
) -> list[dict[str, object]]:
    """One synthetic person's worth of ``person_detected`` events."""
    events: list[dict[str, object]] = []
    now_local = datetime.now(tz)
    today = now_local.date()

    for day_offset in range(days, 0, -1):
        day = today - timedelta(days=day_offset - 1)
        weekday = day.weekday()
        weekend = weekday >= 5
        shape = WEEKEND_SHAPE if weekend else WEEKDAY_SHAPE

        weight = WEEKDAY_WEIGHT[weekday]
        if day_offset <= 7:
            weight *= RECENT_WEEK_MULTIPLIER

        for hour, share in shape.items():
            count = round(share * weight * rng.uniform(0.7, 1.3))
            for _ in range(count):
                local = datetime(
                    day.year, day.month, day.day,
                    hour, rng.randrange(60), rng.randrange(60),
                    tzinfo=tz,
                )
                if local > now_local:
                    continue
                events.append({
                    "event_type": "person_detected",
                    "recorded_at": local.astimezone(UTC).isoformat(),
                    # Bounded by the schema; also by the sensor's real range.
                    "distance_cm": rng.randint(35, 110),
                    "head_yaw": rng.randint(-40, 40),
                    "state": "CURIOUS",
                    "data": {**MARK, "source": "time_of_flight"},
                })
    return events


def heartbeat_events(*, tz: ZoneInfo, rng: random.Random) -> list[dict[str, object]]:
    """Heartbeats for the last twelve hours, with two deliberate outages.

    A real NOVA beats every 30 s. The gaps here are two ten-minute silences,
    which is what the connectivity insight is meant to notice -- and what it
    reports honestly, since it cannot tell a lost connection from somebody
    unplugging the thing.

    Battery rides on these. The first four hours are a charge from 40% to
    100%; the last eight are a discharge. That is deliberate: it gives
    ``current_discharge_segment`` a rise to cut on, so the projection is
    fitted to the discharge alone rather than averaging a charge and a
    discharge into a slope describing neither.
    """
    events: list[dict[str, object]] = []
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(hours=12)

    # Two outages, at four and nine hours in.
    outages = [
        (start + timedelta(hours=4), timedelta(minutes=10)),
        (start + timedelta(hours=9), timedelta(minutes=10)),
    ]

    at = start
    while at <= now:
        if any(begin <= at < begin + length for begin, length in outages):
            at += timedelta(seconds=30)
            continue

        elapsed_hours = (at - start).total_seconds() / 3600
        if elapsed_hours <= 4:
            # Charging: 40 -> 100 over four hours.
            level = 40 + (60 * elapsed_hours / 4)
        else:
            # Discharging at about 6% an hour, with a little gauge noise --
            # a perfectly straight line would give an R² of 1.00, which no
            # real battery produces and which would make the fit check
            # meaningless as a demonstration.
            level = 100 - 6.0 * (elapsed_hours - 4) + rng.uniform(-0.8, 0.8)

        events.append({
            "event_type": "heartbeat",
            "recorded_at": at.isoformat(),
            "battery_percent": max(0, min(100, round(level))),
            "temperature_c": round(rng.uniform(28.0, 34.0), 1),
            "wifi_rssi": rng.randint(-62, -38),
            "uptime_seconds": int((at - start).total_seconds()),
            "state": "IDLE",
            "data": dict(MARK),
        })
        at += timedelta(seconds=30)

    return events


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def batches(events: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    """Split into frames that satisfy *both* server limits.

    The same problem the firmware's ``encode_batch`` solves, and it is here
    for the same reason: a batch that satisfies the count limit and busts the
    byte limit is refused whole, and the sender has no way to tell which of
    its events got through.
    """
    out: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    size = FRAME_OVERHEAD

    for event in events:
        cost = len(json.dumps(event)) + 1
        too_big = size + cost > MAX_FRAME_BYTES
        too_many = len(current) >= MAX_BATCH_EVENTS
        if current and (too_big or too_many):
            out.append(current)
            current, size = [], FRAME_OVERHEAD
        current.append(event)
        size += cost

    if current:
        out.append(current)
    return out


async def send_all(token: str, events: list[dict[str, object]]) -> int:
    """Push every event through the real device WebSocket, in protocol batches."""
    sent = 0
    chunks = batches(events)
    async with websockets.connect(f"{WS}?token={token}", max_size=2**21) as ws:
        for chunk in chunks:
            await ws.send(json.dumps({
                "type": "telemetry.batch",
                "version": 1,
                "id": str(uuid.uuid4()),
                "payload": chunk,
            }))
            reply = json.loads(await ws.recv())
            if reply.get("type") != "ack":
                raise SystemExit(f"server rejected a batch: {reply}")
            sent += len(chunk)
            print(f"\r  sending... {sent}/{len(events)} in {len(chunks)} frames",
                  end="", flush=True)
    print()
    return sent


def render(body: dict[str, object]) -> None:
    coverage = body.get("coverage", {})
    show("window", f"{body.get('window_days')} days, {body.get('timezone')}")
    show("events in window", coverage.get("total_events"))
    show("distinct days", coverage.get("distinct_days"))
    show("sufficient?", coverage.get("is_sufficient"))

    insights = body.get("insights") or []
    if not insights:
        show("insights", "none -- the engine declined to claim anything")
        show("reason", body.get("summary", ""))
        return

    print()
    for insight in insights:
        print(f"  [{insight['confidence']:>6}] {insight['headline']}")
        print(f"           {insight['detail']}")
        print(f"           n={insight['sample_size']}, "
              f"days={insight['days_observed']}, kind={insight['kind']}")
        print()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timezone", default="Europe/Bucharest",
                        help="the account's timezone; analytics bucket local hours in it")
    parser.add_argument("--days", type=int, default=21,
                        help="days of presence history (14+ unlocks weekday comparison)")
    parser.add_argument("--email", default=None, help="reuse an existing account")
    parser.add_argument("--password", default="correct-horse-battery-staple")
    args = parser.parse_args()

    try:
        tz = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        print(f"Unknown timezone: {args.timezone}")
        return 2

    print(__doc__.split("Usage:")[0].rstrip())
    print()

    rng = random.Random(SEED)

    async with httpx.AsyncClient(timeout=30) as http:
        # -- Guard ----------------------------------------------------------
        try:
            health = (await http.get(HEALTH)).json()
        except httpx.RequestError as error:
            print(f"No API at {HEALTH} ({error}). Start it first.")
            return 2

        if health.get("environment") != "local":
            print(f"REFUSING: the API reports environment={health.get('environment')!r}.")
            print("This writes invented data. It runs against a local environment only.")
            return 1
        show("environment", "local (guard passed)")

        # -- Account --------------------------------------------------------
        if args.email:
            r = await http.post(f"{BASE}/auth/login",
                                json={"email": args.email, "password": args.password})
            if r.status_code != 200:
                print(f"Login failed for {args.email}: HTTP {r.status_code}")
                return 1
            email = args.email
        else:
            email = f"seed-{uuid.uuid4().hex[:8]}@example.com"
            r = await http.post(f"{BASE}/auth/register", json={
                "email": email, "password": args.password, "display_name": "Seeded"})
            if r.status_code != 201:
                print(f"Register failed: HTTP {r.status_code} {r.text}")
                return 1

        headers = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
        show("account", email)
        show("password", args.password)

        # The analytics layer buckets by local hour in the account's timezone,
        # so this has to match the timezone the events were generated in or
        # the "usually around" band lands hours off.
        await http.patch(f"{BASE}/users/me", headers=headers,
                         json={"timezone": args.timezone})
        show("timezone", args.timezone)

        # -- Device ---------------------------------------------------------
        hardware_id = f"esp32s3-seed-{uuid.uuid4().hex[:12]}"
        r = await http.post(f"{BASE}/devices/provision", json={
            "hardware_id": hardware_id,
            "model": "ESP32-S3-Touch-AMOLED-2.06",
            "firmware_version": "0.1.0-seed"})
        prov = r.json()
        r = await http.post(f"{BASE}/devices/claim", headers=headers,
                            json={"code": prov["claim_code"], "name": "Nova (seeded)"})
        device_id = r.json()["id"]
        show("device", device_id)

        r = await http.post(f"{BASE}/devices/provision/poll",
                            json={"provisioning_token": prov["provisioning_token"]})
        token = r.json()["device_token"]

        # -- Generate and send ----------------------------------------------
        print("\n=== GENERATING (all of it invented) ===")
        presence = presence_events(days=args.days, tz=tz, rng=rng)
        heartbeats = heartbeat_events(tz=tz, rng=rng)
        show("person_detected", f"{len(presence)} over {args.days} days")
        show("heartbeat", f"{len(heartbeats)} over the last 12 hours")

        print("\n=== SENDING (real device protocol, real validation) ===")
        # Chronological, the way a device would have produced them.
        events = sorted(presence + heartbeats, key=lambda e: str(e["recorded_at"]))
        await send_all(token, events)

        # -- Read it back ---------------------------------------------------
        print("\n=== INSIGHTS ===")
        r = await http.get(f"{BASE}/devices/{device_id}/insights", headers=headers,
                           params={"window_days": 30})
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text}")
            return 1
        render(r.json())

        print("=== HOW TO LOOK AT IT ===")
        show("Swagger UI", "http://127.0.0.1:8000/docs")
        show("insights", f"GET /api/v1/devices/{device_id}/insights")
        show("raw aggregates", f"GET /api/v1/devices/{device_id}/analytics")
        show("log in as", f"{email} / {args.password}")
        print()
        print("  Every row above is marked data.synthetic = true. To clear it,")
        print("  delete the device -- its telemetry goes with it.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
