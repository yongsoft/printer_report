"""
Shared database module for printer page count storage.
Uses SQLite with WAL mode for crash-safe concurrent reads/writes.
Automatically migrates existing CSV data on first run.
"""

import csv
import os
import sqlite3
import logging
import shutil
from datetime import datetime, timedelta

logger = logging.getLogger('report_logger')

DB_FILE = None  # Set by init_db()


def init_db(script_dir):
    """Initialize the database path and create tables if needed."""
    global DB_FILE
    DB_FILE = os.path.join(script_dir, 'printer_data.db')


def get_db_path():
    return DB_FILE


def get_connection():
    """Get a SQLite connection with WAL mode and reasonable settings."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def ensure_schema():
    """Create the table if it doesn't exist."""
    conn = get_connection()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS page_counts (
                date TEXT PRIMARY KEY,
                total_page_count INTEGER NOT NULL DEFAULT 0,
                net_increase INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_page_counts_date ON page_counts(date)')
        conn.commit()
        logger.info("Database schema ensured")
    finally:
        conn.close()


def migrate_from_csv(csv_file):
    """Import existing CSV data into SQLite if the DB is empty or new."""
    if not os.path.exists(csv_file):
        logger.info("No CSV file found, skipping migration")
        return

    conn = get_connection()
    try:
        count = conn.execute('SELECT COUNT(*) FROM page_counts').fetchone()[0]
        if count > 0:
            logger.info(f"Database already has {count} rows, skipping CSV migration")
            return

        with open(csv_file, 'r') as f:
            reader = csv.reader(f)
            rows = []
            for row in reader:
                if len(row) >= 3:
                    date_str, total, net = row[0], row[1], row[2]
                    # Skip header if present
                    if date_str.lower() == 'date':
                        continue
                    rows.append((date_str, int(total), int(net), date_str))

        if rows:
            conn.executemany(
                'INSERT OR REPLACE INTO page_counts (date, total_page_count, net_increase, recorded_at) VALUES (?, ?, ?, ?)',
                rows
            )
            conn.commit()
            logger.info(f"Migrated {len(rows)} rows from CSV to SQLite")

            # Backup the original CSV
            backup = csv_file + '.migrated_backup'
            if not os.path.exists(backup):
                shutil.copy2(csv_file, backup)
                logger.info(f"Backed up original CSV to {backup}")
        else:
            logger.info("CSV file is empty, nothing to migrate")
    except Exception as e:
        logger.error(f"CSV migration error: {e}")
        conn.rollback()
    finally:
        conn.close()


def write_page_count(date_str, total_page_count, net_increase):
    """Atomically insert or update a page count record."""
    conn = get_connection()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO page_counts (date, total_page_count, net_increase, recorded_at)
            VALUES (?, ?, ?, datetime('now'))
        ''', (date_str, total_page_count, net_increase))
        conn.commit()
        logger.info(f"Successfully wrote data for {date_str}: total={total_page_count}, net={net_increase}")
    except Exception as e:
        logger.error(f"Failed to write to database: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


def read_previous_page_count():
    """Read the most recent page count record. Returns (count, date) or (0, None)."""
    conn = get_connection()
    try:
        row = conn.execute(
            'SELECT total_page_count, date FROM page_counts ORDER BY date DESC LIMIT 1'
        ).fetchone()
        if row:
            return int(row[0]), row[1]
        return 0, None
    except Exception as e:
        logger.error(f"Failed to read previous page count: {e}")
        return 0, None
    finally:
        conn.close()


def is_data_exists_for_date(date_str):
    """Check if data already exists for a given date."""
    conn = get_connection()
    try:
        row = conn.execute(
            'SELECT 1 FROM page_counts WHERE date = ? LIMIT 1', (date_str,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def read_all_data():
    """Read all page count records, ordered by date. Returns list of dicts."""
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT date, total_page_count, net_increase FROM page_counts ORDER BY date ASC'
        ).fetchall()
        return [
            {'date': r[0], 'total_page_count': r[1], 'net_increase': r[2]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Failed to read all data: {e}")
        return []
    finally:
        conn.close()


def fill_missing_dates():
    """Fill any gaps in the date sequence by carrying forward the previous page count."""
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT date, total_page_count, net_increase FROM page_counts ORDER BY date ASC'
        ).fetchall()

        if not rows:
            return

        start_date = datetime.strptime(rows[0][0], "%Y-%m-%d")
        end_date = datetime.strptime(rows[-1][0], "%Y-%m-%d")

        dates_dict = {r[0]: (r[1], r[2]) for r in rows}

        current = start_date
        missing = []
        prev_count = 0

        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            if date_str in dates_dict:
                prev_count = dates_dict[date_str][0]
            else:
                logger.warning(f"Found missing date: {date_str}, auto-filled")
                missing.append((date_str, prev_count, 0))
            current += timedelta(days=1)

        if missing:
            conn.executemany(
                'INSERT OR REPLACE INTO page_counts (date, total_page_count, net_increase, recorded_at) VALUES (?, ?, ?, datetime("now"))',
                missing
            )
            conn.commit()
            logger.info(f"Filled {len(missing)} missing dates")
        else:
            logger.info("No missing dates found")
    except Exception as e:
        logger.error(f"Error filling missing dates: {e}")
        conn.rollback()
    finally:
        conn.close()


def backup_database():
    """Create a timestamped backup of the database file."""
    if not os.path.exists(DB_FILE):
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{DB_FILE}.backup_{timestamp}"
    try:
        # Use SQLite backup API for a safe online backup
        source = get_connection()
        dest = sqlite3.connect(backup_path)
        source.backup(dest)
        dest.close()
        source.close()
        logger.info(f"Database backed up to {backup_path}")
    except Exception as e:
        logger.error(f"Database backup failed: {e}")
