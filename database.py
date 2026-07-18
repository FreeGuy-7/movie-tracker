"""Small LMDB-backed document store used by the web application."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import lmdb


class DocumentStore:
    def __init__(self, path: str | Path) -> None:
        database_path = Path(path)
        database_path.mkdir(parents=True, exist_ok=True)
        self.environment = lmdb.open(str(database_path), map_size=256 * 1024 * 1024, max_dbs=8, subdir=True)
        self.collections = {name: self.environment.open_db(name.encode("utf-8")) for name in ("users", "triggers", "states", "sessions")}

    def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        with self.environment.begin(db=self.collections[collection]) as transaction:
            value = transaction.get(document_id.encode("utf-8"))
        return json.loads(value.decode("utf-8")) if value else None

    def put(self, collection: str, document: dict[str, Any]) -> None:
        document_id = str(document["id"])
        value = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with self.environment.begin(write=True, db=self.collections[collection]) as transaction:
            transaction.put(document_id.encode("utf-8"), value)

    def delete(self, collection: str, document_id: str) -> None:
        with self.environment.begin(write=True, db=self.collections[collection]) as transaction:
            transaction.delete(document_id.encode("utf-8"))

    def all(self, collection: str) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        with self.environment.begin(db=self.collections[collection]) as transaction:
            cursor = transaction.cursor()
            for _, value in cursor:
                documents.append(json.loads(value.decode("utf-8")))
        return documents

    def find(self, collection: str, predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        return [document for document in self.all(collection) if predicate(document)]

    def count(self, collection: str, predicate: Callable[[dict[str, Any]], bool] | None = None) -> int:
        return len(self.all(collection)) if predicate is None else len(self.find(collection, predicate))

    def close(self) -> None:
        self.environment.close()
