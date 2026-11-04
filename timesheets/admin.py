from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    ApprovalHistory,
    AuditLog,
    EmployeeProfile,
    Project,
    ProjectTask,
    TimeInLieuEntry,
    TimeInLieuRequest,
    ProjectAccess,
    SectionAccess,
    TimesheetEntry,
)
from .permissions import is_admin
from .services import change_status, log_audit

User = get_user_model()

admin.site.site_header = "Formery Timesheet Database"
admin.site.site_title = "Timesheet Admin"
admin.site.index_title = "Database administration"


class AdministratorRoleMixin:
    """
    Give the application's administrators (active staff or superusers - the same
    rule the custom pages use) full access to the timesheet models in Django
    Admin, so the two interfaces agree about who an administrator is.

    Only models from THIS app use the mixin. The built-in User admin keeps
    Django's normal per-permission checks, so a staff member cannot promote
    themselves to superuser unless a superuser granted them that permission.
    """

    def has_module_permission(self, request):
        return is_admin(request.user)

    def has_view_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_add_permission(self, request, *args, **kwargs):
        return is_admin(request.user)

    def has_change_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_admin(request.user)


class EmployeeProfileInline(admin.StackedInline):
    model = EmployeeProfile
    can_delete = False
    extra = 0
    fields = ("employee_id", "department", "job_title")


class UserAdmin(BaseUserAdmin):
    inlines = [EmployeeProfileInline]
    list_display = ("username", "get_full_name", "email", "is_active", "is_staff", "date_joined")
    list_filter = ("is_active", "is_staff", "is_superuser", "profile__department")
    search_fields = ("username", "first_name", "last_name", "email", "profile__employee_id")
    list_select_related = ("profile",)
    list_per_page = 25

    def get_inline_instances(self, request, obj=None):
        # On the "add user" page the profile is created automatically by a signal,
        # so showing the inline there would try to create a second profile.
        return super().get_inline_instances(request, obj) if obj else []


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(AdministratorRoleMixin, admin.ModelAdmin):
    list_display = ("employee_id", "full_name", "department", "job_title", "account_active", "created_at")
    list_filter = ("department", "user__is_active")
    search_fields = ("employee_id", "user__username", "user__first_name", "user__last_name", "user__email", "department", "job_title")
    list_select_related = ("user",)
    ordering = ("employee_id",)
    list_per_page = 25
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("Account", {"fields": ("user", "employee_id")}),
        ("Role", {"fields": ("department", "job_title")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Active", boolean=True)
    def account_active(self, obj):
        return obj.user.is_active


class ProjectTaskInline(admin.TabularInline):
    model = ProjectTask
    extra = 1


@admin.register(Project)
class ProjectAdmin(AdministratorRoleMixin, admin.ModelAdmin):
    list_display = ("code", "name", "section", "status", "start_date", "end_date", "created_at")
    list_filter = ("section", "status")
    search_fields = ("name", "code")
    date_hierarchy = "created_at"
    ordering = ("name",)
    list_per_page = 25
    readonly_fields = ("created_at",)
    inlines = [ProjectTaskInline]
    fieldsets = (
        ("Project", {"fields": ("name", "code", "section", "status")}),
        ("Details", {"fields": ("start_date", "end_date")}),
        ("Timestamps", {"fields": ("created_at",)}),
    )


class ApprovalHistoryInline(admin.TabularInline):
    model = ApprovalHistory
    extra = 0
    can_delete = False
    readonly_fields = ("reviewer", "previous_status", "new_status", "comments", "acted_at")

    def has_view_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TimesheetEntry)
class TimesheetEntryAdmin(AdministratorRoleMixin, admin.ModelAdmin):
    list_display = (
        "work_date", "employee", "employee_code", "project", "regular_hours",
        "overtime_hours", "hours_source", "status", "created_at",
    )
    list_filter = ("status", "hours_source", "project", "work_date", "employee__profile__department")
    search_fields = (
        "employee__username", "employee__first_name", "employee__last_name",
        "employee__profile__employee_id", "project__name", "project__code",
        "task_description", "notes",
    )
    date_hierarchy = "work_date"
    ordering = ("-work_date", "-created_at")
    list_per_page = 25
    list_select_related = ("employee", "employee__profile", "project")
    autocomplete_fields = ("project",)
    readonly_fields = (
        "created_by", "created_at", "updated_at", "submitted_at", "reviewed_by", "reviewed_at",
    )
    inlines = [ApprovalHistoryInline]
    fieldsets = (
        ("Who and what", {"fields": ("employee", "project", "work_date", "task_description")}),
        ("Time and hours", {"fields": ("time_in", "time_out", "break_minutes", "regular_hours", "overtime_hours", "hours_source")}),
        ("Location and notes", {"fields": ("work_location", "notes")}),
        ("Approval", {"fields": ("status", "rejection_reason", "submitted_at", "reviewed_by", "reviewed_at")}),
        ("Record keeping", {"fields": ("created_by", "created_at", "updated_at")}),
    )

    @admin.display(description="Employee ID", ordering="employee__profile__employee_id")
    def employee_code(self, obj):
        return obj.employee_code

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            super().save_model(request, obj, form, change)
            log_audit(request.user, "entry_created", obj, details="Created in Django Admin", request=request)
            return
        new_status = obj.status
        old_status = form.initial.get("status", new_status)
        obj.status = old_status
        super().save_model(request, obj, form, change)
        log_audit(request.user, "entry_updated", obj,
                  details="Edited in Django Admin: " + ", ".join(form.changed_data), request=request)
        if new_status != old_status:
            change_status(obj, new_status, request.user, comment="Changed in Django Admin", request=request)

    def delete_model(self, request, obj):
        log_audit(request.user, "entry_deleted", obj, details="Deleted in Django Admin", request=request)
        super().delete_model(request, obj)


class ReadOnlyAdmin(AdministratorRoleMixin, admin.ModelAdmin):
    """History tables are append-only: nobody can add or edit them by hand."""

    def has_add_permission(self, request, *args, **kwargs):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(ApprovalHistory)
class ApprovalHistoryAdmin(ReadOnlyAdmin):
    list_display = ("acted_at", "timesheet", "reviewer", "previous_status", "new_status", "comments")
    list_filter = ("new_status", "previous_status", "acted_at")
    search_fields = ("timesheet__employee__username", "timesheet__employee__last_name", "reviewer__username", "comments")
    date_hierarchy = "acted_at"
    ordering = ("-acted_at",)
    list_per_page = 25
    list_select_related = ("timesheet", "timesheet__employee", "reviewer")


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "action", "actor_username", "model_name", "object_repr", "ip_address")
    list_filter = ("action", "model_name", "timestamp")
    search_fields = ("actor_username", "object_repr", "details", "object_id")
    date_hierarchy = "timestamp"
    ordering = ("-timestamp",)
    list_per_page = 50


