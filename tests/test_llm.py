import os
import unittest
from unittest import mock

import llm


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.ok = status < 400

    def json(self):
        return self._body


def ok(text="{}"):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


def quota(seconds):
    return Resp(429, {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{seconds}s"}]}})


class LlmTest(unittest.TestCase):
    def post(self, replies, models=("m1", "m2")):
        it, urls, naps, events = iter(replies), [], [], []
        def fake(url, **kw):
            urls.append(url.split("/models/")[1].split(":")[0])
            return next(it)
        with mock.patch.object(llm.requests, "post", fake), llm.listening(lambda t, s: events.append(s)):
            try:
                out = llm.post("k", {}, models, sleep=naps.append)
            except RuntimeError as e:
                out = e
        return out, urls, naps, events

    def test_model_chain_uses_llm_model_first_then_fallbacks(self):
        with mock.patch.object(llm, "load_dotenv"):
            with mock.patch.dict(os.environ, {llm.MODEL_VAR: "my-model"}):
                self.assertEqual(llm.model_chain(), ("my-model", *llm.FALLBACK_MODELS))
            with mock.patch.dict(os.environ, {llm.MODEL_VAR: ""}):
                self.assertEqual(llm.model_chain()[0], "gemini-3.5-flash-lite")
            with mock.patch.dict(os.environ, {llm.MODEL_VAR: llm.FALLBACK_MODELS[0]}):
                self.assertEqual(llm.model_chain(), llm.FALLBACK_MODELS)  # no duplicates

    def test_429_waits_the_stated_delay_then_retries_the_same_model(self):
        (cand, model), urls, naps, events = self.post([quota(13), ok()])
        self.assertEqual((model, urls, naps), ("m1", ["m1", "m1"], [13.0]))
        self.assertTrue(any("waiting 13s" in e for e in events))
        self.assertTrue(any("answered by m1 after 1 retry" in e for e in events))

    def test_at_most_two_retries_then_next_model(self):
        (cand, model), urls, naps, _ = self.post([quota(2), quota(2), quota(2), ok()])
        self.assertEqual((model, urls, naps), ("m2", ["m1", "m1", "m1", "m2"], [2.0, 2.0]))

    def test_delay_over_the_cap_is_not_waited_out(self):
        (cand, model), urls, naps, _ = self.post([quota(45), ok()])
        self.assertEqual((model, urls, naps), ("m2", ["m1", "m2"], []))

    def test_all_models_failing_raises_with_every_reason(self):
        err, urls, _, _ = self.post([Resp(503), Resp(500)])
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("m1: HTTP 503", str(err))
        self.assertIn("m2: HTTP 500", str(err))

    def test_retry_delay_parsing(self):
        self.assertEqual(llm.retry_delay(quota(7)), 7.0)
        self.assertEqual(llm.retry_delay(Resp(429, {"error": {"message": "Please retry in 13.5s."}})), 13.5)
        self.assertEqual(llm.retry_delay(Resp(429, {}, {"Retry-After": "4"})), 4.0)
        self.assertIsNone(llm.retry_delay(Resp(429, {})))


if __name__ == "__main__":
    unittest.main()
