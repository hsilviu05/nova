import SwiftUI

/// What NOVA is doing right now.
struct HomeView: View {
    @Environment(\.novaAPI) private var api
    @Environment(SessionStore.self) private var session
    @State private var model = DeviceListModel()

    var body: some View {
        NavigationStack {
            Group {
                if model.isFirstLoad {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let device = model.primary {
                    content(for: device)
                } else {
                    NoDeviceView()
                }
            }
            .navigationTitle("NOVA")
            .refreshable { await model.load(using: api) }
        }
        .task { await model.load(using: api) }
    }

    private func content(for device: Device) -> some View {
        ScrollView {
            VStack(spacing: 24) {
                StatusCard(device: device, vitals: model.vitals)

                if let line = model.latestRemark {
                    RemarkCard(text: line)
                }

                VitalsGrid(vitals: model.vitals, device: device)

                if let error = model.error {
                    InlineError(error: error)
                }
            }
            .padding(20)
        }
        .background(Color(.systemGroupedBackground))
    }
}

/// The device's identity and state, the first thing you look at.
private struct StatusCard: View {
    let device: Device
    let vitals: Vitals

    var body: some View {
        VStack(spacing: 18) {
            NovaMark(size: 96, isAwake: device.isOnline)
                .padding(.top, 8)

            VStack(spacing: 6) {
                Text(device.displayName)
                    .font(.title2.weight(.semibold))

                HStack(spacing: 6) {
                    Circle()
                        .fill(device.isOnline ? Color.green : Color.secondary)
                        .frame(width: 8, height: 8)
                    Text(device.isOnline ? "Online" : "Offline")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    if let seen = device.lastSeenAt, !device.isOnline {
                        Text("· last seen \(seen, format: .relative(presentation: .named))")
                            .font(.subheadline)
                            .foregroundStyle(.tertiary)
                    }
                }
            }

            if let state = vitals.state {
                Text(state.capitalized)
                    .font(.caption.weight(.medium))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(Color.accentColor.opacity(0.14), in: .capsule)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 24)
        .background(
            Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 20)
        )
    }
}

/// Something NOVA has observed. Sparse by design — see the personality notes
/// in the README; a companion that comments constantly becomes furniture you
/// switch off.
private struct RemarkCard: View {
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            NovaMark(size: 30)
            Text(text)
                .font(.callout)
            Spacer(minLength: 0)
        }
        .padding(16)
        .background(
            Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16)
        )
    }
}

private struct VitalsGrid: View {
    let vitals: Vitals
    let device: Device

    private var columns: [GridItem] {
        [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)]
    }

    var body: some View {
        LazyVGrid(columns: columns, spacing: 12) {
            VitalTile(
                label: "Battery",
                value: vitals.battery.map { "\($0)%" } ?? "—",
                symbol: batterySymbol
            )
            VitalTile(
                label: "Temperature",
                value: vitals.temperature.map { String(format: "%.0f°C", $0) } ?? "—",
                symbol: "thermometer.medium"
            )
            VitalTile(
                label: "WiFi",
                value: vitals.signalDescription,
                symbol: "wifi"
            )
            VitalTile(
                label: "Uptime",
                value: vitals.uptimeDescription,
                symbol: "clock"
            )
        }
    }

    private var batterySymbol: String {
        switch vitals.battery {
        case let .some(level) where level > 80: "battery.100"
        case let .some(level) where level > 40: "battery.50"
        case .some: "battery.25"
        case nil: "battery.0"
        }
    }
}

private struct VitalTile: View {
    let label: String
    let value: String
    let symbol: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: symbol)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.title3.weight(.medium))
                .monospacedDigit()
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(
            Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16)
        )
    }
}

/// Shown when the account has no device yet.
struct NoDeviceView: View {
    @State private var isClaiming = false

    var body: some View {
        VStack(spacing: 20) {
            NovaMark(size: 72, isAwake: false)
            Text("No NOVA yet")
                .font(.title3.weight(.semibold))
            Text("Power on your NOVA and it will show a code on its face.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)

            Button("Add NOVA") { isClaiming = true }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemGroupedBackground))
        .sheet(isPresented: $isClaiming) { ClaimDeviceView() }
    }
}

struct InlineError: View {
    let error: APIError

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.circle")
                .foregroundStyle(.orange)
            Text(error.userMessage)
                .font(.footnote)
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(
            Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 12)
        )
    }
}
