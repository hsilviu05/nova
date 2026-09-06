# NOVA Firmware

ESP-IDF firmware for the XIAO ESP32-S3 Sense. **Phases 2 and 6.**

Planned module layout — deliberately not one `main.cpp`:

```
src/
├── main/          entry point and wiring
├── network/       WiFi manager, reconnection
├── protocol/      message parsing and validation
├── camera/        frame capture
├── microphone/    audio capture
├── audio/         I2S playback
├── servo/         head yaw and pitch control
├── sensors/       VL53L0X distance
├── telemetry/     event batching and dispatch
├── state/         behaviour state machine
└── storage/       NVS configuration
```

The device must keep behaving when the backend is unreachable: on disconnect
it enters `OFFLINE` and runs its local state machine. See
[ADR 004](../../docs/decisions/004-websocket-device-protocol.md) for the
protocol.
