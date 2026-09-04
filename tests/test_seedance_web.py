from __future__ import annotations

import unittest

from seedance_web import EXPECTED_REFERENCE_LABELS, REFERENCE_TOKEN_RE, SeedanceWebBridge


class SeedanceWebBridgeTests(unittest.TestCase):
    def test_prompt_reference_tokens_allow_optional_spaces(self) -> None:
        prompt = "参考@视频1，人物@图片 1，服装@图片2，场景@图片 3"

        labels = [f"{kind}{number}" for kind, number in REFERENCE_TOKEN_RE.findall(prompt)]

        self.assertEqual(labels, ["视频1", "图片1", "图片2", "图片3"])
        self.assertEqual(tuple(labels), EXPECTED_REFERENCE_LABELS)

    def test_nested_response_artifacts_capture_task_and_video(self) -> None:
        artifacts = {"task_ids": [], "video_urls": []}
        payload = {
            "result": {
                "task_id": "cgt-test-123",
                "content": [
                    {"type": "text", "url": "https://example.com/help"},
                    {
                        "type": "video",
                        "video_url": (
                            "https://ark-content-generation-cn-beijing."
                            "tos-cn-beijing.volces.com/result/object?token=test"
                        ),
                    },
                ],
            }
        }

        SeedanceWebBridge._collect_response_artifacts(payload, artifacts)

        self.assertEqual(artifacts["task_ids"], ["cgt-test-123"])
        self.assertEqual(len(artifacts["video_urls"]), 1)
        self.assertIn("ark-content-generation", artifacts["video_urls"][0])

    def test_mp4_download_url_is_captured(self) -> None:
        artifacts = {"task_ids": [], "video_urls": []}

        SeedanceWebBridge._collect_response_artifacts(
            {"data": {"download_url": "https://example.com/final.mp4?sign=abc"}},
            artifacts,
        )

        self.assertEqual(
            artifacts["video_urls"],
            ["https://example.com/final.mp4?sign=abc"],
        )


if __name__ == "__main__":
    unittest.main()
