import json
import re
import logging

from groq import Groq

from config import settings
from prompts.chart_mode import (
    CHART_MODE_SYSTEM,
    CHART_MODE_USER,
    CHART_MODE_RETRY,
    build_schema_description,
)
from prompts.chat_mode import CHAT_MODE_SYSTEM
from prompts.init_mode import INIT_MODE_SYSTEM, INIT_MODE_USER

log = logging.getLogger("bi-agent")

_client = Groq(api_key=settings.GROQ_API_KEY)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks that QwQ emits before the answer."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json(text: str) -> dict:
    """Extract and parse the first JSON object found in the text."""
    # Strip thinking first
    text = _strip_thinking(text)
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find JSON block with regex
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No valid JSON found in response: {text[:300]}")


def _call_llm(messages: list[dict], model: str = None, max_tokens: int = 1024) -> str:
    """Raw call to Groq. Returns the assistant message content as string."""
    model = model or settings.PRIMARY_MODEL
    response = _client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


# ── Chart Mode ────────────────────────────────────────────────────────────────

def _validate_chart_json(data: dict) -> None:
    """Raise ValueError if required keys are missing or chart_type is invalid."""
    required = {"sql", "chart_type", "x_axis", "y_axis"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Missing keys in LLM response: {missing}")

    valid_types = {"bar", "line", "pie", "scatter"}
    if data["chart_type"] not in valid_types:
        raise ValueError(f"Invalid chart_type: {data['chart_type']}")


def generate_chart_config(
    user_prompt: str,
    schema: dict | list,
    is_multi_sheet: bool = False,
    existing_config: dict = None,
    max_retries: int = 2,
) -> dict:
    """
    CHART MODE — returns validated {sql, chart_type, x_axis, y_axis, group_by, title}.
    Retries up to max_retries times with DuckDB error as context.
    Falls back to FALLBACK_MODEL on final failure.
    """
    schema_desc = build_schema_description(schema, is_multi_sheet)
    system_prompt = CHART_MODE_SYSTEM.format(schema_description=schema_desc)

    # If refining an existing chart, prepend context to the user prompt
    if existing_config:
        prompt = (
            f"Existing chart config: {json.dumps(existing_config)}\n\n"
            f"User refinement: {user_prompt}"
        )
    else:
        prompt = user_prompt

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": CHART_MODE_USER.format(user_prompt=prompt)},
    ]

    last_error = None
    raw = ""
    data = {}

    for attempt in range(max_retries + 1):
        model = settings.FALLBACK_MODEL if attempt == max_retries else settings.PRIMARY_MODEL
        try:
            raw = _call_llm(messages, model=model)
            log.info(f"LLM raw (attempt {attempt + 1}): {raw[:200]}")
            data = _extract_json(raw)
            _validate_chart_json(data)
            return data

        except Exception as e:
            last_error = str(e)
            log.warning(f"Chart mode attempt {attempt + 1} failed: {last_error}")

            if attempt < max_retries:
                sql = data.get("sql", "")
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": CHART_MODE_RETRY.format(sql=sql, error=last_error),
                })

    raise RuntimeError(f"Chart generation failed after {max_retries + 1} attempts: {last_error}")


# ── Chat Mode ─────────────────────────────────────────────────────────────────

def generate_suggestions(
    user_message: str,
    current_charts: list[dict],
    chat_history: list[dict],
    schema_columns: list[str] = None,
) -> dict:
    """
    CHAT MODE — returns {message, suggestions: [{label, action_type, chart_id, action}]}.
    current_charts: list of {chart_id, chart_type, title} for all charts on the dashboard.
    chat_history: list of {role, message} dicts, capped at last 5.
    """
    history_text = "\n".join(
        f"{h['role'].upper()}: {h['message']}" for h in chat_history[-5:]
    ) or "No previous messages."

    charts_text = "\n".join(
        f"- id:{c['chart_id']} | {c['chart_type']} | {c['title']} (x:{c.get('x_axis')} y:{c.get('y_axis')})"
        for c in current_charts
    ) or "No charts yet."

    columns_text = "\n".join(f"- {col}" for col in (schema_columns or [])) or "Unknown."

    system_prompt = CHAT_MODE_SYSTEM.format(
        schema_columns=columns_text,
        current_charts=charts_text,
        chat_history=history_text,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        raw = _call_llm(messages)
        log.info(f"Chat mode raw: {raw[:200]}")
        data = _extract_json(raw)

        if "suggestions" not in data or "message" not in data:
            raise ValueError("Missing 'message' or 'suggestions' in chat response")

        return data

    except Exception as e:
        log.warning(f"Chat mode failed: {e}")
        return {
            "message": "Here are some things you can explore:",
            "suggestions": [
                {"label": "Show top 10", "action": "Limit the results to top 10 by the main metric"},
                {"label": "Change chart type", "action": "Switch to a line chart to show trends over time"},
                {"label": "Add grouping", "action": "Group the data by a category column"},
            ],
        }


# ── Init Mode ─────────────────────────────────────────────────────────────────

def generate_initial_dashboard(
    schema: dict | list,
    is_multi_sheet: bool = False,
    prompt_suggestion: str | None = None,
) -> dict:
    """
    INIT MODE — fires once after file upload.
    Returns {"kpis": [...], "charts": [...]}.
    Never raises — returns empty lists on failure so upload is never blocked.
    """
    schema_desc = build_schema_description(schema, is_multi_sheet)
    system_prompt = INIT_MODE_SYSTEM.format(schema_description=schema_desc)
    user_priority_instructions = (prompt_suggestion or "").strip()

    if user_priority_instructions:
        system_prompt = (
            "Highest-priority user instructions (follow these first while still obeying schema and JSON rules):\n"
            f"{user_priority_instructions}\n\n"
            f"{system_prompt}"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": INIT_MODE_USER},
    ]

    empty = {"kpis": [], "charts": []}

    try:
        raw = _call_llm(messages, max_tokens=4096)
        log.info(f"Init mode raw: {raw[:400]}")
        cleaned = _strip_thinking(raw)

        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in init response")

        raw_json = match.group()
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            from json_repair import repair_json
            payload = json.loads(repair_json(raw_json))

        # Validate KPIs
        valid_kpis = []
        for k in payload.get("kpis", []):
            if "sql" in k and "title" in k and "value_key" in k:
                valid_kpis.append(k)
            else:
                log.warning(f"Skipping invalid KPI: {k}")

        # Validate charts
        valid_charts = []
        for c in payload.get("charts", []):
            try:
                _validate_chart_json(c)
                valid_charts.append(c)
            except Exception as e:
                log.warning(f"Skipping invalid init chart: {e}")

        log.info(f"Init mode: {len(valid_kpis)} KPIs, {len(valid_charts)} charts")
        return {"kpis": valid_kpis, "charts": valid_charts}

    except Exception as e:
        log.warning(f"Init dashboard generation failed: {e}")
        return empty
