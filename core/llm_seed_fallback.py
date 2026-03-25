from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from core.config import FuzzConfig
from core.db_utils import get_seed_generation_context
from core.fuzzer_logging import get_fuzzer_logger
from core.paths import FUZZER_ROOT
from parser import get_target_registry
from seed_corpus import Seed


DEFAULT_TARGET_DESCRIPTIONS: dict[str, str] = {
    "json-decoder": (
        "Parses JSON strings. Useful cases include valid structured JSON, nested "
        "objects/arrays, long strings, escapes, unicode, boundary numbers, and "
        "malformed near-valid delimiters."
    ),
    "IPv4-IPv6-parser": (
        "Parses IPv4 and IPv6 textual addresses. Useful cases include canonical "
        "addresses, compressed IPv6, whitespace/formatting stressors, prefix-like "
        "suffixes, and malformed separators or ranges."
    ),
    "cidrize-runner": (
        "Parses IP and CIDR-like strings. Useful cases include valid CIDR forms, "
        "edge prefix lengths, unusual spacing, malformed masks, and semantically "
        "plausible network strings close to valid syntax."
    ),
}


@dataclass
class LLMSeedFallbackResult:
    seeds: list[str]
    prompt_text: str
    raw_response_text: str


_DOTENV_LOADED = False


def _load_dotenv_if_present() -> None:
    """Load simple KEY=VALUE pairs from project-root `.env` into os.environ."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return

    env_path = FUZZER_ROOT / ".env"
    if not env_path.is_file():
        _DOTENV_LOADED = True
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (
            len(value) >= 2
            and ((value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")))
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)

    _DOTENV_LOADED = True


def _target_type(*, target: str, config: FuzzConfig) -> str:
    entry = get_target_registry(
        parser_config=config.get("parser_config")  # type: ignore[arg-type]
    ).get(target, {})
    if entry.get("oracle") is not None and target != "json-decoder":
        return "black_box_differential"
    if target == "json-decoder":
        return "coverage_guided_oracle"
    return "black_box"


def _input_format(target: str) -> str:
    if "json" in target.lower():
        return "JSON string"
    if "ipv4" in target.lower() or "ipv6" in target.lower():
        return "IPv4 / IPv6 textual address"
    if "cidr" in target.lower():
        return "CIDR / IP textual string"
    return "text"


def _build_prompt(
    *,
    target: str,
    context: dict[str, Any],
    config: FuzzConfig,
) -> str:
    seed_count = config["llm_seed_candidates"]
    payload = {
        "target_name": target,
        "target_type": _target_type(target=target, config=config),
        "input_format": _input_format(target),
        "target_description": DEFAULT_TARGET_DESCRIPTIONS.get(
            target,
            "Generate diverse textual parser inputs appropriate for this target.",
        ),
        "top_interesting_seeds": context["top_interesting_seeds"],
        "not_interesting_seeds": context["not_interesting_seeds"],
        "already_fuzzed_seeds": context["already_fuzzed_seeds"],
        "seed_count": seed_count,
    }

    return """You are helping a fuzzing system recover from seed exhaustion.

Your task is to generate new candidate seeds for the target `{target_name}`.

## Context
The fuzzer has exhausted its current schedulable seeds or failed to discover new interesting behavior for a while.

You are given:
- the target name
- the target input format
- a short description of what the target parses
- examples of seeds that were historically interesting
- examples of seeds that were historically not interesting
- examples of seeds already fuzzed

Your goal is to produce NEW candidate seeds that are:
- valid or near-valid for the target format
- diverse in structure
- meaningfully different from already fuzzed seeds
- biased toward patterns similar to interesting seeds
- but not simple copies of them

## Target
Target: `{target_name}`
Target type: `{target_type}`
Input format: `{input_format}`
Target description:
`{target_description}`

## Fuzzing History

### Top interesting seeds
These seeds previously led to new bugs, new coverage, or new differential behavior:

