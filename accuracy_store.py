"""Idempotent, durable storage for real per-bank accuracy outcomes."""

import math
import sqlite3
import threading
from contextlib import contextmanager


WEIGHTED_PRIOR_OPERATIONS = 20
WEIGHTED_PRIOR_WIN_RATE = 80.0
WEIGHTED_PRIOR_WINS = 16
WEIGHTED_PRIOR_LOSSES = 4
MAX_GALES = 5
WINDOW_OPERATIONS = 100


def weighted_accuracy(real_wins, real_losses):
    """Return accuracy with a 16-win/4-loss prior, without financial effects."""
    wins = int(real_wins)
    losses = int(real_losses)
    if wins < 0 or losses < 0:
        raise ValueError("Real result counts cannot be negative")
    return round(
        (WEIGHTED_PRIOR_WINS + wins)
        / (WEIGHTED_PRIOR_OPERATIONS + wins + losses)
        * 100,
        2,
    )


def gales_for_accuracy(percent):
    value = float(percent)
    if not math.isfinite(value):
        raise ValueError("Accuracy must be finite")
    if value >= 80:
        return 0
    if value >= 78:
        return 1
    if value >= 76:
        return 2
    if value >= 74:
        return 3
    if value >= 72:
        return 4
    return 5


class AccuracyStore:
    """SQLite for isolated tests; PostgreSQL for durable deployment storage."""

    def __init__(self, database_url):
        self.database_url = (database_url or "").strip()
        if not self.database_url:
            raise ValueError("BASKARAIDO_ACCURACY_DATABASE_URL is required")
        if self.database_url.startswith("sqlite:///"):
            self.backend = "sqlite"
            self.database = self.database_url[len("sqlite:///"):]
            if not self.database:
                raise ValueError("SQLite database path is empty")
        elif self.database_url.startswith(("postgres://", "postgresql://")):
            self.backend = "postgres"
            self.database = self.database_url.replace("postgres://", "postgresql://", 1)
        else:
            raise ValueError("Database URL must use PostgreSQL or SQLite")
        self.lock = threading.RLock()

    def _connect(self):
        if self.backend == "sqlite":
            return sqlite3.connect(self.database, timeout=10)
        import psycopg
        return psycopg.connect(self.database, connect_timeout=10)

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS baskaraido_order_results (
                    bank TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('win', 'loss', 'draw')),
                    gale_level INTEGER NOT NULL CHECK (gale_level BETWEEN 0 AND 5),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (bank, order_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS baskaraido_accuracy_windows (
                    bank TEXT PRIMARY KEY,
                    window_id INTEGER NOT NULL DEFAULT 1,
                    operations_completed INTEGER NOT NULL DEFAULT 0
                        CHECK (operations_completed BETWEEN 0 AND 99)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS baskaraido_accuracy_results (
                    bank TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('win', 'loss')),
                    gale_level INTEGER NOT NULL CHECK (gale_level BETWEEN 0 AND 5),
                    window_id INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (bank, operation_id)
                )
                """
            )

    def record_settlement(
        self,
        bank,
        order_id,
        operation_id,
        outcome,
        gale_level,
        terminal_outcome=None,
    ):
        if outcome not in ("win", "loss", "draw"):
            raise ValueError("Invalid settled order outcome")
        if terminal_outcome not in (None, "win", "loss"):
            raise ValueError("Invalid terminal operation outcome")
        level = int(gale_level)
        if level < 0 or level > MAX_GALES:
            raise ValueError("Gale level outside supported range")

        marker = "?" if self.backend == "sqlite" else "%s"
        bank_id = str(bank)
        values = (bank_id, str(order_id), str(operation_id), outcome, level)
        with self.lock:
            with self._connection() as conn:
                cursor = conn.execute(
                    f"""
                    INSERT INTO baskaraido_order_results
                        (bank, order_id, operation_id, outcome, gale_level)
                    VALUES ({marker}, {marker}, {marker}, {marker}, {marker})
                    ON CONFLICT (bank, order_id) DO NOTHING
                    """,
                    values,
                )
                inserted_order = cursor.rowcount == 1
                if not inserted_order or terminal_outcome is None:
                    return inserted_order, False, False

                conn.execute(
                    f"""
                    INSERT INTO baskaraido_accuracy_windows
                        (bank, window_id, operations_completed)
                    VALUES ({marker}, 1, 0)
                    ON CONFLICT (bank) DO NOTHING
                    """,
                    (bank_id,),
                )
                state = conn.execute(
                    f"""
                    SELECT window_id, operations_completed
                    FROM baskaraido_accuracy_windows
                    WHERE bank = {marker}
                    """,
                    (bank_id,),
                ).fetchone()
                window_id, operations_completed = int(state[0]), int(state[1])

                cursor = conn.execute(
                    f"""
                    INSERT INTO baskaraido_accuracy_results
                        (bank, operation_id, outcome, gale_level, window_id)
                    VALUES ({marker}, {marker}, {marker}, {marker}, {marker})
                    ON CONFLICT (bank, operation_id) DO NOTHING
                    """,
                    (bank_id, str(operation_id), terminal_outcome, level, window_id),
                )
                inserted_operation = cursor.rowcount == 1
                if not inserted_operation:
                    return inserted_order, False, False

                operations_completed += 1
                reset_window = operations_completed >= WINDOW_OPERATIONS
                if reset_window:
                    conn.execute(
                        f"""
                        UPDATE baskaraido_accuracy_windows
                        SET window_id = {marker}, operations_completed = 0
                        WHERE bank = {marker}
                        """,
                        (window_id + 1, bank_id),
                    )
                else:
                    conn.execute(
                        f"""
                        UPDATE baskaraido_accuracy_windows
                        SET operations_completed = {marker}
                        WHERE bank = {marker}
                        """,
                        (operations_completed, bank_id),
                    )
        return inserted_order, inserted_operation, reset_window

    def load_counts(self, banks):
        names = tuple(str(bank) for bank in banks)
        counts = {
            bank: {
                "wins": 0,
                "losses": 0,
                "wins_janela": 0,
                "losses_janela": 0,
                "operacoes_janela": 0,
                "wins_by_level": {level: 0 for level in range(MAX_GALES + 1)},
            }
            for bank in names
        }
        if not names:
            return counts

        marker = "?" if self.backend == "sqlite" else "%s"
        placeholders = ", ".join([marker] * len(names))
        with self._connection() as conn:
            totals = conn.execute(
                f"""
                SELECT bank, outcome, gale_level, COUNT(*)
                FROM baskaraido_accuracy_results
                WHERE bank IN ({placeholders})
                GROUP BY bank, outcome, gale_level
                """,
                names,
            ).fetchall()
            windows = conn.execute(
                f"""
                SELECT bank, window_id, operations_completed
                FROM baskaraido_accuracy_windows
                WHERE bank IN ({placeholders})
                """,
                names,
            ).fetchall()
            active_counts = conn.execute(
                f"""
                SELECT result.bank, result.outcome, COUNT(*)
                FROM baskaraido_accuracy_results AS result
                JOIN baskaraido_accuracy_windows AS window
                  ON window.bank = result.bank
                 AND window.window_id = result.window_id
                WHERE result.bank IN ({placeholders})
                GROUP BY result.bank, result.outcome
                """,
                names,
            ).fetchall()

        for bank, outcome, level, count in totals:
            entry = counts.get(str(bank))
            if entry is None:
                continue
            count = int(count)
            if outcome == "win":
                entry["wins"] += count
                entry["wins_by_level"][int(level)] += count
            elif outcome == "loss":
                entry["losses"] += count

        for bank, _window_id, operations_completed in windows:
            if str(bank) in counts:
                counts[str(bank)]["operacoes_janela"] = int(operations_completed)

        for bank, outcome, count in active_counts:
            entry = counts.get(str(bank))
            if entry is None:
                continue
            if outcome == "win":
                entry["wins_janela"] = int(count)
            elif outcome == "loss":
                entry["losses_janela"] = int(count)
        return counts
