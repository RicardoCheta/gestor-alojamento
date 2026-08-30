"""
Turn a pasted Airbnb listing / house-manual blob into structured property fields.

Uses Claude when ANTHROPIC_API_KEY is set; otherwise falls back to a small set of
regex heuristics so the feature still does something useful offline.
"""

from __future__ import annotations

import json
import os
import re

import db

LLM_MODEL = "claude-sonnet-5"

_SYSTEM = """Extract short-stay rental info from the text into JSON with exactly
these keys (empty string if not present):
name, address, wifi_network, wifi_password, wifi_notes, ac_instructions,
parking, checkin, checkout, trash, house_rules, emergency_contact.
Values are plain guest-facing text. Do not invent anything. Reply with only the
minified JSON object."""

_FIELDS = db.PROPERTY_FIELDS


def _empty() -> dict:
    return {f: "" for f in _FIELDS}


def _clean(s: str) -> str:
    return s.strip().strip("\"'").rstrip(".,;:")


def _heuristic(text: str) -> dict:
    out = _empty()
    t = " ".join(text.split())

    # WiFi: require an explicit "network"/"ssid" or "password" label with a separator.
    m = re.search(r"(?:wi[- ]?fi\s+)?(?:network\s*name|network|ssid)\s*[:=]\s*([^\s,;|]{2,40})", t, re.I)
    if m:
        out["wifi_network"] = _clean(m.group(1))
    m = re.search(r"(?:wi[- ]?fi\s+)?(?:password|pass|pwd|senha|palavra[- ]?passe)\s*[:=]\s*([^\s,;|]{4,40})", t, re.I)
    if m:
        out["wifi_password"] = _clean(m.group(1))

    m = re.search(r"check[\s\-]?in[^.\n]{0,40}?(\d{1,2}(?::\d{2})?\s*(?:am|pm|h)?)", t, re.I)
    if m:
        out["checkin"] = f"From {_clean(m.group(1))}."
    m = re.search(r"check[\s\-]?out[^.\n]{0,40}?(\d{1,2}(?::\d{2})?\s*(?:am|pm|h)?)", t, re.I)
    if m:
        out["checkout"] = f"By {_clean(m.group(1))}."

    rules = re.findall(r"\bno (?:parties|smoking|pets|shoes)\b", t, re.I)
    if rules:
        out["house_rules"] = ". ".join(dict.fromkeys(r.capitalize() for r in rules)) + "."

    m = re.search(r"(park(?:ing)?[^.\n]{5,180}\.)", t, re.I)
    if m:
        out["parking"] = _clean(m.group(1)) + "."

    return out


def extract_fields(text: str) -> tuple[dict, str]:
    """Return (fields dict, source) where source is 'ai' or 'heuristic'."""
    text = (text or "").strip()
    if not text:
        return _empty(), "heuristic"

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic()
            resp = client.messages.create(
                model=LLM_MODEL, max_tokens=700, system=_SYSTEM,
                messages=[{"role": "user", "content": text[:6000]}],
            )
            raw = re.sub(r"^```(?:json)?|```$", "", resp.content[0].text.strip()).strip()
            data = json.loads(raw)
            return {f: str(data.get(f, "")).strip() for f in _FIELDS}, "ai"
        except Exception as exc:  # noqa: BLE001
            print(f"[listing_import] AI extraction failed, using heuristics: {exc}", flush=True)

    return _heuristic(text), "heuristic"
