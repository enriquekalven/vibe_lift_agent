"""Local LLM-as-judge for `custom_response_quality` (see eval_config.yaml).

Evaluates the agent's final response on a 1-5 scale for accuracy, relevance,
clarity, and grounding in the tool trace (`agent_data`) and optional `reference`.
Supports Google AI Studio (`GEMINI_API_KEY`), Vertex AI ADC (`google.auth.default`),
and active `gcloud auth` credentials for local CLI execution.
"""

import json
import os
import subprocess
import urllib.request

JUDGE_MODEL = os.environ.get("FINOPS_EVAL_JUDGE_MODEL", "gemini-3.8-flash")
JUDGE_LOCATION = (
    os.environ.get("FINOPS_EVAL_JUDGE_LOCATION")
    or ("global" if JUDGE_MODEL.startswith("gemini-3") else None)
    or os.environ.get("GOOGLE_CLOUD_LOCATION")
    or os.environ.get("GOOGLE_CLOUD_REGION")
    or "global"
)


def _resolve_project_and_token() -> tuple[str | None, str | None]:
  """Resolves (project_id, bearer_token) from ADC or gcloud CLI."""
  project = (
      os.environ.get("GOOGLE_CLOUD_PROJECT")
      or os.environ.get("GCLOUD_PROJECT")
      or None
  )
  try:
    import google.auth
    from google.auth.transport.requests import Request as GoogleAuthRequest

    creds, adc_proj = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    project = project or adc_proj
    if not creds.valid:
      creds.refresh(GoogleAuthRequest())
    if getattr(creds, "token", None):
      return project, creds.token
  except Exception:
    pass

  if not project:
    try:
      res = subprocess.run(
          ["gcloud", "config", "get-value", "project"],
          capture_output=True,
          text=True,
          timeout=5.0,
          check=False,
      )
      if res.returncode == 0 and res.stdout.strip():
        project = res.stdout.strip()
    except Exception:
      pass

  try:
    res = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
      return project, res.stdout.strip()
  except Exception:
    pass
  return project, None


def evaluate(instance: dict) -> dict:
  """Grades the agent response on a 1-5 scale using Gemini LLM-as-a-Judge."""
  reference = instance.get("reference")
  rubric = (
      "Grade the agent's final response on a 1-5 integer scale (1 poor, 5 excellent) for "
      "accuracy, relevance, clarity, and strict factual grounding in the tool outputs."
  )
  if reference:
    rubric += (
        " The response should agree with the expected reference answer below; penalize "
        "factual disagreement or hallucinated numbers."
    )
  prompt = (
      f"You are an expert QA evaluator for the FinOps Dashboard Google Cloud FinOps & Agent Telemetry assistant.\n"
      f"{rubric}\n\n"
      f"User Prompt: {json.dumps(instance.get('prompt', ''))}\n"
      f"Final Response: {json.dumps(instance.get('response', ''))}\n"
  )
  if reference:
    prompt += f"Expected Reference Answer: {json.dumps(reference)}\n"
  prompt += (
      f"Full Agent Trace (tool calls and outputs): {json.dumps(instance.get('agent_data', ''))}\n\n"
      'Respond ONLY with a valid JSON object: {"score": <int 1-5>, "explanation": "<concise rationale>"}'
  )

  project, token = _resolve_project_and_token()
  if not project or not token:
    raise RuntimeError("No valid Google Cloud project or access token available for LLM judge.")

  host = (
      "aiplatform.googleapis.com"
      if JUDGE_LOCATION == "global"
      else f"{JUDGE_LOCATION}-aiplatform.googleapis.com"
  )
  url = (
      f"https://{host}/v1/projects/{project}"
      f"/locations/{JUDGE_LOCATION}/publishers/google/models/{JUDGE_MODEL}:generateContent"
  )
  body = json.dumps({
      "contents": [{"role": "user", "parts": [{"text": prompt}]}],
      "generationConfig": {
          "temperature": 0.0,
          "maxOutputTokens": 1024,
          "responseMimeType": "application/json",
          "thinkingConfig": {"thinkingBudget": 0},
      },
  }).encode("utf-8")

  import time

  last_exc: Exception | None = None
  resp_data: dict = {}
  for attempt in range(3):
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-goog-user-project": project,
        },
        method="POST",
    )
    try:
      with urllib.request.urlopen(req, timeout=30.0) as resp:
        resp_data = json.loads(resp.read().decode("utf-8"))
      last_exc = None
      break
    except Exception as exc:  # pylint: disable=broad-except
      last_exc = exc
      if attempt < 2:
        time.sleep(1.5 * (attempt + 1))
  if last_exc is not None:
    raise last_exc

  candidates = resp_data.get("candidates") or []
  parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
  non_thought = [
      str(p.get("text") or "").strip()
      for p in parts
      if isinstance(p, dict) and not p.get("thought") and str(p.get("text") or "").strip()
  ]
  text = non_thought[-1] if non_thought else (str(parts[0].get("text") or "").strip() if parts else "")
  if not text:
    return {"score": 0, "explanation": "Judge model returned empty response."}

  verdict = json.loads(text)
  score = int(verdict.get("score") or 0)
  explanation = str(verdict.get("explanation") or "")
  return {"score": max(1, min(5, score)), "explanation": explanation}
