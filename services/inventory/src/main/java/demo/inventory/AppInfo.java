package demo.inventory;

import java.io.IOException;
import java.io.InputStream;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Properties;

/**
 * What this server runs: the version from the build, the server's name and
 * location from the release's configuration (rendered by Ansible from a
 * template), and the Java it runs on. Secrets live in a separate file that
 * the deploy writes; only whether they loaded is ever shown.
 */
public final class AppInfo {
    static final String CONFIG = "/opt/inventory/current/conf/inventory.properties";
    static final String SECRETS = "/opt/inventory/shared/secrets.properties";

    private final String version;
    private final Properties config;
    private final boolean secretsLoaded;
    private final String java;

    AppInfo(String version, Properties config, boolean secretsLoaded, String java) {
        this.version = version;
        this.config = config;
        this.secretsLoaded = secretsLoaded;
        this.java = java;
    }

    /** The running app's info: the paths can be moved with -Dinventory.config and -Dinventory.secrets. */
    public static AppInfo load() {
        var config = read(Paths.get(System.getProperty("inventory.config", CONFIG)));
        var secrets = read(Paths.get(System.getProperty("inventory.secrets", SECRETS)));
        var token = secrets.getProperty("stock.feed.token", "");
        return new AppInfo(buildVersion(), config, !token.isBlank(), System.getProperty("java.version"));
    }

    static String buildVersion() {
        var properties = new Properties();
        try (InputStream in = AppInfo.class.getResourceAsStream("/inventory-build.properties")) {
            if (in != null) {
                properties.load(in);
            }
        } catch (IOException e) {
            return "unknown";
        }
        return properties.getProperty("version", "unknown");
    }

    static Properties read(Path path) {
        var properties = new Properties();
        if (Files.isReadable(path)) {
            try (Reader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
                properties.load(reader);
            } catch (IOException e) {
                properties.clear();
            }
        }
        return properties;
    }

    public String version() {
        return version;
    }

    public String host() {
        return config.getProperty("server.name", "unknown").strip();
    }

    public String location() {
        return config.getProperty("server.location", "unknown").strip();
    }

    public String environment() {
        return config.getProperty("environment", "unknown").strip();
    }

    public boolean configLoaded() {
        return !config.isEmpty();
    }

    public boolean secretsLoaded() {
        return secretsLoaded;
    }

    public String java() {
        return java;
    }
}
