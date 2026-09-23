from fastapi import FastAPI
import sqlite3

app = FastAPI()

@app.get("/health")
def health():
    with sqlite3.connect("data/app.db") as db:
        db.execute("select 1")
    return {"status": "ok"}
