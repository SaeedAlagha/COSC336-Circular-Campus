from urllib.parse import urlencode

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from apps.accounts import policy
from apps.core.views import AppView

from . import assistant


class AssistantView(AppView):
    """AI feature 4. Conversation is kept in the session (last 12 turns) for this user only."""

    template_name = "intelligence/assistant.html"
    nav = "assistant"

    def get(self, request):
        if request.GET.get("clear"):
            request.session["assistant"] = []
            return redirect("intelligence:assistant")
        return self.render(request, {"turns": request.session.get("assistant", []), "suggestions": [
            "Any projectors or display screens available?", "What is the status of my requests?", "How long is a reservation held?",
            "I need 3 HDMI monitors for a teaching lab by next month", "Show chairs in the Library & Learning department"]})

    def post(self, request):
        question = request.POST.get("question", "")
        try:
            outcome, out, links = assistant.ask(request.user, question)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("intelligence:assistant")
        draft_url = ""
        if out.get("draft"):
            d = out["draft"]
            params = {k: d.get(k) for k in ("title", "purpose", "specifications", "quantity", "required_date", "urgency", "category") if d.get(k)}
            params["source"] = "assistant"
            draft_url = reverse("exchange:new") + "?" + urlencode(params)
        turn = {
            "q": question[:500], "a": out.get("answer", ""), "cited": [{"id": c, "url": links.get(c, "")} for c in out.get("cited_ids", [])],
            "insufficient": out.get("insufficient_evidence"), "refused": out.get("refused", False), "draft": out.get("draft"), "draft_url": draft_url,
            "source": outcome.result.source_label if outcome else "Server rule — no AI call made", "fallback": outcome.fallback if outcome else True,
            "fallback_reason": outcome.message if outcome else "", "result_id": str(outcome.result.pk) if outcome else "",
            "can_draft": policy.is_requester_like(request.user),
        }
        turns = (request.session.get("assistant", []) + [turn])[-12:]
        request.session["assistant"] = turns
        return redirect(reverse("intelligence:assistant") + "#latest")
