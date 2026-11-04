from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from .exports import write_entries_csv
from .access import accessible_projects
from .forms import EmployeeForm, EntryFilterForm, LoginForm, ProjectForm, AccessForm, ProjectTaskFormSet, ReviewForm, TilReviewForm, TimeInLieuForm, TimeInLieuRequestForm, TimesheetForm
from .models import AuditLog, EmployeeProfile, Project, TimeInLieuEntry, TimeInLieuRequest, TimesheetEntry
from .permissions import (
    admin_required,
    can_delete_entry,
    can_edit_entry,
    can_submit_entry,
    is_admin,
)
from .queries import (
    admin_dashboard_data,
    apply_filters,
    employee_dashboard_data,
    report_data,
    visible_entries,
)
from .til import (
    TilRequestError, approve_request, balance_for, balances_queryset, cancel_request,
    pending_requests_count, reject_request, submit_request,
)
from .services import TransitionError, change_status, log_audit, perform_transition

User = get_user_model()
Status = TimesheetEntry.Status


class AppLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


def _safe_next(request, fallback):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return fallback


def _get_entry_or_404(request, pk):
    # Employees only ever query their own rows, so another employee's ID is a 404.
    return get_object_or_404(visible_entries(request.user), pk=pk)


def _paginate(request, queryset, per_page=None):
    paginator = Paginator(queryset, per_page or settings.PAGE_SIZE)
    return paginator.get_page(request.GET.get("page"))


# --------------------------------------------------------------------------
# Dashboards
# --------------------------------------------------------------------------
@login_required
def dashboard(request):
    if is_admin(request.user):
        return render(request, "timesheets/dashboard_admin.html", admin_dashboard_data())
    data = employee_dashboard_data(request.user)
    data["til_balance"] = balance_for(request.user)[2]
    return render(request, "timesheets/dashboard_employee.html", data)


# --------------------------------------------------------------------------
# Timesheets
# --------------------------------------------------------------------------
@login_required
def timesheet_list(request):
    admin_mode = is_admin(request.user)
    form = EntryFilterForm(request.GET, admin_mode=admin_mode, user=request.user)
    form.is_valid()  # invalid fields are simply left out of cleaned_data
    entries = apply_filters(visible_entries(request.user), form.cleaned_data)
    totals = entries.aggregate(
        count=Count("id"), regular=Sum("regular_hours"), overtime=Sum("overtime_hours"),
    )
    regular = totals["regular"] or 0
    overtime = totals["overtime"] or 0
    page = _paginate(request, entries)
    return render(request, "timesheets/timesheet_list.html", {
        "form": form,
        "page": page,
        "entries": page.object_list,
        "count": totals["count"],
        "total_regular": regular,
        "total_overtime": overtime,
        "total_hours": regular + overtime,
        "filtered": form.is_filtered,
    })


@login_required
def timesheet_detail(request, pk):
    entry = _get_entry_or_404(request, pk)
    admin_mode = is_admin(request.user)
    return render(request, "timesheets/timesheet_detail.html", {
        "entry": entry,
        "history": entry.approval_history.select_related("reviewer"),
        "can_edit": can_edit_entry(request.user, entry),
        "can_delete": can_delete_entry(request.user, entry),
        "can_submit": can_submit_entry(request.user, entry),
        "can_approve": admin_mode and entry.status == Status.SUBMITTED,
        "can_reject": admin_mode and entry.status in (Status.SUBMITTED, Status.APPROVED),
        "audit": (
            AuditLog.objects.filter(model_name="TimesheetEntry", object_id=str(entry.pk))[:10]
            if admin_mode else []
        ),
    })


def _changed_labels(form):
    labels = []
    for name in form.changed_data:
        if name == "hours_mode":
            continue
        field = form.fields.get(name)
        labels.append(str(field.label or name) if field else name)
    return labels


