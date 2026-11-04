from django.conf import settings

from .models import TimeInLieuRequest, TimesheetEntry
from .permissions import is_admin


def app_context(request):
    """Values every template needs: role flag, active nav section, pending badge."""
    user = getattr(request, "user", None)
    admin_user = is_admin(user)

    match = getattr(request, "resolver_match", None)
    name = (match.url_name if match and match.url_name else "") or ""
    if name == "dashboard":
        section = "dashboard"
    elif name == "timesheet_create":
        section = "create"
    elif name.startswith("timesheet"):
        status = request.GET.get("status", "")
        section = "approvals" if (admin_user and status == "submitted" and name == "timesheet_list") else "timesheets"
    elif name.startswith("employee") or name.startswith("staff"):
        section = "employees"
    elif name.startswith("project"):
        section = "projects"
    elif name.startswith("report"):
        section = "reports"
    else:
        section = ""

    pending = til_pending = 0
    if admin_user:
        pending = TimesheetEntry.objects.filter(status=TimesheetEntry.Status.SUBMITTED).count()
        til_pending = TimeInLieuRequest.objects.filter(status=TimeInLieuRequest.Status.PENDING).count()

    return {
        "is_admin_user": admin_user,
        "nav_section": section,
        "pending_count": pending,
        "til_pending_count": til_pending,
        "app_name": "Formery Timesheet Database",
        "time_zone": settings.TIME_ZONE,
    }
