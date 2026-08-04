-- Synthetic fixture (RC-P2-8). Both FKs are attached via a separate
-- ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY statement, the common
-- "constraints added after the fact" DDL style. sql.py's scope tracker only
-- ever scans REFERENCES inside a CREATE TABLE's own scope (ALTER TABLE is one
-- of the boundaries that *ends* a scope, never one that opens its own), so
-- neither FK below is captured -- a second, distinct, documented recall gap
-- from the self-referencing-FK one (see customers.sql / inventory_categories.sql).
CREATE TABLE shipments (
    shipment_id INT PRIMARY KEY,
    shipped_at DATETIME
);
GO

ALTER TABLE shipments ADD CONSTRAINT fk_shipment_order FOREIGN KEY (order_id) REFERENCES orders(order_id);
GO

ALTER TABLE shipments ADD CONSTRAINT fk_shipment_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses(warehouse_id);
GO
