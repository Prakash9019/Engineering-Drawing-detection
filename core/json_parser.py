"""
Robust JSON Parser
==================
Extracts JSON arrays and objects from Gemini responses even when they're
wrapped in markdown fences, contain explanatory text, or have minor malformations.
"""
import json
import re
import logging
from typing import Union

log = logging.getLogger(__name__)


def parse_json(raw: str) -> Union[list, dict, str]:
    """
    Robustly parse JSON from a possibly-noisy LLM response.

    Strategy (in order):
      1. Strip markdown fences and direct json.loads
      2. Bracket-depth matching to find outermost array/object
      3. Find individual {...} objects via regex
      4. Fix common issues (trailing commas, single quotes) and retry

    Returns:
        Parsed list/dict if successful, otherwise the raw string.
    """
    if not raw:
        return []

    t = raw.strip()

    # Strip markdown fences: ```json ... ```
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```\s*$', '', t).strip()

    # Strategy 1: direct parse
    try:
        result = json.loads(t)
        if isinstance(result, (list, dict)):
            return result
    except json.JSONDecodeError:
        pass

    # Strategy 2: bracket-depth scan for outermost [...] or {...}
    depth = 0
    start = -1
    for i, ch in enumerate(t):
        if ch in '[{':
            if depth == 0:
                start = i
            depth += 1
        elif ch in ']}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    result = json.loads(t[start:i+1])
                    return result
                except json.JSONDecodeError:
                    start = -1  # try again

    # Strategy 3: find individual {...} objects
    objs = re.findall(r'\{[^{}]+\}', t)
    results = []
    for obj_str in objs:
        try:
            parsed = json.loads(obj_str)
            if isinstance(parsed, dict):
                results.append(parsed)
        except json.JSONDecodeError:
            # Try fixing: single quotes → double, remove trailing comma
            try:
                fixed = obj_str.replace("'", '"').rstrip(',').rstrip()
                parsed = json.loads(fixed)
                if isinstance(parsed, dict):
                    results.append(parsed)
            except Exception:
                pass
    if results:
        return results

    # Strategy 4: fix the whole string and retry
    try:
        fixed = re.sub(r',\s*]', ']', t)
        fixed = re.sub(r',\s*}', '}', fixed)
        result = json.loads(fixed)
        if isinstance(result, (list, dict)):
            return result
    except json.JSONDecodeError:
        pass

    log.warning(f"Could not parse JSON, returning raw. Preview: {t[:150]}")
    return t
