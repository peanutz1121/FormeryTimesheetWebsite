"""
Database design notes
---------------------
* ``EmployeeProfile`` extends Django's built-in User (one-to-one). Whether an
  account is active is stored once, on ``User.is_active``, and exposed on the
  profile as a read-only property - this avoids two flags drifting apart.
* ``TimesheetEntry.employee`` uses ``on_delete=PROTECT``. An employee who has
  timesheets can therefore never be deleted by accident; accounts are
  *deactivated* instead, which keeps all history intact.
* ``Project`` is protected from deletion while entries reference it.
* ``created_by`` / ``reviewed_by`` / audit actors use SET_NULL so removing an
  administrator account never removes historical records.
* ``ApprovalHistory`` rows belong to an entry (CASCADE). ``AuditLog`` rows are
  deliberately NOT linked by foreign key to what they describe, so the audit
  trail survives even when an entry is deleted.
* Hours are DecimalFields (never floats) and CHECK constraints keep values in
  a sane range at the database level, in addition to form/model validation.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q

from .hours import (
    ZERO,
    calculate_total_hours,
    shift_minutes,
)

User = settings.AUTH_USER_MODEL

HOURS_VALIDATORS = [MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("24"))]


class EmployeeProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    employee_id = models.CharField(
        max_length=20, unique=True, blank=True,
        help_text="Unique staff number. Leave blank to auto-generate (EMP-0001, ...).",
    )
    department = models.CharField(max_length=100, blank=True)
    job_title = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__last_name", "user__first_name", "user__username"]
        indexes = [models.Index(fields=["department"])]

    def __str__(self):
        return f"{self.employee_id} - {self.full_name}"

    @property
    def is_active(self):
        return self.user.is_active

    @property
    def full_name(self):
        return self.user.get_full_name() or self.user.get_username()

    @staticmethod
    def generate_employee_id():
        number = EmployeeProfile.objects.count() + 1
        while EmployeeProfile.objects.filter(employee_id=f"EMP-{number:04d}").exists():
            number += 1
        return f"EMP-{number:04d}"

    def save(self, *args, **kwargs):
        if not self.employee_id:
            self.employee_id = self.generate_employee_id()
        super().save(*args, **kwargs)


class Project(models.Model):
    class Status(models.TextChoices):
        """Architecture project stage (shown in the UI as "Stage Status")."""
        PRE_DESIGN = "pre_design", "Pre-Design / Programming"
        SCHEMATIC = "schematic", "Schematic Design"
        DESIGN_DEV = "design_development", "Design Development"
        CONSTRUCTION_DOCS = "construction_documents", "Construction Documents"
        PERMITTING_BIDDING = "permitting_bidding", "Permitting & Bidding"
        CONSTRUCTION_ADMIN = "construction_administration", "Construction Administration"
        COMPLETED = "completed", "Completed"
        ON_HOLD = "on_hold", "On hold"
        ARCHIVED = "archived", "Archived"

    class Section(models.TextChoices):
        """Where a project sits. The last three are restricted sections."""
        PROJECT = "project", "Architecture projects"
        ADMIN = "admin", "Admin"
        MARKETING_SOCIAL = "marketing_social", "Marketing & Social"
        NETWORKING = "networking", "Networking"

    # Sections hidden from regular employees unless an administrator grants access.
    RESTRICTED_SECTIONS = (Section.ADMIN, Section.MARKETING_SOCIAL, Section.NETWORKING)

    # Stages in which time can no longer be logged against a project.
    CLOSED_STAGES = (Status.COMPLETED, Status.ON_HOLD, Status.ARCHIVED)

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30, unique=True)
    section = models.CharField(max_length=20, choices=Section.choices, default=Section.PROJECT)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        "stage status", max_length=30, choices=Status.choices, default=Status.SCHEMATIC,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["status"]), models.Index(fields=["section"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(start_date__isnull=True)
                | Q(end_date__isnull=True)
                | Q(end_date__gte=F("start_date")),
                name="project_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before the start date."})


class ProjectTask(models.Model):
    """One item in a project's task checklist (maintained by administrators)."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    name = models.CharField(max_length=200)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class TimesheetEntry(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class HoursSource(models.TextChoices):
        MANUAL = "manual", "Entered manually"
        CALCULATED = "calculated", "Calculated from times"

    employee = models.ForeignKey(User, on_delete=models.PROTECT, related_name="timesheet_entries")
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True, related_name="entries",
    )
    work_date = models.DateField()
    task_description = models.TextField(max_length=1000)
    time_in = models.TimeField(null=True, blank=True)
    time_out = models.TimeField(null=True, blank=True)
    break_minutes = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(1440)],
        help_text="Unpaid break in minutes.",
    )
    regular_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=ZERO, validators=HOURS_VALIDATORS,
    )
    overtime_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=ZERO, validators=HOURS_VALIDATORS,
    )
    hours_source = models.CharField(
        max_length=12, choices=HoursSource.choices, default=HoursSource.MANUAL,
        help_text="Whether the hours were typed in or calculated from the times.",
    )
    work_location = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True, max_length=1000)
    rejection_reason = models.CharField(max_length=500, blank=True)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_timesheet_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_timesheet_entries",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-work_date", "-created_at"]
        verbose_name = "timesheet entry"
        verbose_name_plural = "timesheet entries"
        indexes = [
            models.Index(fields=["employee", "work_date"], name="ts_emp_date_idx"),
            models.Index(fields=["status", "work_date"], name="ts_status_date_idx"),
            models.Index(fields=["work_date"], name="ts_date_idx"),
            models.Index(fields=["project", "work_date"], name="ts_project_date_idx"),
            models.Index(fields=["created_at"], name="ts_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(regular_hours__gte=0, regular_hours__lte=24),
                name="ts_regular_hours_0_24",
            ),
            models.CheckConstraint(
                condition=Q(overtime_hours__gte=0, overtime_hours__lte=24),
                name="ts_overtime_hours_0_24",
            ),
            models.CheckConstraint(
                condition=Q(break_minutes__lte=1440),
                name="ts_break_minutes_max",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["draft", "submitted", "approved", "rejected"]),
                name="ts_status_valid",
            ),
        ]

    def __str__(self):
        return f"{self.employee_name} - {self.work_date:%Y-%m-%d}"

    # ----- convenience properties used by templates and exports -------------
    @property
    def employee_name(self):
        return self.employee.get_full_name() or self.employee.get_username()

    @property
    def employee_code(self):
        profile = getattr(self.employee, "profile", None)
        return profile.employee_id if profile else ""

    @property
    def total_hours(self):
        return (self.regular_hours or ZERO) + (self.overtime_hours or ZERO)

    @property
    def calculated_total(self):
        """Hours implied by time in/out and break, or None if not available."""
        if self.time_in is None or self.time_out is None:
            return None
        return calculate_total_hours(self.time_in, self.time_out, self.break_minutes)

    @property
    def differs_from_times(self):
        """True when recorded hours do not match what the times imply."""
        calculated = self.calculated_total
        return calculated is not None and calculated != self.total_hours

    @property
    def project_label(self):
        return self.project.name if self.project else ""

    # ----- validation -------------------------------------------------------
    def clean(self):
        errors = {}
        regular = self.regular_hours if self.regular_hours is not None else ZERO
        overtime = self.overtime_hours if self.overtime_hours is not None else ZERO
        break_minutes = self.break_minutes or 0

        if regular < 0:
            errors["regular_hours"] = "Regular hours cannot be negative."
        if overtime < 0:
            errors["overtime_hours"] = "Overtime hours cannot be negative."
        if "regular_hours" not in errors and "overtime_hours" not in errors:
            if regular + overtime > 24:
                errors["overtime_hours"] = (
                    "Regular plus overtime hours cannot exceed 24 hours in one day."
                )
            elif regular + overtime <= 0:
                errors["regular_hours"] = (
                    "Total hours must be greater than zero. Enter hours manually or "
                    "calculate them from time in and time out."
                )

        if self.time_in is not None and self.time_out is not None:
            minutes = shift_minutes(self.time_in, self.time_out)
            if minutes == 0:
                errors["time_out"] = "Time out must be different from time in."
            elif break_minutes >= minutes:
                errors["break_minutes"] = (
                    "The unpaid break must be shorter than the shift "
                    f"({minutes} minutes between time in and time out)."
                )
        elif (self.time_in is None) != (self.time_out is None):
            field = "time_out" if self.time_in is not None else "time_in"
            errors[field] = "Enter both time in and time out, or leave both blank."

        if errors:
            raise ValidationError(errors)


