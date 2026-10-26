# Project guide for Codex

## Purpose and architecture

PMH Scheduling collects surgeon availability and produces, edits, publishes, and exports monthly call lists using Hong Kong public holidays. Public users view published schedules, submit/remove requests, and inspect cohort availability; session-authenticated administrators manage surgeons, constraints, generation, and schedule versions.

- `app.py`: Flask `create_app()` factory; routes, session authentication, background solver jobs, schedule serialization, statistics, and Excel exports. There is no module-level `app` object.
- `scheduler.py`: OR-Tools CP-SAT model in `solve_schedule_or_tools()`, including eligibility, coverage, spacing, supervision, caps, fairness, and infeasibility diagnostics.
- `helper.py`: SQLAlchemy Core engine, parameterized SQL, schema initialization, configuration, availability, monthly NLTH selection, and shared cohort/history counters. PostgreSQL/Supabase is the intended remote database; SQLite compatibility supports local work. There is no ORM model layer or migration framework.
- `templates/`: server-rendered Jinja with embedded JavaScript/CSS. `base.html` holds shared design tokens/navigation/CSRF metadata; `new_schedule.html`, `edit_publish.html`, and `availability.html` drive the main workflows. `user_help.html` is the existing user documentation. Calendar/chart libraries load from CDNs; there is no frontend build pipeline.
- `tests/test_scheduler_fairness_credit.py`: standard-library `unittest`/`mock` regression suite for scheduling and counters.
- `requirements.txt`, `Dockerfile`, `cloudbuild.yaml`: dependencies and container/deployment configuration. `scripts/solve_check.py` is a limited manual integration helper.

No project README, existing `AGENTS.md`, CI workflow, or formatter/linter configuration was found during initial discovery. Treat `app backup.py`, logs, tracked bytecode, and the checked-in Windows `venv/` as historical artifacts, not implementation references or a usable local environment.

## Development and commands

Use Python 3.11, matching `Dockerfile`. Create a fresh environment outside the repository; do not reuse or edit tracked `venv/`:

```bash
# From the repository root (POSIX shell):
python3.11 -m venv ../pmh-scheduling-venv
. ../pmh-scheduling-venv/bin/activate
python -m pip install -r requirements.txt
```

**Select a disposable database before importing application modules.** `helper.py` loads `.env` and creates `ENGINE` at import; missing `DATABASE_URL` raises an error. Every `create_app()` call runs `init_db()`, which creates/alters tables, backfills data, and seeds defaults. The tracked `surgeon_scheduler.db` is not an automatic/default database and must remain untouched.

Example isolated local session, starting at the repository root with the fresh environment active:

```bash
repo_root="$PWD"
scratch_dir="$(mktemp -d)"
export DATABASE_URL="sqlite:///$scratch_dir/dev.db"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export BASIC_AUTH_USERNAME=local-admin BASIC_AUTH_PASSWORD=local-only-password
export FLASK_ENV=development PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$repo_root"
cd "$scratch_dir"
python -m flask --app 'app:create_app()' run --host 127.0.0.1 --port 5000
```

The example uses disposable local credentials. Export settings before Python starts rather than relying on `.env`. Running `python app.py` from the root also starts the app (default port 5000, configurable via `PORT`; debug only with `FLASK_DEBUG=1`), but creates artifacts in that working directory.

- **Tests:** after stopping the example server, run the following from the scratch directory with its environment still active. An explicit in-memory URL overrides any inherited database setting:

  ```bash
  DATABASE_URL=sqlite:///:memory: PYTHONDONTWRITEBYTECODE=1 \
    python -B -m unittest discover -s "$repo_root/tests" -p 'test_*.py' -v
  ```

  Tests mock database-backed helpers. Keep solver runs outside the repository: `_debug_log()` appends `debug-e91910.log` in the current directory even when `solver_debug=0`.
