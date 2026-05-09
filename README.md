# Система оценки потенциала Северного морского пути

**Автор:** Осадчий Андрей Александрович  
**ВШЭ, Факультет компьютерных наук, «Инженерия данных», 2026**

Система рассчитывает интегральный индекс **NSRPotential** на основе четырёх субиндексов:

```
NSRPotential = 0.30 × IceIndex
             + 0.25 × TradeIndex
             + 0.25 × RouteIndex
             + 0.20 × GeoRisk
```

---

## Структура репозитория

```
nsr_potential/
├── docker/
│   ├── docker-compose.yml      # PostgreSQL 15 + Superset 3.1 + Jupyter Lab
│   └── superset_init.sh        # Инициализация Superset
├── sql/
│   └── init.sql                # Схема БД (таблицы, индексы, VIEW)
├── scripts/
│   ├── load_data.py            # ETL: NSIDC + Росатом + VesselFinder
│   └── compute_metrics.py      # Расчёт NSRPotential и субиндексов
├── notebooks/
│   └── 01_EDA.ipynb            # Разведочный анализ данных
├── data/
│   ├── raw/                    # Скачанные данные NSIDC
│   └── processed/              # Обработанные CSV и графики
└── README.md
```

---

## Быстрый старт

### Требования
- Docker Desktop 24+
- Python 3.10+

### 1. Запуск инфраструктуры

```bash
cd docker
chmod +x superset_init.sh
docker compose up -d
```

| Сервис     | URL                        | Логин / Пароль      |
|------------|----------------------------|---------------------|
| PostgreSQL | localhost:**5433**          | nsr_user / nsr_pass |
| Superset   | http://localhost:8088       | admin / admin123    |
| Jupyter    | http://localhost:8888       | token: nsr_token    |

### 2. Установка зависимостей Python

```bash
pip install pandas sqlalchemy psycopg2-binary requests \
            scipy scikit-learn numpy openpyxl
```

### 3. Загрузка данных

```bash
export DB_URL="postgresql://nsr_user:nsr_pass@localhost:5433/nsr_db"
python scripts/load_data.py
```

### 4. Расчёт индекса

```bash
python scripts/compute_metrics.py
```

### 5. Дашборд Superset

1. Открыть http://localhost:8088
2. **Settings → Database Connections → + Database → PostgreSQL**
3. URI: `postgresql://nsr_user:nsr_pass@postgres:5432/nsr_db`
4. **Datasets** → добавить `v_dashboard_main` и `nsr_metrics`
5. Создать чарты и собрать дашборд (инструкция в диссертации)

---

## Источники данных

### IceIndex — NSIDC Sea Ice Index v4
- **Что:** Ежемесячная площадь морского льда Северного полушария, 1978–2024
- **Формат:** CSV, один файл на месяц (`N_MM_extent_v4.0.csv`)
- **URL:** https://noaadata.apps.nsidc.org/NOAA/G02135/north/monthly/data/
- **Ссылка:** Fetterer, F. et al. (2023). *Sea Ice Index, Version 4*. NSIDC. DOI: 10.7265/N5K072F8

### TradeIndex — грузооборот СМП

| Год | Объём, млн т | Источник |
|-----|-------------|----------|
| 2014 | 3.930 | ФГКУ «Администрация СМП» / ФГУП «Атомфлот», [Коммерсант, 2017](https://www.kommersant.ru/doc/3254502) |
| 2015 | 3.982 | То же |
| 2016 | 5.392 | То же |
| 2017 | 7.200 | [Годовой отчёт Росатома 2018](https://report.rosatom.ru/go/rosatom/go_rosatom_2018/go_2018.pdf) |
| 2018 | 12.700 | То же |
| 2019 | 31.530 | [Пресс-релиз Росатома, январь 2020](https://rosatom.ru/journalist/news/gruzooborot-severnogo-morskogo-puti-v-2019-godu-sostavil-rekordnye-31-5-mln-tonn/) |
| 2020 | 32.010 | Росатом via [arctic.gov.ru](https://arctic.gov.ru), февраль 2021 |
| 2021 | 34.850 | [PortNews, январь 2022](https://portnews.ru/news/323752/) |
| 2022 | 34.340 | [PortNews, январь 2023](https://portnews.ru/news/341357/) |
| 2023 | 36.254 | [Атом Медиа, январь 2024](https://atommedia.online/press-releases/istoricheskij-rekord-sevmorputi-obe/) |
| 2024 | 37.920 | [Росатомфлот, январь 2025](https://rosatomflot.ru/press-centr/novosti-predpriyatiya/2025/01/09/11644/) |

### RouteIndex — расстояния маршрутов

Расстояния верифицированы по двум источникам:
- **Действующие маршруты:** [VesselFinder Route Calculator](https://route.vesselfinder.com/)
- **Маршруты через СМП:** [Great Circle Mapper (gcmap.com)](https://www.gcmap.com/)

Waypoints СМП для gcmap.com:
```
70.4°N 57°E  → Карские Ворота
73.5°N 80.5°E → Диксон
71.6°N 128.9°E → Тикси
69.7°N 170.3°E → Певек
65.7°N 169°W  → Берингов пролив
```

**Критерии отбора маршрутов:**
1. Хотя бы один порт севернее 35°N (геогр. релевантность СМП)
2. Маршрут входит в топ мировых торговых коридоров (UNCTAD, 2023)
3. Действующий маршрут через Суэц/Панаму или протяжённостью >5 000 нм

Маршруты южного полушария (Буэнос-Айрес–Лондон, Мумбаи–Марсель и др.) намеренно исключены как геометрически нерелевантные — для них СМП не может быть кратчайшим маршрутом. Маршрут Лос-Анджелес–Лондон включён как отрицательный пример (СМП на 9.7% длиннее).

### GeoRisk — санкционные пакеты ЕС
- **Источник:** EU Council. *Timeline of restrictive measures against Russia*
- **URL:** https://www.consilium.europa.eu/en/policies/sanctions-against-russia/
- **Метод:** число накопленных пакетов, инвертированная min-max нормировка

---

## Модель данных

```
ice_extent        ← NSIDC, ежемесячно 1978–2024
shipping_stats    ← Росатом / Администрация СМП, 2014–2024
route_economics   ← VesselFinder + GCMap, 2024
       ↓
nsr_metrics       ← субиндексы + NSRPotential
       ↓
v_dashboard_main  ← VIEW для Superset
```
