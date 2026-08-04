-- Synthetic fixture (RC-P2-8). Two inline column-level FKs on one table --
-- both are the parser's easy case (REFERENCES inside the CREATE TABLE scope).
CREATE TABLE addresses (
    address_id INT PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(customer_id),
    region_id INT NOT NULL REFERENCES regions(region_id),
    line1 NVARCHAR(200)
);
GO
