"""SQLite checkpoint creation for short-lived local LangGraph runs."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

DB_PATH = Path("state/agent_runs.sqlite3")


@contextmanager
def sqlite_checkpointer(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    serializer = JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=None,
                                    allowed_msgpack_modules=None)
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        saver = SqliteSaver(connection, serde=serializer)
        saver.setup()
        yield saver
    finally:
        connection.close()
