import SwiftUI

/// Every device on the account.
struct DeviceListView: View {
    @Environment(\.novaAPI) private var api
    @State private var model = DeviceListModel()
    @State private var isClaiming = false

    var body: some View {
        NavigationStack {
            Group {
                if model.isFirstLoad {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if model.devices.isEmpty {
                    NoDeviceView()
                } else {
                    list
                }
            }
            .navigationTitle("Devices")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        isClaiming = true
                    } label: {
                        Label("Add NOVA", systemImage: "plus")
                    }
                }
            }
            .refreshable { await model.load(using: api) }
            .sheet(isPresented: $isClaiming) {
                ClaimDeviceView()
            }
            .onChange(of: isClaiming) { _, presenting in
                // Refresh when the sheet closes so a newly claimed device
                // appears without a manual pull.
                if !presenting {
                    Task { await model.load(using: api) }
                }
            }
        }
        .task { await model.load(using: api) }
    }

    private var list: some View {
        List {
            ForEach(model.devices) { device in
                NavigationLink {
                    DeviceDetailView(device: device)
                } label: {
                    DeviceRow(device: device)
                }
            }

            if let error = model.error {
                Section {
                    InlineError(error: error)
                        .listRowInsets(EdgeInsets())
                        .listRowBackground(Color.clear)
                }
            }
        }
    }
}

private struct DeviceRow: View {
    let device: Device

    var body: some View {
        HStack(spacing: 14) {
            NovaMark(size: 34, isAwake: device.isOnline)

            VStack(alignment: .leading, spacing: 3) {
                Text(device.displayName)
                    .font(.body.weight(.medium))
                Text(device.model)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Circle()
                .fill(device.isOnline ? Color.green : Color.secondary.opacity(0.4))
                .frame(width: 8, height: 8)
        }
        .padding(.vertical, 4)
    }
}

/// One device: status, controls, and recent telemetry.
struct DeviceDetailView: View {
    @Environment(\.novaAPI) private var api
    @Environment(\.dismiss) private var dismiss

    let device: Device

    @State private var events: [TelemetryEvent] = []
    @State private var error: APIError?
    @State private var commandNote: String?
    @State private var isRemoving = false

    var body: some View {
        List {
            Section {
                LabeledContent("Status", value: device.isOnline ? "Online" : "Offline")
                LabeledContent("Model", value: device.model)
                if let firmware = device.firmwareVersion {
                    LabeledContent("Firmware", value: firmware)
                }
                LabeledContent("Hardware") {
                    Text(device.hardwareId)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
                if let seen = device.lastSeenAt {
                    LabeledContent("Last seen") {
                        Text(seen, format: .relative(presentation: .named))
                    }
                }
            }

            Section("Controls") {
                CommandButton(
                    "Look left", symbol: "arrow.left",
                    command: .headMove(yaw: -30, pitch: 0), device: device,
                    note: $commandNote
                )
                CommandButton(
                    "Look right", symbol: "arrow.right",
                    command: .headMove(yaw: 30, pitch: 0), device: device,
                    note: $commandNote
                )
                CommandButton(
                    "Centre", symbol: "arrow.up.and.down.and.arrow.left.and.right",
                    command: .headMove(yaw: 0, pitch: 0), device: device,
                    note: $commandNote
                )
                CommandButton(
                    "Look curious", symbol: "sparkles",
                    command: .expression(.curious), device: device,
                    note: $commandNote
                )

                if let commandNote {
                    Text(commandNote)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Recent activity") {
                if events.isEmpty {
                    Text("Nothing reported yet.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(events.prefix(20)) { event in
                        TelemetryRow(event: event)
                    }
                }
            }

            Section {
                Button("Remove NOVA", role: .destructive) { isRemoving = true }
            } footer: {
                Text("Removing revokes its credentials. You can claim it again from its screen.")
            }

            if let error {
                Section {
                    InlineError(error: error)
                        .listRowInsets(EdgeInsets())
                        .listRowBackground(Color.clear)
                }
            }
        }
        .navigationTitle(device.displayName)
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await loadTelemetry() }
        .task { await loadTelemetry() }
        .confirmationDialog(
            "Remove \(device.displayName)?",
            isPresented: $isRemoving,
            titleVisibility: .visible
        ) {
            Button("Remove", role: .destructive) {
                Task { await remove() }
            }
        } message: {
            Text("Its credentials are revoked immediately.")
        }
    }

    private func loadTelemetry() async {
        do {
            events = try await api.telemetry(
                deviceID: device.id, limit: 50, eventType: nil
            )
            error = nil
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }

    private func remove() async {
        do {
            try await api.removeDevice(id: device.id)
            dismiss()
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }
}

private struct CommandButton: View {
    let title: String
    let symbol: String
    let command: DeviceCommand
    let device: Device
    @Binding var note: String?

    @Environment(\.novaAPI) private var api
    @State private var isSending = false

    init(
        _ title: String,
        symbol: String,
        command: DeviceCommand,
        device: Device,
        note: Binding<String?>
    ) {
        self.title = title
        self.symbol = symbol
        self.command = command
        self.device = device
        self._note = note
    }

    var body: some View {
        Button {
            Task { await send() }
        } label: {
            HStack {
                Label(title, systemImage: symbol)
                Spacer()
                if isSending { ProgressView().controlSize(.small) }
            }
        }
        // The server rejects commands to a disconnected device with 503;
        // disabling here saves the round trip and explains the greying-out.
        .disabled(!device.isOnline || isSending)
    }

    private func send() async {
        isSending = true
        defer { isSending = false }

        do {
            _ = try await api.send(command, to: device.id)
            note = "Sent."
        } catch let error as APIError {
            note = error.userMessage
        } catch {
            note = "Couldn't send that."
        }
    }
}

private struct TelemetryRow: View {
    let event: TelemetryEvent

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(event.eventType.replacingOccurrences(of: "_", with: " ").capitalized)
                    .font(.subheadline)
                Spacer()
                Text(event.recordedAt, format: .relative(presentation: .numeric))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if !summary.isEmpty {
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        }
        .padding(.vertical, 2)
    }

    private var summary: String {
        var parts: [String] = []
        if let distance = event.distanceCm { parts.append("\(distance) cm") }
        if let battery = event.batteryPercent { parts.append("\(battery)%") }
        if let temperature = event.temperatureC {
            parts.append(String(format: "%.0f°C", temperature))
        }
        if let state = event.state { parts.append(state) }
        return parts.joined(separator: " · ")
    }
}
