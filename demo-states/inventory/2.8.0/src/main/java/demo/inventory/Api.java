package demo.inventory;

import java.util.ArrayList;

/** The API's answers as JSON, apart from the servlets so the tests need no container. */
public final class Api {
    private Api() {
    }

    /** /health: what the release pipeline and the server panes read. */
    public static String health(AppInfo info) {
        return Json.object(
                "status", Json.string("ok"),
                "service", Json.string("inventory"),
                "version", Json.string(info.version()),
                "host", Json.string(info.host()),
                "location", Json.string(info.location()),
                "environment", Json.string(info.environment()),
                "java", Json.string(info.java()),
                "config", Json.string(info.configLoaded() ? "loaded" : "missing"),
                "secrets", Json.string(info.secretsLoaded() ? "loaded" : "missing"));
    }

    /** /api/items: every item with its stock over all stores. */
    public static String items() {
        var rows = new ArrayList<String>();
        for (var item : Catalog.ITEMS) {
            rows.add(Json.object(
                    "sku", Json.string(item.sku),
                    "name", Json.string(item.name),
                    "stock", String.valueOf(Catalog.totalStock(item))));
        }
        return Json.object("items", Json.array(rows));
    }

    /** /api/items/SKU: one item with its stock per store (ORD-44), or null when the SKU is unknown. */
    public static String item(String sku) {
        var item = Catalog.item(sku);
        if (item == null) {
            return null;
        }
        var stores = new ArrayList<String>();
        for (var row : Catalog.stockPerStore(item)) {
            var store = Catalog.STORES.get(row[0]);
            stores.add(Json.object(
                    "store", Json.string(store.id),
                    "city", Json.string(store.city),
                    "stock", String.valueOf(row[1])));
        }
        return Json.object(
                "sku", Json.string(item.sku),
                "name", Json.string(item.name),
                "stock", String.valueOf(Catalog.totalStock(item)),
                "stores", Json.array(stores));
    }
}
