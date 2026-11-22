from django.urls import path

from . import views

app_name = "intelligence"
urlpatterns = [path("", views.AssistantView.as_view(), name="assistant")]
