import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), payload TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_run(self, run):
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?)", (run["run_id"], json.dumps(run, ensure_ascii=False)))

    def get_run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("找不到对应运行记录")
        return json.loads(row[0])

    def add_review(self, review, decision):
        self.get_run(review.run_id)
        value = {"id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(), **review.model_dump(mode="json"), "decision": decision}
        with self.connect() as db:
            db.execute("INSERT INTO reviews VALUES (?,?,?)", (value["id"], review.run_id, json.dumps(value, ensure_ascii=False)))
        return value

    def export(self):
        with self.connect() as db:
            runs = [json.loads(r[0]) for r in db.execute("SELECT payload FROM runs ORDER BY rowid")]
            reviews = [json.loads(r[0]) for r in db.execute("SELECT payload FROM reviews ORDER BY rowid")]
        return {"runs": runs, "reviews": reviews}
