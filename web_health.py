# web_health.py
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "service": "crypto-alerts-bot"}

@app.get("/health")
def health():
    return {"status": "ok"}
