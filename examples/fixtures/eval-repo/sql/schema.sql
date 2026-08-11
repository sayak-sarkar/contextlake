CREATE TABLE sensor_reading (
    sample_id BIGINT PRIMARY KEY,
    collector_id INT REFERENCES collector(collector_id),
    reading DOUBLE PRECISION
);

CREATE TABLE collector (
    collector_id INT PRIMARY KEY,
    site_label TEXT
);
