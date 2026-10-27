package demo.inventory;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.Properties;
import org.junit.Test;

public class ApiTest {
    private static AppInfo info(String version) {
        Properties config = new Properties();
        config.setProperty("server.name", "inv-cloud-01");
        config.setProperty("server.location", "AWS eu-west-1");
        config.setProperty("environment", "production");
        return new AppInfo(version, config, true, "1.8.0_482");
    }

    @Test
    public void healthReportsTheVersionAndServer() {
        String health = Api.health(info("2.7.3"));
        assertTrue(health, health.contains("\"version\":\"2.7.3\""));
        assertTrue(health, health.contains("\"host\":\"inv-cloud-01\""));
        assertTrue(health, health.contains("\"status\":\"ok\""));
        assertTrue(health, health.contains("\"java\":\"1.8.0_482\""));
    }

    @Test
    public void healthNeverShowsASecret() {
        String health = Api.health(info("2.7.3"));
        assertTrue(health, health.contains("\"secrets\":\"loaded\""));
        assertTrue(health, !health.contains("token"));
    }

    @Test
    public void itemsListsEveryItemWithItsTotal() {
        String items = Api.items();
        for (Catalog.Item item : Catalog.ITEMS) {
            assertTrue(items, items.contains("\"sku\":\"" + item.sku + "\",\"name\":" + Json.string(item.name)
                    + ",\"stock\":" + Catalog.totalStock(item)));
        }
    }

    @Test
    public void oneItemShowsItsTotal() {
        Catalog.Item item = Catalog.item("SKU-1004");
        assertEquals("{\"sku\":\"SKU-1004\",\"name\":\"Headlamp\",\"stock\":" + Catalog.totalStock(item) + "}",
                Api.item("sku-1004"));
    }

    @Test
    public void anUnknownItemIsNull() {
        assertNull(Api.item("SKU-9999"));
    }

    @Test
    public void stockIsTheSameOnEveryServer() {
        Catalog.Item item = Catalog.item("SKU-1001");
        assertEquals(Catalog.stock(item, Catalog.STORES.get(0)), Catalog.stock(item, Catalog.STORES.get(0)));
        assertTrue(Catalog.stock(item, Catalog.STORES.get(0)) < 40);
    }
}
