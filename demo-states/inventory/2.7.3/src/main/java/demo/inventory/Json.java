package demo.inventory;

import java.util.List;

/** Just enough JSON for this API, without a library: strings, numbers and booleans. */
public final class Json {
    private Json() {
    }

    public static String string(String value) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            switch (c) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
            }
        }
        return out.append('"').toString();
    }

    /** An object from alternating keys and values. Values are already JSON. */
    public static String object(String... pairs) {
        StringBuilder out = new StringBuilder("{");
        for (int i = 0; i + 1 < pairs.length; i += 2) {
            if (i > 0) {
                out.append(',');
            }
            out.append(string(pairs[i])).append(':').append(pairs[i + 1]);
        }
        return out.append('}').toString();
    }

    public static String array(List<String> values) {
        return "[" + String.join(",", values) + "]";
    }
}
