import sqlite3
import logging
from contextlib import contextmanager
from typing import List, Dict, Any, Optional
from app.config import DB_PATH

logger = logging.getLogger("linkplease.db")

@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rules (
                rule_id TEXT PRIMARY KEY,
                keyword TEXT NOT NULL,
                dm_message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                message TEXT NOT NULL,
                dm_id TEXT,
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL, -- pending, sending, dm_accepted, delivered, failed, cancelled
                attempt_number INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT unq_user_rule UNIQUE (user_id, rule_id)
            );

            CREATE TABLE IF NOT EXISTS duplicate_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                user_id TEXT,
                rule_id TEXT,
                reason TEXT NOT NULL, -- duplicate_event, duplicate_user_rule
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS deleted_comments (
                comment_id TEXT PRIMARY KEY,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
    logger.info("Database initialized successfully.")

def reset_unfinished_dms():
    """Resets any DM left in 'sending' status back to 'pending' on startup."""
    with get_connection() as conn:
        conn.execute("UPDATE dms SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE status = 'sending'")
        conn.commit()

def add_rule(rule_id: str, keyword: str, dm_message: str) -> Dict[str, Any]:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO rules (rule_id, keyword, dm_message) VALUES (?, ?, ?)",
            (rule_id, keyword.lower(), dm_message)
        )
        conn.commit()
    return {"rule_id": rule_id, "keyword": keyword, "dm_message": dm_message}

def get_rules() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.execute("SELECT rule_id, keyword, dm_message FROM rules")
        return [dict(row) for row in cursor.fetchall()]

def try_register_event(event_id: str) -> bool:
    """Returns True if new event, False if event_id already exists."""
    with get_connection() as conn:
        try:
            conn.execute("INSERT INTO events (event_id) VALUES (?)", (event_id,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.execute(
                "INSERT INTO duplicate_logs (event_id, reason) VALUES (?, ?)",
                (event_id, "duplicate_event")
            )
            conn.commit()
            return False

def record_deleted_comment(comment_id: str):
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO deleted_comments (comment_id) VALUES (?)", (comment_id,))
        # Cancel any pending or sending DMs for this comment_id
        conn.execute(
            "UPDATE dms SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE comment_id = ? AND status IN ('pending', 'sending')",
            (comment_id,)
        )
        conn.commit()

def process_comment_created(event_id: str, text: str, user_id: str, comment_id: str):
    with get_connection() as conn:
        # Check if comment is already deleted
        cur = conn.execute("SELECT 1 FROM deleted_comments WHERE comment_id = ?", (comment_id,))
        if cur.fetchone():
            return

        text_lower = text.lower()
        rules_cur = conn.execute("SELECT rule_id, keyword, dm_message FROM rules")
        rules = rules_cur.fetchall()

        for rule in rules:
            rule_id = rule["rule_id"]
            keyword = rule["keyword"]
            message = rule["dm_message"]

            if keyword in text_lower:
                idempotency_key = f"dm:{user_id}:{rule_id}:attempt:1"
                try:
                    conn.execute(
                        """
                        INSERT INTO dms (user_id, rule_id, comment_id, message, idempotency_key, status, attempt_number)
                        VALUES (?, ?, ?, ?, ?, 'pending', 1)
                        """,
                        (user_id, rule_id, comment_id, message, idempotency_key)
                    )
                except sqlite3.IntegrityError:
                    # User already received/queued DM for this rule
                    conn.execute(
                        "INSERT INTO duplicate_logs (event_id, user_id, rule_id, reason) VALUES (?, ?, ?, ?)",
                        (event_id, user_id, rule_id, "duplicate_user_rule")
                    )
        conn.commit()

def get_next_pending_dm() -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        while True:
            cur = conn.execute(
                """
                SELECT id, user_id, rule_id, comment_id, message, idempotency_key, attempt_number
                FROM dms
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return None
            
            dm = dict(row)
            # Check if comment was deleted
            del_cur = conn.execute("SELECT 1 FROM deleted_comments WHERE comment_id = ?", (dm["comment_id"],))
            if del_cur.fetchone():
                conn.execute("UPDATE dms SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (dm["id"],))
                conn.commit()
                continue
            
            # Mark as sending
            conn.execute("UPDATE dms SET status = 'sending', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (dm["id"],))
            conn.commit()
            return dm

def update_dm_accepted(dm_db_id: int, dm_id: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE dms SET status = 'dm_accepted', dm_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (dm_id, dm_db_id)
        )
        conn.commit()

def update_dm_status(dm_db_id: int, status: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE dms SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, dm_db_id)
        )
        conn.commit()

def requeue_dm_for_retry(dm_db_id: int, new_attempt: int, new_idempotency_key: str):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE dms
            SET status = 'pending', attempt_number = ?, idempotency_key = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_attempt, new_idempotency_key, dm_db_id)
        )
        conn.commit()

def reset_dm_to_pending(dm_db_id: int):
    """Resets DM status to pending without changing attempt_number or idempotency_key (used for network retries)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE dms SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (dm_db_id,)
        )
        conn.commit()

def get_accepted_dms() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT id, dm_id, user_id, rule_id, attempt_number FROM dms WHERE status = 'dm_accepted'"
        )
        return [dict(row) for row in cur.fetchall()]

def get_stats() -> Dict[str, int]:
    with get_connection() as conn:
        sent = conn.execute("SELECT COUNT(*) FROM dms WHERE status = 'delivered'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM dms WHERE status IN ('failed', 'cancelled')").fetchone()[0]
        queued = conn.execute("SELECT COUNT(*) FROM dms WHERE status IN ('pending', 'sending', 'dm_accepted')").fetchone()[0]
        duplicates_blocked = conn.execute("SELECT COUNT(*) FROM duplicate_logs").fetchone()[0]

        return {
            "sent": sent,
            "failed": failed,
            "queued": queued,
            "duplicates_blocked": duplicates_blocked
        }
