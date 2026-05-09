"""
compute_metrics.py
==================
Рассчитывает индекс NSRPotential и субиндексы за 2014–2024.

Формула:
    NSRPotential = 0.30 × IceIndex
                 + 0.25 × TradeIndex
                 + 0.25 × RouteIndex
                 + 0.20 × GeoRisk

Субиндексы:
    IceIndex   — доступность маршрута по льдовой обстановке
                 Источник: NSIDC Sea Ice Index v4
                 Метод: min-max нормировка, инверсия (меньше льда = выше индекс)

    TradeIndex — коммерческое освоение маршрута
                 Источник: Росатом / Администрация СМП, 2014–2024
                 Метод: z-score + сигмоида (устраняет доминирование субиндекса)

    RouteIndex — физическая выгода маршрута по расстоянию
                 Источник: VesselFinder + GCMap (верифицированные расстояния)
                 Метод: среднее saving_pct по выборке маршрутов, сигмоида
                 Примечание: константа по годам — физическая география не меняется

    GeoRisk    — геополитический риск для пользователей СМП
                 Источник: EU Council, число санкционных пакетов ЕС против России
                 URL: consilium.europa.eu/en/policies/sanctions-against-russia/
                 Метод: инвертированная min-max (больше пакетов = меньше потенциал)

Запуск:
    export DB_URL="postgresql://nsr_user:nsr_pass@localhost:5433/nsr_db"
    python scripts/compute_metrics.py
"""

import os, logging
import pandas as pd
import numpy as np
from scipy.special import expit
from scipy.stats import zscore
from sklearn.preprocessing import MinMaxScaler
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.getenv("DB_URL", "postgresql://nsr_user:nsr_pass@localhost:5433/nsr_db")

YEARS      = list(range(2014, 2025))
W_ICE      = 0.30
W_TRADE    = 0.25
W_ROUTE    = 0.25
W_GEO      = 0.20

# Санкционные пакеты ЕС по годам
# Источник: EU Council — consilium.europa.eu/en/policies/sanctions-against-russia/
GEO_SANCTIONS = {
    2014: 1, 2015: 1, 2016: 1, 2017: 1, 2018: 1,
    2019: 1, 2020: 1, 2021: 1,
    2022: 9, 2023: 11, 2024: 14,
}


# ── Субиндексы ────────────────────────────────────────────────────────────────

def min_max_norm(s: pd.Series) -> pd.Series:
    mn, mx = s.min(), s.max()
    return (s - mn) / (mx - mn) if mx != mn else pd.Series(0.5, index=s.index)


def compute_ice_index(engine) -> pd.DataFrame:
    """
    IceIndex = 1 − MinMax(среднегодовая площадь льда).
    Чем меньше льда → тем выше индекс → тем доступнее СМП.
    """
    df = pd.read_sql("""
        SELECT year, AVG(extent_mkm2) AS extent
        FROM ice_extent
        WHERE region = 'total_NSR'
          AND year BETWEEN 2014 AND 2024
        GROUP BY year
        ORDER BY year
    """, engine)
    if df.empty:
        log.warning("Нет ледовых данных — IceIndex = 0.5")
        return pd.DataFrame({'year': YEARS, 'ice_index': 0.5})
    df['ice_index'] = 1.0 - min_max_norm(df['extent'])
    return df[['year', 'ice_index']]


def compute_trade_index(engine) -> pd.DataFrame:
    """
    TradeIndex = sigmoid(z-score(volume_mt)).
    Z-score + сигмоида даёт S-образную нормировку, устойчивую к выбросам
    и исключающую доминирование одного субиндекса над остальными.
    """
    df = pd.read_sql("""
        SELECT year, volume_mt
        FROM shipping_stats
        WHERE cargo_type = 'total' AND route = 'NSR'
          AND year BETWEEN 2014 AND 2024
        ORDER BY year
    """, engine)
    if df.empty:
        log.warning("Нет данных о перевозках — TradeIndex = 0.5")
        return pd.DataFrame({'year': YEARS, 'trade_index': 0.5})
    df['trade_index'] = expit(zscore(df['volume_mt'].astype(float)))
    return df[['year', 'trade_index']]


