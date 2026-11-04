from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from django.utils import timezone

from .hours import (
    ZERO,
    calculate_total_hours,
    shift_minutes,
    split_regular_overtime,
)
from .models import (
    EmployeeProfile, Project, ProjectAccess, ProjectTask, SectionAccess, TimeInLieuEntry, TimeInLieuRequest, TimesheetEntry,
)
from .access import accessible_projects
from .permissions import is_admin
from .queries import SORT_CHOICES

User = get_user_model()
Status = TimesheetEntry.Status


class LoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"autofocus": True, "autocomplete": "username"})
        self.fields["password"].widget.attrs.update({"autocomplete": "current-password"})


class EmployeeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        profile = getattr(obj, "profile", None)
        code = f" ({profile.employee_id})" if profile else ""
        return f"{obj.get_full_name() or obj.get_username()}{code}"


# --------------------------------------------------------------------------
# Timesheet entry form
# --------------------------------------------------------------------------
class TimesheetForm(forms.ModelForm):
    MODE_MANUAL = "manual"
    MODE_CALCULATE = "calculate"

    hours_mode = forms.ChoiceField(
        label="How should hours be recorded?",
        choices=[
            (MODE_MANUAL, "Enter hours manually"),
            (MODE_CALCULATE, "Calculate from time in and time out"),
        ],
        widget=forms.RadioSelect,
        initial=MODE_MANUAL,
    )

    class Meta:
        model = TimesheetEntry
        fields = [
            "employee", "project", "work_date", "task_description",
            "time_in", "time_out", "break_minutes", "regular_hours", "overtime_hours",
            "work_location", "status", "notes",
        ]
        widgets = {
            "work_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "time_in": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "time_out": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "task_description": forms.Textarea(attrs={"rows": 3, "maxlength": 1000}),
            "notes": forms.Textarea(attrs={"rows": 3, "maxlength": 1000}),
            "break_minutes": forms.NumberInput(attrs={"min": 0, "max": 1440, "step": 1}),
            "regular_hours": forms.NumberInput(attrs={"min": 0, "max": 24, "step": "0.01"}),
            "overtime_hours": forms.NumberInput(attrs={"min": 0, "max": 24, "step": "0.01"}),
        }
        labels = {
            "work_date": "Work date",
            "task_description": "Task or activity description",
            "time_in": "Time in",
            "time_out": "Time out",
            "break_minutes": "Unpaid break (minutes)",
            "regular_hours": "Regular hours",
            "overtime_hours": "Overtime hours",
            "work_location": "Work location",
            "notes": "Notes / remarks",
        }

    def __init__(self, *args, user, **kwargs):
        self.user = user
        self.admin_mode = is_admin(user)
        super().__init__(*args, **kwargs)

        for name in ("regular_hours", "overtime_hours", "break_minutes"):
            self.fields[name].required = False

        current_project_id = self.instance.project_id
        # Administrators entering time for staff may pick any open project; employees
        # only the projects an administrator assigned to them (plus the one already
        # on the entry they are editing, so old entries stay editable).
        base = Project.objects.all() if self.admin_mode else accessible_projects(user)
        self.fields["project"].queryset = Project.objects.filter(
            Q(pk__in=base.values("pk")) & ~Q(status__in=Project.CLOSED_STAGES)
            | Q(pk=current_project_id)
        ).order_by("name")
        self.fields["project"].empty_label = "No project / not applicable"

        # Task checklist per project, used by the form's JavaScript.
        self.project_tasks_map = {}
        for task in ProjectTask.objects.filter(
            project__in=self.fields["project"].queryset
        ).order_by("position", "id"):
            self.project_tasks_map.setdefault(str(task.project_id), []).append(task.name)

        self.employee_code_map = {}
        if self.admin_mode:
            users = User.objects.filter(
                Q(is_active=True) | Q(pk=self.instance.employee_id)
            ).select_related("profile").order_by("last_name", "first_name", "username")
            self.fields["employee"] = EmployeeChoiceField(queryset=users, label="Employee")
            self.employee_code_map = {
                str(u.pk): (u.profile.employee_id if hasattr(u, "profile") else "") for u in users
            }
            self.fields["status"].choices = Status.choices
        else:
            # Employees never choose the employee or the status: both are set by the server.
            del self.fields["employee"]
            del self.fields["status"]

        if self.instance.pk and self.instance.hours_source == TimesheetEntry.HoursSource.CALCULATED:
            self.fields["hours_mode"].initial = self.MODE_CALCULATE
            self.initial.setdefault("hours_mode", self.MODE_CALCULATE)

    def clean_task_description(self):
        value = (self.cleaned_data.get("task_description") or "").strip()
        if not value:
            raise forms.ValidationError("Describe the task or activity.")
        return value

    def clean(self):
        cleaned = super().clean()

        work_date = cleaned.get("work_date")
        if work_date and work_date > timezone.localdate():
            self.add_error("work_date", "The work date cannot be in the future.")

        time_in, time_out = cleaned.get("time_in"), cleaned.get("time_out")
        break_minutes = cleaned.get("break_minutes") or 0
        if "break_minutes" not in self.errors:
            cleaned["break_minutes"] = break_minutes

        if (time_in is None) != (time_out is None):
            missing = "time_out" if time_in is not None else "time_in"
            self.add_error(missing, "Enter both time in and time out, or leave both blank.")

        mode = cleaned.get("hours_mode")
        regular, overtime = cleaned.get("regular_hours"), cleaned.get("overtime_hours")

        if mode == self.MODE_CALCULATE:
            # Explicit user choice: hours come from the times (and are never mixed
            # with typed values). Typed hours are ignored in this mode.
            if time_in is not None and time_out is not None:
                if shift_minutes(time_in, time_out) == 0:
                    self.add_error("time_out", "Time out must be different from time in.")
                else:
                    total = calculate_total_hours(time_in, time_out, break_minutes)
                    if total is None:
                        self.add_error(
                            "break_minutes",
                            "The unpaid break must be shorter than the shift.",
                        )
                    else:
                        regular, overtime = split_regular_overtime(total)
                        self.instance.hours_source = TimesheetEntry.HoursSource.CALCULATED
            elif time_in is None and time_out is None:
                self.add_error("time_in", "Enter time in and time out to calculate hours, "
                                          "or choose manual entry.")
        elif mode == self.MODE_MANUAL:
            regular = regular if regular is not None else ZERO
            overtime = overtime if overtime is not None else ZERO
            self.instance.hours_source = TimesheetEntry.HoursSource.MANUAL

        if mode in (self.MODE_CALCULATE, self.MODE_MANUAL):
            if "regular_hours" not in self.errors and regular is not None:
                cleaned["regular_hours"] = regular
            if "overtime_hours" not in self.errors and overtime is not None:
                cleaned["overtime_hours"] = overtime
        return cleaned


