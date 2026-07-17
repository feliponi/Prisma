"""AI integration module for local LLM calls via Ollama.

Provides transaction categorization and financial insight generation by
calling a locally-hosted Ollama model over its REST API. All prompts enforce
strict JSON output where applicable, with robust parsing fallbacks for
handling LLM formatting hallucinations.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import pandas as pd
import requests

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "llama3.1"
DEFAULT_TIMEOUT_SECONDS = 60
FALLBACK_CATEGORY = "Outros"

DEFAULT_CATEGORIES = [
    "Alimentacao",
    "Transporte",
    "Moradia",
    "Saude",
    "Educacao",
    "Lazer",
    "Compras",
    "Servicos",
    "Salario",
    "Investimentos",
    "Transferencias",
    "Outros",
]


class OllamaConnectionError(Exception):
    """Raised when the local Ollama instance cannot be reached."""


@dataclass
class InsightRequest:
    """Aggregated data needed to generate a budget-conciliation narrative."""

    category_spending: dict[str, float]
    category_budget: dict[str, float]


def _call_ollama(
    prompt: str,
    system_prompt: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    format_json: bool = False,
) -> str:
    """Issue a single generation request to the local Ollama API.

    Raises:
        OllamaConnectionError: if the request fails, times out, or Ollama is unreachable.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    if format_json:
        payload["format"] = "json"

    try:
        response = requests.post(base_url, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        logger.error("Could not connect to Ollama at %s: %s", base_url, exc)
        raise OllamaConnectionError(
            f"Could not connect to local Ollama instance at {base_url}. "
            "Ensure Ollama is running (`ollama serve`)."
        ) from exc
    except requests.exceptions.Timeout as exc:
        logger.error("Ollama request timed out after %ds: %s", timeout, exc)
        raise OllamaConnectionError(f"Ollama request timed out after {timeout}s.") from exc
    except requests.exceptions.RequestException as exc:
        logger.error("Ollama request failed: %s", exc)
        raise OllamaConnectionError(f"Ollama request failed: {exc}") from exc

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        logger.error("Ollama returned non-JSON HTTP response: %s", exc)
        raise OllamaConnectionError("Ollama returned an unparseable HTTP response.") from exc

    return data.get("response", "")


def _extract_json_block(text: str) -> str | None:
    """Best-effort extraction of a JSON object/array embedded in free-form text,
    used as a fallback when the LLM wraps JSON in markdown fences or prose."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)

    start_candidates = [i for i, ch in enumerate(text) if ch in "{["]
    end_candidates = [i for i, ch in enumerate(text) if ch in "}]"]
    if not start_candidates or not end_candidates:
        return None

    start = start_candidates[0]
    end = end_candidates[-1]
    if end <= start:
        return None
    return text[start : end + 1]


def _safe_parse_json(text: str) -> dict | list | None:
    """Parse JSON from LLM output, tolerating markdown fences and surrounding prose."""
    for candidate in (text, _extract_json_block(text)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _build_categorization_prompt(descriptions: list[str], categories: list[str]) -> str:
    numbered = "\n".join(f"{i}: {desc}" for i, desc in enumerate(descriptions))
    categories_list = ", ".join(categories)
    return (
        "Classify each bank transaction description below into exactly one of the "
        f"allowed categories: [{categories_list}].\n"
        f"If none of the categories clearly apply, use \"{FALLBACK_CATEGORY}\".\n"
        "Do not invent new categories. Respond ONLY with a JSON object mapping the "
        'transaction index (as a string) to the chosen category, e.g. {"0": "Alimentacao"}.\n\n'
        f"Transactions:\n{numbered}"
    )


CATEGORIZATION_SYSTEM_PROMPT = (
    "You are a financial data classification engine. You strictly select categories "
    "from a provided list and always respond with valid JSON only, no explanations, "
    "no markdown formatting."
)

INSIGHTS_SYSTEM_PROMPT = (
    "You are a financial analyst assistant. You write concise, analytical executive "
    "summaries in Brazilian Portuguese (pt_BR) about personal budget conciliation, "
    "highlighting the largest deviations and their likely drivers. You never use "
    "JSON or markdown formatting, only plain narrative text."
)


def categorize_transactions(
    df: pd.DataFrame,
    categories: list[str] | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    batch_size: int = 25,
) -> pd.DataFrame:
    """Categorize each transaction's description using the local LLM.

    Args:
        df: DataFrame with a `description` column (canonical data model).
        categories: Allowed category list. Defaults to DEFAULT_CATEGORIES.
        model: Ollama model name.
        base_url: Ollama `/api/generate` endpoint URL.
        timeout: Per-request timeout in seconds.
        batch_size: Number of descriptions sent per LLM call.

    Returns:
        A copy of `df` with the `category` column populated by the LLM
        (falling back to FALLBACK_CATEGORY for any row that failed to parse).
    """
    if categories is None:
        categories = DEFAULT_CATEGORIES

    result_df = df.copy()
    result_df["category"] = FALLBACK_CATEGORY

    descriptions = result_df["description"].fillna("").tolist()

    for batch_start in range(0, len(descriptions), batch_size):
        batch = descriptions[batch_start : batch_start + batch_size]
        prompt = _build_categorization_prompt(batch, categories)

        try:
            raw_response = _call_ollama(
                prompt=prompt,
                system_prompt=CATEGORIZATION_SYSTEM_PROMPT,
                model=model,
                base_url=base_url,
                timeout=timeout,
                format_json=True,
            )
        except OllamaConnectionError as exc:
            logger.error("Categorization batch starting at %d failed: %s", batch_start, exc)
            continue

        parsed = _safe_parse_json(raw_response)
        if not isinstance(parsed, dict):
            logger.warning(
                "Could not parse categorization JSON for batch starting at %d; raw=%r",
                batch_start,
                raw_response,
            )
            continue

        for local_index_str, category in parsed.items():
            try:
                local_index = int(local_index_str)
            except (TypeError, ValueError):
                continue
            global_index = batch_start + local_index
            if global_index >= len(result_df):
                continue
            category_clean = str(category).strip()
            result_df.iat[global_index, result_df.columns.get_loc("category")] = (
                category_clean if category_clean in categories else FALLBACK_CATEGORY
            )

    return result_df


def generate_financial_insights(
    category_spending: dict[str, float],
    category_budget: dict[str, float],
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Generate a plain-text executive summary of budget conciliation.

    Args:
        category_spending: Actual spending per category.
        category_budget: Planned budget per category.
        model: Ollama model name.
        base_url: Ollama `/api/generate` endpoint URL.
        timeout: Request timeout in seconds.

    Returns:
        A concise (max 2 paragraphs) narrative in Brazilian Portuguese highlighting
        the largest deviations and budget overrun drivers. Returns an error message
        string (not raised) if the local LLM is unreachable, so the UI can degrade
        gracefully.
    """
    comparison_lines = []
    for category in sorted(set(category_spending) | set(category_budget)):
        spent = category_spending.get(category, 0.0)
        budget = category_budget.get(category, 0.0)
        deviation = spent - budget
        comparison_lines.append(
            f"- {category}: gasto={spent:.2f}, orcamento={budget:.2f}, desvio={deviation:+.2f}"
        )
    comparison_text = "\n".join(comparison_lines)

    prompt = (
        "Com base nos dados de gastos por categoria comparados ao orcamento planejado, "
        "escreva um resumo executivo conciso (no maximo 2 paragrafos) destacando os maiores "
        "desvios e os principais fatores de estouro de orcamento. Responda em texto corrido, "
        "sem JSON e sem marcacao markdown.\n\n"
        f"Dados:\n{comparison_text}"
    )

    try:
        response_text = _call_ollama(
            prompt=prompt,
            system_prompt=INSIGHTS_SYSTEM_PROMPT,
            model=model,
            base_url=base_url,
            timeout=timeout,
            format_json=False,
        )
    except OllamaConnectionError as exc:
        logger.error("Failed to generate financial insights: %s", exc)
        return (
            "Nao foi possivel gerar os insights financeiros: o servico de IA local "
            "esta indisponivel. Verifique se o Ollama esta em execucao."
        )

    return response_text.strip()
