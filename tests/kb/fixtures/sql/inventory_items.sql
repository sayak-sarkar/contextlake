-- Synthetic fixture (RC-P2-8). One inline FK plus one named
-- CONSTRAINT ... FOREIGN KEY ... REFERENCES FK, referencing a table
-- (suppliers) defined in a *different* file -- exercises cross-file,
-- repo-wide FK resolution.
CREATE TABLE inventory_items (
    item_id INT PRIMARY KEY,
    category_id INT NOT NULL REFERENCES inventory_categories(category_id),
    CONSTRAINT fk_item_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id)
);
GO
