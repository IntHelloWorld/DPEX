import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import requests

from mllmfl.domain.models import Ranking
from mllmfl.domain.schemas import validate_candidates
from mllmfl.infrastructure.io import read_json, write_csv, write_json, write_text
from mllmfl.infrastructure.layout import RunLayout


SYSTEM_PROMPT = """You are an expert in software defect localization. Use the failing behavior,
sequence diagram, and candidate summaries to rank the most likely defective functions.
Return JSON only and select functions exclusively from the supplied candidate list."""


def parse_model_response(text: str) -> Dict[str, Any] | None:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else ""
    if value.endswith("```"):
        value = value.rsplit("\n", 1)[0]
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group())
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def gate_ranking(
    raw: Any,
    candidates: Sequence[str],
    top_k: int,
) -> Tuple[List[Ranking], List[str]]:
    """Resolve model output strictly against unambiguous supplied candidates."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    lookup: Dict[str, str] = {}
    ambiguous = set()
    for candidate in candidates:
        for key in (candidate, ".".join(candidate.split(".")[-2:]), candidate.split(".")[-1]):
            if key in lookup and lookup[key] != candidate:
                ambiguous.add(key)
            else:
                lookup[key] = candidate
    for key in ambiguous:
        lookup.pop(key, None)
    result, dropped, seen = [], [], set()
    if not isinstance(raw, list):
        return result, dropped
    for item in raw:
        if not isinstance(item, dict):
            continue
        supplied = str(item.get("function") or item.get("name") or "").strip()
        candidate = lookup.get(supplied)
        if candidate is None:
            dropped.append(supplied)
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        result.append(Ranking(candidate, len(result) + 1, str(item.get("reason") or "")[:300]))
        if len(result) >= top_k:
            break
    return result, dropped


def build_prompt(
    project: str,
    bug: str,
    test: str,
    failure: str,
    candidates: Sequence[Dict[str, Any]],
    top_k: int,
) -> str:
    summaries = "\n".join(
        f"- {item['function']} | {item.get('summary', '')}" for item in candidates
    )
    return f"""[Project] {project}
[Bug] {bug}
[Failing Test] {test}
[Failure]
{failure[:6000]}

[Candidate Functions]
{summaries[:30000]}

Rank the top {top_k} most likely defective functions. Return:
{{"ranked":[{{"function":"fully.qualified.function","reason":"brief evidence"}}]}}"""


def _call(config: Dict[str, Any], prompt: str, image: Path, timeout: int) -> tuple[str, str]:
    cfg = config.get("mllm", config)
    if cfg.get("api_key"):
        raise ValueError("inline api_key is forbidden; configure api_key_env")
    env_name = str(cfg.get("api_key_env") or "OPENAI_API_KEY")
    api_key = os.environ.get(env_name)
    if not api_key:
        raise ValueError(f"API key missing from {env_name}")
    base_url = str(cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = str(cfg.get("vision_model") or cfg.get("model") or "")
    if not model:
        raise ValueError("vision_model is required")
    image_b64 = base64.b64encode(image.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": int(cfg.get("max_tokens", 4096)),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            },
        ],
    }
    retry = cfg.get("retry") or {}
    attempts, backoff = int(retry.get("max_retries", 3)), float(retry.get("backoff_s", 2))
    last_error = ""
    for attempt in range(attempts):
        try:
            response = requests.post(
                base_url + "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
            data = response.json()
            return str(data["choices"][0]["message"]["content"]), model
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as error:
            last_error = str(error)
            if attempt + 1 < attempts:
                time.sleep(min(backoff * (2**attempt), 15))
    raise RuntimeError(last_error or "model request failed")


def run(
    layout: RunLayout,
    projects: Sequence[str],
    bugs: set[str] | None,
    trigger: str | None,
    config_path: Path,
    timeout: int,
    top_k: int | None,
    dry_run: bool,
    force: bool = False,
) -> List[Dict[str, object]]:
    config = read_json(config_path)
    cfg = config.get("mllm", config)
    selected_top_k = top_k if top_k is not None else int(cfg.get("top_k", 5))
    if selected_top_k <= 0:
        raise ValueError("top_k must be positive")
    rows = []
    for project, bug, number, directory in layout.discover_triggers(projects, bugs, trigger):
        result_path = directory / "localization.json"
        if result_path.exists() and not force:
            rows.append(
                {
                    "project": project,
                    "bug": bug,
                    "trigger": number,
                    "status": "SKIPPED",
                    "top1": "",
                }
            )
            continue
        try:
            data = read_json(directory / "candidates.json")
            validate_candidates(data)
            image_path = directory / "sequence.png"
            if not image_path.is_file():
                raise FileNotFoundError(f"UML image not found: {image_path}")
            candidates = data.get("candidates") or []
            test = (directory / "trigger_test.txt").read_text(encoding="utf-8").strip()
            failure_path = directory / "failure.txt"
            failure = (
                failure_path.read_text(encoding="utf-8", errors="ignore")
                if failure_path.exists()
                else ""
            )
            prompt = build_prompt(project, bug, test, failure, candidates, selected_top_k)
            write_text(directory / "prompt.txt", SYSTEM_PROMPT + "\n\n" + prompt + "\n")
            if dry_run:
                status, ranking, dropped, model = "DRY_RUN", [], [], ""
            else:
                raw, model = _call(config, prompt, image_path, timeout)
                parsed = parse_model_response(raw)
                if parsed is None:
                    raise ValueError("model returned invalid JSON")
                ranking, dropped = gate_ranking(
                    parsed.get("ranked"),
                    [candidate["function"] for candidate in candidates],
                    selected_top_k,
                )
                status = "OK" if ranking else "EMPTY_RANKING"
            output = {
                "schema": "fault-localization",
                "schema_version": 1,
                "project": project,
                "bug": bug,
                "trigger": number,
                "status": status,
                "model": model,
                "candidate_count": len(candidates),
                "ranking": [item.to_dict() for item in ranking],
                "dropped_non_candidates": dropped,
            }
            write_json(result_path, output)
            top1 = ranking[0].function if ranking else ""
            rows.append(
                {
                    "project": project,
                    "bug": bug,
                    "trigger": number,
                    "status": status,
                    "top1": top1,
                }
            )
        except Exception as error:
            write_text(layout.stage_log_dir("localize", project, bug, number) / "error.log",
                       str(error) + "\n")
            rows.append(
                {
                    "project": project,
                    "bug": bug,
                    "trigger": number,
                    "status": "ERROR",
                    "top1": "",
                }
            )
    write_csv(layout.logs / "localize.csv", rows, ["project", "bug", "trigger", "status", "top1"])
    return rows
