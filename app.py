import random
import math
import datetime
import calendar
import sqlite3
import json

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, g, session
from ortools.sat.python import cp_model

app = Flask(__name__)
app.secret_key = "your_secure_key"  # Replace with your secure key

#############################################
# Helper Functions
#############################################

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
    Determines a surgeon’s level-2 group based on their call_levels string.
    Conventions:
      - If the string includes "2A" but NOT "2B", return 1 (Group 1: Needs supervision).
      - If the string includes both "2A" and "2B", return 2 (Group 2: Does not need supervision).
      - If the string includes "2B" but NOT "2A", return 3 (Group 3: Supervisors only).
      - Otherwise, returns None.
    """
    cl = parse_call_levels(surgeon.get("call_levels", ""))
    has_2A = "2A" in cl
    has_2B = "2B" in cl
    if has_2A and not has_2B:
        return 1
    elif has_2A and has_2B:
        return 2
    elif has_2B and not has_2A:
        return 3
    else:
        return None

def get_year_month():
    """
    Reads 'year' and 'month' from query parameters.
    Defaults to today's year and month if not provided.
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

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
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

        # Insert default configuration if not present.
        default_config = {"1": 10, "2": 10, "3": 10, "4": 10}
        for group, max_val in default_config.items():
            cursor.execute("INSERT OR IGNORE INTO max_calls_config (level_group, max_calls) VALUES (?, ?)", (group, max_val))
        # Insert default global config for no_call (1 = hard, 0 = soft)
        cursor.execute("INSERT OR IGNORE INTO global_config (key, value) VALUES ('no_call_hard', '1')")
        db.commit()

init_db()

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

#############################################
# Scheduling: Month, Days, Global Variables
#############################################
# Days are generated dynamically in /new_schedule based on the selected year and month.

#############################################
# Surgeon Management Endpoints
#############################################

@app.route('/surgeons')
def list_surgeons():
    surgeons = get_all_surgeons()
    if request.args.get('sort'):
        level_order = {"1A": 1, "1B": 1, "2A": 2, "2B": 2, "3": 3, "4": 4}
        def get_lowest_level(s):
            levels = parse_call_levels(s.get("call_levels", ""))
            if not levels:
                return 99
            orders = [level_order.get(l, 99) for l in levels]
            return min(orders)
        surgeons.sort(key=lambda s: (get_lowest_level(s), s["name"].lower()))
    return render_template('surgeons_list.html', surgeons=surgeons)

@app.route('/surgeons/add', methods=['GET', 'POST'])
def add_surgeon():
    if request.method == 'POST':
        name = request.form['name']
        call_levels_list = request.form.getlist('call_levels')
        call_levels = ','.join(call_levels_list)
        db = get_db()
        db.execute("INSERT INTO surgeons (name, call_levels) VALUES (?, ?)", (name, call_levels))
        db.commit()
        flash("Surgeon added successfully!")
        return redirect(url_for('list_surgeons'))
    return render_template('surgeon_form.html', surgeon={}, action="Add")

@app.route('/surgeons/edit/<int:surgeon_id>', methods=['GET', 'POST'])
def edit_surgeon(surgeon_id):
    db = get_db()
    row = db.execute("SELECT * FROM surgeons WHERE id = ?", (surgeon_id,)).fetchone()
    if not row:
        flash("Surgeon not found!")
        return redirect(url_for('list_surgeons'))
    surgeon = dict(row)
    if request.method == 'POST':
        name = request.form['name']
        call_levels_list = request.form.getlist('call_levels')
        call_levels = ','.join(call_levels_list)
        db.execute("UPDATE surgeons SET name = ?, call_levels = ? WHERE id = ?", (name, call_levels, surgeon_id))
        db.commit()
        flash("Surgeon updated successfully!")
        return redirect(url_for('list_surgeons'))
    return render_template('surgeon_form.html', surgeon=surgeon, action="Edit")

@app.route('/surgeons/update/<int:surgeon_id>', methods=['POST'])
def update_surgeon_inline(surgeon_id):
    name = request.form['name']
    call_levels_list = request.form.getlist('call_levels')
    call_levels = ','.join(call_levels_list)
    db = get_db()
    db.execute("UPDATE surgeons SET name = ?, call_levels = ? WHERE id = ?", (name, call_levels, surgeon_id))
    db.commit()
    flash("Surgeon updated successfully!")
    return redirect(url_for('list_surgeons'))

