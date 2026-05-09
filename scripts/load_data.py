"""
load_data.py
============
Загружает все исходные данные в PostgreSQL:
  1. Ледовые данные — NSIDC Sea Ice Index v4 (HTTP)
  2. Грузоперевозки — верифицированные данные из открытых источников
  3. Маршруты — расстояния из VesselFinder и gcmap

Запуск:
    export DB_URL="postgresql://nsr_user:nsr_pass@localhost:5433/nsr_db"
    python scripts/load_data.py
"""

import io, os, time, logging, requests
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_URL  = os.getenv("DB_URL", "postgresql://nsr_user:nsr_pass@localhost:5433/nsr_db")
RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
PRO_DIR = Path(__file__).parent.parent / "data" / "processed"
RAW_DIR.mkdir(parents=True, exist_ok=True)
PRO_DIR.mkdir(parents=True, exist_ok=True)

# ── 1. Схема БД ───────────────────────────────────────────────────────────────

def apply_schema(engine):
    sql_path = Path(__file__).parent.parent / "sql" / "init.sql"
    sql = sql_path.read_text()
    with engine.begin() as conn:
        for stmt in sql.split(';'):
            stmt = stmt.strip()
            if stmt:
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    log.debug(f"skip: {str(e)[:80]}")
    # Миграции для существующих БД
    migrations = [
        # source VARCHAR → TEXT
        ("shipping_stats",   "ALTER TABLE shipping_stats ALTER COLUMN source TYPE TEXT"),
        ("route_economics",  "ALTER TABLE route_economics ALTER COLUMN source TYPE TEXT"),
        ("ice_extent",       "ALTER TABLE ice_extent ALTER COLUMN source TYPE TEXT"),
        # новые колонки
        ("route_economics",  "ALTER TABLE route_economics ADD COLUMN IF NOT EXISTS distance_eca_nm INTEGER"),
        ("route_economics",  "ALTER TABLE route_economics ALTER COLUMN origin_port TYPE VARCHAR(100)"),
        ("route_economics",  "ALTER TABLE route_economics ALTER COLUMN dest_port TYPE VARCHAR(100)"),
    ]
    with engine.begin() as conn:
        for label, stmt in migrations:
            try:
                conn.execute(text(stmt))
                log.info(f"  migration ok: {label}")
            except Exception:
                pass
    log.info("Схема готова")

# ── 2. Ледовые данные NSIDC Sea Ice Index v4 ─────────────────────────────────
# Источник: National Snow and Ice Data Center (NSIDC)
# URL: https://noaadata.apps.nsidc.org/NOAA/G02135/north/monthly/data/
# Формат: N_MM_extent_v4.0.csv, один файл на месяц (01–12)
# Колонки: year, mo, source_dataset, region, extent (млн км²), area

NSIDC_BASE = "https://noaadata.apps.nsidc.org/NOAA/G02135/north/monthly/data"

def fetch_text(url: str) -> str | None:
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            log.warning(f"  попытка {attempt+1}/3 — {e}")
            time.sleep(3)
    return None

def load_ice(engine):
    records = []
    for month in range(1, 13):
        url = f"{NSIDC_BASE}/N_{month:02d}_extent_v4.0.csv"
        log.info(f"  NSIDC месяц {month:02d}…")
        raw = fetch_text(url)
        if not raw:
            log.warning(f"  пропускаю месяц {month}")
            continue
        lines = [l for l in raw.splitlines() if l.strip() and not l.startswith('#')]
        if not lines:
            continue
        try:
            df = pd.read_csv(io.StringIO('\n'.join(lines)),
                             skipinitialspace=True, on_bad_lines='skip')
            df.columns = [c.strip().lower().replace('-', '_').replace(' ', '_')
                          for c in df.columns]
            year_col   = next((c for c in df.columns if 'year'   in c), None)
            extent_col = next((c for c in df.columns if 'extent' in c), None)
            if not year_col or not extent_col:
                continue
            for _, row in df.iterrows():
                try:
                    year = int(float(str(row[year_col]).strip()))
                    val  = float(str(row[extent_col]).strip())
                    if not (0 < val < 25):
                        continue
                except Exception:
                    continue
                records.append({'year': year, 'month': month,
                                'region': 'total_NSR',
                                'extent_mkm2': val,
                                'source': 'NSIDC Sea Ice Index v4'})
        except Exception as e:
            log.warning(f"  ошибка парсинга: {e}")

    if not records:
        log.error("Ледовые данные не загружены")
        return

    df_all = pd.DataFrame(records)

    # Аномалии относительно базового периода 1981–2010
    base = df_all[(df_all['year'] >= 1981) & (df_all['year'] <= 2010)]
    mean_by_month = base.groupby('month')['extent_mkm2'].mean()
    df_all['anomaly_mkm2'] = df_all.apply(
        lambda r: r['extent_mkm2'] - mean_by_month.get(r['month'], np.nan)
        if pd.notna(r['extent_mkm2']) else np.nan, axis=1)

    df_all.to_csv(PRO_DIR / 'ice_data.csv', index=False)

    with engine.begin() as conn:
        for _, row in df_all.iterrows():
            conn.execute(text("""
                INSERT INTO ice_extent (year, month, region, extent_mkm2, anomaly_mkm2, source)
                VALUES (:year, :month, :region, :extent, :anomaly, :source)
                ON CONFLICT (year, month, region) DO UPDATE SET
                    extent_mkm2  = EXCLUDED.extent_mkm2,
                    anomaly_mkm2 = EXCLUDED.anomaly_mkm2,
                    loaded_at    = NOW()
            """), {'year': int(row.year), 'month': int(row.month),
                   'region': row.region,
                   'extent': None if pd.isna(row.extent_mkm2) else float(row.extent_mkm2),
                   'anomaly': None if pd.isna(row.anomaly_mkm2) else float(row.anomaly_mkm2),
                   'source': row.source})

    log.info(f"Ледовые данные: {len(df_all)} записей")

