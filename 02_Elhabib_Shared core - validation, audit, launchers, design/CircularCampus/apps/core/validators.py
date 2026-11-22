"""Central input validation (NFR-10: every input is validated on the server).

One place defines what a valid value looks like, so forms, views and services
agree. Every helper either returns a clean Python value or raises a
``BusinessError(VALIDATION_ERROR)`` whose ``field_errors`` name the field, so
the screen shows a red message under that field instead of a server error.

Browser checks (``required``, ``min``, ``type=date``) are convenience only;
these server checks are the real gate because a request can be forged.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.http import Http404

from .errors import BusinessError

# ----------------------------------------------------------------- limits ---
MAX_MONEY = Decimal("10000000")      # AED 10 million per unit or per cost line
MAX_WEIGHT_KG = Decimal("5000")      # one unit (a full lab bench is < 1 t)
MAX_ASSET_QTY = 10_000               # units in one batch
MAX_REQUEST_QTY = 1_000              # units in one request
MAX_VERSION = 2_147_483_647
MAX_SHORT = 300                      # reasons, notes, evidence references
MAX_LONG = 4_000                     # descriptions, purposes, narratives
MAX_NARRATIVE = 20_000

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MARKUP = re.compile(r"<\s*/?\s*[a-zA-Z!?]|javascript\s*:|\bon[a-z]+\s*=", re.I)
_LETTER = re.compile(r"[^\W\d_]", re.U)
PERSON_NAME = re.compile(r"^[^\W\d_]+(?:[ '\-.][^\W\d_]+)*\.?$", re.U)
PLACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'()/#&\-]*$")
CODE = re.compile(r"^[A-Z]{2,12}$")
CAMPUS_ID = re.compile(r"^1000\d{5}$")  # university ID: 9 digits starting 1000, e.g. 100060412
CAMPUS_ID_HELP = "Use the 9-digit university ID, e.g. 100060412."
SERIAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/.]{2,59}$")
DIMENSIONS = re.compile(r"^\d{1,4}(?:\.\d{1,2})?(?:\s*[x×]\s*\d{1,4}(?:\.\d{1,2})?){1,2}\s*(?:mm|cm|m|in)$", re.I)
MATERIAL = re.compile(r"^[A-Za-z][A-Za-z ,/&\-]{1,79}$")
KEYWORD = re.compile(r"^[^\W_][\w .+#/\-]{0,29}$", re.U)

# Canonical port names; the key is how a user may type it (case-insensitive).
PORTS = {
    "hdmi": "HDMI", "displayport": "DisplayPort", "dp": "DisplayPort", "mini displayport": "Mini DisplayPort",
    "mini dp": "Mini DisplayPort", "vga": "VGA", "dvi": "DVI", "usb": "USB-A", "usb-a": "USB-A", "usb a": "USB-A",
    "usb-b": "USB-B", "usb b": "USB-B", "usb-c": "USB-C", "usb c": "USB-C", "type-c": "USB-C", "micro usb": "Micro USB",
    "micro-usb": "Micro USB", "thunderbolt": "Thunderbolt", "thunderbolt 3": "Thunderbolt", "thunderbolt 4": "Thunderbolt",
    "ethernet": "Ethernet", "rj45": "Ethernet", "rj-45": "Ethernet", "audio": "Audio jack", "audio jack": "Audio jack",
    "3.5mm": "Audio jack", "3.5 mm": "Audio jack", "headphone": "Audio jack", "microphone": "Microphone",
    "sd": "SD card", "sd card": "SD card", "microsd": "microSD", "power": "Power", "dc power": "Power",
    "serial": "Serial (RS-232)", "rs-232": "Serial (RS-232)", "rs232": "Serial (RS-232)", "sata": "SATA",
    "pcie": "PCIe", "xlr": "XLR", "optical": "Optical audio", "s/pdif": "Optical audio", "bluetooth": "Bluetooth",
    "wi-fi": "Wi-Fi", "wifi": "Wi-Fi", "kensington": "Kensington lock",
}
PORT_HINT = "HDMI, DisplayPort, VGA, DVI, USB-A, USB-C, Thunderbolt, Ethernet, Audio jack, SD card, Power"


def _err(field, message):
    return BusinessError(BusinessError.VALIDATION_ERROR, message, {field: message} if field else None)


# ------------------------------------------------------------------- text ---
def text(value, field="text", *, label=None, required=False, min_len=0, max_len=MAX_SHORT,
         needs_letter=True, multiline=False, pattern=None, pattern_msg=None):
    """Strip and check free text. Rejects control characters and markup; collapses runs of spaces."""
    label = label or field.replace("_", " ").capitalize()
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    if _CONTROL.search(value):
        raise _err(field, f"{label} contains invisible control characters.")
    value = value.replace("\r\n", "\n").strip()
    if not multiline:
        value = re.sub(r"\s+", " ", value)
    else:
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
    if not value:
        if required:
            raise _err(field, f"{label} is required.")
        return ""
    if len(value) < min_len:
        raise _err(field, f"{label} must be at least {min_len} characters.")
    if len(value) > max_len:
        raise _err(field, f"{label} must be at most {max_len} characters (you entered {len(value)}).")
    if _MARKUP.search(value):
        raise _err(field, f"{label} cannot contain HTML or script markup.")
    if needs_letter and not _LETTER.search(value):
        raise _err(field, f"{label} must contain words, not only numbers or symbols.")
    if pattern is not None and not pattern.match(value):
        raise _err(field, pattern_msg or f"{label} has an invalid format.")
    return value


def reason(value, field="reason", *, required=True, label="Reason", min_len=5, max_len=MAX_SHORT):
    return text(value, field, label=label, required=required, min_len=min_len if required else 0, max_len=max_len, multiline=True)


def person_name(value, field="full_name"):
    v = text(value, field, label="Full name", required=True, min_len=2, max_len=120)
    if not PERSON_NAME.match(v):
        raise _err(field, "Full name may contain only letters, spaces, apostrophes, hyphens and dots.")
    return v


def campus_id(value, field="campus_id"):
    """University ID: exactly 9 digits beginning with 1000 (spaces are removed)."""
    v = re.sub(r"\s+", "", str(value or ""))
    if not v:
        raise _err(field, "Campus ID is required.")
    if not v.isdigit():
        raise _err(field, "Campus ID must contain digits only. " + CAMPUS_ID_HELP)
    if len(v) != 9:
        raise _err(field, f"Campus ID must be exactly 9 digits (you entered {len(v)}). " + CAMPUS_ID_HELP)
    if not CAMPUS_ID.match(v):
        raise _err(field, "Campus ID must start with 1000. " + CAMPUS_ID_HELP)
    return v


def place(value, field, *, label=None, required=True, max_len=80):
    label = label or field.replace("_", " ").capitalize()
    v = text(value, field, label=label, required=required, max_len=max_len, needs_letter=False)
    if v and not PLACE.match(v):
        raise _err(field, f"{label} may contain letters, numbers, spaces and . , ' ( ) / # & - only.")
    if v and not re.search(r"[A-Za-z0-9]", v):
        raise _err(field, f"{label} must contain letters or numbers.")
    return v


def keywords(value, field="tags", *, label="Tags", max_items=20):
    """Comma-separated keywords → clean, de-duplicated list of 1–30 character tokens."""
    raw = text(value, field, label=label, max_len=MAX_SHORT, needs_letter=False)
    out, seen = [], set()
    for tok in (t.strip() for t in raw.split(",")):
        if not tok:
            continue
        if not KEYWORD.match(tok):
            raise _err(field, f"“{tok[:30]}” is not a valid keyword. Use 1–30 letters, numbers, spaces, . + # / -")
        if not _LETTER.search(tok) and not tok.isdigit():
            raise _err(field, f"“{tok[:30]}” must contain letters.")
        if tok.lower() not in seen:
            seen.add(tok.lower())
            out.append(tok)
    if len(out) > max_items:
        raise _err(field, f"At most {max_items} {label.lower()}.")
    return out


# ------------------------------------------------------- type metadata ---
def ports(value, field="ports"):
    """Port list → canonical names. ``hello there`` is rejected."""
    v = text(value, field, label="Ports", max_len=120, needs_letter=False)
    if not v:
        return ""
    out = []
    for tok in re.split(r"[;,]", v):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"^(\d{1,2})\s*[x×]\s*(.+)$", tok, re.I)        # "2x USB-C"
        count, name = (int(m.group(1)), m.group(2).strip()) if m else (1, tok)
        canon = PORTS.get(name.lower())
        if canon is None:
            raise _err(field, f"Unknown port “{tok[:40]}”. Use a comma-separated list such as: {PORT_HINT}.")
        if not 1 <= count <= 16:
            raise _err(field, "Port counts must be between 1 and 16.")
        label = f"{count}× {canon}" if count > 1 else canon
        if label not in out:
            out.append(label)
    return ", ".join(out)


def serial_number(value, field="serial_number"):
    v = text(value, field, label="Serial number", max_len=60, needs_letter=False)
    if v and not SERIAL.match(v):
        raise _err(field, "Serial number: 3–60 letters, numbers, dashes, slashes or dots, no spaces (e.g. SN-48213-B).")
    if v and not re.search(r"\d", v):
        raise _err(field, "A serial number must contain at least one digit.")
    return v.upper() if v else v


def dimensions(value, field="dimensions"):
    v = text(value, field, label="Dimensions", max_len=60, needs_letter=False)
    if v and not DIMENSIONS.match(v):
        raise _err(field, "Dimensions: two or three numbers with a unit, e.g. 140 x 70 cm or 120 x 60 x 75 cm.")
    if v and any(Decimal(n) == 0 for n in re.findall(r"\d+(?:\.\d+)?", v)):
        raise _err(field, "Dimensions must be greater than zero.")
    return v


def material(value, field="material"):
    v = text(value, field, label="Material", max_len=80)
    if v and not MATERIAL.match(v):
        raise _err(field, "Material: words only, e.g. Oak, Steel, Laminated wood.")
    return v


# -------------------------------------------------------------- numbers ---
def integer(value, field="quantity", *, label=None, required=True, min_value=1, max_value=MAX_ASSET_QTY):
    label = label or field.replace("_", " ").capitalize()
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _err(field, f"{label} is required.")
        return None
    if isinstance(value, bool):
        raise _err(field, f"{label} must be a whole number.")
    if isinstance(value, int):
        n = value
    else:
        s = str(value).strip()
        if not re.fullmatch(r"[+-]?\d{1,12}", s):
            raise _err(field, f"{label} must be a whole number.")
        n = int(s)
    if min_value is not None and n < min_value:
        raise _err(field, f"{label} must be at least {min_value}.")
    if max_value is not None and n > max_value:
        raise _err(field, f"{label} must be at most {max_value:,}.")
    return n


def version(value, field="version"):
    """Optimistic-lock version from a hidden field; a tampered value is a stale form, not a crash."""
    try:
        return integer(value, field, label="Form version", min_value=0, max_value=MAX_VERSION)
    except BusinessError:
        raise BusinessError(BusinessError.STALE_VERSION, "This form is out of date or was altered. Reload the page and try again.", retryable=True)


def decimal(value, field, *, label=None, required=False, min_value=Decimal("0"), max_value=MAX_MONEY, places=2,
            allow_zero=True):
    label = label or field.replace("_", " ").capitalize()
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _err(field, f"{label} is required.")
        return None
    if isinstance(value, bool):
        raise _err(field, f"{label} must be a number.")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise _err(field, f"{label} must be a number, e.g. 250 or 250.50.")
    if not d.is_finite():
        raise _err(field, f"{label} must be a real number.")
    if d != d.quantize(Decimal(1).scaleb(-places)) if abs(d) < MAX_MONEY * 1000 else False:
        raise _err(field, f"{label} may have at most {places} decimal places.")
    if min_value is not None and d < min_value:
        raise _err(field, f"{label} cannot be less than {min_value}.")
    if not allow_zero and d == 0:
        raise _err(field, f"{label} must be greater than zero.")
    if max_value is not None and d > max_value:
        raise _err(field, f"{label} cannot exceed {max_value:,}.")
    return d.quantize(Decimal(1).scaleb(-places))


def money(value, field, *, label=None, required=False, allow_zero=True):
    return decimal(value, field, label=label, required=required, allow_zero=allow_zero)


# ---------------------------------------------------------------- dates ---
def a_date(value, field, *, label=None, required=False, not_before=None, not_after=None):
    label = label or field.replace("_", " ").capitalize()
    if value in (None, ""):
        if required:
            raise _err(field, f"{label} is required.")
        return None
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value.strip())
        except ValueError:
            raise _err(field, f"{label} must be a valid date (YYYY-MM-DD).")
    if not_before is not None and value < not_before:
        raise _err(field, f"{label} cannot be before {not_before:%d %b %Y}.")
    if not_after is not None and value > not_after:
        raise _err(field, f"{label} cannot be after {not_after:%d %b %Y}.")
    return value


def years_from_today(n):
    t = date.today()
    try:
        return t.replace(year=t.year + n)
    except ValueError:
        return t + timedelta(days=365 * n)


# ----------------------------------------------------------- identifiers ---
def parse_uuid(value, field="id", *, required=True):
    if value in (None, ""):
        if required:
            raise _err(field, "Choose an item first.")
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        raise _err(field, "The selected item is not valid. Reload the page and choose again.")


def uuid_or_none(value):
    try:
        return parse_uuid(value, required=False)
    except BusinessError:
        return None


def uuid_or_404(value):
    u = uuid_or_none(value)
    if u is None:
        raise Http404("Not found")
    return u


def choice(value, field, choices, *, label=None, required=True):
    label = label or field.replace("_", " ").capitalize()
    allowed = {c[0] if isinstance(c, (tuple, list)) else c for c in choices}
    if value in (None, ""):
        if required:
            raise _err(field, f"Choose a {label.lower()}.")
        return ""
    if value not in allowed:
        raise _err(field, f"“{str(value)[:30]}” is not a valid {label.lower()}.")
    return value


def evidence_ref(value, field="evidence", *, required=True, label="Evidence reference"):
    """A document/photo reference such as ‘Photo IMG_2041’ or ‘Gate pass GP-2207’."""
    return text(value, field, label=label, required=required, min_len=3 if required or (value or "").strip() else 0,
                max_len=MAX_SHORT, needs_letter=True, multiline=False)


# ------------------------------------------------- Django form adapters ---
def form_validator(fn, **kwargs):
    """Wrap a helper above as a Django field validator (keeps one source of truth)."""

    def _v(value):
        try:
            fn(value, **kwargs)
        except BusinessError as exc:
            raise ValidationError(exc.message)

    return _v


class CleanTextMixin:
    """Mixin for forms: every CharField is run through :func:`text` (control chars, markup, length)."""

    multiline_fields: tuple = ()
    letter_optional: tuple = ()

    def _post_clean_text(self):
        for name, field in self.fields.items():
            if isinstance(field, forms.CharField) and not isinstance(field.widget, (forms.PasswordInput, forms.HiddenInput)) \
                    and name in self.cleaned_data and isinstance(self.cleaned_data[name], str):
                try:
                    self.cleaned_data[name] = text(self.cleaned_data[name], name, label=str(field.label or name.replace("_", " ").capitalize()),
                                                   max_len=field.max_length or MAX_LONG, multiline=name in self.multiline_fields,
                                                   needs_letter=name not in self.letter_optional)
                except BusinessError as exc:
                    self.add_error(name, exc.message)
                    self.cleaned_data.pop(name, None)

    def clean(self):
        data = super().clean()
        self._post_clean_text()
        return self.cleaned_data
