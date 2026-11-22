from django.urls import path

from . import views

app_name = "workflow"
urlpatterns = [
    path("approvals/", views.QueueView.as_view(), name="queue"),
    path("approvals/<uuid:pk>/", views.ApprovalView.as_view(), name="approval"),
]
