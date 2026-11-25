"""Online evaluators: a cheap→expensive cascade.

    100% of sampled spans:   format validity · PII · refusal       (regex / parsing, ~free)
    sampled + has context:   groundedness via LLM judge             (one cheap judge call)

Each evaluator returns zero or more ``Score`` rows written to ClickHouse ``scores`` with
``source='eval'``. Low groundedness pushes the trace into an annotation queue for humans,
whose labels in turn calibrate the judge (``calibration.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from lumen.common.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Score:
    name: str
    value: float
    data_type: str = "numeric"  # numeric | boolean | categorical
    string_value: str = ""
    comment: str = ""


# ----------------------------------------------------------------------------- cheap evaluators
_PII = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "us_ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}
_REFUSAL = re.compile(r"\b(I can(?:'|no)t help with|I(?:'m| am) (?:not able|unable) to|I won't|as an AI|"
                      r"I don't have enough information)\b", re.I)


def _luhn(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def pii_types(text: str) -> list[str]:
    found = []
    for name, pat in _PII.items():
        for m in pat.finditer(text):
            if name == "credit_card" and not _luhn(m.group()):
                continue  # Luhn check removes most false positives (order ids, timestamps)
            found.append(name)
            break
    return found


def evaluate_cheap(output: str, finish_reason: str, expects_json: bool = False) -> list[Score]:
    scores: list[Score] = []
    if output:
        types = pii_types(output)
        scores.append(Score("pii_leak", float(bool(types)), "boolean", ",".join(types)))
        scores.append(Score("refusal", float(bool(_REFUSAL.search(output)) or finish_reason == "content_filter"),
                            "boolean"))
        if expects_json:
            try:
                json.loads(output)
                ok = 1.0
            except ValueError:
                ok = 0.0
            scores.append(Score("json_valid", ok, "boolean"))
    if finish_reason == "length":
        scores.append(Score("truncated", 1.0, "boolean", comment="hit max_tokens"))
    return scores


# ----------------------------------------------------------------------------- LLM judge
JUDGE_SYSTEM = """You grade whether an AI answer is grounded in the provided context.
Score 1.0 if every factual claim in the answer is supported by the context, 0.0 if the main claims are
unsupported or contradicted, and in between proportionally. Abstentions ("not enough information") are
grounded. Reply as JSON: {"score": <0..1>, "unsupported_claims": [<strings>], "reason": "<one sentence>"}"""


class GroundednessJudge:
    """Calls an OpenAI-compatible endpoint (by default Atlas's gateway, route ``judge``: a pinned
    model, so scores are comparable over time)."""

    def __init__(self, base_url: str, api_key: str, route: str = "judge", timeout_s: float = 60.0) -> None:
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.route = route
        self._http = httpx.AsyncClient(timeout=timeout_s, headers={"Authorization": f"Bearer {api_key}"})

    async def grade(self, context: str, answer: str) -> Score | None:
        body: dict[str, Any] = {
            "model": self.route, "max_tokens": 400,
            "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                         {"role": "user", "content": f"<context>\n{context[:12000]}\n</context>\n"
                                                     f"<answer>\n{answer[:4000]}\n</answer>"}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "Grade", "strict": True, "schema": {
                "type": "object", "additionalProperties": False, "required": ["score", "unsupported_claims", "reason"],
                "properties": {"score": {"type": "number"}, "unsupported_claims": {"type": "array",
                                                                                    "items": {"type": "string"}},
                               "reason": {"type": "string"}}}}}}
        try:
            r = await self._http.post(self.url, json=body)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content[content.find("{"): content.rfind("}") + 1])
            score = min(1.0, max(0.0, float(data["score"])))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("judge_failed", error=str(exc)[:200])
            return None
        return Score("groundedness", score, comment=str(data.get("reason", ""))[:500])

    async def aclose(self) -> None:
        await self._http.aclose()


def split_context(input_preview: str) -> tuple[str, str]:
    """Extract retrieved context from a captured RAG prompt (Atlas wraps it in <sources>)."""
    m = re.search(r"<sources>(.*?)</sources>", input_preview, re.S)
    return (m.group(1).strip(), input_preview) if m else ("", input_preview)
