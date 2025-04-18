import random
import math
import datetime
import calendar
import sqlite3
import json

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, g, session
from ortools.sat.python import cp_model

app = Flask(__name__)
app.secret_key = "your_secret_key"  # Replace with your secure key

#############################################
# Helper Functions
#############################################

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
        # Create table for saved schedules (now including year and month).
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
        # Create a new table for unavailability / no call requests.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS surgeon_availability (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                surgeon_id INTEGER,
                request_type TEXT,   -- "unavailable" or "no_call"
                date TEXT,
                FOREIGN KEY(surgeon_id) REFERENCES surgeons(id)
            )
        ''')
        # Insert default configuration if not present.
        default_config = {"1": 10, "2": 10, "3": 10, "4": 10}
        for group, max_val in default_config.items():
            cursor.execute("INSERT OR IGNORE INTO max_calls_config (level_group, max_calls) VALUES (?, ?)",
                           (group, max_val))
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

#############################################
# Month, Days, and Global Variables for Scheduling
#############################################
# (Days will be generated dynamically in the /new_schedule route based on the selected year and month.)

#############################################
# Surgeon Management Endpoints
#############################################

@app.route('/surgeons')
def list_surgeons():
    surgeons = get_all_surgeons()
    if request.args.get('sort'):
        # Define a custom ordering for call levels.
        level_order = {"1A": 1, "1B": 1, "2A": 2, "2B": 2, "3": 3, "4": 4}
        def get_lowest_level(s):
            levels = parse_call_levels(s.get("call_levels", ""))
            # If no levels are defined, return a high order.
            if not levels:
                return 99
            # Map each level to its order and return the minimum.
            orders = [level_order.get(l, 99) for l in levels]
            return min(orders)
        # Sort by the lowest level and then by name.
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
    # Retrieve all surgeons so we know which ones to update.
    surgeons = get_all_surgeons()
    for surgeon in surgeons:
        sid = surgeon['id']
        name_field = f"name_{sid}"
        # Note: For checkboxes, use getlist so that multiple selections are captured.
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
# OR‑Tools Scheduling Function (with Revised Level 2)
#############################################

def solve_schedule_or_tools(days, surgeons):
    from ortools.sat.python import cp_model
    model = cp_model.CpModel()
    num_days = len(days)

    # Get maximum calls configuration.
    max_config = get_max_calls_config()  # e.g., {"1":10, "2":10, "3":10, "4":10}

    # Map surgeon names to unique IDs.
    surgeon_ids = {}
    id_to_surgeon = {}
    for idx, s in enumerate(surgeons):
        surgeon_ids[s["name"]] = idx
        s["id"] = idx
        id_to_surgeon[idx] = s

    # --- Build Domains ---
    domain_1A = [s["id"] for s in surgeons if "1A" in parse_call_levels(s.get("call_levels", ""))]
    if not domain_1A:
        domain_1A = [-1]
    domain_1B = [s["id"] for s in surgeons if "1B" in parse_call_levels(s.get("call_levels", ""))]
    domain_1B = domain_1B + [-1] if domain_1B else [-1]
    # For level 2A, include any surgeon with "2A" or "2B" (this now allows Group 3 to be called in 2A).
    domain_2A = [s["id"] for s in surgeons if ("2A" in parse_call_levels(s.get("call_levels", "")) or "2B" in parse_call_levels(s.get("call_levels", "")))]
    if not domain_2A:
        domain_2A = [-1]
    # For level 2B, only include surgeons with "2B".
    domain_2B = [s["id"] for s in surgeons if "2B" in parse_call_levels(s.get("call_levels", ""))]
    domain_2B = domain_2B + [-1] if domain_2B else [-1]
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

    # --- Constraint Set 1: Within-Day Uniqueness for Forced Slots ---
    for d in range(num_days):
        forced_vars = []
        for level, dom in zip(["1A", "2A", "3", "4"], [domain_1A, domain_2A, domain_3, domain_4]):
            if dom != [-1]:
                forced_vars.append(X[(d, level)])
        if len(forced_vars) > 1:
            model.AddAllDifferent(forced_vars)

    # --- Constraint Set 2: 3-Day Gap Constraint ---
    for d in range(num_days):
        for d2 in range(d+1, min(num_days, d+3)):
            for lev1 in all_levels:
                for lev2 in all_levels:
                    b1 = model.NewBoolVar(f'nonempty_{d}_{lev1}')
                    b2 = model.NewBoolVar(f'nonempty_{d2}_{lev2}')
                    model.Add(X[(d, lev1)] != -1).OnlyEnforceIf(b1)
                    model.Add(X[(d, lev1)] == -1).OnlyEnforceIf(b1.Not())
                    model.Add(X[(d2, lev2)] != -1).OnlyEnforceIf(b2)
                    model.Add(X[(d2, lev2)] == -1).OnlyEnforceIf(b2.Not())
                    model.Add(X[(d, lev1)] != X[(d2, lev2)]).OnlyEnforceIf([b1, b2])
                    
    # --- New Constraint: 1A and 1B Must Differ (if 1B is assigned) ---
    for d in range(num_days):
        b1B = model.NewBoolVar(f'nonempty_1B_day_{d}')
        model.Add(X[(d, "1B")] != -1).OnlyEnforceIf(b1B)
        model.Add(X[(d, "1B")] == -1).OnlyEnforceIf(b1B.Not())
        model.Add(X[(d, "1A")] != X[(d, "1B")]).OnlyEnforceIf(b1B)
                    
    # --- Revised Level 2 Constraints ---
    # For each day d and for each candidate s in domain_2A, enforce:
    #   * Determine group using get_level2_group.
    #   * If group == 1 (has "2A" only), then if X[(d, "2A")] == s, enforce that X[(d, "2B")] is non-empty and ≠ s.
    #   * Otherwise (if group is 2 or 3), enforce that X[(d, "2B")] == -1.
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
    num_surgeons = len(surgeons)
    indicators = {}
    for d in range(num_days):
        for level in all_levels:
            for s in range(num_surgeons):
                indicators[(d, level, s)] = model.NewBoolVar(f'ind_{d}_{level}_{s}')
                model.Add(X[(d, level)] == s).OnlyEnforceIf(indicators[(d, level, s)])
                model.Add(X[(d, level)] != s).OnlyEnforceIf(indicators[(d, level, s)].Not())
    call_count_group = {"1": {}, "2": {}, "3": {}, "4": {}}
    for s in range(num_surgeons):
        call_count_group["1"][s] = model.NewIntVar(0, num_days*2, f'count1_{s}')
        model.Add(call_count_group["1"][s] == sum(indicators[(d, "1A", s)] + indicators[(d, "1B", s)] for d in range(num_days)))
        call_count_group["2"][s] = model.NewIntVar(0, num_days*2, f'count2_{s}')
        model.Add(call_count_group["2"][s] == sum(indicators[(d, "2A", s)] + indicators[(d, "2B", s)] for d in range(num_days)))
        call_count_group["3"][s] = model.NewIntVar(0, num_days, f'count3_{s}')
        model.Add(call_count_group["3"][s] == sum(indicators[(d, "3", s)] for d in range(num_days)))
        call_count_group["4"][s] = model.NewIntVar(0, num_days, f'count4_{s}')
        model.Add(call_count_group["4"][s] == sum(indicators[(d, "4", s)] for d in range(num_days)))
        if domain_1A != [-1] or domain_1B != [-1]:
            model.Add(call_count_group["1"][s] <= max_config.get("1", 10))
        if domain_2A != [-1] or domain_2B != [-1]:
            model.Add(call_count_group["2"][s] <= max_config.get("2", 10))
        if domain_3 != [-1]:
            model.Add(call_count_group["3"][s] <= max_config.get("3", 10))
        if domain_4 != [-1]:
            model.Add(call_count_group["4"][s] <= max_config.get("4", 10))
            
    # --- Overall Fairness ---
    call_count_overall = {}
    for s in range(num_surgeons):
        call_count_overall[s] = model.NewIntVar(0, num_days*len(all_levels), f'count_all_{s}')
        model.Add(call_count_overall[s] == sum(indicators[(d, level, s)] for d in range(num_days) for level in all_levels))
    max_all = model.NewIntVar(0, num_days*len(all_levels), 'max_all')
    min_all = model.NewIntVar(0, num_days*len(all_levels), 'min_all')
    model.AddMaxEquality(max_all, [call_count_overall[s] for s in range(num_surgeons)])
    model.AddMinEquality(min_all, [call_count_overall[s] for s in range(num_surgeons)])
    diff_all = model.NewIntVar(0, num_days*len(all_levels), 'diff_all')
    model.Add(diff_all == max_all - min_all)
    
    # --- Incentive for Optional Level 1B ---
    indicator_1B = {}
    for d in range(num_days):
        indicator_1B[d] = model.NewBoolVar(f'nonempty_1B_{d}')
        model.Add(X[(d, "1B")] != -1).OnlyEnforceIf(indicator_1B[d])
        model.Add(X[(d, "1B")] == -1).OnlyEnforceIf(indicator_1B[d].Not())
    total_1B = model.NewIntVar(0, num_days, 'total_1B')
    model.Add(total_1B == sum(indicator_1B[d] for d in range(num_days)))
    gamma1 = 1
    model.Minimize(diff_all - gamma1 * total_1B)
    
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
# Schedule Generation and Saving Endpoints
#############################################

@app.route('/new_schedule', methods=['GET'])
def new_schedule():
    # Get year and month from query parameters; default to today's year/month.
    year_sel, month_sel = get_year_month()
    # Generate days for the selected period.
    days_sel = [datetime.date(year_sel, month_sel, d).isoformat() for d in range(1, calendar.monthrange(year_sel, month_sel)[1] + 1)]
    global days, num_days
    days = days_sel
    num_days = len(days_sel)
    
    sched, cost = solve_schedule_or_tools(days, get_all_surgeons())
    if sched is None:
        flash("No feasible schedule was found. Check configuration and surgeon eligibility.")
        return render_template('no_schedule.html')
    session['last_generated_schedule'] = json.dumps(sched)
    session['last_generated_cost'] = cost
    session['generated_year'] = year_sel
    session['generated_month'] = month_sel
    weekend_set = {d for d in days if datetime.date.fromisoformat(d).weekday() >= 5}
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
    db = get_db()
    row = db.execute("SELECT * FROM saved_schedule WHERE year = ? AND month = ?", (year_sel, month_sel)).fetchone()
    if row:
        sched = json.loads(row['schedule_data'])
    else:
        flash("No saved schedule found for the selected period.")
        sched = {}
    weekend_set = {d for d in days if datetime.date.fromisoformat(d).weekday() >= 5}
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
# Availability / Unavailability Feature
#############################################

# New endpoint: Display the availability calendar.
@app.route('/availability', methods=['GET', 'POST'])
def availability():
    db = get_db()
    if request.method == 'POST':
        # Process a new unavailability/no call request over a date range.
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
                # Insert a record for each date in the range.
                while current_dt <= end_dt:
                    db.execute("INSERT INTO surgeon_availability (surgeon_id, request_type, date) VALUES (?, ?, ?)",
                               (surgeon_id, request_type, current_dt.isoformat()))
                    current_dt += datetime.timedelta(days=1)
                db.commit()
                flash("Requests submitted successfully!")
            except Exception as e:
                flash(f"Error processing the dates: {str(e)}")
        return redirect(url_for('availability', surgeon_id=surgeon_id))
    else:
        # GET: Show the availability page for the selected surgeon (if any).
        surgeon_id = request.args.get('surgeon_id')
        if surgeon_id:
            try:
                surgeon_id = int(surgeon_id)
            except ValueError:
                surgeon_id = None
        else:
            surgeon_id = None
        
        # Retrieve requests only for the selected surgeon, if one is chosen.
        if surgeon_id is not None:
            rows = db.execute(
                "SELECT sa.date, sa.request_type, s.name, s.call_levels FROM surgeon_availability sa JOIN surgeons s ON sa.surgeon_id = s.id WHERE s.id = ?",
                (surgeon_id,)
            ).fetchall()
        else:
            # If no surgeon is selected, show no events.
            rows = []
            
        events = []
        for row in rows:
            # Determine surgeon's level (for simplicity, take the first digit of the first call level).
            levels = parse_call_levels(row["call_levels"])
            level = levels[0][0] if levels else "?"
            events.append({
                "date": row["date"],
                "title": f'{row["name"]} (Level {level}) - {row["request_type"]}',
                "request_type": row["request_type"],
                "surgeon": row["name"],
                "level": level
            })
        
        # Retrieve all surgeons for the drop-down.
        surgeons = get_all_surgeons()
        return render_template('availability.html', events=events, surgeons=surgeons, selected_surgeon_id=surgeon_id)

#############################################
# Run the App
#############################################

if __name__ == '__main__':
    app.run(debug=True)
