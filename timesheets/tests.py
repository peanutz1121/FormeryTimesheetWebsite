import csv
import io
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .exports import sanitize_cell
from .hours import calculate_total_hours, shift_minutes, split_regular_overtime
from .models import ApprovalHistory, AuditLog, EmployeeProfile, Project, ProjectAccess, SectionAccess, TimeInLieuEntry, TimeInLieuRequest, TimesheetEntry

User = get_user_model()
Status = TimesheetEntry.Status
PASSWORD = "Str0ng-test-pass!"


def make_user(username, staff=False, **extra):
    user = User.objects.create_user(
        username=username, password=PASSWORD, first_name=username.title(),
        last_name="Tester", email=f"{username}@example.com", is_staff=staff, **extra,
    )
    return user


def make_entry(employee, work_date=None, status=Status.DRAFT, regular="8.00", overtime="0.00", **extra):
    entry = TimesheetEntry.objects.create(
        employee=employee,
        work_date=work_date or timezone.localdate() - timedelta(days=1),
        task_description=extra.pop("task_description", "Some work"),
        regular_hours=Decimal(regular), overtime_hours=Decimal(overtime),
        created_by=employee, **extra,
    )
    if status != Status.DRAFT:
        entry.status = status
        entry.save(update_fields=["status"])
    return entry


def valid_post(**overrides):
    data = {
        "work_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
        "task_description": "Wrote the quarterly report",
        "hours_mode": "manual",
        "regular_hours": "7.50",
        "overtime_hours": "1.00",
        "break_minutes": "30",
        "work_location": "Head office",
        "notes": "",
        "project": "",
        "action": "save",
    }
    data.update(overrides)
    return data


class BaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user("boss", staff=True)
        cls.alice = make_user("alice")
        cls.bob = make_user("bob")
        cls.project = Project.objects.create(name="Atlas", code="ATL-1")
        ProjectAccess.objects.create(user=cls.alice, project=cls.project)  # bob is NOT assigned

    def login(self, user):
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))


class HoursCalculationTests(TestCase):
    def test_simple_shift_with_break(self):
        self.assertEqual(calculate_total_hours(time(9, 0), time(17, 30), 30), Decimal("8.00"))

    def test_overnight_shift(self):
        self.assertEqual(shift_minutes(time(22, 0), time(6, 0)), 480)
        self.assertEqual(calculate_total_hours(time(22, 0), time(6, 0), 0), Decimal("8.00"))

    def test_identical_times_are_not_a_valid_shift(self):
        self.assertEqual(shift_minutes(time(9, 0), time(9, 0)), 0)

    def test_break_longer_than_shift_returns_none(self):
        self.assertIsNone(calculate_total_hours(time(9, 0), time(10, 0), 60))

    def test_overtime_split(self):
        self.assertEqual(split_regular_overtime(Decimal("10.50")), (Decimal("8.00"), Decimal("2.50")))
        self.assertEqual(split_regular_overtime(Decimal("6.25")), (Decimal("6.25"), Decimal("0.00")))


