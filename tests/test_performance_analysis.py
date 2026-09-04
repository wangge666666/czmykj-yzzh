from __future__ import annotations

import unittest
from unittest.mock import Mock

from performance_analysis import (
    ArkPerformanceAnalyzer,
    build_performance_prompt,
    build_white_model_performance_prompt,
    normalize_cast_continuity,
    normalize_performance_analysis,
)


class PerformanceAnalysisTests(unittest.TestCase):
    def test_cast_continuity_keeps_identity_when_people_change_sides(self) -> None:
        result = normalize_cast_continuity(
            {
                "characters": [
                    {"character_id": 1, "description": "女性"},
                    {"character_id": 2, "description": "男性"},
                ],
                "assignments": [
                    {"shot_index": 1, "slot": 1, "character_id": 2, "confidence": 0.9},
                    {"shot_index": 1, "slot": 2, "character_id": 1, "confidence": 0.9},
                    {"shot_index": 2, "slot": 1, "character_id": 1, "confidence": 0.9},
                    {"shot_index": 2, "slot": 2, "character_id": 2, "confidence": 0.9},
                ],
            },
            shot_slot_counts={1: 2, 2: 2},
        )
        self.assertEqual(
            [(item["shot_index"], item["slot"], item["character_id"]) for item in result["assignments"]],
            [(1, 1, 2), (1, 2, 1), (2, 1, 1), (2, 2, 2)],
        )

    def test_cast_continuity_rejects_missing_slots(self) -> None:
        with self.assertRaisesRegex(Exception, "缺少槽位"):
            normalize_cast_continuity(
                {"assignments": [{"shot_index": 1, "slot": 1, "character_id": 1}]},
                shot_slot_counts={1: 2},
            )

    def test_cast_continuity_invalid_json_uses_specific_error_label(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"output_text": "这不是JSON"}
        session = Mock()
        session.post.return_value = response
        analyzer = ArkPerformanceAnalyzer("test-key", session=session)
        with self.assertRaisesRegex(Exception, "跨镜人物连续性分析接口没有返回可解析的 JSON"):
            analyzer.analyze_cast_continuity(
                "data:video/mp4;base64,AAAA",
                shot_slot_counts={1: 1},
                shot_ranges=[{"index": 1, "start": 0.0, "end": 1.0}],
            )

    def test_holistic_expression_semantics_are_not_replaced_by_cues(self) -> None:
        analysis = normalize_performance_analysis(
            {
                "has_dialogue": True,
                "dialogue": [
                    {
                        "speaker_slot": 1,
                        "start": 0.2,
                        "end": 1.2,
                        "text": "你好",
                        "delivery": "轻声，有短暂停顿",
                        "mouth_motion": "先闭唇，随后两个音节各张口一次",
                    }
                ],
                "performance": [
                    {
                        "actor_slot": 1,
                        "start": 0,
                        "end": 1.4,
                        "core_intent": "害羞而克制的微笑",
                        "visible_evidence": "稍稍低头、短暂回避视线、嘴角含蓄上扬",
                        "exclude": "不是紧张、讽刺或大笑",
                        "confidence": 0.88,
                    }
                ],
            },
            duration=1.4,
        )
        prompt = build_performance_prompt(analysis)
        self.assertIn("核心表演语义为“害羞而克制的微笑”", prompt)
        self.assertIn("外在证据", prompt)
        self.assertIn("排除误读", prompt)
        self.assertLess(prompt.index("害羞而克制的微笑"), prompt.index("稍稍低头"))

    def test_new_voice_prompt_keeps_words_and_timing_but_rejects_original_timbre(self) -> None:
        analysis = normalize_performance_analysis(
            {
                "has_dialogue": True,
                "dialogue": [
                    {
                        "speaker_slot": 1,
                        "start": 0.15,
                        "end": 1.25,
                        "text": "别担心，我会回来。",
                        "delivery": "平静，句中短暂停顿",
                        "mouth_motion": "句中短暂闭唇，末尾下颌回收",
                    }
                ],
                "audio_summary": "先由左侧人物说话，随后安静",
            },
            duration=1.5,
        )
        prompt = build_performance_prompt(
            analysis,
            generate_new_voice=True,
            speaker_roles=["新的女主角"],
            max_chars=2000,
        )
        self.assertIn("别担心，我会回来", prompt)
        self.assertIn("0.15–1.25秒", prompt)
        self.assertIn("新的女主角", prompt)
        self.assertIn("逐音节同步", prompt)
        self.assertIn("严禁模仿、复制或保留原片人物的声纹、音色", prompt)
        self.assertIn("@视频1是完全静音", prompt)
        self.assertIn("结构化时间轴", prompt)
        self.assertIn("不得复用原人物声音", prompt)
        self.assertIn("不得复刻或混入原片对白、BGM、环境音", prompt)
        self.assertIn("其他人物不得串词", prompt)

    def test_no_dialogue_new_voice_prompt_forbids_invented_speech(self) -> None:
        prompt = build_performance_prompt(
            {"has_dialogue": False, "dialogue": [], "performance": []},
            generate_new_voice=True,
        )
        self.assertIn("不得生成任何人物说话声", prompt)

    def test_compact_no_dialogue_prompt_omits_voice_boilerplate_and_wardrobe(self) -> None:
        prompt = build_performance_prompt(
            {
                "has_dialogue": False,
                "dialogue": [],
                "manual_dialogue_text": "",
                "manual_performance_text": (
                    "[0.00–1.93秒] P1：保持凝视；可见证据：位于画面左侧，侧身站立，"
                    "穿着黑色西装和白衬衫；避免误读：没有额外动作"
                ),
            },
            generate_new_voice=True,
            compact=True,
            max_chars=2000,
        )
        self.assertIn("本镜无台词", prompt)
        self.assertIn("位于画面左侧", prompt)
        self.assertIn("侧身站立", prompt)
        self.assertNotIn("台词文字和时序必须复刻", prompt)
        self.assertNotIn("黑色西装", prompt)
        self.assertNotIn("白衬衫", prompt)

    def test_white_model_prompt_removes_wardrobe_but_keeps_motion_and_positions(self) -> None:
        prompt = build_white_model_performance_prompt(
            {
                "has_dialogue": False,
                "dialogue": [],
                "performance": [
                    {
                        "actor_slot": 1,
                        "start": 0,
                        "end": 1.93,
                        "core_intent": "保持深情凝视的姿态，身穿黑色西装与女性互动",
                        "visible_evidence": "位于画面左侧，以侧身姿态站立，穿着黑色西装搭配白色衬衫和黑色领带，面向右侧女性",
                        "exclude": "没有说话，不要改变蕾丝婚纱与珍珠肩带",
                    },
                    {
                        "actor_slot": 2,
                        "start": 0,
                        "end": 1.93,
                        "core_intent": "仰头注视对面的男性",
                        "visible_evidence": "位于画面右侧，穿着带珍珠肩带的蕾丝婚纱，佩戴珍珠耳饰和发饰，保持仰头站立",
                        "exclude": "没有额外肢体动作",
                    },
                ],
            },
            max_chars=3000,
        )
        self.assertIn("位于画面左侧", prompt)
        self.assertIn("侧身姿态站立", prompt)
        self.assertIn("面向右侧女性", prompt)
        self.assertIn("位于画面右侧", prompt)
        self.assertIn("保持仰头站立", prompt)
        self.assertNotIn("黑色西装", prompt)
        self.assertNotIn("白色衬衫", prompt)
        self.assertNotIn("领带", prompt)
        self.assertNotIn("蕾丝婚纱", prompt)
        self.assertNotIn("珍珠肩带", prompt)
        self.assertNotIn("珍珠耳饰", prompt)
        self.assertNotIn("发饰", prompt)

    def test_manual_text_overrides_automatic_dialogue_and_performance(self) -> None:
        prompt = build_performance_prompt(
            {
                "has_dialogue": True,
                "dialogue": [{"speaker_slot": 1, "start": 0, "end": 1, "text": "错误台词"}],
                "performance": [{"actor_slot": 1, "start": 0, "end": 1, "core_intent": "错误表演"}],
                "manual_dialogue_text": "[0.10–0.90秒] P2：人工校订台词",
                "manual_performance_text": "[0.00–1.00秒] P2：克制地观察",
            },
            generate_new_voice=True,
            max_chars=2000,
        )
        self.assertIn("人工校订台词", prompt)
        self.assertIn("克制地观察", prompt)
        self.assertNotIn("错误台词", prompt)
        self.assertNotIn("错误表演", prompt)

    def test_performance_prompt_uses_visual_role_anchor_instead_of_conflicting_slot_role(self) -> None:
        prompt = build_performance_prompt(
            {
                "has_dialogue": True,
                "dialogue": [{"speaker_slot": 1, "start": 0, "end": 0.8, "text": "需要咖啡吗"}],
                "performance": [{
                    "actor_slot": 1,
                    "start": 0,
                    "end": 1.4,
                    "core_intent": "礼貌询问",
                    "visible_evidence": "站在右侧前景，手持咖啡杯",
                }],
            },
            speaker_roles=["错误映射的男人"],
            max_chars=2000,
        )
        self.assertIn("只能由P1（执行“礼貌询问”表演（可见位置与动作：站在右侧前景，手持咖啡杯）的角色）准确说", prompt)
        self.assertNotIn("错误映射的男人", prompt)

    def test_hidden_mouth_keeps_character_speaker_and_bgm_is_not_dialogue(self) -> None:
        analysis = normalize_performance_analysis(
            {
                "dialogue": [
                    {"source_type": "character", "speaker_slot": 1, "start": 0, "end": 0.8, "text": "需要咖啡吗", "mouth_motion": "背对镜头，嘴部不可见"},
                    {"source_type": "bgm_vocal", "speaker_slot": 2, "start": 0, "end": 1.2, "text": "歌曲歌词"},
                ]
            },
            duration=1.2,
            max_people=2,
        )
        self.assertEqual(analysis["dialogue"][0]["speaker_slot"], 1)
        self.assertEqual(analysis["dialogue"][1]["speaker_slot"], 0)
        prompt = build_performance_prompt(analysis, generate_new_voice=True, max_chars=2000)
        self.assertIn("背对、遮挡或嘴部不可见", prompt)
        self.assertIn("是BGM人声，不生成该声音", prompt)

    def test_ultra_compact_prompt_preserves_dialogue_and_fits_budget(self) -> None:
        analysis = normalize_performance_analysis(
            {
                "dialogue": [
                    {"source_type": "character", "speaker_slot": 2, "start": 1.0, "end": 2.8, "text": "ポイントカードはお持ちですか"},
                    {"source_type": "character", "speaker_slot": 2, "start": 5.6, "end": 6.6, "text": "はい、あります"},
                ],
                "performance": [
                    {"actor_slot": 1, "start": 0, "end": 6.6, "core_intent": "面对顾客保持静止", "visible_evidence": "左侧前景只露头肩和背部，保持遮挡关系"},
                    {"actor_slot": 2, "start": 0, "end": 6.6, "core_intent": "询问后取出卡片递出", "visible_evidence": "右侧人物先说话，再低头取卡并向左递出"},
                ],
            },
            duration=6.7,
            max_people=2,
        )
        prompt = build_performance_prompt(
            analysis,
            generate_new_voice=True,
            max_chars=430,
            compact=True,
            ultra_compact=True,
        )
        self.assertLessEqual(len(prompt), 430)
        self.assertIn("ポイントカードはお持ちですか", prompt)
        self.assertIn("はい、あります", prompt)
        self.assertIn("P2", prompt)
        self.assertIn("全新音色", prompt)

    def test_analyzer_uses_enough_frames_for_entry_and_exit_trajectory(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"output_text": '{"has_dialogue":false,"dialogue":[],"performance":[]}'}
        session = Mock()
        session.post.return_value = response
        ArkPerformanceAnalyzer("test-key", session=session).analyze("https://example.com/shot.mp4", duration=1.0)
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["input"][0]["content"][0]["fps"], 5)
        self.assertIn("首帧、中间帧和末帧", body["input"][0]["content"][1]["text"])
        self.assertIn("模糊前景", body["input"][0]["content"][1]["text"])
        self.assertIn("只露头肩/背影/身体局部", body["input"][0]["content"][1]["text"])

    def test_analyzer_sends_video_and_parses_output_text(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "output_text": '{"has_dialogue":false,"dialogue":[],"performance":[{"actor_slot":1,"start":0,"end":1,"core_intent":"专注地观察","visible_evidence":"视线固定","exclude":"不是害怕","confidence":0.9}],"audio_summary":"安静"}'
        }
        session = Mock()
        session.post.return_value = response
        analyzer = ArkPerformanceAnalyzer("test-key", session=session)
        result = analyzer.analyze("https://example.com/shot.mp4", duration=1.0, max_people=1)
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["input"][0]["content"][0]["type"], "input_video")
        self.assertEqual(body["input"][0]["content"][0]["video_url"], "https://example.com/shot.mp4")
        self.assertEqual(result["performance"][0]["core_intent"], "专注地观察")

if __name__ == "__main__":
    unittest.main()
