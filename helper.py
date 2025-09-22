import datetime
import flask
from dateutil.parser import parse
from flask import g, request
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Supabase Postgres connection URL (from your .env)
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL environment variable")

# Create a SQLAlchemy engine
ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True)

#############################################
# Database Setup and Utility Functions
#############################################

def get_db() -> Connection:
    """
    Returns a SQLAlchemy Connection bound to the Supabase Postgres database.
    """
    if '_db' not in g:
        g._db = ENGINE.connect()
    return g._db

def close_db(error=None):
    """
    Closes the SQLAlchemy Connection at the end of the request.
    """
    conn = g.pop('_db', None)
    if conn is not None:
        conn.close()



def init_db():
    db = get_db()

    # Use a transaction context manager
    with db.begin():
        # ── 1) Create all tables if they don't exist ──

        ddl_statements = [
            """
            CREATE TABLE IF NOT EXISTS surgeons (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                call_levels TEXT NOT NULL,
                nlth BOOLEAN NOT NULL DEFAULT FALSE,
                team TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS saved_schedule (
                id SERIAL PRIMARY KEY,
                year INTEGER,
                month INTEGER,
                schedule_data JSONB,
                date_saved TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
                UNIQUE (year, month)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS max_calls_config (
                level_group TEXT PRIMARY KEY,
                max_calls INTEGER
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS surgeon_availability (
                id SERIAL PRIMARY KEY,
                surgeon_id INTEGER REFERENCES surgeons(id),
                request_type TEXT,
                date DATE
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS global_config (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """,
            # New table: one row per (team, weekday)
            """
            CREATE TABLE IF NOT EXISTS team_day_preferences (
                team TEXT NOT NULL,
                weekday INTEGER NOT NULL,
                preference INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (team, weekday)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS preassignments (
                id SERIAL PRIMARY KEY,
                year INTEGER,
                month INTEGER,
                preassignment_data JSONB,
                date_updated TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
                UNIQUE (year, month)
            );
            """
        ]
        for ddl in ddl_statements:
            # exec_driver_sql allows raw SQL strings
            db.exec_driver_sql(ddl)

        # ── 1b) Ensure surgeons table has required columns/constraints ──
        # Add columns if they are missing
$
        db.exec_driver_sql(
            """
            ALTER TABLE surgeons
            ADD COLUMN IF NOT EXISTS nlth BOOLEAN DEFAULT FALSE
            """
        )
        db.exec_driver_sql(
            """
            ALTER TABLE surgeons
            ADD COLUMN IF NOT EXISTS team TEXT DEFAULT ''
            """
        )
        # Backfill NULLs, then enforce NOT NULL
        db.exec_driver_sql("UPDATE surgeons SET nlth = FALSE WHERE nlth IS NULL")
        db.exec_driver_sql("UPDATE surgeons SET team = '' WHERE team IS NULL")
        db.exec_driver_sql("ALTER TABLE surgeons ALTER COLUMN nlth SET NOT NULL")
        db.exec_driver_sql("ALTER TABLE surgeons ALTER COLUMN team SET NOT NULL")

        # ── 2) Seed global_config defaults ──
        defaults = {
            "no_call_hard":          "1",
            "fairness_weight":       "1000",
            "gamma_no_call":         "10",
            "gamma_unavail_prev":    "5",
            "gamma_1B":              "1",
            "gamma_balance":         "100",
            "gamma_spacing":         "10",
            "spacing_threshold":     "7",
            "gamma_weekend_balance": "50",
            "gamma_consec_weekend":  "20",
            "gamma_team_pref":       "10"
        }
        
        insert_gc = text("""
            INSERT INTO global_config (key, value)
            VALUES (:key, :value)
            ON CONFLICT (key) DO NOTHING
        """)
        for key, val in defaults.items():
            db.execute(insert_gc, {"key": key, "value": val})

        # ── 3) Seed max_calls_config defaults ──
        max_defaults = {"1": 10, "2": 10, "3": 10, "4": 10}
        insert_mc = text("""
            INSERT INTO max_calls_config (level_group, max_calls)
            VALUES (:group, :max_calls)
            ON CONFLICT (level_group) DO NOTHING
        """)
        for group, max_val in max_defaults.items():
            db.execute(insert_mc, {"group": group, "max_calls": max_val})

        # Seed one row per team per weekday with default preference=0
        teams = ['Team 1', 'Team 2', 'Team 3', 'Team 4', 'Urology']
        insert_pref = text("""
        INSERT INTO team_day_preferences (team, weekday, preference)
        VALUES (:team, :weekday, 0)
        ON CONFLICT (team, weekday) DO NOTHING
        """)

        for team in teams:
            for weekday in range(7):
                db.execute(insert_pref, {"team": team, "weekday": weekday})

        # Transaction will be automatically committed when exiting the context

