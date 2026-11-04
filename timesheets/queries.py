"""
Read-side logic: which entries a user may see, filtering/sorting, and the
aggregations behind the dashboards and reports. Everything is computed in the
database with the ORM - no records are loaded into Python just to be summed.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncMonth, TruncWeek
from django.utils import timezone

from .models import AuditLog, TimesheetEntry
from .permissions import is_admin

Status = TimesheetEntry.Status
ZERO = Decimal("0.00")

HOURS_FIELD = DecimalField(max_digits=8, decimal_places=2)
TOTAL_EXPR = ExpressionWrapper(F("regular_hours") + F("overtime_hours"), output_field=HOURS_FIELD)

SORT_MAP = {
    "work_date": ("work_date", "created_at"),
    "-work_date": ("-work_date", "-created_at"),
    "employee": ("employee__last_name", "employee__first_name", "work_date"),
    "-employee": ("-employee__last_name", "-employee__first_name", "-work_date"),
    "project": ("project__name", "work_date"),
    "-project": ("-project__name", "-work_date"),
    "status": ("status", "-work_date"),
    "-status": ("-status", "-work_date"),
    "total": ("total", "-work_date"),
    "-total": ("-total", "-work_date"),
    "created": ("created_at",),
    "-created": ("-created_at",),
}
SORT_CHOICES = [
    ("-work_date", "Work date (newest first)"),
    ("work_date", "Work date (oldest first)"),
    ("employee", "Employee (A-Z)"),
    ("-employee", "Employee (Z-A)"),
    ("project", "Project (A-Z)"),
    ("-total", "Total hours (high to low)"),
    ("total", "Total hours (low to high)"),
    ("status", "Status (A-Z)"),
    ("-created", "Recently created"),
]


def visible_entries(user):
    """The ONLY entry point for fetching entries in views: employees see just their own."""
    qs = TimesheetEntry.objects.select_related(
        "employee", "employee__profile", "project", "created_by", "reviewed_by",
    )
    if is_admin(user):
        return qs
    return qs.filter(employee=user)


def apply_filters(qs, data, ordered=True):
    """Apply cleaned filter-form data to an entry queryset (all in SQL)."""
    q = (data.get("q") or "").strip()
    for token in q.split()[:6]:
        qs = qs.filter(
            Q(employee__first_name__icontains=token)
            | Q(employee__last_name__icontains=token)
            | Q(employee__username__icontains=token)
            | Q(employee__profile__employee_id__icontains=token)
            | Q(project__name__icontains=token)
            | Q(project__code__icontains=token)
            | Q(task_description__icontains=token)
        )
    if data.get("employee"):
        qs = qs.filter(employee=data["employee"])
    if data.get("department"):
        qs = qs.filter(employee__profile__department__iexact=data["department"])
    if data.get("project"):
        qs = qs.filter(project=data["project"])
    if data.get("status"):
        qs = qs.filter(status=data["status"])
    if data.get("start"):
        qs = qs.filter(work_date__gte=data["start"])
    if data.get("end"):
        qs = qs.filter(work_date__lte=data["end"])
    if ordered:
        qs = qs.annotate(total=TOTAL_EXPR).order_by(
            *SORT_MAP.get(data.get("sort") or "-work_date", SORT_MAP["-work_date"]), "-id"
        )
    return qs


def _num(value):
    return value if value is not None else ZERO


def _month_bounds(today):
    first = today.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    return first, next_month - timedelta(days=1)


def _display_name(first, last, username):
    return f"{first or ''} {last or ''}".strip() or username


# --------------------------------------------------------------------------
# Administrator dashboard
# --------------------------------------------------------------------------
def admin_dashboard_data():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    today = timezone.localdate()
    month_start, month_end = _month_bounds(today)

    agg = TimesheetEntry.objects.aggregate(
        total_entries=Count("id"),
        regular=Sum("regular_hours"),
        overtime=Sum("overtime_hours"),
        approved_regular=Sum("regular_hours", filter=Q(status=Status.APPROVED)),
        approved_overtime=Sum("overtime_hours", filter=Q(status=Status.APPROVED)),
        submitted=Count("id", filter=Q(status=Status.SUBMITTED)),
        drafts=Count("id", filter=Q(status=Status.DRAFT)),
        rejected=Count("id", filter=Q(status=Status.REJECTED)),
        approved=Count("id", filter=Q(status=Status.APPROVED)),
        month_regular=Sum("regular_hours", filter=Q(work_date__range=(month_start, month_end))),
        month_overtime=Sum("overtime_hours", filter=Q(work_date__range=(month_start, month_end))),
    )
    stats = {
        "total_entries": agg["total_entries"],
        "active_employees": User.objects.filter(is_active=True, is_staff=False, is_superuser=False).count(),
        "total_hours": _num(agg["regular"]) + _num(agg["overtime"]),
        "approved_hours": _num(agg["approved_regular"]) + _num(agg["approved_overtime"]),
        "overtime_hours": _num(agg["overtime"]),
        "submitted": agg["submitted"],
        "drafts": agg["drafts"],
        "rejected": agg["rejected"],
        "approved": agg["approved"],
        "month_hours": _num(agg["month_regular"]) + _num(agg["month_overtime"]),
    }

    # Status distribution (counts) for the stacked bar.
    counts = {row["status"]: row["n"] for row in TimesheetEntry.objects.values("status").annotate(n=Count("id"))}
    total = sum(counts.values()) or 1
    distribution = [
        {"status": s.value, "label": s.label, "count": counts.get(s.value, 0),
         "percent": round(counts.get(s.value, 0) * 100 / total, 1)}
        for s in Status
    ]

    # Monthly summary: the last six calendar months including this one.
    months = []
    cursor = month_start
    for _ in range(6):
        months.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months.reverse()
    rows = (
        TimesheetEntry.objects.filter(work_date__gte=months[0])
        .annotate(month=TruncMonth("work_date"))
        .values("month")
        .annotate(regular=Sum("regular_hours"), overtime=Sum("overtime_hours"), entries=Count("id"))
    )
    by_month = {r["month"]: r for r in rows}
    monthly = []
    for m in months:
        r = by_month.get(m)
        regular = _num(r["regular"]) if r else ZERO
        overtime = _num(r["overtime"]) if r else ZERO
        monthly.append({"month": m, "regular": regular, "overtime": overtime,
                        "total": regular + overtime, "entries": r["entries"] if r else 0})
    peak = max((m["total"] for m in monthly), default=ZERO) or Decimal("1")
    for m in monthly:
        m["percent"] = round(float(m["total"] / peak * 100), 1)
        m["overtime_percent"] = round(float(m["overtime"] / peak * 100), 1)

    # Employees with the most recorded hours.
    top = list(
        TimesheetEntry.objects.values(
            "employee_id", "employee__first_name", "employee__last_name",
            "employee__username", "employee__profile__employee_id",
        )
        .annotate(total=Sum(TOTAL_EXPR), overtime=Sum("overtime_hours"))
        .order_by("-total")[:5]
    )
    top_peak = (top[0]["total"] if top else ZERO) or Decimal("1")
    for r in top:
        r["name"] = _display_name(r["employee__first_name"], r["employee__last_name"], r["employee__username"])
        r["code"] = r["employee__profile__employee_id"] or ""
        r["percent"] = round(float(r["total"] / top_peak * 100), 1)

    base = TimesheetEntry.objects.select_related("employee", "employee__profile", "project")
    return {
        "stats": stats,
        "distribution": distribution,
        "monthly": monthly,
        "top_employees": top,
        "pending": base.filter(status=Status.SUBMITTED).order_by("submitted_at", "id")[:6],
        "recent": base.order_by("-created_at", "-id")[:10],
        "recent_changes": AuditLog.objects.select_related("actor").order_by("-timestamp", "-id")[:8],
        "month_label": today.strftime("%B %Y"),
    }


# --------------------------------------------------------------------------
# Employee dashboard
# --------------------------------------------------------------------------
def employee_dashboard_data(user):
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)
    month_start, month_end = _month_bounds(today)
    mine = TimesheetEntry.objects.filter(employee=user)

    agg = mine.aggregate(
        regular=Sum("regular_hours"),
        overtime=Sum("overtime_hours"),
        week_regular=Sum("regular_hours", filter=Q(work_date__range=(week_start, week_end))),
        week_overtime=Sum("overtime_hours", filter=Q(work_date__range=(week_start, week_end))),
        month_regular=Sum("regular_hours", filter=Q(work_date__range=(month_start, month_end))),
        month_overtime=Sum("overtime_hours", filter=Q(work_date__range=(month_start, month_end))),
        approved_regular=Sum("regular_hours", filter=Q(status=Status.APPROVED)),
        approved_overtime=Sum("overtime_hours", filter=Q(status=Status.APPROVED)),
        drafts=Count("id", filter=Q(status=Status.DRAFT)),
        submitted=Count("id", filter=Q(status=Status.SUBMITTED)),
        approved=Count("id", filter=Q(status=Status.APPROVED)),
        rejected=Count("id", filter=Q(status=Status.REJECTED)),
        entries=Count("id"),
    )
    stats = {
        "entries": agg["entries"],
        "regular": _num(agg["regular"]),
        "overtime": _num(agg["overtime"]),
        "total": _num(agg["regular"]) + _num(agg["overtime"]),
        "week_hours": _num(agg["week_regular"]) + _num(agg["week_overtime"]),
        "month_hours": _num(agg["month_regular"]) + _num(agg["month_overtime"]),
        "approved_hours": _num(agg["approved_regular"]) + _num(agg["approved_overtime"]),
        "drafts": agg["drafts"], "submitted": agg["submitted"],
        "approved": agg["approved"], "rejected": agg["rejected"],
    }
    base = mine.select_related("project")
    return {
        "stats": stats,
        "recent": base.order_by("-work_date", "-created_at")[:8],
        "needs_attention": base.filter(status__in=[Status.REJECTED, Status.DRAFT]).order_by("-work_date")[:5],
        "week_start": week_start,
        "week_end": week_end,
        "month_label": today.strftime("%B %Y"),
    }


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def _with_total(rows):
    for r in rows:
        r["regular"] = _num(r["regular"])
        r["overtime"] = _num(r["overtime"])
        r["total"] = r["regular"] + r["overtime"]
    return rows


def _period_rows(qs, key, limit):
    return _with_total(list(
        qs.values(key).annotate(regular=Sum("regular_hours"), overtime=Sum("overtime_hours"), entries=Count("id"))
        .order_by(f"-{key}")[:limit]
    ))


def report_data(qs):
    """``qs`` must be an UNORDERED, already-filtered TimesheetEntry queryset."""
    totals_raw = qs.aggregate(
        entries=Count("id"), regular=Sum("regular_hours"), overtime=Sum("overtime_hours"),
    )
    totals = {
        "entries": totals_raw["entries"],
        "regular": _num(totals_raw["regular"]),
        "overtime": _num(totals_raw["overtime"]),
    }
    totals["total"] = totals["regular"] + totals["overtime"]

    by_employee = _with_total(list(
        qs.values(
            "employee_id", "employee__first_name", "employee__last_name", "employee__username",
            "employee__profile__employee_id", "employee__profile__department",
        )
        .annotate(entries=Count("id"), regular=Sum("regular_hours"), overtime=Sum("overtime_hours"))
        .order_by("employee__last_name", "employee__first_name", "employee_id")
    ))
    for r in by_employee:
        r["name"] = _display_name(r["employee__first_name"], r["employee__last_name"], r["employee__username"])
        r["code"] = r["employee__profile__employee_id"] or ""
        r["department"] = r["employee__profile__department"] or ""

    by_project = _with_total(list(
        qs.values("project_id", "project__name", "project__code")
        .annotate(entries=Count("id"), regular=Sum("regular_hours"), overtime=Sum("overtime_hours"))
        .order_by("project__name")
    ))
    for r in by_project:
        r["name"] = r["project__name"] or "No project"
        r["code"] = r["project__code"] or ""

    weekly = _with_total(list(
        qs.annotate(week=TruncWeek("work_date")).values("week")
        .annotate(entries=Count("id"), regular=Sum("regular_hours"), overtime=Sum("overtime_hours"))
        .order_by("-week")[:26]
    ))
    monthly = _with_total(list(
        qs.annotate(month=TruncMonth("work_date")).values("month")
        .annotate(entries=Count("id"), regular=Sum("regular_hours"), overtime=Sum("overtime_hours"))
        .order_by("-month")[:24]
    ))
    daily = _period_rows(qs, "work_date", 62)

    status_rows = {
        r["status"]: r for r in qs.values("status").annotate(
            entries=Count("id"), regular=Sum("regular_hours"), overtime=Sum("overtime_hours"))
    }
    by_status = []
    for s in Status:
        r = status_rows.get(s.value, {"entries": 0, "regular": ZERO, "overtime": ZERO})
        regular, overtime = _num(r["regular"]), _num(r["overtime"])
        by_status.append({"status": s.value, "label": s.label, "entries": r["entries"],
                          "regular": regular, "overtime": overtime, "total": regular + overtime})
    lookup = {r["status"]: r for r in by_status}
    return {
        "totals": totals,
        "by_employee": by_employee,
        "by_project": by_project,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "by_status": by_status,
        "approved_hours": lookup["approved"]["total"],
        "pending_hours": lookup["submitted"]["total"],
        "draft_hours": lookup["draft"]["total"],
        "rejected_count": lookup["rejected"]["entries"],
        "rejected_hours": lookup["rejected"]["total"],
    }
