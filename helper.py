import calendar
import datetime
import json
import flask
from dateutil.parser import parse
from flask import g, request, has_app_context
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

def json_cast(param_name):
    """
    Returns the SQL fragment for inserting a JSON parameter.
    Postgres requires CAST(:param AS JSONB), while SQLite expects just :param (TEXT).
    """
    db = get_db()
    if db.dialect.name == 'sqlite':
        return f":{param_name}"
    else:
        return f"CAST(:{param_name} AS JSONB)"

def sql_now():
    """
    Returns the SQL function for current timestamp.
    Postgres: now()
    SQLite: CURRENT_TIMESTAMP
    """
    db = get_db()
    if db.dialect.name == 'sqlite':
        return "CURRENT_TIMESTAMP"
    else:
        return "now()"

def sql_true():
    """
    Returns the SQL literal for TRUE.
    Postgres: TRUE
    SQLite: 1
    """
    db = get_db()
    if db.dialect.name == 'sqlite':
        return "1"
    else:
        return "TRUE"

def sql_false():
    """
    Returns the SQL literal for FALSE.
    Postgres: FALSE
    SQLite: 0
    """
    db = get_db()
    if db.dialect.name == 'sqlite':
        return "0"
    else:
        return "FALSE"

def close_db(error=None):
    """
    Closes the SQLAlchemy Connection at the end of the request.
    """
    conn = g.pop('_db', None)
    if conn is not None:
        conn.close()



