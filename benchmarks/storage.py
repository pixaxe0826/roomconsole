"""Benchmark-only SQLite ownership; retain transactions, close handles on exit."""
from pathlib import Path
import os
import sqlite3
from unittest.mock import patch


class ClosingConnection(sqlite3.Connection):
    """Keep SQLite commit/rollback semantics but release each owned handle promptly.

    sqlite3.Connection.__exit__ does not close a connection. A captured exception
    can retain that handle even after gc.collect(), preventing Windows cleanup.
    The benchmark never reuses a connection after its transaction context ends.
    """
    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def owned_sqlite(root: Path):
    """Limit the worker's SQLite factory to its temporary tree, including imports.

    Installed only inside Runtime's ExitStack, never in the deployed web server.
    No transaction, query, timeout, isolation-level or thread check is replaced.
    """
    root = root.resolve()
    original = sqlite3.connect

    def connect(database, *args, **kwargs):
        path = Path(os.fsdecode(database)).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError('Refusing SQLite access outside the benchmark temporary tree')
        if len(args) > 4 or kwargs.get('factory', ClosingConnection) is not ClosingConnection:
            raise RuntimeError('An unreviewed SQLite connection factory is not allowed')
        kwargs['factory'] = ClosingConnection
        return original(database, *args, **kwargs)

    return patch.object(sqlite3, 'connect', connect)
