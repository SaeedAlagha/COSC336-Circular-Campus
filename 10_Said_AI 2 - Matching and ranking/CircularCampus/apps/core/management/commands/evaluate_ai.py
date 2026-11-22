"""Run the fixed, labelled classification sample (design 5.11.2) and store an EvaluationRun.

    python manage.py evaluate_ai
Uses Gemini when configured, otherwise the labelled keyword-rule fallback. The
sample is tiny and synthetic: results are NOT evidence of production accuracy.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Category
from apps.intelligence import classification
from apps.intelligence.models import EvaluationObservation, EvaluationRun

CASES = [
    ("c01", "Dell 24-inch monitor", "Small scratch on bezel, HDMI", "Monitor"),
    ("c02", "Samsung display screen", "27 inch LED panel", "Monitor"),
    ("c03", "ThinkPad notebook", "Core i5 laptop with charger", "Laptop"),
    ("c04", "HP desktop tower", "Office PC, 8GB RAM", "Desktop computer"),
    ("c05", "Mesh office chair", "Adjustable seat, worn armrests", "Chair"),
    ("c06", "Lab stool", "Height adjustable seating for benches", "Chair"),
    ("c07", "Oak writing desk", "Wooden table with drawer", "Desk & table"),
    ("c08", "Steel cupboard", "Two-door lockable cabinet", "Storage"),
    ("c09", "Epson projector", "Classroom beamer, HDMI", "Projector"),
    ("c10", "Student microscope", "Optical, 1000x", "Microscope"),
    ("c11", "Digital oscilloscope", "4 channel scope with probes", "Test & measurement"),
    ("c12", "Engineering maths textbook", "Kreyszig 10th edition book", "Textbooks"),
]


class Command(BaseCommand):
    help = "Evaluate classification on a fixed labelled sample and record the run."

    def handle(self, *args, **opts):
        from apps.accounts.models import User

        user = User.objects.filter(role_grants__role__code="CUSTODIAN").first()
        if user is None or not Category.objects.exists():
            raise CommandError("Load reference data first (python manage.py seed_demo).")
        rows, correct = [], 0
        for case_id, name, desc, expected in CASES:
            out = classification.suggest(user, name, desc)
            label = (out.output.get("category_label") or "").split(" / ")[-1]
            ok = label == expected
            correct += ok
            rows.append((case_id, expected, label, ok, out))
        first = rows[0][4].result
        run = EvaluationRun.objects.create(function="classify", model_version=first.model_version, dataset_version="classify-sample-v1",
                                           sample_size=len(CASES), metric_name="accuracy", metric_value=round(correct / len(CASES), 3), run_by=user,
                                           notes="Synthetic 12-item sample; fallback" if first.fallback else "Synthetic 12-item sample; Gemini")
        for case_id, expected, label, ok, out in rows:
            EvaluationObservation.objects.create(run=run, case_id=case_id, expected={"category": expected}, observed={"category": label, "source": out.result.source_label},
                                                 correct=ok, latency_ms=out.result.task.latency_ms, schema_ok=not out.result.task.error_code == "INVALID_SCHEMA")
            self.stdout.write(f"{case_id}  expected={expected:<20} got={label or '—':<20} {'✓' if ok else '✗'}")
        self.stdout.write(self.style.SUCCESS(f"Accuracy {correct}/{len(CASES)} using {first.source_label}. Stored as EvaluationRun {str(run.pk)[:8]}. Small synthetic sample — not a production accuracy claim."))