def init_db():
    db = get_db()
    is_sqlite = db.dialect.name == 'sqlite'

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
                team TEXT NOT NULL,
                manual_less_calls_credit INTEGER NOT NULL DEFAULT 0,
                manual_more_calls_credit INTEGER NOT NULL DEFAULT 0
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
            ,
            # Stores prior-month last two day assignments per current (year, month)
            """
            CREATE TABLE IF NOT EXISTS prior_last_two (
                id SERIAL PRIMARY KEY,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                m2 JSONB,
                m1 JSONB,
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
                UNIQUE (year, month)
            );
            """
            ,
            # Versioned saved schedules
            """
            CREATE TABLE IF NOT EXISTS saved_schedule_versions (
                id SERIAL PRIMARY KEY,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                version INTEGER NOT NULL,
                version_name TEXT NOT NULL DEFAULT '',
                schedule_data JSONB NOT NULL,
                published BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
                UNIQUE (year, month, version)
            );
            """
        ]
        
        for ddl in ddl_statements:
            if is_sqlite:
                # Basic replacements for SQLite compatibility
                ddl = ddl.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                ddl = ddl.replace("JSONB", "TEXT")
                ddl = ddl.replace("TIMESTAMP WITHOUT TIME ZONE DEFAULT now()", "DATETIME DEFAULT CURRENT_TIMESTAMP")
                ddl = ddl.replace("BOOLEAN", "INTEGER") # SQLite uses 0/1
                ddl = ddl.replace("TRUE", "1").replace("FALSE", "0")
            
            # exec_driver_sql allows raw SQL strings
            db.exec_driver_sql(ddl)

        # ── 1b) Ensure surgeons table has required columns/constraints ──
        # Add columns if they are missing
        if is_sqlite:
            # SQLite ADD COLUMN support is good, but IF NOT EXISTS is syntax dependent.
            # safe to catch error if column exists
            try:
                db.exec_driver_sql("ALTER TABLE surgeons ADD COLUMN nlth INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                db.exec_driver_sql("ALTER TABLE surgeons ADD COLUMN team TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                db.exec_driver_sql("ALTER TABLE surgeons ADD COLUMN manual_less_calls_credit INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                db.exec_driver_sql("ALTER TABLE surgeons ADD COLUMN manual_more_calls_credit INTEGER DEFAULT 0")
            except Exception:
                pass
        else:
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
            db.exec_driver_sql(
                """
                ALTER TABLE surgeons
                ADD COLUMN IF NOT EXISTS manual_less_calls_credit INTEGER DEFAULT 0
                """
            )
            db.exec_driver_sql(
                """
                ALTER TABLE surgeons
                ADD COLUMN IF NOT EXISTS manual_more_calls_credit INTEGER DEFAULT 0
                """
            )
            db.exec_driver_sql(
                """
                ALTER TABLE saved_schedule_versions
                ADD COLUMN IF NOT EXISTS version_name TEXT DEFAULT ''
                """
            )

        # Backfill NULLs, then enforce NOT NULL
        false_val = "0" if is_sqlite else "FALSE"
        db.exec_driver_sql(f"UPDATE surgeons SET nlth = {false_val} WHERE nlth IS NULL")
        db.exec_driver_sql("UPDATE surgeons SET team = '' WHERE team IS NULL")
        db.exec_driver_sql("UPDATE surgeons SET manual_less_calls_credit = 0 WHERE manual_less_calls_credit IS NULL")
        db.exec_driver_sql("UPDATE surgeons SET manual_more_calls_credit = 0 WHERE manual_more_calls_credit IS NULL")
        # Add version_name only when missing (avoid raising in-transaction errors).
        has_version_name = False
        if is_sqlite:
            cols = db.exec_driver_sql("PRAGMA table_info(saved_schedule_versions)").fetchall()
            has_version_name = any((c[1] == "version_name") for c in cols)
        else:
            row = db.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'saved_schedule_versions'
                      AND column_name = 'version_name'
                    LIMIT 1
                    """
                )
            ).fetchone()
            has_version_name = bool(row)
        if not has_version_name:
            db.exec_driver_sql("ALTER TABLE saved_schedule_versions ADD COLUMN version_name TEXT DEFAULT ''")
        db.exec_driver_sql("UPDATE saved_schedule_versions SET version_name = '' WHERE version_name IS NULL")
        db.exec_driver_sql("UPDATE saved_schedule_versions SET version_name = 'v' || version WHERE version_name = ''")
        
        if not is_sqlite:
            db.exec_driver_sql("ALTER TABLE surgeons ALTER COLUMN nlth SET NOT NULL")
            db.exec_driver_sql("ALTER TABLE surgeons ALTER COLUMN team SET NOT NULL")
            db.exec_driver_sql("ALTER TABLE surgeons ALTER COLUMN manual_less_calls_credit SET NOT NULL")
            db.exec_driver_sql("ALTER TABLE surgeons ALTER COLUMN manual_more_calls_credit SET NOT NULL")


        # ── 2) Seed global_config defaults ──
        defaults = {
            "no_call_hard":          "1",
            "fairness_weight":       "1000",
            "fairness_fallback_policy": "auto_relax",
            "gamma_no_call":         "10",
            "gamma_unavail_prev":    "5",
            "gamma_1B":              "1",
            "gamma_balance":         "100",
            "gamma_spacing":         "10",
            "spacing_threshold":     "7",
            "gamma_weekend_balance": "50",
            "gamma_consec_weekend":  "20",
            "gamma_team_pref":       "10",
            "enable_two_pass_fairness_priority": "1",
            "enable_l2g1_primary_calls": "0",
            "enable_l2g1_primary_2a_same_day_penalty": "1",
            "gamma_l2g1_primary_2a_same_day": "30",
        }
        
        insert_gc = text("""
            INSERT INTO global_config (key, value)
            VALUES (:key, :value)
            ON CONFLICT (key) DO NOTHING
        """)
        for key, val in defaults.items():
            db.execute(insert_gc, {"key": key, "value": val})

        # ── 3) Seed max_calls_config defaults ──
        max_defaults = {"1": 10, "2": 10, "3": 10, "4": 10, "l2g1_1ab": 4}
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
    stmt = text(
        """
        INSERT INTO team_day_preferences (team, weekday, preference)
        VALUES (:team, :weekday, :preference)
        ON CONFLICT (team, weekday) DO UPDATE
        SET preference = EXCLUDED.preference
        """
    )
    # Reuse an already-open request transaction (autobegin) when present.
    if db.in_transaction():
        for team, by_wd in new_prefs.items():
            for wd, pref in by_wd.items():
                db.execute(stmt, {
                    "preference": pref,
                    "team":       team,
                    "weekday":    wd
                })
        return
    with db.begin():
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
    upsert_stmt = text(
        """
        INSERT INTO global_config (key, value)
        VALUES (:key, :value)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """
    )
    # Reuse an already-open request transaction (autobegin) when present.
    if db.in_transaction():
        for key, value in new_config.items():
            db.execute(upsert_stmt, {"key": key, "value": str(value)})
        return
    with db.begin():
        for key, value in new_config.items():
            db.execute(upsert_stmt, {"key": key, "value": str(value)})

def get_next_month_unavail_deadline(now: datetime.datetime | None = None):
    """
    Returns a datetime representing the configured deadline for submitting
    unavailability requests for the next month. If the deadline feature
    is disabled or misconfigured, returns None.
    The deadline is defined as an absolute datetime in the current month
    (the month preceding the upcoming schedule month).
    """
    cfg = get_global_config()
    if str(cfg.get("enable_unavail_deadline", "0")) == "0":
        return None
    if now is None:
        now = datetime.datetime.now()
    try:
        deadline_day = int(cfg.get("unavail_deadline_day", "20"))
    except Exception:
        deadline_day = 20
    time_str = cfg.get("unavail_deadline_time", "23:59")
    try:
        hour, minute = [int(part) for part in time_str.split(":")[:2]]
    except Exception:
        hour, minute = 23, 59
    # clamp components
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    year = now.year
    month = now.month
    # Ensure day exists within this month
    max_day = calendar.monthrange(year, month)[1]
    day = max(1, min(max_day, deadline_day))
    try:
        return datetime.datetime(year, month, day, hour, minute)
    except ValueError:
        # fallback to end of month
        return datetime.datetime(year, month, max_day, hour, minute)

def is_unavail_deadline_passed(now: datetime.datetime | None = None):
    """
    Helper returning (deadline_datetime, passed_bool). Deadline is None and passed False
    when feature disabled.
    """
    deadline_dt = get_next_month_unavail_deadline(now=now)
    if deadline_dt is None:
        return None, False
    if now is None:
        now = datetime.datetime.now()
    return deadline_dt, now >= deadline_dt

def get_all_surgeons():
    db = get_db()
    result = db.execute(text("SELECT * FROM surgeons"))
    rows = result.mappings().all()      # ← get list of dicts
    surgeons = [dict(r) for r in rows]
    for surgeon in surgeons:
        try:
            less_credit = int(surgeon.get("manual_less_calls_credit", 0) or 0)
        except Exception:
            less_credit = 0
        try:
            more_credit = int(surgeon.get("manual_more_calls_credit", 0) or 0)
        except Exception:
            more_credit = 0
        less_credit = max(0, less_credit)
        more_credit = max(0, more_credit)
        # UI semantics: negative = fewer calls, positive = more calls
        surgeon["manual_call_credit"] = more_credit - less_credit
    return surgeons


def update_surgeon_manual_call_credits(credits_by_surgeon_id: dict):
    """
    Persist per-surgeon manual call-credit preferences.
    credits_by_surgeon_id format:
      {
        12: {"less_calls_credit": 2, "more_calls_credit": 0},
        15: {"less_calls_credit": 0, "more_calls_credit": 1},
      }
    """
    db = get_db()
    stmt = text(
        """
        UPDATE surgeons
        SET manual_less_calls_credit = :less_credit,
            manual_more_calls_credit = :more_credit
        WHERE id = :sid
        """
    )
    with db.begin():
        for sid_raw, values in (credits_by_surgeon_id or {}).items():
            try:
                sid = int(sid_raw)
            except Exception:
                continue
            values = values or {}
            try:
                less_credit = int(values.get("less_calls_credit", 0))
            except Exception:
                less_credit = 0
            try:
                more_credit = int(values.get("more_calls_credit", 0))
            except Exception:
                more_credit = 0
            less_credit = max(0, less_credit)
            more_credit = max(0, more_credit)
            db.execute(
                stmt,
                {
                    "sid": sid,
                    "less_credit": less_credit,
                    "more_credit": more_credit,
                },
            )

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


BLOCKING_REQUEST_TYPES = ("unavailable", "study_leave", "no_call")


def get_g1_g4_cohorts(surgeons: list | None = None):
    """
    Return cohort memberships aligned with the New Schedule G1/G2/G3/G4 logic.
    """
    if surgeons is None:
        surgeons = get_all_surgeons()

    def has_level(surgeon_obj, level):
        return level in parse_call_levels(surgeon_obj.get("call_levels", ""))

    group1_members = sorted(
        [s for s in surgeons if has_level(s, "1A") or has_level(s, "1B")],
        key=lambda s: (s.get("name", "") or "").lower(),
    )
    if has_app_context() and str(get_global_config().get("enable_l2g1_primary_calls", "0")) == "1":
        seen_g1 = {s.get("id") for s in group1_members}
        for s in surgeons:
            if s.get("id") is None or s.get("id") in seen_g1:
                continue
            if get_level2_group(s) == 1:
                group1_members.append(s)
                seen_g1.add(s.get("id"))
        group1_members.sort(key=lambda x: (x.get("name", "") or "").lower())
    group2_members = sorted(
        [s for s in surgeons if get_level2_group(s) in (1, 2, 3)],
        key=lambda s: (s.get("name", "") or "").lower(),
    )
    group4_l2_ids = {
        int(s["id"])
        for s in surgeons
        if s.get("id") is not None and get_level2_group(s) == 4
    }
    group3_members = sorted(
        [
            s
            for s in surgeons
            if has_level(s, "3")
            or (s.get("id") is not None and int(s["id"]) in group4_l2_ids)
        ],
        key=lambda s: (s.get("name", "") or "").lower(),
    )
    group4_members = sorted(
        [s for s in surgeons if has_level(s, "4")],
        key=lambda s: (s.get("name", "") or "").lower(),
    )

    def normalize_members(members):
        normalized = []
        for surgeon in members:
            sid = surgeon.get("id")
            if sid is None:
                continue
            normalized.append(
                {
                    "id": int(sid),
                    "name": str(surgeon.get("name", "") or ""),
                }
            )
        return normalized

    return [
        {"key": "g1", "label": "G1 (1A+1B)", "members": normalize_members(group1_members)},
        {"key": "g2", "label": "G2 (2A+2B)", "members": normalize_members(group2_members)},
        {"key": "g3", "label": "G3 (3 + 2B if subgroup 4)", "members": normalize_members(group3_members)},
        {"key": "g4", "label": "G4 (4)", "members": normalize_members(group4_members)},
    ]


def build_cohort_availability_calendar(start_date: datetime.date, end_date: datetime.date, surgeons: list | None = None):
    """
    Build day-level availability by G1/G2/G3/G4 cohorts for [start_date, end_date).
    """
    if not isinstance(start_date, datetime.date) or not isinstance(end_date, datetime.date):
        raise TypeError("start_date and end_date must be date objects")
    if end_date <= start_date:
        raise ValueError("end_date must be after start_date")

    if surgeons is None:
        surgeons = get_all_surgeons()
    cohorts = get_g1_g4_cohorts(surgeons=surgeons)
    availability = get_availability_requests()

    blocked_by_sid = {}
    for surgeon in surgeons:
        sid = surgeon.get("id")
        if sid is None:
            continue
        sid = int(sid)
        blocked_dates = set()
        for req in availability.get(sid, []):
            if req.get("request_type") not in BLOCKING_REQUEST_TYPES:
                continue
            raw = req.get("date")
            req_date = raw if isinstance(raw, datetime.date) else None
            if req_date is None:
                try:
                    req_date = datetime.date.fromisoformat(str(raw))
                except Exception:
                    continue
            if start_date <= req_date < end_date:
                blocked_dates.add(req_date)
        blocked_by_sid[sid] = blocked_dates

    days_payload = {}
    cursor = start_date
    while cursor < end_date:
        day_key = cursor.isoformat()
        day_result = {}
        for cohort in cohorts:
            available_names = []
            unavailable_names = []
            for member in cohort["members"]:
                sid = member["id"]
                name = member["name"]
                if cursor in blocked_by_sid.get(sid, set()):
                    unavailable_names.append(name)
                else:
                    available_names.append(name)
            day_result[cohort["key"]] = {
                "total_count": len(cohort["members"]),
                "available_count": len(available_names),
                "unavailable_count": len(unavailable_names),
                "available_names": available_names,
                "unavailable_names": unavailable_names,
            }
        days_payload[day_key] = day_result
        cursor += datetime.timedelta(days=1)

    return {
        "cohorts": [
            {
                "key": cohort["key"],
                "label": cohort["label"],
                "total_count": len(cohort["members"]),
                "members": cohort["members"],
            }
            for cohort in cohorts
        ],
        "days": days_payload,
    }


def compute_unavailability_credit_by_surgeon(surgeons, availability, days, unavail_credit_days):
    """
    Returns {surgeon_id: credit_calls} where credit_calls is derived only from
    unavailable/study_leave days in the current solve window.
    """
    try:
        window = int(unavail_credit_days)
    except Exception:
        window = 7
    if window < 1:
        window = 7

    day_set = set(days or [])
    credits = {s['id']: 0 for s in (surgeons or []) if isinstance(s, dict) and s.get('id') is not None}
    if not day_set:
        return credits

    for sid, req_list in (availability or {}).items():
        if sid not in credits:
            continue
        count_unavail = 0
        for req in req_list or []:
            if req.get('request_type') not in ('unavailable', 'study_leave'):
                continue
            raw = req.get('date')
            if isinstance(raw, str):
                if raw in day_set:
                    count_unavail += 1
            elif isinstance(raw, datetime.date):
                if raw.isoformat() in day_set:
                    count_unavail += 1
        credits[sid] = count_unavail // window
    return credits

#############################################
# Half-year horizon helpers
#############################################

def get_published_schedule_version(year: int, month: int):
    db = get_db()
    # Try published version first
    row = db.execute(
        text(
            f"""
            SELECT schedule_data
            FROM saved_schedule_versions
            WHERE year = :y AND month = :m AND published = {sql_true()}
            ORDER BY version DESC
            LIMIT 1
            """
        ),
        {"y": year, "m": month}
    ).mappings().fetchone()
    if not row:
        row = db.execute(
            text(
                """
                SELECT schedule_data
                FROM saved_schedule_versions
                WHERE year = :y AND month = :m
                ORDER BY version DESC
                LIMIT 1
                """
            ),
            {"y": year, "m": month}
        ).mappings().fetchone()
    if not row or row.get("schedule_data") in (None, ""):
        return None
    data = row["schedule_data"]
    try:
        import json
        return data if isinstance(data, dict) else json.loads(data)
    except Exception:
        return None

def get_horizon_prior_levels_and_credit(year: int, month: int, surgeons: list):
    """
    Aggregate prior counts per level and unavailability credit over the same half‑year
    up to but excluding (year, month).
    Returns {
      'prior_levels': { '1A':{sid:cnt}, '1B':{...}, '2A':{...}, '2B':{...}, '3':{...}, '4':{...} },
      'prior_unavail_days': {sid: days},
      'prior_unavail_credit_calls': {sid: credit_calls}
    }
    """
    import calendar as _cal
    import datetime as _dt

    id_by_name = {s["name"]: s["id"] for s in surgeons}
    all_ids = [s["id"] for s in surgeons]
    levels = ["1A","1B","2A","2B","3","4"]
    prior_levels = {L: {sid: 0 for sid in all_ids} for L in levels}

    # Determine half-year window
    if 1 <= month <= 6:
        start_month, end_month_excl = 1, 7
    else:
        start_month, end_month_excl = 7, 13
    months = [m for m in range(start_month, min(month, end_month_excl))]

    # Accumulate prior level counts from published schedules
    for m in months:
        sched = get_published_schedule_version(year, m)
        if not sched:
            continue
        for day_str, assigns in (sched or {}).items():
            if not isinstance(assigns, dict):
                continue
            for L in levels:
                name = assigns.get(L)
                if not name:
                    continue
                sid = id_by_name.get(name)
                if sid is None:
                    continue
                prior_levels[L][sid] = prior_levels[L].get(sid, 0) + 1

    # Compute unavailability credit across same window (up to month-1)
    # Range: [year-start_month-01, year-(month-1)-last_day]
    if months:
        start_date = _dt.date(year, months[0], 1)
        last_m = months[-1]
        last_day = _cal.monthrange(year, last_m)[1]
        end_date = _dt.date(year, last_m, last_day)
    else:
        start_date = end_date = None

    prior_unavail_days = {sid: 0 for sid in all_ids}
    if start_date and end_date:
        db = get_db()
        rows = db.execute(
            text(
                """
                SELECT surgeon_id, date
                FROM surgeon_availability
                WHERE request_type IN ('unavailable','study_leave')
                  AND date >= :start_d AND date <= :end_d
                """
            ),
            {"start_d": start_date, "end_d": end_date}
        ).mappings().all()
        for r in rows:
            try:
                sid = int(r["surgeon_id"]) if r.get("surgeon_id") is not None else None
            except Exception:
                continue
            if sid in prior_unavail_days:
                prior_unavail_days[sid] += 1

    # Convert days to credit calls using global config
    cfg = get_global_config()
    try:
        unavail_credit_days = int(cfg.get("unavail_credit_days", "7"))
    except Exception:
        unavail_credit_days = 7
    if unavail_credit_days < 1:
        unavail_credit_days = 7
    prior_unavail_credit_calls = {
        sid: (prior_unavail_days.get(sid, 0) // unavail_credit_days)
        for sid in all_ids
    }

    return {
        "prior_levels": prior_levels,
        "prior_unavail_days": prior_unavail_days,
        "prior_unavail_credit_calls": prior_unavail_credit_calls,
    }


def get_half_year_months_before(month: int):
    """
    Return prior months in the current half-year window.
    - For Jan..Jun: Jan through month-1
    - For Jul..Dec: Jul through month-1
    """
    if month < 1 or month > 12:
        return []
    if 1 <= month <= 6:
        start_month, end_month_excl = 1, 7
    else:
        start_month, end_month_excl = 7, 13
    return [m for m in range(start_month, min(month, end_month_excl))]


def build_half_year_cohort_summary(year: int, month: int, surgeons: list | None = None):
    """
    Build Jan/Jul-to-date (prior months only) cohort counts used on New Schedule.
    Data source: published schedules only (via get_published_schedule_version).
    """
    if surgeons is None:
        surgeons = get_all_surgeons()

    levels = ["1A", "1B", "2A", "2B", "3", "4"]
    months = get_half_year_months_before(month)
    id_by_name = {s.get("name"): s.get("id") for s in surgeons if s.get("name")}
    counts_by_sid = {int(s["id"]): {L: 0 for L in levels} for s in surgeons if s.get("id") is not None}

    for m in months:
        sched = get_published_schedule_version(year, m)
        if not sched:
            continue
        for _, assigns in (sched or {}).items():
            if not isinstance(assigns, dict):
                continue
            for L in levels:
                name = assigns.get(L)
                if not name:
                    continue
                sid = id_by_name.get(name)
                if sid is None or sid not in counts_by_sid:
                    continue
                counts_by_sid[sid][L] += 1

    def has_level(surgeon_obj, level):
        return level in parse_call_levels(surgeon_obj.get("call_levels", ""))

    group1_members = sorted([s for s in surgeons if has_level(s, "1A") or has_level(s, "1B")], key=lambda s: s.get("name", ""))
    if has_app_context() and str(get_global_config().get("enable_l2g1_primary_calls", "0")) == "1":
        seen_g1 = {s.get("id") for s in group1_members}
        for s in surgeons:
            if s.get("id") is None or s.get("id") in seen_g1:
                continue
            if get_level2_group(s) == 1:
                group1_members.append(s)
                seen_g1.add(s.get("id"))
        group1_members.sort(key=lambda x: (x.get("name", "") or ""))
    group2_members = sorted([s for s in surgeons if get_level2_group(s) in (1, 2, 3)], key=lambda s: s.get("name", ""))
    group4_l2_ids = {int(s["id"]) for s in surgeons if s.get("id") is not None and get_level2_group(s) == 4}
    group3_members = sorted(
        [s for s in surgeons if has_level(s, "3") or (s.get("id") is not None and int(s["id"]) in group4_l2_ids)],
        key=lambda s: s.get("name", ""),
    )
    group4_members = sorted([s for s in surgeons if has_level(s, "4")], key=lambda s: s.get("name", ""))

    group_definitions = [
        ("g1", "G1 (1A+1B)", group1_members, lambda sid: counts_by_sid.get(sid, {}).get("1A", 0) + counts_by_sid.get(sid, {}).get("1B", 0)),
        ("g2", "G2 (2A+2B)", group2_members, lambda sid: counts_by_sid.get(sid, {}).get("2A", 0) + counts_by_sid.get(sid, {}).get("2B", 0)),
        ("g3", "G3 (3 + 2B if subgroup 4)", group3_members, lambda sid: counts_by_sid.get(sid, {}).get("3", 0) + (counts_by_sid.get(sid, {}).get("2B", 0) if sid in group4_l2_ids else 0)),
        ("g4", "G4 (4)", group4_members, lambda sid: counts_by_sid.get(sid, {}).get("4", 0)),
    ]

    groups = []
    for key, label, members, getter in group_definitions:
        rows = []
        member_counts = []
        for surgeon_obj in members:
            sid = int(surgeon_obj.get("id"))
            count = int(getter(sid))
            member_counts.append(count)
            rows.append(
                {
                    "surgeon_id": sid,
                    "name": surgeon_obj.get("name", ""),
                    "count": count,
                    "manual_call_credit": int(surgeon_obj.get("manual_call_credit", 0) or 0),
                }
            )
        avg = (sum(member_counts) / len(member_counts)) if member_counts else 0.0
        for row in rows:
            delta = row["count"] - avg
            if delta > 0:
                status = "above"
            elif delta < 0:
                status = "below"
            else:
                status = "at"
            row["delta"] = round(delta, 2)
            row["status"] = status
        groups.append(
            {
                "key": key,
                "label": label,
                "average": round(avg, 2),
                "members": rows,
            }
        )

    if 1 <= month <= 6:
        window_start_month = 1
    else:
        window_start_month = 7

    return {
        "year": year,
        "month": month,
        "window_start_month": window_start_month,
        "months_included": months,
        "groups": groups,
    }

#############################################
# Prior last-two storage
#############################################

def get_prior_last_two(year: int, month: int):
    db = get_db()
    row = db.execute(text("SELECT m2, m1 FROM prior_last_two WHERE year = :y AND month = :m"), {"y": year, "m": month}).mappings().fetchone()
    if not row:
        return {"m2": {}, "m1": {}}
    def to_dict(val):
        if isinstance(val, dict):
            return val
        if val is None:
            return {}
        try:
            return json.loads(val)
        except Exception:
            return {}
    return {"m2": to_dict(row["m2"]), "m1": to_dict(row["m1"]) }

def save_prior_last_two(year: int, month: int, m2: dict, m1: dict):
    db = get_db()
    with db.begin():
        stmt = text(
            f"""
            INSERT INTO prior_last_two (year, month, m2, m1, updated_at)
            VALUES (:y, :m, {json_cast('m2')}, {json_cast('m1')}, {sql_now()})
            ON CONFLICT (year, month) DO UPDATE
            SET m2 = EXCLUDED.m2, m1 = EXCLUDED.m1, updated_at = {sql_now()}
            """
        )
        db.execute(stmt, {"y": year, "m": month, "m2": json.dumps(m2 or {}), "m1": json.dumps(m1 or {})})