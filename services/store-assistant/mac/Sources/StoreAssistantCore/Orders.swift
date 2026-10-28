import Foundation

/// One order as orders-service's /api/orders returns it.
public struct Order: Decodable, Identifiable, Equatable, Sendable {
    public let id: String
    public let store: String
    public let items: [String]
    public let totalEur: Double
    public let status: String

    enum CodingKeys: String, CodingKey {
        case id, store, items, status
        case totalEur = "total_eur"
    }
}

/// Today's orders. `count` is every order the API sent; `orders` holds the
/// ones this version could read. A row it can't read is skipped, so one odd
/// order doesn't empty the whole list.
public struct OrdersDay: Decodable, Equatable, Sendable {
    public let date: String
    public let orders: [Order]
    public let count: Int

    enum CodingKeys: String, CodingKey { case date, orders }

    public init(date: String, orders: [Order], count: Int) {
        self.date = date
        self.orders = orders
        self.count = count
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        date = try container.decode(String.self, forKey: .date)
        var list = try container.nestedUnkeyedContainer(forKey: .orders)
        var readable: [Order] = []
        var seen = 0
        while !list.isAtEnd {
            seen += 1
            if let order = try? list.decode(Order.self) {
                readable.append(order)
            } else {
                // A failed decode doesn't move the container on: skip the row.
                _ = try list.decode(Skipped.self)
            }
        }
        orders = readable
        count = seen
    }

    public static func parse(_ data: Data) throws -> OrdersDay {
        try JSONDecoder().decode(OrdersDay.self, from: data)
    }
}

/// Decodes any JSON value and keeps nothing.
private struct Skipped: Decodable {
    init(from decoder: Decoder) throws {}
}

/// Formats a euro amount the way the stores read it: € 89,95.
public func euros(_ amount: Double) -> String {
    let formatter = NumberFormatter()
    formatter.numberStyle = .currency
    formatter.currencyCode = "EUR"
    formatter.locale = Locale(identifier: "nl_NL")
    return formatter.string(from: NSNumber(value: amount)) ?? String(format: "€ %.2f", amount)
}
