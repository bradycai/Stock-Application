"""Central Claude client. Every Claude request in the app goes through `call_structured`.

What it guarantees for each call:
- Model and effort come from the caller's ROLE (strategist / position / research /
  watcher), configured in settings — never hard-coded at the call site.
- Bounded latency and retries: a per-attempt timeout and a bounded SDK retry count.
- Structured output: the response is constrained to the JSON schema of a Pydantic model
  (`output_config.format`), then validated again with Pydantic, bounds included.
- Fail closed: a refusal (`stop_reason == "refusal"`), a truncated response
  (`max_tokens`), any other incomplete stop, an API error, or output that fails
  validation raises LLMError. Callers never receive partial or unvalidated output.
- Accounting: tokens, estimated cost, elapsed time, prompt version, served model and
  fallback use are logged and written to the `llm_calls` ledger.
- Refusal fallbacks: on models that support server-side fallback, a safety refusal is
  re-run server-side on Anthropic's recommended fallback model (`fallbacks: "default"`).

Claude gets no tools here and never sees credentials.
"""
import hashlib
import logging
import time
from dataclasses import dataclass, field

import anthropic
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.logging_config import log_event, run_id_var

logger = logging.getLogger("trading.llm")

EFFORTS = ("low", "medium", "high", "xhigh", "max")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept server-side refusal fallbacks. Claude Haiku 5.5 has none: sending the
# parameter there either does nothing or is rejected, so it is never sent for Haiku.
FALLBACK_MODELS = {
    "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1",
}

# List prices, USD per million tokens: (input, output, cache-read multiplier).
# 5-minute cache writes cost 1.25x input. Estimates only — check the pricing page.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.05),
    "claude-opus-5": (5.00, 25.00, 0.10),
    "claude-opus-4-8": (5.00, 25.00, 0.10),
    "claude-sonnet-5-5": (2.00, 10.00, 0.05),
    "claude-sonnet-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.10),
}
HAIKU_55_LONG_PROMPT = (100_000, 0.50, 2.50)  # prompts over 100K tokens use a higher rate card
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True)
class Role:
    name: str
    model: str
    effort: str
    max_tokens: int


def get_role(name: str) -> Role:
    s = settings
    roles = {
        "strategist": Role("strategist", s.strategist_model, s.strategist_effort, s.strategist_max_tokens),
        "position": Role("position", s.position_model or s.strategist_model, s.position_effort,
                         s.position_max_tokens),
        "research": Role("research", s.research_model, s.research_effort, s.research_max_tokens),
        "watcher": Role("watcher", s.watcher_model, s.watcher_effort, s.watcher_max_tokens),
    }
    if name not in roles:
        raise ValueError(f"unknown model role {name!r}")
    role = roles[name]
    if role.effort not in EFFORTS:
        raise ValueError(f"{name} effort {role.effort!r} must be one of {EFFORTS}")
    return role


def prompt_version(prompt_name: str, system_prompt: str) -> str:
    """Stable version id: the prompt's name plus a hash of its exact text."""
    return f"{prompt_name}@{hashlib.sha256(system_prompt.encode()).hexdigest()[:10]}"


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    """Estimated USD cost from list prices; None for an unknown model."""
    if not model or model not in PRICES:
        return None
    inp, out, read_mult = PRICES[model]
    prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
    if model == "claude-haiku-5-5" and prompt_tokens > HAIKU_55_LONG_PROMPT[0]:
        inp, out = HAIKU_55_LONG_PROMPT[1], HAIKU_55_LONG_PROMPT[2]
    cost = (input_tokens * inp
            + cache_write_tokens * inp * CACHE_WRITE_MULTIPLIER
            + cache_read_tokens * inp * read_mult
            + output_tokens * out) / 1_000_000
    return round(cost, 6)


class LLMError(Exception):
    """A Claude call that produced no usable, validated output. `kind` is one of
    refusal | incomplete | invalid_output | api_error."""

    def __init__(self, kind: str, detail: str, meta: dict):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.meta = meta