@login_required
def timesheet_create(request):
    admin_mode = is_admin(request.user)
    instance = TimesheetEntry() if admin_mode else TimesheetEntry(employee=request.user)
    form = TimesheetForm(request.POST or None, instance=instance, user=request.user)

    if request.method == "POST" and form.is_valid():
        entry = form.save(commit=False)
        if admin_mode:
            desired = form.cleaned_data.get("status") or Status.DRAFT
        else:
            entry.employee = request.user
            desired = Status.SUBMITTED if request.POST.get("action") == "submit" else Status.DRAFT
        entry.created_by = request.user
        entry.status = Status.DRAFT
        with transaction.atomic():
            entry.save()
            log_audit(request.user, AuditLog.Action.ENTRY_CREATED, entry,
                      details=f"{entry.total_hours} hours on {entry.work_date}", request=request)
            if desired != Status.DRAFT:
                comment = "Set by administrator when the entry was created." if admin_mode else ""
                change_status(entry, desired, request.user, comment=comment, request=request)
        if desired == Status.SUBMITTED and not admin_mode:
            messages.success(request, "Timesheet saved and submitted for approval.")
        else:
            messages.success(request, "Timesheet entry saved.")
        return redirect("timesheet_detail", pk=entry.pk)

    if request.method == "POST":
        messages.error(request, "Please correct the highlighted fields and try again.")
    return render(request, "timesheets/timesheet_form.html", {
        "form": form, "entry": None, "admin_mode": admin_mode,
        "standard_hours": settings.STANDARD_DAILY_HOURS,
    })


@login_required
def timesheet_edit(request, pk):
    entry = _get_entry_or_404(request, pk)
    admin_mode = is_admin(request.user)
    if not can_edit_entry(request.user, entry):
        messages.error(
            request,
            "This timesheet is locked because it has been "
            f"{entry.get_status_display().lower()}. Only an administrator can change it.",
        )
        return redirect("timesheet_detail", pk=entry.pk)

    original_status = entry.status
    form = TimesheetForm(request.POST or None, instance=entry, user=request.user)

    if request.method == "POST" and form.is_valid():
        changed = _changed_labels(form)
        entry = form.save(commit=False)
        if admin_mode:
            desired = form.cleaned_data.get("status") or original_status
        else:
            if request.POST.get("action") == "submit":
                desired = Status.SUBMITTED
            else:
                desired = Status.DRAFT if original_status == Status.REJECTED else original_status
        entry.status = original_status
        with transaction.atomic():
            entry.save()
            if changed:
                log_audit(request.user, AuditLog.Action.ENTRY_UPDATED, entry,
                          details="Changed: " + ", ".join(changed), request=request)
            if desired != original_status:
                comment = "Changed by administrator while editing." if admin_mode else ""
                change_status(entry, desired, request.user, comment=comment, request=request)
        if desired == Status.SUBMITTED and original_status != Status.SUBMITTED:
            messages.success(request, "Timesheet updated and submitted for approval.")
        else:
            messages.success(request, "Timesheet entry updated.")
        return redirect("timesheet_detail", pk=entry.pk)

    if request.method == "POST":
        messages.error(request, "Please correct the highlighted fields and try again.")
    return render(request, "timesheets/timesheet_form.html", {
        "form": form, "entry": entry, "admin_mode": admin_mode,
        "standard_hours": settings.STANDARD_DAILY_HOURS,
    })


@login_required
def timesheet_delete(request, pk):
    entry = _get_entry_or_404(request, pk)
    if not can_delete_entry(request.user, entry):
        messages.error(request, "You do not have permission to delete this timesheet. "
                                "Employees can only delete their own drafts.")
        return redirect("timesheet_detail", pk=entry.pk)

    if request.method == "POST":
        summary = (f"{entry.employee_name}, {entry.work_date}, {entry.total_hours} hours, "
                   f"status {entry.get_status_display().lower()}")
        with transaction.atomic():
            log_audit(request.user, AuditLog.Action.ENTRY_DELETED, entry, details=summary, request=request)
            entry.delete()
        messages.success(request, "Timesheet entry deleted.")
        return redirect("timesheet_list")
    return render(request, "timesheets/timesheet_confirm_delete.html", {"entry": entry})


@login_required
@require_POST
def timesheet_submit(request, pk):
    entry = _get_entry_or_404(request, pk)
    if not can_submit_entry(request.user, entry):
        if entry.status in (Status.DRAFT, Status.REJECTED):
            raise PermissionDenied("You cannot submit this timesheet.")
        messages.error(request, "This timesheet has already been submitted.")
        return redirect("timesheet_detail", pk=entry.pk)
    try:
        perform_transition(entry, "submit", request.user, request=request)
    except TransitionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Timesheet submitted for approval.")
    return redirect(_safe_next(request, reverse("timesheet_detail", args=[entry.pk])))


