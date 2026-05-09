-- ============================================================
-- NSR Potential Assessment System — Database Schema
-- ============================================================

-- 1. Ледовые данные (NSIDC Sea Ice Index v4)
CREATE TABLE IF NOT EXISTS ice_extent (
    id              SERIAL PRIMARY KEY,
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    region          VARCHAR(50) NOT NULL,
    extent_mkm2     NUMERIC(8,4),
    anomaly_mkm2    NUMERIC(8,4),
    source          VARCHAR(100) DEFAULT 'NSIDC Sea Ice Index v4',
    loaded_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE (year, month, region)
);

-- 2. Грузоперевозки по СМП
CREATE TABLE IF NOT EXISTS shipping_stats (
    id              SERIAL PRIMARY KEY,
    year            INTEGER NOT NULL,
    cargo_type      VARCHAR(50),
    volume_mt       NUMERIC(10,3),
    vessel_count    INTEGER,
    route           VARCHAR(30) DEFAULT 'NSR',
    source          TEXT,
    loaded_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE (year, cargo_type, route)
);

-- 3. Расстояния и параметры маршрутов (VesselFinder + gcmap)
CREATE TABLE IF NOT EXISTS route_economics (
    id                  SERIAL PRIMARY KEY,
    year                INTEGER NOT NULL,
    route               VARCHAR(30) NOT NULL,
    origin_port         VARCHAR(50),
    dest_port           VARCHAR(50),
    distance_nm         INTEGER,
    distance_eca_nm     INTEGER,
    avg_transit_days    NUMERIC(5,1),
    source              TEXT,
    loaded_at           TIMESTAMP DEFAULT NOW()
);

-- 4. Порты СМП
-- Координаты портов хранятся в EDA-ноутбуке и используются
-- только для построения карты. Таблица в БД не требуется.


-- 4. Расчётный слой: субиндексы и NSRPotential
CREATE TABLE IF NOT EXISTS nsr_metrics (
    id              SERIAL PRIMARY KEY,
    year            INTEGER NOT NULL,
    month           INTEGER,
    ice_index       NUMERIC(6,4),
    trade_index     NUMERIC(6,4),
    route_index     NUMERIC(6,4),
    geo_risk        NUMERIC(6,4),
    nsr_potential   NUMERIC(6,4),
    w_ice           NUMERIC(4,3) DEFAULT 0.300,
    w_trade         NUMERIC(4,3) DEFAULT 0.250,
    w_route         NUMERIC(4,3) DEFAULT 0.250,
    w_geo           NUMERIC(4,3) DEFAULT 0.200,
    calculated_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE (year, month)
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_ice_year_month ON ice_extent (year, month);
CREATE INDEX IF NOT EXISTS idx_ice_region     ON ice_extent (region);
CREATE INDEX IF NOT EXISTS idx_shipping_year  ON shipping_stats (year, route);
CREATE INDEX IF NOT EXISTS idx_metrics_year   ON nsr_metrics (year);

-- Витрина для Superset
CREATE OR REPLACE VIEW v_dashboard_main AS
SELECT
    m.year,
    m.ice_index,
    m.trade_index,
    m.route_index,
    m.geo_risk,
    m.nsr_potential,
    i.extent_mkm2   AS ice_extent_mkm2,
    i.anomaly_mkm2  AS ice_anomaly,
    s.volume_mt     AS shipping_volume_mt,
    s.vessel_count
FROM nsr_metrics m
LEFT JOIN ice_extent     i ON m.year = i.year
                           AND i.month = 9
                           AND i.region = 'total_NSR'
LEFT JOIN shipping_stats s ON m.year = s.year
                           AND s.cargo_type = 'total'
                           AND s.route = 'NSR'
WHERE m.month IS NULL
ORDER BY m.year;

-- Витрина сравнения маршрутов
CREATE OR REPLACE VIEW v_route_comparison AS
SELECT
    origin_port,
    dest_port,
    MAX(CASE WHEN route != 'NSR' THEN distance_nm END) AS dist_current_nm,
    MAX(CASE WHEN route  = 'NSR' THEN distance_nm END) AS dist_nsr_nm,
    MAX(CASE WHEN route != 'NSR' THEN avg_transit_days END) AS days_current,
    MAX(CASE WHEN route  = 'NSR' THEN avg_transit_days END) AS days_nsr,
    ROUND(
        (MAX(CASE WHEN route != 'NSR' THEN distance_nm END) -
         MAX(CASE WHEN route  = 'NSR' THEN distance_nm END))::NUMERIC /
        NULLIF(MAX(CASE WHEN route != 'NSR' THEN distance_nm END), 0) * 100, 1
    ) AS saving_pct
FROM route_economics
GROUP BY origin_port, dest_port
ORDER BY saving_pct DESC NULLS LAST;