@dataclass
class LLMResult:
    output: BaseModel
    meta: dict = field(default_factory=dict)


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            # None lets the SDK resolve credentials from the environment instead.
            api_key=settings.anthropic_api_key or None,
            timeout=settings.llm_timeout_sec,
            max_retries=settings.llm_max_retries,
        )
    return _client


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    iterations = getattr(u, "iterations", None) or []
    return {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        "fallback_used": any(getattr(it, "type", None) == "fallback_message" for it in iterations),
    }


def _record_call(meta: dict) -> None:
    """Append the call to the llm_calls ledger in its own transaction. Never raises: the
    ledger must not break analysis (the structured log line is the fallback record)."""
    try:
        from app.database import SessionLocal
        from app.models import LLMCall

        with SessionLocal() as db:
            db.add(LLMCall(**{k: meta.get(k) for k in (
                "run_id", "role", "prompt_name", "prompt_version", "requested_model", "served_model",
                "effort", "status", "stop_reason", "refusal_category", "fallback_used",
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "cost_usd", "elapsed_ms", "request_id", "error")}))
            db.commit()
    except Exception as e:  # noqa: BLE001
        log_event(logger, "llm.ledger_write_failed", logging.WARNING, error=str(e)[:300])


def _finish(meta: dict, status: str, error: str | None = None) -> dict:
    meta["status"] = status
    meta["error"] = error
    level = logging.INFO if status == "ok" else logging.WARNING
    log_event(logger, "llm.call", level, **{k: v for k, v in meta.items() if k != "error"},
              **({"error": error[:500]} if error else {}))
    _record_call(meta)
    return meta


def call_structured(*, role: str, prompt_name: str, system: str, user: str,
                    output_model: type[BaseModel], max_tokens: int | None = None) -> LLMResult:
    """Make one structured Claude request and return validated output, or raise LLMError."""
    r = get_role(role)
    meta: dict = {
        "run_id": run_id_var.get(),
        "role": r.name,
        "prompt_name": prompt_name,
        "prompt_version": prompt_version(prompt_name, system),
        "requested_model": r.model,
        "served_model": None,
        "effort": r.effort,
        "stop_reason": None,
        "refusal_category": None,
        "fallback_used": False,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "cost_usd": None,
        "elapsed_ms": None,
        "request_id": None,
    }
    request = {
        "model": r.model,
        "max_tokens": max_tokens or r.max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": r.effort,
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(output_model)},
        },
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    if settings.llm_refusal_fallbacks and r.model in FALLBACK_MODELS:
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"

    started = time.monotonic()
    try:
        resp = _get_client().beta.messages.create(**request)
    except anthropic.APIStatusError as e:
        meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        meta["request_id"] = getattr(e, "request_id", None)
        raise LLMError("api_error", f"HTTP {e.status_code}: {e.message}",
                       _finish(meta, "api_error", f"HTTP {e.status_code}: {e.message}"))
    except anthropic.APIError as e:  # connection errors and timeouts, after SDK retries
        meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        raise LLMError("api_error", str(e), _finish(meta, "api_error", f"{type(e).__name__}: {e}"))

    meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    meta["request_id"] = getattr(resp, "_request_id", None)
    meta["served_model"] = getattr(resp, "model", None) or r.model
    meta["stop_reason"] = resp.stop_reason
    meta.update(_usage(resp))
    meta["cost_usd"] = estimate_cost(meta["served_model"], meta["input_tokens"], meta["output_tokens"],
                                     meta["cache_read_tokens"], meta["cache_write_tokens"])

    # Check why the model stopped BEFORE reading any content.
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        meta["refusal_category"] = getattr(details, "category", None)
        raise LLMError("refusal", f"declined (category={meta['refusal_category']})",
                       _finish(meta, "refusal", "model declined the request"))
    if resp.stop_reason != "end_turn":
        detail = f"stop_reason={resp.stop_reason}"
        raise LLMError("incomplete", detail, _finish(meta, "incomplete", detail))

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    try:
        output = output_model.model_validate_json(text)
    except ValidationError as e:
        detail = f"{e.error_count()} validation error(s): {e.errors()[:3]}"
        raise LLMError("invalid_output", detail, _finish(meta, "invalid_output", detail))

    return LLMResult(output=output, meta=_finish(meta, "ok"))
