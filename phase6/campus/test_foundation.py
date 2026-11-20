from django.test import TestCase
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from .models import Department, Profile, Asset
from .policy import require
class FoundationTests(TestCase):
    def setUp(self):
        self.dept=Department.objects.create(name='Foundation test department')
        self.user=User.objects.create_user('foundation',password='Test-only-123!')
        Profile.objects.create(user=self.user,department=self.dept,role='Requester')
    def test_role_is_explicit(self):
        require(self.user,'Requester')
        with self.assertRaises(PermissionDenied): require(self.user,'Assigned Approver')
    def test_department_deactivation_preserves_records(self):
        self.dept.active=False; self.dept.save()
        with self.assertRaises(PermissionDenied): require(self.user,'Requester')
        self.assertEqual(Profile.objects.count(),1)
    def test_negative_stock_database_constraint(self):
        with self.assertRaises(IntegrityError),transaction.atomic(): Asset.objects.create(code='BAD',name='Invalid',description='Invalid',category='Computers',quantity=0,department=self.dept,custodian=self.user,building='B',room='1')
    def test_anonymous_admin_requires_login(self):
        self.assertEqual(self.client.get('/admin/').status_code,302)
