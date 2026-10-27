import Foundation
import Testing
@testable import StoreAssistantCore

/// A response recorded from orders-service's /api/orders, three orders long.
private func fixture() throws -> Data {
    let url = try #require(Bundle.module.url(forResource: "orders-2026-10-27", withExtension: "json",
                                             subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
}

@Test func readsEveryOrderFromTheRecordedResponse() throws {
    let day = try OrdersDay.parse(fixture())
    #expect(day.date == "2026-10-27")
    #expect(day.count == 3)
    #expect(day.orders.count == day.count)
    #expect(day.orders.first?.id == "261027-001")
    #expect(day.orders.first?.totalEur == 14.99)
}

@Test func skipsAnOrderItCantReadAndKeepsTheRest() throws {
    let json = #"""
    {"date": "2026-10-27", "orders": [
      {"id": "1", "store": "Utrecht Centraal", "items": ["Rain jacket"], "total_eur": 74.5, "status": "packed"},
      {"id": "2", "store": "Utrecht Centraal"}
    ]}
    """#
    let day = try OrdersDay.parse(Data(json.utf8))
    #expect(day.count == 2)
    #expect(day.orders.map(\.id) == ["1"])
}

@Test func anEmptyDayHasNoOrders() throws {
    let day = try OrdersDay.parse(Data(#"{"date": "2026-10-27", "orders": []}"#.utf8))
    #expect(day.count == 0)
    #expect(day.orders.isEmpty)
}

@Test func ordersURLAddsTheAPIPath() {
    let defaults = UserDefaults(suiteName: "StoreAssistantTests")!
    defaults.set("https://orders.example.invalid/", forKey: "OrdersURL")
    #expect(Settings.ordersURL(defaults: defaults, environment: [:])?.absoluteString
        == "https://orders.example.invalid/api/orders")
    defaults.removeObject(forKey: "OrdersURL")
    #expect(Settings.ordersURL(defaults: defaults, environment: [:]) == nil)
}

@Test func eurosUseDutchNotation() {
    #expect(euros(89.95).contains("89,95"))
}