def compute_route_index(engine) -> pd.DataFrame:
    """
    RouteIndex = sigmoid((mean_saving_pct − 25) / 15).
    Рассчитывается как среднее saving_pct по выборке верифицированных
    маршрутов (VesselFinder + GCMap). Константа по годам — физическая
    география маршрутов не изменяется.

    Критерии отбора маршрутов:
    1. Хотя бы один порт севернее 35°N
    2. Входит в топ мировых торговых коридоров (UNCTAD 2023)
    3. Действующий маршрут через Суэц/Панаму или >5000 нм
    """
    df = pd.read_sql("""
        SELECT origin_port, dest_port, route, distance_nm
        FROM route_economics
    """, engine)
    if df.empty:
        log.warning("Нет данных о маршрутах — RouteIndex = 0.5")
        return pd.DataFrame({'year': YEARS, 'route_index': 0.5})

    smp  = df[df['route'] == 'NSR'].rename(columns={'distance_nm': 'dist_smp'})
    base = df[df['route'] != 'NSR'].rename(columns={'distance_nm': 'dist_base'})
    merged = smp.merge(base, on=['origin_port', 'dest_port'])
    merged['saving_pct'] = (
        (merged['dist_base'] - merged['dist_smp']) / merged['dist_base'] * 100
    )

    mean_saving = merged['saving_pct'].mean()
    log.info(f"RouteIndex: среднее saving_pct = {mean_saving:.1f}% по {len(merged)} маршрутам")
    log.info(f"\n{merged[['origin_port','dest_port','saving_pct']].to_string()}")

    route_index = float(expit((mean_saving - 25) / 15))
    return pd.DataFrame({'year': YEARS, 'route_index': route_index})


def compute_geo_risk() -> pd.DataFrame:
    """
    GeoRisk = 1 − MinMax(число санкционных пакетов ЕС).
    Инверсия: больше пакетов → выше риск → ниже потенциал.
    Источник: EU Council, Timeline of restrictive measures against Russia.
    """
    geo = pd.DataFrame({
        'year':     list(GEO_SANCTIONS.keys()),
        'packages': list(GEO_SANCTIONS.values()),
    })
    scaler = MinMaxScaler()
    geo['geo_risk'] = 1.0 - scaler.fit_transform(geo[['packages']])
    return geo[['year', 'geo_risk']]


# ── Агрегация и запись ────────────────────────────────────────────────────────

def calculate_and_store(engine) -> pd.DataFrame:
    ice_df   = compute_ice_index(engine)
    trade_df = compute_trade_index(engine)
    route_df = compute_route_index(engine)
    geo_df   = compute_geo_risk()

    result = pd.DataFrame({'year': YEARS})
    for df in [ice_df, trade_df, route_df, geo_df]:
        if not df.empty and 'year' in df.columns:
            result = result.merge(df, on='year', how='left')

    for col in ['ice_index', 'trade_index', 'route_index', 'geo_risk']:
        if col not in result.columns:
            result[col] = 0.5
        result[col] = result[col].fillna(result[col].median())

    result['nsr_potential'] = (
        W_ICE   * result['ice_index']   +
        W_TRADE * result['trade_index'] +
        W_ROUTE * result['route_index'] +
        W_GEO   * result['geo_risk']
    ).clip(0, 1)

    log.info(f"\n{result[['year','ice_index','trade_index','route_index','geo_risk','nsr_potential']].to_string()}")

    # Миграции — добавляем новые колонки в существующую БД
    for stmt in [
        "ALTER TABLE nsr_metrics ADD COLUMN IF NOT EXISTS geo_risk NUMERIC(6,4)",
        "ALTER TABLE nsr_metrics ADD COLUMN IF NOT EXISTS w_geo    NUMERIC(4,3) DEFAULT 0.200",
        "ALTER TABLE nsr_metrics ADD COLUMN IF NOT EXISTS w_ice    NUMERIC(4,3) DEFAULT 0.300",
        "ALTER TABLE nsr_metrics ADD COLUMN IF NOT EXISTS w_trade  NUMERIC(4,3) DEFAULT 0.250",
        "ALTER TABLE nsr_metrics ADD COLUMN IF NOT EXISTS w_route  NUMERIC(4,3) DEFAULT 0.250",
    ]:
        with engine.begin() as conn:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass

    with engine.begin() as conn:
        for _, row in result.iterrows():
            conn.execute(text("""
                INSERT INTO nsr_metrics
                    (year, month, ice_index, trade_index, route_index,
                     geo_risk, nsr_potential)
                VALUES
                    (:year, NULL, :ice, :trade, :route, :geo, :nsr)
                ON CONFLICT (year, month) DO UPDATE SET
                    ice_index     = EXCLUDED.ice_index,
                    trade_index   = EXCLUDED.trade_index,
                    route_index   = EXCLUDED.route_index,
                    geo_risk      = EXCLUDED.geo_risk,
                    nsr_potential = EXCLUDED.nsr_potential,
                    calculated_at = NOW()
            """), {
                'year':  int(row.year),
                'ice':   float(row.ice_index),
                'trade': float(row.trade_index),
                'route': float(row.route_index),
                'geo':   float(row.geo_risk),
                'nsr':   float(row.nsr_potential),
            })

    log.info("✅ NSRPotential записан в nsr_metrics")
    return result


def main():
    engine = create_engine(DB_URL)
    calculate_and_store(engine)


if __name__ == "__main__":
    main()
