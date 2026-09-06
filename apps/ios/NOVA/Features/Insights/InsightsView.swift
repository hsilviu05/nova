import Charts
import SwiftUI

/// What NOVA has noticed.
///
/// The screen is built around one rule that runs through the whole phase:
/// **nothing here claims more than the data supports.** Every statement
/// carries the sample size and the number of days behind it, the coverage is
/// shown rather than implied, and when there is too little to go on NOVA says
/// so instead of drawing a chart of three points.
struct InsightsView: View {
    @Environment(\.novaAPI) private var api
    @State private var model: InsightsModel?

    var body: some View {
        NavigationStack {
            if let model {
                InsightsContent(model: model)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task {
            // Built here rather than in an initialiser: the model needs the
            // API from the environment, unavailable until the view is in a
            // hierarchy.
            if model == nil { model = InsightsModel(api: api) }
        }
    }
}

private struct InsightsContent: View {
    @Bindable var model: InsightsModel

    var body: some View {
        content
            .navigationTitle("Insights")
            // Reloads on first appearance and whenever the window changes;
            // the previous load is cancelled rather than racing this one.
            .task(id: model.windowDays) { await model.load() }
            .refreshable { await model.load() }
    }

    @ViewBuilder
    private var content: some View {
        if model.isFirstLoad {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if !model.hasDevice {
            NoDeviceView()
        } else {
            ScrollView {
                VStack(spacing: 20) {
                    WindowPicker(selection: $model.windowDays)

                    if let error = model.error {
                        InlineError(error: error)
                    }

                    if let coverage = model.analytics?.coverage {
                        CoverageCard(coverage: coverage, windowDays: model.windowDays)
                    }

                    if let reason = model.insufficientReason {
                        StillWatchingCard(reason: reason)
                    }

                    ForEach(model.insights?.insights ?? []) { insight in
                        InsightCard(insight: insight)
                    }

                    if model.isSufficient, let analytics = model.analytics {
                        WeekHeatmap(cells: model.weekGrid, peak: analytics.peakWeekdayHourCount)
                        HourlyChart(buckets: analytics.presenceByHour, timezone: analytics.timezone)

                        if analytics.battery.count > 1 {
                            BatteryChart(points: analytics.battery)
                        }
                        if !analytics.eventTypes.isEmpty {
                            EventBreakdown(types: analytics.eventTypes)
                        }
                    }
                }
                .padding(20)
            }
            .background(Color(.systemGroupedBackground))
        }
    }
}

// MARK: - Controls

private struct WindowPicker: View {
    @Binding var selection: Int

    var body: some View {
        Picker("Window", selection: $selection) {
            ForEach(InsightsModel.windows, id: \.self) { days in
                Text(days >= 30 ? "\(days / 30)m" : "\(days / 7)w").tag(days)
            }
        }
        .pickerStyle(.segmented)
    }
}

// MARK: - Cards

/// How much the numbers below rest on. Shown, not implied: without it a chart
/// of two days looks exactly like a chart of two months.
private struct CoverageCard: View {
    let coverage: Coverage
    let windowDays: Int

    var body: some View {
        HStack(spacing: 0) {
            metric("\(coverage.distinctDays)", "days observed")
            Divider().frame(height: 34)
            metric("\(coverage.totalEvents)", "events")
            Divider().frame(height: 34)
            metric(
                coverage.isSufficient ? "Enough" : "Thin",
                "for patterns"
            )
        }
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity)
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16))
    }

    private func metric(_ value: String, _ label: String) -> some View {
        VStack(spacing: 3) {
            Text(value)
                .font(.title3.weight(.semibold))
                .monospacedDigit()
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

/// Shown when NOVA declines to draw conclusions.
///
/// This is the feature working, not an error state, and it is styled to read
/// that way — no warning icon, no orange.
private struct StillWatchingCard: View {
    let reason: String

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            NovaMark(size: 28)
            Text(reason)
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
        }
        .padding(16)
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16))
    }
}

