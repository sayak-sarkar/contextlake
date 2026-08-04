-- Synthetic fixture (RC-P2-8): no real schema, invented for the SQL-parser
-- precision/recall corpus. See tests/kb/fixtures/sql/expected_edges.json.
CREATE TABLE regions (
    region_id INT PRIMARY KEY,
    region_name NVARCHAR(100) NOT NULL
);
GO
