package demo.inventory;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Properties;
import org.junit.Test;

public class AppInfoTest {
    @Test
    public void theBuildVersionComesFromMaven() {
        assertEquals("2.7.3", AppInfo.buildVersion());
    }

    @Test
    public void readsTheReleaseConfiguration() throws IOException {
        Path file = Files.createTempFile("inventory", ".properties");
        Files.write(file, "server.name = inv-dc-02 \nserver.location=Data center (demo)\n".getBytes(StandardCharsets.UTF_8));
        AppInfo info = new AppInfo("2.7.3", AppInfo.read(file), false, "1.8.0");
        assertEquals("inv-dc-02", info.host());
        assertEquals("Data center (demo)", info.location());
        assertTrue(info.configLoaded());
        assertFalse(info.secretsLoaded());
        Files.delete(file);
    }

    @Test
    public void aMissingFileIsEmpty() {
        AppInfo info = new AppInfo("2.7.3", AppInfo.read(Paths.get("/nonexistent/inventory.properties")), false, "1.8.0");
        assertFalse(info.configLoaded());
        assertEquals("unknown", info.host());
    }

    @Test
    public void jsonEscapesQuotesAndControlCharacters() {
        assertEquals("\"a\\\"b\\\\c\\n\\u0001\"", Json.string("a\"b\\c\n\u0001"));
    }

    @Test
    public void thePageEscapesHtml() {
        Properties config = new Properties();
        config.setProperty("server.name", "<b>x</b>");
        String page = Page.render(new AppInfo("2.7.3", config, true, "1.8.0"));
        assertTrue(page.contains("&lt;b&gt;x&lt;/b&gt;"));
        assertTrue(page.contains("Demo system, not a client's."));
    }
}