def _review(request, pk, action):
    entry = _get_entry_or_404(request, pk)
    form = ReviewForm(request.POST)
    comment = form.cleaned_data["comment"].strip() if form.is_valid() else ""
    try:
        perform_transition(entry, action, request.user, comment=comment, request=request)
    except TransitionError as exc:
        messages.error(request, str(exc))
    else:
        verb = "approved" if action == "approve" else "rejected"
        messages.success(request, f"Timesheet for {entry.employee_name} on {entry.work_date} {verb}.")
    return redirect(_safe_next(request, reverse("timesheet_detail", args=[entry.pk])))


@admin_required
@require_POST
def timesheet_approve(request, pk):
    return _review(request, pk, "approve")


@admin_required
@require_POST
def timesheet_reject(request, pk):
    return _review(request, pk, "reject")


# --------------------------------------------------------------------------
# Employee management (administrators only)
# --------------------------------------------------------------------------
def _employee_queryset():
    return User.objects.select_related("profile").annotate(
        total_regular=Sum("timesheet_entries__regular_hours"),
        total_overtime=Sum("timesheet_entries__overtime_hours"),
        total_hours=Sum(
            F("timesheet_entries__regular_hours") + F("timesheet_entries__overtime_hours"),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        ),
        entry_count=Count("timesheet_entries"),
    )


@admin_required
def employee_list(request):
    users = _employee_queryset()
    q = (request.GET.get("q") or "").strip()
    state = request.GET.get("state", "")
    department = (request.GET.get("department") or "").strip()
    role = request.GET.get("role", "")
    for token in q.split()[:5]:
        users = users.filter(
            Q(first_name__icontains=token) | Q(last_name__icontains=token)
            | Q(username__icontains=token) | Q(email__icontains=token)
            | Q(profile__employee_id__icontains=token) | Q(profile__job_title__icontains=token)
        )
    if state == "active":
        users = users.filter(is_active=True)
    elif state == "inactive":
        users = users.filter(is_active=False)
    if role == "staff":
        users = users.filter(Q(is_staff=True) | Q(is_superuser=True))
    elif role == "employee":
        users = users.filter(is_staff=False, is_superuser=False)
    else:
        role = ""
    if department:
        users = users.filter(profile__department__iexact=department)
    users = users.order_by("last_name", "first_name", "username")
    departments = (
        EmployeeProfile.objects.exclude(department="").order_by("department")
        .values_list("department", flat=True).distinct()
    )
    page = _paginate(request, users)
    return render(request, "timesheets/employee_list.html", {
        "page": page, "employees": page.object_list, "q": q, "state": state,
        "department": department, "departments": departments, "role": role,
        "filtered": bool(q or state or department or role),
    })


@admin_required
def employee_create(request):
    form = EmployeeForm(request.POST or None, creating=True)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        log_audit(request.user, AuditLog.Action.EMPLOYEE_CREATED, user,
                  details=f"Employee ID {user.profile.employee_id}", request=request)
        messages.success(request, f"Employee account created for {user.get_full_name() or user.username}.")
        return redirect("employee_detail", pk=user.pk)
    if request.method == "POST":
        messages.error(request, "Please correct the highlighted fields and try again.")
    return render(request, "timesheets/employee_form.html", {"form": form, "employee": None})


@admin_required
def staff_create(request):
    """Create a staff-level administrator account (is_staff, never superuser)."""
    form = EmployeeForm(request.POST or None, creating=True, staff=True)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        log_audit(request.user, AuditLog.Action.EMPLOYEE_CREATED, user,
                  details=f"Staff (administrator) account. Employee ID {user.profile.employee_id}",
                  request=request)
        messages.success(request, f"Staff account created for {user.get_full_name() or user.username}. "
                                  "They can now sign in as an administrator.")
        return redirect("employee_detail", pk=user.pk)
    if request.method == "POST":
        messages.error(request, "Please correct the highlighted fields and try again.")
    return render(request, "timesheets/employee_form.html",
                  {"form": form, "employee": None, "staff_mode": True})


