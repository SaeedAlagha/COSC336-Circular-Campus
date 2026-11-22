"""Collect every interface string that needs an Arabic translation (run: python scripts/i18n_collect.py).

Writes locale/msgids.json. Translations live in locale/ar.json (msgid -> Arabic);
scripts/i18n_build.py turns them into locale/ar/LC_MESSAGES/django.po and django.mo.
"""
import ast
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("CAMPUS_TESTING", "1")
import django  # noqa: E402

django.setup()

ids = set()
TRANS = re.compile(r"""{%\s*translate\s+(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
INCLUDE_ARGS = re.compile(r"""\b(?:title|text|link_text|hint)=(?:"([^"]*)"|'([^']*)')""")
for p in (ROOT / "templates").rglob("*.html"):
    src = p.read_text(encoding="utf-8")
    for m in TRANS.finditer(src):
        ids.add(m.group(1) if m.group(1) is not None else m.group(2))
    for inc in re.findall(r"{%\s*include[^%]*%}", src):
        for m in INCLUDE_ARGS.finditer(inc):
            ids.add(m.group(1) if m.group(1) is not None else m.group(2))

# Python sources: plain (non f-string) literals shown to people.
SHOWN = {"BusinessError", "_err", "messages.success", "messages.error", "messages.info", "messages.warning", "ValidationError",
         "forms.ValidationError", "require", "policy.require", "add_error", "ValueError"}


def call_name(node):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        base = f.value.id if isinstance(f.value, ast.Name) else ""
        return f"{base}.{f.attr}" if base else f.attr
    return ""


for p in (ROOT / "apps").rglob("*.py"):
    if "migrations" in p.parts or p.name.startswith("test"):
        continue
    tree = ast.parse(p.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (call_name(node) in SHOWN or call_name(node).split(".")[-1] in {"require", "add_error", "_err"}):
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and re.search(r"[A-Za-z]{3,}", arg.value) and " " in arg.value:
                    ids.add(arg.value)
                if isinstance(arg, ast.Dict):
                    for v in arg.values:
                        if isinstance(v, ast.Constant) and isinstance(v.value, str) and re.search(r"[A-Za-z]{3,}", v.value):
                            ids.add(v.value)
        # Labels / help texts / choices / limitations written as keyword arguments.
        if isinstance(node, ast.keyword) and node.arg in {"label", "help_text", "empty_label", "limitations", "verbose_name"}:
            vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            for v in vals:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    ids.add(v.value)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"CHOICES", "STATUSES", "TYPES", "RESULTS", "WORK_TYPES", "METHODS", "DECISIONS", "STAGES", "KINDS", "INDICATORS"} for t in node.targets):
            for elt in getattr(node.value, "elts", []):
                if isinstance(elt, ast.Tuple) and len(elt.elts) == 2 and isinstance(elt.elts[1], ast.Constant):
                    ids.add(elt.elts[1].value)

# Navigation labels and group names.
ctx = (ROOT / "apps/core/context.py").read_text(encoding="utf-8")
ids.update(re.findall(r'\("[a-z]+", "([^"]+)", "[a-z0-9-]+", "[a-z]+:[a-z_]+"\)', ctx))
ids.update(re.findall(r'nav\.append\(\("([^"]+)"', ctx))
ids.update(["Workspace", "Insight", "Administration", "No role assigned"])

# Form field labels and help texts exactly as Django renders them.
from django import forms  # noqa: E402

from apps.accounts import forms as af  # noqa: E402
from apps.catalog import forms as cf  # noqa: E402
from apps.exchange import forms as ef  # noqa: E402
from apps.reporting import views as rv  # noqa: E402
from apps.workflow import views as wv  # noqa: E402

for mod in (af, cf, ef, rv, wv):
    for name in dir(mod):
        cls = getattr(mod, name)
        if isinstance(cls, type) and issubclass(cls, forms.Form) and cls.__module__ == mod.__name__:
            for fname, field in cls.base_fields.items():
                ids.add(str(field.label) if field.label else fname.replace("_", " ").capitalize())
                if field.help_text:
                    ids.add(str(field.help_text))
                for _v, lab in getattr(field, "choices", []) or []:
                    if isinstance(lab, str):
                        ids.add(lab)

# Leaderboard, metrics and assistant suggestions.
from apps.reporting import leaderboard  # noqa: E402

for b in leaderboard.BADGES:
    ids.update([b.name, b.rule])
ids.update(v[0] for v in leaderboard.PERIODS.values())
ids.add(leaderboard.FORMULA)
mt = (ROOT / "apps/reporting/metrics.py").read_text(encoding="utf-8")
ids.update(re.findall(r'indicator\("[a-z_]+", "([^"]+)"', mt))
ids.update(re.findall(r'note="([^"{}]+)"', mt))
ids.update(re.findall(r'"([a-z][a-z0-9 ()/,\-]+)":', mt.split("def compute", 1)[1]))
iv = (ROOT / "apps/intelligence/views.py").read_text(encoding="utf-8")
ids.update(re.findall(r'"([A-Z][^"]{8,}\?)"', iv))
ids.update(re.findall(r'"(I need [^"]+|Show [^"]+)"', iv))

ids = sorted(i for i in ids if i and re.search(r"[A-Za-z]{2,}", i))
(ROOT / "locale").mkdir(exist_ok=True)
(ROOT / "locale" / "msgids.json").write_text(json.dumps(ids, ensure_ascii=False, indent=0), encoding="utf-8")
print(len(ids), "strings")
