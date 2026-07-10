"""Deterministic prompt-injection signal tagging for scraped content."""

from __future__ import annotations

import re

_RULES = (
    (
        "ignore-prior-instructions",
        "high",
        re.compile(
            r"\b(ignore|discard|forget)\b.{0,80}\b(previous|prior|above)\b"
            r".{0,80}\binstructions?\b",
            re.I | re.S,
        ),
    ),
    (
        "system-prompt-exfiltration",
        "high",
        re.compile(
            r"\b(reveal|print|show|exfiltrate|leak)\b.{0,80}\b(system|developer)\b"
            r".{0,40}\b(prompt|message|instructions?)\b",
            re.I | re.S,
        ),
    ),
    (
        "secret-exfiltration",
        "high",
        re.compile(
            r"\b(api[_ -]?key|token|secret|password|credential)\b.{0,80}\b"
            r"(send|post|upload|exfiltrate|leak)\b",
            re.I | re.S,
        ),
    ),
    (
        "tool-call-instruction",
        "medium",
        re.compile(
            r"\b(call|invoke|use|execute)\b.{0,40}\b"
            r"(tool|function|command|shell|browser)\b",
            re.I | re.S,
        ),
    ),
    (
        "role-override",
        "medium",
        re.compile(
            r"\byou are now\b.{0,80}\b(system|developer|admin|root|unrestricted)\b",
            re.I | re.S,
        ),
    ),
)

_SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _snippet(text: str, start: int, end: int, *, radius: int = 80) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()[:240]


def audit_content(text: str, *, max_signals: int = 10) -> dict:
    """Return deterministic prompt-injection signals without blocking content."""
    if not text:
        return {"risk": "none", "signals": []}

    sample = text[:200_000]
    signals: list[dict[str, str]] = []
    highest = "none"
    for rule_id, severity, pattern in _RULES:
        for match in pattern.finditer(sample):
            signals.append({
                "rule": rule_id,
                "severity": severity,
                "snippet": _snippet(sample, match.start(), match.end()),
            })
            if _SEVERITY_RANK[severity] > _SEVERITY_RANK[highest]:
                highest = severity
            if len(signals) >= max_signals:
                return {"risk": highest, "signals": signals}

    return {"risk": highest, "signals": signals}
