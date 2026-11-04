# Formery Timesheet Database

A Django web application for manually entering, reviewing, approving and reporting on
employee timesheets, backed by one central SQLite database.

It uses only Python, Django, Django Templates, HTML, CSS, vanilla JavaScript and SQLite.
There is no Node.js, npm, frontend framework, CSS framework or Docker, and the only Python
dependency is Django itself.

> **Status:** built and tested for local use and small internal deployments. It has **not**
> had an independent security audit. Read [Production deployment warnings](#14-production-deployment-warnings)
> before putting it on a public server.

---

## Contents

1. [Features](#1-features)
2. [Technology stack](#2-technology-stack)
3. [Prerequisites](#3-prerequisites)
4. [Folder structure](#4-folder-structure)
5. [Installation (step by step)](#5-installation-step-by-step)
6. [Creating accounts](#6-creating-accounts)
7. [Using the website](#7-using-the-website)
8. [How hours are calculated](#8-how-hours-are-calculated)
9. [Database design decisions](#9-database-design-decisions)
10. [Running the automated tests](#10-running-the-automated-tests)
11. [Branding, design and the login image](#11-branding-design-and-the-login-image)
12. [Configuration](#12-configuration)
13. [Common setup errors](#13-common-setup-errors)
14. [Production deployment warnings](#14-production-deployment-warnings)
15. [Known limitations](#15-known-limitations)

---

## 1. Features

**Employees**
- Secure login and logout.
- Create timesheet entries manually (save as draft or save and submit).
- Edit their own drafts and rejected entries; delete their own drafts.
- See the status of every entry and the reason when an entry is rejected.
- Search, filter, sort and paginate their own history; see totals for the filtered results.
- Personal dashboard: this week, this month, regular vs overtime, approved hours.
- Approved and submitted entries are locked against employee edits.

**Administrators**
- Dashboard with live totals (entries, active employees, hours, approved hours, overtime,
  awaiting approval, drafts, rejected, hours this month), status distribution, six-month
  hours chart, top employees, pending approvals, recent activity and recent changes.
  All values are calculated from the database with Django ORM aggregation.
- Full timesheet management: create, view, edit, delete (with confirmation), search by
  employee name, employee ID, project or task; filter by employee, status,
  project and date range; sort; paginate.
- Approve or reject submitted timesheets (rejection reason optional); reverse an approved
  entry by rejecting it. Reviewer and time are recorded.
- Employee management: create, edit, deactivate/reactivate, view profile with totals.
- Project management (**Projects** in the sidebar): add, edit, search, archive and delete unused
  projects, with entry counts and total hours per project.
- Quick links on the dashboard to add a project or employee and to open Django Admin.
- Reports (daily, weekly, monthly, by employee, by project, approved vs pending, rejected
  counts, work history) with filters for start date, end date, employee, department,
  project and status.
- CSV export of exactly the records matching the active filters.
- Django Admin for direct database maintenance.

**Cross-cutting**
- Approval history and an audit log of important actions (create, edit, delete, submit,
  approve, reject, account changes, CSV exports, sign-in and sign-out).
- Authorization is enforced in the views, not just by hiding links.
- Responsive layout (desktop, tablet, phone) with custom 403, 404 and 500 pages.

## 2. Technology stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Framework | Django 5.1 or 5.2 (authentication, ORM, forms, sessions, CSRF, admin) |
| Templates | Django Templates with template inheritance |
| Front end | HTML5, CSS3, vanilla JavaScript (no JS libraries or CSS frameworks). Fonts: Cormorant Garamond and Jost from Google Fonts when online, with system serif/sans-serif fallbacks when offline |
| Database | SQLite (file `db.sqlite3`) |

## 3. Prerequisites

- **Python 3.10 or newer.** Check with `python --version` (Windows) or `python3 --version`
  (macOS/Linux). Download from <https://www.python.org/downloads/> if needed. On Windows,
  tick **"Add python.exe to PATH"** in the installer.
- An internet connection once, to download Django with `pip`.

## 4. Folder structure

```
formery_timesheet_db/
├── manage.py                  Django command-line entry point
├── requirements.txt           Python dependencies (Django only)
├── README.md                  This file
├── .gitignore
├── config/                    Project configuration
│   ├── settings.py            Settings (environment-variable driven)
│   ├── urls.py                Root URL configuration
│   └── wsgi.py / asgi.py
├── timesheets/                The application
│   ├── models.py              EmployeeProfile, Project, TimesheetEntry, ApprovalHistory, AuditLog
│   ├── forms.py               Timesheet, filter, review and employee forms + validation
│   ├── views.py               Pages and actions (authorization enforced here)
│   ├── permissions.py         Role checks (is_admin, can_edit_entry, ...)
│   ├── services.py            Approval workflow and audit logging
│   ├── queries.py             Visibility rules, filters, dashboard and report aggregation
│   ├── hours.py               Pure functions for shift/hour calculation
│   ├── exports.py             CSV writer with formula-injection protection
│   ├── signals.py             Auto-create profiles; log sign-in/out
│   ├── admin.py               Django Admin configuration
│   ├── urls.py
│   ├── tests.py               Automated tests
│   ├── migrations/            Database migrations
│   ├── templatetags/          Small template helpers (badges, formatting)
│   └── management/commands/seed_demo.py   Optional demo data (development only)
├── templates/                 base.html, 403/404/500, login, partials, page templates
└── static/                    css/app.css, js/app.js
```

## 5. Installation (step by step)

Open a terminal (Command Prompt or PowerShell on Windows, Terminal on macOS/Linux) and
change into the project folder, the one that contains `manage.py`:

```
cd formery_timesheet_db
```

### 5.1 Create a virtual environment

Windows:
```
python -m venv venv
```
macOS / Linux:
```
python3 -m venv venv
```

### 5.2 Activate it

Windows (Command Prompt):
```
venv\Scripts\activate
```
Windows (PowerShell):
```
venv\Scripts\Activate.ps1
```
If PowerShell refuses with "running scripts is disabled", run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then try again.

macOS / Linux:
```
source venv/bin/activate
```
Your prompt should now start with `(venv)`.

### 5.3 Install dependencies

```
pip install -r requirements.txt
```

### 5.4 Create the database

```
python manage.py migrate
```
This creates `db.sqlite3` next to `manage.py` (the database location).

### 5.5 Create the first administrator

```
python manage.py createsuperuser
```
Enter a username, email and a password of at least 10 characters that is not common or
all-numeric. This account is an administrator in both the website and Django Admin.

(Use `python3` instead of `python` on macOS/Linux if `python` is not found.)

### 5.6 Start the server

```
python manage.py runserver
```

- Website: <http://127.0.0.1:8000/>
- Django Admin: <http://127.0.0.1:8000/admin/>

Sign in with the administrator account. Stop the server with `Ctrl+C`.

### 5.7 Optional: load demo data

For a quick tour with sample employees, projects and about 180 entries (development only;
refuses to run when `DJANGO_DEBUG=False`):
```
python manage.py seed_demo
```
Accounts created: `demo_admin` (administrator) and `amara`, `ben`, `carla`, `dev`
(employees). The password is printed by the command (`Demo-Pass-2026!x` by default;
change it with `--password`). **Never use demo accounts on a real server.**

## 6. Creating accounts

### Employee accounts
1. Sign in as an administrator and open **Employees → New employee**.
2. Fill in name, username, email and password (min. 10 characters). Optionally set
   employee ID (blank = auto-generated like `EMP-0004`), department, job title.
3. Save. The new account is a normal employee. The **Add employee** form can never grant administrator rights.

### Administrator (staff) accounts
An administrator is an active user with **staff** or **superuser** status.
- **From the website:** an administrator opens **Employees → Add staff account** (or the
  **Add a staff account** card on the dashboard) and fills in the same form as for an
  employee. The new person gets staff-level administrator access: approvals, timesheets,
  employees, projects, reports, CSV export and the timesheet tables in Django Admin. Superuser
  rights are never granted from the website. Creation is written to the audit log, and the
  **Role** filter on the Employees page lists staff and employees separately.
- First administrator: `python manage.py createsuperuser` (see 5.5).
- More administrators: sign in to **Django Admin → Users**, open the person, tick
  **Staff status** (and **Superuser status** if they should manage users/permissions in
  Django Admin), and save. Only superusers should be given access to the Users section.

Staff administrators can use every page of the website and have full access to the
timesheet tables in Django Admin. Managing *user accounts* in Django Admin still needs
Django's normal permissions, so a staff member cannot promote themselves.

### Deactivating employees
Open an employee and choose **Deactivate**. They can no longer sign in (existing sessions
stop working too) and all their timesheet history is preserved. **Reactivate** restores
access. Employees with timesheets can't be hard-deleted (see section 9).

## 7. Using the website

### Adding projects (administrators)
Open **Projects → Add project** (or the **Add a project** card on the dashboard). Enter a name,
a unique code, the **Stage Status** (Pre-Design, Schematic Design, Design Development, Construction
Documents, Permitting & Bidding, Construction Administration, Completed, On hold, Archived) and
optional dates. Under **Tasks** add, rename or remove the project's task checklist; staff tick these
on the timesheet form and the ticked tasks are written into the task description. Projects stay
selectable on timesheets until they are Completed, On hold or Archived. No client names are stored.
The **Django Admin** link in the sidebar and dashboard opens the full database admin.

### Access control (sections and projects)
Every project belongs to a **Section**: *Architecture projects*, or one of the restricted sections
**Admin**, **Marketing & Social** and **Networking**. Administrators (accounts with Staff status,
e.g. Emma and Menchie) can use every section and project. Employees can use **only** the projects an
administrator assigns to them, and a project in a restricted section additionally needs that section
granted to the employee (this is how, for example, Diane can be given Social projects only).
Open **Access** in the sidebar, choose a person, tick their projects and any restricted sections, and
save; it applies on their next request. Employees see their projects under **My projects**. The checks
run on the server for every page, timesheet form, filter and direct URL (an unassigned project is a
404), and changes are audit-logged. New employees start with **no** projects until assigned.

### Time in Lieu (TIL)
**Time in Lieu** in the sidebar tracks banked hours for every staff member and employee.
Administrators see all balances (earned, used, balance), open a person's ledger and record
**Earned**, **Taken** or **Deducted / expired** hours with a date and note; the balance is
earned minus used, and taking more than the banked balance is blocked. Employees see only their
own ledger (**My TIL**) and balance on their dashboard, and cannot edit it. Entries are audit-logged.

**Requesting time in lieu (needs administrator approval).** Any employee or staff member can open
their own ledger (**My TIL**) and use **Request time in lieu**: choose the **Type** (**Earned** to bank
hours worked, or **Taken** for time off), then date, hours and reason. Earned dates cannot be in the
future; Taken requests cannot exceed the banked balance (after other pending time off). The
request stays **Pending** and does not change the balance; the ledger shows what is awaiting approval. Administrators see a count on the **Time in Lieu** sidebar link and review everything
under **Time in Lieu > Requests** (or from the person's ledger): **Approve** records an *Earned* or
*Taken* ledger entry (time off is re-checked against the balance at approval), **Reject** changes nothing and can carry a reason. Requesters can cancel
their own pending requests. Decisions are final, cannot be applied twice, and an administrator
cannot approve or reject their own request (another administrator must). Every step is audit-logged.
Run `python manage.py migrate` after updating (migrations 0007 and 0008).

### Entering timesheet records
1. Click **New timesheet**.
2. Choose the work date (cannot be in the future), optional project/location, and
   describe the task.
3. Enter time in, time out and unpaid break (optional), and choose how hours are recorded:
   **Enter hours manually** or **Calculate from time in and time out** (see section 8).
4. **Save as draft**, or **Save and submit for approval**. Administrators additionally
   choose the employee and the status.

Drafts and rejected entries can be edited; use **Submit** on the detail page when ready.

### Approving or rejecting submissions
1. Open **Approvals** in the sidebar (or the "Pending approvals" panel on the dashboard).
2. **Approve** (confirmation prompt), or **Reject…** with an optional reason the employee
   will see.
3. Approved entries are locked for employees. An administrator can correct them or reverse
   them by rejecting; every change is recorded in the entry's history and the audit log.

### Generating reports
Open **Reports**, choose any combination of start date, end date, employee, department,
project, status and search text, and press **Run report**. The page shows totals,
approved vs pending hours, rejected counts, hours by employee and project, monthly, weekly
and daily summaries and the matching work-history records.

### Exporting CSV
On **Reports**, set filters and press **Export CSV**. The file contains only the records
matching those filters. It is a plain **CSV** text file (not an Excel workbook); Excel,
LibreOffice and Google Sheets open it. Text cells beginning with `=`, `+`, `-` or `@` are
prefixed with an apostrophe so spreadsheets don't execute them as formulas. Only
administrators can export, and each export is written to the audit log.

## 8. How hours are calculated

- **Shift length** = time out − time in. If time out is earlier than time in, it is an
  **overnight shift** ending the next day (22:00 → 06:00 = 8 h). Identical times are rejected.
- **Net hours** = shift length − unpaid break. A break as long as the shift is rejected.
- **Manual mode:** the regular and overtime hours you type are saved exactly as typed.
  Nothing is overwritten, even if times are also entered (the detail page flags when typed
  hours differ from the times).
- **Calculated mode:** hours come from the times; up to 8.00 hours are regular and the rest
  overtime (`STANDARD_DAILY_HOURS` in `config/settings.py`). Typed hour values are ignored.
- Each entry stores **Entered manually** or **Calculated from times** so the two are always
  distinguishable. Regular + overtime must be greater than 0 and no more than 24.
- Hours use `DecimalField` (never floats).

## 9. Database design decisions

- **EmployeeProfile** is one-to-one with Django's `User`. "Active" lives only on
  `User.is_active`, so there is a single source of truth and Django itself blocks login.
- **TimesheetEntry.employee** uses `PROTECT`: an employee with timesheets can't be deleted
  accidentally; deactivate instead. `created_by`/`reviewed_by` use `SET_NULL` so removing an
  administrator doesn't remove history. **Project** is protected while entries use it.
- **ApprovalHistory** rows belong to an entry. **AuditLog** rows deliberately have no
  foreign key to what they describe, so the trail survives deletion of the entry.
- CHECK constraints keep hours (0–24), break minutes and status values valid at the
  database level in addition to form and model validation.
- Indexes cover the common filters: employee+date, status+date, date, project+date, created time.
- Employee ID is unique and auto-generated when blank.

## 10. Running the automated tests

```
python manage.py test
```
Expected: all tests pass (about 84, a few seconds). They cover login/logout, employee
data isolation, administrator permissions, entry creation, hour calculation (including
overnight shifts), the approval workflow, deactivated accounts, dashboard figures, filters,
CSV export and its access restrictions, error pages and Django Admin access.

Other useful checks:
```
python manage.py check
python manage.py makemigrations --check --dry-run
```

## 11. Branding, design and the login image

**Name and logo.** The product is called *Formery Timesheet Database*. The logo is an original
monogram and wordmark built from inline SVG (`templates/partials/_brand.html`,
`static/img/logo.svg`, `static/img/favicon.svg`). The name appears in `timesheets/context_processors.py`
(`app_name`), `timesheets/admin.py`, `timesheets/apps.py` and `templates/admin/base_site.html`.

**Design.** A light, editorial interface: warm white background, near-black text, hairline rules,
a horizontal top navigation, serif headings with italic accents, small uppercase labels and square
outlined buttons. Colours and fonts are CSS variables at the top of `static/css/app.css`.
The design is an original interpretation of that style; it does not use any images, text or
logo from other websites. Google Fonts are requested from the browser when online (a
third-party request); if you must avoid that, delete the Google Fonts `<link>` lines in
`templates/base.html`, `templates/500.html` and `templates/admin/base_site.html`, and the
site falls back to system fonts.

**Login page image.** The right half of the login page is a full-height portrait image panel.
To use your own picture, **replace `static/img/login-side.jpg`** with your image (keep the
same file name; JPG, ideally portrait, around 1000 x 1500 px or larger, under 500 KB).
Refresh the page (press Ctrl+F5 if the old image is cached). On phones the image becomes a
banner above the form. To use a different file name or format, edit the `.auth-media`
rule in `static/css/app.css`. If you run with `DJANGO_DEBUG=False`, run
`python manage.py collectstatic` again after changing the image.

## 12. Configuration

Everything works with no configuration for local development. For anything else, set
environment variables:

| Variable | Purpose | Default |
|---|---|---|
| `DJANGO_DEBUG` | `False` for production | `True` |
| `DJANGO_SECRET_KEY` | Required when debug is off | insecure dev key (debug only) |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated host names | `localhost,127.0.0.1,[::1]` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | e.g. `https://timesheets.example.com` | empty |
| `DJANGO_TIME_ZONE` | Where your staff work | `Asia/Manila` |
| `DJANGO_SECURE_SSL_REDIRECT` | Redirect HTTP to HTTPS when debug is off | `True` |
| `DJANGO_BEHIND_PROXY` | Trust `X-Forwarded-Proto` from your reverse proxy | `False` |
| `DJANGO_HSTS_SECONDS` | HSTS duration when debug is off | `3600` |

Generate a secret key with:
```
python -c "import secrets; print(secrets.token_urlsafe(60))"
```

## 13. Common setup errors

| Problem | Solution |
|---|---|
| `python is not recognized` (Windows) | Reinstall Python with "Add to PATH" ticked, or use `py` instead of `python`. |
| `command not found: python` (macOS/Linux) | Use `python3`. |
| `No module named django` | The virtual environment isn't active or dependencies aren't installed. Activate it (5.2) and run `pip install -r requirements.txt`. |
| PowerShell "running scripts is disabled" | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then activate again. |
| `Could not find manage.py` / `can't open file` | `cd` into the folder that contains `manage.py`. |
| `no such table: ...` | Run `python manage.py migrate`. |
| `Error: That port is already in use` | `python manage.py runserver 8001` and open that port. |
| `DisallowedHost` / 400 Bad Request | Add your host name to `DJANGO_ALLOWED_HOSTS`. |
| `ImproperlyConfigured: DJANGO_SECRET_KEY must be set` | You set `DJANGO_DEBUG=False`; also set `DJANGO_SECRET_KEY`. |
| CSRF verification failed behind HTTPS/proxy | Set `DJANGO_CSRF_TRUSTED_ORIGINS=https://your.domain`. |
| Pages unstyled with `DEBUG=False` | Django doesn't serve static files itself in production. Run `python manage.py collectstatic` and serve `staticfiles/` with your web server. |
| `This password is too common` / too short | Use at least 10 characters that aren't common or all digits. |
| Forgot administrator password | `python manage.py changepassword <username>` |
| Can't sign in with a known password | The account may be deactivated: reactivate it under Employees or in Django Admin. |
| Want to start over | Stop the server, delete `db.sqlite3`, run `python manage.py migrate` again. This permanently deletes all data. |

## 14. Production deployment warnings

`python manage.py runserver` is a **development server only**. Before real use:

1. Set `DJANGO_DEBUG=False`, a long random `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS`.
2. Serve over **HTTPS** (secure cookies and HTTP-to-HTTPS redirect are enabled when debug is off).
   If you're behind a reverse proxy, set `DJANGO_BEHIND_PROXY=True` *only* if the proxy
   always sets `X-Forwarded-Proto` itself.
3. Run under a real WSGI server (for example gunicorn or uWSGI) behind nginx/Apache, and
   serve static files with the web server after `python manage.py collectstatic`.
4. **Back up `db.sqlite3`** regularly and keep it, and your environment variables, out of
   version control and out of any web-served folder.
5. **Never run `seed_demo` or use demo passwords** on a real server.
6. SQLite is fine for a small team; it handles one writer at a time. For many concurrent
   users, move to PostgreSQL (a settings change, since the code uses the ORM only).
7. Login attempts are logged but **not rate-limited**. Put rate limiting in front of the
   site (reverse proxy, firewall) if it is exposed to the internet.
8. `python manage.py check --deploy` is a useful final check. With the settings above it
   reports only the optional HSTS subdomain/preload recommendations (`W005`, `W021`),
   which are deliberately left off; enable them only if you understand the consequences.
9. Keep Django updated with security releases.

## 15. Known limitations

- No password-reset-by-email flow (no mail server is assumed); administrators reset
  passwords from the employee edit form, or with `changepassword`.
- No login rate limiting or two-factor authentication.
- Overtime calculation uses a fixed daily threshold (8 h); it does not model weekly
  overtime rules, rounding policies or pay calculations. No hourly rates or client names are stored.
- Projects that have timesheets cannot be deleted (archive them instead); there is no project-level budget or team assignment.
- CSV is the only export format.
- The web fonts could not be loaded in the build environment, so the layout was verified with fallback fonts; check the look once on a machine with internet access.
- Verified with automated tests and manual checks in Chromium; other browsers and real
  device testing were not performed.
