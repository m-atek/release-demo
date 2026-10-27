package demo.inventory;

import java.util.ArrayList;
import java.util.List;

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
        List<String> rows = new ArrayList<String>();
        for (Catalog.Item item : Catalog.ITEMS) {
            rows.add(row(item));
        }
        return Json.object("items", Json.array(rows));
    }

    /** /api/items/SKU: one item with its total stock, or null when the SKU is unknown. */
    public static String item(String sku) {
        Catalog.Item item = Catalog.item(sku);
        return item == null ? null : row(item);
    }

    private static String row(Catalog.Item item) {
        return Json.object(
                "sku", Json.string(item.sku),
                "name", Json.string(item.name),
                "stock", String.valueOf(Catalog.totalStock(item)));
    }
}