# ── 3. Грузоперевозки по СМП ─────────────────────────────────────────────────
# Источники (верифицированы, см. README.md):
#   2014–2016: ФГКУ «Администрация СМП» / ФГУП «Атомфлот»
#              Коммерсант, 2017. kommersant.ru/doc/3254502
#   2017–2018: Годовой отчёт ГК «Росатом» 2018
#              report.rosatom.ru/go/rosatom/go_rosatom_2018/go_2018.pdf
#   2019:      Пресс-релиз Росатома, январь 2020
#              rosatom.ru/journalist/news/gruzooborot-severnogo-morskogo-puti-v-2019-godu-sostavil-rekordnye-31-5-mln-tonn/
#   2020:      arctic.gov.ru, февраль 2021
#   2021:      PortNews, январь 2022. portnews.ru/news/323752/
#   2022:      PortNews, январь 2023. portnews.ru/news/341357/
#   2023:      Росатом / Атом Медиа, январь 2024
#              atommedia.online/press-releases/istoricheskij-rekord-sevmorputi-obe/
#   2024:      Росатомфлот, январь 2025
#              rosatomflot.ru/press-centr/novosti-predpriyatiya/2025/01/09/11644/

SHIPPING_DATA = [
    (2014,  3.930, "ФГКУ Администрация СМП / Атомфлот, Коммерсант 2017, kommersant.ru/doc/3254502"),
    (2015,  3.982, "ФГКУ Администрация СМП / Атомфлот, Коммерсант 2017, kommersant.ru/doc/3254502"),
    (2016,  5.392, "ФГКУ Администрация СМП / Атомфлот, Коммерсант 2017, kommersant.ru/doc/3254502"),
    (2017,  7.200, "Годовой отчёт Росатома 2018, стр. раздел СМП, report.rosatom.ru/go/rosatom/go_rosatom_2018/go_2018.pdf"),
    (2018, 12.700, "Годовой отчёт Росатома 2018, стр. раздел СМП, report.rosatom.ru/go/rosatom/go_rosatom_2018/go_2018.pdf"),
    (2019, 31.530, "Пресс-релиз Росатома, январь 2020, rosatom.ru/journalist/news/gruzooborot-severnogo-morskogo-puti-v-2019-godu-sostavil-rekordnye-31-5-mln-tonn/"),
    (2020, 32.010, "Росатом, arctic.gov.ru, февраль 2021"),
    (2021, 34.850, "PortNews, январь 2022, portnews.ru/news/323752/"),
    (2022, 34.340, "PortNews, январь 2023, portnews.ru/news/341357/"),
    (2023, 36.254, "Росатом / Атом Медиа, январь 2024, atommedia.online/press-releases/istoricheskij-rekord-sevmorputi-obe/"),
    (2024, 37.920, "Росатомфлот, январь 2025, rosatomflot.ru/press-centr/novosti-predpriyatiya/2025/01/09/11644/"),
]

def load_shipping(engine):
    with engine.begin() as conn:
        for year, volume, source in SHIPPING_DATA:
            conn.execute(text("""
                INSERT INTO shipping_stats (year, cargo_type, volume_mt, route, source)
                VALUES (:year, 'total', :volume, 'NSR', :source)
                ON CONFLICT (year, cargo_type, route) DO UPDATE SET
                    volume_mt = EXCLUDED.volume_mt,
                    source    = EXCLUDED.source
            """), {'year': year, 'volume': volume, 'source': source})
    log.info(f"✅ Грузоперевозки: {len(SHIPPING_DATA)} записей")

