package demo.inventory;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.Properties;
import org.junit.Test;

public class ApiTest {
    private static AppInfo info(String version) {
        var config = new Properties();
        config.setProperty("server.name", "inv-cloud-01");
        config.setProperty("server.location", "AWS eu-west-1");
        config.setProperty("environment", "production");
        return new AppInfo(version, config, true, "11.0.28");
    }

    @Test
    public void healthReportsTheVersionAndServer() {
        var health = Api.health(info("2.8.0"));
        assertTrue(health, health.contains("\"version\":\"2.8.0\""));
        assertTrue(health, health.contains("\"host\":\"inv-cloud-01\""));
        assertTrue(health, health.contains("\"status\":\"ok\""));
        assertTrue(health, health.contains("\"java\":\"11.0.28\""));
    }

    @Test
    public void healthNeverShowsASecret() {
        var health = Api.health(info("2.8.0"));
        assertTrue(health, health.contains("\"secrets\":\"loaded\""));
        assertTrue(health, !health.contains("token"));
    }

    @Test
    public void itemsListsEveryItemWithItsTotal() {
        var items = Api.items();
        for (var item : Catalog.ITEMS) {
            assertTrue(items, items.contains("\"sku\":\"" + item.sku + "\",\"name\":" + Json.string(item.name)
                    + ",\"stock\":" + Catalog.totalStock(item)));
        }
    }

    @Test
    public void oneItemShowsStockPerStore() {
        var body = Api.item("sku-1004");
        for (var store : Catalog.STORES) {
            assertTrue(body, body.contains("\"store\":\"" + store.id + "\",\"city\":\"" + store.city + "\""));
        }
    }

    @Test
    public void anUnknownItemIsNull() {
        assertNull(Api.item("SKU-9999"));
    }

    @Test
    public void storesAddUpToTheTotal() {
        for (var item : Catalog.ITEMS) {
            var sum = 0;
            for (var row : Catalog.stockPerStore(item)) {
                sum += row[1];
            }
            assertEquals(Catalog.totalStock(item), sum);
        }
    }

    @Test
    public void storesWithTheMostStockComeFirst() {
        var rows = Catalog.stockPerStore(Catalog.ITEMS.get(0));
        for (var i = 1; i < rows.size(); i++) {
            assertTrue(rows.get(i - 1)[1] >= rows.get(i)[1]);
        }
    }

    @Test
    public void stockIsTheSameOnEveryServer() {
        var item = Catalog.item("SKU-1001");
        assertEquals(Catalog.stock(item, Catalog.STORES.get(0)), Catalog.stock(item, Catalog.STORES.get(0)));
        assertTrue(Catalog.stock(item, Catalog.STORES.get(0)) < 40);
    }
}
