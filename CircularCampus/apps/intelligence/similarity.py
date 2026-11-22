"""Local concept-similarity (fallback for semantic suitability S).

This is NOT a language model. It normalises words, maps synonyms to shared
concepts (so "screen" and "display" both match "monitor"), weights terms by
inverse document frequency and computes a cosine similarity. It is used only
when Gemini embeddings are unavailable, and results are labelled as such.
"""
import math
import re
from collections import Counter

CONCEPTS = {
    "monitor": ["monitor", "screen", "display", "lcd", "led", "panel", "visual"],
    "computer": ["computer", "pc", "desktop", "workstation", "tower", "cpu"],
    "laptop": ["laptop", "notebook", "ultrabook", "portable"],
    "chair": ["chair", "seat", "seating", "stool", "armchair"],
    "desk": ["desk", "table", "bench", "worktop"],
    "storage": ["cabinet", "cupboard", "locker", "shelf", "shelving", "drawer", "bookcase", "storage"],
    "projector": ["projector", "beamer", "projection"],
    "printer": ["printer", "printing", "copier", "scanner", "multifunction"],
    "whiteboard": ["whiteboard", "board", "dry", "erase", "flipchart"],
    "microscope": ["microscope", "magnifier", "optical", "lens"],
    "lab": ["lab", "laboratory", "experiment", "bench", "practical"],
    "teaching": ["teaching", "class", "classroom", "lecture", "course", "student", "students", "tutorial"],
    "office": ["office", "admin", "administrative", "staff"],
    "hdmi": ["hdmi", "displayport", "vga", "video"],
    "network": ["network", "router", "switch", "wifi", "ethernet"],
    "audio": ["speaker", "speakers", "audio", "microphone", "headset", "sound"],
    "camera": ["camera", "webcam", "video-conference", "conference"],
    "book": ["book", "books", "textbook", "manual", "journal"],
    "ergonomic": ["ergonomic", "adjustable", "lumbar", "height"],
    "keyboard": ["keyboard", "mouse", "peripheral", "peripherals"],
    "oscilloscope": ["oscilloscope", "scope", "signal", "multimeter", "measurement"],
    "power": ["ups", "battery", "charger", "power"],
}
WORD_TO_CONCEPT = {w: c for c, words in CONCEPTS.items() for w in words}
STOP = set("a an the and or for of to in on with need needs needed we our my i is are be by at as from this that it into per each any some new used".split())


def tokens(text):
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", (text or "").lower())
    out = []
    for w in words:
        if w in STOP or len(w) < 2:
            continue
        if w.endswith("s") and len(w) > 3 and w[:-1] in WORD_TO_CONCEPT:
            w = w[:-1]
        out.append("c:" + WORD_TO_CONCEPT[w] if w in WORD_TO_CONCEPT else w)
    return out


def similarity_matrix(query, documents):
    """Cosine similarity of TF-IDF concept vectors; returns a list aligned with documents."""
    docs = [Counter(tokens(d)) for d in documents]
    q = Counter(tokens(query))
    n = len(docs) + 1
    df = Counter()
    for d in docs + [q]:
        df.update(set(d))
    idf = {t: math.log((1 + n) / (1 + df[t])) + 1 for t in df}

    def vec(c):
        return {t: (1 + math.log(f)) * idf[t] for t, f in c.items()}

    qv = vec(q)
    qn = math.sqrt(sum(v * v for v in qv.values())) or 1.0
    q_concepts = {t for t in q if t.startswith("c:")}
    result = []
    for d in docs:
        dv = vec(d)
        dn = math.sqrt(sum(v * v for v in dv.values())) or 1.0
        dot = sum(qv[t] * dv.get(t, 0.0) for t in qv)
        cos = max(0.0, min(1.0, dot / (qn * dn)))
        if q_concepts:
            # Share of the request's concepts (synonym groups) that the asset mentions.
            coverage = len(q_concepts & set(d)) / len(q_concepts)
            cos = 0.5 * cos + 0.5 * coverage
        result.append(round(max(0.0, min(1.0, cos)), 4))
    return result


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        raise ValueError("zero vector")
    return max(0.0, min(1.0, dot / (na * nb)))
