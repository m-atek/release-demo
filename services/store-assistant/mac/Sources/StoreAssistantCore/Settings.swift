import Foundation

/// Where the app reads its settings. Managed Macs get the orders URL from a
/// preference the device management sets (domain nl.es.StoreAssistant, key
/// OrdersURL); scripts/demos/desktop/pilot_setup.sh writes it on the pilot Mac.
/// The repository holds no instance URLs, so there is no default.
public enum Settings {
    public static let domain = "nl.es.StoreAssistant"

    public static func ordersURL(
        defaults: UserDefaults = .standard,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> URL? {
        let value = environment["STORE_ASSISTANT_ORDERS_URL"] ?? defaults.string(forKey: "OrdersURL")
        guard let value, var components = URLComponents(string: value), components.scheme != nil else {
            return nil
        }
        if !components.path.hasSuffix("/api/orders") {
            components.path = components.path.replacingOccurrences(of: "/$", with: "", options: .regularExpression)
                + "/api/orders"
        }
        return components.url
    }

    /// The version the app bundle says it is, or "dev" outside a bundle.
    public static func version(bundle: Bundle = .main) -> String {
        bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
    }
}