class ProjectAccess(models.Model):
    """An employee may use this project (administrators always may use every project)."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="project_access")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="access_grants")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "project"], name="uniq_project_access")]
        verbose_name_plural = "project access"

    def __str__(self):
        return f"{self.user_id} -> {self.project_id}"


class SectionAccess(models.Model):
    """An employee may open a restricted section (Admin, Marketing & Social, Networking)."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="section_access")
    section = models.CharField(max_length=20, choices=Project.Section.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "section"], name="uniq_section_access")]
        verbose_name_plural = "section access"

    def __str__(self):
        return f"{self.user_id} -> {self.section}"


class TimeInLieuEntry(models.Model):
    """
    One line in an employee's Time in Lieu (TIL) ledger. Hours are always stored
    as positive numbers; the kind decides whether they are added to or taken
    from the banked balance.
    """
    class Kind(models.TextChoices):
        EARNED = "earned", "Earned (banked)"
        TAKEN = "taken", "Taken (time off)"
        DEDUCTED = "deducted", "Deducted / expired"

    CREDIT_KINDS = (Kind.EARNED,)

    employee = models.ForeignKey(User, on_delete=models.PROTECT, related_name="til_entries")
    entry_date = models.DateField()
    kind = models.CharField(max_length=10, choices=Kind.choices)
    hours = models.DecimalField(
        max_digits=6, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(Decimal("999"))],
    )
    note = models.CharField(max_length=300, blank=True)
    recorded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="recorded_til_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-entry_date", "-id"]
        verbose_name = "time in lieu entry"
        verbose_name_plural = "time in lieu entries"
        indexes = [models.Index(fields=["employee", "entry_date"])]
        constraints = [
            models.CheckConstraint(condition=Q(hours__gt=0), name="til_hours_positive"),
            models.CheckConstraint(
                condition=Q(kind__in=["earned", "taken", "deducted"]), name="til_kind_valid",
            ),
        ]

    def __str__(self):
        return f"{self.employee_id}: {self.get_kind_display()} {self.hours}h on {self.entry_date}"

    @property
    def signed_hours(self):
        return self.hours if self.kind == self.Kind.EARNED else -self.hours


