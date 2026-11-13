"""Shared, deliberately narrow SQLite connection lifetime policy.

No global patch, pooling or implicit isolation/thread changes. A connection
returned by Store.connect is owned by its caller; its transaction context is
single-use. Borrowed connections must not be re-entered/closed by callees.
"""
import sqlite3


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback exactly as sqlite3 does, then close even on commit failure."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()
