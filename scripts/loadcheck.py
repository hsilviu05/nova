"""Measure the API under concurrent load and print latency percentiles.

    docker compose up -d           # or run the API on the host
    python scripts/loadcheck.py --users 20 --rounds 10

Not a benchmark to be quoted -- a laptop, a local Postgres and the offline
chat provider are not production -- but a way to see the shape of the
numbers, catch an endpoint that is an order of magnitude off its neighbours,
and check that a change did not make something ten times slower. The same
script before and after a change is the comparison that matters.

Each simulated user: registers, reads their profile, claims a device, pushes
telemetry over the device WebSocket, sends chat messages (offline provider,
so no model latency in the numbers), lists memories, and reads analytics and
insights. Every request's wall time is recorded by endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx
import websockets

BASE = "http://127.0.0.1:8000/api/v1"
WS = "ws://127.0.0.1:8000/api/v1/devices/ws"

Timings = dict[str, list[float]]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


async def timed(timings: Timings, label: str, coro):  # type: ignore[no-untyped-def]
    start = time.perf_counter()
    result = await coro
    timings[label].append((time.perf_counter() - start) * 1000)
    return result


async def one_user(index: int, rounds: int, timings: Timings, failures: list[str]) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        email = f"load-{index}-{uuid.uuid4().hex[:6]}@example.com"
        r = await timed(
            timings,
            "POST auth/register",
            http.post(
                f"{BASE}/auth/register",
                json={
                    "email": email,
                    "password": "correct-horse-battery-staple",
                    "display_name": "L",
                },
            ),
        )
        if r.status_code != 201:
            failures.append(f"register {r.status_code}")
            return
        headers = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}

        for _ in range(rounds):
            r = await timed(timings, "GET users/me", http.get(f"{BASE}/users/me", headers=headers))
            if r.status_code != 200:
                failures.append(f"me {r.status_code}")

        # Device: provision, claim, collect the token.
        r = await timed(
            timings,
            "POST devices/provision",
            http.post(
                f"{BASE}/devices/provision",
                json={
                    "hardware_id": f"load-{uuid.uuid4().hex[:12]}",
                    "model": "ESP32-S3",
                    "firmware_version": "0",
                },
            ),
        )
        if r.status_code != 201:
            failures.append(f"provision {r.status_code}")
            return
        prov = r.json()
        r = await timed(
            timings,
            "POST devices/claim",
            http.post(
                f"{BASE}/devices/claim",
                headers=headers,
                json={"code": prov["claim_code"], "name": "L"},
            ),
        )
        if r.status_code != 201:
            failures.append(f"claim {r.status_code}")
            return
        device_id = r.json()["id"]
        r = await http.post(
            f"{BASE}/devices/provision/poll",
            json={"provisioning_token": prov["provisioning_token"]},
        )
        if r.status_code != 200 or "device_token" not in r.json():
            failures.append(f"provision/poll {r.status_code}")
            return
        token = r.json()["device_token"]

        # Telemetry over the real socket, in protocol-sized batches.
        now = datetime.now(UTC)
        async with websockets.connect(f"{WS}?token={token}") as ws:
            for round_index in range(rounds):
                batch = [
                    {
                        "event_type": "person_detected",
                        "recorded_at": (
                            now - timedelta(minutes=5 * (round_index * 20 + i))
                        ).isoformat(),
                        "distance_cm": 60 + i,
                        "state": "curious",
                        "data": {"synthetic": True, "generator": "loadcheck"},
                    }
                    for i in range(20)
                ]
                start = time.perf_counter()
                await ws.send(
                    json.dumps(
                        {
                            "type": "telemetry.batch",
                            "version": 1,
                            "id": str(uuid.uuid4()),
                            "payload": batch,
                        }
                    )
                )
                ack = json.loads(await ws.recv())
                timings["WS telemetry.batch (20)"].append((time.perf_counter() - start) * 1000)
                if ack.get("type") != "ack":
                    failures.append(f"batch {ack.get('type')}")

        # Chat with the offline provider: streaming path, no model latency.
        r = await timed(
            timings,
            "POST conversations",
            http.post(f"{BASE}/conversations", headers=headers, json={"title": "load"}),
        )
        if r.status_code != 201:
            failures.append(f"conversation {r.status_code}")
            return
        conversation = r.json()["id"]
        for i in range(rounds):
            r = await timed(
                timings,
                "POST conversations/{id}/messages",
                http.post(
                    f"{BASE}/conversations/{conversation}/messages",
                    headers=headers,
                    json={"content": f"hello {i}"},
                ),
            )
            if r.status_code != 201:
                failures.append(f"message {r.status_code}")
        r = await timed(
            timings,
            "GET conversations/{id}",
            http.get(f"{BASE}/conversations/{conversation}", headers=headers),
        )

        for label, path in (
            ("GET memories", f"{BASE}/memories"),
            ("GET devices/{id}/telemetry", f"{BASE}/devices/{device_id}/telemetry?limit=100"),
            ("GET devices/{id}/analytics", f"{BASE}/devices/{device_id}/analytics?window_days=30"),
            ("GET devices/{id}/insights", f"{BASE}/devices/{device_id}/insights?window_days=30"),
        ):
            for _ in range(max(1, rounds // 2)):
                r = await timed(timings, label, http.get(path, headers=headers))
                if r.status_code != 200:
                    failures.append(f"{label} {r.status_code}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--users", type=int, default=20, help="concurrent simulated users")
    parser.add_argument("--rounds", type=int, default=10, help="repetitions per user per endpoint")
    args = parser.parse_args()

    try:
        async with httpx.AsyncClient(timeout=5) as probe:
            health = (await probe.get("http://127.0.0.1:8000/health")).json()
    except httpx.HTTPError as error:
        print(f"no API at 127.0.0.1:8000 ({error})")
        return 2
    if health.get("environment") not in ("local", "test"):
        print(f"refusing: API reports environment={health.get('environment')!r}; this writes rows")
        return 1

    timings: Timings = defaultdict(list)
    failures: list[str] = []
    started = time.perf_counter()
    await asyncio.gather(*(one_user(i, args.rounds, timings, failures) for i in range(args.users)))
    elapsed = time.perf_counter() - started

    total = sum(len(v) for v in timings.values())
    print(
        f"\n{args.users} users x {args.rounds} rounds: {total} requests in {elapsed:.1f}s "
        f"({total / elapsed:.0f} req/s), {len(failures)} failures\n"
    )
    print(f"  {'endpoint':<36} {'n':>5} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}   ms")
    for label, values in sorted(timings.items(), key=lambda kv: -percentile(kv[1], 0.95)):
        print(
            f"  {label:<36} {len(values):>5} {percentile(values, 0.5):>8.1f} "
            f"{percentile(values, 0.95):>8.1f} {percentile(values, 0.99):>8.1f} {max(values):>8.1f}"
        )
    if failures:
        print("\nfailures:", ", ".join(sorted(set(failures))))
        if any(f.endswith(" 429") for f in failures):
            print(
                "  429s are the per-IP rate limits doing their job against one machine\n"
                "  playing many users. For a load run, raise them on the API:\n"
                "    NOVA_SECURITY__AUTH_RATE_LIMIT_ATTEMPTS=100000\n"
                "    NOVA_DEVICE__PROVISION_RATE_LIMIT_ATTEMPTS=100000\n"
                "    NOVA_DEVICE__CLAIM_RATE_LIMIT_ATTEMPTS=100000"
            )
    mean = statistics.fmean(v for vs in timings.values() for v in vs)
    print(
        "\nrows written are marked data.synthetic=true where the schema allows; "
        f"mean latency across everything: {mean:.1f} ms"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