- **Lint:** no configured lint command exists. Follow surrounding style and run `git diff --check` from the repository root; this checks whitespace, not Python correctness.
- **Build/run:** there is no separate application compilation step. The container recipe is `docker build -t pmh-scheduling .`; its entry point is `gunicorn -b 0.0.0.0:8080 'app:create_app()'`. Read the packaging warning below before building. `Procfile.txt` references the absent `app:app` and should not be used as the run command.
- **Deployment:** `cloudbuild.yaml` builds/pushes an image and immediately deploys Cloud Run in `asia-east2` with unauthenticated access. Do not run it as a validation check or deploy without explicit permission.

Verification status at initial documentation: setup, server startup, and container builds were not executed. The isolated unittest command was attempted but collection failed with `ModuleNotFoundError: flask`; no regression tests executed. These recipes are source-derived and must be reverified in a configured environment. SQLite success would not verify PostgreSQL behavior.

## Workflows and contracts to preserve

1. Administrators configure surgeon call levels, maximum calls, constraint flags/weights, monthly NLTH assignments, requests, and ID-based preassignments. Monthly NLTH selection overrides stale per-surgeon flags before solving.
2. Generation combines those inputs with HK holidays, explicitly supplied prior-month last-two-day assignments, and optional half-year fairness history. `/start_solve` starts a daemon thread; `/solve_status/<job_id>` polls factory-local `solve_jobs`. Normal async solving can retry with `allow_empty=True`; preserve `solver_mode`, `fallback_used`, `attempt_mode`, and error/analysis responses. The synchronous `/new_schedule` preview is a separate path.
3. The solver returns `(schedule, objective)`: date/level assignments contain surgeon **names** or `None`, with optional `__solver_mode__` metadata; failures contain `errors`/diagnostics. Preassignments, prior-last-two inputs, and editing use numeric surgeon **IDs**. Keep conversions at these boundaries; do not confuse metadata with date rows.
4. `/save_schedule` updates legacy `saved_schedule` and creates an unpublished `saved_schedule_versions` entry. Version APIs separately save/overwrite/rename/delete/publish. Publishing clears other published flags for that year/month; public `/saved_schedule` must show only published versions. Preserve the distinction between legacy and versioned storage.

Jobs are process-local and disappear on restart; changing worker counts or instance routing can break polling. Cancellation currently sets a flag that is not passed into the solver; do not claim it interrupts CP-SAT. `scripts/solve_check.py` lacks admin-session/CSRF handling and reliable failure exit status, so it is not a regression gate.

## Scheduling and regression-sensitive rules

- Preserve canonical levels `1A, 1B, Urology, 2A, 2B, 3, 4`, comma-separated eligibility, and ISO date keys. Urology eligibility may coexist only with `1A`/`1B`. Persisted name-based history makes surgeon renames/deletions sensitive.
- Eligibility, preassignments, unavailability/study leave/no-call policies, same-day exceptions, three-day call spacing, previous-month boundaries, supervision, and monthly caps interact. Preassignments survive some domain pruning but do not override every later constraint. Preserve enabled/disabled and hard/soft policy behavior.
- Preserve Urology exceptions: `1B+Urology` may share a surgeon; Urology-only coverage can leave internal `1B` empty while the display mirrors `1A`. Internal empties use `-1`; displayed mirroring must not silently redefine solver counts.
- Preserve cohort formulas: G1 deduplicates `1A/1B` per surgeon/day; G2 counts `2A+2B` for L2 subgroups 1–3; G3 includes `2B+3` for L2 subgroup 4 and excludes dual `3+4` surgeons; G4 counts `4` plus `3` for dual `3+4` surgeons. Use `get_level2_group()` and shared counter helpers rather than stale grouping comments.
- Consumers differ today: solver fairness excludes NLTH surgeons and optionally adds L2 subgroup 1 to G1 (`enable_l2g1_primary_calls`); helper cohort membership differs. General `/stats` and Excel “Total Calls” count occupied slots, including mirrored slots, rather than deduplicated cohort calls. Trace solver/helper/UI/export consumers together; reconciling these differences requires explicit scope and regression tests.
- Half-year history resets in January/July and excludes the target month. **Existing caveat:** `get_published_schedule_version()` prefers a published version but falls back to the latest saved draft. History/fairness/export consumers can therefore include drafts despite “published only” comments; preserve or explicitly resolve this behavior with tests when authorized.
- Enabled fairness range caps clamp to `0..1`. `no_fallback` enforces the cap; `auto_relax` minimizes cohort overflow. Preserve objective priority: non-`2B` empty slots first in `allow_empty` mode, then fairness priorities, then remaining penalties. A time-limited feasible result does not prove optimality.
- Manual UI credit is negative for fewer calls and positive for more; solver credit reverses that sign. Unavailable/study-leave credit uses integer division by its configured window; no-call requests do not earn that credit.
- Request submission deadlines block non-admin submissions for the next month while allowing later months and admin overrides. Deadline calculations use server-local naive time. Calendar request ranges have inclusive ends; FullCalendar uses exclusive ends.

