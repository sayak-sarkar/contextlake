-- Synthetic fixture (RC-P2-8). The `region_id` column below is commented-out
-- dead DDL (an old direct region FK, replaced by going through `addresses`)
-- -- exactly the kind of history line real DDL scripts accumulate. It used to
-- be captured as a real FK, a measured FALSE POSITIVE (orders -> regions);
-- the parser masks comments before matching now, so this line is the negative
-- case that keeps it that way. See expected_edges.json.
CREATE TABLE orders (
    order_id INT PRIMARY KEY,
    -- region_id INT NULL REFERENCES regions(region_id),
    customer_id INT NOT NULL REFERENCES customers(customer_id),
    CONSTRAINT fk_order_address FOREIGN KEY (ship_address_id) REFERENCES addresses(address_id)
);
GO
