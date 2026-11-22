"""Optional LIVE Gemini check — run only after putting GEMINI_API_KEY in .env.

    python manage.py gemini_smoke              # test the configured models
    python manage.py gemini_smoke --find-model # also try other text models and recommend one that works
Prints status codes and model names only; the key is never printed.
"""
import time

from django.core.management.base import BaseCommand

from apps.intelligence.gemini import GeminiClient, GeminiError

SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}


class Command(BaseCommand):
    help = "Live smoke test of the configured Gemini models (needs GEMINI_API_KEY)."

    def add_arguments(self, parser):
        parser.add_argument("--find-model", action="store_true", help="Try other text models and recommend a working GEMINI_MODEL.")

    def gen(self, client, model):
        old = client.model
        client.model = model
        t = time.monotonic()
        try:
            out, rev = client.generate_json("Reply with JSON only.", 'Return {"status": "ok"}.', SCHEMA, timeout=max(client.timeout, 30))
            return True, f"{int((time.monotonic() - t) * 1000)} ms", out
        except GeminiError as exc:
            return False, exc.code, None
        finally:
            client.model = old

    def handle(self, *args, **opts):
        c = GeminiClient()
        if not c.configured:
            self.stdout.write(self.style.WARNING("GEMINI_API_KEY is not set (or GEMINI_ENABLED=0). Add it to .env, then re-run. Nothing was sent."))
            return
        self.stdout.write(f"Model: {c.model} · Embedding model: {c.embedding_model} · Base: {c.base_url}")
        ok = True
        try:
            models = c.list_model_details()
        except GeminiError as exc:
            self.stdout.write(self.style.ERROR(f"FAIL list models: {exc.code} — {exc.safe_message}"))
            return
        names = {m["name"]: m["methods"] for m in models}
        text_models = [n for n, meth in names.items() if "generateContent" in meth]
        self.stdout.write(self.style.SUCCESS(f"PASS list models: {len(names)} visible, {len(text_models)} support text generation"))
        passed, info, out = self.gen(c, c.model)
        if passed:
            self.stdout.write(self.style.SUCCESS(f"PASS generateContent with {c.model} ({info}): {out}"))
        else:
            ok = False
            self.stdout.write(self.style.ERROR(f"FAIL generateContent with {c.model}: {info}"))
        try:
            t = time.monotonic()
            v = c.embed(["24-inch HDMI monitor", "display screen"])
            self.stdout.write(self.style.SUCCESS(f"PASS embeddings with {c.embedding_model} ({int((time.monotonic() - t) * 1000)} ms): dimension {len(v[0])}"))
        except GeminiError as exc:
            ok = False
            self.stdout.write(self.style.ERROR(f"FAIL embeddings with {c.embedding_model}: {exc.code}"))
        if not passed or opts["find_model"]:
            candidates = sorted([n for n in text_models if "flash" in n and not any(x in n for x in ("image", "tts", "audio", "live", "thinking", "exp"))], reverse=True)[:8]
            self.stdout.write("Trying other text models (stable flash models first)…")
            for name in candidates:
                good, info, _ = self.gen(c, name)
                self.stdout.write((self.style.SUCCESS("  works   ") if good else self.style.ERROR("  fails   ")) + f"{name} ({info})")
                if good:
                    self.stdout.write(self.style.SUCCESS(f"\nRecommended: open .env and set   GEMINI_MODEL={name}   then restart the app."))
                    break
            else:
                self.stdout.write(self.style.WARNING("No flash model answered right now. Google may be busy — try again in a few minutes."))
        self.stdout.write(self.style.SUCCESS("Live Gemini smoke test passed.") if ok else self.style.WARNING("Some live calls failed; the app uses labelled fallbacks for those until fixed."))