`{top_interesting_seeds}`

### Low-value / not-interesting seeds
These seeds were fuzzed but did not lead to interesting behavior:

`{not_interesting_seeds}`

### Already fuzzed seeds
Do not repeat or trivially rewrite these:

`{already_fuzzed_seeds}`

## Instructions
Follow these steps internally before producing the final answer:
1. Identify structural patterns that appear in the interesting seeds.
2. Identify repetitive or unproductive patterns from the not-interesting seeds.
3. Propose new inputs that preserve useful structure while varying boundary values, nesting, formatting, semantic edge cases, and malformed-near-valid cases.
4. Avoid exact duplicates and avoid trivial mutations of already fuzzed seeds.
5. Prefer diversity over quantity duplication.
6. Before answering, self-check that your entire response is valid JSON parseable by a strict JSON parser.
7. Before answering, self-check that every `candidate_seeds[i].seed` value is a literal concrete string, not code, not pseudocode, not a template, and not an expression.

## Seed generation goals
Generate seeds that include a mix of:
- valid structured inputs
- near-valid edge cases
- boundary-value cases
- malformed-but-plausible cases
- semantically unusual cases

## Balance requirements
- Include at least 2 `valid` seeds.
- Include at least 2 seeds split across `malformed` or `near_valid`.
- Include at least 1 `semantic_edge` seed.
- Do not let malformed seeds dominate the full set.
- Prefer a balanced portfolio over many variations of the same malformed pattern.

## Output requirements
Return ONLY valid JSON with this exact schema:

{{
  "target": "{target_name}",
  "generation_strategy": [
    "short bullet describing pattern 1",
    "short bullet describing pattern 2",
    "short bullet describing pattern 3"
  ],
  "candidate_seeds": [
    {{
      "seed": "string seed here",
      "category": "valid | near_valid | malformed | boundary | semantic_edge",
      "why": "one short sentence"
    }}
  ]
}}

## Hard constraints
- Output exactly `{seed_count}` candidate seeds.
- Do not include markdown fences.
- Do not include explanations outside the JSON.
- Do not output any seed that exactly matches an already fuzzed seed.
- Keep each seed as a single string.
- Ensure the returned set satisfies the balance requirements above.
- Every `seed` field must be a fully materialized literal string value.
- Do not use Python, JavaScript, or pseudocode expressions inside `seed` values.
- Do not use string concatenation, repetition, interpolation, builders, helper functions, or placeholders.
- Invalid examples of forbidden `seed` values:
  - `"{{\\"x\\":\\"" + "a" * 100 + "\\"}}"`
  - `"[" + ",".join(["null"] * 10) + "]"`
  - `"<repeat 100 times>"`
