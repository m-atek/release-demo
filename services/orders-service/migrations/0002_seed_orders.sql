-- Fictional orders for the demo's six stores.
INSERT INTO orders (id, store, items, total_eur, status, delivery_days) VALUES
  ('26-1001', 'Utrecht Centraal', ARRAY['Running shoes', 'Water bottle'], 102.90, 'packed', 2),
  ('26-1002', 'Amsterdam Zuid', ARRAY['Rain jacket'], 74.50, 'picking', 2),
  ('26-1003', 'Rotterdam Blaak', ARRAY['Day backpack', 'Sports socks, 3 pairs'], 63.99, 'received', 2),
  ('26-1004', 'Eindhoven Strijp', ARRAY['Training shirt'], 24.99, 'shipped', 3),
  ('26-1005', 'Den Haag Spui', ARRAY['Running shoes'], 89.95, 'packed', 2),
  ('26-1006', 'Groningen Vismarkt', ARRAY['Rain jacket', 'Training shirt'], 99.49, 'received', 3),
  ('26-1007', 'Utrecht Centraal', ARRAY['Sports socks, 3 pairs'], 14.99, 'shipped', 2),
  ('26-1008', 'Amsterdam Zuid', ARRAY['Day backpack', 'Water bottle'], 61.95, 'picking', 2),
  ('26-1009', 'Rotterdam Blaak', ARRAY['Running shoes', 'Training shirt'], 114.94, 'packed', 2),
  ('26-1010', 'Groningen Vismarkt', ARRAY['Water bottle'], 12.95, 'received', 3)
ON CONFLICT (id) DO NOTHING;
