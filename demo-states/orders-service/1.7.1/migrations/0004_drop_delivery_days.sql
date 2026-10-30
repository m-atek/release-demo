SET lock_timeout = '5s';
SET statement_timeout = '60s';

DROP TRIGGER IF EXISTS orders_copy_delivery_days
  ON orders;

-- Only the trigger above called it.
-- squawk-ignore ban-drop-function
DROP FUNCTION IF EXISTS orders_copy_delivery_days();

-- 1.7.0 and later don't read it.
-- squawk-ignore ban-drop-column
ALTER TABLE orders DROP COLUMN IF EXISTS delivery_days;