class AuthenticationTests(BaseTestCase):
    def test_login_required_for_protected_pages(self):
        for name in ("dashboard", "timesheet_list", "timesheet_create", "employee_list", "reports", "report_export"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse("login"), response["Location"], name)

    def test_login_and_logout_flow(self):
        response = self.client.post(reverse("login"), {"username": "alice", "password": PASSWORD})
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        # Logout must be a POST; GET is not allowed.
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)

    def test_wrong_password_rejected_and_audited(self):
        response = self.client.post(reverse("login"), {"username": "alice", "password": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOGIN_FAILED).exists())
        self.assertNotIn("nope", "".join(AuditLog.objects.values_list("details", flat=True)))

    def test_deactivated_account_cannot_log_in_but_history_stays(self):
        entry = make_entry(self.alice)
        self.alice.is_active = False
        self.alice.save()
        response = self.client.post(reverse("login"), {"username": "alice", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(TimesheetEntry.objects.filter(pk=entry.pk).exists())

    def test_post_requires_csrf_token(self):
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        strict.login(username="boss", password=PASSWORD)
        entry = make_entry(self.alice, status=Status.SUBMITTED)
        response = strict.post(reverse("timesheet_approve", args=[entry.pk]))
        self.assertEqual(response.status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.SUBMITTED)


class EmployeeIsolationTests(BaseTestCase):
    def setUp(self):
        self.alice_entry = make_entry(self.alice, task_description="Alice private work")
        self.bob_entry = make_entry(self.bob, task_description="Bob private work")
        self.login(self.alice)

    def test_list_shows_only_own_entries(self):
        response = self.client.get(reverse("timesheet_list"))
        self.assertContains(response, "Alice private work")
        self.assertNotContains(response, "Bob private work")

    def test_employee_filter_param_cannot_expose_others(self):
        response = self.client.get(reverse("timesheet_list"), {"employee": self.bob.pk})
        self.assertNotContains(response, "Bob private work")
        response = self.client.get(reverse("timesheet_list"), {"q": "Bob"})
        self.assertNotContains(response, "Bob private work")

    def test_cannot_view_edit_delete_or_submit_other_entries(self):
        pk = self.bob_entry.pk
        self.assertEqual(self.client.get(reverse("timesheet_detail", args=[pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("timesheet_edit", args=[pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("timesheet_edit", args=[pk]), valid_post()).status_code, 404)
        self.assertEqual(self.client.post(reverse("timesheet_delete", args=[pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("timesheet_submit", args=[pk])).status_code, 404)
        self.bob_entry.refresh_from_db()
        self.assertEqual(self.bob_entry.status, Status.DRAFT)
        self.assertEqual(self.bob_entry.task_description, "Bob private work")

    def test_employee_cannot_spoof_employee_field_on_create(self):
        response = self.client.post(
            reverse("timesheet_create"), valid_post(employee=self.bob.pk, status="approved"),
        )
        entry = TimesheetEntry.objects.exclude(pk__in=[self.alice_entry.pk, self.bob_entry.pk]).get()
        self.assertEqual(entry.employee, self.alice)
        self.assertEqual(entry.status, Status.DRAFT)
        self.assertRedirects(response, reverse("timesheet_detail", args=[entry.pk]))

    def test_employee_blocked_from_admin_areas(self):
        for name in ("employee_list", "employee_create", "reports", "report_export"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.assertEqual(self.client.get(reverse("employee_detail", args=[self.bob.pk])).status_code, 403)
        self.assertEqual(self.client.get("/admin/").status_code, 302)  # Django admin redirects non-staff

    def test_employee_cannot_approve_or_reject(self):
        entry = make_entry(self.alice, status=Status.SUBMITTED)
        for name in ("timesheet_approve", "timesheet_reject"):
            self.assertEqual(self.client.post(reverse(name, args=[entry.pk])).status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.SUBMITTED)

    def test_employee_dashboard_totals_are_personal(self):
        response = self.client.get(reverse("dashboard"))
        self.assertTemplateUsed(response, "timesheets/dashboard_employee.html")
        self.assertEqual(response.context["stats"]["entries"], 1)


class AdministratorPermissionTests(BaseTestCase):
    def setUp(self):
        self.alice_entry = make_entry(self.alice, task_description="Alice work")
        self.bob_entry = make_entry(self.bob, task_description="Bob work")
        self.login(self.admin)

    def test_admin_sees_all_entries(self):
        response = self.client.get(reverse("timesheet_list"))
        self.assertContains(response, "Alice work")
        self.assertContains(response, "Bob work")

    def test_admin_dashboard_and_management_pages_load(self):
        for name in ("dashboard", "timesheet_list", "timesheet_create", "employee_list",
                     "employee_create", "reports"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertTemplateUsed(self.client.get(reverse("dashboard")), "timesheets/dashboard_admin.html")
        self.assertEqual(self.client.get(reverse("employee_detail", args=[self.alice.pk])).status_code, 200)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_admin_can_create_entry_for_any_employee(self):
        self.client.post(reverse("timesheet_create"), valid_post(employee=self.bob.pk, status="draft"))
        entry = TimesheetEntry.objects.get(task_description="Wrote the quarterly report")
        self.assertEqual(entry.employee, self.bob)
        self.assertEqual(entry.created_by, self.admin)

    def test_admin_can_edit_approved_entry_and_it_is_audited(self):
        entry = make_entry(self.alice, status=Status.APPROVED)
        response = self.client.post(
            reverse("timesheet_edit", args=[entry.pk]),
            valid_post(employee=self.alice.pk, status="approved", regular_hours="6.00", overtime_hours="0"),
        )
        self.assertRedirects(response, reverse("timesheet_detail", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.regular_hours, Decimal("6.00"))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ENTRY_UPDATED, object_id=str(entry.pk)).exists())

    def test_admin_can_delete_with_post_only_and_audit_survives(self):
        entry = make_entry(self.alice)
        pk = entry.pk
        self.assertEqual(self.client.get(reverse("timesheet_delete", args=[pk])).status_code, 200)  # confirm page
        self.assertTrue(TimesheetEntry.objects.filter(pk=pk).exists())  # GET never deletes
        self.client.post(reverse("timesheet_delete", args=[pk]))
        self.assertFalse(TimesheetEntry.objects.filter(pk=pk).exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ENTRY_DELETED, object_id=str(pk)).exists())


class EntryCreationTests(BaseTestCase):
    def setUp(self):
        self.login(self.alice)

    def test_manual_hours_saved_exactly_as_typed(self):
        self.client.post(reverse("timesheet_create"), valid_post(
            time_in="09:00", time_out="17:00", break_minutes="30",  # would calculate to 7.5
            regular_hours="6.25", overtime_hours="0.75", project=self.project.pk))
        entry = TimesheetEntry.objects.get()
        self.assertEqual(entry.regular_hours, Decimal("6.25"))
        self.assertEqual(entry.overtime_hours, Decimal("0.75"))
        self.assertEqual(entry.hours_source, TimesheetEntry.HoursSource.MANUAL)
        self.assertTrue(entry.differs_from_times)

    def test_calculated_hours_with_overtime(self):
        self.client.post(reverse("timesheet_create"), valid_post(
            hours_mode="calculate", time_in="08:00", time_out="18:30", break_minutes="30",
            regular_hours="", overtime_hours=""))
        entry = TimesheetEntry.objects.get()
        self.assertEqual((entry.regular_hours, entry.overtime_hours), (Decimal("8.00"), Decimal("2.00")))
        self.assertEqual(entry.hours_source, TimesheetEntry.HoursSource.CALCULATED)

    def test_calculated_overnight_shift(self):
        self.client.post(reverse("timesheet_create"), valid_post(
            hours_mode="calculate", time_in="22:00", time_out="06:00", break_minutes="0",
            regular_hours="", overtime_hours=""))
        entry = TimesheetEntry.objects.get()
        self.assertEqual(entry.total_hours, Decimal("8.00"))

    def test_calculate_mode_ignores_typed_hours(self):
        self.client.post(reverse("timesheet_create"), valid_post(
            hours_mode="calculate", time_in="09:00", time_out="13:00", break_minutes="0",
            regular_hours="20", overtime_hours="3"))
        entry = TimesheetEntry.objects.get()
        self.assertEqual(entry.total_hours, Decimal("4.00"))

    def test_validation_errors(self):
        bad_cases = {
            "negative hours": valid_post(regular_hours="-1"),
            "over 24 hours": valid_post(regular_hours="20", overtime_hours="5"),
            "zero hours": valid_post(regular_hours="0", overtime_hours="0"),
            "blank hours manual": valid_post(regular_hours="", overtime_hours=""),
            "future date": valid_post(work_date=(timezone.localdate() + timedelta(days=3)).isoformat()),
            "negative break": valid_post(break_minutes="-5"),
            "break too long": valid_post(hours_mode="calculate", time_in="09:00", time_out="10:00",
                                         break_minutes="90", regular_hours="", overtime_hours=""),
            "same in and out": valid_post(hours_mode="calculate", time_in="09:00", time_out="09:00",
                                          regular_hours="", overtime_hours=""),
            "only time in": valid_post(time_in="09:00"),
            "empty task": valid_post(task_description="   "),
            "calculate without times": valid_post(hours_mode="calculate", regular_hours="", overtime_hours=""),
        }
        for label, data in bad_cases.items():
            response = self.client.post(reverse("timesheet_create"), data)
            self.assertEqual(response.status_code, 200, label)
            self.assertTrue(response.context["form"].errors, label)
        self.assertEqual(TimesheetEntry.objects.count(), 0)

    def test_save_draft_then_submit_flow(self):
        self.client.post(reverse("timesheet_create"), valid_post(action="submit"))
        entry = TimesheetEntry.objects.get()
        self.assertEqual(entry.status, Status.SUBMITTED)
        self.assertIsNotNone(entry.submitted_at)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ENTRY_CREATED).exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ENTRY_SUBMITTED).exists())

    def test_employee_edit_own_draft(self):
        entry = make_entry(self.alice)
        response = self.client.post(reverse("timesheet_edit", args=[entry.pk]),
                                    valid_post(task_description="Edited text"))
        self.assertRedirects(response, reverse("timesheet_detail", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.task_description, "Edited text")

    def test_employee_can_delete_own_draft_only(self):
        draft = make_entry(self.alice)
        submitted = make_entry(self.alice, status=Status.SUBMITTED)
        self.client.post(reverse("timesheet_delete", args=[draft.pk]))
        self.client.post(reverse("timesheet_delete", args=[submitted.pk]))
        self.assertFalse(TimesheetEntry.objects.filter(pk=draft.pk).exists())
        self.assertTrue(TimesheetEntry.objects.filter(pk=submitted.pk).exists())


class ApprovalWorkflowTests(BaseTestCase):
    def test_full_cycle_submit_reject_resubmit_approve_then_lock(self):
        self.login(self.alice)
        self.client.post(reverse("timesheet_create"), valid_post(action="submit"))
        entry = TimesheetEntry.objects.get()

        # Submitted entries are locked for the employee.
        locked = self.client.post(reverse("timesheet_edit", args=[entry.pk]), valid_post(task_description="Sneaky"))
        self.assertEqual(locked.status_code, 302)
        entry.refresh_from_db()
        self.assertNotEqual(entry.task_description, "Sneaky")

        self.login(self.admin)
        self.client.post(reverse("timesheet_reject", args=[entry.pk]), {"comment": "Missing client"})
        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.REJECTED)
        self.assertEqual(entry.rejection_reason, "Missing client")
        self.assertEqual(entry.reviewed_by, self.admin)
        self.assertIsNotNone(entry.reviewed_at)

        # Employee sees the reason, fixes it (rejected -> draft), then resubmits.
        self.login(self.alice)
        self.assertContains(self.client.get(reverse("timesheet_detail", args=[entry.pk])), "Missing client")
        self.client.post(reverse("timesheet_edit", args=[entry.pk]), valid_post(task_description="Fixed", action="save"))
        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.DRAFT)
        self.client.post(reverse("timesheet_submit", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.SUBMITTED)

        self.login(self.admin)
        self.client.post(reverse("timesheet_approve", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.APPROVED)
        self.assertEqual(entry.reviewed_by, self.admin)

        # Approved: locked for the employee, even via direct POST.
        self.login(self.alice)
        self.client.post(reverse("timesheet_edit", args=[entry.pk]), valid_post(task_description="After approval"))
        self.client.post(reverse("timesheet_delete", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.task_description, "Fixed")
        self.assertEqual(entry.status, Status.APPROVED)

        history = list(ApprovalHistory.objects.filter(timesheet=entry).values_list("previous_status", "new_status"))
        self.assertEqual(history, [
            ("draft", "submitted"), ("submitted", "rejected"), ("rejected", "draft"),
            ("draft", "submitted"), ("submitted", "approved"),
        ])
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ENTRY_APPROVED).exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ENTRY_REJECTED).exists())

    def test_admin_can_reverse_an_approval(self):
        entry = make_entry(self.alice, status=Status.APPROVED)
        self.login(self.admin)
        self.client.post(reverse("timesheet_reject", args=[entry.pk]), {"comment": "Wrong project"})
        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.REJECTED)

    def test_cannot_approve_draft_or_approve_twice(self):
        draft = make_entry(self.alice)
        self.login(self.admin)
        self.client.post(reverse("timesheet_approve", args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.status, Status.DRAFT)

    def test_approve_and_reject_require_post(self):
        entry = make_entry(self.alice, status=Status.SUBMITTED)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("timesheet_approve", args=[entry.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse("timesheet_reject", args=[entry.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse("timesheet_submit", args=[entry.pk])).status_code, 405)


class FilteringTests(BaseTestCase):
    def setUp(self):
        today = timezone.localdate()
        make_entry(self.alice, work_date=today - timedelta(days=40), task_description="Old budget review", status=Status.APPROVED)
        make_entry(self.alice, work_date=today - timedelta(days=2), task_description="Recent design work", project=self.project)
        make_entry(self.bob, work_date=today - timedelta(days=3), task_description="Bob testing", status=Status.SUBMITTED, regular="4.00")
        self.today = today
        self.login(self.admin)

    def test_status_filter(self):
        response = self.client.get(reverse("timesheet_list"), {"status": "submitted"})
        self.assertEqual(response.context["count"], 1)
        self.assertContains(response, "Bob testing")

    def test_date_range_filter(self):
        response = self.client.get(reverse("timesheet_list"), {
            "start": (self.today - timedelta(days=10)).isoformat(), "end": self.today.isoformat()})
        self.assertEqual(response.context["count"], 2)
        self.assertNotContains(response, "Old budget review")

    def test_search_by_name_employee_id_project_and_task(self):
        self.assertEqual(self.client.get(reverse("timesheet_list"), {"q": "Alice"}).context["count"], 2)
        code = self.bob.profile.employee_id
        self.assertEqual(self.client.get(reverse("timesheet_list"), {"q": code}).context["count"], 1)
        self.assertEqual(self.client.get(reverse("timesheet_list"), {"q": "Atlas"}).context["count"], 1)
        self.assertEqual(self.client.get(reverse("timesheet_list"), {"q": "Acme"}).context["count"], 0)  # client names no longer exist
        self.assertEqual(self.client.get(reverse("timesheet_list"), {"q": "budget"}).context["count"], 1)

    def test_filtered_total_hours_and_employee_filter(self):
        response = self.client.get(reverse("timesheet_list"), {"employee": self.bob.pk})
        self.assertEqual(response.context["total_hours"], Decimal("4.00"))

    def test_empty_state_and_invalid_dates_do_not_crash(self):
        response = self.client.get(reverse("timesheet_list"), {"q": "zzzz-no-match"})
        self.assertContains(response, "No timesheets match these filters")
        response = self.client.get(reverse("timesheet_list"), {"start": "not-a-date", "employee": "abc"})
        self.assertEqual(response.status_code, 200)

    def test_pagination(self):
        for i in range(30):
            make_entry(self.alice, work_date=self.today - timedelta(days=100 + i))
        response = self.client.get(reverse("timesheet_list"))
        self.assertEqual(len(response.context["entries"]), 20)
        self.assertEqual(response.context["page"].paginator.num_pages, 2)
        self.assertEqual(self.client.get(reverse("timesheet_list"), {"page": 2}).status_code, 200)

    def test_sorting(self):
        response = self.client.get(reverse("timesheet_list"), {"sort": "total"})
        totals = [e.total for e in response.context["entries"]]
        self.assertEqual(totals, sorted(totals))


class DashboardTests(BaseTestCase):
    def test_dashboard_numbers_come_from_database(self):
        today = timezone.localdate()
        make_entry(self.alice, work_date=today, regular="8.00", overtime="2.00", status=Status.APPROVED)
        make_entry(self.alice, work_date=today, regular="5.00", status=Status.SUBMITTED)
        make_entry(self.bob, work_date=today, regular="3.00", status=Status.REJECTED)
        make_entry(self.bob, work_date=today, regular="1.50")
        make_entry(self.bob, work_date=date(2020, 1, 15), regular="4.00", status=Status.APPROVED)
        self.login(self.admin)
        stats = self.client.get(reverse("dashboard")).context["stats"]
        self.assertEqual(stats["total_entries"], 5)
        self.assertEqual(stats["active_employees"], 2)
        self.assertEqual(stats["total_hours"], Decimal("23.50"))
        self.assertEqual(stats["approved_hours"], Decimal("14.00"))
        self.assertEqual(stats["overtime_hours"], Decimal("2.00"))
        self.assertEqual((stats["submitted"], stats["drafts"], stats["rejected"]), (1, 1, 1))
        self.assertEqual(stats["month_hours"], Decimal("19.50"))  # the four entries dated today; 2020 entry excluded

    def test_dashboard_with_empty_database(self):
        self.login(self.admin)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["total_hours"], Decimal("0"))

    def test_top_employees_ordering(self):
        today = timezone.localdate()
        make_entry(self.alice, work_date=today, regular="2.00")
        make_entry(self.bob, work_date=today, regular="9.00")
        self.login(self.admin)
        top = self.client.get(reverse("dashboard")).context["top_employees"]
        self.assertEqual(top[0]["employee_id"], self.bob.pk)


class EmployeeManagementTests(BaseTestCase):
    def setUp(self):
        self.login(self.admin)

    def payload(self, **kw):
        data = {
            "username": "newhire", "first_name": "New", "last_name": "Hire", "email": "new@example.com",
            "is_active": "on", "employee_id": "", "department": "Sales", "job_title": "Rep",
            "password1": "Another-Strong-Pass1", "password2": "Another-Strong-Pass1",
        }
        data.update(kw)
        return data

    def test_create_employee_is_not_admin_and_password_hashed(self):
        response = self.client.post(reverse("employee_create"), self.payload())
        user = User.objects.get(username="newhire")
        self.assertRedirects(response, reverse("employee_detail", args=[user.pk]))
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertNotEqual(user.password, "Another-Strong-Pass1")
        self.assertTrue(user.password.startswith("pbkdf2_") or "$" in user.password)
        self.assertTrue(user.check_password("Another-Strong-Pass1"))
        self.assertTrue(user.profile.employee_id.startswith("EMP-"))
        self.assertEqual(EmployeeProfile.objects.filter(user=user).count(), 1)

    def test_create_rejects_weak_or_mismatched_password_and_duplicate_id(self):
        self.assertEqual(self.client.post(reverse("employee_create"), self.payload(password1="12345", password2="12345")).status_code, 200)
        self.assertEqual(self.client.post(reverse("employee_create"), self.payload(password2="different")).status_code, 200)
        self.assertFalse(User.objects.filter(username="newhire").exists())
        self.client.post(reverse("employee_create"), self.payload())
        clash = self.client.post(reverse("employee_create"), self.payload(
            username="second", email="s@example.com", employee_id=User.objects.get(username="newhire").profile.employee_id))
        self.assertEqual(clash.status_code, 200)
        self.assertFalse(User.objects.filter(username="second").exists())

    def test_cannot_escalate_privileges_via_form(self):
        self.client.post(reverse("employee_create"), self.payload(is_staff="on", is_superuser="on"))
        user = User.objects.get(username="newhire")
        self.assertFalse(user.is_staff or user.is_superuser)

    def test_edit_keeps_password_when_blank(self):
        self.client.post(reverse("employee_create"), self.payload())
        user = User.objects.get(username="newhire")
        old_hash = user.password
        self.client.post(reverse("employee_edit", args=[user.pk]), self.payload(
            job_title="Senior Rep", password1="", password2="", employee_id=user.profile.employee_id))
        user.refresh_from_db()
        self.assertEqual(user.password, old_hash)
        self.assertEqual(user.profile.job_title, "Senior Rep")

    def test_deactivate_blocks_login_but_keeps_timesheets(self):
        entry = make_entry(self.alice, status=Status.APPROVED)
        self.client.post(reverse("employee_toggle_active", args=[self.alice.pk]))
        self.alice.refresh_from_db()
        self.assertFalse(self.alice.is_active)
        self.assertTrue(TimesheetEntry.objects.filter(pk=entry.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.EMPLOYEE_DEACTIVATED).exists())
        from django.test import Client
        other = Client()
        self.assertFalse(other.login(username="alice", password=PASSWORD))
        # Reactivate
        self.client.post(reverse("employee_toggle_active", args=[self.alice.pk]))
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_active)

    def test_admin_cannot_deactivate_self(self):
        self.client.post(reverse("employee_toggle_active", args=[self.admin.pk]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_deactivation_requires_post(self):
        self.assertEqual(self.client.get(reverse("employee_toggle_active", args=[self.alice.pk])).status_code, 405)

    def test_employee_search_and_totals(self):
        make_entry(self.alice, regular="5.00", overtime="1.00")
        response = self.client.get(reverse("employee_list"), {"q": "alice"})
        rows = list(response.context["employees"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].total_hours, Decimal("6.00"))
        self.assertEqual(rows[0].total_overtime, Decimal("1.00"))


class ModelIntegrityTests(BaseTestCase):
    def test_user_with_timesheets_cannot_be_deleted(self):
        make_entry(self.alice)
        with self.assertRaises(ProtectedError):
            self.alice.delete()

    def test_database_constraints_reject_bad_hours(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TimesheetEntry.objects.create(
                employee=self.alice, work_date=date(2025, 1, 6), task_description="x",
                regular_hours=Decimal("30.00"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TimesheetEntry.objects.create(
                employee=self.alice, work_date=date(2025, 1, 6), task_description="x",
                regular_hours=Decimal("-1.00"))

    def test_profile_created_automatically_with_unique_ids(self):
        ids = list(EmployeeProfile.objects.values_list("employee_id", flat=True))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.startswith("EMP-") for i in ids))


class ReportTests(BaseTestCase):
    def setUp(self):
        today = timezone.localdate()
        self.alice.profile.department = "Engineering"
        self.alice.profile.save()
        make_entry(self.alice, work_date=today - timedelta(days=1), regular="8.00", overtime="1.00", status=Status.APPROVED, project=self.project)
        make_entry(self.bob, work_date=today - timedelta(days=2), regular="6.00", status=Status.SUBMITTED)
        make_entry(self.bob, work_date=today - timedelta(days=3), regular="2.00", status=Status.REJECTED)
        self.login(self.admin)

    def test_report_totals_and_breakdowns(self):
        data = self.client.get(reverse("reports")).context["data"]
        self.assertEqual(data["totals"]["total"], Decimal("17.00"))
        self.assertEqual(data["approved_hours"], Decimal("9.00"))
        self.assertEqual(data["pending_hours"], Decimal("6.00"))
        self.assertEqual(data["rejected_count"], 1)
        self.assertEqual(len(data["by_employee"]), 2)
        self.assertEqual(sum(r["overtime"] for r in data["by_employee"]), Decimal("1.00"))
        self.assertTrue(data["daily"] and data["weekly"] and data["monthly"])

    def test_report_department_and_project_filters(self):
        data = self.client.get(reverse("reports"), {"department": "Engineering"}).context["data"]
        self.assertEqual(data["totals"]["entries"], 1)
        data = self.client.get(reverse("reports"), {"project": self.project.pk}).context["data"]
        self.assertEqual(data["totals"]["total"], Decimal("9.00"))


class CsvExportTests(BaseTestCase):
    def setUp(self):
        today = timezone.localdate()
        self.e1 = make_entry(self.alice, work_date=today - timedelta(days=1), status=Status.APPROVED,
                             task_description='=HYPERLINK("http://evil.example","click")', project=self.project)
        self.e2 = make_entry(self.bob, work_date=today - timedelta(days=2), status=Status.SUBMITTED,
                             task_description="+SUM(1,1) normal, with comma")
        self.e3 = make_entry(self.bob, work_date=today - timedelta(days=60), task_description="@cmd old")

    def fetch_rows(self, params=None):
        response = self.client.get(reverse("report_export"), params or {})
        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8-sig")
        return response, list(csv.reader(io.StringIO(text)))

    def test_export_requires_admin(self):
        self.assertEqual(self.client.get(reverse("report_export")).status_code, 302)  # anonymous
        self.login(self.alice)
        self.assertEqual(self.client.get(reverse("report_export")).status_code, 403)

    def test_export_headers_and_content_type(self):
        self.login(self.admin)
        response, rows = self.fetch_rows()
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertTrue(response["Content-Disposition"].endswith('.csv"'))
        self.assertEqual(rows[0][0], "Employee ID")
        self.assertEqual(len(rows), 4)  # header + 3 rows

    def test_export_matches_active_filters_only(self):
        self.login(self.admin)
        _, rows = self.fetch_rows({"status": "submitted"})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], "Bob Tester")
        _, rows = self.fetch_rows({"employee": self.alice.pk})
        self.assertEqual(len(rows), 2)
        today = timezone.localdate()
        _, rows = self.fetch_rows({"start": (today - timedelta(days=10)).isoformat(), "end": today.isoformat()})
        self.assertEqual(len(rows), 3)

    def test_formula_injection_is_neutralised(self):
        self.login(self.admin)
        _, rows = self.fetch_rows()
        tasks = [r[rows[0].index("Task description")] for r in rows[1:]]
        self.assertTrue(all(not t.startswith(("=", "+", "-", "@")) for t in tasks), tasks)
        self.assertIn("'=HYPERLINK(\"http://evil.example\",\"click\")", tasks)
        self.assertIn("'+SUM(1,1) normal, with comma", tasks)  # comma survives csv quoting

    def test_sanitize_cell_unit(self):
        for dangerous in ("=1+1", "+1", "-1", "@a", "\t=x", "  =x"):
            self.assertTrue(sanitize_cell(dangerous).startswith("'"), dangerous)
        self.assertEqual(sanitize_cell("plain"), "plain")
        self.assertEqual(sanitize_cell(None), "")

    def test_invalid_filter_does_not_export_everything(self):
        self.login(self.admin)
        response = self.client.get(reverse("report_export"), {"start": "garbage"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("reports"), response["Location"])

    def test_export_is_audited(self):
        self.login(self.admin)
        self.client.get(reverse("report_export"))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CSV_EXPORT).exists())


class ErrorPageTests(BaseTestCase):
    def test_404_page_and_403_page_render(self):
        self.login(self.alice)
        response = self.client.get("/definitely/not/here/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "could not find", status_code=404)
        denied = self.client.get(reverse("reports"))
        self.assertContains(denied, "do not have permission", status_code=403)

    def test_500_template_renders_standalone(self):
        from django.template.loader import render_to_string
        html = render_to_string("500.html")
        self.assertIn("Something went wrong", html)

    def test_every_sidebar_link_resolves_for_admin(self):
        self.login(self.admin)
        page = self.client.get(reverse("dashboard")).content.decode()
        for url in (reverse("timesheet_list"), reverse("timesheet_create"), reverse("employee_list"),
                    reverse("reports"), reverse("project_list"), "/admin/"):
            self.assertIn(f'href="{url}"', page)
            self.assertIn(self.client.get(url).status_code, (200, 302))


class DjangoAdminAccessTests(BaseTestCase):
    """The custom UI and Django Admin must agree on who an administrator is."""

    def test_staff_administrator_can_use_timesheet_models_in_django_admin(self):
        self.login(self.admin)  # is_staff but NOT superuser
        entry = make_entry(self.alice)
        for url in (
            reverse("admin:timesheets_project_changelist"),
            reverse("admin:timesheets_timesheetentry_changelist"),
            reverse("admin:timesheets_timesheetentry_change", args=[entry.pk]),
            reverse("admin:timesheets_employeeprofile_changelist"),
            reverse("admin:timesheets_approvalhistory_changelist"),
            reverse("admin:timesheets_auditlog_changelist"),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_staff_administrator_cannot_manage_user_accounts_in_django_admin(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("admin:auth_user_changelist")).status_code, 403)

    def test_employee_is_locked_out_of_django_admin(self):
        self.login(self.alice)
        response = self.client.get(reverse("admin:timesheets_timesheetentry_changelist"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])


class ProjectManagementTests(BaseTestCase):
    def project_post(self, **overrides):
        data = {"name": "Beacon", "code": "bcn-9", "section": "project", "status": "schematic",
                "start_date": "", "end_date": "",
                "tasks-TOTAL_FORMS": "0", "tasks-INITIAL_FORMS": "0",
                "tasks-MIN_NUM_FORMS": "0", "tasks-MAX_NUM_FORMS": "1000"}
        data.update(overrides)
        return data

    def test_admin_can_open_pages_and_dashboard_links_to_them(self):
        self.login(self.admin)
        for name in ("project_list", "project_create"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("project_edit", args=[self.project.pk])).status_code, 200)
        page = self.client.get(reverse("dashboard")).content.decode()
        for url in (reverse("project_list"), reverse("project_create"), reverse("admin:index")):
            self.assertIn(f'href="{url}"', page)

    def test_admin_creates_project_with_uppercase_code_and_audit(self):
        self.login(self.admin)
        response = self.client.post(reverse("project_create"), self.project_post())
        self.assertRedirects(response, reverse("project_list"))
        project = Project.objects.get(name="Beacon")
        self.assertEqual(project.code, "BCN-9")
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.PROJECT_CREATED,
                                                object_id=str(project.pk)).exists())

    def test_duplicate_code_and_bad_dates_rejected(self):
        self.login(self.admin)
        response = self.client.post(reverse("project_create"), self.project_post(code="atl-1"))
        self.assertContains(response, "already in use")
        response = self.client.post(reverse("project_create"), self.project_post(
            start_date="2026-05-10", end_date="2026-05-01"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Project.objects.filter(name="Beacon").exists())

    def test_edit_and_archive_hides_project_from_new_timesheets(self):
        self.login(self.admin)
        self.client.post(reverse("project_edit", args=[self.project.pk]),
                         self.project_post(name="Atlas", code="ATL-1", status="archived"))
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "archived")
        self.login(self.alice)
        page = self.client.get(reverse("timesheet_create")).content.decode()
        self.assertNotIn("Atlas (ATL-1)", page)  # option label; the flash text is unrelated

    def test_search_and_status_filter(self):
        Project.objects.create(name="Zephyr", code="ZEP-1", status="completed")
        self.login(self.admin)
        self.assertContains(self.client.get(reverse("project_list"), {"q": "zephyr"}), "Zephyr")
        self.assertNotContains(self.client.get(reverse("project_list"), {"q": "zephyr"}), "Atlas")
        self.assertNotContains(self.client.get(reverse("project_list"), {"status": "completed"}), "Atlas")

    def test_no_client_or_hourly_rate_fields_anywhere(self):
        self.login(self.admin)
        for name in ("project_create", "employee_create"):
            self.assertNotContains(self.client.get(reverse(name)), "lient")
        self.assertNotContains(self.client.get(reverse("employee_create")), "ourly")
        self.assertNotContains(self.client.get(reverse("project_create")), "Description")
        self.assertContains(self.client.get(reverse("project_create")), "Stage Status")

    def test_stage_status_choices_are_architecture_stages(self):
        values = Project.Status.values
        for stage in ("schematic", "design_development", "construction_documents",
                      "construction_administration"):
            self.assertIn(stage, values)

    def test_admin_adds_edits_and_removes_checklist_tasks(self):
        self.login(self.admin)
        data = self.project_post(**{"tasks-TOTAL_FORMS": "2", "tasks-0-name": "Floor plans",
                                    "tasks-1-name": "Elevations"})
        self.client.post(reverse("project_create"), data)
        project = Project.objects.get(name="Beacon")
        self.assertEqual(list(project.tasks.values_list("name", flat=True)), ["Floor plans", "Elevations"])
        first, second = list(project.tasks.all())
        edit = self.project_post(**{
            "name": "Beacon", "code": "BCN-9",
            "tasks-TOTAL_FORMS": "3", "tasks-INITIAL_FORMS": "2",
            "tasks-0-id": first.pk, "tasks-0-project": project.pk, "tasks-0-name": "Floor plans v2",
            "tasks-1-id": second.pk, "tasks-1-project": project.pk, "tasks-1-name": "Elevations",
            "tasks-1-DELETE": "on",
            "tasks-2-name": "Sections",
        })
        self.client.post(reverse("project_edit", args=[project.pk]), edit)
        self.assertEqual(list(project.tasks.values_list("name", flat=True)), ["Floor plans v2", "Sections"])

    def test_checklist_tasks_offered_on_timesheet_form(self):
        self.project.tasks.create(name="Site analysis")
        self.login(self.alice)
        response = self.client.get(reverse("timesheet_create"))
        self.assertEqual(response.context["form"].project_tasks_map, {str(self.project.pk): ["Site analysis"]})
        self.assertContains(response, "project-tasks-map")

    def test_delete_unused_project_but_protect_used_one(self):
        make_entry(self.alice, project=self.project)
        spare = Project.objects.create(name="Spare", code="SPR-1")
        self.login(self.admin)
        self.client.post(reverse("project_delete", args=[self.project.pk]))
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.client.post(reverse("project_delete", args=[spare.pk]))
        self.assertFalse(Project.objects.filter(pk=spare.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.PROJECT_DELETED).exists())

    def test_delete_requires_post(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("project_delete", args=[self.project.pk])).status_code, 405)

    def test_employee_and_anonymous_are_blocked(self):
        urls = [reverse("project_list"), reverse("project_create"),
                reverse("project_edit", args=[self.project.pk])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302)  # anonymous -> login
        self.login(self.alice)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(reverse("project_create"), self.project_post()).status_code, 403)
        self.assertEqual(self.client.post(reverse("project_delete", args=[self.project.pk])).status_code, 403)
        self.assertFalse(Project.objects.filter(name="Beacon").exists())
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn(reverse("project_list"), page)


