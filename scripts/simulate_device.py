"""Simulate a NOVA device against the live API: provision, claim, connect, report."""
import asyncio, json, os, random, sys, uuid
from datetime import UTC, datetime

import httpx
import websockets

BASE = "http://127.0.0.1:8000/api/v1"
WS = "ws://127.0.0.1:8000/api/v1/devices/ws"


def show(step, detail=""):
    print(f"  {step:<46} {detail}")


async def main() -> int:
    hardware_id = f"esp32s3-{uuid.uuid4().hex[:16]}"
    async with httpx.AsyncClient(timeout=10) as http:
        print("\n=== 1. USER SIGNS UP (phone) ===")
        email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
        r = await http.post(f"{BASE}/auth/register", json={
            "email": email, "password": "correct-horse-battery-staple",
            "display_name": "Silviu"})
        assert r.status_code == 201, r.text
        headers = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
        show("registered", email)

        print("\n=== 2. DEVICE BOOTS UNPROVISIONED ===")
        r = await http.post(f"{BASE}/devices/provision", json={
            "hardware_id": hardware_id,
            "model": "ESP32-S3-Touch-AMOLED-2.06",
            "firmware_version": "0.1.0"})
        assert r.status_code == 201, r.text
        prov = r.json()
        show("hardware_id", hardware_id)
        show("code shown on the AMOLED face", f">>>  {prov['claim_code']}  <<<")
        show("provisioning token (never displayed)", prov["provisioning_token"][:16] + "...")

        print("\n=== 3. DEVICE POLLS (nobody has claimed it) ===")
        r = await http.post(f"{BASE}/devices/provision/poll",
                            json={"provisioning_token": prov["provisioning_token"]})
        show("poll status", r.json()["status"])
        assert r.json()["status"] == "pending"

        print("\n=== 4. USER TYPES THE CODE IN THE APP ===")
        # Typed the way a person actually types it: lower case, no hyphen.
        typed = prov["claim_code"].lower().replace("-", "")
        r = await http.post(f"{BASE}/devices/claim", headers=headers,
                            json={"code": typed, "name": "Nova"})
        assert r.status_code == 201, r.text
        device = r.json()
        show(f"typed {typed!r} -> claimed", device["name"])
        show("claim response leaks the device token?",
             "NO" if "device_token" not in r.text else "YES  <-- BUG")

        print("\n=== 5. DEVICE COLLECTS ITS CREDENTIAL ===")
        r = await http.post(f"{BASE}/devices/provision/poll",
                            json={"provisioning_token": prov["provisioning_token"]})
        assert r.json()["status"] == "claimed", r.text
        token = r.json()["device_token"]
        show("device_token", token[:14] + "...")

        r = await http.post(f"{BASE}/devices/provision/poll",
                            json={"provisioning_token": prov["provisioning_token"]})
        show("second collection attempt", f"HTTP {r.status_code} (must be 401)")
        assert r.status_code == 401

        print("\n=== 6. REPLAYING THE CLAIM CODE ===")
        r = await http.post(f"{BASE}/devices/claim", headers=headers,
                            json={"code": prov["claim_code"]})
        show("reusing a spent code", f"HTTP {r.status_code} (must be 404)")
        assert r.status_code == 404

        print("\n=== 7. DEVICE CONNECTS AND REPORTS ===")
        async with websockets.connect(f"{WS}?token={token}") as ws:
            await ws.send(json.dumps({
                "type": "telemetry.heartbeat", "version": 1, "id": str(uuid.uuid4()),
                "payload": {"uptime_seconds": 3600, "state": "IDLE",
                            "battery_percent": 78, "temperature_c": 31.4,
                            "wifi_rssi": -47, "firmware_version": "0.1.0"}}))
            show("heartbeat ->", json.loads(await ws.recv())["type"])

            for n in range(5):
                await ws.send(json.dumps({
                    "type": "telemetry.event", "version": 1, "id": str(uuid.uuid4()),
                    "payload": {"event_type": "person_detected",
                                "recorded_at": datetime.now(UTC).isoformat(),
                                "distance_cm": random.randint(40, 120),
                                "head_yaw": random.randint(-30, 30),
                                "state": "CURIOUS",
                                "data": {"source": "time_of_flight"}}}))
                await ws.recv()
            show("5 person_detected events ->", "acked")

            await ws.send(json.dumps({"type": "garbage"}))
            err = json.loads(await ws.recv())
            show("malformed frame ->", f'{err["type"]}/{err["code"]}')

            print("\n=== 8. PHONE COMMANDS THE DEVICE ===")
            r = await http.post(f"{BASE}/devices/{device['id']}/commands", headers=headers,
                                json={"command": "head.move",
                                      "payload": {"yaw": 20, "pitch": -5, "duration_ms": 500}})
            show("POST /commands", f"HTTP {r.status_code}")
            assert r.status_code == 202, r.text
            cmd = json.loads(await ws.recv())
            show("device received", f'{cmd["command"]} {cmd["payload"]}')
            assert cmd["id"] == r.json()["command_id"], "command id must correlate"
            show("command id correlates", "YES")

            r = await http.post(f"{BASE}/devices/{device['id']}/commands", headers=headers,
                                json={"command": "head.move", "payload": {"yaw": 999, "pitch": 0}})
            show("out-of-range yaw=999", f"HTTP {r.status_code} (must be 422)")
            assert r.status_code == 422

        print("\n=== 9. AFTER DISCONNECT ===")
        await asyncio.sleep(0.4)
        r = await http.post(f"{BASE}/devices/{device['id']}/commands", headers=headers,
                            json={"command": "device.restart", "payload": {}})
        show("command to a disconnected device",
             f'HTTP {r.status_code} {r.json()["error"]["code"]}')
        assert r.status_code == 503

        print("\n=== 10. WHAT THE PHONE SEES ===")
        d = (await http.get(f"{BASE}/devices/{device['id']}", headers=headers)).json()
        show("name / online", f'{d["name"]} / {d["is_online"]}')
        show("last_seen_at", d["last_seen_at"])

        t = (await http.get(f"{BASE}/devices/{device['id']}/telemetry",
                            headers=headers)).json()
        show("telemetry rows stored", str(len(t)))
        kinds = {}
        for row in t:
            kinds[row["event_type"]] = kinds.get(row["event_type"], 0) + 1
        show("by event_type", json.dumps(kinds))
        sample = next(r for r in t if r["event_type"] == "person_detected")
        show("sample person_detected", f'distance={sample["distance_cm"]}cm '
                                       f'yaw={sample["head_yaw"]} payload={sample["payload"]}')

        print("\n=== 11. ANOTHER USER CANNOT TOUCH IT ===")
        r = await http.post(f"{BASE}/auth/register", json={
            "email": f"intruder-{uuid.uuid4().hex[:8]}@example.com",
            "password": "correct-horse-battery-staple", "display_name": "Intruder"})
        bad = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
        for label, coro in [
            ("GET device", http.get(f"{BASE}/devices/{device['id']}", headers=bad)),
            ("GET telemetry", http.get(f"{BASE}/devices/{device['id']}/telemetry", headers=bad)),
            ("DELETE device", http.delete(f"{BASE}/devices/{device['id']}", headers=bad)),
        ]:
            r = await coro
            show(f"intruder {label}", f"HTTP {r.status_code} (must be 404)")
            assert r.status_code == 404

        print("\n=== 12. FACTORY RESET TRANSFERS OWNERSHIP ===")
        r = await http.post(f"{BASE}/devices/provision", json={
            "hardware_id": hardware_id, "model": "ESP32-S3-Touch-AMOLED-2.06",
            "firmware_version": "0.1.0"})
        show("re-provisioned same hardware", f"HTTP {r.status_code}")
        owned = (await http.get(f"{BASE}/devices", headers=headers)).json()
        show("still on original owner's account?", "NO" if owned == [] else "YES  <-- BUG")
        assert owned == []
        try:
            async with websockets.connect(f"{WS}?token={token}"):
                print("  OLD CREDENTIAL STILL WORKS  <-- BUG"); return 1
        except Exception as exc:
            show("old credential now", f"rejected ({type(exc).__name__})")

    print("\nALL CHECKS PASSED\n")
    return 0


sys.exit(asyncio.run(main()))
