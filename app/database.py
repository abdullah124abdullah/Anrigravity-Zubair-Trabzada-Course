import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'skool_app.db')

def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create tables if they don't exist."""
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        skool_email TEXT NOT NULL,
        skool_password TEXT NOT NULL,
        classroom_url TEXT NOT NULL,
        google_token TEXT,
        google_email TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',
        progress_percent INTEGER DEFAULT 0,
        current_course TEXT,
        current_lesson TEXT,
        total_lessons INTEGER DEFAULT 0,
        completed_lessons INTEGER DEFAULT 0,
        download_dir TEXT,
        drive_folder_id TEXT,
        started_at TEXT,
        finished_at TEXT,
        error_message TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    
    conn.commit()
    conn.close()

def save_user(skool_email, skool_password, classroom_url):
    """Save or update a user's Skool credentials."""
    conn = get_db()
    c = conn.cursor()
    
    # Check if user already exists
    c.execute('SELECT id FROM users WHERE skool_email = ?', (skool_email,))
    existing = c.fetchone()
    
    if existing:
        c.execute('''UPDATE users SET skool_password = ?, classroom_url = ?, updated_at = ?
                      WHERE skool_email = ?''',
                  (skool_password, classroom_url, datetime.now().isoformat(), skool_email))
        user_id = existing['id']
    else:
        c.execute('''INSERT INTO users (skool_email, skool_password, classroom_url)
                      VALUES (?, ?, ?)''',
                  (skool_email, skool_password, classroom_url))
        user_id = c.lastrowid
    
    conn.commit()
    conn.close()
    return user_id

def get_user(user_id):
    """Get user by ID."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    return dict(user) if user else None

def get_user_by_email(email):
    """Get user by Skool email."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE skool_email = ?', (email,))
    user = c.fetchone()
    conn.close()
    return dict(user) if user else None

def save_google_token(user_id, token_json, google_email=None):
    """Save Google OAuth token for a user."""
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET google_token = ?, google_email = ?, updated_at = ? WHERE id = ?',
              (token_json, google_email, datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()

def create_job(user_id, download_dir):
    """Create a new scraping job."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO jobs (user_id, status, download_dir, started_at)
                  VALUES (?, 'running', ?, ?)''',
              (user_id, download_dir, datetime.now().isoformat()))
    job_id = c.lastrowid
    conn.commit()
    conn.close()
    return job_id

def update_job(job_id, **kwargs):
    """Update job fields."""
    conn = get_db()
    c = conn.cursor()
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    c.execute(f'UPDATE jobs SET {sets} WHERE id = ?', values)
    conn.commit()
    conn.close()

def get_job(job_id):
    """Get job by ID."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM jobs WHERE id = ?', (job_id,))
    job = c.fetchone()
    conn.close()
    return dict(job) if job else None

def get_latest_job(user_id):
    """Get the most recent job for a user."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC LIMIT 1', (user_id,))
    job = c.fetchone()
    conn.close()
    return dict(job) if job else None