class AccessControlTests(BaseTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.social = Project.objects.create(name="Instagram Campaign", code="SOC-1", section="marketing_social")
        cls.admin_proj = Project.objects.create(name="Office Admin", code="ADM-1", section="admin")
        cls.network = Project.objects.create(name="Networking Events", code="NET-1", section="networking")
        cls.secret = Project.objects.create(name="Secret House", code="SEC-1")

    def ids(self, user):
        from .access import accessible_projects
        return set(accessible_projects(user).values_list("code", flat=True))

    def test_admin_sees_everything_employee_sees_only_assigned(self):
        self.assertEqual(self.ids(self.admin), {"ATL-1", "SOC-1", "ADM-1", "NET-1", "SEC-1"})
        self.assertEqual(self.ids(self.alice), {"ATL-1"})
        self.assertEqual(self.ids(self.bob), set())

    def test_restricted_section_needs_both_assignment_and_section_grant(self):
        ProjectAccess.objects.create(user=self.bob, project=self.social)
        self.assertEqual(self.ids(self.bob), set())  # assigned but section not granted
        SectionAccess.objects.create(user=self.bob, section="marketing_social")
        self.assertEqual(self.ids(self.bob), {"SOC-1"})
        SectionAccess.objects.create(user=self.alice, section="admin")  # grant without assignment
        self.assertEqual(self.ids(self.alice), {"ATL-1"})

    def test_direct_urls_for_unassigned_projects_are_404(self):
        self.login(self.bob)
        self.assertEqual(self.client.get(reverse("my_project_detail", args=[self.project.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("my_project_detail", args=[self.secret.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("my_projects")), "Atlas")
        self.login(self.alice)
        self.assertEqual(self.client.get(reverse("my_project_detail", args=[self.project.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("my_project_detail", args=[self.secret.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("my_project_detail", args=[self.social.pk])).status_code, 404)

    def test_employee_cannot_use_management_urls(self):
        self.login(self.alice)
        for url in (reverse("access_list"), reverse("access_edit", args=[self.bob.pk]),
                    reverse("project_edit", args=[self.secret.pk]), reverse("project_create"),
                    reverse("project_list")):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        response = self.client.post(reverse("access_edit", args=[self.alice.pk]),
                                    {"projects": [self.secret.pk], "sections": ["admin"]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.ids(self.alice), {"ATL-1"})

    def test_timesheet_rejects_unassigned_project_and_hides_it_from_form(self):
        self.login(self.alice)
        page = self.client.get(reverse("timesheet_create")).content.decode()
        self.assertIn("Atlas", page)
        self.assertNotIn("Secret House", page)
        self.assertNotIn("Instagram Campaign", page)
        response = self.client.post(reverse("timesheet_create"), valid_post(project=self.secret.pk, action="save"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TimesheetEntry.objects.filter(project=self.secret).exists())
        response = self.client.post(reverse("timesheet_create"), valid_post(project=self.social.pk, action="save"))
        self.assertFalse(TimesheetEntry.objects.filter(project=self.social).exists())

    def test_timesheet_filter_dropdown_does_not_leak_other_projects(self):
        self.login(self.alice)
        page = self.client.get(reverse("timesheet_list")).content.decode()
        self.assertNotIn("Secret House", page)
        self.assertNotIn("Instagram Campaign", page)

    def test_admin_assigns_and_removes_access_and_it_applies_immediately(self):
        self.login(self.admin)
        url = reverse("access_edit", args=[self.bob.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"projects": [self.secret.pk, self.social.pk], "sections": ["marketing_social"]})
        self.assertEqual(self.ids(self.bob), {"SEC-1", "SOC-1"})
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ACCESS_UPDATED).exists())
        self.login(self.bob)
        self.assertEqual(self.client.get(reverse("my_project_detail", args=[self.secret.pk])).status_code, 200)
        self.login(self.admin)
        self.client.post(url, {"projects": [self.secret.pk]})
        self.assertEqual(self.ids(self.bob), {"SEC-1"})
        self.login(self.bob)
        self.assertEqual(self.client.get(reverse("my_project_detail", args=[self.social.pk])).status_code, 404)

    def test_admin_account_shows_full_access_and_list_shows_everyone(self):
        self.login(self.admin)
        self.assertContains(self.client.get(reverse("access_edit", args=[self.admin.pk])), "Full administrator access")
        people = {u.username for u in self.client.get(reverse("access_list")).context["people"]}
        self.assertTrue({"alice", "bob", "boss"} <= people)


class TimeInLieuTests(BaseTestCase):
    def til_post(self, **kw):
        data = {"entry_date": "2026-01-05", "kind": "earned", "hours": "8", "note": "Saturday site visit"}
        data.update(kw)
        return data

    def test_admin_records_earned_and_taken_and_balance_updates(self):
        self.login(self.admin)
        url = reverse("til_ledger", args=[self.alice.pk])
        self.client.post(url, self.til_post())
        self.client.post(url, self.til_post(kind="taken", hours="3"))
        self.client.post(url, self.til_post(kind="deducted", hours="1.5"))
        response = self.client.get(url)
        self.assertEqual(response.context["earned"], Decimal("8"))
        self.assertEqual(response.context["used"], Decimal("4.5"))
        self.assertEqual(response.context["balance"], Decimal("3.5"))
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.TIL_RECORDED).count(), 3)

    def test_cannot_take_more_than_banked(self):
        self.login(self.admin)
        url = reverse("til_ledger", args=[self.alice.pk])
        self.client.post(url, self.til_post(hours="2"))
        response = self.client.post(url, self.til_post(kind="taken", hours="5"))
        self.assertContains(response, "banked hours are available")
        self.assertEqual(TimeInLieuEntry.objects.filter(employee=self.alice).count(), 1)

    def test_rejects_zero_and_negative_hours(self):
        self.login(self.admin)
        url = reverse("til_ledger", args=[self.alice.pk])
        for bad in ("0", "-2"):
            self.assertEqual(self.client.post(url, self.til_post(hours=bad)).status_code, 200)
        self.assertFalse(TimeInLieuEntry.objects.exists())

    def test_employee_sees_only_own_ledger_and_cannot_write(self):
        TimeInLieuEntry.objects.create(employee=self.bob, entry_date="2026-01-01", kind="earned", hours=5)
        self.login(self.alice)
        self.assertRedirects(self.client.get(reverse("til_overview")),
                             reverse("til_ledger", args=[self.alice.pk]))
        self.assertEqual(self.client.get(reverse("til_ledger", args=[self.alice.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("til_ledger", args=[self.bob.pk])).status_code, 403)
        self.client.post(reverse("til_ledger", args=[self.alice.pk]), self.til_post())
        self.assertFalse(TimeInLieuEntry.objects.filter(employee=self.alice).exists())
        entry = TimeInLieuEntry.objects.get(employee=self.bob)
        self.assertEqual(self.client.post(reverse("til_delete", args=[entry.pk])).status_code, 403)
        self.assertTrue(TimeInLieuEntry.objects.filter(pk=entry.pk).exists())

    def test_overview_lists_balances_for_staff_and_employees(self):
        TimeInLieuEntry.objects.create(employee=self.alice, entry_date="2026-01-01", kind="earned", hours=6)
        TimeInLieuEntry.objects.create(employee=self.admin, entry_date="2026-01-01", kind="earned", hours=2)
        TimeInLieuEntry.objects.create(employee=self.alice, entry_date="2026-01-02", kind="taken", hours=2)
        self.login(self.admin)
        people = {p.pk: p for p in self.client.get(reverse("til_overview")).context["people"]}
        self.assertEqual(people[self.alice.pk].til_balance, Decimal("4"))
        self.assertEqual(people[self.admin.pk].til_balance, Decimal("2"))
        self.assertEqual(people[self.bob.pk].til_balance, Decimal("0"))

    def test_admin_can_delete_entry_and_anonymous_blocked(self):
        entry = TimeInLieuEntry.objects.create(employee=self.alice, entry_date="2026-01-01", kind="earned", hours=1)
        self.assertEqual(self.client.get(reverse("til_overview")).status_code, 302)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("til_delete", args=[entry.pk])).status_code, 405)
        self.client.post(reverse("til_delete", args=[entry.pk]))
        self.assertFalse(TimeInLieuEntry.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.TIL_DELETED).exists())

    def test_balance_shown_on_employee_dashboard(self):
        TimeInLieuEntry.objects.create(employee=self.alice, entry_date="2026-01-01", kind="earned", hours=7.5)
        self.login(self.alice)
        self.assertEqual(self.client.get(reverse("dashboard")).context["til_balance"], Decimal("7.5"))


class StaffAccountTests(BaseTestCase):
    def staff_post(self, **overrides):
        data = {"username": "newstaff", "first_name": "Nora", "last_name": "Staff",
                "email": "nora@example.com", "password1": PASSWORD, "password2": PASSWORD,
                "is_active": "on", "employee_id": "", "department": "Admin", "job_title": "Supervisor"}
        data.update(overrides)
        return data

    def test_admin_creates_staff_account_that_can_sign_in_as_administrator(self):
        self.login(self.admin)
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(f'href="{reverse("staff_create")}"', page)
        response = self.client.post(reverse("staff_create"), self.staff_post())
        user = User.objects.get(username="newstaff")
        self.assertRedirects(response, reverse("employee_detail", args=[user.pk]))
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotEqual(user.password, PASSWORD)
        self.assertTrue(user.profile.employee_id)
        log = AuditLog.objects.get(action=AuditLog.Action.EMPLOYEE_CREATED, object_id=str(user.pk))
        self.assertIn("Staff", log.details)
        # The new staff account really has administrator access.
        self.client.logout()
        self.assertTrue(self.client.login(username="newstaff", password=PASSWORD))
        self.assertEqual(self.client.get(reverse("reports")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:timesheets_project_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:auth_user_changelist")).status_code, 403)

    def test_regular_employee_form_still_creates_non_admin(self):
        self.login(self.admin)
        self.client.post(reverse("employee_create"), self.staff_post(username="plain"))
        self.assertFalse(User.objects.get(username="plain").is_staff)

    def test_cannot_smuggle_privileges_through_posted_fields(self):
        self.login(self.admin)
        self.client.post(reverse("staff_create"), self.staff_post(is_superuser="on", is_staff="on"))
        self.assertFalse(User.objects.get(username="newstaff").is_superuser)
        self.client.post(reverse("employee_create"), self.staff_post(username="sneaky", is_staff="on"))
        self.assertFalse(User.objects.get(username="sneaky").is_staff)

    def test_validation_errors(self):
        self.login(self.admin)
        r = self.client.post(reverse("staff_create"), self.staff_post(password2="different"))
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("staff_create"), self.staff_post(password1="short", password2="short"))
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("staff_create"), self.staff_post(username="alice"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="newstaff").exists())

    def test_employee_and_anonymous_cannot_use_page(self):
        self.assertEqual(self.client.get(reverse("staff_create")).status_code, 302)
        self.login(self.alice)
        self.assertEqual(self.client.get(reverse("staff_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("staff_create"), self.staff_post()).status_code, 403)
        self.assertFalse(User.objects.filter(username="newstaff").exists())

    def test_role_filter_on_employee_list(self):
        self.login(self.admin)
        staff_only = self.client.get(reverse("employee_list"), {"role": "staff"}).content.decode()
        self.assertIn("boss", staff_only)
        self.assertNotIn("alice", staff_only)
        emp_only = self.client.get(reverse("employee_list"), {"role": "employee"}).content.decode()
        self.assertIn("alice", emp_only)
        self.assertNotIn("boss@example.com", emp_only)


class TimeInLieuRequestTests(BaseTestCase):
    def req_post(self, **kw):
        data = {"action": "request", "kind": "earned",
                "work_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
                "hours": "4", "reason": "Saturday site visit"}
        data.update(kw)
        return data

    def make_request(self, user, hours="4", **kw):
        return TimeInLieuRequest.objects.create(
            employee=user, work_date=timezone.localdate() - timedelta(days=1),
            hours=Decimal(hours), reason="Saturday site visit", **kw)

    def test_employee_request_is_pending_and_adds_nothing_to_balance(self):
        self.login(self.alice)
        url = reverse("til_ledger", args=[self.alice.pk])
        self.assertRedirects(self.client.post(url, self.req_post()), url)
        req = TimeInLieuRequest.objects.get(employee=self.alice)
        self.assertEqual(req.status, "pending")
        self.assertFalse(TimeInLieuEntry.objects.exists())
        response = self.client.get(url)
        self.assertEqual(response.context["balance"], Decimal("0"))
        self.assertEqual(response.context["pending_earned"], Decimal("4"))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.TIL_REQUESTED).exists())

    def test_invalid_requests_are_rejected(self):
        self.login(self.alice)
        url = reverse("til_ledger", args=[self.alice.pk])
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        for bad in ({"hours": "0"}, {"hours": "-1"}, {"hours": "25"}, {"work_date": tomorrow}, {"reason": "  "}):
            self.assertEqual(self.client.post(url, self.req_post(**bad)).status_code, 200)
        self.assertFalse(TimeInLieuRequest.objects.exists())

    def test_cannot_request_on_behalf_of_someone_else(self):
        self.login(self.alice)
        self.assertEqual(self.client.post(reverse("til_ledger", args=[self.bob.pk]), self.req_post()).status_code, 403)
        self.assertFalse(TimeInLieuRequest.objects.exists())

    def test_admin_approval_banks_hours_once(self):
        req = self.make_request(self.alice)
        self.login(self.admin)
        url = reverse("til_request_approve", args=[req.pk])
        self.client.post(url)
        self.client.post(url)  # double click / replay must not double-credit
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")
        self.assertEqual(req.reviewed_by, self.admin)
        entries = TimeInLieuEntry.objects.filter(employee=self.alice)
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().kind, "earned")
        self.assertEqual(req.entry, entries.get())
        response = self.client.get(reverse("til_ledger", args=[self.alice.pk]))
        self.assertEqual(response.context["balance"], Decimal("4"))
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.TIL_REQUEST_APPROVED).count(), 1)

    def test_rejection_adds_nothing_and_keeps_comment(self):
        req = self.make_request(self.alice)
        self.login(self.admin)
        self.client.post(reverse("til_request_reject", args=[req.pk]), {"comment": "Not pre-approved"})
        req.refresh_from_db()
        self.assertEqual((req.status, req.review_comment), ("rejected", "Not pre-approved"))
        self.assertFalse(TimeInLieuEntry.objects.exists())
        self.client.post(reverse("til_request_approve", args=[req.pk]))  # cannot flip a decided request
        self.assertFalse(TimeInLieuEntry.objects.exists())

    def test_employees_cannot_approve_or_reject(self):
        req = self.make_request(self.alice)
        self.login(self.alice)
        self.assertEqual(self.client.post(reverse("til_request_approve", args=[req.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("til_request_reject", args=[req.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("til_requests")).status_code, 403)
        req.refresh_from_db()
        self.assertEqual(req.status, "pending")
        self.assertFalse(TimeInLieuEntry.objects.exists())

    def test_admin_cannot_approve_own_request(self):
        req = self.make_request(self.admin)
        self.login(self.admin)
        self.client.post(reverse("til_request_approve", args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.status, "pending")
        self.assertFalse(TimeInLieuEntry.objects.exists())
        other = make_user("boss2", staff=True)
        self.login(other)
        self.client.post(reverse("til_request_approve", args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")
        self.assertEqual(TimeInLieuEntry.objects.get().employee, self.admin)

    def test_employee_can_cancel_own_pending_only(self):
        mine, theirs = self.make_request(self.alice), self.make_request(self.bob)
        self.login(self.alice)
        self.client.post(reverse("til_request_cancel", args=[theirs.pk]))
        self.client.post(reverse("til_request_cancel", args=[mine.pk]))
        mine.refresh_from_db(); theirs.refresh_from_db()
        self.assertEqual((mine.status, theirs.status), ("cancelled", "pending"))
        self.login(self.admin)
        self.client.post(reverse("til_request_approve", args=[mine.pk]))  # cancelled cannot be approved
        self.assertFalse(TimeInLieuEntry.objects.exists())

    def test_queue_and_nav_badge_for_admin(self):
        self.make_request(self.alice); self.make_request(self.bob)
        self.login(self.admin)
        response = self.client.get(reverse("til_requests"))
        self.assertEqual(len(response.context["requests_list"]), 2)
        self.assertEqual(response.context["til_pending_count"], 2)
        self.assertEqual(len(self.client.get(reverse("til_requests") + "?status=approved").context["requests_list"]), 0)

    def test_existing_admin_direct_recording_still_works(self):
        self.login(self.admin)
        url = reverse("til_ledger", args=[self.alice.pk])
        self.client.post(url, {"entry_date": "2026-01-05", "kind": "earned", "hours": "8", "note": "x"})
        self.assertEqual(TimeInLieuEntry.objects.filter(employee=self.alice).count(), 1)
        self.assertFalse(TimeInLieuRequest.objects.exists())

    # ---- request type: earned (banked) vs taken (time off)
    def bank(self, user, hours):
        TimeInLieuEntry.objects.create(employee=user, entry_date="2026-01-01", kind="earned", hours=Decimal(hours))

    def test_request_type_is_required(self):
        self.login(self.alice)
        url = reverse("til_ledger", args=[self.alice.pk])
        for bad in ("", "deducted", "nonsense"):
            self.assertEqual(self.client.post(url, self.req_post(kind=bad)).status_code, 200)
        self.assertFalse(TimeInLieuRequest.objects.exists())

    def test_taken_request_deducts_only_after_approval(self):
        self.bank(self.alice, "8")
        self.login(self.alice)
        url = reverse("til_ledger", args=[self.alice.pk])
        self.client.post(url, self.req_post(kind="taken", hours="3"))
        req = TimeInLieuRequest.objects.get(employee=self.alice)
        self.assertEqual(req.kind, "taken")
        response = self.client.get(url)
        self.assertEqual(response.context["balance"], Decimal("8"))
        self.assertEqual(response.context["pending_taken"], Decimal("3"))
        self.login(self.admin)
        self.client.post(reverse("til_request_approve", args=[req.pk]))
        entry = TimeInLieuEntry.objects.get(employee=self.alice, kind="taken")
        self.assertEqual(entry.hours, Decimal("3"))
        self.assertEqual(self.client.get(url).context["balance"], Decimal("5"))

    def test_taken_request_cannot_exceed_balance_including_pending_time_off(self):
        self.bank(self.alice, "5")
        self.login(self.alice)
        url = reverse("til_ledger", args=[self.alice.pk])
        self.assertEqual(self.client.post(url, self.req_post(kind="taken", hours="6")).status_code, 200)
        self.assertFalse(TimeInLieuRequest.objects.exists())
        self.client.post(url, self.req_post(kind="taken", hours="4"))
        response = self.client.post(url, self.req_post(kind="taken", hours="2"))  # only 1 h left unreserved
        self.assertContains(response, "banked hours are available")
        self.assertEqual(TimeInLieuRequest.objects.count(), 1)

    def test_taken_approval_is_rechecked_against_current_balance(self):
        self.bank(self.alice, "5")
        req = self.make_request(self.alice, hours="4", kind="taken")
        TimeInLieuEntry.objects.create(employee=self.alice, entry_date="2026-02-01", kind="deducted", hours=Decimal("3"))
        self.login(self.admin)
        self.client.post(reverse("til_request_approve", args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.status, "pending")
        self.assertFalse(TimeInLieuEntry.objects.filter(kind="taken").exists())

    def test_taken_date_may_be_in_future_but_earned_may_not(self):
        self.bank(self.alice, "8")
        self.login(self.alice)
        url = reverse("til_ledger", args=[self.alice.pk])
        future = (timezone.localdate() + timedelta(days=7)).isoformat()
        self.assertEqual(self.client.post(url, self.req_post(kind="earned", work_date=future)).status_code, 200)
        self.assertFalse(TimeInLieuRequest.objects.exists())
        self.client.post(url, self.req_post(kind="taken", work_date=future, hours="2"))
        self.assertEqual(TimeInLieuRequest.objects.filter(kind="taken").count(), 1)

