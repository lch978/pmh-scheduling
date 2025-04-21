import datetime
import sqlite3
from dateutil.parser import parse
from flask import g, request

#############################################
# Database Setup and Utility Functions
#############################################

DATABASE = 'surgeon_scheduler.db'

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row  # Enables dict-like access.
    return db

def init_db():
    from app import app
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        # Create table for surgeons.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS surgeons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                call_levels TEXT NOT NULL
            )
        ''')
        # Create table for saved_schedule with year and month.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS saved_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER,
                month INTEGER,
                schedule_data TEXT,
                date_saved TEXT,
                UNIQUE (year, month)
            )
        ''')
        # Create table for maximum calls configuration.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS max_calls_config (
                level_group TEXT PRIMARY KEY,
                max_calls INTEGER
            )
        ''')
        # Create table for surgeon_availability to record unavailability/no_call requests.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS surgeon_availability (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                surgeon_id INTEGER,
                request_type TEXT,   -- "unavailable" or "no_call"
                date TEXT,
                FOREIGN KEY(surgeon_id) REFERENCES surgeons(id)
            )
        ''')
        # Create global_config table.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS global_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Global configuration table and default values.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS global_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Insert default values if not present:
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('no_call_hard', '1')")
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('fairness_weight', '1000')")
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('gamma_no_call', '10')")
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('gamma_unavail_prev', '5')")
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('gamma_1B', '1')")
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('gamma_balance', '100')")
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('gamma_spacing', '10')")
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('spacing_threshold', '7')")

        # Insert default configuration if not present.
        default_config = {"1": 10, "2": 10, "3": 10, "4": 10}
        for group, max_val in default_config.items():
            cursor.execute("INSERT OR IGNORE INTO max_calls_config (level_group, max_calls) VALUES (?, ?)", (group, max_val))
        # Insert default global config for no_call (1 = hard, 0 = soft)
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('no_call_hard', '1')")
        db.commit()

init_db()

def group_dates(date_list):
    """
    Given a sorted list of ISO date strings, group consecutive dates into ranges.
    Returns a list of dictionaries with keys 'start' and 'end'.
    """
    if not date_list:
        return []
    # Convert the date strings to date objects.
    date_objs = sorted([datetime.datetime.strptime(d, "%Y-%m-%d").date() for d in date_list])
    groups = []
    start = date_objs[0]
    end = date_objs[0]
    for d in date_objs[1:]:
        if d == end + datetime.timedelta(days=1):
            end = d
        else:
            groups.append({"start": start.isoformat(), "end": end.isoformat()})
            start = d
            end = d
    groups.append({"start": start.isoformat(), "end": end.isoformat()})
    return groups

def parse_call_levels(call_levels_str):
    """
    Converts a comma-separated string of call levels (e.g. "1A,2A,2B,3")
    into a list of trimmed call-level codes.
    Returns an empty list if call_levels_str is empty or None.
    """
    if not call_levels_str:
        return []
    return [level.strip() for level in call_levels_str.split(',') if level.strip()]

def get_level2_group(surgeon):
    """
    Level-2 grouping:
      1 → 2A only
      2 → 2A + 2B
      3 → 2B only
    """
    cl       = parse_call_levels(surgeon.get("call_levels",""))
    has_2A   = "2A" in cl
    has_2B   = "2B" in cl
    has_3    = "3" in cl

    # 1A-only surgeons who still need supervision
    if has_2A and not has_2B:
        return 1

    # 2A-only surgeons who do NOT need supervision
    if has_2A and has_2B:
        return 2

    # Supervisors (can do both 2A and 2B)
    if not has_2A and has_2B and not has_3:
        return 3

    # 3rd calls who are also supervisors
    if not has_2A and has_2B and has_3:
        return 4
    
    return None

def get_year_month():
    """
    Reads 'year' and 'month' from query parameters. If not provided,
    defaults to today's year and month.
    """
    try:
        year_val = int(request.args.get('year', datetime.date.today().year))
        month_val = int(request.args.get('month', datetime.date.today().month))
    except ValueError:
        year_val = datetime.date.today().year
        month_val = datetime.date.today().month
    return year_val, month_val

#############################################
# Global Config for No Call Request Handling
#############################################

def get_global_config():
    db = get_db()
    rows = db.execute("SELECT key, value FROM global_config").fetchall()
    config = {row["key"]: row["value"] for row in rows}
    return config

def update_global_config(new_config):
    db = get_db()
    cursor = db.cursor()
    for key, value in new_config.items():
        cursor.execute("UPDATE global_config SET value = ? WHERE key = ?", (value, key))
    db.commit()

def get_all_surgeons():
    db = get_db()
    rows = db.execute("SELECT * FROM surgeons").fetchall()
    return [dict(row) for row in rows]

def get_max_calls_config():
    db = get_db()
    rows = db.execute("SELECT level_group, max_calls FROM max_calls_config").fetchall()
    config = {}
    for row in rows:
        config[row["level_group"]] = row["max_calls"]
    return config

def update_max_calls_config(new_config):
    db = get_db()
    cursor = db.cursor()
    for group, max_val in new_config.items():
        cursor.execute("UPDATE max_calls_config SET max_calls = ? WHERE level_group = ?", (max_val, group))
    db.commit()

def get_availability_requests():
    db = get_db()
    rows = db.execute("SELECT surgeon_id, request_type, date FROM surgeon_availability").fetchall()
    requests = {}
    for row in rows:
        # Convert surgeon_id from the row to integer.
        sid = int(row["surgeon_id"])
        if sid not in requests:
            requests[sid] = []
        requests[sid].append({
            "date": row["date"],
            "request_type": row["request_type"]
        })
    return requests
