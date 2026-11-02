-- The catalog's table and its fictional products, as they stood in the file
-- data/catalog.json. The database seeds itself from this at its first start.

CREATE TABLE products (
  id        text PRIMARY KEY,
  name      text NOT NULL,
  supplier  text NOT NULL,
  price_eur numeric(8, 2) NOT NULL
);

INSERT INTO products (id, name, supplier, price_eur) VALUES
  ('P-1001', 'Running shoes', 'Kempen Schoenmakers', 89.95),
  ('P-1002', 'Trail running shoes', 'Kempen Schoenmakers', 119.00),
  ('P-1003', 'Walking boots', 'Kempen Schoenmakers', 139.50),
  ('P-1004', 'Rain jacket', 'Noordkaap Outdoor', 74.50),
  ('P-1005', 'Softshell jacket', 'Noordkaap Outdoor', 99.00),
  ('P-1006', 'Fleece pullover', 'Noordkaap Outdoor', 44.95),
  ('P-1007', 'Day backpack', 'Brabant Bagage', 49.00),
  ('P-1008', 'Hiking backpack, 40 litres', 'Brabant Bagage', 94.00),
  ('P-1009', 'Hip bag', 'Brabant Bagage', 19.95),
  ('P-1010', 'Water bottle', 'Wadden Waterwerk', 12.95),
  ('P-1011', 'Insulated bottle', 'Wadden Waterwerk', 24.50),
  ('P-1012', 'Hydration bladder', 'Wadden Waterwerk', 29.95),
  ('P-1013', 'Training shirt', 'Veluwe Textiel', 24.99),
  ('P-1014', 'Running tights', 'Veluwe Textiel', 39.95),
  ('P-1015', 'Sports socks, 3 pairs', 'Veluwe Textiel', 14.99),
  ('P-1016', 'Base layer shirt', 'Veluwe Textiel', 34.50),
  ('P-1017', 'Yoga mat', 'Delta Sportgoed', 29.00),
  ('P-1018', 'Resistance bands', 'Delta Sportgoed', 17.50),
  ('P-1019', 'Foam roller', 'Delta Sportgoed', 22.95),
  ('P-1020', 'Skipping rope', 'Delta Sportgoed', 9.95),
  ('P-1021', 'Cycling gloves', 'Noordkaap Outdoor', 21.00),
  ('P-1022', 'Head torch', 'Noordkaap Outdoor', 27.95),
  ('P-1023', 'Gym bag', 'Brabant Bagage', 34.95),
  ('P-1024', 'Swim goggles', 'Wadden Waterwerk', 15.50);