/// One derived statement, with its evidence attached.
private struct InsightCard: View {
    let insight: Insight

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Image(systemName: insight.symbol)
                    .foregroundStyle(Color.accentColor)
                Text(insight.headline)
                    .font(.callout.weight(.semibold))
                Spacer(minLength: 0)
            }

            Text(insight.detail)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                // The confidence and the sample size sit next to the claim,
                // not behind a tap. Someone reading a statement about their
                // own life should see what it rests on.
                Text(insight.confidence.label)
                    .font(.caption2.weight(.medium))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(confidenceTint, in: .capsule)
                Text(insight.evidence)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16))
    }

    private var confidenceTint: Color {
        switch insight.confidence {
        case .high: Color.green.opacity(0.16)
        case .medium: Color.accentColor.opacity(0.14)
        case .low: Color.secondary.opacity(0.14)
        }
    }
}

// MARK: - Charts

/// The week as a grid. Denser than seven line charts and easier to read at a
/// glance, which is the only way anybody looks at this.
private struct WeekHeatmap: View {
    let cells: [WeekdayHourBucket]
    let peak: Int

    var body: some View {
        ChartCard(title: "Your week", subtitle: "Darker is busier") {
            Chart(cells) { cell in
                RectangleMark(
                    x: .value("Hour", cell.hour),
                    y: .value("Day", cell.weekdayName)
                )
                .foregroundStyle(
                    Color.accentColor.opacity(
                        cell.count == 0 ? 0.06 : 0.15 + 0.85 * Double(cell.count) / Double(peak)
                    )
                )
            }
            .chartXScale(domain: -0.5...23.5)
            .chartYScale(domain: WeekdayHourBucket.weekdayNames.reversed())
            .chartXAxis {
                AxisMarks(values: [0, 6, 12, 18]) { value in
                    AxisValueLabel {
                        if let hour = value.as(Int.self) {
                            Text(String(format: "%02d", hour))
                        }
                    }
                }
            }
            .frame(height: 170)
        }
    }
}

private struct HourlyChart: View {
    let buckets: [HourBucket]
    let timezone: String

    var body: some View {
        ChartCard(
            title: "Time of day",
            // The timezone is named, because "14:00" is meaningless without
            // knowing whose two o'clock it is.
            subtitle: "Local time · \(timezone)"
        ) {
            Chart(buckets) { bucket in
                BarMark(
                    x: .value("Hour", bucket.hour),
                    y: .value("Times noticed", bucket.count)
                )
                .foregroundStyle(Color.accentColor)
            }
            .chartXScale(domain: -0.5...23.5)
            .chartXAxis {
                AxisMarks(values: [0, 6, 12, 18]) { value in
                    AxisValueLabel {
                        if let hour = value.as(Int.self) {
                            Text(String(format: "%02d:00", hour))
                        }
                    }
                }
            }
            .frame(height: 140)
        }
    }
}

private struct BatteryChart: View {
    let points: [BatteryPoint]

    var body: some View {
        ChartCard(title: "Battery", subtitle: nil) {
            Chart(points) { point in
                LineMark(
                    x: .value("When", point.recordedAt),
                    y: .value("Charge", point.percent)
                )
                .foregroundStyle(Color.accentColor)
                .interpolationMethod(.monotone)
            }
            // Fixed to 0–100. An auto-scaled axis turns a 3% overnight drift
            // into a cliff, which is exactly the wrong impression.
            .chartYScale(domain: 0...100)
            .chartYAxis {
                AxisMarks(values: [0, 50, 100]) { value in
                    AxisGridLine()
                    AxisValueLabel {
                        if let percent = value.as(Int.self) {
                            Text("\(percent)%")
                        }
                    }
                }
            }
            .frame(height: 130)
        }
    }
}

private struct EventBreakdown: View {
    let types: [EventTypeBucket]

    var body: some View {
        ChartCard(title: "What NOVA reported", subtitle: nil) {
            VStack(spacing: 8) {
                ForEach(types.prefix(6)) { type in
                    HStack {
                        Text(type.label)
                            .font(.footnote)
                        Spacer(minLength: 12)
                        Text("\(type.count)")
                            .font(.footnote.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

/// Shared chrome, so the panels line up and read as one screen.
private struct ChartCard<Content: View>: View {
    let title: String
    let subtitle: String?
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                if let subtitle {
                    Text(subtitle)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16))
    }
}