## Change conventions and database safety

- Read the affected implementation and callers/templates before editing. Preserve functionality unless explicitly directed otherwise; prefer minimal, maintainable changes over rewrites or unrelated cleanup. Match nearby Python naming/indentation and Jinja/JavaScript conventions; avoid whole-file reformatting.
- Extend existing helpers and templates. Preserve shared `base.html` tokens, `url_for`, safe JSON serialization/escaping, route payloads, and responsive table/calendar behavior. A dependency upgrade or addition needs a concrete justification and compatibility checks; do not regenerate the vendored environment. Requirements are partly pinned; `app.py` imports pandas, currently supplied transitively by OR-Tools.
- Keep bound SQL parameters, explicit transactions, Flask application contexts for background DB work, and teardown connection cleanup. Preserve `json_cast`, `sql_now`, and boolean dialect helpers. Database JSON may arrive as a decoded dict or serialized string; global configuration values are strings, with flags generally `"1"`/`"0"`.
- Schema changes live in startup `init_db()`. Review repeat-start/idempotency behavior and both PostgreSQL and SQLite paths; test migrations on disposable data and define recovery before changing them. Never migrate, seed, delete, or inspect production records as an incidental test. Production data, credentials, and deployment settings require explicit permission to modify.

## Security and existing packaging risks

- Never print, copy into documentation, or commit secret values, connection URLs, session cookies, staff records, or sensitive logs. `.env`, the database, logs, `venv/`, and bytecode are already tracked; leave them untouched unless specifically in scope. Do not assume tracked artifacts are safe to expose.
- Preserve session-based `admin_required`, global Flask-WTF CSRF protection, hidden form `csrf_token`, and JSON `X-CSRFToken` headers from the base meta tag. Public request submission/removal and cohort views are intentional existing behavior; do not silently change their access policy.
- Preserve cookie/header/request-size protections. The code defaults to `SECRET_KEY="dev-key"` and development cookie settings; these defaults do not establish production safety. Login redirect validation and public request ownership protection also need separate review if security work is requested.
- **Packaging risk:** Docker recognizes `.dockerignore`, but this repository has only `.dockerignore.txt`. With `COPY . .`, an image may contain `.env`, the database, logs, Git metadata, and the vendored environment. Do not build/publish an image until its context/exclusions have been explicitly reviewed; any deployment-file fix must be separately authorized and scoped.

## Testing and definition of done

- Add/update focused tests when behavior changes. Follow existing `unittest` fixtures/mocks; assert constraints, counts, and feasible/infeasible outcomes rather than an arbitrary optimal assignment. Cover applicable feature flags, empty-slot/fairness fallbacks, ID/name conversions, prior-month/half-year boundaries, and overlapping eligibility groups.
- For UI/API/storage changes, exercise affected flows with disposable data: public/admin access, CSRF, request deadlines/ranges, generate/poll/failure, save/edit/overwrite, publish/unpublish visibility, live counters, and Excel output. Existing tests do not establish browser, authentication, deployment, or full database coverage.
- Run relevant tests and required checks, inspect the diff, and run `git diff --check`. Confirm only intended files changed and no database, log, bytecode, credentials, or environment artifacts slipped in. Do not merge, deploy, or change production settings without authorization.
- Report what changed, exact checks/results, remaining risks, and unverified assumptions. Never claim setup, a command, a test, or a deployment works unless actually verified; distinguish dependency/collection failures from executed failing tests.
