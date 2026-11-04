from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.AppLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),  # POST only

    path("timesheets/", views.timesheet_list, name="timesheet_list"),
    path("timesheets/new/", views.timesheet_create, name="timesheet_create"),
    path("timesheets/<int:pk>/", views.timesheet_detail, name="timesheet_detail"),
    path("timesheets/<int:pk>/edit/", views.timesheet_edit, name="timesheet_edit"),
    path("timesheets/<int:pk>/delete/", views.timesheet_delete, name="timesheet_delete"),
    path("timesheets/<int:pk>/submit/", views.timesheet_submit, name="timesheet_submit"),
    path("timesheets/<int:pk>/approve/", views.timesheet_approve, name="timesheet_approve"),
    path("timesheets/<int:pk>/reject/", views.timesheet_reject, name="timesheet_reject"),

    path("employees/", views.employee_list, name="employee_list"),
    path("employees/new/", views.employee_create, name="employee_create"),
    path("staff/new/", views.staff_create, name="staff_create"),
    path("employees/<int:pk>/", views.employee_detail, name="employee_detail"),
    path("employees/<int:pk>/edit/", views.employee_edit, name="employee_edit"),
    path("employees/<int:pk>/toggle-active/", views.employee_toggle_active, name="employee_toggle_active"),

    path("projects/", views.project_list, name="project_list"),
    path("projects/new/", views.project_create, name="project_create"),
    path("projects/<int:pk>/edit/", views.project_edit, name="project_edit"),
    path("projects/<int:pk>/delete/", views.project_delete, name="project_delete"),

    path("access/", views.access_list, name="access_list"),
    path("access/<int:pk>/", views.access_edit, name="access_edit"),
    path("my-projects/", views.my_projects, name="my_projects"),
    path("my-projects/<int:pk>/", views.my_project_detail, name="my_project_detail"),

    path("time-in-lieu/", views.til_overview, name="til_overview"),
    path("time-in-lieu/requests/", views.til_requests, name="til_requests"),
    path("time-in-lieu/requests/<int:pk>/approve/", views.til_request_approve, name="til_request_approve"),
    path("time-in-lieu/requests/<int:pk>/reject/", views.til_request_reject, name="til_request_reject"),
    path("time-in-lieu/requests/<int:pk>/cancel/", views.til_request_cancel, name="til_request_cancel"),
    path("time-in-lieu/<int:pk>/", views.til_ledger, name="til_ledger"),
    path("time-in-lieu/entry/<int:pk>/delete/", views.til_delete, name="til_delete"),

    path("reports/", views.reports, name="reports"),
    path("reports/export.csv", views.report_export, name="report_export"),
]
