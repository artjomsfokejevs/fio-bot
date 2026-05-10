#!/bin/bash
cd "$(dirname "$0")"
[ ! -d ".venv" ] && python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -q
mkdir -p data/intake
cp -n .env.example .env 2>/dev/null
python app.py
