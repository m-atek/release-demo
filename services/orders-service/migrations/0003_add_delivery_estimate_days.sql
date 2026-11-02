SET lock_timeout = '5s';
SET statement_timeout = '60s';

ALTER TABLE orders
  ADD COLUMN IF NOT EXISTS
  delivery_estimate_days bigint;

-- A row written with delivery_days alone gets both.
CREATE OR REPLACE FUNCTION orders_copy_delivery_days()
  RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.delivery_estimate_days := NEW.delivery_days;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER orders_copy_delivery_days
  BEFORE INSERT OR UPDATE ON orders
  FOR EACH ROW
  EXECUTE FUNCTION orders_copy_delivery_days();

UPDATE orders SET delivery_estimate_days = delivery_days
  WHERE delivery_estimate_days IS NULL;