@admin_required
def employee_detail(request, pk):
    employee = get_object_or_404(_employee_queryset(), pk=pk)
    entries = TimesheetEntry.objects.filter(employee=employee)
    status_counts = {row["status"]: row["n"] for row in entries.values("status").annotate(n=Count("id"))}
    approved = entries.filter(status=Status.APPROVED).aggregate(
        r=Sum("regular_hours"), o=Sum("overtime_hours"))
    return render(request, "timesheets/employee_detail.html", {
        "employee": employee,
        "profile": employee.profile,
        "total_hours": employee.total_hours or 0,
        "approved_hours": (approved["r"] or 0) + (approved["o"] or 0),
        "status_counts": [(s.label, s.value, status_counts.get(s.value, 0)) for s in Status],
        "recent": entries.select_related("project").order_by("-work_date", "-created_at")[:8],
        "audit": AuditLog.objects.filter(
            Q(model_name="User", object_id=str(employee.pk)) | Q(actor=employee)
        ).exclude(action__in=[AuditLog.Action.LOGIN, AuditLog.Action.LOGOUT])[:8],
        "is_self": employee.pk == request.user.pk,
        "til_balance": balance_for(employee)[2],
    })


@admin_required
def employee_edit(request, pk):
    employee = get_object_or_404(User.objects.select_related("profile"), pk=pk)
    form = EmployeeForm(request.POST or None, instance=employee)
    if request.method == "POST" and form.is_valid():
        if employee.pk == request.user.pk and not form.cleaned_data.get("is_active"):
            form.add_error("is_active", "You cannot deactivate your own account.")
        else:
            was_active = User.objects.values_list("is_active", flat=True).get(pk=employee.pk)
            changed = [str(form.fields[n].label or n) for n in form.changed_data if n not in ("password1", "password2")]
            user = form.save()
            if form.cleaned_data.get("password1"):
                log_audit(request.user, AuditLog.Action.PASSWORD_CHANGED, user, request=request)
            if changed:
                log_audit(request.user, AuditLog.Action.EMPLOYEE_UPDATED, user,
                          details="Changed: " + ", ".join(changed), request=request)
            if was_active != user.is_active:
                action = (AuditLog.Action.EMPLOYEE_ACTIVATED if user.is_active
                          else AuditLog.Action.EMPLOYEE_DEACTIVATED)
                log_audit(request.user, action, user, request=request)
            messages.success(request, "Employee details updated.")
            return redirect("employee_detail", pk=user.pk)
    if request.method == "POST":
        messages.error(request, "Please correct the highlighted fields and try again.")
    return render(request, "timesheets/employee_form.html", {"form": form, "employee": employee})


@admin_required
@require_POST
def employee_toggle_active(request, pk):
    employee = get_object_or_404(User, pk=pk)
    if employee.pk == request.user.pk:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("employee_detail", pk=employee.pk)
    employee.is_active = not employee.is_active
    employee.save(update_fields=["is_active"])
    action = AuditLog.Action.EMPLOYEE_ACTIVATED if employee.is_active else AuditLog.Action.EMPLOYEE_DEACTIVATED
    log_audit(request.user, action, employee, request=request)
    if employee.is_active:
        messages.success(request, f"{employee.get_full_name() or employee.username} can sign in again.")
    else:
        messages.success(
            request,
            f"{employee.get_full_name() or employee.username} has been deactivated and can no longer "
            "sign in. Their timesheet history is preserved.",
        )
    return redirect(_safe_next(request, reverse("employee_detail", args=[employee.pk])))


# --------------------------------------------------------------------------
# Project management (administrators only)
# --------------------------------------------------------------------------
@admin_required
def project_list(request):
    projects = Project.objects.annotate(
        entry_count=Count("entries"),
        total_hours=Sum(
            F("entries__regular_hours") + F("entries__overtime_hours"),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        ),
    )
    q = (request.GET.get("q") or "").strip()
    status = request.GET.get("status", "")
    for token in q.split()[:5]:
        projects = projects.filter(
            Q(name__icontains=token) | Q(code__icontains=token)
        )
    if status in Project.Status.values:
        projects = projects.filter(status=status)
    else:
        status = ""
    page = _paginate(request, projects.order_by("name"))
    return render(request, "timesheets/project_list.html", {
        "page": page, "projects": page.object_list, "q": q, "status": status,
        "status_choices": Project.Status.choices, "filtered": bool(q or status),
    })


