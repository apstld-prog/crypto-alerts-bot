# Helper commands for local/dev
.PHONY: install web worker bot seed

install:
	pip install --upgrade pip
	pip install -r requirements.txt

web:
	uvicorn server_combined:app --host 0.0.0.0 --port 8000

worker:
	python worker.py

bot:
	python bot.py

seed:
	psql "$$DATABASE_URL" -f seed.sql
