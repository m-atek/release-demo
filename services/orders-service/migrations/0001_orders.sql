-- The orders the page and /api/orders read. orders_owner, which runs the
-- migrations, owns them; the functions read them as orders_app.
CREATE TABLE IF NOT EXISTS orders (
  id text PRIMARY KEY,
  store text NOT NULL,
  items text[] NOT NULL,
  total_eur numeric(10, 2) NOT NULL,
  status text NOT NULL,
  delivery_days bigint NOT NULL
);

GRANT SELECT ON orders TO orders_app;
