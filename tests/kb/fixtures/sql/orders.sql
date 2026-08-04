-- Synthetic fixture (RC-P2-8). The `region_id` column below is commented-out
-- dead DDL (an old direct region FK, replaced by going through `addresses`)
-- -- exactly the kind of history line real DDL scripts accumulate. The
-- parser has no comment-awareness, so it still captures the REFERENCES
-- inside the comment as a real FK: a genuine, measured FALSE POSITIVE
-- (orders -> regions), not a hypothetical one. See expected_edges.json.
CREATE TABLE orders (
    order_id INT PRIMARY KEY,
    -- region_id INT NULL REFERENCES regions(region_id),
    customer_id INT NOT NULL REFERENCES customers(customer_id),
    CONSTRAINT fk_order_address FOREIGN KEY (ship_address_id) REFERENCES addresses(address_id)
);
GO
