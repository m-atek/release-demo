SET lock_timeout = '5s';
SET statement_timeout = '60s';

ALTER TABLE orders
  -- squawk-ignore renaming-column
  RENAME COLUMN delivery_days TO delivery_estimate_days;
