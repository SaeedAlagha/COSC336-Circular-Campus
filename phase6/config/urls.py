from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import path
from importlib.util import find_spec
from django.http import HttpResponse

def foundation(request):
    return HttpResponse('<h1>Circular Campus foundation</h1><p>Data model, policy and workflow services are installed.</p><p>The interface and AI/reporting review bundles must be integrated before the full demonstration.</p><a href="/admin/">Account administration</a>')

# Keep the foundation independently runnable while teammates integrate their bundles.
if all(find_spec(name) for name in ['campus.views','campus.intelligence','campus.reporting']):
    from campus import views as v
else:
    v=None
urlpatterns=([path('admin/',admin.site.urls),path('login/',v.CampusLogin.as_view(),name='login'),path('logout/',LogoutView.as_view(),name='logout'),path('',v.dashboard,name='dashboard'),path('assets/',v.catalog,name='catalog'),path('assets/new/',v.asset_edit,name='asset_new'),path('assets/<uuid:pk>/',v.asset_detail,name='asset'),path('assets/<uuid:pk>/edit/',v.asset_edit,name='asset_edit'),path('assets/<uuid:pk>/publish/',v.publish,name='publish'),path('attachments/<int:pk>/',v.attachment,name='attachment'),path('requests/',v.requests_view,name='requests'),path('requests/new/',v.need_create,name='need_new'),path('requests/<uuid:pk>/',v.need_detail,name='need'),path('workflows/',v.workflows,name='workflows'),path('workflows/<uuid:pk>/',v.transfer_detail,name='transfer'),path('maintenance/',v.maintenance,name='maintenance'),path('assets/<uuid:pk>/disposition/',v.disposition,name='disposition'),path('intelligence/',v.intelligence_view,name='intelligence'),path('intelligence/<int:pk>/review/',v.ai_review,name='ai_review'),path('reports/',v.reports,name='reports'),path('reports/evidence/<uuid:pk>/',v.impact_edit,name='impact'),path('audit/',v.audit_view,name='audit'),path('notices/',v.notices,name='notices')] if v else [path('admin/',admin.site.urls),path('',foundation)])
