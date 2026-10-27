package demo.inventory;

/** The home page: the service, its version and the server, in large type. */
public final class Page {
    private Page() {
    }

    static String html(String value) {
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;");
    }

    public static String render(AppInfo info) {
        return "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                + "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                + "<title>inventory " + html(info.version()) + "</title>"
                + "<style>body{margin:0;background:#101418;color:#f3f5f7;font-family:system-ui,sans-serif;"
                + "display:flex;min-height:100vh;align-items:center;justify-content:center;text-align:center}"
                + "h1{font-size:12vw;margin:0}p{font-size:3vw;margin:.4em 0;color:#b8c2cc}"
                + "small{display:block;margin-top:2em;color:#7d8a96;font-size:1.2rem}</style></head><body><main>"
                + "<p>inventory</p><h1>" + html(info.version()) + "</h1>"
                + "<p>" + html(info.host()) + " &middot; " + html(info.location()) + "</p>"
                + "<p>Java " + html(info.java()) + "</p>"
                + "<small>Demo system, not a client's.</small></main></body></html>\n";
    }
}
