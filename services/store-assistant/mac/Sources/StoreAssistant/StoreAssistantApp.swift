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

    @ViewBuilder private var orders: some View {
        Text("Today's orders")
            .font(.title3.weight(.semibold))
        if let day = model.day, !day.orders.isEmpty {
            List(day.orders) { order in
                HStack {
                    Text(order.id).monospacedDigit().frame(width: 110, alignment: .leading)
                    Text(order.store).frame(maxWidth: .infinity, alignment: .leading)
                    Text("\(order.items.count) items").foregroundStyle(.secondary).frame(width: 70, alignment: .trailing)
                    Text(euros(order.totalEur)).monospacedDigit().frame(width: 90, alignment: .trailing)
                    Text(order.status).foregroundStyle(.secondary).frame(width: 80, alignment: .trailing)
                }
                .font(.title3)
            }
            .listStyle(.inset(alternatesRowBackgrounds: true))
        } else if model.day != nil {
            Text("No orders to show.")
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 160)
        }
    }
}