@admin_required
def project_create(request):
    form = ProjectForm(request.POST or None)
    tasks = ProjectTaskFormSet(request.POST or None, instance=Project())
    if request.method == "POST" and form.is_valid() and tasks.is_valid():
        with transaction.atomic():
            project = form.save()
            tasks.instance = project
            tasks.save()
        log_audit(request.user, AuditLog.Action.PROJECT_CREATED, project, request=request)
        messages.success(request, f"Project {project.name} ({project.code}) created.")
        return redirect("project_list")
    if request.method == "POST":
        messages.error(request, "Please correct the highlighted fields and try again.")
    return render(request, "timesheets/project_form.html", {"form": form, "tasks": tasks, "project": None})


@admin_required
def project_edit(request, pk):
    project = get_object_or_404(Project.objects.annotate(entry_count=Count("entries")), pk=pk)
    form = ProjectForm(request.POST or None, instance=project)
    tasks = ProjectTaskFormSet(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid() and tasks.is_valid():
        changed = [str(form.fields[n].label or n) for n in form.changed_data]
        if tasks.has_changed():
            changed.append("Task checklist")
        with transaction.atomic():
            project = form.save()
            tasks.save()
        if changed:
            log_audit(request.user, AuditLog.Action.PROJECT_UPDATED, project,
                      details="Changed: " + ", ".join(changed), request=request)
        messages.success(request, f"Project {project.name} updated.")
        return redirect("project_list")
    if request.method == "POST":
        messages.error(request, "Please correct the highlighted fields and try again.")
    return render(request, "timesheets/project_form.html", {"form": form, "tasks": tasks, "project": project})


@admin_required
@require_POST
def project_delete(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if project.entries.exists():
        messages.error(request, f"{project.name} has timesheet entries, so it cannot be deleted. "
                                "Set its stage status to Archived instead.")
        return redirect("project_edit", pk=project.pk)
    label = str(project)
    log_audit(request.user, AuditLog.Action.PROJECT_DELETED, project, request=request)
    project.delete()
    messages.success(request, f"Project {label} deleted.")
    return redirect("project_list")


# --------------------------------------------------------------------------
# Access management (administrators only)
# --------------------------------------------------------------------------
@admin_required
def access_list(request):
    q = (request.GET.get("q") or "").strip()
    people = User.objects.select_related("profile").annotate(
        project_count=Count("project_access", distinct=True),
        section_count=Count("section_access", distinct=True),
    ).filter(is_active=True)
    for token in q.split()[:5]:
        people = people.filter(
            Q(first_name__icontains=token) | Q(last_name__icontains=token)
            | Q(username__icontains=token) | Q(profile__employee_id__icontains=token)
        )
    return render(request, "timesheets/access_list.html", {
        "people": people.order_by("-is_staff", "last_name", "first_name", "username"),
        "q": q, "nav_section": "access",
    })


@admin_required
def access_edit(request, pk):
    person = get_object_or_404(User.objects.select_related("profile"), pk=pk)
    full_access = bool(person.is_staff or person.is_superuser)
    form = None if full_access else AccessForm(request.POST or None, person=person)
    if form is not None and request.method == "POST":
        if form.is_valid():
            summary = form.save()
            if summary:
                log_audit(request.user, AuditLog.Action.ACCESS_UPDATED, person, details=summary, request=request)
            messages.success(request, f"Access updated for {person.get_full_name() or person.username}. "
                                      "It applies immediately.")
            return redirect("access_list")
        messages.error(request, "Please correct the highlighted fields and try again.")
    return render(request, "timesheets/access_edit.html", {
        "person": person, "form": form, "full_access": full_access, "nav_section": "access",
    })


# --------------------------------------------------------------------------
# My projects (employees see only what is assigned to them)
# --------------------------------------------------------------------------
@login_required
def my_projects(request):
    projects = accessible_projects(request.user).prefetch_related("tasks").order_by("name")
    return render(request, "timesheets/my_projects.html", {"projects": projects, "nav_section": "myprojects"})


@login_required
def my_project_detail(request, pk):
    # Anything not assigned is a 404, so the URL does not even confirm it exists.
    project = get_object_or_404(accessible_projects(request.user).prefetch_related("tasks"), pk=pk)
    mine = TimesheetEntry.objects.filter(employee=request.user, project=project)
    totals = mine.aggregate(r=Sum("regular_hours"), o=Sum("overtime_hours"))
    return render(request, "timesheets/my_project_detail.html", {
        "project": project, "total_hours": (totals["r"] or 0) + (totals["o"] or 0),
        "recent": mine.order_by("-work_date")[:8], "nav_section": "myprojects",
    })


# --------------------------------------------------------------------------
# Time in Lieu (TIL): banked hours
# --------------------------------------------------------------------------
@login_required
def til_overview(request):
    if not is_admin(request.user):
        return redirect("til_ledger", pk=request.user.pk)
    q = (request.GET.get("q") or "").strip()
    people = balances_queryset().select_related("profile").filter(is_active=True)
    for token in q.split()[:5]:
        people = people.filter(
            Q(first_name__icontains=token) | Q(last_name__icontains=token)
            | Q(username__icontains=token) | Q(profile__employee_id__icontains=token)
        )
    people = list(people.order_by("last_name", "first_name", "username"))
    zero = Decimal("0")
    for p in people:
        p.til_earned = p.til_earned or zero
        p.til_used = p.til_used or zero
        p.til_balance = p.til_earned - p.til_used
    return render(request, "timesheets/til_overview.html", {
        "people": people, "q": q,
        "total_balance": sum((p.til_balance for p in people), zero),
        "nav_section": "til",
    })


@login_required
def til_ledger(request, pk):
    person = get_object_or_404(User.objects.select_related("profile"), pk=pk)
    admin_mode = is_admin(request.user)
    is_self = person.pk == request.user.pk
    if not admin_mode and not is_self:
        raise PermissionDenied("You can only view your own time in lieu.")

    is_post = request.method == "POST"
    posting_request = is_post and request.POST.get("action") == "request"
    form = request_form = None
    if admin_mode:
        form = TimeInLieuForm(request.POST if (is_post and not posting_request) else None, employee=person)
    if is_self:
        request_form = TimeInLieuRequestForm(request.POST if posting_request else None, employee=person)

    if posting_request:
        if request_form is None:
            raise PermissionDenied("You can only request time in lieu for yourself.")
        if request_form.is_valid():
            data = request_form.cleaned_data
            submit_request(person, data["kind"], data["work_date"], data["hours"], data["reason"], request=request)
            messages.success(request, "Request sent. Your balance changes once an administrator approves it.")
            return redirect("til_ledger", pk=person.pk)
        messages.error(request, "Please correct the highlighted fields and try again.")
    elif is_post and admin_mode:
        if form.is_valid():
            entry = form.save(commit=False)
            entry.employee, entry.recorded_by = person, request.user
            entry.save()
            log_audit(request.user, AuditLog.Action.TIL_RECORDED, entry,
                      details=f"{person.get_username()}: {entry.get_kind_display()} {entry.hours} h",
                      request=request)
            messages.success(request, "Time in lieu recorded.")
            return redirect("til_ledger", pk=person.pk)
        messages.error(request, "Please correct the highlighted fields and try again.")

    earned, used, balance = balance_for(person)
    page = _paginate(request, person.til_entries.select_related("recorded_by"))
    til_requests = list(person.til_requests.select_related("reviewed_by")[:10])
    pending = [r for r in til_requests if r.status == TimeInLieuRequest.Status.PENDING]
    pending_earned = sum((r.hours for r in pending if r.kind == TimeInLieuRequest.Kind.EARNED), Decimal("0"))
    pending_taken = sum((r.hours for r in pending if r.kind == TimeInLieuRequest.Kind.TAKEN), Decimal("0"))
    return render(request, "timesheets/til_ledger.html", {
        "person": person, "form": form, "request_form": request_form,
        "page": page, "entries": page.object_list,
        "earned": earned, "used": used, "balance": balance,
        "til_requests": til_requests, "pending_earned": pending_earned, "pending_taken": pending_taken,
        "can_review": admin_mode and not is_self,
        "is_self": is_self,
        "nav_section": "til",
    })


@admin_required
def til_requests(request):
    """Administrator queue of requests to bank Time in Lieu."""
    status = request.GET.get("status") or TimeInLieuRequest.Status.PENDING
    qs = TimeInLieuRequest.objects.select_related("employee", "employee__profile", "reviewed_by")
    if status != "all":
        if status not in TimeInLieuRequest.Status.values:
            status = TimeInLieuRequest.Status.PENDING
        qs = qs.filter(status=status)
    if status == TimeInLieuRequest.Status.PENDING:
        qs = qs.order_by("created_at", "id")  # oldest first so nothing is forgotten
    page = _paginate(request, qs)
    return render(request, "timesheets/til_requests.html", {
        "page": page, "requests_list": page.object_list, "status": status,
        "statuses": TimeInLieuRequest.Status.choices,
        "pending_total": pending_requests_count(),
        "nav_section": "til",
    })


def _til_request_action(request, pk, action):
    form = TilReviewForm(request.POST)
    comment = form.cleaned_data["comment"] if form.is_valid() else ""
    try:
        action(pk, request.user, comment, request=request)
    except TimeInLieuRequest.DoesNotExist:
        raise Http404("No such request.")
    except TilRequestError as exc:
        messages.error(request, str(exc))
        return False
    return True


@admin_required
@require_POST
def til_request_approve(request, pk):
    if _til_request_action(request, pk, approve_request):
        messages.success(request, "Request approved and the balance was updated.")
    return redirect(_safe_next(request, reverse("til_requests")))


@admin_required
@require_POST
def til_request_reject(request, pk):
    if _til_request_action(request, pk, reject_request):
        messages.success(request, "Request rejected. The balance was not changed.")
    return redirect(_safe_next(request, reverse("til_requests")))


@login_required
@require_POST
def til_request_cancel(request, pk):
    try:
        cancel_request(pk, request.user, request=request)
        messages.success(request, "Request cancelled.")
    except TimeInLieuRequest.DoesNotExist:
        raise Http404("No such request.")
    except TilRequestError as exc:
        messages.error(request, str(exc))
    return redirect("til_ledger", pk=request.user.pk)


@admin_required
@require_POST
def til_delete(request, pk):
    entry = get_object_or_404(TimeInLieuEntry, pk=pk)
    person = entry.employee
    log_audit(request.user, AuditLog.Action.TIL_DELETED, entry,
              details=f"{person.get_username()}: {entry.get_kind_display()} {entry.hours} h", request=request)
    entry.delete()
    messages.success(request, "Time in lieu entry deleted.")
    return redirect("til_ledger", pk=person.pk)


# --------------------------------------------------------------------------
# Reports and CSV export (administrators only)
# --------------------------------------------------------------------------
@admin_required
def reports(request):
    form = EntryFilterForm(request.GET, admin_mode=True)
    form.is_valid()
    base = TimesheetEntry.objects.all()
    filtered_qs = apply_filters(base, form.cleaned_data, ordered=False)
    data = report_data(filtered_qs)
    detail = apply_filters(visible_entries(request.user), form.cleaned_data)
    page = _paginate(request, detail, per_page=25)

    params = request.GET.copy()
    params.pop("page", None)
    params.pop("sort", None)
    return render(request, "timesheets/reports.html", {
        "form": form,
        "data": data,
        "page": page,
        "entries": page.object_list,
        "export_query": params.urlencode(),
        "filtered": form.is_filtered,
        "form_valid": form.is_valid(),
    })


@admin_required
@require_GET
def report_export(request):
    form = EntryFilterForm(request.GET, admin_mode=True)
    if not form.is_valid():
        # Never export "everything" when the requested filters were not understood.
        messages.error(request, "The export filters are not valid, so nothing was exported. "
                                "Please fix the highlighted filters first.")
        return redirect(f"{reverse('reports')}?{request.GET.urlencode()}")

    queryset = apply_filters(visible_entries(request.user), form.cleaned_data)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="timesheets-{stamp}.csv"'
    response.write("﻿")  # UTF-8 byte-order mark so spreadsheet apps read accents correctly
    rows = write_entries_csv(response, queryset)
    filters = {k: str(v) for k, v in form.cleaned_data.items() if v and k != "sort"}
    log_audit(request.user, AuditLog.Action.CSV_EXPORT, details=f"{rows} rows; filters: {filters or 'none'}",
              request=request)
    return response