- Valid examples must already contain the final literal text exactly as it should be fuzzed.
- If you cannot produce valid JSON that passes these checks, output `{{"target":"{target_name}","generation_strategy":[],"candidate_seeds":[]}}`.
""".format(**payload)


def _extract_seed_candidates(response_text: str) -> list[str]:
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    candidates = payload.get("candidate_seeds")
    if not isinstance(candidates, list):
        return []
    out: list[str] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        seed = item.get("seed")
        if isinstance(seed, str) and seed.strip():
            out.append(seed)
    return out


def _llm_seed_provider_status() -> tuple[bool, str]:
    _load_dotenv_if_present()
    model = (os.environ.get("LLM_SEED_MODEL") or "").strip()
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("LLM_SEED_API_KEY")
        or ""
    ).strip()
    if not model:
        return False, "missing LLM_SEED_MODEL"
    if not api_key:
        return False, "missing LLM_SEED_API_KEY / ANTHROPIC_API_KEY"
    return True, "configured"


def _call_openai_compatible(prompt_text: str) -> str:
    _load_dotenv_if_present()
    api_key = os.environ.get("LLM_SEED_API_KEY")
    model = os.environ.get("LLM_SEED_MODEL")
    base_url = os.environ.get("LLM_SEED_API_URL", "https://api.openai.com/v1/chat/completions")
    if not api_key or not model:
        return ""

    response = requests.post(
        base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0.7,
            "max_tokens": 2400,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You generate seed candidates for fuzzing. Output JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt_text,
                },
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        return "".join(text_parts)
    return ""


def _call_anthropic(prompt_text: str) -> str:
    _load_dotenv_if_present()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_SEED_API_KEY")
    model = os.environ.get("LLM_SEED_MODEL")
    if not api_key or not model:
        return ""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2400,
            "temperature": 0.7,
            "system": "You generate seed candidates for fuzzing. Output JSON only.",
            "messages": [
                {
                    "role": "user",
                    "content": prompt_text,
                }
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    text_parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
    return "".join(text_parts)


def call_seed_generation_model(prompt_text: str) -> str:
    """
    Call the configured LLM provider.

    Selection rules:
    - If `LLM_SEED_API_URL` is set, use the OpenAI-compatible path.
    - Else if the configured model name starts with `claude`, call Anthropic directly.
    - Otherwise, default to the OpenAI-compatible path.
    """
    _load_dotenv_if_present()
    model = (os.environ.get("LLM_SEED_MODEL") or "").strip().lower()
    custom_url = os.environ.get("LLM_SEED_API_URL")
    if custom_url:
        return _call_openai_compatible(prompt_text)
    if model.startswith("claude"):
        return _call_anthropic(prompt_text)
    return _call_openai_compatible(prompt_text)


def maybe_generate_seed_candidates(
    *,
    conn: sqlite3.Connection,
    corpus: Any,
    target: str,
    config: FuzzConfig,
    results_folder: Path,
    include_corpus_context: bool = True,
) -> LLMSeedFallbackResult | None:
    log = get_fuzzer_logger()
    ready, provider_status = _llm_seed_provider_status()
    if not ready:
        log.warning("LLM seed generation is unavailable because %s.", provider_status)
        return None

    context = get_seed_generation_context(
        conn=conn,
        corpus=corpus,
        target=target,
        include_corpus_seed_fallback=include_corpus_context,
    )
    prompt_text = _build_prompt(target=target, context=context, config=config)
    try:
        raw_response_text = call_seed_generation_model(prompt_text)
    except requests.RequestException as exc:
        log.warning("LLM seed fallback request failed: %s", exc)
        raw_response_text = ""
    except Exception as exc:
        log.warning("LLM seed fallback failed unexpectedly: %s", exc)
        raw_response_text = ""
    seeds = _extract_seed_candidates(raw_response_text)

    debug_path = results_folder / "llm_seed_fallback"
    debug_path.mkdir(parents=True, exist_ok=True)
    prompt_file = debug_path / "last_prompt.txt"
    response_file = debug_path / "last_response.json"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    response_file.write_text(raw_response_text or "", encoding="utf-8")

    if not raw_response_text:
        log.warning(
            "LLM seed fallback produced an empty response. Check the model/API configuration."
        )
        return None
    if not seeds:
        log.warning(
            "LLM seed fallback returned output, but no candidate seeds could be parsed. "
            "See %s for the raw response.",
            response_file,
        )
        return None
    return LLMSeedFallbackResult(
        seeds=seeds,
        prompt_text=prompt_text,
        raw_response_text=raw_response_text,
    )


def make_generated_seed(
    *,
    text: str,
    family: str,
    ordinal: int,
) -> Seed:
    fingerprint = hashlib.sha256(
        f"{family}:{text}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    seed_id = f"llm-{family}-{fingerprint}"
    return Seed(
        seed_id=seed_id,
        family=family,
        bucket="generated",
        label=seed_id,
        text=text,
        tags=("llm_generated",),
        expected="",
        ordinal=ordinal,
        fingerprint=fingerprint,
    )
