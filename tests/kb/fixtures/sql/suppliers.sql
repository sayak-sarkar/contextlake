-- Synthetic fixture (RC-P2-8).
CREATE TABLE suppliers (
    supplier_id INT PRIMARY KEY,
    region_id INT NOT NULL REFERENCES regions(region_id)
);
GO