# ── 4. Маршруты (расстояния) ──────────────────────────────────────────────────
# Источники:
#   Действующие маршруты: VesselFinder Route Calculator
#     https://route.vesselfinder.com/
#   Маршруты через СМП: Great Circle Mapper
#     https://www.gcmap.com/ с waypoints через Карские Ворота → Диксон →
#     Тикси → Певек → Берингов пролив
#
# Критерии отбора маршрутов (см. README.md):
#   1. Хотя бы один порт севернее 35°N (географическая релевантность СМП)
#   2. Маршрут входит в топ мировых торговых коридоров (UNCTAD 2023)
#   3. Действующий маршрут проходит через Суэц/Панаму или >5000 нм
#   Маршруты южного полушария исключены как геометрически нерелевантные.
#   Лос-Анджелес → Лондон включён намеренно как отрицательный пример.

ROUTES = [
    # (origin, dest, route_type, distance_nm, distance_eca_nm, avg_transit_days)
    # --- Зона 1: экономия >25% ---
    ("Rotterdam",        "Singapore",   "NSR",      10000, 778,  29.8),
    ("Rotterdam",        "Singapore",   "Suez",     20915, 2742, 62.2),
    ("Rotterdam",        "Shanghai",    "NSR",       7935, 674,  23.6),
    ("Rotterdam",        "Shanghai",    "Suez",     20215, 5011, 60.2),
    ("Hamburg",          "Yokohama",    "NSR",       7113, 575,  21.2),
    ("Hamburg",          "Yokohama",    "Suez",     12106, 2604, 36.0),
    ("Saint Petersburg", "Vladivostok", "NSR",       7816, 1333, 23.3),
    ("Saint Petersburg", "Vladivostok", "Suez",     13274, 3675, 39.5),
    ("Oslo",             "Busan",       "NSR",       7344, 465,  21.9),
    ("Oslo",             "Busan",       "Suez",     11935, 2839, 35.5),
    ("London",           "Shenzhen",    "NSR",       8766, 1071, 26.1),
    ("London",           "Shenzhen",    "Suez",     13055, 432,  38.9),
    ("Dongguan",         "Bordeaux",    "NSR",       9374, 182,  27.9),
    ("Dongguan",         "Bordeaux",    "Suez",     12724, 97,   37.9),
    ("Sydney",           "Murmansk",    "NSR",       9408, 0,    28.0),
    ("Sydney",           "Murmansk",    "Direct",   14280, 148,  42.5),
    # --- Зона 2: экономия 10–25% ---
    ("Guangao",          "Lisboa",      "NSR",       9488, 43,   28.2),
    ("Guangao",          "Lisboa",      "Suez",     12144, 32,   36.1),
    ("Wellington",       "Stockholm",   "NSR",      11496, 1028, 34.2),
    ("Wellington",       "Stockholm",   "Direct",   14693, 1438, 43.7),
    # --- Зона 2 (слабая): экономия 0–10% ---
    ("Ohshima",          "Monaco",      "NSR",       9207, 811,  27.4),
    ("Ohshima",          "Monaco",      "Suez",     10002, 1520, 29.8),
    # --- Зона 3: СМП длиннее (отрицательный пример) ---
    ("Los Angeles",      "London",      "NSR",       8497, 1336, 25.3),
    ("Los Angeles",      "London",      "Atlantic",  7749, 442,  23.1),
]

def load_routes(engine):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM route_economics"))
        for origin, dest, route, dist, eca, days in ROUTES:
            conn.execute(text("""
                INSERT INTO route_economics
                    (year, route, origin_port, dest_port,
                     distance_nm, distance_eca_nm, avg_transit_days, source)
                VALUES
                    (2024, :route, :origin, :dest,
                     :dist, :eca, :days,
                     'VesselFinder Route Calculator + GCMap, 2024')
            """), {'route': route, 'origin': origin, 'dest': dest,
                   'dist': dist, 'eca': eca, 'days': days})
    log.info(f" Маршруты: {len(ROUTES)} записей")

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    engine = create_engine(DB_URL)
    log.info("Подключение к БД установлено")
    apply_schema(engine)
    log.info("─── Загрузка ледовых данных (NSIDC) ───")
    load_ice(engine)
    log.info("─── Загрузка грузоперевозок ───")
    load_shipping(engine)
    log.info("─── Загрузка маршрутов ───")
    load_routes(engine)
if __name__ == "__main__":
    main()
