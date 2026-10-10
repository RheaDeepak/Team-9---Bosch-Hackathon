"""The one place that talks to the LLM (planner, planning agent and verdict all go through post()).

Model order: LLM_MODEL from .env (default gemini-3.5-flash-lite), then the shared fallback list.
On HTTP 429 the API says how long to wait; we wait that long (capped) and retry the same model before
moving on to the next one. Every answer is logged with the model that gave it.
"""
import logging
import os
import re
import threading
import time
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

API_KEY_VAR = "LLM_API_KEY"
MODEL_VAR = "LLM_MODEL"
PLACEHOLDER_KEY = "your-api-key-here"  # value shipped in .env.example
DEFAULT_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = ("gemini-3-flash-preview", "gemini-flash-lite-latest")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_TOKENS = 4096  # thinking models spend part of this on reasoning before the answer
TIMEOUT_S = 20
MAX_RETRIES = 2          # extra attempts on one model after a 429
MAX_RETRY_WAIT_S = 20    # a longer retry delay is not worth waiting for: use the next model instead
DEFAULT_RETRY_WAIT_S = 5  # 429 without a stated delay

log = logging.getLogger("llm")
last_model = None  # the model that answered most recently (any thread)
_local = threading.local()


def load_api_key():
    """The Gemini API key from .env / the environment, or None if unset or still the placeholder."""
    load_dotenv()
    key = os.getenv(API_KEY_VAR, "").strip()
    return None if not key or key == PLACEHOLDER_KEY else key


def model_chain():
    """LLM_MODEL first, then the fallbacks, without duplicates."""
    load_dotenv()
    first = os.getenv(MODEL_VAR, "").strip() or DEFAULT_MODEL
    return tuple(dict.fromkeys((first, *FALLBACK_MODELS)))


@contextmanager
def listening(fn):
    """Route this thread's LLM events (fn(type, summary)) to fn, e.g. the agent's activity feed."""
    prev = getattr(_local, "fn", None)
    _local.fn = fn
    try:
        yield
    finally:
        _local.fn = prev


def _event(summary):
    log.info(summary)
    fn = getattr(_local, "fn", None)
    if fn:
        try:
            fn("llm", summary)
        except Exception:  # a broken listener must never break a call
            pass


def retry_delay(resp):
    """Seconds the API asks us to wait after a 429 (RetryInfo, 'retry in 13.2s', or Retry-After); None if unstated."""
    try:
        for d in resp.json().get("error", {}).get("details", []):
            m = re.fullmatch(r"\s*([\d.]+)s\s*", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
        m = re.search(r"retry in ([\d.]+)s", str(resp.json().get("error", {}).get("message", "")), re.I)
        if m:
            return float(m.group(1))
    except (ValueError, AttributeError):
        pass
    try:
        return float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


def post(api_key, body, models=None, sleep=time.sleep):
    """POST one generateContent request -> (candidate, model). Waits out 429s, then tries the next model."""
    global last_model
    errors = []
    for model in (tuple(models) if models else model_chain()):
        retries = 0
        while True:
            try:
                resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=TIMEOUT_S,
                                     headers={"x-goog-api-key": api_key})
            except requests.RequestException as e:
                errors.append(f"{model}: {type(e).__name__}")
                _event(f"{model}: {type(e).__name__}, trying the next model")
                break
            if resp.status_code == 429:
                delay = retry_delay(resp)
                wait = DEFAULT_RETRY_WAIT_S if delay is None else delay
                if retries < MAX_RETRIES and wait <= MAX_RETRY_WAIT_S:
                    retries += 1
                    _event(f"{model}: HTTP 429, waiting {wait:.0f}s (retry {retries}/{MAX_RETRIES})")
                    sleep(wait)
                    continue
                errors.append(f"{model}: HTTP 429" + (f" (retry in {delay:.0f}s)" if delay is not None else ""))
                _event(f"{model}: HTTP 429, {'retries used up' if retries else f'retry delay {delay:.0f}s is too long'};"
                       " trying the next model")
                break
            if resp.ok:
                cand = resp.json()["candidates"][0]
                if cand.get("finishReason") == "MAX_TOKENS":
                    errors.append(f"{model}: reply truncated (MAX_TOKENS)")
                    _event(f"{model}: reply truncated (MAX_TOKENS), trying the next model")
                    break
                last_model = model
                _event(f"answered by {model}" + (f" after {retries} retr{'y' if retries == 1 else 'ies'}" if retries else ""))
                return cand, model
            errors.append(f"{model}: HTTP {resp.status_code}")
            _event(f"{model}: HTTP {resp.status_code}, trying the next model")
            break
    raise RuntimeError("all models failed (" + "; ".join(errors) + ")")