def group_dates(date_list):
    """
    Given a sorted list of ISO date strings, group consecutive dates into ranges.
    Returns a list of dictionaries with keys 'start' and 'end'.
    """
    if not date_list:
        return []
    # Convert the date strings to date objects.
    # Normalize everything to date objects
    date_objs = []
    for d in date_list:
        if isinstance(d, datetime.date):
            date_objs.append(d)
        elif isinstance(d, str):
            # Fastest way to parse YYYY-MM-DD
            date_objs.append(datetime.date.fromisoformat(d))
        else:
            raise TypeError(f"Unsupported type in date_list: {type(d)}")
    date_objs.sort()
    
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

def parse_call_levels(call_levels):
    if isinstance(call_levels, (list, tuple)):
        return [str(l).strip() for l in call_levels if str(l).strip()]
    if not call_levels:
        return []
    s = str(call_levels).strip().strip('"').strip("'")
    return [lvl.strip().strip('"').strip("'") for lvl in s.split(',') if lvl.strip()]


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
# Team preferences by day
#############################################

def get_team_day_prefs():
    db = get_db()
    result = db.execute(
        text("SELECT team, weekday, preference FROM team_day_preferences")
    )
    rows = result.mappings().all()
    # build { team: { weekday: preference, … }, … }
    prefs = {}
    for row in rows:
        team = row['team']
        wd   = row['weekday']
        pref = row['preference']
        prefs.setdefault(team, {})[wd] = pref
    return prefs

def update_team_day_prefs(new_prefs):
    db = get_db()
    with db.begin():
        stmt = text("""
            UPDATE team_day_preferences
               SET preference = :preference
             WHERE team      = :team
               AND weekday   = :weekday
        """)
        for team, by_wd in new_prefs.items():
            for wd, pref in by_wd.items():
                db.execute(stmt, {
                    "preference": pref,
                    "team":       team,
                    "weekday":    wd
                })

#############################################
# Global Config for No Call Request Handling
#############################################

def get_global_config():
    db = get_db()
    result = db.execute(text("SELECT key, value FROM global_config"))
    rows = result.fetchall()
    return {row._mapping['key']: row._mapping['value'] for row in rows}


def update_global_config(new_config):
    db = get_db()
    with db.begin():
        upsert_stmt = text(
            """
            INSERT INTO global_config (key, value)
            VALUES (:key, :value)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """
        )
        for key, value in new_config.items():
            db.execute(upsert_stmt, {"key": key, "value": str(value)})

def get_all_surgeons():
    db = get_db()
    result = db.execute(text("SELECT * FROM surgeons"))
    rows = result.mappings().all()      # ← get list of dicts
    return [dict(r) for r in rows]

def get_max_calls_config():
    db = get_db()
    result = db.execute(text("SELECT level_group, max_calls FROM max_calls_config"))
    rows = result.fetchall()
    return {row._mapping['level_group']: row._mapping['max_calls'] for row in rows}

def update_max_calls_config(new_config):
    db = get_db()
    with db.begin():
        for group, max_val in new_config.items():
            db.execute(
                text("UPDATE max_calls_config SET max_calls = :max_val WHERE level_group = :group"),
                {"group": group, "max_val": max_val}
            )

def get_availability_requests():
    db = get_db()
    result = db.execute(text(
        "SELECT surgeon_id, request_type, date FROM surgeon_availability"
    ))
    rows = result.fetchall()
    requests = {}
    for row in rows:
        mapping = row._mapping
        sid = int(mapping['surgeon_id'])
        requests.setdefault(sid, []).append({
            'date': mapping['date'],
            'request_type': mapping['request_type']
        })
    return requests