class TimeInLieuRequest(models.Model):
    """
    A request by an employee or staff member to bank earned Time in Lieu hours.
    Nothing is added to the balance until an administrator approves it; approval
    creates the matching ``TimeInLieuEntry`` (kind = earned).
    """
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    class Kind(models.TextChoices):
        EARNED = "earned", "Earned (banked)"
        TAKEN = "taken", "Taken (time off)"

    employee = models.ForeignKey(User, on_delete=models.PROTECT, related_name="til_requests")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.EARNED)
    work_date = models.DateField(help_text="Date worked (earned) or date of the time off (taken)")
    hours = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(Decimal("24"))],
    )
    reason = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_til_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comment = models.CharField(max_length=300, blank=True)
    entry = models.OneToOneField(
        TimeInLieuEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_request",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "time in lieu request"
        verbose_name_plural = "time in lieu requests"
        indexes = [
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(hours__gt=0), name="til_request_hours_positive"),
            models.CheckConstraint(condition=Q(kind__in=["earned", "taken"]), name="til_request_kind_valid"),
        ]

    def __str__(self):
        return f"{self.employee_id}: {self.get_kind_display()} {self.hours}h on {self.work_date} ({self.get_status_display()})"


class ApprovalHistory(models.Model):
    timesheet = models.ForeignKey(
        TimesheetEntry, on_delete=models.CASCADE, related_name="approval_history",
    )
    reviewer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approval_actions",
    )
    previous_status = models.CharField(max_length=12, choices=TimesheetEntry.Status.choices, blank=True)
    new_status = models.CharField(max_length=12, choices=TimesheetEntry.Status.choices)
    comments = models.CharField(max_length=500, blank=True)
    acted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["acted_at", "id"]
        verbose_name = "approval history record"
        verbose_name_plural = "approval history"
        indexes = [models.Index(fields=["timesheet", "acted_at"])]

    def __str__(self):
        return f"{self.timesheet_id}: {self.previous_status or '-'} -> {self.new_status}"


class AuditLog(models.Model):
    class Action(models.TextChoices):
        ENTRY_CREATED = "entry_created", "Timesheet created"
        ENTRY_UPDATED = "entry_updated", "Timesheet edited"
        ENTRY_DELETED = "entry_deleted", "Timesheet deleted"
        ENTRY_SUBMITTED = "entry_submitted", "Timesheet submitted"
        ENTRY_APPROVED = "entry_approved", "Timesheet approved"
        ENTRY_REJECTED = "entry_rejected", "Timesheet rejected"
        ENTRY_STATUS_CHANGED = "entry_status_changed", "Timesheet status changed"
        EMPLOYEE_CREATED = "employee_created", "Employee created"
        EMPLOYEE_UPDATED = "employee_updated", "Employee updated"
        EMPLOYEE_ACTIVATED = "employee_activated", "Employee account activated"
        EMPLOYEE_DEACTIVATED = "employee_deactivated", "Employee account deactivated"
        PASSWORD_CHANGED = "password_changed", "Password changed by administrator"
        PROJECT_CREATED = "project_created", "Project created"
        PROJECT_UPDATED = "project_updated", "Project updated"
        PROJECT_DELETED = "project_deleted", "Project deleted"
        ACCESS_UPDATED = "access_updated", "Access permissions updated"
        TIL_RECORDED = "til_recorded", "Time in lieu recorded"
        TIL_DELETED = "til_deleted", "Time in lieu entry deleted"
        TIL_REQUESTED = "til_requested", "Time in lieu requested"
        TIL_REQUEST_APPROVED = "til_request_approved", "Time in lieu request approved"
        TIL_REQUEST_REJECTED = "til_request_rejected", "Time in lieu request rejected"
        TIL_REQUEST_CANCELLED = "til_request_cancelled", "Time in lieu request cancelled"
        CSV_EXPORT = "csv_export", "CSV export"
        LOGIN = "login", "Signed in"
        LOGOUT = "logout", "Signed out"
        LOGIN_FAILED = "login_failed", "Failed sign-in"

    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_events",
    )
    actor_username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=30, choices=Action.choices)
    model_name = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=40, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    details = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["action", "timestamp"]),
            models.Index(fields=["model_name", "object_id"]),
        ]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.get_action_display()}"
