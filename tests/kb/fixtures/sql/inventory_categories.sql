-- Synthetic fixture (RC-P2-8). Another self-referencing FK (category tree),
-- a common real-world shape -- and another documented recall miss.
CREATE TABLE inventory_categories (
    category_id INT PRIMARY KEY,
    parent_category_id INT NULL REFERENCES inventory_categories(category_id)
);
GO
