"""Build locale/ar/LC_MESSAGES/django.po and django.mo from the reviewed translation files.

locale/msgids_v1.json      English strings (frozen order)
locale/ar/partNN.json      Arabic strings, same order
locale/ar/extra.json       {english: arabic} for strings added later
No GNU gettext is needed: the .mo file is written directly (works the same on Windows and macOS).
"""
import json
import pathlib
import struct

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOC = ROOT / "locale"

ids = json.loads((LOC / "msgids_v1.json").read_text(encoding="utf-8"))
arabic = []
for part in sorted((LOC / "ar").glob("part*.json")):
    arabic += json.loads(part.read_text(encoding="utf-8"))
if len(arabic) != len(ids):
    raise SystemExit(f"{len(ids)} English strings but {len(arabic)} Arabic strings — files are out of step.")
catalog = dict(zip(ids, arabic))
extra = LOC / "ar" / "extra.json"
if extra.exists():
    catalog.update(json.loads(extra.read_text(encoding="utf-8")))
catalog = {k: v for k, v in catalog.items() if k and v}


def po_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


header = "Content-Type: text/plain; charset=UTF-8\nLanguage: ar\nPlural-Forms: nplurals=6; plural=n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 && n%100<=99 ? 4 : 5;\n"
out_dir = LOC / "ar" / "LC_MESSAGES"
out_dir.mkdir(parents=True, exist_ok=True)
lines = ['msgid ""', f'msgstr "{po_escape(header)}"', ""]
for k in sorted(catalog):
    lines += [f'msgid "{po_escape(k)}"', f'msgstr "{po_escape(catalog[k])}"', ""]
(out_dir / "django.po").write_text("\n".join(lines), encoding="utf-8")

# GNU .mo format: header, then two tables of (length, offset) for originals and translations.
entries = {"": header, **catalog}
keys = sorted(entries)
kb = [k.encode("utf-8") for k in keys]
vb = [entries[k].encode("utf-8") for k in keys]
n = len(keys)
start = 7 * 4 + 16 * n
koffs, voffs, kdata, vdata = [], [], b"", b""
for k in kb:
    koffs.append((len(k), start + len(kdata)))
    kdata += k + b"\0"
vstart = start + len(kdata)
for v in vb:
    voffs.append((len(v), vstart + len(vdata)))
    vdata += v + b"\0"
mo = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + 8 * n, 0, 0)
mo += b"".join(struct.pack("ii", *o) for o in koffs) + b"".join(struct.pack("ii", *o) for o in voffs) + kdata + vdata
(out_dir / "django.mo").write_bytes(mo)
print(f"Wrote {len(catalog)} translations to {out_dir}")
