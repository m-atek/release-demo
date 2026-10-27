package demo.inventory;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.zip.CRC32;

/**
 * Fictional items and stores. Stock comes from a checksum of the item and the
 * store, so every server answers the same and nothing random shows on screen.
 */
public final class Catalog {
    public static final class Item {
        public final String sku;
        public final String name;

        Item(String sku, String name) {
            this.sku = sku;
            this.name = name;
        }
    }

    public static final class Store {
        public final String id;
        public final String city;

        Store(String id, String city) {
            this.id = id;
            this.city = city;
        }
    }

    public static final List<Item> ITEMS = Collections.unmodifiableList(Arrays.asList(
            new Item("SKU-1001", "Water bottle, 750 ml"),
            new Item("SKU-1002", "Rain jacket"),
            new Item("SKU-1003", "Trail socks, two pairs"),
            new Item("SKU-1004", "Headlamp"),
            new Item("SKU-1005", "Thermos, 1 litre"),
            new Item("SKU-1006", "Daypack, 20 litres")));

    public static final List<Store> STORES = Collections.unmodifiableList(Arrays.asList(
            new Store("101", "Amsterdam"),
            new Store("102", "Utrecht"),
            new Store("103", "Rotterdam"),
            new Store("104", "Eindhoven"),
            new Store("105", "Groningen")));

    private Catalog() {
    }

    public static Item item(String sku) {
        for (Item item : ITEMS) {
            if (item.sku.equalsIgnoreCase(sku.trim())) {
                return item;
            }
        }
        return null;
    }

    /** Units of an item in one store: 0 to 39. */
    public static int stock(Item item, Store store) {
        CRC32 crc = new CRC32();
        crc.update((item.sku + "/" + store.id).getBytes(StandardCharsets.UTF_8));
        return (int) (crc.getValue() % 40);
    }

    public static int totalStock(Item item) {
        int total = 0;
        for (Store store : STORES) {
            total += stock(item, store);
        }
        return total;
    }
}
