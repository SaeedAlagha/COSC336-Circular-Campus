"""C1: authentication, roles and server-side authorisation (direct GET and POST, not hidden buttons)."""
from django.test import TestCase
from django.urls import reverse

from apps.accounts import services
from apps.core.errors import BusinessError
from apps.core.testing import PW, World
from apps.reporting.models import AuditEvent


class AuthTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_login_logout_and_generic_failure(self):
        r = self.client.post(reverse("accounts:login"), {"campus_id": "100060101", "password": PW})
        self.assertRedirects(r, reverse("core:dashboard"))
        r = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(r, reverse("accounts:login"))
        r = self.client.post(reverse("accounts:login"), {"campus_id": "100060101", "password": "wrong password!!"})
        r2 = self.client.post(reverse("accounts:login"), {"campus_id": "100069999", "password": "wrong password!!"})
        self.assertContains(r, "Sign-in failed")
        self.assertEqual(r.context["form"].non_field_errors(), r2.context["form"].non_field_errors(), "same message for unknown ID and wrong password")
        for bad in ("NOPE-1", "12345", "1000604120", "200060412", "10006041a"):
            r3 = self.client.post(reverse("accounts:login"), {"campus_id": bad, "password": "wrong password!!"})
            self.assertIn("campus_id", r3.context["form"].errors, bad)  # format error only; never says whether an account exists
        self.assertTrue(AuditEvent.objects.filter(action="Sign-in failed").exists())

    def test_inactive_account_and_deactivated_session(self):
        self.client.force_login(self.w.requester)
        services.set_active(self.w.admin, self.w.requester, False)
        r = self.client.get(reverse("core:dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(reverse("accounts:login")))
        r = self.client.post(reverse("accounts:login"), {"campus_id": "100060101", "password": PW})
        self.assertContains(r, "Sign-in failed")

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get(reverse("catalog:list")).status_code, 302)

    def test_account_validation(self):
        with self.assertRaises(BusinessError) as ctx:
            services.create_user(self.w.admin, "100060101", "Dup", self.w.cs, "", PW, PW)
        self.assertIn("campus_id", ctx.exception.field_errors)
        with self.assertRaises(BusinessError) as ctx:
            services.create_user(self.w.admin, "100067777", "Short Pw", self.w.cs, "", "short", "short")
        self.assertIn("password1", ctx.exception.field_errors)
        with self.assertRaises(BusinessError):
            services.create_user(self.w.admin, "100067778", "Mismatch", self.w.cs, "", PW, PW + "x")
        u = services.create_user(self.w.admin, " 100067779 ", "Good User", self.w.cs, "good@example.edu", PW, PW)
        self.assertEqual(u.campus_id, "100067779")  # spaces stripped
        self.assertNotIn(PW, u.password, "only a salted hash is stored")

    def test_admin_cannot_grant_self_approval(self):
        with self.assertRaises(BusinessError):
            services.grant_role(self.w.admin, self.w.admin, "APPROVER", self.w.cs)

    def test_non_admin_cannot_manage_accounts_directly(self):
        self.client.force_login(self.w.approver)
        self.assertEqual(self.client.get(reverse("accounts:users")).status_code, 403)
        r = self.client.post(reverse("accounts:users"), {"campus_id": "U-1", "full_name": "x", "department": self.w.cs.pk, "password1": PW, "password2": PW})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(AuditEvent.objects.filter(outcome="DENIED", actor=self.w.approver).exists())

    def test_department_deactivation_keeps_records(self):
        a = self.w.asset()
        services.set_department_active(self.w.admin, self.w.eng, False)
        a.refresh_from_db()
        self.assertEqual(a.owner_department, self.w.eng)


class DirectAccessTests(TestCase):
    """Server checks every protected read and write, including URLs typed directly."""

    def setUp(self):
        self.w = World()
        self.asset = self.w.asset(publish=False)

    def test_requester_cannot_register_or_publish_or_view_draft(self):
        self.client.force_login(self.w.requester)
        self.assertEqual(self.client.get(reverse("catalog:new")).status_code, 403)
        self.assertEqual(self.client.get(reverse("catalog:asset", args=[self.asset.pk])).status_code, 403)
        r = self.client.post(reverse("catalog:action", args=[self.asset.pk]), {"action": "publish", "version": self.asset.version})
        self.assertEqual(r.status_code, 403)
        self.asset.refresh_from_db()
        self.assertFalse(self.asset.is_published)

    def test_other_department_custodian_cannot_edit(self):
        self.client.force_login(self.w.cs_cust)
        self.assertEqual(self.client.get(reverse("catalog:edit", args=[self.asset.pk])).status_code, 403)

    def test_non_reviewer_cannot_post_a_decision(self):
        from apps.catalog import services as catalog
        catalog.publish(self.w.eng_cust, self.asset.pk, self.asset.version)
        req, version, hold = self.w.submit(self.w.request(), self.asset, 2)
        stage = version.approvals.get(stage="RELEASE_CHECK")
        self.client.force_login(self.w.requester)
        r = self.client.post(reverse("workflow:approval", args=[stage.pk]), {"decision": "APPROVED", "version_id": version.pk})
        stage.refresh_from_db()
        self.assertIsNone(stage.decision)
        self.client.force_login(self.w.tech)
        r = self.client.post(reverse("workflow:approval", args=[stage.pk]), {"decision": "APPROVED", "version_id": version.pk})
        stage.refresh_from_db()
        self.assertIsNone(stage.decision)

    def test_other_users_request_is_hidden(self):
        req = self.w.request()
        outsider = self.w.user("100060999", "Other Req", self.w.eng, [("REQUESTER", self.w.eng)])
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("exchange:request", args=[req.pk])).status_code, 403)

    def test_financial_fields_hidden_from_requesters(self):
        from apps.catalog import services as catalog
        catalog.publish(self.w.eng_cust, self.asset.pk, self.asset.version)
        self.client.force_login(self.w.requester)
        r = self.client.get(reverse("catalog:asset", args=[self.asset.pk]))
        self.assertContains(r, "Hidden for your role")
        self.assertNotContains(r, "AED 1,000")
