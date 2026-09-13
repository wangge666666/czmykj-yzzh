from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

import web_app


class WardrobeSwapTests(unittest.TestCase):
    def setUp(self) -> None:
        web_app.app.config.update(TESTING=True)
        self.client = web_app.app.test_client()

    @staticmethod
    def image_bytes(width: int = 640, height: int = 480) -> bytes:
        image = np.full((height, width, 3), 245, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise AssertionError("无法创建测试图片")
        return encoded.tobytes()

    def test_unified_page_exposes_four_modes_white_model_and_seedance_25(self) -> None:
        response = self.client.get("/projects/wardrobe")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("衣装智换", html)
        self.assertIn('data-mode="person"', html)
        self.assertIn('data-mode="scene"', html)
        self.assertIn('data-mode="clothing"', html)
        self.assertIn('data-mode="custom"', html)
        self.assertIn('id="customReferencePicker"', html)
        self.assertIn('name="customPersonChoice"', html)
        self.assertIn('name="customClothingChoice"', html)
        self.assertIn('name="customSceneChoice"', html)
        self.assertIn("打码、白膜与素材提取", html)
        self.assertIn('id="mosaicBtn"', html)
        self.assertIn('id="whiteModelBtn"', html)
        self.assertIn('id="extractReferencesBtn"', html)
        self.assertIn("Seedance 2.0 · 480p", html)
        self.assertIn("Seedance 2.5", html)
        self.assertIn('id="resolution"', html)
        self.assertIn("/static/character_library.js", html)
        self.assertIn("character_library.js?v=20260902-1", html)
        self.assertIn("/static/wardrobe.js?v=20260903-1", html)
        self.assertIn("原片人物三视图（白 T 短裤）", html)
        self.assertIn('id="preparedPersonImage"', html)
        self.assertIn('id="submitPreparedPerson"', html)
        self.assertIn('id="preparedPersonGroup"', html)
        self.assertIn('id="retryPersonExtraction"', html)
        response.close()

    def test_wardrobe_character_library_refreshes_processing_assets(self) -> None:
        response = self.client.get("/static/character_library.js")
        self.assertEqual(response.status_code, 200)
        script = response.get_data(as_text=True)
        self.assertIn('asset.status === "Processing"', script)
        self.assertIn('cache: "no-store"', script)
        self.assertIn("正在向火山查询该人物的最新审核状态", script)
        response.close()

    def test_restored_failed_job_does_not_replay_historical_error_toast(self) -> None:
        response = self.client.get("/static/wardrobe.js")
        self.assertEqual(response.status_code, 200)
        script = response.get_data(as_text=True)
        self.assertIn("terminalNoticeKeys: new Set()", script)
        self.assertIn("if (restoring) state.terminalNoticeKeys.add(terminalNoticeKey)", script)
        self.assertIn("历史任务", script)
        self.assertIn("window.confirm", script)
        self.assertIn("job.person_revision", script)
        self.assertIn("job.person_reference_current", script)
        self.assertIn("旧版需按白 T 短裤规则重绘", script)
        self.assertIn("restoreLatestMode(state.mode)", script)
        self.assertIn("/api/wardrobe-swap/latest?mode=", script)
        response.close()

    def test_latest_endpoint_restores_prepare_job_for_requested_mode(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            jobs = []
            for job_id, mode, created_at, directory in (
                ("wardrobe-latest-person", "person", "9998-01-01T00:00:00", first_dir),
                ("wardrobe-latest-clothing", "clothing", "9999-01-01T00:00:00", second_dir),
            ):
                root = Path(directory)
                mosaic = root / "face_mosaic.mp4"
                white = root / "white_model.mp4"
                mosaic.write_bytes(b"mosaic")
                white.write_bytes(b"white")
                job = web_app.WebJob(
                    id=job_id,
                    kind=f"wardrobe_prepare_{mode}",
                    project=web_app.WARDROBE_SWAP_PROJECT,
                    run_dir=root,
                    status="succeeded",
                    mosaic_path=mosaic,
                    white_model_path=white,
                    created_at=created_at,
                )
                jobs.append(job)
            with web_app.JOBS_LOCK:
                for job in jobs:
                    web_app.JOBS[job.id] = job
            try:
                response = self.client.get("/api/wardrobe-swap/latest?mode=person")
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["id"], "wardrobe-latest-person")
                self.assertEqual(payload["kind"], "wardrobe_prepare_person")
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    for job in jobs:
                        web_app.JOBS.pop(job.id, None)

    def test_historical_final_video_is_relinked_to_prepare_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "web_app.PROJECT_DIR", Path(temp_dir)
        ):
            runs = Path(temp_dir) / "runs"
            prepare_dir = runs / "20260901_web_wardrobe_prepare_clothing_source"
            generate_dir = runs / "20260902_web_wardrobe_generate_clothing_result"
            prepare_dir.mkdir(parents=True)
            generate_dir.mkdir(parents=True)
            scene = prepare_dir / "scene_reference.jpg"
            output = generate_dir / "生成成片_test.mp4"
            scene.write_bytes(self.image_bytes())
            output.write_bytes(b"final-video")
            (generate_dir / "job.json").write_text(
                json.dumps(
                    {
                        "status": "succeeded",
                        "created_at": "2026-09-02T12:00:00",
                        "scene": str(scene),
                        "output": str(output),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            source = web_app.WebJob(
                id="wardrobe-historical-source",
                kind="wardrobe_prepare_clothing",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=prepare_dir,
                scene_path=scene,
            )

            restored = web_app.restore_wardrobe_output_link(source, "clothing")

            self.assertTrue(restored)
            self.assertEqual(source.output_path, output.resolve())
            self.assertTrue(any("重新关联最终成片" in line for line in source.logs))

    def test_generation_result_is_written_back_to_prepare_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "web_app.PROJECT_DIR", Path(temp_dir)
        ):
            runs = Path(temp_dir) / "runs"
            source_dir = runs / "20260901_web_wardrobe_prepare_clothing_source"
            result_dir = runs / "20260902_web_wardrobe_generate_clothing_result"
            source_dir.mkdir(parents=True)
            result_dir.mkdir(parents=True)
            source_video = source_dir / "reference.mp4"
            output = result_dir / "生成成片_test.mp4"
            source_video.write_bytes(b"source")
            output.write_bytes(b"final")
            source = web_app.WebJob(
                id="wardrobe-persistent-source",
                kind="wardrobe_prepare_clothing",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=source_dir,
                status="succeeded",
            )
            generated = web_app.WebJob(
                id="wardrobe-persistent-result",
                kind="wardrobe_generate_clothing",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=result_dir,
                status="succeeded",
                output_path=output,
            )

            web_app.persist_wardrobe_generation_result(generated, source, "clothing")

            self.assertEqual(source.output_path, output)
            source_record = json.loads((source_dir / "wardrobe_manifest.json").read_text(encoding="utf-8"))
            result_record = json.loads((result_dir / "wardrobe_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(source_record["output"], str(output))
            self.assertEqual(result_record["source_job_id"], source.id)
            self.assertIn("可随时恢复预览和下载", source_record["stage"])

    def test_job_public_person_revision_changes_with_extracted_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            person = Path(temp_dir) / "original_person_triview.jpg"
            person.write_bytes(self.image_bytes())
            job = web_app.WebJob(
                id="wardrobe-person-revision",
                kind="wardrobe_prepare_clothing",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=Path(temp_dir),
                person_path=person,
            )

            payload = job.public()

            self.assertGreater(payload["person_revision"], 0)
            self.assertFalse(payload["person_reference_current"])
            (Path(temp_dir) / "original_person_triview.version").write_text(
                web_app.WARDROBE_NEUTRAL_PERSON_TRIVIEW_VERSION,
                encoding="utf-8",
            )
            self.assertTrue(job.public()["person_reference_current"])

    def test_mode_prompts_keep_white_model_and_three_image_roles_separate(self) -> None:
        person = web_app.build_wardrobe_swap_prompt("person")
        scene = web_app.build_wardrobe_swap_prompt("scene")
        clothing = web_app.build_wardrobe_swap_prompt("clothing")
        custom = web_app.build_wardrobe_swap_prompt("custom")
        for prompt in (person, scene, clothing, custom):
            self.assertIn("@视频1是唯一动作与镜头母版", prompt)
            self.assertIn("@图片1", prompt)
            self.assertIn("@图片2", prompt)
            self.assertIn("@图片3", prompt)
            self.assertIn("禁止生成字幕", prompt)
        self.assertIn("唯一新人物身份依据", person)
        self.assertIn("唯一新场景依据", scene)
        self.assertIn("唯一新服装依据", clothing)
        self.assertIn("完成随心换", custom)
        self.assertIn("用户最终选定的唯一人物", custom)
        self.assertIn("用户最终选定的唯一服装", custom)
        self.assertIn("用户最终选定的唯一场景", custom)

    def test_wardrobe_person_triview_uses_white_tshirt_and_shorts(self) -> None:
        prompt = web_app.WARDROBE_NEUTRAL_PERSON_TRIVIEW_PROMPT
        constraint = web_app.WARDROBE_NEUTRAL_PERSON_TRIVIEW_REQUIRED_CONSTRAINT
        self.assertIn("纯白色", prompt)
        self.assertIn("短袖圆领T恤", prompt)
        self.assertIn("纯白色无图案短裤", constraint)
        self.assertIn("绝对禁止保留", constraint)
        self.assertNotIn("原始服装一致", prompt)

    @patch("web_app.conform_video_duration")
    @patch("web_app.inspect_video")
    def test_source_over_15_seconds_is_trimmed_before_all_processing(
        self, mock_inspect: Mock, mock_conform: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            clipped = root / "reference_first_15s.mp4"
            source.write_bytes(b"source")
            clipped.write_bytes(b"clipped")
            mock_inspect.side_effect = [Mock(duration=21.4), Mock(duration=15.0)]
            mock_conform.return_value = clipped
            job = web_app.WebJob(id="trim", kind="wardrobe_prepare_person", run_dir=root)

            result = web_app.normalize_wardrobe_source_duration(job, source)

            self.assertEqual(result, clipped)
            mock_conform.assert_called_once_with(
                source,
                root / "reference_first_15s.mp4",
                web_app.WARDROBE_MAX_SOURCE_SECONDS,
                with_audio=False,
            )
            self.assertTrue(any("后续打码、白膜、素材提取和成片" in line for line in job.logs))

    @patch("web_app.conform_video_duration")
    @patch("web_app.inspect_video")
    def test_source_at_or_below_15_seconds_is_not_trimmed(
        self, mock_inspect: Mock, mock_conform: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(b"source")
            mock_inspect.return_value = Mock(duration=14.8)
            job = web_app.WebJob(id="keep", kind="wardrobe_prepare_person", run_dir=Path(temp_dir))

            self.assertEqual(web_app.normalize_wardrobe_source_duration(job, source), source)
            mock_conform.assert_not_called()

    @patch("web_app.resolved_seedance_ratio", return_value="9:16")
    def test_white_model_uses_seedance_20_480p_with_fixed_controls(
        self, _mock_ratio: Mock
    ) -> None:
        options = web_app.build_wardrobe_white_model_options(Path("source.mp4"), 5.2)
        self.assertEqual(options["model"], web_app.DEFAULT_SEEDANCE_MODEL)
        self.assertEqual(options["resolution"], "480p")
        self.assertEqual(options["ratio"], "9:16")
        self.assertEqual(options["duration"], 6)
        self.assertFalse(options["generate_audio"])
        self.assertEqual(options["reference_upload_strategy"], "stable")
        self.assertIn("结束姿势定格", options["prompt"])
        self.assertIn("非写实的纯白三维动画人偶", options["prompt"])
        self.assertIn("宽松、不透明的纯白长袖长裤连体工作服", options["prompt"])
        self.assertNotIn("裸露感", options["prompt"])
        self.assertNotIn("解剖细节", options["prompt"])
        self.assertNotEqual(options["prompt"], web_app.DEFAULT_WHITE_MODEL_PROMPT)

    def test_overwide_wardrobe_reference_is_padded_without_cropping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "wide-clothing.png"
            source.write_bytes(self.image_bytes(900, 300))

            result = web_app.conform_wardrobe_seedance_reference_image(
                source,
                root / "conformed.jpg",
            )

            self.assertTrue(result["changed"])
            self.assertEqual((result["original_width"], result["original_height"]), (900, 300))
            self.assertEqual((result["width"], result["height"]), (900, 360))
            conformed = web_app._read_reference_image(Path(result["path"]))
            self.assertIsNotNone(conformed)
            self.assertEqual(conformed.shape[:2], (360, 900))
            # The original image remains intact; conformance creates a paid-request copy.
            original = web_app._read_reference_image(source)
            self.assertEqual(original.shape[:2], (300, 900))

    def test_stable_video_reference_prefers_tos_when_fully_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "white.mp4"
            source.write_bytes(b"video")
            tos = Mock()
            tos.upload_video.return_value = Mock(
                signed_url="https://tos.example/white.mp4",
                object_key="seedance-inputs/white.mp4",
            )
            tos_factory = Mock(return_value=tos)
            tos_factory.configured.return_value = True
            with patch.object(web_app, "TosMediaStore", tos_factory):
                reference = web_app.prepare_seedance_stable_video_reference(source)

            self.assertEqual(reference.channel, "tos")
            self.assertEqual(reference.url, "https://tos.example/white.mp4")
            tos.upload_video.assert_called_once_with(source)
            reference.close(delete_remote=True)
            tos.delete.assert_called_once_with("seedance-inputs/white.mp4")

    def test_stable_video_reference_uses_project_tunnel_without_tos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "white.mp4"
            source.write_bytes(b"video")
            video_server = Mock()
            video_server.local_origin = "http://127.0.0.1:45678"
            video_server.route_path = "/media/token/depth.mp4"
            video_server_factory = Mock(return_value=video_server)
            tunnel = Mock()
            tunnel.start.return_value = "https://wardrobe.trycloudflare.com"
            tunnel_factory = Mock(return_value=tunnel)
            tos_factory = Mock()
            tos_factory.configured.return_value = False
            with (
                patch.object(web_app, "TosMediaStore", tos_factory),
                patch.object(web_app, "TemporaryVideoServer", video_server_factory),
                patch.object(web_app, "TemporaryPublicTunnel", tunnel_factory),
            ):
                reference = web_app.prepare_seedance_stable_video_reference(source)

            self.assertEqual(reference.channel, "project_tunnel")
            self.assertEqual(
                reference.url,
                "https://wardrobe.trycloudflare.com/media/token/depth.mp4",
            )
            video_server.start.assert_called_once_with()
            tunnel.wait_until_reachable.assert_called_once_with(reference.url)
            reference.close()
            tunnel.close.assert_called_once_with()
            video_server.close.assert_called_once_with()

    def test_stable_video_reference_falls_back_to_temporary_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "white.mp4"
            source.write_bytes(b"video")
            video_server = Mock()
            video_server.local_origin = "http://127.0.0.1:45678"
            video_server_factory = Mock(return_value=video_server)
            tunnel = Mock()
            tunnel.start.side_effect = ConnectionAbortedError("tunnel unavailable")
            tunnel_factory = Mock(return_value=tunnel)
            tos_factory = Mock()
            tos_factory.configured.return_value = False
            temporary_store = Mock()
            temporary_store.upload_video.return_value = Mock(
                signed_url="https://temporary.example/white.mp4",
                object_key="temporary-file-id",
            )
            temporary_factory = Mock(return_value=temporary_store)
            with (
                patch.object(web_app, "TosMediaStore", tos_factory),
                patch.object(web_app, "TemporaryVideoServer", video_server_factory),
                patch.object(web_app, "TemporaryPublicTunnel", tunnel_factory),
                patch.object(web_app, "TempFileMediaStore", temporary_factory),
            ):
                reference = web_app.prepare_seedance_stable_video_reference(source)

            self.assertEqual(reference.channel, "temporary")
            self.assertEqual(reference.url, "https://temporary.example/white.mp4")
            temporary_store.upload_video.assert_called_once_with(source, expires_hours=1)
            reference.close()
            temporary_store.delete.assert_called_once_with("temporary-file-id")
            tunnel.close.assert_called_once_with()
            video_server.close.assert_called_once_with()

    @patch("web_app.threading.Thread")
    def test_prepare_starts_one_unified_mode_job(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "web_app.timestamped_run_dir", return_value=Path(temp_dir)
        ):
            response = self.client.post(
                "/api/wardrobe-swap/prepare",
                data={
                    "mode": "person",
                    "reference_video": (io.BytesIO(b"video"), "source.mp4"),
                    "original_scene_image": (io.BytesIO(b"scene"), "scene.png"),
                    "original_clothing_image": (io.BytesIO(b"clothing"), "clothing.png"),
                    "extract_missing": "true",
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 202)
            payload = response.get_json()
            self.assertEqual(payload["project"], web_app.WARDROBE_SWAP_PROJECT)
            self.assertEqual(payload["kind"], "wardrobe_prepare_person")
            mock_thread.return_value.start.assert_called_once()
            with web_app.JOBS_LOCK:
                web_app.JOBS.pop(payload["id"], None)
            response.close()

    @patch("web_app.threading.Thread")
    def test_mosaic_stage_creates_only_one_prepare_job(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "web_app.timestamped_run_dir", return_value=Path(temp_dir)
        ):
            response = self.client.post(
                "/api/wardrobe-swap/mosaic",
                data={
                    "mode": "person",
                    "reference_video": (io.BytesIO(b"video"), "source.mp4"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 202)
            payload = response.get_json()
            self.assertEqual(payload["kind"], "wardrobe_prepare_person")
            self.assertIn("wardrobe-mosaic-person", mock_thread.call_args.kwargs["name"])
            mock_thread.return_value.start.assert_called_once()
            with web_app.JOBS_LOCK:
                web_app.JOBS.pop(payload["id"], None)
            response.close()

    @patch("web_app.threading.Thread")
    def test_white_model_stage_reuses_existing_mosaic_job(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = root / "reference.mp4"
            mosaic = root / "face_mosaic.mp4"
            reference.write_bytes(b"video")
            mosaic.write_bytes(b"mosaic")
            source = web_app.WebJob(
                id="wardrobe-split-white",
                kind="wardrobe_prepare_person",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
                status="succeeded",
                mosaic_path=mosaic,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                response = self.client.post(
                    "/api/wardrobe-swap/white-model",
                    data={"source_job_id": source.id},
                )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertEqual(payload["id"], source.id)
                self.assertEqual(payload["status"], "running")
                self.assertIn("wardrobe-white-person", mock_thread.call_args.kwargs["name"])
                mock_thread.return_value.start.assert_called_once()
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    @patch("web_app.threading.Thread")
    def test_reference_extraction_stage_does_not_regenerate_white_model(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = root / "reference.mp4"
            mosaic = root / "face_mosaic.mp4"
            white = root / "white_model.mp4"
            scene = root / "original_scene.png"
            clothing = root / "original_clothing.png"
            for path in (reference, mosaic, white):
                path.write_bytes(b"video")
            scene.write_bytes(self.image_bytes())
            clothing.write_bytes(self.image_bytes())
            source = web_app.WebJob(
                id="wardrobe-split-extract",
                kind="wardrobe_prepare_person",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
                status="succeeded",
                mosaic_path=mosaic,
                white_model_path=white,
                scene_path=scene,
                clothing_path=clothing,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                response = self.client.post(
                    "/api/wardrobe-swap/extract-references",
                    data={"source_job_id": source.id, "extract_missing": "false"},
                )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertEqual(payload["id"], source.id)
                self.assertIn("wardrobe-extract-person", mock_thread.call_args.kwargs["name"])
                mock_thread.return_value.start.assert_called_once()
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    @patch("web_app.threading.Thread")
    def test_custom_prepare_always_accepts_source_and_extracts_all_references(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "web_app.timestamped_run_dir", return_value=Path(temp_dir)
        ):
            response = self.client.post(
                "/api/wardrobe-swap/prepare",
                data={
                    "mode": "custom",
                    "reference_video": (io.BytesIO(b"video"), "source.mp4"),
                    # Even an old UI submitting false cannot disable the mandatory
                    # three-way source extraction of 随心换.
                    "extract_missing": "false",
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 202)
            payload = response.get_json()
            self.assertEqual(payload["kind"], "wardrobe_prepare_custom")
            mock_thread.return_value.start.assert_called_once()
            with web_app.JOBS_LOCK:
                web_app.JOBS.pop(payload["id"], None)
            response.close()

    @patch("web_app.threading.Thread")
    def test_scene_generation_forces_seedance_25_and_selected_resolution(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            source_root = Path(source_dir)
            white = source_root / "white_model.mp4"
            mosaic = source_root / "face_mosaic.mp4"
            clothing = source_root / "original_clothing.png"
            for path in (white, mosaic):
                path.write_bytes(b"artifact")
            clothing.write_bytes(self.image_bytes())
            source = web_app.WebJob(
                id="wardrobe-src",
                kind="wardrobe_prepare_scene",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=source_root,
                status="succeeded",
                white_model_path=white,
                mosaic_path=mosaic,
                clothing_path=clothing,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                with patch("web_app.timestamped_run_dir", return_value=Path(output_dir)):
                    response = self.client.post(
                        "/api/wardrobe-swap/generate",
                        data={
                            "source_job_id": source.id,
                            "mode": "scene",
                            "person_asset": "asset://asset-abcdef123",
                            "resolution": "480p",
                            "new_scene_image": (io.BytesIO(self.image_bytes()), "new-scene.png"),
                        },
                        content_type="multipart/form-data",
                    )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertEqual(payload["kind"], "wardrobe_generate_scene")
                kwargs = mock_thread.call_args.kwargs["kwargs"]
                self.assertEqual(kwargs["options"]["model"], web_app.DEFAULT_SEEDANCE_25_MODEL)
                self.assertEqual(kwargs["options"]["resolution"], "480p")
                self.assertEqual(kwargs["options"]["ratio"], "adaptive")
                self.assertEqual(kwargs["options"]["duration"], -1)
                self.assertEqual(kwargs["options"]["reference_upload_strategy"], "stable")
                self.assertEqual(kwargs["person_source"], "asset://asset-abcdef123")
                self.assertEqual(kwargs["depth_path"], white)
                self.assertEqual(Path(kwargs["clothing_source"]).resolve(), clothing.resolve())
                self.assertTrue(Path(kwargs["scene_source"]).is_file())
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(payload["id"], None)
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    @patch("web_app.threading.Thread")
    def test_custom_generation_can_use_three_original_extractions_without_role_asset(
        self, mock_thread: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            source_root = Path(source_dir)
            white = source_root / "white_model.mp4"
            mosaic = source_root / "face_mosaic.mp4"
            person = source_root / "original_person.png"
            clothing = source_root / "original_clothing.png"
            scene = source_root / "original_scene.png"
            for path in (white, mosaic):
                path.write_bytes(b"artifact")
            for path in (person, clothing, scene):
                path.write_bytes(self.image_bytes())
            source = web_app.WebJob(
                id="wardrobe-custom-src",
                kind="wardrobe_prepare_custom",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=source_root,
                status="succeeded",
                white_model_path=white,
                mosaic_path=mosaic,
                person_path=person,
                clothing_path=clothing,
                scene_path=scene,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                with patch("web_app.timestamped_run_dir", return_value=Path(output_dir)):
                    response = self.client.post(
                        "/api/wardrobe-swap/generate",
                        data={
                            "source_job_id": source.id,
                            "mode": "custom",
                            "resolution": "720p",
                            "custom_person_choice": "original",
                            "custom_clothing_choice": "original",
                            "custom_scene_choice": "original",
                        },
                        content_type="multipart/form-data",
                    )
                self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
                payload = response.get_json()
                self.assertEqual(payload["kind"], "wardrobe_generate_custom")
                kwargs = mock_thread.call_args.kwargs["kwargs"]
                self.assertEqual(Path(kwargs["person_source"]).resolve(), person.resolve())
                self.assertEqual(Path(kwargs["clothing_source"]).resolve(), clothing.resolve())
                self.assertEqual(Path(kwargs["scene_source"]).resolve(), scene.resolve())
                self.assertIn("完成随心换", kwargs["options"]["prompt"])
                self.assertEqual(kwargs["options"]["model"], web_app.DEFAULT_SEEDANCE_25_MODEL)
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(payload["id"], None)
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    @patch("web_app.threading.Thread")
    def test_custom_generation_accepts_independent_uploaded_person_reference(
        self, mock_thread: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            source_root = Path(source_dir)
            white = source_root / "white_model.mp4"
            person = source_root / "original_person.png"
            clothing = source_root / "original_clothing.png"
            scene = source_root / "original_scene.png"
            white.write_bytes(b"artifact")
            for path in (person, clothing, scene):
                path.write_bytes(self.image_bytes())
            source = web_app.WebJob(
                id="wardrobe-custom-upload-src",
                kind="wardrobe_prepare_custom",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=source_root,
                status="succeeded",
                white_model_path=white,
                person_path=person,
                clothing_path=clothing,
                scene_path=scene,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                with patch("web_app.timestamped_run_dir", return_value=Path(output_dir)):
                    response = self.client.post(
                        "/api/wardrobe-swap/generate",
                        data={
                            "source_job_id": source.id,
                            "mode": "custom",
                            "resolution": "480p",
                            "custom_person_choice": "upload",
                            "custom_person_image": (io.BytesIO(self.image_bytes()), "replacement-person.png"),
                            "custom_clothing_choice": "original",
                            "custom_scene_choice": "original",
                        },
                        content_type="multipart/form-data",
                    )
                self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
                payload = response.get_json()
                kwargs = mock_thread.call_args.kwargs["kwargs"]
                self.assertNotEqual(Path(kwargs["person_source"]).resolve(), person.resolve())
                self.assertTrue(Path(kwargs["person_source"]).is_file())
                self.assertEqual(Path(kwargs["clothing_source"]).resolve(), clothing.resolve())
                self.assertEqual(Path(kwargs["scene_source"]).resolve(), scene.resolve())
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(payload["id"], None)
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    @patch("web_app.threading.Thread")
    def test_recover_white_model_reuses_existing_task_without_resubmission(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mosaic = root / "face_mosaic.mp4"
            mosaic.write_bytes(b"mosaic")
            white_dir = root / "white_model_task"
            white_dir.mkdir()
            (white_dir / "job.json").write_text(
                '{"task_id":"cgt-existing","cloud_status":"succeeded","status":"failed"}',
                encoding="utf-8",
            )
            parent = web_app.WebJob(
                id="wardrobe-recover",
                kind="wardrobe_prepare_person",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
                status="failed",
                mosaic_path=mosaic,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[parent.id] = parent
            try:
                response = self.client.post(
                    "/api/wardrobe-swap/recover-white-model",
                    data={"source_job_id": parent.id},
                )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.get_json()["status"], "running")
                args = mock_thread.call_args.kwargs["args"]
                self.assertIs(args[0], parent)
                self.assertEqual(args[1], "person")
                self.assertEqual(
                    mock_thread.call_args.kwargs["target"],
                    web_app.resume_wardrobe_prepare_after_white_download,
                )
                self.assertEqual(web_app.JOBS[f"{parent.id}-white"].task_id, "cgt-existing")
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(parent.id, None)
                    web_app.JOBS.pop(f"{parent.id}-white", None)

    def test_failed_white_cloud_task_never_becomes_download_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            white_dir = root / "white_model_task"
            white_dir.mkdir()
            (white_dir / "job.json").write_text(
                json.dumps(
                    {
                        "task_id": "cgt-failed",
                        "status": "failed",
                        "cloud_status": "failed",
                        "error": "Seedance output review failed",
                    }
                ),
                encoding="utf-8",
            )
            parent = web_app.WebJob(
                id="wardrobe-failed",
                kind="wardrobe_prepare_person",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
                status="running",
                recovery_action="resume_wardrobe_white_download",
            )

            web_app.reconcile_wardrobe_white_task_state(parent, allow_network=False)

            self.assertEqual(parent.status, "failed")
            self.assertEqual(parent.recovery_action, "")
            self.assertIn("output review failed", parent.error)

    def test_running_local_mosaic_is_not_misclassified_as_missing_white_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = web_app.WebJob(
                id="wardrobe-mosaic-running",
                kind="wardrobe_prepare_person",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=Path(temp_dir),
                status="running",
                stage="正在生成人脸打码视频 · 当前最多 1 张脸",
                progress=42,
            )

            web_app.reconcile_wardrobe_white_task_state(parent, allow_network=False)

            self.assertEqual(parent.status, "running")
            self.assertEqual(parent.error, "")
            self.assertEqual(parent.stage, "正在生成人脸打码视频 · 当前最多 1 张脸")

    def test_new_submitting_record_clears_stale_cloud_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "job.json").write_text(
                '{"status":"failed","error":"old review failure"}',
                encoding="utf-8",
            )
            job = web_app.WebJob(
                id="wardrobe-new-submit",
                kind="wardrobe_white_model",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
            )

            web_app.persist_cloud_job(job, status="submitting", error="")
            record = json.loads((root / "job.json").read_text(encoding="utf-8"))

            self.assertEqual(record["status"], "submitting")
            self.assertEqual(record["error"], "")

    @patch("web_app.run_person_triview_extraction")
    @patch("web_app.threading.Thread")
    def test_scene_extraction_still_extracts_source_person_when_asset_is_selected(
        self, mock_thread: Mock, mock_extract_person: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = root / "reference_first_15s.mp4"
            mosaic = root / "mosaic.mp4"
            white = root / "white_model.mp4"
            clothing = root / "original_clothing.png"
            for path in (reference, mosaic, white):
                path.write_bytes(b"video")
            clothing.write_bytes(self.image_bytes())
            source = web_app.WebJob(
                id="wardrobe-scene-extract-person",
                kind="wardrobe_prepare_scene",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
                status="succeeded",
                mosaic_path=mosaic,
                white_model_path=white,
                clothing_path=clothing,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                response = self.client.post(
                    "/api/wardrobe-swap/extract-references",
                    data={
                        "source_job_id": source.id,
                        "extract_missing": "true",
                        "person_asset": "asset://asset-existing123",
                    },
                )
                self.assertEqual(response.status_code, 202)
                worker = mock_thread.call_args.kwargs["target"]
                worker()
                mock_extract_person.assert_called_once()
                extraction = mock_extract_person.call_args.kwargs
                self.assertEqual(
                    extraction["prompt"],
                    web_app.WARDROBE_NEUTRAL_PERSON_TRIVIEW_PROMPT,
                )
                self.assertEqual(
                    extraction["required_constraint"],
                    web_app.WARDROBE_NEUTRAL_PERSON_TRIVIEW_REQUIRED_CONSTRAINT,
                )
                self.assertEqual(
                    extraction["artifact_version"],
                    web_app.WARDROBE_NEUTRAL_PERSON_TRIVIEW_VERSION,
                )
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    @patch("web_app.threading.Thread")
    @patch("web_app.ark_assets_client")
    @patch("web_app.prepare_ark_character_upload_source")
    @patch("web_app.ark_assets_configured", return_value=True)
    def test_extracted_person_can_be_submitted_to_character_library(
        self,
        _mock_configured: Mock,
        mock_prepare_source: Mock,
        mock_assets_client: Mock,
        mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as upload_dir:
            root = Path(temp_dir)
            person = root / "person.png"
            person.write_bytes(self.image_bytes())
            source = web_app.WebJob(
                id="wardrobe-submit-extracted-person",
                kind="wardrobe_prepare_clothing",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
                status="succeeded",
                person_path=person,
            )
            upload_source = Mock(url="https://example.test/person.png", channel="test")
            mock_prepare_source.return_value = upload_source
            mock_assets_client.return_value.create_asset.return_value = "asset-submit123"
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                with patch("web_app.timestamped_run_dir", return_value=Path(upload_dir)):
                    response = self.client.post(
                        "/api/wardrobe-swap/extracted-person/character-library",
                        data={
                            "source_job_id": source.id,
                            "group_id": "group-abcdef123",
                            "name": "原片人物",
                            "authorization_confirmed": "true",
                        },
                    )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertEqual(payload["uri"], "asset://asset-submit123")
                mock_assets_client.return_value.create_asset.assert_called_once_with(
                    group_id="group-abcdef123",
                    url="https://example.test/person.png",
                    name="原片人物",
                    asset_type="Image",
                )
                self.assertEqual(source.actors[0]["ark_library_upload_status"], "Processing")
                self.assertEqual(source.actors[0]["ark_library_asset_uri"], "asset://asset-submit123")
                mock_thread.return_value.start.assert_called_once()
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    @patch("web_app.ark_assets_configured", return_value=True)
    @patch("web_app.prepare_ark_character_upload_source")
    def test_extracted_person_submission_requires_authorization(self, mock_upload: Mock, _configured: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            person = root / "person.png"
            person.write_bytes(self.image_bytes())
            source = web_app.WebJob(
                id="wardrobe-submit-without-auth",
                kind="wardrobe_prepare_custom",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
                status="succeeded",
                person_path=person,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                response = self.client.post(
                    "/api/wardrobe-swap/extracted-person/character-library",
                    data={
                        "source_job_id": source.id,
                        "group_id": "group-abcdef123",
                        "name": "原片人物",
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("合法授权", response.get_json()["error"])
                mock_upload.assert_not_called()
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    @patch("web_app.threading.Thread")
    def test_person_only_retry_reuses_white_and_does_not_repeat_other_steps(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = root / "reference_first_15s.mp4"
            mosaic = root / "mosaic.mp4"
            white = root / "white_model.mp4"
            scene = root / "scene.png"
            for path in (reference, mosaic, white):
                path.write_bytes(b"video")
            scene.write_bytes(self.image_bytes())
            source = web_app.WebJob(
                id="wardrobe-person-only-retry",
                kind="wardrobe_prepare_clothing",
                project=web_app.WARDROBE_SWAP_PROJECT,
                run_dir=root,
                status="failed",
                error="previous person extraction network failure",
                mosaic_path=mosaic,
                white_model_path=white,
                scene_path=scene,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[source.id] = source
            try:
                response = self.client.post(
                    "/api/wardrobe-swap/extract-person",
                    data={"source_job_id": source.id},
                )
                self.assertEqual(response.status_code, 202)
                self.assertIn("wardrobe-person-extract-clothing", mock_thread.call_args.kwargs["name"])
                self.assertEqual(source.status, "running")
                self.assertEqual(source.error, "")
                mock_thread.return_value.start.assert_called_once()
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source.id, None)

    def test_temporary_store_uploads_image_with_image_mime_and_verifies_public_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "person.png"
            source.write_bytes(self.image_bytes())
            session = Mock()
            upload_response = Mock()
            upload_response.raise_for_status.return_value = None
            upload_response.json.return_value = {
                "files": [{"id": "imagefile123", "size": source.stat().st_size}]
            }
            head_response = Mock()
            head_response.raise_for_status.return_value = None
            head_response.headers = {
                "Content-Type": "image/png",
                "Content-Length": str(source.stat().st_size),
            }
            session.post.return_value = upload_response
            session.head.return_value = head_response
            store = web_app.TempFileMediaStore(session=session)

            uploaded = store.upload_file(source, expires_hours=6, attempts=1)

            self.assertEqual(uploaded.object_key, "imagefile123")
            self.assertIn("imagefile123", uploaded.signed_url)
            upload_tuple = session.post.call_args.kwargs["files"]["files"]
            self.assertEqual(upload_tuple[2], "image/png")
            session.head.assert_called_once()

    @patch("web_app.TempFileMediaStore")
    @patch("web_app.TemporaryPublicTunnel")
    @patch("web_app.TemporaryFileServer")
    @patch("web_app.TosMediaStore.configured", return_value=False)
    def test_character_upload_uses_temporary_image_when_project_tunnel_fails(
        self,
        _mock_tos_configured: Mock,
        mock_file_server_type: Mock,
        mock_tunnel_type: Mock,
        mock_temp_store_type: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "person.png"
            source.write_bytes(self.image_bytes())
            file_server = mock_file_server_type.return_value
            file_server.local_origin = "http://127.0.0.1:39001"
            file_server.route_path = "/asset/person.png"
            mock_tunnel_type.return_value.start.side_effect = web_app.WorkflowError("network reset")
            temp_store = mock_temp_store_type.return_value
            temp_store.upload_file.return_value = Mock(
                object_key="temporary-image-123",
                signed_url="https://tempfile.org/temporary-image-123/download",
            )

            result = web_app.prepare_ark_character_upload_source(source)

            self.assertEqual(result.channel, "temporary_image")
            self.assertEqual(result.url, "https://tempfile.org/temporary-image-123/download")
            temp_store.upload_file.assert_called_once_with(source, expires_hours=6, attempts=5)
            result.close()
            temp_store.delete.assert_called_once_with("temporary-image-123")


if __name__ == "__main__":
    unittest.main()
