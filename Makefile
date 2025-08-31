# Helper commands for local work
.PHONY: install web worker bot

install:
	pip install --upgrade pip
	pip install -r requirements.txt

web:
	uvicorn server_combined:app --host 0.0.0.0 --port 8000

worker:
	python worker.py

bot:
	python bot.py
