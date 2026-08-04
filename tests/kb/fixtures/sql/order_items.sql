-- Synthetic fixture (RC-P2-8).
CREATE TABLE order_items (
    order_item_id INT PRIMARY KEY,
    order_id INT NOT NULL REFERENCES orders(order_id),
    item_id INT NOT NULL REFERENCES inventory_items(item_id)
);
GO