@admin.register(TimeInLieuEntry)
class TimeInLieuEntryAdmin(AdministratorRoleMixin, admin.ModelAdmin):
    list_display = ("employee", "entry_date", "kind", "hours", "note", "recorded_by")
    list_filter = ("kind", "entry_date")
    search_fields = ("employee__username", "employee__first_name", "employee__last_name", "note")
    date_hierarchy = "entry_date"
    readonly_fields = ("recorded_by", "created_at")


@admin.register(TimeInLieuRequest)
class TimeInLieuRequestAdmin(ReadOnlyAdmin):
    """Read-only here on purpose: approving must go through the app so hours and audit stay in step."""
    list_display = ("employee", "work_date", "hours", "status", "reviewed_by", "reviewed_at")
    list_filter = ("status", "work_date")
    search_fields = ("employee__username", "employee__first_name", "employee__last_name", "reason")
    date_hierarchy = "work_date"


@admin.register(ProjectAccess)
class ProjectAccessAdmin(AdministratorRoleMixin, admin.ModelAdmin):
    list_display = ("user", "project", "created_at")
    list_filter = ("project",)
    search_fields = ("user__username", "user__first_name", "user__last_name", "project__name", "project__code")


@admin.register(SectionAccess)
class SectionAccessAdmin(AdministratorRoleMixin, admin.ModelAdmin):
    list_display = ("user", "section", "created_at")
    list_filter = ("section",)
    search_fields = ("user__username", "user__first_name", "user__last_name")
