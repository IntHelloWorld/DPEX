import unittest
from unittest.mock import patch

from mllmfl.stages.refine.client import _post_response


TOOL = {
    "type": "function",
    "name": "inspect",
    "description": "Inspect one runtime occurrence.",
    "parameters": {
        "type": "object",
        "properties": {"invocation_id": {"type": "string"}},
        "required": ["invocation_id"],
        "additionalProperties": False,
    },
}


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.status_code = 200
        self.text = ""
        self._body = body

    def json(self) -> dict:
        return self._body


class RefinementClientTests(unittest.TestCase):
    def config(self, api_style: str) -> dict:
        return {
            "mllm": {
                "api_style": api_style,
                "base_url": "https://provider.example/v1",
                "api_key_env": "REFINEMENT_TEST_KEY",
                "vision_model": "test-model",
                "reasoning_effort": "high",
                "retry": {"max_retries": 1},
            }
        }

    @patch.dict("os.environ", {"REFINEMENT_TEST_KEY": "secret"})
    @patch("mllmfl.stages.refine.client.requests.post")
    def test_responses_payload_keeps_refinement_contract(self, post) -> None:
        post.return_value = FakeResponse({
            "id": "resp-1",
            "model": "served-model",
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "[]"}],
            }],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        })

        message, model, response_id, output, usage, finish_reason = _post_response(
            self.config("responses"),
            [{"role": "user", "content": [{"type": "text", "text": "rank"}]}],
            30,
            instructions="system",
            tools=[TOOL],
            max_tokens=321,
            prompt_cache_key="cache-key",
        )

        self.assertEqual(message["content"], "[]")
        self.assertEqual(model, "served-model")
        self.assertEqual(response_id, "resp-1")
        self.assertEqual(output, post.return_value._body["output"])
        self.assertEqual(usage["input_tokens"], 3)
        self.assertIsNone(finish_reason)
        endpoint = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertEqual(endpoint, "https://provider.example/v1/responses")
        self.assertEqual(payload["instructions"], "system")
        self.assertEqual(payload["max_output_tokens"], 321)
        self.assertEqual(payload["tools"], [TOOL])
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["prompt_cache_key"], "cache-key")
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertFalse(payload["store"])

    @patch.dict("os.environ", {"REFINEMENT_TEST_KEY": "secret"})
    @patch("mllmfl.stages.refine.client.requests.post")
    def test_chat_payload_preserves_reasoning_and_finish_reason(self, post) -> None:
        post.return_value = FakeResponse({
            "id": "chat-1",
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "content": None,
                    "reasoning_content": "still reasoning",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "inspect",
                            "arguments": '{"invocation_id":"T1-C2"}',
                        },
                    }],
                },
            }],
            "usage": {"prompt_tokens": 4},
        })

        message, _, response_id, output, _, finish_reason = _post_response(
            self.config("chat_completions"),
            [{"role": "user", "content": "rank"}],
            30,
            instructions="system",
            tools=[TOOL],
            max_tokens=654,
        )

        self.assertEqual(response_id, "chat-1")
        self.assertEqual(message["reasoning_content"], "still reasoning")
        self.assertEqual(output, [message])
        self.assertEqual(finish_reason, "length")
        endpoint = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertEqual(endpoint, "https://provider.example/v1/chat/completions")
        self.assertEqual(payload["messages"][0], {
            "role": "system", "content": "system",
        })
        self.assertEqual(payload["max_tokens"], 654)
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["tools"], [{
            "type": "function",
            "function": {key: value for key, value in TOOL.items() if key != "type"},
        }])
        self.assertFalse(payload["parallel_tool_calls"])

    @patch.dict("os.environ", {"REFINEMENT_TEST_KEY": "secret"})
    def test_refinement_tools_are_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "refinement tools"):
            _post_response(
                self.config("responses"),
                [{"role": "user", "content": "rank"}],
                30,
                instructions="system",
                tools=None,
            )


if __name__ == "__main__":
    unittest.main()
