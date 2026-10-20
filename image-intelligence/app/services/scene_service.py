"""Scene description from visible evidence only (objects, text, colours).

Every conclusion lists its evidence. No personal attributes are inferred.
"""

from __future__ import annotations

import cv2
import numpy as np

INDOOR = {"chair", "couch", "bed", "table", "monitor/tv", "laptop", "keyboard", "mouse", "refrigerator", "oven",
          "microwave", "sink", "toilet", "book", "clock", "vase", "potted plant", "remote", "toaster"}
OFFICE = {"laptop", "keyboard", "mouse", "monitor/tv", "book", "chair", "mobile phone", "table"}
VEHICLE = {"car", "bus", "truck", "motorcycle", "bicycle", "train", "boat", "airplane"}
STREET = {"traffic light", "stop sign", "fire hydrant", "parking meter", "bench"}
ANIMALS = {"bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}


def _colour_stats(bgr: np.ndarray) -> dict:
    small = cv2.resize(bgr, (160, max(1, int(160 * bgr.shape[0] / bgr.shape[1]))))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    top = hsv[: hsv.shape[0] // 3]
    sky = ((top[..., 0] >= 90) & (top[..., 0] <= 130) & (top[..., 1] > 40) & (top[..., 2] > 120)).mean()
    green = ((h >= 35) & (h <= 85) & (s > 50) & (v > 40)).mean()
    # Flat backgrounds (screenshots / cards / documents): few distinct colours.
    q = (small // 32).reshape(-1, 3)
    _, counts = np.unique(q, axis=0, return_counts=True)
    top3 = np.sort(counts)[::-1][:3].sum() / q.shape[0]
    return {"sky_top": float(sky), "green": float(green), "flat": float(top3)}


def analyze(bgr: np.ndarray, labels: list[str], text_lines: list[dict], has_contact: bool,
            person_count: int, objects_available: bool) -> dict:
    labels_set = set(labels)
    h, w = bgr.shape[:2]
    stats = _colour_stats(bgr)
    text_area = sum(l["bbox"]["width"] * l["bbox"]["height"] for l in text_lines if l.get("bbox")) / float(w * h)
    categories, evidence = [], []

    doc_like = (len(text_lines) >= 6 and text_area > 0.06) or (stats["flat"] > 0.6 and len(text_lines) >= 3)
    if doc_like:
        kind = "profile-card-like" if has_contact else "document/screenshot-like"
        categories.append(kind)
        evidence.append(f"{len(text_lines)} text lines covering {text_area:.0%} of the image"
                        + (", contact details visible" if has_contact else ""))
    if labels_set & VEHICLE:
        categories.append("vehicle")
        evidence.append("vehicles: " + ", ".join(sorted(labels_set & VEHICLE)))
    if labels_set & STREET or (labels_set & {"car", "bus", "truck"} and stats["sky_top"] > 0.15):
        categories.append("street")
        evidence.append("street objects / vehicles with sky")
    office_hits = labels_set & OFFICE
    if len(office_hits) >= 2 or {"laptop", "keyboard"} & labels_set:
        categories.append("office-like")
        evidence.append("office items: " + ", ".join(sorted(office_hits)))
    elif labels_set & {"bed", "couch", "refrigerator", "oven", "sink", "toilet", "tv", "monitor/tv"}:
        categories.append("room")
        evidence.append("room furniture/appliances: " + ", ".join(sorted(labels_set & INDOOR)))
    if (stats["green"] > 0.25 or labels_set & ANIMALS) and not (labels_set & INDOOR):
        categories.append("nature")
        evidence.append(f"{stats['green']:.0%} vegetation colour" + (", animals" if labels_set & ANIMALS else ""))

    indoor_votes = len(labels_set & INDOOR)
    # On cards / screenshots a blue or green background is design, not sky or grass.
    colour_votes = 0 if doc_like else (stats["sky_top"] > 0.2) * 2 + (stats["green"] > 0.3)
    outdoor_votes = colour_votes + len(labels_set & (VEHICLE | STREET))
    if doc_like and not indoor_votes and not outdoor_votes:
        setting = "unknown"
    elif indoor_votes > outdoor_votes:
        setting = "indoor"
        evidence.append("indoor objects: " + ", ".join(sorted(labels_set & INDOOR)))
    elif outdoor_votes > indoor_votes:
        setting = "outdoor"
        if colour_votes and stats["sky_top"] > 0.2:
            evidence.append(f"sky in the upper third ({stats['sky_top']:.0%})")
    else:
        setting = "unknown"

    if not objects_available:
        evidence.append("object detector unavailable: scene based on colours/text only")

    parts = []
    if "office-like" in categories:
        parts.append("office-like")
    elif "room" in categories:
        parts.append("room")
    if setting != "unknown":
        parts.append(setting)
    for c in ("street", "vehicle", "nature", "profile-card-like", "document/screenshot-like"):
        if c in categories:
            parts.append(c)
    desc = " ".join(dict.fromkeys(parts)) or "unclassified scene"
    if person_count:
        desc += f" with {person_count} person{'s' if person_count > 1 else ''}"
    return {"setting": setting, "categories": categories, "description": desc, "evidence": evidence}
