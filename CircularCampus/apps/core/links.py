"""Safe link building for notifications and service records.

Works like django.urls.reverse, but returns "" when the target page is not installed,
so a business action never fails just because a notice cannot link to a page yet.
"""
from django.urls import NoReverseMatch
from django.urls import reverse as _reverse


def reverse(viewname, args=None, kwargs=None):
    try:
        return _reverse(viewname, args=args, kwargs=kwargs)
    except NoReverseMatch:
        return ""
