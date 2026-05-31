import unittest

from jarvis.assistant import parse_assistant_message


class ParseAssistantMessageTests(unittest.TestCase):
    def test_parses_plain_json(self):
        self.assertEqual(parse_assistant_message('{"reply":"Hello"}'), {"reply": "Hello"})

    def test_parses_fenced_json(self):
        self.assertEqual(
            parse_assistant_message('```json\n{"tool":"list_files","args":{"path":"."}}\n```'),
            {"tool": "list_files", "args": {"path": "."}},
        )

    def test_falls_back_to_reply(self):
        self.assertEqual(parse_assistant_message("Hello there"), {"reply": "Hello there"})


if __name__ == "__main__":
    unittest.main()
