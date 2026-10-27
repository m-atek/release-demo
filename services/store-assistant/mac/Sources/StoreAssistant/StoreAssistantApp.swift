import StoreAssistantCore
import SwiftUI

@main
struct StoreAssistantApp: App {
    var body: some Scene {
        WindowGroup("Store Assistant") {
            ContentView()
                .frame(minWidth: 640, minHeight: 560)
        }
        .defaultSize(width: 760, height: 680)
    }
}

/// Loads today's orders when the window opens and every 30 seconds after.
@MainActor
final class OrdersModel: ObservableObject {
    @Published var day: OrdersDay?
    @Published var problem: String?
    @Published var updated: Date?

    func refresh() async {
        guard let url = Settings.ordersURL() else {
            problem = "No orders source set. Set OrdersURL in the nl.es.StoreAssistant preferences."
            return
        }
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                problem = "orders-service answered \((response as? HTTPURLResponse)?.statusCode ?? 0)."
                return
            }
            day = try OrdersDay.parse(data)
            problem = nil
            updated = Date()
        } catch {
            problem = "Couldn't load today's orders: \(error.localizedDescription)"
        }
    }
}

struct ContentView: View {
    @StateObject private var model = OrdersModel()
    private let version = Settings.version()

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Store Assistant")
                .font(.title2.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(version)
                .font(.system(size: 112, weight: .bold, design: .rounded))
                .monospacedDigit()
            summary
            Divider()
            orders
            Spacer(minLength: 0)
            HStack {
                if let updated = model.updated {
                    Text("Updated \(updated.formatted(date: .omitted, time: .shortened)) from orders-service")
                }
                Spacer()
                Text("Demo system, not a client's.")
            }
            .font(.footnote)
            .foregroundStyle(.secondary)
        }
        .padding(32)
        .task {
            while !Task.isCancelled {
                await model.refresh()
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }

    @ViewBuilder private var summary: some View {
        if let problem = model.problem {
            Label(problem, systemImage: "exclamationmark.triangle")
                .font(.title3)
                .foregroundStyle(.orange)
        } else if let day = model.day {
            Text("\(day.count) orders today")
                .font(.title.weight(.medium))
        } else {
            ProgressView("Loading today's orders")
        }
    }

    private var orders: some View {
        Label("Open today 08:00 to 21:00", systemImage: "clock")
            .font(.title3)
            .foregroundStyle(.secondary)
    }
}