# --------------------------------------------------------------------------
# Filters (timesheet list, reports, CSV export)
# --------------------------------------------------------------------------
class EntryFilterForm(forms.Form):
    q = forms.CharField(
        required=False, max_length=100, label="Search",
        widget=forms.TextInput(attrs={"placeholder": "Name, employee ID, project or task"}),
    )
    employee = EmployeeChoiceField(queryset=User.objects.none(), required=False, empty_label="All employees")
    department = forms.ChoiceField(required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.none(), required=False, empty_label="All projects")
    status = forms.ChoiceField(required=False, choices=[("", "All statuses")] + list(Status.choices))
    start = forms.DateField(required=False, label="From", widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(required=False, label="To", widget=forms.DateInput(attrs={"type": "date"}))
    sort = forms.ChoiceField(required=False, choices=SORT_CHOICES, label="Sort by")

    def __init__(self, *args, admin_mode=False, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if admin_mode or user is None:
            self.fields["project"].queryset = Project.objects.order_by("name")
        else:
            # Employees can only filter by projects they may access or have logged time on.
            own = TimesheetEntry.objects.filter(employee=user).values("project_id")
            self.fields["project"].queryset = Project.objects.filter(
                Q(pk__in=accessible_projects(user).values("pk")) | Q(pk__in=own)
            ).order_by("name")
        if admin_mode:
            self.fields["employee"].queryset = (
                User.objects.select_related("profile").order_by("last_name", "first_name", "username")
            )
            departments = (
                EmployeeProfile.objects.exclude(department="").order_by("department")
                .values_list("department", flat=True).distinct()
            )
            self.fields["department"].choices = [("", "All departments")] + [(d, d) for d in departments]
        else:
            del self.fields["employee"]
            del self.fields["department"]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start"), cleaned.get("end")
        if start and end and start > end:
            self.add_error("end", "The end date must be on or after the start date.")
        return cleaned

    @property
    def is_filtered(self):
        data = getattr(self, "cleaned_data", {})
        return any(data.get(k) for k in ("q", "employee", "department", "project", "status", "start", "end"))


class ReviewForm(forms.Form):
    comment = forms.CharField(required=False, max_length=500)


# --------------------------------------------------------------------------
# Employee management
# --------------------------------------------------------------------------
class EmployeeForm(forms.ModelForm):
    employee_id = forms.CharField(
        required=False, max_length=20, label="Employee ID",
        help_text="Leave blank to generate the next ID automatically.",
    )
    department = forms.CharField(required=False, max_length=100)
    job_title = forms.CharField(required=False, max_length=100, label="Job title")
    password1 = forms.CharField(
        required=False, label="Password", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        required=False, label="Confirm password", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active"]
        labels = {"first_name": "First name", "last_name": "Last name", "is_active": "Account is active"}

    def __init__(self, *args, creating=False, staff=False, **kwargs):
        self.creating = creating
        # staff=True is only ever passed by the administrator-only "add staff account" view.
        self.staff = staff and creating
        super().__init__(*args, **kwargs)
        for name in ("first_name", "last_name", "email"):
            self.fields[name].required = True
        if creating:
            self.fields["password1"].required = True
            self.fields["password2"].required = True
            self.fields["is_active"].initial = True
            self.fields["password1"].help_text = "At least 10 characters, not too common."
        else:
            self.fields["password1"].label = "New password"
            self.fields["password1"].help_text = "Leave both password fields blank to keep the current password."
            profile = getattr(self.instance, "profile", None)
            if profile:
                self.initial.update({
                    "employee_id": profile.employee_id, "department": profile.department,
                    "job_title": profile.job_title,
                })

    def clean_employee_id(self):
        value = (self.cleaned_data.get("employee_id") or "").strip().upper()
        if not value:
            return value
        clash = EmployeeProfile.objects.filter(employee_id__iexact=value)
        profile = getattr(self.instance, "profile", None) if self.instance.pk else None
        if profile:
            clash = clash.exclude(pk=profile.pk)
        if clash.exists():
            raise forms.ValidationError("That employee ID is already in use.")
        return value

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 or p2:
            if p1 != p2:
                self.add_error("password2", "The two password fields did not match.")
            else:
                try:
                    validate_password(p1, user=self.instance)
                except forms.ValidationError as exc:
                    self.add_error("password1", exc)
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        # Superuser rights are never granted from this form. Staff (administrator)
        # rights are granted only on the dedicated staff-account page.
        if self.creating:
            user.is_staff = self.staff
            user.is_superuser = False
        password = self.cleaned_data.get("password1")
        if password:
            user.set_password(password)
        user.save()
        profile = user.profile  # created automatically by the post_save signal
        profile.employee_id = self.cleaned_data.get("employee_id") or profile.employee_id
        profile.department = self.cleaned_data.get("department", "")
        profile.job_title = self.cleaned_data.get("job_title", "")
        profile.save()
        return user


# --------------------------------------------------------------------------
# Project management
# --------------------------------------------------------------------------
class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "code", "section", "status", "start_date", "end_date"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        labels = {"name": "Project name", "code": "Project code", "section": "Section", "status": "Stage Status"}
        help_texts = {
            "section": "Admin, Marketing & Social and Networking are restricted: only administrators and employees explicitly granted that section can use them.",
            "code": "Short unique code, e.g. WEB-01. Stored in upper case.",
            "status": "Current architectural stage. Completed, on-hold and archived projects cannot be chosen on new timesheets.",
        }

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip().upper()
        clash = Project.objects.filter(code__iexact=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("That project code is already in use.")
        return code

    def clean_name(self):
        return (self.cleaned_data.get("name") or "").strip()


class ProjectTaskForm(forms.ModelForm):
    class Meta:
        model = ProjectTask
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. Prepare floor plans", "maxlength": 200})}
        labels = {"name": "Task"}

    def clean_name(self):
        return (self.cleaned_data.get("name") or "").strip()


class BaseProjectTaskFormSet(forms.BaseInlineFormSet):
    def save(self, commit=True):
        # Rows the admin left blank are ignored; the rest keep the order shown on screen.
        for position, form in enumerate(self.forms):
            if form.instance.pk is None and not form.has_changed():
                continue
            form.instance.position = position
        return super().save(commit=commit)


ProjectTaskFormSet = forms.inlineformset_factory(
    Project, ProjectTask, form=ProjectTaskForm, formset=BaseProjectTaskFormSet,
    extra=1, can_delete=True,
)


# --------------------------------------------------------------------------
# Time in Lieu (administrators record entries)
# --------------------------------------------------------------------------
class TimeInLieuForm(forms.ModelForm):
    class Meta:
        model = TimeInLieuEntry
        fields = ["entry_date", "kind", "hours", "note"]
        widgets = {
            "entry_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "hours": forms.NumberInput(attrs={"min": "0.01", "step": "0.25"}),
            "note": forms.TextInput(attrs={"maxlength": 300, "placeholder": "e.g. Worked Saturday site visit"}),
        }
        labels = {"entry_date": "Date", "kind": "Type", "hours": "Hours", "note": "Note"}

    def __init__(self, *args, employee, **kwargs):
        self.employee = employee
        super().__init__(*args, **kwargs)
        self.fields["entry_date"].initial = timezone.localdate()

    def clean_note(self):
        return (self.cleaned_data.get("note") or "").strip()

    def clean(self):
        cleaned = super().clean()
        kind, hours = cleaned.get("kind"), cleaned.get("hours")
        if kind and hours and kind != TimeInLieuEntry.Kind.EARNED:
            from .til import balance_for
            balance = balance_for(self.employee)[2]
            if hours > balance:
                self.add_error("hours", f"Only {balance:.2f} banked hours are available.")
        return cleaned


class TimeInLieuRequestForm(forms.ModelForm):
    """Employees and staff ask to bank earned hours or take time off; an administrator must approve."""
    class Meta:
        model = TimeInLieuRequest
        fields = ["kind", "work_date", "hours", "reason"]
        widgets = {
            "work_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "hours": forms.NumberInput(attrs={"min": "0.25", "max": "24", "step": "0.25"}),
            "reason": forms.TextInput(attrs={"maxlength": 300, "placeholder": "e.g. Worked Saturday site visit"}),
        }
        labels = {"kind": "Type", "work_date": "Date", "hours": "Hours", "reason": "Reason"}
        help_texts = {"work_date": "Date worked (earned) or date of the time off (taken)."}

    def __init__(self, *args, employee=None, **kwargs):
        self.employee = employee
        super().__init__(*args, **kwargs)
        self.fields["work_date"].initial = timezone.localdate()
        self.fields["reason"].required = True

    def clean_reason(self):
        value = (self.cleaned_data.get("reason") or "").strip()
        if not value:
            raise forms.ValidationError("Please say why you are making this request.")
        return value

    def clean(self):
        cleaned = super().clean()
        kind, hours, day = cleaned.get("kind"), cleaned.get("hours"), cleaned.get("work_date")
        if kind == TimeInLieuRequest.Kind.EARNED and day and day > timezone.localdate():
            self.add_error("work_date", "Hours can only be banked for days you have already worked.")
        if kind == TimeInLieuRequest.Kind.TAKEN and hours and self.employee is not None:
            from .til import balance_for, pending_taken_hours
            available = balance_for(self.employee)[2] - pending_taken_hours(self.employee)
            if hours > available:
                self.add_error("hours", f"Only {max(available, 0):.2f} banked hours are available "
                                        "(after any time off already requested).")
        return cleaned


class TilReviewForm(forms.Form):
    comment = forms.CharField(max_length=300, required=False)

    def clean_comment(self):
        return (self.cleaned_data.get("comment") or "").strip()


# --------------------------------------------------------------------------
# Access management (administrators only)
# --------------------------------------------------------------------------
class ProjectChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.name} ({obj.code}) - {obj.get_section_display()}"


class AccessForm(forms.Form):
    sections = forms.MultipleChoiceField(
        required=False, label="Restricted sections",
        choices=[(s.value, s.label) for s in Project.RESTRICTED_SECTIONS],
        widget=forms.CheckboxSelectMultiple,
        help_text="Projects in these sections are hidden unless the section is ticked here "
                  "AND the project is ticked below.",
    )
    projects = ProjectChoiceField(
        required=False, label="Projects", queryset=Project.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, person, **kwargs):
        self.person = person
        super().__init__(*args, **kwargs)
        self.fields["projects"].queryset = Project.objects.order_by("section", "name")
        if not self.is_bound:
            self.initial["sections"] = list(
                SectionAccess.objects.filter(user=person).values_list("section", flat=True))
            self.initial["projects"] = list(
                ProjectAccess.objects.filter(user=person).values_list("project_id", flat=True))

    def save(self):
        """Replace the person's grants with the submitted selection. Returns a change summary."""
        from django.db import transaction
        new_sections = set(self.cleaned_data["sections"])
        new_projects = {p.pk: p for p in self.cleaned_data["projects"]}
        with transaction.atomic():
            old_sections = set(SectionAccess.objects.filter(user=self.person).values_list("section", flat=True))
            old_projects = set(ProjectAccess.objects.filter(user=self.person).values_list("project_id", flat=True))
            SectionAccess.objects.filter(user=self.person).exclude(section__in=new_sections).delete()
            for section in new_sections - old_sections:
                SectionAccess.objects.create(user=self.person, section=section)
            ProjectAccess.objects.filter(user=self.person).exclude(project_id__in=new_projects).delete()
            for pk in set(new_projects) - old_projects:
                ProjectAccess.objects.create(user=self.person, project_id=pk)
        names = {p.pk: p.code for p in Project.objects.filter(
            pk__in=(set(new_projects) | old_projects))}
        parts = []
        for label, added, removed in (
            ("sections", new_sections - old_sections, old_sections - new_sections),
            ("projects", {names[k] for k in set(new_projects) - old_projects},
             {names[k] for k in old_projects - set(new_projects)}),
        ):
            if added:
                parts.append(f"added {label}: {', '.join(sorted(added))}")
            if removed:
                parts.append(f"removed {label}: {', '.join(sorted(removed))}")
        return "; ".join(parts)