@app.route('/update_all_surgeons', methods=['POST'])
def update_all_surgeons():
    db = get_db()
    surgeons = get_all_surgeons()
    for surgeon in surgeons:
        sid = surgeon['id']
        name_field = f"name_{sid}"
        call_levels_field = f"call_levels_{sid}"
        new_name = request.form.get(name_field)
        new_levels = request.form.getlist(call_levels_field)
        new_levels_str = ",".join(new_levels)
        db.execute("UPDATE surgeons SET name = ?, call_levels = ? WHERE id = ?", (new_name, new_levels_str, sid))
    db.commit()
    flash("Surgeon updates applied successfully!")
    return redirect(url_for('list_surgeons'))

#############################################
# Maximum Calls Configuration Endpoint
#############################################

@app.route('/config_max_calls', methods=['GET', 'POST'])
def config_max_calls():
    if request.method == 'POST':
        new_config = {
            "1": int(request.form.get("group_1", 10)),
            "2": int(request.form.get("group_2", 10)),
            "3": int(request.form.get("group_3", 10)),
            "4": int(request.form.get("group_4", 10))
        }
        update_max_calls_config(new_config)
        flash("Maximum calls configuration updated successfully!")
        return redirect(url_for('config_max_calls'))
    config = get_max_calls_config()
    return render_template('config_max_calls.html', config=config)

#############################################
# Global Config for No Call Request Constraint Endpoint
#############################################

@app.route('/global_config', methods=['GET', 'POST'])
def global_config_page():
    if request.method == 'POST':
        no_call_hard_val = request.form.get("no_call_hard", "1")
        update_global_config({
            "no_call_hard": no_call_hard_val,
            "fairness_weight": request.form.get("fairness_weight", "1000"),
            "gamma_no_call": request.form.get("gamma_no_call", "10"),
            "gamma_unavail_prev": request.form.get("gamma_unavail_prev", "5"),
            "gamma_1B": request.form.get("gamma_1B", "1")
        })
        flash("Global configuration updated successfully!")
        return redirect(url_for('global_config_page'))
    else:
        config = get_global_config()
        return render_template('global_config.html', config=config)



#############################################
# OR‑Tools Scheduling Function (with Availability Constraints)
#############################################

