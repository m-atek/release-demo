package demo.inventory;

import java.io.IOException;
import javax.servlet.ServletContextEvent;
import javax.servlet.ServletContextListener;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/** The servlets and the startup listener, mapped in WEB-INF/web.xml. */
public final class Servlets {
    private Servlets() {
    }

    static void send(HttpServletResponse response, int status, String type, String body) throws IOException {
        response.setStatus(status);
        response.setContentType(type);
        response.setCharacterEncoding("UTF-8");
        response.setHeader("Cache-Control", "no-store");
        response.getWriter().write(body);
    }

    /** Logs the version and the Java it runs on when Tomcat starts the app. */
    public static final class Startup implements ServletContextListener {
        @Override
        public void contextInitialized(ServletContextEvent event) {
            AppInfo info = AppInfo.load();
            event.getServletContext().log("inventory " + info.version() + " started on " + info.host()
                    + " with Java " + info.java());
        }

        @Override
        public void contextDestroyed(ServletContextEvent event) {
            event.getServletContext().log("inventory stopping");
        }
    }

    /** GET /health */
    public static final class Health extends HttpServlet {
        @Override
        protected void doGet(HttpServletRequest request, HttpServletResponse response) throws IOException {
            send(response, 200, "application/json", Api.health(AppInfo.load()) + "\n");
        }
    }

    /** GET /api/items and /api/items/SKU */
    public static final class Items extends HttpServlet {
        @Override
        protected void doGet(HttpServletRequest request, HttpServletResponse response) throws IOException {
            String sku = request.getPathInfo() == null ? "" : request.getPathInfo().replaceFirst("^/", "");
            if (sku.trim().isEmpty()) {
                send(response, 200, "application/json", Api.items() + "\n");
                return;
            }
            String body = Api.item(sku);
            if (body == null) {
                send(response, 404, "application/json", Json.object("error", Json.string("unknown sku")) + "\n");
            } else {
                send(response, 200, "application/json", body + "\n");
            }
        }
    }

    /** GET / : the service, version and server in large type. */
    public static final class Home extends HttpServlet {
        @Override
        protected void doGet(HttpServletRequest request, HttpServletResponse response) throws IOException {
            if (!"/".equals(request.getServletPath()) || request.getPathInfo() != null) {
                send(response, 404, "text/plain", "Not found\n");
                return;
            }
            AppInfo info = AppInfo.load();
            send(response, 200, "text/html", Page.render(info));
        }
    }
}
