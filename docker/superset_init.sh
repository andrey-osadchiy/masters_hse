#!/bin/bash
set -e

superset db upgrade

superset fab create-admin \
  --username admin \
  --firstname Admin \
  --lastname NSR \
  --email admin@nsr.local \
  --password admin123 || true

superset init

superset run -h 0.0.0.0 -p 8088 --with-threads --reload --debugger
