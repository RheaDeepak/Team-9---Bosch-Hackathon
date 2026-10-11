import os
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage

from agents import model
from agents.models import HypothesisVerdict


class FakeChat:
    def __init__(self, value):
        self.value = value

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema, method):
        return self

    def invoke(self, messages):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class ModelFactoryTest(unittest.TestCase):
    def test_key_and_configured_fallback_chain(self):
        env = {model.API_KEY_VAR: "key", model.MODEL_VAR: "primary",
               model.FALLBACK_MODELS_VAR: "secondary, primary, tertiary"}
        with mock.patch.dict(os.environ, env), mock.patch.object(model, "load_dotenv"):
            self.assertEqual(model.load_api_key(), "key")
            self.assertEqual(model.model_chain(), ("primary", "secondary", "tertiary"))

    def test_placeholder_key_is_unavailable(self):
        with mock.patch.dict(os.environ, {model.API_KEY_VAR: model.PLACEHOLDER_KEY}), \
                mock.patch.object(model, "load_dotenv"):
            self.assertIsNone(model.load_api_key())

    def test_tool_call_falls_back_to_the_next_model(self):
        models = model.GeminiModels.__new__(model.GeminiModels)
        models.names = ("first", "second")
        answer = AIMessage(content="ready")
        models.models = [FakeChat(RuntimeError("quota")), FakeChat(answer)]
        actual, name = models.invoke_tools("system", [HumanMessage(content="input")], [])
        self.assertIs(actual, answer)
        self.assertEqual(name, "second")

    def test_structured_output_is_pydantic_validated(self):
        models = model.GeminiModels.__new__(model.GeminiModels)
        models.names = ("only",)
        models.models = [FakeChat({"claims": [], "verdict": "held", "explanation": "ok",
                                  "recommendation": "keep testing"})]
        value, name = models.invoke_structured("system", [], HypothesisVerdict)
        self.assertEqual((value.verdict, name), ("held", "only"))


if __name__ == "__main__":
    unittest.main()
