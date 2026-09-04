from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from long_video_core import (
    _constrain_boundaries,
    _summarize_people_counts,
    _xyxy_to_xywh_boxes,
    cluster_scene_features,
    compose_white_model_scene_control,
    conform_video_duration,
    extend_video_with_trailing_hold,
    mux_prosody_audio,
    pad_video_with_trailing_black,
    separate_dialogue_background,
    slice_audio_track,
    strip_video_audio,
)
from workflow_core import WorkflowError


class LongVideoCoreTests(unittest.TestCase):
    def test_real_scene_control_uses_green_key_and_h264(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            white = directory / "white.mp4"
            scene = directory / "scene.jpg"
            output = directory / "control.mp4"
            white.write_bytes(b"white")
            scene.write_bytes(b"scene")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                Path(command[-1]).write_bytes(b"control")

            with patch("long_video_core.inspect_video", return_value=SimpleNamespace(width=864, height=496)), patch(
                "long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")
            ), patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg) as mock_run:
                result = compose_white_model_scene_control(white, scene, output)
            self.assertEqual(result, output.resolve())
            command = mock_run.call_args.args[0]
            self.assertIn("colorkey=0x00B140", " ".join(command))
            self.assertIn("libx264", command)
            self.assertEqual(output.read_bytes(), b"control")

    def test_audio_stem_separation_is_cached_by_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "reference.mp4"
            source.write_bytes(b"source-video-with-audio")
            output_dir = directory / "audio_stems"

            def run_ffmpeg(command: list[str], _label: str) -> None:
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[-1]).write_bytes(b"extracted-audio")

            def run_demucs(command: list[str], **_kwargs) -> SimpleNamespace:
                stem_dir = output_dir / "demucs" / "htdemucs_ft" / "source_audio"
                stem_dir.mkdir(parents=True, exist_ok=True)
                (stem_dir / "vocals.wav").write_bytes(b"dialogue")
                (stem_dir / "no_vocals.wav").write_bytes(b"background")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg) as mock_ffmpeg,
                patch("long_video_core.subprocess.run", side_effect=run_demucs) as mock_demucs,
            ):
                first = separate_dialogue_background(source, output_dir)
                second = separate_dialogue_background(source, output_dir)

            self.assertEqual(first, second)
            self.assertEqual(first[0].read_bytes(), b"dialogue")
            self.assertEqual(first[1].read_bytes(), b"background")
            mock_ffmpeg.assert_called_once()
            mock_demucs.assert_called_once()

    def test_dialogue_slice_preserves_timing_without_tempo_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "dialogue.wav"
            output = directory / "shot_dialogue.wav"
            source.write_bytes(b"dialogue")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                audio_filter = command[command.index("-af") + 1]
                self.assertIn("atrim=start=5.070000:duration=2.200000", audio_filter)
                self.assertIn("asetpts=PTS-STARTPTS", audio_filter)
                self.assertNotIn("atempo", audio_filter)
                Path(command[-1]).write_bytes(b"slice")

            with (
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg),
            ):
                result = slice_audio_track(source, output, start=5.07, duration=2.2)

            self.assertEqual(result.read_bytes(), b"slice")

    def test_prosody_mux_uses_only_isolated_dialogue_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            video = directory / "white_model.mp4"
            dialogue = directory / "dialogue_timing.wav"
            output = directory / "reference_with_prosody.mp4"
            video.write_bytes(b"video")
            dialogue.write_bytes(b"dialogue")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                self.assertEqual(command[command.index("-map") + 1], "0:v:0")
                self.assertIn("[prosody]", command)
                self.assertNotIn("atempo", command)
                Path(command[-1]).write_bytes(b"muxed")

            with (
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg),
            ):
                result = mux_prosody_audio(video, dialogue, output, target_duration=4.0)

            self.assertEqual(result.read_bytes(), b"muxed")

    def test_yolox_boxes_are_converted_to_xywh_before_opencv_nms(self) -> None:
        boxes = np.array([[10.0, 20.0, 110.0, 220.0], [30.0, 40.0, 80.0, 140.0]])
        self.assertEqual(
            _xyxy_to_xywh_boxes(boxes),
            [[10.0, 20.0, 100.0, 200.0], [30.0, 40.0, 50.0, 100.0]],
        )

    def test_strip_video_audio_copies_only_video_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "white_model_with_original_voice.mp4"
            output = directory / "seedance_silent_reference.mp4"
            source.write_bytes(b"video-and-original-voice")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                self.assertIn("0:v:0", command)
                self.assertIn("copy", command)
                self.assertIn("-an", command)
                self.assertNotIn("0:a", command)
                Path(command[-1]).write_bytes(b"video-only")

            with (
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg) as mock_run,
            ):
                result = strip_video_audio(source, output)

            self.assertEqual(result, output.resolve())
            self.assertEqual(output.read_bytes(), b"video-only")
            mock_run.assert_called_once()

    def test_short_depth_reference_is_padded_with_trailing_black_to_two_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "depth.mp4"
            output = directory / "depth_seedance_2s.mp4"
            source.write_bytes(b"short-depth")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                video_filter = command[command.index("-vf") + 1]
                self.assertIn("setpts=PTS-STARTPTS", video_filter)
                self.assertIn("fps=30", video_filter)
                self.assertIn("tpad=stop_mode=add:stop_duration=2.000000:color=black", video_filter)
                self.assertIn("2.000000", command)
                self.assertEqual(command[command.index("-r") + 1], "30")
                self.assertEqual(command[command.index("-fps_mode") + 1], "cfr")
                Path(command[-1]).write_bytes(b"padded-depth")

            with (
                patch(
                    "long_video_core.inspect_video",
                    side_effect=[
                        SimpleNamespace(duration=1.24, fps=9558.86),
                        SimpleNamespace(duration=2.0, fps=30.0),
                    ],
                ),
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg) as mock_run,
            ):
                result = pad_video_with_trailing_black(source, output)

            self.assertEqual(result, output.resolve())
            self.assertEqual(output.read_bytes(), b"padded-depth")
            mock_run.assert_called_once()

    def test_depth_reference_at_least_two_seconds_is_not_reencoded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "depth.mp4"
            source.write_bytes(b"long-enough")
            with (
                patch("long_video_core.inspect_video", return_value=SimpleNamespace(duration=2.0)),
                patch("long_video_core._run_ffmpeg") as mock_run,
            ):
                result = pad_video_with_trailing_black(source, Path(temp_dir) / "unused.mp4")
            self.assertEqual(result, source.resolve())
            mock_run.assert_not_called()

    def test_duration_conform_trims_without_speed_change_at_constant_thirty_fps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "generated.mp4"
            output = directory / "conformed.mp4"
            source.write_bytes(b"generated-video")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                video_filter = command[command.index("-vf") + 1]
                self.assertIn("trim=duration=1.000000", video_filter)
                self.assertIn("setpts=PTS-STARTPTS", video_filter)
                self.assertIn("fps=30", video_filter)
                self.assertIn("tpad=stop_mode=clone", video_filter)
                self.assertNotIn("*PTS", video_filter)
                self.assertEqual(command[command.index("-r") + 1], "30")
                self.assertEqual(command[command.index("-fps_mode") + 1], "cfr")
                Path(command[-1]).write_bytes(b"conformed-video")

            with (
                patch("long_video_core.inspect_video", return_value=SimpleNamespace(duration=4.0)),
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg) as mock_run,
            ):
                result = conform_video_duration(source, output, 1.0)

            self.assertEqual(result, output.resolve())
            self.assertEqual(output.read_bytes(), b"conformed-video")
            mock_run.assert_called_once()

    def test_short_reference_is_extended_by_holding_last_frame_without_retiming(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "white_model.mp4"
            output = directory / "white_model_seedance_timed.mp4"
            source.write_bytes(b"short-white-model")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                video_filter = command[command.index("-vf") + 1]
                self.assertIn("setpts=PTS-STARTPTS", video_filter)
                self.assertIn("fps=30", video_filter)
                self.assertIn("tpad=stop_mode=clone:stop_duration=4.000000", video_filter)
                self.assertNotIn("stop_mode=add", video_filter)
                self.assertEqual(command[command.index("-t") + 1], "4.000000")
                Path(command[-1]).write_bytes(b"held-reference")

            with (
                patch(
                    "long_video_core.inspect_video",
                    side_effect=[
                        SimpleNamespace(duration=0.467, fps=30.0),
                        SimpleNamespace(duration=4.0, fps=30.0),
                    ],
                ),
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg) as mock_run,
            ):
                result = extend_video_with_trailing_hold(source, output, target_duration=4.0)

            self.assertEqual(result, output.resolve())
            self.assertEqual(output.read_bytes(), b"held-reference")
            mock_run.assert_called_once()

    def test_short_reference_can_preserve_original_prosody_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "white_model_with_original_audio.mp4"
            output = directory / "white_model_seedance_timed.mp4"
            source.write_bytes(b"short-white-model-with-audio")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                self.assertIn("0:a:0?", command)
                self.assertNotIn("-an", command)
                audio_filter = command[command.index("-af") + 1]
                self.assertIn("apad=pad_dur=4.000000", audio_filter)
                self.assertEqual(command[command.index("-c:a") + 1], "aac")
                Path(command[-1]).write_bytes(b"held-reference-with-audio")

            with (
                patch(
                    "long_video_core.inspect_video",
                    side_effect=[
                        SimpleNamespace(duration=2.2, fps=30.0),
                        SimpleNamespace(duration=4.0, fps=30.0),
                    ],
                ),
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg),
            ):
                result = extend_video_with_trailing_hold(
                    source,
                    output,
                    target_duration=4.0,
                    with_audio=True,
                )

            self.assertEqual(result, output.resolve())
            self.assertEqual(output.read_bytes(), b"held-reference-with-audio")

    def test_duration_conform_trims_audio_without_tempo_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "generated_with_new_voice.mp4"
            output = directory / "conformed_with_new_voice.mp4"
            source.write_bytes(b"generated-video-and-audio")

            def run_ffmpeg(command: list[str], _label: str) -> None:
                filters = command[command.index("-filter_complex") + 1]
                self.assertIn("trim=duration=1.500000", filters)
                self.assertIn("atrim=duration=1.500000", filters)
                self.assertIn("asetpts=PTS-STARTPTS", filters)
                self.assertIn("apad=pad_dur=1.500000", filters)
                self.assertNotIn("atempo", filters)
                self.assertNotIn("*PTS", filters)
                Path(command[-1]).write_bytes(b"trimmed-video-and-audio")

            with (
                patch("long_video_core.inspect_video", return_value=SimpleNamespace(duration=4.0)),
                patch("long_video_core.resolve_ffmpeg", return_value=Path("ffmpeg")),
                patch("long_video_core._run_ffmpeg", side_effect=run_ffmpeg) as mock_run,
            ):
                result = conform_video_duration(source, output, 1.5, with_audio=True)

            self.assertEqual(result, output.resolve())
            self.assertEqual(output.read_bytes(), b"trimmed-video-and-audio")
            mock_run.assert_called_once()

    def test_scene_features_group_non_adjacent_matching_backgrounds(self) -> None:
        room = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        room_variant = np.array([0.98, 0.04, 0.0], dtype=np.float32)
        street = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        labels = cluster_scene_features([room, room_variant, street, room], threshold=0.1)
        self.assertEqual(labels, [1, 1, 2, 1])

    def test_people_suggestion_uses_maximum_across_shot_samples(self) -> None:
        suggestion = _summarize_people_counts([1, 2, 2], max_people=4)
        self.assertEqual(suggestion["suggested_actor_count"], 2)
        self.assertEqual(suggestion["detection_confidence"], "medium")
        self.assertEqual(suggestion["sample_actor_counts"], [1, 2, 2])

    def test_people_suggestion_allows_empty_shot_but_marks_low_confidence(self) -> None:
        suggestion = _summarize_people_counts([0, 0, 0], max_people=4)
        self.assertEqual(suggestion["suggested_actor_count"], 0)
        self.assertEqual(suggestion["detection_confidence"], "low")

    def test_short_detected_shots_are_preserved(self) -> None:
        cuts = _constrain_boundaries(
            [1.0, 4.5, 5.2, 10.0],
            14.0,
            min_shot_seconds=4.0,
            max_shot_seconds=14.5,
            max_shots=24,
        )
        self.assertEqual(cuts, [1.0, 4.5, 5.2, 10.0])

    def test_long_shot_is_split_to_seedance_safe_segments(self) -> None:
        cuts = _constrain_boundaries(
            [],
            34.0,
            min_shot_seconds=4.0,
            max_shot_seconds=14.5,
            max_shots=24,
        )
        points = [0.0, *cuts, 34.0]
        durations = [points[index + 1] - points[index] for index in range(len(points) - 1)]
        self.assertTrue(all(0.0 < value <= 14.5 for value in durations))

    def test_maximum_shot_count_never_silently_drops_real_cuts(self) -> None:
        with self.assertRaises(WorkflowError):
            _constrain_boundaries(
                [float(value) for value in range(4, 80, 4)],
                80.0,
                min_shot_seconds=4.0,
                max_shot_seconds=14.5,
                max_shots=6,
            )

    def test_cut_near_start_and_end_is_preserved(self) -> None:
        cuts = _constrain_boundaries(
            [0.1, 9.95],
            10.0,
            min_shot_seconds=4.0,
            max_shot_seconds=14.5,
            max_shots=24,
        )
        self.assertEqual(cuts, [0.1, 9.95])


if __name__ == "__main__":
    unittest.main()