def solve_schedule_or_tools(days, surgeons):
    from ortools.sat.python import cp_model
    import datetime
    model = cp_model.CpModel()
    num_days = len(days)

    # Load global configuration weights.
    global_config = get_global_config()
    fairness_weight = int(global_config.get("fairness_weight", "1000"))
    gamma_no_call = int(global_config.get("gamma_no_call", "10"))
    gamma_unavail_prev = int(global_config.get("gamma_unavail_prev", "5"))
    gamma_1B = int(global_config.get("gamma_1B", "1"))
    gamma_balance = int(global_config.get("gamma_balance", "100"))
    no_call_hard = global_config.get("no_call_hard", "1") == "1"
    
    # Get maximum calls configuration.
    max_config = get_max_calls_config()  # e.g., {"1":10, "2":10, "3":10, "4":10}
    
    # Use actual surgeon IDs from the database.
    id_to_surgeon = {s["id"]: s for s in surgeons}
    all_surgeon_ids = [s["id"] for s in surgeons]
    
    # --- Build Domains (using actual IDs) ---
    domain_1A = [s["id"] for s in surgeons if "1A" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_1A:
        domain_1A = [-1]
    domain_1B = [s["id"] for s in surgeons if "1B" in parse_call_levels(s.get("call_levels", ""))]
    domain_1B = (domain_1B + [-1]) if domain_1B else [-1]
    domain_2A = [s["id"] for s in surgeons if ("2A" in parse_call_levels(s.get("call_levels", "")) or "2B" in parse_call_levels(s.get("call_levels", "")))]
    if not domain_2A:
        domain_2A = [-1]
    domain_2B = [s["id"] for s in surgeons if "2B" in parse_call_levels(s.get("call_levels", ""))]
    domain_2B = (domain_2B + [-1]) if domain_2B else [-1]
    domain_3 = [s["id"] for s in surgeons if "3" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_3:
        domain_3 = [-1]
    domain_4 = [s["id"] for s in surgeons if "4" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_4:
        domain_4 = [-1]
    
    all_levels = ["1A", "1B", "2A", "2B", "3", "4"]
    
    # --- Decision Variables ---
    X = {}
    for d in range(num_days):
        X[(d, "1A")] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(domain_1A), f'X_{d}_1A')
        X[(d, "1B")] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(domain_1B), f'X_{d}_1B')
        X[(d, "2A")] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(domain_2A), f'X_{d}_2A')
        X[(d, "2B")] = model.NewIntVarFromDomain(cp_model.Domain.FromValues(domain_2B), f'X_{d}_2B')
        X[(d, "3")]  = model.NewIntVarFromDomain(cp_model.Domain.FromValues(domain_3),  f'X_{d}_3')
        X[(d, "4")]  = model.NewIntVarFromDomain(cp_model.Domain.FromValues(domain_4),  f'X_{d}_4')
    
    # --- Constraint Set 1: Within-Day Uniqueness for Forced Slots (levels 1A, 2A, 3, 4) ---
    for d in range(num_days):
        forced_vars = []
        for level, dom in zip(["1A", "2A", "3", "4"], [domain_1A, domain_2A, domain_3, domain_4]):
            if dom != [-1]:
                forced_vars.append(X[(d, level)])
        if len(forced_vars) > 1:
            model.AddAllDifferent(forced_vars)
    
    # --- Constraint Set 2: 3-Day Gap ---
    for d in range(num_days):
        for d2 in range(d + 1, min(num_days, d + 3)):
            for lev1 in all_levels:
                for lev2 in all_levels:
                    b1 = model.NewBoolVar(f'nonempty_{d}_{lev1}')
                    b2 = model.NewBoolVar(f'nonempty_{d2}_{lev2}')
                    model.Add(X[(d, lev1)] != -1).OnlyEnforceIf(b1)
                    model.Add(X[(d, lev1)] == -1).OnlyEnforceIf(b1.Not())
                    model.Add(X[(d2, lev2)] != -1).OnlyEnforceIf(b2)
                    model.Add(X[(d2, lev2)] == -1).OnlyEnforceIf(b2.Not())
                    model.Add(X[(d, lev1)] != X[(d2, lev2)]).OnlyEnforceIf([b1, b2])
    
    # --- New Constraint: Level 1 Pairing (1A and 1B must differ if 1B is assigned) ---
    for d in range(num_days):
        b1B = model.NewBoolVar(f'nonempty_1B_day_{d}')
        model.Add(X[(d, "1B")] != -1).OnlyEnforceIf(b1B)
        model.Add(X[(d, "1B")] == -1).OnlyEnforceIf(b1B.Not())
        model.Add(X[(d, "1A")] != X[(d, "1B")]).OnlyEnforceIf(b1B)
    
    # --- Revised Level 2 Constraints ---
    for d in range(num_days):
        for s in domain_2A:
            b = model.NewBoolVar(f'level2A_{d}_{s}')
            model.Add(X[(d, "2A")] == s).OnlyEnforceIf(b)
            model.Add(X[(d, "2A")] != s).OnlyEnforceIf(b.Not())
            group = get_level2_group(id_to_surgeon[s])
            if group == 1:
                model.Add(X[(d, "2B")] != -1).OnlyEnforceIf(b)
                model.Add(X[(d, "2B")] != s).OnlyEnforceIf(b)
            else:
                model.Add(X[(d, "2B")] == -1).OnlyEnforceIf(b)
    
    # --- Constraint Set 3: Maximum Calls per Group ---
    indicators = {}
    for d in range(num_days):
        for level in all_levels:
            for s in all_surgeon_ids:
                indicators[(d, level, s)] = model.NewBoolVar(f'ind_{d}_{level}_{s}')
                model.Add(X[(d, level)] == s).OnlyEnforceIf(indicators[(d, level, s)])
                model.Add(X[(d, level)] != s).OnlyEnforceIf(indicators[(d, level, s)].Not())
    
    call_count_overall = {}
    for s in all_surgeon_ids:
        call_count_overall[s] = model.NewIntVar(0, num_days * len(all_levels), f'count_all_{s}')
        model.Add(call_count_overall[s] == sum(indicators[(d, level, s)] for d in range(num_days) for level in all_levels))
    
    max_all = model.NewIntVar(0, num_days * len(all_levels), 'max_all')
    min_all = model.NewIntVar(0, num_days * len(all_levels), 'min_all')
    model.AddMaxEquality(max_all, [call_count_overall[s] for s in all_surgeon_ids])
    model.AddMinEquality(min_all, [call_count_overall[s] for s in all_surgeon_ids])
    
    diff_all = model.NewIntVar(0, num_days * len(all_levels), 'diff_all')
    model.Add(diff_all == max_all - min_all)
    
    # --- New Constraint: Availability / No Call Hard Constraints ---
    availability = get_availability_requests()
    for i, day in enumerate(days):
        current_day = datetime.datetime.strptime(day, "%Y-%m-%d").date()
        for s_id, req_list in availability.items():
            for req in req_list:
                try:
                    req_date = datetime.datetime.strptime(req["date"], "%Y-%m-%d").date()
                except Exception:
                    continue
                if req_date == current_day:
                    if req["request_type"] == "unavailable":
                        model.Add(sum(indicators[(i, lev, s_id)] for lev in all_levels) == 0)
                    elif req["request_type"] == "no_call" and no_call_hard:
                        model.Add(sum(indicators[(i, lev, s_id)] for lev in all_levels) == 0)
    
    # --- Soft Penalties for Availability ---
    soft_penalties_unavail_prev = []
    for i in range(num_days - 1):
        next_day = datetime.datetime.strptime(days[i+1], "%Y-%m-%d").date()
        for s_id, req_list in get_availability_requests().items():
            for req in req_list:
                try:
                    req_date = datetime.datetime.strptime(req["date"], "%Y-%m-%d").date()
                except Exception:
                    continue
                if req_date == next_day and req["request_type"] == "unavailable":
                    for lev in all_levels:
                        b = model.NewBoolVar(f'penalty_unavailprev_{i}_{lev}_{s_id}')
                        model.Add(X[(i, lev)] == s_id).OnlyEnforceIf(b)
                        model.Add(X[(i, lev)] != s_id).OnlyEnforceIf(b.Not())
                        soft_penalties_unavail_prev.append(b)
    
    soft_penalties_nocall = []
    if not no_call_hard:
        for i, day in enumerate(days):
            for s_id, req_list in get_availability_requests().items():
                for req in req_list:
                    try:
                        req_date = datetime.datetime.strptime(req["date"], "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if req_date == datetime.datetime.strptime(day, "%Y-%m-%d").date() and req["request_type"] == "no_call":
                        for lev in all_levels:
                            b = model.NewBoolVar(f'penalty_nocall_{i}_{lev}_{s_id}')
                            model.Add(X[(i, lev)] == s_id).OnlyEnforceIf(b)
                            model.Add(X[(i, lev)] != s_id).OnlyEnforceIf(b.Not())
                            soft_penalties_nocall.append(b)
    
    penalty_unavail_prev = model.NewIntVar(0, num_days * len(all_levels) * 10, 'penalty_unavail_prev')
    if soft_penalties_unavail_prev:
        model.Add(penalty_unavail_prev == sum(soft_penalties_unavail_prev))
    else:
        model.Add(penalty_unavail_prev == 0)
    
    penalty_nocall = model.NewIntVar(0, num_days * len(all_levels) * 10, 'penalty_nocall')
    if soft_penalties_nocall:
        model.Add(penalty_nocall == sum(soft_penalties_nocall))
    else:
        model.Add(penalty_nocall == 0)
    
    # --- Incentive for Optional Level 1B ---
    indicator_1B = {}
    for d in range(num_days):
        indicator_1B[d] = model.NewBoolVar(f'nonempty_1B_{d}')
        model.Add(X[(d, "1B")] != -1).OnlyEnforceIf(indicator_1B[d])
        model.Add(X[(d, "1B")] == -1).OnlyEnforceIf(indicator_1B[d].Not())
    total_1B = model.NewIntVar(0, num_days, 'total_1B')
    model.Add(total_1B == sum(indicator_1B[d] for d in range(num_days)))
    
    # --- Additional Fairness: Penalize Deviation from Average Calls ---
    # Let T be the total calls and N be the number of surgeons.
    N = len(all_surgeon_ids)
    T = model.NewIntVar(0, num_days * len(all_levels) * N, "T")
    model.Add(T == sum(call_count_overall[s] for s in all_surgeon_ids))
    deviations = {}
    for s in all_surgeon_ids:
        # diff = (call_count_overall[s]*N - T)
        diff = model.NewIntVar(-num_days * len(all_levels) * N, num_days * len(all_levels) * N, f"diff_{s}")
        model.Add(diff == call_count_overall[s] * N - T)
        deviations[s] = model.NewIntVar(0, num_days * len(all_levels) * N, f"dev_{s}")
        model.Add(deviations[s] >= diff)
        model.Add(deviations[s] >= -diff)
    deviation_sum = model.NewIntVar(0, num_days * len(all_levels) * N, "deviation_sum")
    model.Add(deviation_sum == sum(deviations[s] for s in all_surgeon_ids))
    
    # --- Final Objective ---
    model.Minimize(
        fairness_weight * diff_all 
        - gamma_1B * total_1B 
        + gamma_no_call * penalty_nocall 
        + gamma_unavail_prev * penalty_unavail_prev
        + gamma_balance * deviation_sum
    )
    
    # --- Solve the Model ---
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        solution = {}
        for d in range(num_days):
            day_solution = {}
            for level in all_levels:
                s_id = solver.Value(X[(d, level)])
                day_solution[level] = id_to_surgeon[s_id]["name"] if s_id != -1 else None
            solution[days[d]] = day_solution
        return solution, solver.ObjectiveValue()
    else:
        print("No solution found")
        return None, None
          
#############################################
# Constraint weights Endpoints
#############################################

@app.route('/constraint_weights', methods=['GET', 'POST'])
def constraint_weights():
    if request.method == 'POST':
        # Retrieve new weight values from the form.
        fairness_weight = request.form.get('fairness_weight', '1000')
        gamma_no_call = request.form.get('gamma_no_call', '10')
        gamma_unavail_prev = request.form.get('gamma_unavail_prev', '5')
        gamma_1B = request.form.get('gamma_1B', '1')
        # Update global configuration.
        update_global_config({
            "fairness_weight": fairness_weight,
            "gamma_no_call": gamma_no_call,
            "gamma_unavail_prev": gamma_unavail_prev,
            "gamma_1B": gamma_1B
        })
        flash("Constraint weights updated successfully!")
        return redirect(url_for('constraint_weights'))
    else:
        config = get_global_config()
        return render_template('constraint_weights.html', config=config)


#############################################
# Schedule Generation and Saving Endpoints
#############################################

@app.route('/new_schedule', methods=['GET'])
def new_schedule():
    year_sel, month_sel = get_year_month()
    days_sel = [datetime.date(year_sel, month_sel, d).isoformat() 
                for d in range(1, calendar.monthrange(year_sel, month_sel)[1] + 1)]
    global num_days
    num_days = len(days_sel)
    
    sched, cost = solve_schedule_or_tools(days_sel, get_all_surgeons())
    if sched is None:
        flash("No feasible schedule was found. Check configuration and surgeon eligibility.")
        return render_template('no_schedule.html')
    session['last_generated_schedule'] = json.dumps(sched)
    session['last_generated_cost'] = cost
    session['generated_year'] = year_sel
    session['generated_month'] = month_sel
    weekend_set = {d for d in days_sel if datetime.date.fromisoformat(d).weekday() >= 5}
    return render_template('new_schedule.html', schedule=sched, cost=cost,
                           weekend_set=weekend_set, year=year_sel, month=month_sel)

@app.route('/save_schedule', methods=['POST'])
def save_schedule():
    generated = session.get('last_generated_schedule')
    cost = session.get('last_generated_cost')
    year_sel = session.get('generated_year')
    month_sel = session.get('generated_month')
    if not generated or year_sel is None or month_sel is None:
        flash("No generated schedule to save.")
        return redirect(url_for('new_schedule'))
    db = get_db()
    row = db.execute("SELECT * FROM saved_schedule WHERE year = ? AND month = ?", (year_sel, month_sel)).fetchone()
    if row:
        db.execute("UPDATE saved_schedule SET schedule_data = ?, date_saved = datetime('now') WHERE year = ? AND month = ?",
                   (generated, year_sel, month_sel))
    else:
        db.execute("INSERT INTO saved_schedule (year, month, schedule_data, date_saved) VALUES (?, ?, ?, datetime('now'))",
                   (year_sel, month_sel, generated))
    db.commit()
    flash("Schedule saved successfully.")
    return redirect(url_for('saved_schedule', year=year_sel, month=month_sel))

@app.route('/saved_schedule', methods=['GET'])
def saved_schedule():
    year_sel, month_sel = get_year_month()
    # Generate the days for the selected period.
    days_sel = [datetime.date(year_sel, month_sel, d).isoformat() 
                for d in range(1, calendar.monthrange(year_sel, month_sel)[1] + 1)]
    
    db = get_db()
    row = db.execute("SELECT * FROM saved_schedule WHERE year = ? AND month = ?", 
                     (year_sel, month_sel)).fetchone()
    if row:
        sched = json.loads(row['schedule_data'])
    else:
        flash("No saved schedule found for the selected period.")
        sched = {}
    # Use the locally computed days_sel to determine weekend_set.
    weekend_set = {d for d in days_sel if datetime.date.fromisoformat(d).weekday() >= 5}
    return render_template('saved_schedule.html', schedule=sched, weekend_set=weekend_set,
                           year=year_sel, month=month_sel)

@app.route('/')
def index():
    year_sel, month_sel = get_year_month()
    db = get_db()
    row = db.execute("SELECT * FROM saved_schedule WHERE year = ? AND month = ?", (year_sel, month_sel)).fetchone()
    if row:
        return redirect(url_for('saved_schedule', year=year_sel, month=month_sel))
    else:
        return redirect(url_for('new_schedule', year=year_sel, month=month_sel))

#############################################
# Availability / Unavailability Endpoint
#############################################

@app.route('/availability', methods=['GET', 'POST'])
def availability():
    db = get_db()
    if request.method == 'POST':
        # Process submission of a new request over a date range.
        surgeon_id = request.form.get('surgeon_id')
        request_type = request.form.get('request_type')  # "unavailable" or "no_call"
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        if not surgeon_id or not start_date or not end_date or not request_type:
            flash("Please fill in all fields.")
        else:
            try:
                start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
                end_dt = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
                if start_dt > end_dt:
                    flash("Start date must be before or equal to end date.")
                    return redirect(url_for('availability', surgeon_id=surgeon_id))
                current_dt = start_dt
                while current_dt <= end_dt:
                    db.execute("INSERT INTO surgeon_availability (surgeon_id, request_type, date) VALUES (?, ?, ?)",
                               (surgeon_id, request_type, current_dt.isoformat()))
                    current_dt += datetime.timedelta(days=1)
                db.commit()
                flash("Request submitted successfully!")
            except Exception as e:
                flash(f"Error processing the dates: {str(e)}")
        return redirect(url_for('availability', surgeon_id=surgeon_id))
    else:
        # GET: Display the page with existing requests grouped by date range.
        surgeon_id = request.args.get('surgeon_id')
        try:
            surgeon_id = int(surgeon_id) if surgeon_id else None
        except ValueError:
            surgeon_id = None

        events = {}
        surgeon_name = ""
        if surgeon_id:
            rows = db.execute(
                "SELECT sa.date, sa.request_type, s.name, s.call_levels FROM surgeon_availability sa JOIN surgeons s ON sa.surgeon_id = s.id WHERE s.id = ? ORDER BY sa.date",
                (surgeon_id,)
            ).fetchall()
            if rows:
                surgeon_name = rows[0]["name"]
            # Group records by request_type.
            grouped_by_type = {}
            for row in rows:
                rtype = row["request_type"]
                if rtype not in grouped_by_type:
                    grouped_by_type[rtype] = []
                grouped_by_type[rtype].append(row["date"])
            # Group the dates in each request type into ranges.
            for rtype, date_list in grouped_by_type.items():
                events[rtype] = group_dates(date_list)
        # Fetch all surgeons for drop-down
        surgeons = get_all_surgeons()
        return render_template('availability.html', events=events, surgeons=surgeons, selected_surgeon_id=surgeon_id, surgeon_name=surgeon_name)

#############################################
# Delete Availability Request Endpoint
#############################################

@app.route('/delete_availability', methods=['POST'])
def delete_availability():
    surgeon_id = request.form.get('surgeon_id')
    request_type = request.form.get('request_type')
    start_date = request.form.get('start_date')
    end_date = request.form.get('end_date')
    if not surgeon_id or not request_type or not start_date or not end_date:
        flash("Missing parameters for deletion.")
        return redirect(url_for('availability', surgeon_id=surgeon_id))
    db = get_db()
    db.execute("DELETE FROM surgeon_availability WHERE surgeon_id = ? AND request_type = ? AND date BETWEEN ? AND ?",
               (surgeon_id, request_type, start_date, end_date))
    db.commit()
    flash("Request deleted successfully.")
    return redirect(url_for('availability', surgeon_id=surgeon_id))

#############################################
# Stats endpoint
#############################################

@app.route('/stats', methods=['GET'])
def stats():
    import datetime
    # Get the start and end period from query parameters.
    try:
        start_year = int(request.args.get('start_year', datetime.date.today().year))
        start_month = int(request.args.get('start_month', datetime.date.today().month))
        end_year = int(request.args.get('end_year', datetime.date.today().year))
        end_month = int(request.args.get('end_month', datetime.date.today().month))
    except ValueError:
        flash("Invalid date range provided.")
        return redirect(url_for('stats'))
    
    # We assume that each saved_schedule record has integer columns "year" and "month".
    # Query the saved_schedule table for records in the selected range.
    db = get_db()
    # One simple way: only include records where (year, month) is within the selected range.
    # Here we compare first by year, then by month.
    records = db.execute(
        """
        SELECT * FROM saved_schedule 
        WHERE 
          (year > ? OR (year = ? AND month >= ?))
          AND
          (year < ? OR (year = ? AND month <= ?))
        ORDER BY year, month
        """, 
        (start_year, start_year, start_month, end_year, end_year, end_month)
    ).fetchall()
    
    # Define mapping for call level ranking.
    level_ranks = {
        "1A": 1,
        "1B": 1,
        "2A": 2,
        "2B": 3,
        "3": 4,
        "4": 5
    }
    
    # Initialize dictionary to aggregate stats for each surgeon.
    # We use surgeon names as keys.
    stats_dict = {}
    
    # Process each saved schedule record.
    for record in records:
        schedule_data = json.loads(record["schedule_data"])
        # schedule_data is assumed to be a dictionary mapping day string to assignments.
        for day, assignments in schedule_data.items():
            try:
                day_obj = datetime.date.fromisoformat(day)
            except Exception:
                continue
            # Determine if the day is a weekend.
            is_weekend = day_obj.weekday() >= 5
            for level, surgeon in assignments.items():
                if surgeon and surgeon.strip() != "":
                    if surgeon not in stats_dict:
                        stats_dict[surgeon] = {
                            "total_calls": 0,
                            "weekend_calls": 0,
                            "min_level_rank": None
                        }
                    stats_dict[surgeon]["total_calls"] += 1
                    if is_weekend:
                        stats_dict[surgeon]["weekend_calls"] += 1
                    # Determine rank for this level.
                    rank = level_ranks.get(level, 99)
                    if stats_dict[surgeon]["min_level_rank"] is None or rank < stats_dict[surgeon]["min_level_rank"]:
                        stats_dict[surgeon]["min_level_rank"] = rank
    
    # Convert dictionary to list and sort by min_level_rank (lowest first), then by surgeon name.
    stats_list = []
    for surgeon, data in stats_dict.items():
        stats_list.append({
            "surgeon": surgeon,
            "total_calls": data["total_calls"],
            "weekend_calls": data["weekend_calls"],
            "min_level_rank": data["min_level_rank"]
        })
    stats_list.sort(key=lambda x: (x["min_level_rank"], x["surgeon"].lower()))
    
    return render_template('stats.html', stats=stats_list,
                           start_year=start_year, start_month=start_month,
                           end_year=end_year, end_month=end_month)

#############################################
# Run the App
#############################################
if __name__ == '__main__':
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    
if __name__ == '__main__':
    app.run(debug=True)

