-- Synthetic fixture (RC-P2-8). Self-referencing FK: a customer can be referred
-- by another customer. The parser drops self-referential FKs by design
-- (sql.py excludes `target == name`), so this edge is a documented recall miss
-- -- see expected_edges.json ("detectable": false).
CREATE TABLE customers (
    customer_id INT PRIMARY KEY,
    email NVARCHAR(255) NOT NULL,
    referred_by INT NULL REFERENCES customers(customer_id)
);
GO
