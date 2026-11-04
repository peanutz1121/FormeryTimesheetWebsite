"""CSV export helpers (standard library ``csv`` module only)."""
import csv

# A spreadsheet may treat a cell starting with one of these characters as a
# formula (CSV / formula injection). We neutralise such text cells by
# prefixing an apostrophe, which spreadsheets display as plain text.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")

CSV_COLUMNS = [
    "Employee ID", "Employee name", "Username", "Department", "Work date",
    "Project code", "Project", "Task description", "Time in", "Time out",
    "Break (minutes)", "Regular hours", "Overtime hours", "Total hours", "Hours source",
    "Work location", "Status", "Notes", "Rejection reason", "Created by",
    "Created at", "Last updated", "Reviewed by", "Reviewed at",
]


def sanitize_cell(value):
    """Return a CSV-safe string. Numbers and dates formatted by us are unaffected."""
    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip(" ")
    if stripped.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _fmt_dt(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def entry_row(entry):
    profile = getattr(entry.employee, "profile", None)
    text_cells = [
        profile.employee_id if profile else "",
        entry.employee_name,
        entry.employee.get_username(),
        profile.department if profile else "",
    ]
    return [sanitize_cell(c) for c in text_cells] + [
        entry.work_date.isoformat(),
    ] + [sanitize_cell(c) for c in (
        entry.project.code if entry.project else "",
        entry.project.name if entry.project else "",
        entry.task_description,
    )] + [
        entry.time_in.strftime("%H:%M") if entry.time_in else "",
        entry.time_out.strftime("%H:%M") if entry.time_out else "",
        entry.break_minutes,
        f"{entry.regular_hours:.2f}",
        f"{entry.overtime_hours:.2f}",
        f"{entry.total_hours:.2f}",
        entry.get_hours_source_display(),
    ] + [sanitize_cell(c) for c in (
        entry.work_location,
        entry.get_status_display(),
        entry.notes,
        entry.rejection_reason,
        entry.created_by.get_username() if entry.created_by else "",
    )] + [
        _fmt_dt(entry.created_at),
        _fmt_dt(entry.updated_at),
    ] + [sanitize_cell(entry.reviewed_by.get_username() if entry.reviewed_by else "")] + [
        _fmt_dt(entry.reviewed_at),
    ]


def write_entries_csv(file_like, queryset):
    """Write the header plus one row per entry; returns the number of data rows."""
    writer = csv.writer(file_like)
    writer.writerow(CSV_COLUMNS)
    count = 0
    for entry in queryset.iterator(chunk_size=500):
        writer.writerow(entry_row(entry))
        count += 1
    return count
