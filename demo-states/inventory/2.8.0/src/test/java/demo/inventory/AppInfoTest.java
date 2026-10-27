package demo.inventory;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Properties;
import org.junit.Test;

public class AppInfoTest {
    @Test
    public void theBuildVersionComesFromMaven() {
        assertEquals("2.8.0", AppInfo.buildVersion());
    }

    @Test
    public void readsTheReleaseConfiguration() throws IOException {
        var file = Files.createTempFile("inventory", ".properties");
        Files.write(file, "server.name = inv-dc-02 \nserver.location=Data center (demo)\n".getBytes(StandardCharsets.UTF_8));
        var info = new AppInfo("2.8.0", AppInfo.read(file), false, "11");
        assertEquals("inv-dc-02", info.host());
        assertEquals("Data center (demo)", info.location());
        assertTrue(info.configLoaded());
        assertFalse(info.secretsLoaded());
        Files.delete(file);
    }

    @Test
    public void aMissingFileIsEmpty() {
        var info = new AppInfo("2.8.0", AppInfo.read(java.nio.file.Paths.get("/nonexistent/inventory.properties")), false, "11");
        assertFalse(info.configLoaded());
        assertEquals("unknown", info.host());
    }

    @Test
    public void jsonEscapesQuotesAndControlCharacters() {
        assertEquals("\"a\\\"b\\\\c\\n\\u0001\"", Json.string("a\"b\\c\n\u0001"));
    }

    @Test
    public void thePageEscapesHtml() {
        var config = new Properties();
        config.setProperty("server.name", "<b>x</b>");
        var page = Page.render(new AppInfo("2.8.0", config, true, "11"));
        assertTrue(page.contains("&lt;b&gt;x&lt;/b&gt;"));
        assertTrue(page.contains("Demo system, not a client's."));
    }
}
