from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import cv2
import numpy as np

import web_app


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        web_app.app.config.update(TESTING=True)
        self.client = web_app.app.test_client()

    def test_home_serves_project_center(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("视频复刻项目中心", html)
        self.assertNotIn("单人视频复刻", html)
        self.assertNotIn("多人视频复刻", html)
        self.assertIn("只更换人物", html)
        self.assertIn("只更换场景", html)
        self.assertIn("只更换服装", html)
        self.assertIn("长视频重绘", html)
        self.assertNotIn('href="/projects/single"', html)
        self.assertNotIn('href="/projects/multi"', html)
        self.assertIn('href="/projects/wardrobe"', html)
        self.assertNotIn('href="/projects/person"', html)
        self.assertNotIn('href="/projects/scene"', html)
        self.assertIn("3</b> 个能力组", html)
        self.assertIn("4</b> 个独立项目", html)
        self.assertIn('href="/projects/motion-transfer"', html)
        self.assertNotIn('href="/projects/shop-dance"', html)
        self.assertNotIn('href="/projects/clothing"', html)
        self.assertIn('href="/projects/long-video"', html)
        self.assertIn('href="/projects/real-long-video"', html)
        self.assertIn("火山角色库", html)
        self.assertNotIn("真人素材先生成眼部隐私遮挡图", html)
        response.close()

    def test_removed_shop_dance_feature_is_unavailable(self) -> None:
        for path in (
            "/projects/shop-dance",
            "/api/shop-dance/latest",
            "/api/shop-dance/jobs/archived-job",
            "/api/shop-dance/jobs/archived-job/files/white_model",
            "/api/shop-dance/jobs/archived-job/download.zip",
            "/static/shop_dance.html",
            "/static/shop_dance.js",
            "/static/shop_dance.css",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        for action in ("mosaic", "white-model", "generate", "recover"):
            with self.subTest(action=action):
                self.assertEqual(self.client.post(f"/api/shop-dance/{action}").status_code, 404)

    def test_healthz_is_available_without_site_login(self) -> None:
        with patch.dict("os.environ", {"DEPTHFLOW_SITE_USER": "demo", "DEPTHFLOW_SITE_PASSWORD": "secret"}, clear=False):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_error_categories_cover_six_user_facing_families(self) -> None:
        cases = {
            "AccountOverdueError: overdue balance": "余额/额度",
            "output video may contain sensitive information": "审核",
            "ratio must be adaptive": "参数",
            "请选择人物素材": "素材",
            "ProxyError: Remote end closed connection": "网络",
            "unexpected worker crash": "系统",
        }
        for message, label in cases.items():
            with self.subTest(message=message):
                self.assertEqual(web_app.classify_error_message(message)["label"], label)

    def test_single_person_route_keeps_existing_interface(self) -> None:
        response = self.client.get("/projects/single")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("DepthFlow", html)
        self.assertIn("Seedance 纯 API", html)
        self.assertIn('id="depthBtn"', html)
        self.assertIn('id="fullBtn"', html)
        self.assertIn('id="reuseDepthBtn"', html)
        self.assertNotIn('id="finalBtn"', html)
        self.assertIn('id="depthResultVideo"', html)
        self.assertIn('id="finalResultVideo"', html)
        self.assertIn('id="seedanceLivePreview"', html)
        self.assertIn('id="seedanceProgressPercent"', html)
        self.assertIn('name="duration" type="number" min="4" max="15" value="15" readonly', html)
        response.close()

    def test_multi_person_route_has_independent_interface(self) -> None:
        response = self.client.get("/projects/multi")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("多人视频复刻", html)
        self.assertIn('id="actorList"', html)
        self.assertIn('id="addActorBtn"', html)
        self.assertIn('id="reuseDepthBtn"', html)
        self.assertIn('id="fullBtn"', html)
        self.assertIn('/static/multi.js', html)
        response.close()

    def test_person_only_route_has_scene_and_final_previews(self) -> None:
        response = self.client.get("/projects/person")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/projects/wardrobe#person")
        response.close()
        response = self.client.get("/projects/person", follow_redirects=True)
        html = response.get_data(as_text=True)
        self.assertIn('id="whiteVideo"', html)
        self.assertIn('id="outputVideo"', html)
        self.assertIn('/static/inline_cast.js?', html)
        response.close()

    def test_person_only_prompt_maps_depth_person_clothes_and_original_scene(self) -> None:
        prompt = web_app.build_person_only_prompt()
        self.assertIn("@视频1", prompt)
        self.assertIn("@图片1", prompt)
        self.assertIn("@图片2", prompt)
        self.assertIn("@图片3", prompt)
        self.assertIn("原视频场景必须保留", prompt)
        self.assertIn("最终输出正常彩色视频", prompt)

    def test_partial_replacement_pages_share_ark_character_library(self) -> None:
        for route in ("/projects/person", "/projects/scene", "/projects/clothing"):
            with self.subTest(route=route):
                response = self.client.get(route, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn('/static/character_library.js?v=20260915-upload4', response.get_data(as_text=True))
                response.close()
        script_response = self.client.get("/static/character_library.js")
        script = script_response.get_data(as_text=True)
        self.assertIn("审核并上传角色库", script)
        self.assertIn("data-character-select", script)
        self.assertIn("data-character-delete-confirmed", script)
        self.assertIn('/api/character-library?', script)
        self.assertIn('personAssetInput.name = "person_asset"', script)
        self.assertNotIn("window.prompt", script)
        self.assertNotIn("window.confirm", script)
        script_response.close()

    def test_shared_character_library_api_and_asset_validation(self) -> None:
        library = {
            "configured": True,
            "upload_ready": True,
            "groups": [{"id": "group-abcdef123", "name": "角色组", "group_type": "AIGC"}],
            "assets": [{"id": "asset-abcdef123", "uri": "asset://asset-abcdef123", "status": "Active"}],
        }
        with patch.object(web_app, "_load_ark_character_library", return_value=library):
            response = self.client.get("/api/character-library")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["assets"][0]["uri"], "asset://asset-abcdef123")
        response.close()
        with web_app.app.test_request_context(
            "/api/scene-only/generate",
            method="POST",
            data={"person_asset": "asset://asset-abcdef123"},
        ):
            self.assertEqual(web_app.requested_character_asset(), "asset://asset-abcdef123")
        with web_app.app.test_request_context(
            "/api/clothing-only/generate",
            method="POST",
            data={"person_asset": "https://example.com/person.png"},
        ):
            with self.assertRaises(web_app.WorkflowError):
                web_app.requested_character_asset()

    def test_scene_only_route_has_extraction_and_five_previews(self) -> None:
        response = self.client.get("/projects/scene")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/projects/wardrobe#scene")
        response.close()
        response = self.client.get("/projects/scene", follow_redirects=True)
        html = response.get_data(as_text=True)
        self.assertIn('id="whiteVideo"', html)
        self.assertIn('id="outputVideo"', html)
        self.assertIn('/static/inline_cast.js?', html)
        response.close()

    def test_scene_only_prompt_preserves_subject_and_replaces_only_background(self) -> None:
        prompt = web_app.build_scene_only_prompt()
        self.assertIn("@视频1", prompt)
        self.assertIn("@图片1", prompt)
        self.assertIn("@图片2", prompt)
        self.assertIn("@图片3", prompt)
        self.assertIn("仅将原视频背景", prompt)
        self.assertIn("人物在画面中的尺寸", prompt)

    def test_clothing_only_route_has_two_extractions_and_five_previews(self) -> None:
        response = self.client.get("/projects/clothing")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/projects/wardrobe#clothing")
        response.close()
        response = self.client.get("/projects/clothing", follow_redirects=True)
        html = response.get_data(as_text=True)
        self.assertIn('id="whiteVideo"', html)
        self.assertIn('id="outputVideo"', html)
        self.assertIn('/static/inline_cast.js?', html)
        response.close()

    def test_clothing_only_prompt_replaces_only_wardrobe(self) -> None:
        prompt = web_app.build_clothing_only_prompt()
        self.assertIn("@视频1", prompt)
        self.assertIn("@图片1", prompt)
        self.assertIn("@图片2", prompt)
        self.assertIn("@图片3", prompt)
        self.assertIn("只将主角服装替换", prompt)
        self.assertIn("原视频场景必须完整保留", prompt)
        self.assertIn("@图片2是唯一服装依据", prompt)
        self.assertIn("严禁沿用、复制或恢复@视频1中的原片服装", prompt)

    def test_project_five_person_prompt_removes_original_clothing(self) -> None:
        prompt = web_app.DEFAULT_CLOTHING_PERSON_TRIVIEW_PROMPT
        self.assertIn("纯白色、无图案", prompt)
        self.assertIn("不要提取、复制或重建原视频中的任何服装", prompt)
        self.assertIn("移除原片的外套", prompt)
        self.assertIn("原始服装一致", web_app.DEFAULT_PERSON_TRIVIEW_PROMPT)

    def test_project_five_rejects_unversioned_legacy_person_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            job = web_app.WebJob(id="legacy-clothing", kind="clothing_prepare", run_dir=directory)
            self.assertFalse(web_app.project_five_person_reference_is_current(job))
            (directory / "original_person_triview.version").write_text(
                web_app.CLOTHING_PERSON_TRIVIEW_VERSION,
                encoding="utf-8",
            )
            self.assertTrue(web_app.project_five_person_reference_is_current(job))

    def test_long_video_route_has_analysis_shots_and_final_preview(self) -> None:
        response = self.client.get("/projects/long-video")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("长视频多分镜复刻", html)
        self.assertIn('id="analyzeBtn"', html)
        self.assertIn('id="shotGrid"', html)
        self.assertIn('id="generateBtn"', html)
        self.assertIn('id="regenerateAllBtn"', html)
        self.assertIn('id="finalResultVideo"', html)
        self.assertIn("固定新声音", html)
        self.assertNotIn('id="generateNewVoice"', html)
        self.assertNotIn('id="preserveOriginalAudio"', html)
        self.assertIn('id="mosaicBtn"', html)
        self.assertIn('id="performanceBtn"', html)
        self.assertIn('id="whiteModelBtn"', html)
        self.assertNotIn('id="whiteReferenceVideo"', html)
        self.assertNotIn('id="whiteModelPrompt"', html)
        self.assertIn("无需上传白模参考", html)
        self.assertIn("光滑光头 · 无发型毛发", html)
        self.assertIn('id="requireWhiteModel"', html)
        self.assertIn('id="mosaicVideo"', html)
        self.assertIn('id="whiteModelVideo"', html)
        self.assertIn('id="actorList"', html)
        self.assertIn('id="confirmCast"', html)
        self.assertIn("0人重绘新场景；1人走单人流程；2–4人自动走多人流程", html)
        self.assertIn('/static/long_video.js', html)
        self.assertIn('long_video.js?v=20260914-multi1', html)
        self.assertIn('long_video.css?v=20260812-02', html)
        self.assertIn('id="downloadCenter"', html)
        self.assertIn('id="whiteShotDownloadList"', html)
        self.assertIn('id="finalShotDownloadList"', html)
        self.assertIn("结果下载中心", html)
        self.assertIn("裁剪前成品片段", html)
        self.assertIn("尚未按原分镜时长裁剪", html)
        self.assertIn("SEEDANCE 2.0 · 480P · 按镜计费", html)
        self.assertIn("短分镜保持原动作速度，片尾定格延长到生成时长", html)
        self.assertIn('id="sceneGroupList"', html)
        self.assertIn('id="autoMatchScenesBtn"', html)
        self.assertIn('id="finalStyleLibrary"', html)
        self.assertIn('id="finalStyle"', html)
        self.assertIn("成片风格库", html)
        self.assertIn("固定无文字", html)
        self.assertNotIn('id="watermark"', html)
        self.assertIn("无人镜头也会重绘", html)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        response.close()

    def test_real_long_video_route_uses_character_library_only(self) -> None:
        response = self.client.get("/projects/real-long-video")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("真实人物复刻重绘", html)
        self.assertIn('id="realPersonAuthorized"', html)
        self.assertIn('id="prepareRealActorsBtn"', html)
        self.assertNotIn('id="sketchPrompt"', html)
        self.assertRegex(html, r'src="/static/real_long_video\.js\?v=[^"]+"')
        self.assertIn('<select id="resolution"><option>480p</option><option selected>720p</option><option>1080p</option>', html)
        self.assertIn('value="doubao-seedance-2-5-260628">Seedance 2.5', html)
        self.assertIn('id="ratioHint"', html)
        self.assertIn('id="arkCharacterAssetList"', html)
        self.assertIn('id="createRealCharacterGroupBtn"', html)
        self.assertNotIn('id="arkCharacterValidationPanel"', html)
        self.assertNotIn('id="checkCharacterValidationBtn"', html)
        self.assertIn('id="renameCharacterGroupBtn"', html)
        self.assertIn('id="deleteCharacterGroupBtn"', html)
        self.assertIn('/static/long_video.css?v=20260822-08', html)
        self.assertIn('class="ark-character-assets-heading"', html)
        self.assertLess(html.index('id="realPersonAuthorized"'), html.index('class="ark-character-library"'))
        self.assertIn("上传虚拟人物图后直接提交 AIGC 角色库", html)
        self.assertIn("创建人像组", html)
        self.assertIn('id="quickCreateCharacterGroupBtn"', html)
        self.assertIn('id="quickCharacterGroupDialog"', html)
        self.assertIn("AIGC 虚拟人像组不需要真人活体认证", html)
        self.assertIn("上传并绑定火山角色库", html)
        self.assertNotIn("Seedream素描参考", html)
        self.assertIn('id="realActorStatusHint"', html)
        self.assertIn('id="whiteModelRatio"', html)
        self.assertIn("跟随原片（推荐）", html)
        self.assertIn("裁剪前成品片段", html)
        real_script = self.client.get("/static/real_long_video.js")
        self.assertEqual(real_script.status_code, 200)
        script = real_script.get_data(as_text=True)
        self.assertIn("最终人物身份来源", script)
        self.assertIn("real-reference-placeholder", script)
        self.assertIn("人物原图只用于创建角色库素材", script)
        self.assertIn("actorAssetPreviewMarkup", script)
        self.assertIn("actor-selected-preview-image", script)
        self.assertIn("savedActorReferencePreviewMarkup", script)
        self.assertIn("clothing_preview_url", script)
        self.assertIn("本地原图回退", script)
        self.assertIn("ark-character-asset-preview", script)
        self.assertIn('data-create-character-group="#actorArkGroup${index}"', script)
        self.assertIn("openQuickCharacterGroupDialog", script)
        self.assertIn("确认费用并重生此镜白模", script)
        self.assertIn('form.append("white_retry_cost_confirmed"', script)
        self.assertIn("monitorModeForJob", script)
        self.assertIn('monitor(latest.id, monitorModeForJob(latest), { scroll: false })', script)
        self.assertIn('data-upload-actor-character="${index}"', script)
        self.assertIn('data-delete-actor-character="${index}"', script)
        self.assertIn("上传此人物到角色库", script)
        self.assertIn("从角色库删除此人物", script)
        self.assertNotIn('id="uploadActorToArk${index}"', script)
        self.assertNotIn('form.append("sketch_prompt"', script)
        self.assertIn("A completed job must never inherit a stale local submission lock", script)
        self.assertIn('form.append("ratio", $("#whiteModelRatio").value)', script)
        self.assertIn('ratio.value = "adaptive"', script)
        self.assertIn('ratio.disabled = true', script)
        self.assertIn('form.append(`ark_group_id_${index}`, groupId)', script)
        self.assertNotIn('form.append("ratio", $("#ratio").value);\n  form.append("generate_audio", "false")', script)
        self.assertIn("处理分镜 ${String(pendingCompositionShots[0])", script)
        self.assertNotIn("generationBlocked || compositionApprovalBlocked", script)
        real_script.close()
        self.assertNotIn('/static/long_video.js?', html)
        response.close()

    def test_real_character_library_creates_aigc_group_and_manages_it(self) -> None:
        assets = Mock()
        assets.create_asset_group.return_value = "group-virtualperson123"
        with (
            patch.object(web_app, "ark_assets_configured", return_value=True),
            patch.object(web_app, "ark_assets_client", return_value=assets),
        ):
            created = self.client.post(
                "/api/real-long-video/character-library/groups",
                json={"name": "虚拟女主角", "description": "AIGC 虚拟角色"},
            )
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.get_json()["group_id"], "group-virtualperson123")
            self.assertEqual(created.get_json()["group_type"], "AIGC")
            assets.create_asset_group.assert_called_once_with(
                name="虚拟女主角",
                description="AIGC 虚拟角色",
                group_type="AIGC",
            )

            renamed = self.client.patch(
                "/api/real-long-video/character-library/groups/group-virtualperson123",
                json={"name": "虚拟女主角B", "description": "新说明"},
            )
            self.assertEqual(renamed.status_code, 200)
            deleted = self.client.delete(
                "/api/real-long-video/character-library/groups/group-virtualperson123",
            )
            self.assertEqual(deleted.status_code, 200)
            assets.delete_asset_group.assert_called_once_with("group-virtualperson123")

    def test_real_character_validation_pending_does_not_fail_the_flow(self) -> None:
        assets = Mock()
        assets.get_visual_validate_result.side_effect = web_app.ArkAPIError(
            400,
            "validation is not finished",
            "VisualValidateNotFinished",
        )
        with (
            patch.object(web_app, "ark_assets_configured", return_value=True),
            patch.object(web_app, "ark_assets_client", return_value=assets),
        ):
            response = self.client.post(
                "/api/real-long-video/character-library/validation-sessions/result",
                json={"byted_token": "token-real-person-123", "name": "女主角"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "pending")

    def test_shared_character_upload_accepts_new_local_picture(self) -> None:
        assets = Mock()
        assets.create_asset.return_value = "asset-newpicture123"
        source = Mock(url="https://example.test/temporary-picture", channel="test")
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(web_app, "ark_assets_configured", return_value=True),
            patch.object(web_app, "ark_assets_client", return_value=assets),
            patch.object(web_app, "timestamped_run_dir", return_value=Path(temporary)),
            patch.object(web_app, "prepare_ark_character_upload_source", return_value=source) as prepare,
            patch.object(web_app.threading, "Thread"),
        ):
            response = self.client.post("/api/character-library/assets", data={
                "group_id": "group-newpeople123", "name": "全新人物",
                "asset_file": (io.BytesIO(b"new-local-picture"), "new-person.png"),
            })
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["uri"], "asset://asset-newpicture123")
            self.assertEqual(response.get_json()["status"], "Processing")
            self.assertEqual(prepare.call_args.args[0].read_bytes(), b"new-local-picture")
            assets.create_asset.assert_called_once_with(
                group_id="group-newpeople123", url=source.url, name="全新人物", asset_type="Image",
            )

    def test_real_actor_upload_binds_active_character_asset_as_only_identity(self) -> None:
        runs_dir = web_app.PROJECT_DIR / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runs_dir) as temp_dir:
            directory = Path(temp_dir)
            person = directory / "real_person_1_original.png"
            person.write_bytes(b"authorized-person-image")
            actor = {
                "id": 1,
                "original_person_source": str(person),
                "local_person_source": str(person),
                "person_source": str(person),
                "ark_library_upload_requested": True,
                "ark_library_group_id": "group-test123",
                "ark_library_asset_name": "女主角",
            }
            job = web_app.WebJob(
                id="real-actor-ark-upload",
                kind="real_long_actor_prepare",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                actors=[actor],
            )
            uploaded = SimpleNamespace(
                signed_url="https://tos.example/character.png",
                object_key="seedance-inputs/character.png",
            )
            tos = Mock()
            tos.upload_file.return_value = uploaded
            tos_factory = Mock(return_value=tos)
            tos_factory.configured.return_value = True
            assets = Mock()
            assets.create_asset.return_value = "asset-test123"
            assets.get_asset.return_value = {"Status": "Active"}
            with (
                patch.object(web_app, "ark_assets_configured", return_value=True),
                patch.object(web_app, "TosMediaStore", tos_factory),
                patch.object(web_app, "ark_assets_client", return_value=assets),
            ):
                uri = web_app.submit_real_actor_to_character_library(job, actor)
            self.assertEqual(uri, "asset://asset-test123")
            self.assertEqual(actor["original_person_source"], "asset://asset-test123")
            self.assertEqual(actor["person_source"], "asset://asset-test123")
            self.assertEqual(actor["trusted_asset_uri"], "asset://asset-test123")
            self.assertEqual(actor["local_person_source"], str(person))
            self.assertEqual(actor["ark_library_asset_uri"], "asset://asset-test123")
            self.assertEqual(actor["ark_library_upload_status"], "Active")
            self.assertFalse(actor["ark_library_upload_requested"])
            assets.create_asset.assert_called_once_with(
                group_id="group-test123",
                url="https://tos.example/character.png",
                name="女主角",
                asset_type="Image",
            )
            tos.delete.assert_called_once_with("seedance-inputs/character.png")

    def test_real_actor_upload_uses_project_tunnel_when_tos_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            person = Path(temp_dir) / "person.png"
            person.write_bytes(b"virtual-person-image")
            file_server = Mock()
            file_server.local_origin = "http://127.0.0.1:45678"
            file_server.route_path = "/media/token/person.png"
            file_server_factory = Mock(return_value=file_server)
            tunnel = Mock()
            tunnel.start.return_value = "https://one-time.trycloudflare.com"
            tunnel_factory = Mock(return_value=tunnel)
            tos_factory = Mock()
            tos_factory.configured.return_value = False
            with (
                patch.object(web_app, "TosMediaStore", tos_factory),
                patch.object(web_app, "TemporaryFileServer", file_server_factory),
                patch.object(web_app, "TemporaryPublicTunnel", tunnel_factory),
            ):
                upload_source = web_app.prepare_ark_character_upload_source(person)
            self.assertEqual(upload_source.channel, "project_tunnel")
            self.assertEqual(
                upload_source.url,
                "https://one-time.trycloudflare.com/media/token/person.png",
            )
            file_server.start.assert_called_once_with()
            tunnel.wait_until_reachable.assert_called_once_with(upload_source.url)
            upload_source.close()
            tunnel.close.assert_called_once_with()
            file_server.close.assert_called_once_with()

    def test_real_actor_saved_person_and_clothing_previews_are_served(self) -> None:
        runs_dir = web_app.PROJECT_DIR / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runs_dir) as temp_dir:
            directory = Path(temp_dir)
            person = directory / "real_person_1_original.png"
            clothing = directory / "real_clothing_1.png"
            person.write_bytes(b"saved-person-preview")
            clothing.write_bytes(b"saved-clothing-preview")
            job = web_app.WebJob(
                id="real-actor-saved-previews",
                kind="real_long_analyze",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                actors=[{
                    "id": 1,
                    "role": "女主角",
                    "person_source": "asset://asset-preview123",
                    "original_person_source": "asset://asset-preview123",
                    "trusted_asset_uri": "asset://asset-preview123",
                    "clothing_source": str(clothing),
                }],
            )
            web_app.JOBS[job.id] = job
            try:
                actor = job.public()["actors"][0]
                self.assertEqual(
                    actor["person_preview_url"],
                    f"/api/jobs/{job.id}/actors/1/file/person",
                )
                self.assertEqual(
                    actor["clothing_preview_url"],
                    f"/api/jobs/{job.id}/actors/1/file/clothing",
                )
                person_response = self.client.get(actor["person_preview_url"])
                clothing_response = self.client.get(actor["clothing_preview_url"])
                self.assertEqual(person_response.status_code, 200)
                self.assertEqual(clothing_response.status_code, 200)
                self.assertEqual(person_response.data, b"saved-person-preview")
                self.assertEqual(clothing_response.data, b"saved-clothing-preview")
                person_response.close()
                clothing_response.close()
            finally:
                web_app.JOBS.pop(job.id, None)

    def test_real_actor_bound_character_asset_can_be_deleted_per_actor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            person = directory / "real_person_1_original.png"
            person.write_bytes(b"authorized-person-image")
            job = web_app.WebJob(
                id="real-actor-delete-one",
                kind="real_long_actor_prepare",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                actors=[{
                    "id": 1,
                    "role": "女主角",
                    "local_person_source": str(person),
                    "original_person_source": "asset://asset-test123",
                    "person_source": "asset://asset-test123",
                    "trusted_asset_uri": "asset://asset-test123",
                    "ark_library_asset_uri": "asset://asset-test123",
                    "ark_library_asset_name": "女主角",
                    "ark_library_upload_status": "Active",
                }],
            )
            assets = Mock()
            web_app.JOBS[job.id] = job
            try:
                with (
                    patch.object(web_app, "ark_assets_client", return_value=assets),
                    patch.object(web_app, "_persist_long_job"),
                ):
                    response = self.client.delete(
                        "/api/real-long-video/actors/1/character-asset",
                        json={"source_job_id": job.id, "asset_uri": "asset://asset-test123"},
                    )
                self.assertEqual(response.status_code, 200)
                assets.delete_asset.assert_called_once_with("asset-test123")
                self.assertEqual(job.actors[0]["person_source"], str(person))
                self.assertEqual(job.actors[0]["original_person_source"], str(person))
                self.assertEqual(job.actors[0]["trusted_asset_uri"], "")
                self.assertEqual(job.actors[0]["ark_library_asset_uri"], "")
                self.assertEqual(job.actors[0]["ark_library_upload_status"], "已从角色库删除")
            finally:
                web_app.JOBS.pop(job.id, None)

    def test_long_shot_download_exposes_unconformed_seedance_result(self) -> None:
        runs_dir = web_app.PROJECT_DIR / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runs_dir) as temp_dir:
            directory = Path(temp_dir)
            shot_dir = directory / "shots" / "shot_01"
            shot_dir.mkdir(parents=True)
            source = shot_dir / "source.mp4"
            conformed = shot_dir / "final_conformed_signature.mp4"
            raw_output = shot_dir / "多人复刻成片_task-raw.mp4"
            source.write_bytes(b"source")
            conformed.write_bytes(b"trimmed")
            raw_output.write_bytes(b"untrimmed-seedance")
            job = web_app.WebJob(
                id="raw-shot-download",
                kind="long_generate",
                project=web_app.VIRTUAL_LONG_PROJECT,
                run_dir=directory,
                status="succeeded",
                shots=[{
                    "index": 1,
                    "source_path": str(source),
                    "output_path": str(conformed),
                    "task_id": "task-raw",
                }],
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                public_shot = job.public()["shots"][0]
                self.assertTrue(public_shot["has_raw_output"])
                self.assertEqual(
                    public_shot["raw_output_url"],
                    "/api/jobs/raw-shot-download/shots/1/file/raw_output",
                )
                self.assertNotIn("raw_output_path", public_shot)
                response = self.client.get(public_shot["raw_output_url"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b"untrimmed-seedance")
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    def test_real_long_continuity_retry_is_isolated_from_virtual_project(self) -> None:
        continuity = {
            "characters": [{"character_id": 1, "description": "同一人物"}],
            "assignments": [{"shot_index": 1, "slot": 1, "character_id": 1}],
        }
        real_job = web_app.WebJob(
            id="real-continuity-retry",
            kind="real_long_performance",
            project=web_app.REAL_PERSON_LONG_PROJECT,
            run_dir=Path("."),
        )
        real_analyzer = Mock()
        real_analyzer.analyze_cast_continuity.side_effect = [
            web_app.ArkConnectionError("POST", "proxy disconnected", 1),
            continuity,
        ]
        with patch("web_app.time.sleep") as mock_sleep:
            result = web_app.analyze_long_cast_continuity(
                real_job,
                real_analyzer,
                "https://example.com/full.mp4",
                shot_slot_counts={1: 1},
                shot_ranges=[{"index": 1, "start": 0.0, "end": 1.0}],
            )
        self.assertEqual(result, continuity)
        self.assertEqual(real_analyzer.analyze_cast_continuity.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)

        virtual_job = web_app.WebJob(
            id="virtual-continuity-no-retry",
            kind="long_performance",
            project=web_app.VIRTUAL_LONG_PROJECT,
            run_dir=Path("."),
        )
        virtual_analyzer = Mock()
        virtual_analyzer.analyze_cast_continuity.side_effect = web_app.ArkConnectionError(
            "POST", "proxy disconnected", 1
        )
        with patch("web_app.time.sleep") as virtual_sleep:
            with self.assertRaises(web_app.ArkConnectionError):
                web_app.analyze_long_cast_continuity(
                    virtual_job,
                    virtual_analyzer,
                    "https://example.com/full.mp4",
                    shot_slot_counts={1: 1},
                    shot_ranges=[{"index": 1, "start": 0.0, "end": 1.0}],
                )
        self.assertEqual(virtual_analyzer.analyze_cast_continuity.call_count, 1)
        virtual_sleep.assert_not_called()

    def test_real_continuity_invalid_json_or_missing_slots_retries_then_recovers(self) -> None:
        continuity = {
            "characters": [{"character_id": 1, "description": "同一人物"}],
            "assignments": [{"shot_index": 1, "slot": 1, "character_id": 1}],
        }
        job = web_app.WebJob(
            id="real-continuity-format-retry",
            kind="real_long_performance",
            project=web_app.REAL_PERSON_LONG_PROJECT,
            run_dir=Path("."),
        )
        analyzer = Mock()
        analyzer.analyze_cast_continuity.side_effect = [
            web_app.WorkflowError("跨镜人物连续性分析接口没有返回可解析的 JSON。"),
            web_app.WorkflowError("跨镜人物连续性分析缺少槽位：分镜01-P1。"),
            continuity,
        ]
        with patch("web_app.time.sleep") as mock_sleep:
            result = web_app.analyze_long_cast_continuity(
                job,
                analyzer,
                "data:video/mp4;base64,AAAA",
                shot_slot_counts={1: 1},
                shot_ranges=[{"index": 1, "start": 0.0, "end": 1.0}],
            )
        self.assertEqual(result, continuity)
        self.assertEqual(analyzer.analyze_cast_continuity.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        final_instruction = analyzer.analyze_cast_continuity.call_args.kwargs["retry_instruction"]
        self.assertIn("分镜01-P1", final_instruction)

    def test_real_continuity_repeated_invalid_output_falls_back_to_manual_mapping(self) -> None:
        job = web_app.WebJob(
            id="real-continuity-manual-fallback",
            kind="real_long_performance",
            project=web_app.REAL_PERSON_LONG_PROJECT,
            run_dir=Path("."),
        )
        analyzer = Mock()
        analyzer.analyze_cast_continuity.side_effect = web_app.WorkflowError(
            "跨镜人物连续性分析接口没有返回可解析的 JSON。"
        )
        with patch("web_app.time.sleep"):
            result = web_app.analyze_long_cast_continuity(
                job,
                analyzer,
                "data:video/mp4;base64,AAAA",
                shot_slot_counts={1: 1, 2: 2},
                shot_ranges=[
                    {"index": 1, "start": 0.0, "end": 1.0},
                    {"index": 2, "start": 1.0, "end": 2.0},
                ],
            )
        self.assertTrue(result["manual_review_required"])
        self.assertEqual(result["assignments"], [])
        self.assertEqual(analyzer.analyze_cast_continuity.call_count, 3)
        self.assertTrue(any("逐镜人工人物绑定" in message for message in job.logs))

    def test_real_continuity_reuses_complete_success_instead_of_overwriting_it(self) -> None:
        continuity = {
            "version": 1,
            "characters": [
                {"character_id": 1, "description": "女性"},
                {"character_id": 2, "description": "男性"},
            ],
            "assignments": [
                {"shot_index": 1, "slot": 1, "character_id": 2},
                {"shot_index": 2, "slot": 1, "character_id": 1},
                {"shot_index": 2, "slot": 2, "character_id": 2},
            ],
        }
        job = web_app.WebJob(
            id="real-continuity-preserve-success",
            kind="real_long_performance",
            project=web_app.REAL_PERSON_LONG_PROJECT,
            run_dir=Path("."),
            cast_continuity=continuity,
        )
        analyzer = Mock()
        result = web_app.analyze_long_cast_continuity(
            job,
            analyzer,
            "data:video/mp4;base64,AAAA",
            shot_slot_counts={1: 1, 2: 2},
            shot_ranges=[
                {"index": 1, "start": 0.0, "end": 1.0},
                {"index": 2, "start": 1.0, "end": 2.0},
            ],
        )
        self.assertEqual(result, continuity)
        analyzer.analyze_cast_continuity.assert_not_called()
        self.assertTrue(any("直接复用" in message for message in job.logs))

    def test_performance_video_transport_is_isolated_by_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "shot.mp4"
            source.write_bytes(b"video-bytes")
            store = Mock()

            real_job = web_app.WebJob(
                id="real-inline-video",
                kind="real_long_performance",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=Path(temporary),
            )
            real_reference, real_object_key = web_app.long_performance_video_reference(
                real_job, source, store
            )
            self.assertTrue(real_reference.startswith("data:video/mp4;base64,"))
            self.assertEqual(real_object_key, "")
            store.upload_video.assert_not_called()

            store.upload_video.return_value = SimpleNamespace(
                signed_url="https://temporary.example/shot.mp4",
                object_key="temporary-file-id",
            )
            virtual_job = web_app.WebJob(
                id="virtual-temporary-video",
                kind="long_performance",
                project=web_app.VIRTUAL_LONG_PROJECT,
                run_dir=Path(temporary),
            )
            virtual_reference, virtual_object_key = web_app.long_performance_video_reference(
                virtual_job, source, store
            )
            self.assertEqual(virtual_reference, "https://temporary.example/shot.mp4")
            self.assertEqual(virtual_object_key, "temporary-file-id")
            store.upload_video.assert_called_once_with(source, expires_hours=1)

    def test_real_long_video_prompt_uses_asset_clothing_then_scene(self) -> None:
        prompt = web_app.build_long_shot_prompt(
            [
                {"id": 1, "role": "左侧人物", "source_slot": 1},
                {"id": 2, "role": "右侧人物", "source_slot": 2},
            ],
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
            replace_scene=True,
            real_person_mode=True,
        )
        self.assertIn("@图片1是火山角色库已授权人物素材", prompt)
        self.assertIn("@图片2只提供服装", prompt)
        self.assertIn("@图片3是火山角色库已授权人物素材", prompt)
        self.assertIn("@图片4只提供服装", prompt)
        self.assertIn("替换为@图片5中的新场景", prompt)
        self.assertNotIn("遮眼条", prompt)
        self.assertNotIn("素描质感", prompt)

    def test_real_person_sketch_prompt_keeps_three_views_as_one_identity(self) -> None:
        self.assertIn("正面、侧面、背面", web_app.REAL_PERSON_SKETCH_PROMPT)
        self.assertIn("同一个人物身份", web_app.REAL_PERSON_SKETCH_PROMPT)
        self.assertIn("不得解释成多人", web_app.REAL_PERSON_SKETCH_PROMPT)

    def test_real_person_seedance_sources_use_only_asset_and_clothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            clothing = directory / "clothing.png"
            clothing.write_bytes(b"clothing")
            sources = web_app.real_person_character_sources([
                {
                    "id": 1,
                    "original_person_source": "asset://asset-test123",
                    "person_source": "asset://asset-test123",
                    "trusted_asset_uri": "asset://asset-test123",
                    "clothing_source": str(clothing),
                }
            ])
            self.assertEqual(sources, [("asset://asset-test123", str(clothing))])

            with self.assertRaisesRegex(web_app.WorkflowError, "角色库"):
                web_app.real_person_character_sources([
                    {
                        "id": 1,
                        "original_person_source": str(directory / "raw.png"),
                        "clothing_source": str(clothing),
                    }
                ])

    def test_real_final_generation_reuses_bound_character_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            clothing = directory / "clothing.png"
            clothing.write_bytes(b"clothing")
            job = web_app.WebJob(
                id="same-real-reupload",
                kind="real_long_analyze",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                actors=[{
                    "id": 1,
                    "role": "人物1",
                    "person_source": "asset://asset-test123",
                    "original_person_source": "asset://asset-test123",
                    "trusted_asset_uri": "asset://asset-test123",
                    "clothing_source": str(clothing),
                }],
            )
            with web_app.app.test_request_context(
                "/api/real-long-video/generate",
                method="POST",
                data={
                    "role_description_1": "人物1",
                    "person_source_mode_1": "ark_asset",
                    "person_asset_1": "asset://asset-test123",
                },
                content_type="multipart/form-data",
            ):
                actors = web_app.save_long_actor_references(
                    job,
                    actor_count=1,
                    used_actor_ids={1},
                    require_real_derivatives=True,
                )
            self.assertEqual(actors[0]["person_source"], "asset://asset-test123")
            self.assertEqual(actors[0]["masked_person_source"], "")
            self.assertEqual(actors[0]["sketch_person_source"], "")

    def test_real_and_virtual_long_jobs_are_isolated_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            virtual = web_app.WebJob(
                id="virtual-isolated",
                kind="long_analyze",
                project=web_app.VIRTUAL_LONG_PROJECT,
                run_dir=Path(temp_dir) / "virtual",
                created_at="2026-01-01T00:00:00",
            )
            real = web_app.WebJob(
                id="real-isolated",
                kind="real_long_analyze",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=Path(temp_dir) / "real",
                created_at="2026-01-02T00:00:00",
            )
            with patch.dict(web_app.JOBS, {virtual.id: virtual, real.id: real}, clear=True):
                self.assertIs(web_app.restore_latest_long_video_job(), virtual)
                self.assertIs(web_app.restore_latest_long_video_job(web_app.REAL_PERSON_LONG_PROJECT), real)

    def test_long_video_workspaces_create_rename_bind_and_keep_projects_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            web_app, "LONG_WORKSPACE_INDEX_PATH", Path(temp_dir) / "workspaces.json"
        ), patch.object(web_app, "restore_latest_long_video_job", return_value=None):
            virtual_default = web_app.ensure_long_workspace(web_app.VIRTUAL_LONG_PROJECT)
            real_default = web_app.ensure_long_workspace(web_app.REAL_PERSON_LONG_PROJECT)
            self.assertEqual(virtual_default[0]["name"], "重绘项目 1")
            self.assertNotEqual(virtual_default[0]["id"], real_default[0]["id"])

            second = web_app.create_long_workspace(web_app.REAL_PERSON_LONG_PROJECT, "重绘项目 2")
            renamed = web_app.update_long_workspace(
                web_app.REAL_PERSON_LONG_PROJECT, second["id"], name="卧室篇"
            )
            self.assertEqual(renamed["name"], "卧室篇")

            job = web_app.WebJob(
                id="workspace-job",
                kind="real_long_analyze",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=Path(temp_dir) / "job",
            )
            job.run_dir.mkdir()
            web_app.bind_long_workspace(job, second["id"])
            web_app._persist_long_job(job)
            records = web_app.list_long_workspaces(web_app.REAL_PERSON_LONG_PROJECT)
            bound = next(item for item in records if item["id"] == second["id"])
            self.assertEqual(bound["job_id"], job.id)
            self.assertEqual(job.workspace_name, "卧室篇")

    def test_long_video_workspace_api_is_available_to_both_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            web_app, "LONG_WORKSPACE_INDEX_PATH", Path(temp_dir) / "workspaces.json"
        ), patch.object(web_app, "restore_latest_long_video_job", return_value=None):
            virtual = self.client.get("/api/long-video/workspaces")
            real = self.client.post("/api/real-long-video/workspaces", json={"name": "重绘项目 2"})
            self.assertEqual(virtual.status_code, 200)
            self.assertEqual(real.status_code, 201)
            self.assertEqual(virtual.get_json()["project"], web_app.VIRTUAL_LONG_PROJECT)
            self.assertEqual(real.get_json()["workspace"]["name"], "重绘项目 2")
            virtual.close()
            real.close()

    def test_workspace_manager_is_shared_by_real_and_virtual_long_pages(self) -> None:
        script = self.client.get("/static/workflow_common.js")
        text = script.get_data(as_text=True)
        self.assertIn("重绘项目", text)
        self.assertIn("data-workspace-create", text)
        self.assertIn("workspace-operation-modal", text)
        self.assertNotIn("window.prompt", text)
        self.assertNotIn("window.confirm", text)
        self.assertIn("DepthFlowWorkspaces", text)
        self.assertIn("workspace_id", text)
        script.close()

    def test_static_long_video_script_disables_browser_cache(self) -> None:
        response = self.client.get("/static/long_video.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertIn("data-regenerate-white-shot", response.get_data(as_text=True))
        self.assertIn("data-delete-shot", response.get_data(as_text=True))
        self.assertIn("data-save-performance", response.get_data(as_text=True))
        self.assertIn("data-position-lock", response.get_data(as_text=True))
        response.close()

    def test_final_style_library_defaults_to_character_reference_and_adds_prompt(self) -> None:
        styled, preset = web_app.apply_final_style_prompt("基础约束", "match_character")
        self.assertEqual(preset["id"], "match_character")
        self.assertIn("基础约束", styled)
        self.assertIn("跟随新人物参考图", preset["label"])
        self.assertIn("真人参考输出真人影视质感", styled)
        self.assertIn("不得出现可读文字或字符", styled)
        self.assertIn("台词只能作为声音与口型存在", styled)
        with self.assertRaises(web_app.WorkflowError):
            web_app.apply_final_style_prompt("基础约束", "unknown-style")

    def test_manual_performance_edits_are_saved_and_mark_shot_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shot_dir = directory / "shots" / "shot_01"
            shot_dir.mkdir(parents=True)
            source = shot_dir / "source.mp4"
            source.write_bytes(b"video")
            performance_path = shot_dir / "performance.json"
            automatic = {
                "has_dialogue": True,
                "dialogue": [{"speaker_slot": 1, "start": 0, "end": 1, "text": "旧台词"}],
                "performance": [{"actor_slot": 1, "start": 0, "end": 1, "core_intent": "旧表演"}],
            }
            performance_path.write_text(json.dumps(automatic, ensure_ascii=False), encoding="utf-8")
            job = web_app.WebJob(
                id="manual-performance-edit",
                kind="long_performance",
                project="long_video_replication",
                run_dir=directory,
                status="succeeded",
                shots=[{
                    "index": 1,
                    "source_path": str(source),
                    "performance_path": str(performance_path),
                    "performance": automatic,
                }],
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                response = self.client.put(
                    f"/api/long-video/{job.id}/shots/1/performance",
                    json={
                        "dialogue_text": "[0.10–0.90秒] P1：新台词",
                        "performance_text": "[0.00–1.00秒] P1：坚定地注视",
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                shot = payload["shots"][0]
                self.assertTrue(shot["performance_dirty"])
                self.assertTrue(shot["performance"]["manual_reviewed"])
                saved = json.loads(performance_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["manual_dialogue_text"], "[0.10–0.90秒] P1：新台词")
                self.assertEqual(saved["manual_performance_text"], "[0.00–1.00秒] P1：坚定地注视")
                self.assertTrue(job.performance_path.is_file())
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    def test_long_video_can_delete_one_shot_without_deleting_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shots = []
            for index in (1, 2):
                shot_dir = directory / f"shot_{index:02d}"
                shot_dir.mkdir()
                source = shot_dir / "source.mp4"
                source.write_bytes(f"source-{index}".encode())
                shots.append(
                    {
                        "index": index,
                        "start": float(index - 1),
                        "end": float(index),
                        "duration": 1.0,
                        "source_path": str(source),
                        "performance": {
                            "has_dialogue": False,
                            "dialogue": [],
                            "performance": [],
                            "audio_summary": "安静",
                        },
                    }
                )
            mosaic = directory / "人脸打码视频.mp4"
            white_model = directory / "白模绿幕视频.mp4"
            output = directory / "长视频多分镜复刻成片.mp4"
            for path in (mosaic, white_model, output):
                path.write_bytes(b"old-merged-output")
            job = web_app.WebJob(
                id="delete-one-shot",
                kind="long_white_model",
                project="long_video_replication",
                run_dir=directory,
                status="failed",
                shots=shots,
                mosaic_path=mosaic,
                white_model_path=white_model,
                output_path=output,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                response = self.client.delete("/api/long-video/delete-one-shot/shots/1")
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["shot_count"], 1)
                self.assertEqual(payload["shots"][0]["index"], 2)
                self.assertEqual(job.kind, "long_edit")
                self.assertEqual(job.status, "succeeded")
                self.assertIsNone(job.mosaic_path)
                self.assertIsNone(job.white_model_path)
                self.assertIsNone(job.output_path)
                self.assertTrue((directory / "shot_01" / "source.mp4").is_file())
                self.assertTrue(any("原分镜文件仍保留" in message for message in job.logs))
                summary = json.loads((directory / "performance_summary.json").read_text(encoding="utf-8"))
                self.assertEqual([item["index"] for item in summary["shots"]], [2])
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    def test_long_video_rejects_deleting_the_last_shot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "source.mp4"
            source.write_bytes(b"only-shot")
            job = web_app.WebJob(
                id="delete-last-shot",
                kind="long_analyze",
                project="long_video_replication",
                run_dir=directory,
                status="succeeded",
                shots=[{"index": 1, "source_path": str(source)}],
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                response = self.client.delete("/api/long-video/delete-last-shot/shots/1")
                self.assertEqual(response.status_code, 400)
                self.assertIn("至少需要保留一个分镜", response.get_json()["error"])
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    def test_all_six_projects_load_shared_clickable_upload_previews(self) -> None:
        for path in (
            "/projects/single",
            "/projects/multi",
            "/projects/person",
            "/projects/scene",
            "/projects/clothing",
            "/projects/long-video",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn('/static/upload_preview.js?v=20260808-1', response.get_data(as_text=True))
                response.close()

        response = self.client.get("/static/upload_preview.js")
        self.assertEqual(response.status_code, 200)
        script = response.get_data(as_text=True)
        self.assertIn("URL.createObjectURL", script)
        self.assertIn("upload-preview-modal", script)
        self.assertIn("scene-library-image", script)
        response.close()

    def test_long_video_prompt_keeps_character_and_clothing_consistent(self) -> None:
        prompt = web_app.build_long_shot_prompt(
            [
                {"id": 1, "role": "画面左侧人物"},
                {"id": 2, "role": "画面右侧人物"},
            ],
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
        )
        self.assertIn("@视频1", prompt)
        self.assertIn("@图片1", prompt)
        self.assertIn("@图片2", prompt)
        self.assertIn("@图片4", prompt)
        self.assertIn("@图片5", prompt)
        self.assertIn("恰好保留 2 位出场人物", prompt)
        self.assertIn("跨分镜保持同一人物身份", prompt)
        self.assertIn("Seedance根据新人物形象生成全新的角色音色", prompt)
        self.assertIn("不得模仿、复制或保留原片人物的声纹", prompt)

    def test_final_long_shot_prompt_compacts_duplicate_audio_rules_before_timeline(self) -> None:
        styled, _ = web_app.apply_final_style_prompt(
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
            "ultra_realistic",
        )
        compact_common = web_app.compact_long_generation_constraints(styled)
        actors = [
            {
                "id": 2,
                "role": "画面第二人物",
                "source_slot": 1,
                "position_anchor": "位于画面左侧，以侧身姿态站立，面向右侧女性，穿着黑色西装",
            },
            {
                "id": 1,
                "role": "画面主要人物",
                "source_slot": 2,
                "position_anchor": "位于画面右侧，仰头站立，面向左侧男性，穿着蕾丝婚纱",
            },
        ]
        base = web_app.build_long_shot_prompt(
            actors,
            compact_common,
            replace_scene=True,
            white_model_reference=True,
            real_person_mode=True,
        )
        performance = web_app.build_performance_prompt(
            {
                "has_dialogue": False,
                "dialogue": [],
                "manual_dialogue_text": "",
                "manual_performance_text": (
                    "[0.00–1.93秒] P1：保持凝视；可见证据：位于画面左侧，侧身站立，穿着黑色西装\n"
                    "[0.00–1.93秒] P2：仰头注视；可见证据：位于画面右侧，仰头站立，穿着蕾丝婚纱"
                ),
            },
            generate_new_voice=True,
            compact=True,
            max_chars=10000,
        )
        prompt = f"{base}\n{web_app.seedance_hold_timing_prompt(1.93, 4)}\n{performance}"
        self.assertLessEqual(len(prompt), 1980)
        self.assertIn("本镜无台词", prompt)
        self.assertIn("唯一逐帧依据", prompt)
        self.assertNotIn("有台词时由Seedance", prompt)
        self.assertNotIn("黑色西装", performance)
        self.assertNotIn("蕾丝婚纱", performance)

    def test_real_final_prompt_fits_current_two_actor_manual_performance_without_truncation(self) -> None:
        styled, _ = web_app.apply_final_style_prompt(
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
            "match_character",
        )
        actors = [
            {
                "id": 2,
                "role": "画面第二人物",
                "source_slot": 1,
                "source_character_id": 2,
                "position_anchor": (
                    "占据画面主体，是出镜的女生；初始闭着双眼，头部缓慢抬起，随后睁开眼睛望向前方的人物，"
                    "颈部随抬头动作自然伸展，全程保持上半身在画面中，无大幅度位移"
                ),
            },
            {
                "id": 1,
                "role": "画面主要人物",
                "source_slot": 2,
                "source_character_id": 1,
                "position_anchor": (
                    "在画面左侧前景，只露出部分头部与肩背，处于虚化状态，全程静止未发生位移，面向右侧的女生"
                ),
            },
        ]
        base = web_app.build_long_shot_prompt(
            actors,
            web_app.compact_long_generation_constraints(styled, real_person_mode=True),
            replace_scene=True,
            white_model_reference=True,
            real_person_mode=True,
        )
        performance = web_app.build_performance_prompt(
            {
                "has_dialogue": False,
                "dialogue": [],
                "manual_dialogue_text": "",
                "manual_performance_text": (
                    "[0.00–3.73秒] P1：女生从低头闭目状态缓缓抬头，睁开眼后望向前方人物，颈部线条自然舒展；"
                    "可见证据：占据画面主体，是出镜的女生；初始闭着双眼，头部缓慢抬起，随后睁开眼睛望向前方的人物，"
                    "颈部随抬头动作自然伸展，全程保持上半身在画面中，无大幅度位移；避免误读：没有开口说话的动作\n"
                    "[0.00–3.73秒] P2：保持静止遮挡状态面对女生，无额外动作；可见证据：在画面左侧前景，"
                    "只露出部分头部与肩背，处于虚化状态，全程静止未发生位移，面向右侧的女生；"
                    "避免误读：没有做出主动互动动作，没有开口，没有离开画面"
                ),
            },
            generate_new_voice=True,
            compact=True,
            max_chars=10000,
        )
        prompt = (
            f"{base}\n"
            f"{web_app.seedance_hold_timing_prompt(3.73, 4, compact=True)}\n"
            f"{performance}"
        )
        self.assertLessEqual(len(prompt), 1980)
        self.assertIn("P1：女生从低头闭目状态缓缓抬头", prompt)
        self.assertIn("P2：保持静止遮挡状态面对女生", prompt)
        self.assertIn("火山角色库人物@图片1只能对应表演槽位P1", prompt)
        self.assertIn("火山角色库人物@图片3只能对应表演槽位P2", prompt)

    def test_virtual_long_prompt_compaction_is_unchanged_by_real_person_budget_fix(self) -> None:
        styled, _ = web_app.apply_final_style_prompt(
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
            "match_character",
        )
        virtual_compact = web_app.compact_long_generation_constraints(styled)
        self.assertIn(web_app.NO_VISIBLE_TEXT_CONSTRAINT, virtual_compact)
        self.assertIn("成片风格约束：", virtual_compact)
        self.assertNotIn("真实人物参考共用规则", virtual_compact)

    def test_real_whole_video_prompt_is_text_free_and_ignores_white_model_hair(self) -> None:
        styled, _ = web_app.apply_final_style_prompt(
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
            "ultra_realistic",
        )
        prompt = web_app.build_real_whole_video_prompt(
            [
                {"id": 1, "role": "画面主要人物"},
                {"id": 2, "role": "画面第二人物"},
            ],
            [
                {
                    "index": 1,
                    "start": 0.0,
                    "actor_ids": [2, 1],
                    "actor_mappings": [
                        {"slot": 1, "actor_id": 2, "source_position": "画面左侧前景，局部露出"},
                        {"slot": 2, "actor_id": 1, "source_position": "画面右侧主体，完整露出"},
                    ],
                    "performance": {
                        "manual_dialogue_text": "[0.20–1.30秒] P1：请问需要咖啡吗？",
                    },
                },
            ],
            styled,
        )
        self.assertLessEqual(len(prompt), web_app.SEEDANCE_SAFE_PROMPT_LIMIT)
        self.assertTrue(prompt.startswith(web_app.REAL_WHOLE_NO_VISIBLE_TEXT_CONSTRAINT))
        self.assertIn("最终全片每一帧都必须是无字幕母版", prompt)
        self.assertIn("白模中即使出现头发形状也只视为错误占位", prompt)
        self.assertIn("最终发型只能来自对应火山角色@图片", prompt)
        self.assertIn("每个切点前后的首尾帧都必须对应@视频1", prompt)
        self.assertIn("@图片1是画面主要人物的火山角色身份", prompt)
        self.assertIn("@图片3是画面第二人物的火山角色身份", prompt)
        self.assertIn("最终检查：全片零叠加字幕、零标题、零文字层", prompt)
        self.assertIn("逐镜构图硬锁", prompt)
        self.assertIn("逐镜人物身份硬锁", prompt)
        self.assertIn("S1:P1[画面左侧前景，局部露出]=人物2@图片3", prompt)
        self.assertIn("P2[画面右侧主体，完整露出]=人物1@图片1", prompt)
        self.assertIn("严禁换脸、换装、互换台词", prompt)

    def test_real_whole_video_prompt_keeps_different_cast_mapping_per_shot(self) -> None:
        prompt = web_app.build_real_whole_video_prompt(
            [
                {"id": 1, "role": "人物一"},
                {"id": 2, "role": "人物二"},
            ],
            [
                {
                    "index": 1,
                    "start": 0.0,
                    "actor_mappings": [
                        {"slot": 1, "actor_id": 2, "source_position": "左侧近景"},
                        {"slot": 2, "actor_id": 1, "source_position": "右侧远景"},
                    ],
                },
                {
                    "index": 2,
                    "start": 2.0,
                    "actor_mappings": [
                        {"slot": 1, "actor_id": 1, "source_position": "中央特写"},
                    ],
                },
            ],
            "",
        )
        self.assertIn(
            "S1:P1[左侧近景]=人物2@图片3,P2[右侧远景]=人物1@图片1",
            prompt,
        )
        self.assertIn("S2:P1[中央特写]=人物1@图片1", prompt)
        self.assertNotIn("S2:P1[中央特写]=人物2@图片3", prompt)

    def test_real_long_mapping_ui_changes_only_the_current_shot(self) -> None:
        response = self.client.get("/static/real_long_video.js")
        self.assertEqual(response.status_code, 200)
        script = response.get_data(as_text=True)
        self.assertIn("其他分镜保持不变", script)
        self.assertIn("人工选择只作用于本镜", script)
        self.assertIn("manual_identity_override: state.manualCasts.has", script)
        self.assertNotIn("已在全部分镜统一映射为", script)
        self.assertNotIn("function continuityActorMap()", script)
        self.assertIn("data-skip-shot", script)
        self.assertIn("data-restore-skipped-shot", script)
        self.assertIn("skip_shot_index", script)
        self.assertIn("恢复并重新生成此镜", script)
        self.assertIn('generationForm("selected", [Number(index)], false)', script)
        self.assertNotIn("const voiceModeReady = shot?.dialogue_voice_mode", script)
        response.close()

    def test_real_manual_identity_override_is_parsed_per_shot(self) -> None:
        job = web_app.WebJob(
            id="real-per-shot-identity",
            kind="real_long_generate",
            project=web_app.REAL_PERSON_LONG_PROJECT,
            run_dir=Path("."),
            shots=[{"index": 1}, {"index": 2}],
        )
        shot_casts = json.dumps([
            {"index": 1, "manual_identity_override": False},
            {"index": 2, "manual_identity_override": True},
        ])
        with web_app.app.test_request_context(
            "/api/real-long-video/generate",
            method="POST",
            data={"shot_casts": shot_casts},
        ):
            self.assertEqual(
                web_app.parse_real_long_manual_identity_override_shots(job),
                {2},
            )

    def test_virtual_mapping_script_keeps_its_existing_global_behavior_file_untouched(self) -> None:
        real_script = Path("web/real_long_video.js").read_text(encoding="utf-8")
        virtual_script = Path("web/long_video.js").read_text(encoding="utf-8")
        self.assertIn("人工选择只作用于本镜", real_script)
        self.assertNotIn("人工选择只作用于本镜", virtual_script)
        self.assertIn("data-skip-shot", real_script)
        self.assertNotIn("data-skip-shot", virtual_script)

    def test_real_long_pause_endpoint_is_project_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            real_job = web_app.WebJob(
                id="real-pause-job",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=Path(temporary),
                status="running",
                shots=[{"index": 1, "status": "running"}],
            )
            sub_job = web_app.WebJob(
                id="real-pause-job-s01",
                kind="real_long_shot",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=Path(temporary),
                status="running",
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[real_job.id] = real_job
                web_app.JOBS[sub_job.id] = sub_job
            try:
                response = self.client.post(
                    "/api/real-long-video/pause",
                    json={"source_job_id": real_job.id},
                )
                self.assertEqual(response.status_code, 202)
                self.assertTrue(real_job.pause_requested)
                self.assertTrue(sub_job.pause_requested)
                self.assertIn("暂停请求", real_job.stage)
                self.assertEqual(self.client.post("/api/long-video/pause").status_code, 404)
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(real_job.id, None)
                    web_app.JOBS.pop(sub_job.id, None)

    def test_restore_skipped_real_shot_is_free_and_invalidates_old_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old_output = directory / "old.mp4"
            old_output.write_bytes(b"old")
            merged = directory / "merged.mp4"
            merged.write_bytes(b"merged")
            job = web_app.WebJob(
                id="restore-skipped-real-shot",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                status="awaiting_approval",
                output_path=merged,
                shots=[{
                    "index": 1,
                    "generation_skipped": True,
                    "composition_approval_required": False,
                    "output_path": str(old_output),
                    "output_signature": "old-signature",
                    "status": "skipped",
                }],
            )
            with patch.dict(web_app.JOBS, {job.id: job}, clear=True):
                response = self.client.post(
                    f"/api/real-long-video/{job.id}/shots/1/restore"
                )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertFalse(payload["shots"][0]["generation_skipped"])
            self.assertEqual(payload["shots"][0]["output_signature"], "")
            self.assertEqual(payload["shots"][0]["stage"], "已恢复，等待重新生成")
            self.assertFalse(payload["has_output"])

    def test_manual_white_model_approval_is_free_and_unblocks_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.mp4"
            white = directory / "white.mp4"
            source.write_bytes(b"source")
            white.write_bytes(b"white")
            job = web_app.WebJob(
                id="approve-white-model",
                kind="real_long_white_model",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                status="awaiting_approval",
                shots=[{
                    "index": 1,
                    "source_path": str(source),
                    "white_model_path": str(white),
                    "white_model_qa": {"passed": False, "reasons": ["minor drift"]},
                    "white_model_approval_required": True,
                    "status": "awaiting_approval",
                }],
            )
            with patch.dict(web_app.JOBS, {job.id: job}, clear=True), patch(
                "web_app.rebuild_real_long_white_model_merge", return_value=white
            ):
                response = self.client.post(
                    f"/api/real-long-video/{job.id}/shots/1/approve-white-model"
                )
            self.assertEqual(response.status_code, 200)
            shot = response.get_json()["shots"][0]
            self.assertTrue(shot["white_model_manually_approved"])
            self.assertFalse(shot["white_model_approval_required"])
            self.assertEqual(shot["stage"], "白模已人工确认可用")

    def test_white_model_with_visible_text_cannot_be_manually_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.mp4"
            white = directory / "white.mp4"
            source.write_bytes(b"source")
            white.write_bytes(b"white")
            job = web_app.WebJob(
                id="reject-text-white-model",
                kind="real_long_white_model",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                status="awaiting_approval",
                shots=[{
                    "index": 1,
                    "source_path": str(source),
                    "white_model_path": str(white),
                    "white_model_qa": {
                        "passed": False,
                        "reasons": ["白模多帧检测到疑似字幕、时间码、Logo或文字"],
                    },
                    "white_model_approval_required": True,
                    "status": "awaiting_approval",
                }],
            )
            with patch.dict(web_app.JOBS, {job.id: job}, clear=True):
                response = self.client.post(
                    f"/api/real-long-video/{job.id}/shots/1/approve-white-model"
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn("不可人工放行", response.get_json()["error"])
            self.assertFalse(web_app.real_long_white_model_is_approved(job.shots[0]))
            self.assertFalse(bool(job.shots[0].get("white_model_manually_approved")))

    def test_text_region_mask_expands_ocr_box_to_cover_subtitle_outline(self) -> None:
        frame = np.zeros((200, 320, 3), dtype=np.uint8)
        mask = web_app._text_region_mask(
            frame,
            [{"box": [[100, 150], [220, 150], [220, 170], [100, 170]]}],
        )
        self.assertGreater(int(np.count_nonzero(mask)), 120 * 20)
        self.assertEqual(int(mask[160, 160]), 255)

    @patch("web_app.threading.Thread")
    def test_real_white_model_retry_after_two_attempts_requires_renewed_cost_confirmation(
        self,
        mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.mp4"
            mosaic = directory / "mosaic.mp4"
            white = directory / "white.mp4"
            for path in (source, mosaic, white):
                path.write_bytes(b"video")
            job = web_app.WebJob(
                id="confirm-third-white-attempt",
                kind="real_long_white_model",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                status="awaiting_approval",
                shots=[{
                    "index": 1,
                    "source_path": str(source),
                    "mosaic_path": str(mosaic),
                    "mosaic_reviewed": True,
                    "white_model_path": str(white),
                    "white_model_task_id": "second-task",
                    "white_model_retry_count": 1,
                    "white_model_approval_required": True,
                }],
            )
            request_data = {
                "source_job_id": job.id,
                "privacy_review_confirmed": "true",
                "white_regeneration_mode": "selected",
                "force_white_shots": "[1]",
            }
            with patch.dict(web_app.JOBS, {job.id: job}, clear=True):
                rejected = self.client.post(
                    "/api/real-long-video/white-model",
                    data=request_data,
                    content_type="multipart/form-data",
                )
                self.assertEqual(rejected.status_code, 400)
                self.assertIn("费用确认", rejected.get_json()["error"])
                accepted = self.client.post(
                    "/api/real-long-video/white-model",
                    data={**request_data, "white_retry_cost_confirmed": "true"},
                    content_type="multipart/form-data",
                )
            self.assertEqual(accepted.status_code, 202)
            thread_kwargs = mock_thread.call_args.kwargs["kwargs"]
            self.assertEqual(thread_kwargs["confirmed_paid_retry_indices"], {1})
            self.assertTrue(any("只会新增 1 次付费任务" in message for message in job.logs))
            rejected.close()
            accepted.close()

    def test_real_long_page_exposes_pause_button_only_in_real_script(self) -> None:
        page_response = self.client.get("/projects/real-long-video")
        real_script_response = self.client.get("/static/real_long_video.js")
        virtual_script_response = self.client.get("/static/long_video.js")
        html = page_response.get_data(as_text=True)
        real_script = real_script_response.get_data(as_text=True)
        virtual_script = virtual_script_response.get_data(as_text=True)
        self.assertIn('id="pauseGenerateBtn"', html)
        self.assertIn("async function pauseGeneration()", real_script)
        self.assertNotIn("pauseGenerateBtn", virtual_script)
        page_response.close()
        real_script_response.close()
        virtual_script_response.close()

    def test_role_description_limit_is_200_characters(self) -> None:
        role = "人" * 200
        self.assertEqual(web_app._clean_long_role(role, 1), role)
        self.assertIn(role, web_app.build_multi_prompt([role, "第二人物"]))
        with self.assertRaisesRegex(web_app.WorkflowError, "不能超过 200 个字符"):
            web_app._clean_long_role("人" * 201, 1)
        with self.assertRaisesRegex(web_app.WorkflowError, "不能超过 200 个字符"):
            web_app.build_multi_prompt(["人" * 201, "第二人物"])

    def test_long_video_prompt_can_replace_scene_instead_of_preserving_it(self) -> None:
        prompt = web_app.build_long_shot_prompt(
            [{"id": 1, "role": "本分镜左侧原人物"}],
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
            replace_scene=True,
        )
        self.assertIn("替换为@图片3中的新场景", prompt)
        self.assertIn("镜头机位、景别、透视、裁切和构图不得改变", prompt)
        self.assertIn("不得残留、恢复或复用原场景内容", prompt)
        self.assertIn("人物身份与空间位置绑定为最高优先级", prompt)
        self.assertIn("严禁人物交换位置、交换动作、交换台词", prompt)

    def test_white_model_is_the_only_final_composition_and_camera_master(self) -> None:
        prompt = web_app.build_long_shot_prompt(
            [
                {"id": 2, "role": "左侧前景局部人物", "source_slot": 1, "position_anchor": "左侧极近前景虚焦，只露头肩"},
                {"id": 1, "role": "右侧清晰人物", "source_slot": 2, "position_anchor": "右侧中近景清晰人物"},
            ],
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
            replace_scene=True,
            white_model_reference=True,
        )
        self.assertIn("唯一逐帧依据", prompt)
        self.assertIn("只在前景露出模糊头肩、背影或身体局部", prompt)
        self.assertIn("人物图只提供身份", prompt)
        self.assertIn("场景图只提供环境外观", prompt)
        self.assertIn("禁止重新构图", prompt)
        self.assertIn("改变运镜", prompt)

    def test_long_video_prompt_keeps_long_position_anchor_separate_from_role_limit(self) -> None:
        position = (
            "站在打开的门后，一只手端着茶杯托盘，另一只手轻抬后放在杯柄上，"
            "面部带着柔和的妆容，嘴部配合说话运动，视线朝前，整体姿态带着探身招呼的感觉"
        )
        prompt = web_app.build_long_shot_prompt(
            [{
                "id": 1,
                "role": "画面主要人物",
                "source_slot": 1,
                "position_anchor": position,
            }],
            web_app.DEFAULT_LONG_VIDEO_PROMPT,
            replace_scene=True,
        )
        self.assertIn("表演槽位P1「画面主要人物」", prompt)
        self.assertIn(f"空间动作锚点固定为「{position}」", prompt)

    def test_long_shot_uses_performance_evidence_as_identity_position_anchor(self) -> None:
        shot = {
            "person_slots": [{"position": "错误的检测位置"}],
            "performance": {
                "performance": [{
                    "actor_slot": 1,
                    "core_intent": "递咖啡",
                    "visible_evidence": "站在右侧前景，手持咖啡杯面向左侧内景",
                }]
            },
        }
        self.assertEqual(
            web_app.long_shot_performance_slot_anchor(shot, 1),
            "站在右侧前景，手持咖啡杯面向左侧内景",
        )

    def test_incomplete_performance_analysis_falls_back_to_stable_detector_slots(self) -> None:
        shot = {
            "suggested_actor_count": 2,
            "sample_actor_counts": [2, 2, 1, 1, 1],
            "person_slots": [{"position": "左侧前景局部人物"}, {"position": "右侧人物"}],
            "performance": {"performance": [{"actor_slot": 1, "visible_evidence": "只分析到右侧人物"}]},
        }
        self.assertEqual(web_app.long_shot_stable_detected_people_count(shot), 2)
        self.assertEqual(web_app.long_shot_performance_slot_anchor(shot, 1), "左侧前景局部人物")
        self.assertEqual(web_app.long_shot_performance_slot_anchor(shot, 2), "右侧人物")

    def test_single_detector_spike_does_not_create_a_phantom_actor(self) -> None:
        shot = {
            "suggested_actor_count": 3,
            "sample_actor_counts": [2, 2, 2, 3, 2],
            "person_slots": [{"position": "左侧人物"}, {"position": "中间人物"}, {"position": "右侧误检"}],
        }
        self.assertEqual(web_app.long_shot_stable_detected_people_count(shot), 2)

    def test_cross_shot_identity_conflict_blocks_paid_generation(self) -> None:
        job = web_app.WebJob(
            id="cast-check",
            kind="long_edit",
            run_dir=Path("."),
            cast_continuity={
                "assignments": [
                    {"shot_index": 1, "slot": 1, "character_id": 2},
                    {"shot_index": 2, "slot": 1, "character_id": 2},
                ]
            },
        )
        with self.assertRaisesRegex(web_app.WorkflowError, "跨镜人物身份发生互换"):
            web_app.validate_long_cast_continuity(job, {1: [2], 2: [1]})

    def test_confirmed_manual_casts_override_missing_or_conflicting_c_identity(self) -> None:
        missing_job = web_app.WebJob(id="manual-no-c", kind="long_edit", run_dir=Path("."))
        warnings = web_app.validate_long_cast_continuity(
            missing_job,
            {1: [1, 2], 2: [2, 1]},
            allow_manual_override=True,
        )
        self.assertIn("采用人工逐镜人物映射", warnings[0])

        conflict_job = web_app.WebJob(
            id="manual-conflict-c",
            kind="long_edit",
            run_dir=Path("."),
            cast_continuity={
                "assignments": [
                    {"shot_index": 1, "slot": 1, "character_id": 1},
                    {"shot_index": 2, "slot": 1, "character_id": 1},
                ]
            },
        )
        warnings = web_app.validate_long_cast_continuity(
            conflict_job,
            {1: [1], 2: [2]},
            allow_manual_override=True,
        )
        self.assertTrue(any("以人工逐镜选择为准" in warning for warning in warnings))

    def test_composition_qa_rejects_severe_closeup_to_fullbody_drift(self) -> None:
        source = {
            "frames": [{"faces": [{"center_x": 0.3}, {"center_x": 0.7}]}],
            "median_face_height": 0.34,
            "median_face_area": 0.1,
            "median_face_count": 2,
        }
        output = {
            "frames": [{"faces": [{"center_x": 0.42}, {"center_x": 0.59}]}],
            "median_face_height": 0.05,
            "median_face_area": 0.003,
            "median_face_count": 2,
        }
        with patch("web_app._sample_face_scale_profile", side_effect=[source, output]), patch(
            "web_app._sample_motion_profile", side_effect=[0.005, 0.004]
        ):
            result = web_app.validate_final_composition(Path("source.mp4"), Path("output.mp4"), expected_actor_count=2)
        self.assertFalse(result["passed"])
        self.assertIn("景别发生严重变化", "；".join(result["reasons"]))

    def test_white_model_gate_rejects_text_and_colored_clothing(self) -> None:
        people = {
            "frames": [{"people": [{"center_x": 0.5, "center_y": 0.5, "area": 0.3}]}],
            "median_count": 1,
            "max_count": 1,
            "median_largest_area": 0.3,
        }
        with patch("web_app._sample_person_layout_profile", side_effect=[people, people]), patch(
            "web_app._sample_motion_profile", side_effect=[0.005, 0.004]
        ), patch(
            "web_app._sample_global_motion_vector",
            side_effect=[{"x": 1.0, "y": 0.0, "magnitude": 1.0}, {"x": 0.9, "y": 0.0, "magnitude": 0.9}],
        ), patch(
            "web_app._sample_white_model_profile",
            return_value={
                "frames": [],
                "median_green_ratio": 0.45,
                "median_subject_ratio": 0.55,
                "median_neutral_bright_ratio": 0.45,
                "median_colored_subject_ratio": 0.3,
            },
        ), patch(
            "web_app._sample_text_profile",
            return_value={"detected": True, "suspected": False, "ocr_available": True, "frames": []},
        ):
            result = web_app.validate_white_model(
                Path("source.mp4"), Path("white.mp4"), expected_actor_count=1
            )
        self.assertFalse(result["passed"])
        reasons = "；".join(result["reasons"])
        self.assertIn("彩色内容", reasons)
        self.assertIn("文字", reasons)

    def test_white_model_gate_allows_small_geometry_difference(self) -> None:
        source_people = {
            "frames": [{"people": [{"center_x": 0.48, "center_y": 0.5, "area": 0.3}]}],
            "median_count": 1,
            "max_count": 1,
            "median_largest_area": 0.3,
        }
        white_people = {
            "frames": [{"people": [{"center_x": 0.53, "center_y": 0.51, "area": 0.34}]}],
            "median_count": 1,
            "max_count": 1,
            "median_largest_area": 0.34,
        }
        with patch("web_app._sample_person_layout_profile", side_effect=[source_people, white_people]), patch(
            "web_app._sample_motion_profile", side_effect=[0.005, 0.004]
        ), patch(
            "web_app._sample_global_motion_vector",
            side_effect=[{"x": 1.0, "y": 0.0, "magnitude": 1.0}, {"x": 0.9, "y": 0.1, "magnitude": 0.91}],
        ), patch(
            "web_app._sample_white_model_profile",
            return_value={
                "frames": [],
                "median_green_ratio": 0.45,
                "median_subject_ratio": 0.55,
                "median_neutral_bright_ratio": 0.9,
                "median_colored_subject_ratio": 0.02,
            },
        ), patch(
            "web_app._sample_text_profile",
            return_value={"detected": False, "suspected": False, "ocr_available": True, "frames": []},
        ):
            result = web_app.validate_white_model(
                Path("source.mp4"), Path("white.mp4"), expected_actor_count=1
            )
        self.assertTrue(result["passed"])

    def test_reference_text_sanitizer_inpaints_ocr_regions(self) -> None:
        image = np.full((160, 240, 3), 60, dtype=np.uint8)
        image[80:115, 90:170] = 245
        cv2.putText(image, "STAFF", (95, 105), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (10, 10, 10), 2)
        with patch(
            "web_app._rapid_ocr_regions",
            return_value=[{
                "text": "STAFF",
                "confidence": 0.99,
                "box": [[90, 80], [170, 80], [170, 115], [90, 115]],
            }],
        ):
            sanitized, removed = web_app.sanitize_reference_badges(image)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(np.array_equal(image, sanitized))

    def test_scene_plate_signature_is_unique_to_shot_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            scene = directory / "scene.png"
            source = directory / "source.mp4"
            motion_one = directory / "white-one.mp4"
            motion_two = directory / "white-two.mp4"
            scene.write_bytes(b"scene")
            source.write_bytes(b"source")
            motion_one.write_bytes(b"motion-one")
            motion_two.write_bytes(b"motion-two")
            first = web_app.long_scene_plate_signature(
                shot_index=1,
                scene_source=str(scene),
                source_reference=str(source),
                motion_reference=str(motion_one),
                prompt="scene prompt",
                model="seedream",
            )
            other_shot = web_app.long_scene_plate_signature(
                shot_index=2,
                scene_source=str(scene),
                source_reference=str(source),
                motion_reference=str(motion_one),
                prompt="scene prompt",
                model="seedream",
            )
            other_motion = web_app.long_scene_plate_signature(
                shot_index=1,
                scene_source=str(scene),
                source_reference=str(source),
                motion_reference=str(motion_two),
                prompt="scene prompt",
                model="seedream",
            )
        self.assertNotEqual(first, other_shot)
        self.assertNotEqual(first, other_motion)

    def test_composition_correction_prompt_uses_measured_scale(self) -> None:
        prompt = web_app.build_composition_correction_prompt(
            {
                "face_scale_ratio": 0.5,
                "reasons": ["人物脸部尺度仅为原片的 0.50 倍，景别发生严重变化"],
            },
            retry_number=1,
        )
        self.assertIn("构图纠偏第1次", prompt)
        self.assertIn("放大约2.00倍", prompt)
        self.assertIn("人物图仍只负责长相", prompt)

    def test_real_composition_correction_targets_white_model_not_unseen_previous_output(self) -> None:
        prompt = web_app.build_composition_correction_prompt(
            {
                "face_scale_ratio": 0.42,
                "reasons": ["人物脸部尺度仅为原片的 0.42 倍，景别发生严重变化"],
            },
            retry_number=3,
            absolute_white_model=True,
        )
        self.assertIn("恢复到@视频1的100%人物尺度", prompt)
        self.assertIn("@视频1只提供白模人物的逐帧动作", prompt)
        self.assertIn("场景参考图只提供当前分镜的新环境外观", prompt)
        self.assertNotIn("相对上一版放大", prompt)

    def test_real_prompt_keeps_white_model_video_and_scene_image_separate(self) -> None:
        prompt = web_app.build_long_shot_prompt(
            [
                {"id": 1, "role": "左侧前景男人", "source_slot": 1, "position_anchor": "左侧近前景，只露头肩"},
                {"id": 2, "role": "右侧女人", "source_slot": 2, "position_anchor": "右侧中景站立"},
            ],
            web_app.compact_long_generation_constraints(
                web_app.DEFAULT_LONG_VIDEO_PROMPT,
                real_person_mode=True,
            ),
            replace_scene=True,
            white_model_reference=True,
            real_person_mode=True,
        )
        self.assertIn("最高优先级白模母版规则", prompt)
        self.assertIn("@图片4只提供服装", prompt)
        self.assertIn("@图片5中的新场景", prompt)
        self.assertIn("只是场景参考，不做必要背景", prompt)
        self.assertIn("将本分镜全部背景", prompt)
        self.assertLess(len(prompt), web_app.SEEDANCE_SAFE_PROMPT_LIMIT)

    def test_real_generation_submits_white_model_video_and_separate_scene_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shot_dir = directory / "shot_01"
            shot_dir.mkdir()
            paths = {
                name: shot_dir / filename
                for name, filename in {
                    "source": "source.mp4",
                    "depth": "depth.mp4",
                    "white": "white.mp4",
                    "target": "target.jpg",
                    "scene": "scene_plate.jpg",
                    "clothing": "clothing.jpg",
                }.items()
            }
            for path in paths.values():
                path.write_bytes(b"asset")
            actor = {
                "id": 1,
                "role": "画面主要人物",
                "person_source": "asset://asset-test123",
                "original_person_source": "asset://asset-test123",
                "trusted_asset_uri": "asset://asset-test123",
                "clothing_source": str(paths["clothing"]),
            }
            job = web_app.WebJob(
                id="real-composite-routing",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                actors=[actor],
                shots=[{
                    "index": 1,
                    "start": 0.0,
                    "end": 5.0,
                    "duration": 5.0,
                    "source_path": str(paths["source"]),
                    "depth_path": str(paths["depth"]),
                    "white_model_path": str(paths["white"]),
                    "target_scene_path": str(paths["target"]),
                    "scene_path": str(paths["scene"]),
                    "scene_plate_signature": "scene-signature",
                    "requested_scene_signature": "scene-signature",
                    "requested_signature": "video-signature",
                    "actor_ids": [1],
                    "person_slots": [{"position": "画面中央近景人物"}],
                    "performance": {"has_dialogue": False, "dialogue": [], "performance": []},
                    "composition_approval_required": True,
                    "composition_retry_count": 2,
                }],
            )

            def finish_generation(sub_job: web_app.WebJob, **kwargs) -> None:
                self.assertTrue(kwargs["include_scene_reference"])
                self.assertEqual(Path(kwargs["depth_path"]), paths["white"])
                self.assertEqual(Path(kwargs["scene_source"]), paths["scene"])
                self.assertIn("@图片3中的新场景", kwargs["options"]["prompt"])
                self.assertEqual(kwargs["options"]["ratio"], "adaptive")
                self.assertEqual(kwargs["options"]["duration"], -1)
                output = shot_dir / "generated.mp4"
                output.write_bytes(b"video")
                sub_job.update(status="succeeded", output_path=output, task_id="task-real-control")

            def conform(_source: Path, target: Path, _duration: float, **_kwargs) -> Path:
                target.write_bytes(b"conformed")
                return target

            def concatenate(_paths: list[Path], target: Path) -> Path:
                target.write_bytes(b"merged")
                return target

            try:
                with (
                    patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                    patch("web_app.real_person_shot_character_sources", return_value=([
                        ("asset://asset-test123", str(paths["clothing"]))
                    ], False)),
                    patch("web_app.run_multi_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=conform),
                    patch("web_app.concatenate_videos", side_effect=concatenate),
                    patch("web_app.validate_final_composition", return_value={"passed": True, "reasons": []}),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_video_generation(
                        job,
                        actors=[actor],
                        scene_prompt="scene",
                        image_model="image-model",
                        options={
                            "prompt": web_app.DEFAULT_LONG_VIDEO_PROMPT,
                            "model": web_app.DEFAULT_SEEDANCE_25_MODEL,
                            "resolution": "720p",
                            "ratio": "16:9",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                            "preserve_original_audio": False,
                        },
                        blur_range="",
                        force_shot_indices={1},
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("real-composite-routing-s01", None)
            mock_generate.assert_called_once()
            self.assertEqual(job.shots[0]["generation_mode"], "real_character_asset")
            self.assertEqual(job.shots[0]["control_reference_path"], "")
            self.assertEqual(job.status, "succeeded")

    def test_real_scene_geometry_prompt_marks_closeup_and_forbids_wide_framing(self) -> None:
        with patch(
            "web_app._sample_face_scale_profile",
            return_value={"median_face_height": 0.39, "primary_center_x": 0.51, "primary_center_y": 0.45},
        ), patch(
            "web_app._sample_person_layout_profile",
            return_value={"median_largest_area": 0.88},
        ):
            prompt = web_app.build_real_scene_geometry_prompt(Path("white-model.mp4"))
        self.assertIn("人物特写", prompt)
        self.assertIn("主脸高度约占画面39%", prompt)
        self.assertIn("严禁展示人物腰部", prompt)
        self.assertIn("禁止改成广角", prompt)

    def test_real_closeup_reference_crops_front_panel_instead_of_full_triview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "triview.png"
            output = directory / "closeup.jpg"
            board = web_app.np.full((600, 900, 3), 255, dtype=web_app.np.uint8)
            web_app.cv2.rectangle(board, (80, 40), (250, 560), (0, 0, 0), -1)
            self.assertTrue(web_app.cv2.imwrite(str(source), board))
            web_app.crop_real_person_closeup_reference(source, output)
            cropped = web_app._read_reference_image(output)
            self.assertIsNotNone(cropped)
            assert cropped is not None
            self.assertLess(cropped.shape[1], board.shape[1] / 2)
            self.assertLess(cropped.shape[0], board.shape[0])

    def test_real_reference_crop_pads_tall_panel_inside_ark_aspect_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "portrait_triview.png"
            output = directory / "safe_sketch.jpg"
            board = web_app.np.full((1568, 2784, 3), 245, dtype=web_app.np.uint8)
            board[:, 1136:1748] = (35, 55, 75)
            self.assertTrue(web_app.cv2.imwrite(str(source), board))

            web_app.crop_real_person_closeup_reference(
                source,
                output,
                closeup=False,
                panel_index=0,
                portrait_three_view=True,
            )
            safe = web_app._read_reference_image(output)
            self.assertIsNotNone(safe)
            assert safe is not None
            height, width = safe.shape[:2]
            self.assertLessEqual(height / width, web_app.REAL_PERSON_REFERENCE_MAX_ASPECT)
            self.assertEqual(height, 1568)
            self.assertGreaterEqual(width, 654)

    def test_real_portrait_plus_three_view_uses_portrait_and_requested_body_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "portrait_triview.png"
            identity_output = directory / "identity.jpg"
            side_output = directory / "side.jpg"
            board = web_app.np.zeros((500, 1000, 3), dtype=web_app.np.uint8)
            board[:, :400] = (20, 20, 220)
            board[:, 400:610] = (20, 220, 20)
            board[:, 610:810] = (220, 20, 20)
            board[:, 810:] = (180, 180, 20)
            self.assertTrue(web_app.cv2.imwrite(str(source), board))
            web_app.crop_real_person_closeup_reference(
                source,
                identity_output,
                closeup=False,
                portrait_three_view=True,
                identity_portrait=True,
            )
            web_app.crop_real_person_closeup_reference(
                source,
                side_output,
                closeup=False,
                panel_index=1,
                portrait_three_view=True,
            )
            identity = web_app._read_reference_image(identity_output)
            side = web_app._read_reference_image(side_output)
            self.assertIsNotNone(identity)
            self.assertIsNotNone(side)
            assert identity is not None and side is not None
            self.assertGreater(float(identity[:, :, 2].mean()), float(identity[:, :, 0].mean()) * 5)
            self.assertGreater(float(side[:, :, 0].mean()), float(side[:, :, 2].mean()) * 5)

    def test_final_generation_defaults_to_720p_but_accepts_manual_1080p(self) -> None:
        with web_app.app.test_request_context("/api/real-long-video/generate", method="POST"):
            self.assertEqual(web_app.generation_options()["resolution"], "720p")
        with web_app.app.test_request_context(
            "/api/real-long-video/generate",
            method="POST",
            data={"resolution": "1080p"},
        ):
            self.assertEqual(web_app.generation_options()["resolution"], "1080p")

    def test_real_final_model_resolution_matrix(self) -> None:
        web_app.validate_real_final_video_options(
            {"model": web_app.DEFAULT_SEEDANCE_MODEL, "resolution": "1080p"}
        )
        web_app.validate_real_final_video_options(
            {"model": web_app.DEFAULT_SEEDANCE_25_MODEL, "resolution": "720p"}
        )
        with self.assertRaisesRegex(web_app.WorkflowError, "只支持 480p、720p"):
            web_app.validate_real_final_video_options(
                {"model": web_app.DEFAULT_SEEDANCE_25_MODEL, "resolution": "1080p"}
            )

    def test_real_seedance_25_video_editing_forces_adaptive_and_auto_duration(self) -> None:
        normalized = web_app.normalize_real_final_video_options(
            {
                "model": web_app.DEFAULT_SEEDANCE_25_MODEL,
                "resolution": "720p",
                "ratio": "16:9",
                "duration": 7,
            },
            video_editing=True,
        )
        self.assertEqual(normalized["ratio"], "adaptive")
        self.assertEqual(normalized["duration"], -1)

        seedance_20 = web_app.normalize_real_final_video_options(
            {
                "model": web_app.DEFAULT_SEEDANCE_MODEL,
                "resolution": "720p",
                "ratio": "16:9",
                "duration": 7,
            },
            video_editing=True,
        )
        self.assertEqual(seedance_20["ratio"], "16:9")
        self.assertEqual(seedance_20["duration"], 7)

    def test_real_retry_scene_plate_materially_zooms_the_control_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "scene.png"
            output = directory / "retry.jpg"
            gradient = web_app.np.zeros((400, 240, 3), dtype=web_app.np.uint8)
            gradient[:, :, 0] = web_app.np.arange(240, dtype=web_app.np.uint8)[None, :]
            self.assertTrue(web_app.cv2.imwrite(str(source), gradient))
            result, zoom = web_app.build_real_composition_retry_scene_plate(
                source,
                output,
                {
                    "face_scale_ratio": 0.42,
                    "source": {"primary_center_x": 0.5, "primary_center_y": 0.45},
                },
                retry_number=1,
            )
            self.assertEqual(result, output)
            self.assertGreater(zoom, 2.0)
            corrected = web_app._read_reference_image(output)
            self.assertIsNotNone(corrected)
            assert corrected is not None
            self.assertEqual(corrected.shape[:2], gradient.shape[:2])
            self.assertFalse(web_app.np.array_equal(corrected, gradient))

    def test_long_generation_auto_corrects_composition_at_most_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shot_dir = directory / "shot_01"
            shot_dir.mkdir()
            source = shot_dir / "source.mp4"
            depth = shot_dir / "depth.mp4"
            scene = shot_dir / "scene_plate.jpg"
            target_scene = shot_dir / "target_scene.jpg"
            for path, content in (
                (source, b"source"),
                (depth, b"depth"),
                (scene, b"scene"),
                (target_scene, b"target"),
            ):
                path.write_bytes(content)
            job = web_app.WebJob(
                id="composition-auto-retry",
                kind="long_generate",
                project=web_app.VIRTUAL_LONG_PROJECT,
                run_dir=directory,
                shots=[{
                    "index": 1,
                    "start": 0.0,
                    "end": 5.0,
                    "duration": 5.0,
                    "source_path": str(source),
                    "depth_path": str(depth),
                    "target_scene_path": str(target_scene),
                    "scene_path": str(scene),
                    "scene_plate_signature": "scene-signature",
                    "requested_scene_signature": "scene-signature",
                    "requested_signature": "video-signature",
                    "actor_ids": [],
                }],
            )
            generation_counter = {"value": 0}

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                generation_counter["value"] += 1
                output = shot_dir / f"generated-{generation_counter['value']}.mp4"
                output.write_bytes(b"video")
                sub_job.update(status="succeeded", output_path=output, task_id=f"task-{generation_counter['value']}")

            def conform(_source: Path, target: Path, _duration: float, **_kwargs) -> Path:
                target.write_bytes(b"conformed")
                return target

            def concatenate(_paths: list[Path], target: Path) -> Path:
                target.write_bytes(b"final")
                return target

            failed_qa = {
                "passed": False,
                "face_scale_ratio": 0.5,
                "reasons": ["人物脸部尺度仅为原片的 0.50 倍，景别发生严重变化"],
            }
            passed_qa = {"passed": True, "reasons": []}
            try:
                with (
                    patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                    patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=conform),
                    patch("web_app.concatenate_videos", side_effect=concatenate),
                    patch("web_app.validate_final_composition", side_effect=[failed_qa, failed_qa, passed_qa]),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_video_generation(
                        job,
                        actors=[],
                        scene_prompt="scene",
                        image_model="image-model",
                        options={
                            "prompt": web_app.DEFAULT_LONG_VIDEO_PROMPT,
                            "model": "video-model",
                            "resolution": "720p",
                            "ratio": "adaptive",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                            "preserve_original_audio": False,
                        },
                        blur_range="",
                        force_shot_indices={1},
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("composition-auto-retry-s01", None)
            self.assertEqual(mock_generate.call_count, 3)
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.shots[0]["composition_retry_count"], 2)
            self.assertFalse(job.shots[0]["composition_approval_required"])
            self.assertEqual(
                Path(job.shots[0]["raw_output_path"]).read_bytes(),
                b"video",
            )
            self.assertEqual(Path(job.shots[0]["raw_output_path"]).name, "generated-3.mp4")

    def test_long_generation_stops_after_two_failed_auto_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shot_dir = directory / "shot_01"
            shot_dir.mkdir()
            paths = {
                name: shot_dir / filename
                for name, filename in {
                    "source": "source.mp4",
                    "depth": "depth.mp4",
                    "scene": "scene_plate.jpg",
                    "target": "target_scene.jpg",
                }.items()
            }
            for path in paths.values():
                path.write_bytes(b"data")
            job = web_app.WebJob(
                id="composition-awaiting-approval",
                kind="long_generate",
                project=web_app.VIRTUAL_LONG_PROJECT,
                run_dir=directory,
                shots=[{
                    "index": 1,
                    "start": 0.0,
                    "end": 5.0,
                    "duration": 5.0,
                    "source_path": str(paths["source"]),
                    "depth_path": str(paths["depth"]),
                    "target_scene_path": str(paths["target"]),
                    "scene_path": str(paths["scene"]),
                    "scene_plate_signature": "scene-signature",
                    "requested_scene_signature": "scene-signature",
                    "requested_signature": "video-signature",
                    "actor_ids": [],
                }],
            )
            counter = {"value": 0}

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                counter["value"] += 1
                output = shot_dir / f"generated-{counter['value']}.mp4"
                output.write_bytes(b"video")
                sub_job.update(status="succeeded", output_path=output, task_id=f"task-{counter['value']}")

            def conform(_source: Path, target: Path, _duration: float, **_kwargs) -> Path:
                target.write_bytes(b"conformed")
                return target

            failed_qa = {
                "passed": False,
                "face_scale_ratio": 0.5,
                "reasons": ["人物脸部尺度仅为原片的 0.50 倍，景别发生严重变化"],
            }
            try:
                with (
                    patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                    patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=conform),
                    patch("web_app.concatenate_videos") as mock_concat,
                    patch("web_app.validate_final_composition", return_value=failed_qa),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_video_generation(
                        job,
                        actors=[],
                        scene_prompt="scene",
                        image_model="image-model",
                        options={
                            "prompt": web_app.DEFAULT_LONG_VIDEO_PROMPT,
                            "model": "video-model",
                            "resolution": "720p",
                            "ratio": "adaptive",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                            "preserve_original_audio": False,
                        },
                        blur_range="",
                        force_shot_indices={1},
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("composition-awaiting-approval-s01", None)
            self.assertEqual(mock_generate.call_count, 3)
            mock_concat.assert_not_called()
            self.assertEqual(job.status, "awaiting_approval")
            self.assertTrue(job.shots[0]["composition_approval_required"])
            self.assertEqual(job.shots[0]["composition_retry_count"], 2)

    def test_real_composition_approval_can_continue_when_later_shots_are_unfinished(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            completed = directory / "shot_01.mp4"
            completed.write_bytes(b"video")
            job = web_app.WebJob(
                id="real-composition-deadlock",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                shots=[
                    {"index": 1, "output_path": str(completed)},
                    {"index": 2, "output_path": ""},
                    {"index": 3, "output_path": ""},
                ],
            )
            form = {
                "regeneration_mode": "selected",
                "force_shots": "[1]",
            }
            with web_app.app.test_request_context(
                "/api/real-long-video/generate",
                method="POST",
                data=form,
            ):
                with self.assertRaisesRegex(web_app.WorkflowError, "其他分镜尚无可复用成片"):
                    web_app.parse_long_regeneration_request(job)
            with web_app.app.test_request_context(
                "/api/real-long-video/generate",
                method="POST",
                data=form,
            ):
                mode, indices = web_app.parse_long_regeneration_request(
                    job,
                    allow_missing_unforced_outputs=True,
                )
            self.assertEqual(mode, "selected")
            self.assertEqual(indices, {1})

    def test_real_regeneration_ignores_skipped_shots_but_virtual_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            completed = directory / "shot_02.mp4"
            completed.write_bytes(b"video")
            real_job = web_app.WebJob(
                id="real-skipped-regeneration",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                shots=[
                    {"index": 1, "output_path": "", "generation_skipped": True},
                    {"index": 2, "output_path": str(completed)},
                ],
            )
            with web_app.app.test_request_context(
                "/api/real-long-video/generate",
                method="POST",
                data={"regeneration_mode": "selected", "force_shots": "[2]"},
            ):
                mode, indices = web_app.parse_long_regeneration_request(real_job)
            self.assertEqual(mode, "selected")
            self.assertEqual(indices, {2})
            with web_app.app.test_request_context(
                "/api/real-long-video/generate",
                method="POST",
                data={"regeneration_mode": "all", "force_shots": "[]"},
            ):
                mode, indices = web_app.parse_long_regeneration_request(real_job)
            self.assertEqual(mode, "all")
            self.assertEqual(indices, {2})

            real_missing_job = web_app.WebJob(
                id="real-missing-selected-output",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                shots=[{"index": 1, "output_path": "", "status": "failed"}],
            )
            with web_app.app.test_request_context(
                "/api/real-long-video/generate",
                method="POST",
                data={"regeneration_mode": "selected", "force_shots": "[1]"},
            ):
                mode, indices = web_app.parse_long_regeneration_request(
                    real_missing_job,
                    allow_missing_unforced_outputs=True,
                )
            self.assertEqual(mode, "selected")
            self.assertEqual(indices, {1})

            virtual_job = web_app.WebJob(
                id="virtual-does-not-skip",
                kind="long_generate",
                project=web_app.VIRTUAL_LONG_PROJECT,
                run_dir=directory,
                shots=[
                    {"index": 1, "output_path": "", "generation_skipped": True},
                    {"index": 2, "output_path": str(completed)},
                ],
            )
            with web_app.app.test_request_context(
                "/api/long-video/generate",
                method="POST",
                data={"regeneration_mode": "all", "force_shots": "[]"},
            ):
                mode, indices = web_app.parse_long_regeneration_request(virtual_job)
            self.assertEqual(mode, "all")
            self.assertEqual(indices, {1, 2})
            with web_app.app.test_request_context(
                "/api/long-video/generate",
                method="POST",
                data={"regeneration_mode": "selected", "force_shots": "[1]"},
            ):
                with self.assertRaisesRegex(web_app.WorkflowError, "尚无成片"):
                    web_app.parse_long_regeneration_request(
                        virtual_job,
                        allow_missing_unforced_outputs=True,
                    )

    def test_real_approved_retry_stops_before_next_shot_if_composition_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shots = []
            for index in (1, 2):
                shot_dir = directory / f"shot_{index:02d}"
                shot_dir.mkdir()
                source = shot_dir / "source.mp4"
                depth = shot_dir / "depth.mp4"
                scene = shot_dir / "scene_plate.jpg"
                target = shot_dir / "target_scene.jpg"
                for path in (source, depth, scene, target):
                    path.write_bytes(b"data")
                shots.append({
                    "index": index,
                    "start": float(index - 1) * 5.0,
                    "end": float(index) * 5.0,
                    "duration": 5.0,
                    "source_path": str(source),
                    "depth_path": str(depth),
                    "target_scene_path": str(target),
                    "scene_path": str(scene),
                    "scene_plate_signature": f"scene-signature-{index}",
                    "requested_scene_signature": f"scene-signature-{index}",
                    "requested_signature": f"video-signature-{index}",
                    "actor_ids": [],
                    "composition_retry_count": 2 if index == 1 else 0,
                    "composition_approval_required": index == 1,
                })
            job = web_app.WebJob(
                id="real-approved-retry-stop",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                shots=shots,
            )

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                output = Path(sub_job.run_dir) / "generated.mp4"
                output.write_bytes(b"video")
                sub_job.update(status="succeeded", output_path=output, task_id="task-approved-retry")

            def conform(_source: Path, target: Path, _duration: float, **_kwargs) -> Path:
                target.write_bytes(b"conformed")
                return target

            failed_qa = {
                "passed": False,
                "face_scale_ratio": 0.5,
                "reasons": ["人物脸部尺度仅为原片的 0.50 倍，景别发生严重变化"],
            }
            try:
                with (
                    patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                    patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=conform),
                    patch("web_app.concatenate_videos") as mock_concat,
                    patch("web_app.validate_final_composition", return_value=failed_qa),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_video_generation(
                        job,
                        actors=[],
                        scene_prompt="scene",
                        image_model="image-model",
                        options={
                            "prompt": web_app.DEFAULT_LONG_VIDEO_PROMPT,
                            "model": "video-model",
                            "resolution": "720p",
                            "ratio": "adaptive",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                            "preserve_original_audio": False,
                        },
                        blur_range="",
                        force_shot_indices={1},
                        composition_approved_shot_indices={1},
                        reuse_unforced_outputs=True,
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("real-approved-retry-stop-s01", None)
                    web_app.JOBS.pop("real-approved-retry-stop-s02", None)
            self.assertEqual(mock_generate.call_count, 1)
            mock_concat.assert_not_called()
            self.assertEqual(job.status, "awaiting_approval")
            self.assertTrue(job.shots[0]["composition_approval_required"])
            self.assertNotEqual(job.shots[1].get("status"), "running")
            self.assertIn("后续分镜尚未提交", job.stage)

    def test_real_generation_skips_selected_shot_and_continues_with_later_shots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            skipped_dir = directory / "shot_01"
            active_dir = directory / "shot_02"
            skipped_dir.mkdir()
            active_dir.mkdir()
            old_output = skipped_dir / "old_bad_output.mp4"
            old_output.write_bytes(b"old")
            active_files = {
                name: active_dir / name
                for name in ("source.mp4", "depth.mp4", "scene_plate.jpg", "target_scene.jpg")
            }
            for path in active_files.values():
                path.write_bytes(b"data")
            job = web_app.WebJob(
                id="real-skip-and-continue",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                shots=[
                    {
                        "index": 1,
                        "generation_skipped": True,
                        "composition_approval_required": True,
                        "output_path": str(old_output),
                        "status": "awaiting_approval",
                    },
                    {
                        "index": 2,
                        "start": 5.0,
                        "end": 10.0,
                        "duration": 5.0,
                        "source_path": str(active_files["source.mp4"]),
                        "depth_path": str(active_files["depth.mp4"]),
                        "target_scene_path": str(active_files["target_scene.jpg"]),
                        "scene_path": str(active_files["scene_plate.jpg"]),
                        "scene_plate_signature": "scene-signature-2",
                        "requested_scene_signature": "scene-signature-2",
                        "requested_signature": "video-signature-2",
                        "actor_ids": [],
                    },
                ],
            )

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                output = Path(sub_job.run_dir) / "generated.mp4"
                output.write_bytes(b"video")
                sub_job.update(status="succeeded", output_path=output, task_id="task-after-skip")

            def conform(_source: Path, target: Path, _duration: float, **_kwargs) -> Path:
                target.write_bytes(b"conformed")
                return target

            def concatenate(inputs: list[Path], target: Path) -> Path:
                self.assertEqual(len(inputs), 1)
                self.assertIn("shot_02", str(inputs[0]))
                target.write_bytes(b"merged")
                return target

            passed_qa = {"passed": True, "reasons": []}
            try:
                with (
                    patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                    patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=conform),
                    patch("web_app.concatenate_videos", side_effect=concatenate) as mock_concat,
                    patch("web_app.validate_final_composition", return_value=passed_qa),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_video_generation(
                        job,
                        actors=[],
                        scene_prompt="scene",
                        image_model="image-model",
                        options={
                            "prompt": web_app.DEFAULT_LONG_VIDEO_PROMPT,
                            "model": "video-model",
                            "resolution": "720p",
                            "ratio": "adaptive",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                            "preserve_original_audio": False,
                        },
                        blur_range="",
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("real-skip-and-continue-s02", None)
            self.assertEqual(mock_generate.call_count, 1)
            self.assertEqual(mock_concat.call_count, 1)
            self.assertEqual(job.shots[0]["status"], "skipped")
            self.assertFalse(job.shots[0]["composition_approval_required"])
            self.assertEqual(job.shots[1]["status"], "succeeded")
            self.assertEqual(job.status, "succeeded")
            self.assertIn("未跳过分镜", job.stage)

    def test_real_single_shot_generation_defers_paused_incompatible_shots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            selected_dir = directory / "shot_01"
            selected_dir.mkdir()
            selected_files = {
                name: selected_dir / name
                for name in ("source.mp4", "depth.mp4", "scene_plate.jpg", "target_scene.jpg")
            }
            for path in selected_files.values():
                path.write_bytes(b"data")
            paused_dir = directory / "shot_07"
            paused_dir.mkdir()
            paused_output = paused_dir / "intermediate.mp4"
            paused_output.write_bytes(b"intermediate")
            job = web_app.WebJob(
                id="real-selected-only-deferred",
                kind="long_generate",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                shots=[
                    {
                        "index": 1,
                        "start": 0.0,
                        "end": 5.0,
                        "duration": 5.0,
                        "source_path": str(selected_files["source.mp4"]),
                        "depth_path": str(selected_files["depth.mp4"]),
                        "target_scene_path": str(selected_files["target_scene.jpg"]),
                        "scene_path": str(selected_files["scene_plate.jpg"]),
                        "scene_plate_signature": "scene-signature-1",
                        "requested_scene_signature": "scene-signature-1",
                        "requested_signature": "video-signature-1",
                        "output_path": str(selected_dir / "old.mp4"),
                        "actor_ids": [],
                        "status": "succeeded",
                    },
                    {
                        "index": 7,
                        "output_path": str(paused_output),
                        "output_signature": "",
                        "requested_signature": "new-signature-7",
                        "performance_dirty": True,
                        "position_binding_dirty": True,
                        "status": "paused",
                    },
                ],
            )
            Path(job.shots[0]["output_path"]).write_bytes(b"old")

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                output = Path(sub_job.run_dir) / "generated.mp4"
                output.write_bytes(b"video")
                sub_job.update(status="succeeded", output_path=output, task_id="task-selected-only")

            def conform(_source: Path, target: Path, _duration: float, **_kwargs) -> Path:
                target.write_bytes(b"conformed")
                return target

            try:
                with (
                    patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                    patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=conform),
                    patch("web_app.concatenate_videos") as mock_concat,
                    patch(
                        "web_app.validate_final_composition",
                        return_value={"passed": True, "reasons": []},
                    ),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_video_generation(
                        job,
                        actors=[],
                        scene_prompt="scene",
                        image_model="image-model",
                        options={
                            "prompt": web_app.DEFAULT_LONG_VIDEO_PROMPT,
                            "model": "video-model",
                            "resolution": "720p",
                            "ratio": "adaptive",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                            "preserve_original_audio": False,
                        },
                        blur_range="",
                        force_shot_indices={1},
                        reuse_unforced_outputs=True,
                        selected_only_generation=True,
                        deferred_unforced_shot_indices={7},
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("real-selected-only-deferred-s01", None)
            self.assertEqual(mock_generate.call_count, 1)
            mock_concat.assert_not_called()
            self.assertEqual(job.shots[0]["status"], "succeeded")
            self.assertEqual(job.shots[1]["status"], "paused")
            self.assertEqual(job.status, "succeeded")
            self.assertIn("整片等待其他分镜完成", job.stage)
            self.assertTrue(any("不会复用该中间结果" in message for message in job.logs))

    def test_long_shot_manual_position_lock_overrides_automatic_analysis(self) -> None:
        shot = {
            "position_binding_manual": True,
            "actor_mappings": [{"slot": 1, "actor_id": 1, "source_position": "右侧前景站立，手持咖啡"}],
            "performance": {
                "performance": [{
                    "actor_slot": 1,
                    "visible_evidence": "自动分析错误位置",
                }]
            },
        }
        self.assertEqual(
            web_app.long_shot_performance_slot_anchor(shot, 1),
            "右侧前景站立，手持咖啡",
        )

    def test_long_video_person_mapping_preserves_original_slot_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            job = web_app.WebJob(id="mapping", kind="long_analyze", run_dir=Path(temp_dir))
            job.shots = [{"index": 1}]
            payload = [
                {
                    "index": 1,
                    "actor_ids": [1, 2],
                    "actor_mappings": [
                        {"slot": 1, "actor_id": 2},
                        {"slot": 2, "actor_id": 1},
                    ],
                }
            ]
            with web_app.app.test_request_context(
                "/api/long-video/generate",
                method="POST",
                data={"cast_confirmed": "true", "shot_casts": json.dumps(payload)},
            ):
                self.assertEqual(web_app.parse_long_shot_casts(job, 2), {1: [2, 1]})

    def test_long_video_position_locks_follow_performance_slot_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            job = web_app.WebJob(id="positions", kind="long_analyze", run_dir=Path(temp_dir))
            job.shots = [{"index": 1}]
            payload = [{
                "index": 1,
                "actor_mappings": [
                    {"slot": 1, "actor_id": 2, "position_lock": "右侧前景站立"},
                    {"slot": 2, "actor_id": 1, "position_lock": "左侧内景坐着"},
                ],
            }]
            with web_app.app.test_request_context(
                "/api/long-video/generate",
                method="POST",
                data={"cast_confirmed": "true", "shot_casts": json.dumps(payload, ensure_ascii=False)},
            ):
                casts = web_app.parse_long_shot_casts(job, 2)
                self.assertEqual(
                    web_app.parse_long_shot_position_locks(job, casts),
                    {1: ["右侧前景站立", "左侧内景坐着"]},
                )

    def test_long_video_regeneration_request_parses_selected_and_all_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shots = []
            for index in (1, 2):
                output = directory / f"output-{index}.mp4"
                output.write_bytes(b"video")
                shots.append({"index": index, "output_path": str(output)})
            job = web_app.WebJob(id="regeneration", kind="long_generate", run_dir=directory, shots=shots)

            with web_app.app.test_request_context(
                "/api/long-video/generate",
                method="POST",
                data={"regeneration_mode": "selected", "force_shots": "[2]"},
            ):
                self.assertEqual(web_app.parse_long_regeneration_request(job), ("selected", {2}))

            with web_app.app.test_request_context(
                "/api/long-video/generate",
                method="POST",
                data={"regeneration_mode": "all", "force_shots": "[1]"},
            ):
                self.assertEqual(web_app.parse_long_regeneration_request(job), ("all", {1, 2}))

    def test_long_video_white_model_regeneration_request_parses_selected_shot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shots = []
            for index in (1, 2):
                white_model = directory / f"white-model-{index}.mp4"
                white_model.write_bytes(b"video")
                shots.append({"index": index, "white_model_path": str(white_model)})
            job = web_app.WebJob(id="white-regeneration", kind="long_white_model", run_dir=directory, shots=shots)
            with web_app.app.test_request_context(
                "/api/long-video/white-model",
                method="POST",
                data={"white_regeneration_mode": "selected", "force_white_shots": "[1]"},
            ):
                self.assertEqual(
                    web_app.parse_long_white_model_regeneration_request(job),
                    ("selected", {1}),
                )

    def test_long_video_scene_library_requires_explicit_per_shot_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shots = []
            for index in (1, 2):
                shot_dir = directory / f"shot_{index:02d}"
                shot_dir.mkdir()
                source = shot_dir / "source.mp4"
                source.write_bytes(b"video")
                shots.append({"index": index, "source_path": str(source)})
            job = web_app.WebJob(id="scenes", kind="long_analyze", run_dir=directory)
            job.shots = shots
            with web_app.app.test_request_context(
                "/api/long-video/generate",
                method="POST",
                data={
                    "scene_library": json.dumps([
                        {"id": "scene_1", "name": "客厅", "image_count": 1},
                        {"id": "scene_2", "name": "街道", "image_count": 1},
                    ]),
                    "scene_assignments": json.dumps([
                        {"index": 1, "group_id": "scene_1", "image_index": 1},
                        {"index": 2, "group_id": "scene_2", "image_index": 1},
                    ]),
                    "scene_group_scene_1_1": (io.BytesIO(b"living-room"), "living.png"),
                    "scene_group_scene_2_1": (io.BytesIO(b"street"), "street.jpg"),
                },
                content_type="multipart/form-data",
            ):
                scenes, assignments = web_app.save_long_scene_library(job)
            self.assertEqual(Path(scenes[1]).read_bytes(), b"living-room")
            self.assertEqual(Path(scenes[2]).read_bytes(), b"street")
            self.assertNotEqual(scenes[1], scenes[2])
            self.assertEqual(assignments[1]["group_name"], "客厅")
            self.assertEqual(assignments[2]["group_id"], "scene_2")
            self.assertEqual(len(job.scene_groups), 2)

    @patch("web_app._persist_long_job")
    @patch("web_app.concatenate_videos")
    @patch("web_app.conform_video_duration")
    @patch("web_app.run_multi_generation")
    @patch("web_app.run_generation")
    @patch("web_app.run_long_scene_plate")
    @patch("web_app.inspect_video")
    def test_long_video_routes_empty_single_and_multi_shots_independently(
        self,
        mock_inspect: Mock,
        mock_scene_plate: Mock,
        mock_single: Mock,
        mock_multi: Mock,
        mock_conform: Mock,
        mock_concat: Mock,
        _mock_persist: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "reference.mp4").write_bytes(b"full-original")
            shots = []
            for index, actor_ids in enumerate(([], [1], [1, 2]), start=1):
                shot_dir = directory / f"shot_{index:02d}"
                shot_dir.mkdir()
                source = shot_dir / "source.mp4"
                source.write_bytes(b"source")
                (shot_dir / "depth.mp4").write_bytes(b"depth")
                target_scene = shot_dir / "target_scene.jpg"
                target_scene.write_bytes(b"target-scene")
                shot_duration = 1.25 if index == 1 else 5.0
                shot_start = float(index - 1) * 5
                shots.append(
                    {
                        "index": index,
                        "start": shot_start,
                        "end": shot_start + shot_duration,
                        "duration": shot_duration,
                        "source_path": str(source),
                        "depth_path": "",
                        "scene_path": "",
                        "output_path": "",
                        "actor_ids": actor_ids,
                        "target_scene_path": str(target_scene),
                        "requested_signature": f"signature-{index}",
                        "requested_scene_signature": f"scene-signature-{index}",
                        "performance": {
                            "has_dialogue": False,
                            "dialogue": [],
                            "performance": [],
                            "audio_summary": "安静",
                        },
                    }
                )
            actors = [
                {"id": 1, "role": "左侧人物", "person_source": "asset://one", "clothing_source": str(directory / "clothing1.png")},
                {"id": 2, "role": "右侧人物", "person_source": "asset://two", "clothing_source": str(directory / "clothing2.png")},
            ]
            for name in ("clothing1.png", "clothing2.png"):
                (directory / name).write_bytes(b"image")

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                output = sub_job.run_dir / f"generated-{sub_job.id}.mp4"
                output.write_bytes(b"generated")
                sub_job.update(status="succeeded", output_path=output)

            def conform(_source: Path, target: Path, _duration: float, **_kwargs) -> Path:
                Path(target).write_bytes(b"conformed")
                return Path(target)

            def concatenate(_paths: list[Path], target: Path) -> Path:
                Path(target).write_bytes(b"final")
                return Path(target)

            mock_single.side_effect = finish_generation
            mock_multi.side_effect = finish_generation
            def finish_scene_plate(sub_job: web_app.WebJob, *_args, **_kwargs) -> Path:
                output = sub_job.run_dir / "scene_plate.jpg"
                output.write_bytes(b"scene-plate")
                sub_job.scene_path = output
                return output

            mock_scene_plate.side_effect = finish_scene_plate
            mock_conform.side_effect = conform
            mock_concat.side_effect = concatenate
            mock_inspect.side_effect = lambda path: SimpleNamespace(
                duration=(
                    4.0
                    if "timed" in Path(path).name
                    else 1.25
                    if Path(path).parent.name == "shot_01"
                    else 5.0
                )
            )
            job = web_app.WebJob(
                id="long-routing",
                kind="long_generate",
                project="long_video_replication",
                run_dir=directory,
                shots=shots,
            )
            def hold_short_depth(_source: Path, target: Path, **_kwargs) -> Path:
                Path(target).write_bytes(b"held-depth")
                return Path(target)

            def strip_audio(_video: Path, target: Path) -> Path:
                Path(target).write_bytes(b"silent-reference")
                return Path(target)

            try:
                with (
                    patch(
                        "web_app.extend_video_with_trailing_hold",
                        side_effect=hold_short_depth,
                    ) as mock_hold,
                    patch("web_app.strip_video_audio", side_effect=strip_audio) as mock_strip,
                    patch(
                        "web_app.validate_final_composition",
                        return_value={"passed": True, "reasons": []},
                    ),
                ):
                    web_app.run_long_video_generation(
                        job,
                        actors=actors,
                        scene_prompt="scene",
                        image_model="image-model",
                        options={
                            "prompt": web_app.DEFAULT_LONG_VIDEO_PROMPT,
                            "model": "video-model",
                            "resolution": "720p",
                            "ratio": "adaptive",
                            "generate_audio": True,
                            "dialogue_voice_mode": "seedance_new_voice",
                            "watermark": False,
                            "delete_tos_after": True,
                            "preserve_original_audio": False,
                        },
                        blur_range="",
                    )
            finally:
                with web_app.JOBS_LOCK:
                    for index in (1, 2, 3):
                        web_app.JOBS.pop(f"long-routing-s{index:02d}", None)
            self.assertEqual(mock_scene_plate.call_count, 3)
            self.assertEqual(mock_single.call_count, 2)
            self.assertEqual(mock_multi.call_count, 1)
            self.assertEqual(job.shots[0]["generation_mode"], "scene_only")
            self.assertEqual(job.shots[1]["generation_mode"], "single")
            self.assertEqual(job.shots[2]["generation_mode"], "multi")
            self.assertEqual(mock_strip.call_count, 3)
            mock_hold.assert_called_once()
            self.assertTrue(job.shots[0]["depth_padded"])
            self.assertEqual(job.shots[0]["depth_reference_duration"], 4.0)
            self.assertFalse(mock_hold.call_args.kwargs["with_audio"])
            self.assertEqual(Path(mock_single.call_args_list[0].kwargs["depth_path"]).name, "final_reference_seedance_silent.mp4")
            self.assertIn("不得生成任何人物说话声", mock_single.call_args_list[0].kwargs["options"]["prompt"])
            self.assertIn("@视频1是完全静音", mock_single.call_args_list[0].kwargs["options"]["prompt"])
            self.assertIn("结束姿势定格", mock_single.call_args_list[0].kwargs["options"]["prompt"])
            self.assertIn("禁止把动作慢放", mock_single.call_args_list[0].kwargs["options"]["prompt"])
            self.assertEqual(mock_conform.call_args_list[0].args[2], 1.25)
            self.assertTrue(mock_conform.call_args_list[0].kwargs["with_audio"])
            self.assertEqual(job.status, "succeeded")

    def test_long_video_force_regeneration_bypasses_output_and_cloud_task_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shot_dir = directory / "shot_01"
            shot_dir.mkdir()
            source = shot_dir / "source.mp4"
            depth = shot_dir / "depth.mp4"
            target_scene = shot_dir / "target_scene.jpg"
            scene_plate = shot_dir / "scene_plate.jpg"
            existing_output = shot_dir / "final_conformed_signature-1.mp4"
            for path in (source, depth, target_scene, scene_plate, existing_output):
                path.write_bytes(b"asset")
            second_dir = directory / "shot_02"
            second_dir.mkdir()
            second_source = second_dir / "source.mp4"
            second_output = second_dir / "final-existing.mp4"
            second_source.write_bytes(b"source")
            second_output.write_bytes(b"existing")
            job = web_app.WebJob(
                id="long-force-regenerate",
                kind="long_generate",
                project="long_video_replication",
                run_dir=directory,
                shots=[{
                    "index": 1,
                    "start": 0.0,
                    "end": 5.0,
                    "duration": 5.0,
                    "source_path": str(source),
                    "depth_path": str(depth),
                    "target_scene_path": str(target_scene),
                    "scene_path": str(scene_plate),
                    "scene_plate_signature": "scene-signature-1",
                    "requested_scene_signature": "scene-signature-1",
                    "output_path": str(existing_output),
                    "output_signature": "signature-1",
                    "requested_signature": "signature-1",
                    "actor_ids": [],
                }, {
                    "index": 2,
                    "start": 5.0,
                    "end": 10.0,
                    "duration": 5.0,
                    "source_path": str(second_source),
                    "output_path": str(second_output),
                    "output_signature": "old-signature-2",
                    "requested_signature": "changed-signature-2",
                    "actor_ids": [],
                    "composition_qa": {"passed": True, "reasons": []},
                }],
            )

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                output = shot_dir / "new-generation.mp4"
                output.write_bytes(b"new-video")
                sub_job.update(status="succeeded", output_path=output, task_id="new-task")

            def conform(_source: Path, target: Path, _duration: float, **_kwargs) -> Path:
                Path(target).write_bytes(b"new-conformed")
                return Path(target)

            def concatenate(_paths: list[Path], target: Path) -> Path:
                Path(target).write_bytes(b"final")
                return Path(target)

            try:
                with (
                    patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                    patch("web_app.run_long_scene_plate") as mock_scene_plate,
                    patch("web_app._resume_matching_long_shot_task") as mock_resume,
                    patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=conform),
                    patch("web_app.concatenate_videos", side_effect=concatenate),
                    patch(
                        "web_app.validate_final_composition",
                        return_value={"passed": True, "reasons": []},
                    ),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_video_generation(
                        job,
                        actors=[],
                        scene_prompt="scene",
                        image_model="image-model",
                        options={
                            "prompt": web_app.DEFAULT_LONG_VIDEO_PROMPT,
                            "model": "video-model",
                            "resolution": "720p",
                            "ratio": "adaptive",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                            "preserve_original_audio": False,
                        },
                        blur_range="",
                        force_shot_indices={1},
                        reuse_unforced_outputs=True,
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("long-force-regenerate-s01", None)
            mock_scene_plate.assert_not_called()
            mock_resume.assert_not_called()
            mock_generate.assert_called_once()
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.shots[0]["task_id"], "new-task")
            self.assertTrue(any("强制重新生成" in message for message in job.logs))
            self.assertTrue(any("不在本次单镜重生范围内" in message for message in job.logs))

    def test_long_video_single_white_model_regeneration_reuses_other_white_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            shots = []
            for index in (1, 2):
                shot_dir = directory / f"shot_{index:02d}"
                shot_dir.mkdir()
                source = shot_dir / "source.mp4"
                mosaic = shot_dir / "face_mosaic.mp4"
                existing = shot_dir / f"white-model-{index}.mp4"
                source.write_bytes(b"source")
                mosaic.write_bytes(b"mosaic")
                existing.write_bytes(b"white-model")
                shots.append({
                    "index": index,
                    "start": float(index - 1) * 5,
                    "end": float(index) * 5,
                    "duration": 5.0,
                    "source_path": str(source),
                    "mosaic_path": str(mosaic),
                    "mosaic_reviewed": True,
                    "white_model_path": str(existing),
                    "white_model_signature": "matching" if index == 1 else "outdated",
                })
            options = {
                "model": "video-model",
                "resolution": "720p",
                "ratio": "adaptive",
                "generate_audio": False,
                "watermark": False,
                "delete_tos_after": True,
            }
            forced_signature = web_app._white_model_signature(
                Path(shots[0]["mosaic_path"]),
                prompt=web_app.DEFAULT_WHITE_MODEL_PROMPT,
                options=options,
            )
            shots[0]["white_model_signature"] = forced_signature
            job = web_app.WebJob(
                id="single-white-regenerate",
                kind="long_white_model",
                project="long_video_replication",
                run_dir=directory,
                shots=shots,
            )

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                output = sub_job.run_dir / "new-white-generation.mp4"
                output.write_bytes(b"new-white")
                sub_job.update(status="succeeded", output_path=output, task_id="new-white-task")

            def copy_output(_source: Path, target: Path, *_args, **_kwargs) -> Path:
                Path(target).write_bytes(b"processed")
                return Path(target)

            def mux_output(_visual: Path, _source: Path, target: Path) -> Path:
                Path(target).write_bytes(b"muxed")
                return Path(target)

            try:
                with (
                    patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                    patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=copy_output),
                    patch("web_app.mux_original_audio", side_effect=mux_output),
                    patch("web_app.concatenate_videos", side_effect=copy_output),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_white_model_generation(
                        job,
                        options=options,
                        force_shot_indices={1},
                        reuse_unforced_outputs=True,
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("single-white-regenerate-w01", None)
            mock_generate.assert_called_once()
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.shots[0]["white_model_task_id"], "new-white-task")
            self.assertTrue(any("强制重新生成白模" in message for message in job.logs))
            self.assertTrue(any("不在本次单镜白模重生范围内" in message for message in job.logs))

    def test_white_model_prompt_upgrade_does_not_reuse_legacy_white_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "source.mp4"
            mosaic = directory / "face_mosaic.mp4"
            existing = directory / "legacy-white-model.mp4"
            for path in (source, mosaic, existing):
                path.write_bytes(b"asset")
            job = web_app.WebJob(
                id="legacy-white-480p",
                kind="long_white_model",
                project="long_video_replication",
                run_dir=directory,
                shots=[{
                    "index": 1,
                    "duration": 5.0,
                    "source_path": str(source),
                    "mosaic_path": str(mosaic),
                    "mosaic_reviewed": True,
                    "white_model_path": str(existing),
                    "white_model_signature": "old-720p-signature",
                }],
            )

            def concatenate(_paths: list[Path], target: Path) -> Path:
                Path(target).write_bytes(b"merged")
                return Path(target)

            def finish_generation(sub_job: web_app.WebJob, **_kwargs) -> None:
                output = sub_job.run_dir / "new-white.mp4"
                output.write_bytes(b"new-white")
                sub_job.update(status="succeeded", output_path=output, task_id="new-white-task")

            def copy_output(_source: Path, target: Path, *_args, **_kwargs) -> Path:
                Path(target).write_bytes(b"processed")
                return Path(target)

            with (
                patch("web_app.inspect_video", return_value=SimpleNamespace(duration=5.0)),
                patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                patch("web_app.conform_video_duration", side_effect=copy_output),
                patch("web_app.concatenate_videos", side_effect=concatenate),
                patch("web_app._persist_long_job"),
            ):
                web_app.run_long_white_model_generation(
                    job,
                    options={
                        "model": web_app.DEFAULT_SEEDANCE_MODEL,
                        "resolution": "480p",
                        "ratio": "adaptive",
                        "generate_audio": False,
                        "watermark": False,
                        "delete_tos_after": True,
                    },
                )

            mock_generate.assert_called_once()
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.shots[0]["white_model_task_id"], "new-white-task")

    def test_multi_prompt_maps_each_actor_to_two_images(self) -> None:
        prompt = web_app.build_multi_prompt(["左侧人物", "右侧人物"])
        self.assertIn("@图片1", prompt)
        self.assertIn("@图片2", prompt)
        self.assertIn("@图片3", prompt)
        self.assertIn("@图片4", prompt)
        self.assertIn("@图片5", prompt)
        self.assertIn("禁止串脸", prompt)

    def test_seedance_duration_matches_nearest_supported_second(self) -> None:
        self.assertEqual(web_app.match_seedance_duration(2.4), 4)
        self.assertEqual(web_app.match_seedance_duration(5.49), 5)
        self.assertEqual(web_app.match_seedance_duration(5.5), 6)
        self.assertEqual(web_app.match_seedance_duration(14.5), 15)
        self.assertEqual(web_app.match_seedance_duration(30), 15)

    def test_long_shot_duration_always_covers_original_without_cutting(self) -> None:
        self.assertEqual(web_app.match_seedance_cover_duration(0.467), 4)
        self.assertEqual(web_app.match_seedance_cover_duration(5.01), 6)
        self.assertEqual(web_app.match_seedance_cover_duration(14.5), 15)

    def test_long_shot_timing_prompt_marks_extended_tail_as_frozen(self) -> None:
        prompt = web_app.seedance_hold_timing_prompt(0.467, 4)
        self.assertIn("0.00–0.47秒", prompt)
        self.assertIn("0.47–4.00秒", prompt)
        self.assertIn("结束姿势定格", prompt)
        self.assertIn("禁止把动作慢放", prompt)

    def test_timing_fix_uses_new_signature_without_losing_legacy_match(self) -> None:
        actor = {
            "id": 1,
            "role": "主角",
            "person_source": "asset://person",
            "clothing_source": "asset://clothing",
        }
        kwargs = {
            "options": {"prompt": "动作", "model": "seedance", "resolution": "720p", "ratio": "9:16"},
            "scene_prompt": "场景",
            "image_model": "seedream",
            "scene_source": "asset://scene",
            "motion_reference": "asset://white-model",
            "performance": {"has_dialogue": False},
        }
        current = web_app.long_shot_generation_signature(
            [actor], **kwargs, position_locks=["右侧前景站立"]
        )
        legacy = web_app.long_shot_generation_signature([actor], **kwargs, signature_version=6)
        self.assertNotEqual(current, legacy)
        self.assertEqual(
            current,
            web_app.long_shot_generation_signature(
                [actor], **kwargs, position_locks=["右侧前景站立"], signature_version=14
            ),
        )
        self.assertNotEqual(
            current,
            web_app.long_shot_generation_signature(
                [actor], **kwargs, position_locks=["右侧前景站立"], signature_version=9
            ),
        )
        self.assertEqual(
            web_app.long_shot_generation_signature([actor], **kwargs, signature_version=7),
            web_app.long_shot_generation_signature(
                [actor], **kwargs, position_locks=["不会进入旧签名"], signature_version=7
            ),
        )

    @patch("web_app.inspect_video")
    def test_adaptive_ratio_resolves_to_task_metadata_ratio(self, mock_inspect: Mock) -> None:
        mock_inspect.return_value = SimpleNamespace(width=720, height=1280)
        self.assertEqual(web_app.resolved_seedance_ratio("adaptive", Path("depth.mp4")), "9:16")
        self.assertEqual(web_app.resolved_seedance_ratio("16:9", Path("depth.mp4")), "16:9")

    def test_real_white_model_resolves_adaptive_ratio_per_source_shot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "source.mp4"
            mosaic = directory / "face_mosaic.mp4"
            source.write_bytes(b"source")
            mosaic.write_bytes(b"mosaic")
            job = web_app.WebJob(
                id="real-white-adaptive-ratio",
                kind="real_long_white_model",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                shots=[{
                    "index": 1,
                    "duration": 4.0,
                    "source_path": str(source),
                    "mosaic_path": str(mosaic),
                    "mosaic_reviewed": True,
                    "suggested_actor_count": 1,
                }],
            )

            def video_info(path: Path) -> SimpleNamespace:
                if Path(path).name == "new-white.mp4":
                    return SimpleNamespace(duration=4.0, width=496, height=864)
                return SimpleNamespace(duration=4.0, width=1080, height=1920)

            def finish_generation(sub_job: web_app.WebJob, **kwargs: object) -> None:
                self.assertEqual(kwargs["options"]["ratio"], "9:16")
                self.assertIn(
                    web_app.REAL_PERSON_SAFE_WHITE_MODEL_PROMPT,
                    kwargs["options"]["prompt"],
                )
                self.assertEqual(kwargs["options"]["reference_upload_strategy"], "stable")
                self.assertEqual(
                    kwargs["options"]["reference_upload_context"],
                    "真实人物复刻重绘白膜",
                )
                self.assertTrue(kwargs["options"]["verify_tos_public"])
                output = sub_job.run_dir / "new-white.mp4"
                output.write_bytes(b"new-white")
                sub_job.update(status="succeeded", output_path=output, task_id="white-ratio-task")

            def copy_output(_source: Path, target: Path, *_args: object, **_kwargs: object) -> Path:
                Path(target).write_bytes(b"processed")
                return Path(target)

            qa = {"passed": True, "reasons": [], "warnings": []}
            try:
                with (
                    patch("web_app.inspect_video", side_effect=video_info),
                    patch("web_app.run_generation", side_effect=finish_generation) as mock_generate,
                    patch("web_app.conform_video_duration", side_effect=copy_output),
                    patch("web_app.validate_white_model", return_value=qa),
                    patch("web_app.concatenate_videos", side_effect=copy_output),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_white_model_generation(
                        job,
                        options={
                            "model": web_app.DEFAULT_SEEDANCE_MODEL,
                            "resolution": "480p",
                            "ratio": "adaptive",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                        },
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("real-white-adaptive-ratio-w01q1", None)

            mock_generate.assert_called_once()
            self.assertEqual(job.status, "succeeded")
            self.assertTrue(any("自动跟随原片：9:16" in message for message in job.logs))

    def test_real_white_model_network_failure_resets_running_shot_to_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "source.mp4"
            mosaic = directory / "face_mosaic.mp4"
            source.write_bytes(b"source")
            mosaic.write_bytes(b"mosaic")
            job = web_app.WebJob(
                id="real-white-network-failure",
                kind="real_long_white_model",
                project=web_app.REAL_PERSON_LONG_PROJECT,
                run_dir=directory,
                shots=[{
                    "index": 1,
                    "duration": 4.0,
                    "source_path": str(source),
                    "mosaic_path": str(mosaic),
                    "mosaic_reviewed": True,
                    "suggested_actor_count": 1,
                }],
            )

            def fail_before_submit(sub_job: web_app.WebJob, **_kwargs: object) -> None:
                sub_job.update(
                    status="failed",
                    error="tempfile.org connection timed out",
                    task_id="",
                )

            try:
                with (
                    patch(
                        "web_app.inspect_video",
                        return_value=SimpleNamespace(duration=4.0, width=1080, height=1920),
                    ),
                    patch("web_app.run_generation", side_effect=fail_before_submit),
                    patch("web_app._persist_long_job"),
                ):
                    web_app.run_long_white_model_generation(
                        job,
                        options={
                            "model": web_app.DEFAULT_SEEDANCE_MODEL,
                            "resolution": "480p",
                            "ratio": "adaptive",
                            "generate_audio": False,
                            "watermark": False,
                            "delete_tos_after": True,
                        },
                    )
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("real-white-network-failure-w01q1", None)

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.shots[0]["status"], "retryable")
            self.assertEqual(
                job.shots[0]["stage"],
                "网络失败：未提交、未计费，可以安全重试",
            )
            self.assertFalse(job.shots[0]["submitted"])
            self.assertEqual(job.shots[0]["white_model_total_generation_count"], 0)

    def test_white_model_quality_gate_rejects_aspect_ratio_mismatch(self) -> None:
        source = Path("source.mp4")
        white = Path("white.mp4")

        def inspect(path: Path) -> SimpleNamespace:
            if Path(path) == source:
                return SimpleNamespace(width=1080, height=1920)
            return SimpleNamespace(width=864, height=496)

        with (
            patch("web_app.inspect_video", side_effect=inspect),
            patch("web_app._sample_person_layout_profile", return_value={"max_count": 1}),
            patch("web_app._people_primary_layout", return_value={"area": 0.2, "center_x": 0.5, "center_y": 0.5}),
            patch("web_app._sample_motion_profile", return_value=0.1),
            patch("web_app._sample_global_motion_vector", return_value={"x": 0.0, "y": 0.0, "magnitude": 0.0}),
            patch("web_app._sample_white_model_profile", return_value={
                "median_green_ratio": 0.5,
                "median_subject_ratio": 0.3,
                "median_neutral_bright_ratio": 0.8,
                "median_colored_subject_ratio": 0.0,
            }),
            patch("web_app._sample_text_profile", return_value={}),
        ):
            result = web_app.validate_white_model(source, white, expected_actor_count=1)

        self.assertFalse(result["passed"])
        self.assertTrue(any("画面比例与原片不一致" in reason for reason in result["reasons"]))
        self.assertEqual(result["source_dimensions"], "1080×1920")
        self.assertEqual(result["white_model_dimensions"], "864×496")

    @patch("web_app.inspect_video")
    def test_automatic_duration_overrides_manual_form_value(self, mock_inspect: Mock) -> None:
        mock_inspect.return_value = SimpleNamespace(duration=7.6)
        options = {"duration": 15}
        job = web_app.WebJob(id="durationjob", kind="generate", run_dir=Path("."))
        matched = web_app.apply_automatic_duration(job, options, Path("depth.mp4"))
        self.assertEqual(matched, 8)
        self.assertEqual(options["duration"], 8)
        self.assertEqual(job.generation_duration, 8)

    def test_cloud_progress_is_exposed_as_estimate_while_running(self) -> None:
        job = web_app.WebJob(
            id="cloudjob",
            kind="generate",
            run_dir=Path("."),
            progress=68,
            cloud_status="running",
            cloud_started_at=web_app.time.time() - 240,
        )
        payload = job.public()
        self.assertTrue(payload["progress_estimated"])
        self.assertGreater(payload["progress"], 68)
        self.assertLess(payload["progress"], 90)

    def test_config_never_exposes_api_key(self) -> None:
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("ark_ready", payload)
        self.assertTrue(payload["temporary_tunnel_ready"])
        self.assertTrue(payload["temporary_upload_ready"])
        self.assertNotIn("api_key", payload)
        self.assertNotIn("ARK_API_KEY", response.get_data(as_text=True))

    @patch("web_app.api_client")
    def test_api_check_returns_safe_status(self, mock_api_client: Mock) -> None:
        mock_api_client.return_value.check_credentials.return_value = "API Key 有效"
        response = self.client.post("/api/check")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_generated_file_can_be_streamed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            video = directory / "depth.mp4"
            video.write_bytes(b"test-video")
            job = web_app.WebJob(id="testjob", kind="depth", run_dir=directory, depth_path=video)
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                response = self.client.get("/api/jobs/testjob/file/depth")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "video/mp4")
                self.assertEqual(response.data, b"test-video")
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    def test_generated_scene_can_be_previewed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            scene = directory / "scene_reference.jpg"
            scene.write_bytes(b"test-scene")
            job = web_app.WebJob(
                id="scenejob",
                kind="person_prepare",
                run_dir=directory,
                scene_path=scene,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                payload = job.public()
                self.assertTrue(payload["has_scene"])
                self.assertEqual(payload["scene_url"], "/api/jobs/scenejob/file/scene")
                response = self.client.get(payload["scene_url"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/jpeg")
                self.assertEqual(response.data, b"test-scene")
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    def test_extracted_person_and_clothing_can_be_previewed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            person = directory / "original_person_triview.jpg"
            clothing = directory / "original_clothing_triview.jpg"
            person.write_bytes(b"person-board")
            clothing.write_bytes(b"clothing-board")
            job = web_app.WebJob(
                id="scene-assets",
                kind="scene_prepare",
                run_dir=directory,
                person_path=person,
                clothing_path=clothing,
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                payload = job.public()
                self.assertEqual(payload["person_url"], "/api/jobs/scene-assets/file/person")
                self.assertEqual(payload["clothing_url"], "/api/jobs/scene-assets/file/clothing")
                person_response = self.client.get(payload["person_url"])
                clothing_response = self.client.get(payload["clothing_url"])
                self.assertEqual(person_response.data, b"person-board")
                self.assertEqual(clothing_response.data, b"clothing-board")
                person_response.close()
                clothing_response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    @patch("web_app.inspect_video")
    def test_interrupted_person_scene_job_restores_local_depth_and_references(
        self,
        mock_inspect: Mock,
    ) -> None:
        mock_inspect.return_value = SimpleNamespace(duration=8.2)
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            run_dir = project_dir / "runs" / "20260807_135811_web_person_full_retry123abc"
            run_dir.mkdir(parents=True)
            for name in (
                "reference.mp4",
                "depth.mp4",
                "scene_source_01.jpg",
                "scene_source_02.jpg",
                "scene_source_03.jpg",
                "person.png",
                "clothing.png",
            ):
                (run_dir / name).write_bytes(b"artifact")
            with patch.object(web_app, "PROJECT_DIR", project_dir):
                restored = web_app.restore_latest_person_retry_job()
            try:
                self.assertIsNotNone(restored)
                assert restored is not None
                self.assertEqual(restored.id, "retry123abc")
                self.assertEqual(restored.recovery_action, "retry_scene")
                self.assertTrue(restored.public()["has_depth"])
                self.assertTrue(restored.public()["has_person_reference"])
                self.assertTrue(restored.public()["has_clothing_reference"])
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("retry123abc", None)

    @patch("web_app.threading.Thread")
    def test_person_scene_retry_reuses_existing_depth_without_automatic_submission(
        self,
        mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            depth = run_dir / "depth.mp4"
            reference = run_dir / "reference.mp4"
            person = run_dir / "person.png"
            clothing = run_dir / "clothing.png"
            for path in (depth, reference, person, clothing):
                path.write_bytes(b"artifact")
            job = web_app.WebJob(
                id="scene-retry-job",
                kind="person_full",
                project="person_only_replacement",
                run_dir=run_dir,
                status="failed",
                progress=47,
                depth_path=depth,
                person_path=person,
                clothing_path=clothing,
                recovery_action="retry_scene",
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                response = self.client.post(
                    "/api/person-only/retry-scene",
                    data={"source_job_id": job.id},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertEqual(payload["id"], job.id)
                self.assertTrue(payload["has_depth"])
                self.assertFalse(payload["has_scene"])
                self.assertEqual(job.kind, "person_prepare_retry")
                self.assertIn("不会重跑深度", job.logs[-1])
                mock_thread.return_value.start.assert_called_once()
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    @patch("web_app.threading.Thread")
    @patch("web_app.api_client")
    def test_person_seedance_recovery_attaches_existing_task_without_resubmitting(
        self,
        mock_api_client: Mock,
        mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            depth = run_dir / "depth.mp4"
            scene = run_dir / "scene_reference.jpg"
            for path in (depth, scene):
                path.write_bytes(b"artifact")
            job = web_app.WebJob(
                id="lost-seedance-job",
                kind="person_full",
                project="person_only_replacement",
                run_dir=run_dir,
                status="failed",
                progress=60,
                depth_path=depth,
                scene_path=scene,
                generation_duration=5,
                recovery_action="recover_seedance_submission",
            )
            mock_api_client.return_value.recover_created_task.return_value = "cgt-recovered"
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                response = self.client.post(
                    "/api/person-only/recover-seedance",
                    data={
                        "source_job_id": job.id,
                        "model": "doubao-seedance-2-0-260128",
                        "resolution": "720p",
                        "ratio": "9:16",
                        "duration": "5",
                        "generate_audio": "false",
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertEqual(payload["task_id"], "cgt-recovered")
                self.assertEqual(job.recovery_action, "")
                mock_api_client.return_value.create_task.assert_not_called()
                mock_thread.return_value.start.assert_called_once()
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    @patch("web_app.api_client")
    @patch("web_app.extract_scene_reference_frames")
    def test_seedream_connection_reset_exposes_safe_manual_retry(
        self,
        mock_extract: Mock,
        mock_api_client: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            frames = []
            for index in range(3):
                frame = run_dir / f"scene_source_{index + 1:02d}.jpg"
                frame.write_bytes(b"frame")
                frames.append(frame)
            mock_extract.return_value = frames
            mock_api_client.return_value.generate_image.side_effect = web_app.ArkConnectionError(
                "POST", "connection reset", 1
            )
            job = web_app.WebJob(id="reset-job", kind="person_full", run_dir=run_dir)
            with self.assertRaisesRegex(web_app.WorkflowError, "安全重试场景提取"):
                web_app.run_scene_extraction(
                    job,
                    run_dir / "reference.mp4",
                    prompt="extract scene",
                    model="seedream-test",
                )
            self.assertEqual(job.recovery_action, "retry_scene")

    @patch("web_app.threading.Thread")
    def test_submitted_cloud_job_is_restored_after_service_restart(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            run_dir = project_dir / "runs" / "20260807_120000_web_multi_full_restore123abc"
            run_dir.mkdir(parents=True)
            depth = run_dir / "depth.mp4"
            depth.write_bytes(b"depth-video")
            (run_dir / "job.json").write_text(
                """{
                  "local_job_id": "restore123abc",
                  "kind": "multi_full",
                  "project": "multi_person_replication",
                  "task_id": "cgt-restored",
                  "status": "running",
                  "cloud_status": "running",
                  "depth": "%s",
                  "duration": 8
                }""" % str(depth).replace("\\", "\\\\"),
                encoding="utf-8",
            )
            with patch.object(web_app, "PROJECT_DIR", project_dir):
                response = self.client.get("/api/jobs/restore123abc")
            try:
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["task_id"], "cgt-restored")
                self.assertEqual(payload["project"], "multi_person_replication")
                self.assertTrue(payload["has_depth"])
                mock_thread.assert_called_once()
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop("restore123abc", None)

    @patch("web_app.threading.Thread")
    @patch("web_app.inspect_video")
    @patch("web_app.new_job")
    def test_generation_job_keeps_reused_depth_for_preview(
        self,
        mock_new_job: Mock,
        mock_inspect: Mock,
        _mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source_dir = directory / "source"
            generate_dir = directory / "generate"
            source_dir.mkdir()
            generate_dir.mkdir()
            depth = source_dir / "depth.mp4"
            depth.write_bytes(b"depth-video")
            source_job = web_app.WebJob(
                id="source-depth-job",
                kind="depth",
                run_dir=source_dir,
                depth_path=depth,
            )
            generation_job = web_app.WebJob(
                id="generation-job",
                kind="generate",
                run_dir=generate_dir,
            )
            mock_new_job.return_value = generation_job
            mock_inspect.return_value = SimpleNamespace(duration=8.04)
            with web_app.JOBS_LOCK:
                web_app.JOBS[source_job.id] = source_job
            try:
                response = self.client.post(
                    "/api/generate",
                    data={
                        "depth_job_id": source_job.id,
                        "person_asset": "asset://authorized-person",
                        "clothing_image": (io.BytesIO(b"clothing"), "clothing.png"),
                        "scene_image": (io.BytesIO(b"scene"), "scene.png"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertTrue(payload["has_depth"])
                self.assertEqual(payload["depth_url"], "/api/jobs/generation-job/file/depth")
                self.assertEqual(generation_job.depth_path, depth)
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source_job.id, None)

    @patch("web_app.threading.Thread")
    @patch("web_app.inspect_video")
    @patch("web_app.new_job")
    def test_multi_generation_keeps_depth_and_actor_order(
        self,
        mock_new_job: Mock,
        mock_inspect: Mock,
        mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source_dir = directory / "source"
            generate_dir = directory / "multi"
            source_dir.mkdir()
            generate_dir.mkdir()
            depth = source_dir / "depth.mp4"
            depth.write_bytes(b"depth-video")
            source_job = web_app.WebJob(
                id="multi-source-depth",
                kind="depth",
                run_dir=source_dir,
                depth_path=depth,
            )
            generation_job = web_app.WebJob(
                id="multi-generation",
                kind="multi_generate",
                run_dir=generate_dir,
            )
            mock_new_job.return_value = generation_job
            mock_inspect.return_value = SimpleNamespace(duration=8.04)
            with web_app.JOBS_LOCK:
                web_app.JOBS[source_job.id] = source_job
            try:
                response = self.client.post(
                    "/api/multi/generate",
                    data={
                        "depth_job_id": source_job.id,
                        "actor_count": "2",
                        "role_description_1": "左侧人物",
                        "role_description_2": "右侧人物",
                        "person_asset_1": "asset://person-one",
                        "person_asset_2": "asset://person-two",
                        "clothing_image_1": (io.BytesIO(b"clothing-one"), "clothing-1.png"),
                        "clothing_image_2": (io.BytesIO(b"clothing-two"), "clothing-2.png"),
                        "scene_image": (io.BytesIO(b"scene"), "scene.png"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 202)
                self.assertTrue(response.get_json()["has_depth"])
                self.assertEqual(generation_job.depth_path, depth)
                kwargs = mock_thread.call_args.kwargs["kwargs"]
                self.assertEqual(
                    kwargs["character_sources"],
                    [
                        ("asset://person-one", str(generate_dir / "clothing_1.png")),
                        ("asset://person-two", str(generate_dir / "clothing_2.png")),
                    ],
                )
                self.assertIn("@图片5", kwargs["options"]["prompt"])
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source_job.id, None)

    @patch("web_app.threading.Thread")
    @patch("web_app.inspect_video")
    @patch("web_app.new_job")
    def test_person_only_generation_reuses_depth_and_extracted_scene(
        self,
        mock_new_job: Mock,
        mock_inspect: Mock,
        mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source_dir = directory / "source"
            generate_dir = directory / "generate"
            source_dir.mkdir()
            generate_dir.mkdir()
            depth = source_dir / "depth.mp4"
            scene = source_dir / "scene_reference.jpg"
            depth.write_bytes(b"depth-video")
            scene.write_bytes(b"clean-scene")
            source_job = web_app.WebJob(
                id="person-source",
                kind="person_prepare",
                run_dir=source_dir,
                depth_path=depth,
                scene_path=scene,
            )
            generation_job = web_app.WebJob(
                id="person-generation",
                kind="person_generate",
                run_dir=generate_dir,
            )
            mock_new_job.return_value = generation_job
            mock_inspect.return_value = SimpleNamespace(duration=9.2)
            with web_app.JOBS_LOCK:
                web_app.JOBS[source_job.id] = source_job
            try:
                response = self.client.post(
                    "/api/person-only/generate",
                    data={
                        "source_job_id": source_job.id,
                        "person_asset": "asset://authorized-person",
                        "clothing_image": (io.BytesIO(b"clothing"), "clothing.png"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertTrue(payload["has_depth"])
                self.assertTrue(payload["has_scene"])
                self.assertEqual(generation_job.depth_path, depth)
                self.assertEqual(generation_job.scene_path, scene)
                kwargs = mock_thread.call_args.kwargs["kwargs"]
                self.assertEqual(kwargs["scene_source"], str(scene))
                self.assertEqual(kwargs["person_source"], "asset://authorized-person")
                self.assertIn("@图片3", kwargs["options"]["prompt"])
                self.assertEqual(kwargs["options"]["duration"], 9)
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source_job.id, None)

    @patch("web_app.download_file")
    @patch("web_app.api_client")
    @patch("web_app.extract_scene_reference_frames")
    def test_scene_only_extracts_person_and_clothing_as_two_ordered_seedream_calls(
        self,
        mock_extract: Mock,
        mock_api_client: Mock,
        mock_download: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            frames = []
            for index in range(3):
                frame = directory / f"scene_source_{index + 1:02d}.jpg"
                frame.write_bytes(b"frame")
                frames.append(frame)
            mock_extract.return_value = frames
            mock_api_client.return_value.generate_image.side_effect = [
                {"url": "https://example.com/person.jpg"},
                {"url": "https://example.com/clothing.jpg"},
            ]
            mock_download.side_effect = lambda _url, target, **_kwargs: Path(target).write_bytes(b"image")
            job = web_app.WebJob(id="scene-extract", kind="scene_prepare", run_dir=directory)
            person, clothing = web_app.run_original_subject_extraction(
                job,
                directory / "reference.mp4",
                person_prompt="person prompt",
                clothing_prompt="clothing prompt",
                model="seedream-test",
            )
            self.assertTrue(person.is_file())
            self.assertTrue(clothing.is_file())
            self.assertEqual(mock_api_client.return_value.generate_image.call_count, 2)
            calls = mock_api_client.return_value.generate_image.call_args_list
            self.assertIn("person prompt", calls[0].kwargs["prompt"])
            self.assertIn("横向16:9", calls[0].kwargs["prompt"])
            self.assertEqual(calls[0].kwargs["size"], "2560x1440")
            self.assertIn("clothing prompt", calls[1].kwargs["prompt"])
            self.assertIn("纯白色背景", calls[1].kwargs["prompt"])
            self.assertIn("严禁出现真人、模特", calls[1].kwargs["prompt"])
            self.assertIn("人台", calls[1].kwargs["prompt"])
            self.assertIn("玩偶", calls[1].kwargs["prompt"])
            self.assertEqual(calls[1].kwargs["size"], "2K")
            self.assertEqual(job.person_path, person)
            self.assertEqual(job.clothing_path, clothing)

    @patch("web_app.threading.Thread")
    @patch("web_app.inspect_video")
    @patch("web_app.new_job")
    def test_scene_only_generation_reuses_extracted_subject_and_maps_new_scene(
        self,
        mock_new_job: Mock,
        mock_inspect: Mock,
        mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source_dir = directory / "source"
            generate_dir = directory / "generate"
            source_dir.mkdir()
            generate_dir.mkdir()
            depth = source_dir / "depth.mp4"
            person = source_dir / "original_person_triview.jpg"
            clothing = source_dir / "original_clothing_triview.jpg"
            for path in (depth, person, clothing):
                path.write_bytes(b"artifact")
            source_job = web_app.WebJob(
                id="scene-source",
                kind="scene_prepare",
                project="scene_only_replacement",
                run_dir=source_dir,
                depth_path=depth,
                person_path=person,
                clothing_path=clothing,
            )
            generation_job = web_app.WebJob(
                id="scene-generation",
                kind="scene_generate",
                project="scene_only_replacement",
                run_dir=generate_dir,
            )
            mock_new_job.return_value = generation_job
            mock_inspect.return_value = SimpleNamespace(duration=6.1)
            with web_app.JOBS_LOCK:
                web_app.JOBS[source_job.id] = source_job
            try:
                response = self.client.post(
                    "/api/scene-only/generate",
                    data={
                        "source_job_id": source_job.id,
                        "scene_image": (io.BytesIO(b"new-scene"), "scene.png"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertTrue(payload["has_depth"])
                self.assertTrue(payload["has_person_reference"])
                self.assertTrue(payload["has_clothing_reference"])
                self.assertTrue(payload["has_scene"])
                kwargs = mock_thread.call_args.kwargs["kwargs"]
                self.assertEqual(kwargs["person_source"], str(person))
                self.assertEqual(kwargs["clothing_source"], str(clothing))
                self.assertEqual(kwargs["scene_source"], str(generate_dir / "new_scene.png"))
                self.assertIn("@图片3", kwargs["options"]["prompt"])
                self.assertEqual(kwargs["options"]["duration"], 6)
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source_job.id, None)

    @patch("web_app.threading.Thread")
    @patch("web_app.inspect_video")
    @patch("web_app.new_job")
    def test_scene_and_clothing_generation_use_selected_character_asset(
        self,
        mock_new_job: Mock,
        mock_inspect: Mock,
        mock_thread: Mock,
    ) -> None:
        asset_uri = "asset://asset-sharedcharacter123"
        mock_inspect.return_value = SimpleNamespace(duration=6.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            scene_source_dir = directory / "scene-source"
            scene_generate_dir = directory / "scene-generate"
            clothing_source_dir = directory / "clothing-source"
            clothing_generate_dir = directory / "clothing-generate"
            for folder in (scene_source_dir, scene_generate_dir, clothing_source_dir, clothing_generate_dir):
                folder.mkdir()

            scene_depth = scene_source_dir / "depth.mp4"
            scene_person = scene_source_dir / "person.jpg"
            scene_clothing = scene_source_dir / "clothing.jpg"
            for path in (scene_depth, scene_person, scene_clothing):
                path.write_bytes(b"artifact")
            scene_source_job = web_app.WebJob(
                id="scene-asset-source",
                kind="scene_prepare",
                project="scene_only_replacement",
                run_dir=scene_source_dir,
                depth_path=scene_depth,
                person_path=scene_person,
                clothing_path=scene_clothing,
            )
            scene_generation_job = web_app.WebJob(
                id="scene-asset-generation",
                kind="scene_generate",
                project="scene_only_replacement",
                run_dir=scene_generate_dir,
            )

            clothing_depth = clothing_source_dir / "depth.mp4"
            clothing_person = clothing_source_dir / "person.jpg"
            clothing_scene = clothing_source_dir / "scene.jpg"
            for path in (clothing_depth, clothing_person, clothing_scene):
                path.write_bytes(b"artifact")
            (clothing_source_dir / "original_person_triview.version").write_text(
                web_app.CLOTHING_PERSON_TRIVIEW_VERSION,
                encoding="utf-8",
            )
            clothing_source_job = web_app.WebJob(
                id="clothing-asset-source",
                kind="clothing_prepare",
                project="clothing_only_replacement",
                run_dir=clothing_source_dir,
                depth_path=clothing_depth,
                person_path=clothing_person,
                scene_path=clothing_scene,
            )
            clothing_generation_job = web_app.WebJob(
                id="clothing-asset-generation",
                kind="clothing_generate",
                project="clothing_only_replacement",
                run_dir=clothing_generate_dir,
            )
            mock_new_job.side_effect = [scene_generation_job, clothing_generation_job]
            with web_app.JOBS_LOCK:
                web_app.JOBS[scene_source_job.id] = scene_source_job
                web_app.JOBS[clothing_source_job.id] = clothing_source_job
            try:
                scene_response = self.client.post(
                    "/api/scene-only/generate",
                    data={
                        "source_job_id": scene_source_job.id,
                        "person_asset": asset_uri,
                        "scene_image": (io.BytesIO(b"new-scene"), "scene.png"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(scene_response.status_code, 202)
                self.assertEqual(mock_thread.call_args.kwargs["kwargs"]["person_source"], asset_uri)
                scene_response.close()

                clothing_response = self.client.post(
                    "/api/clothing-only/generate",
                    data={
                        "source_job_id": clothing_source_job.id,
                        "person_asset": asset_uri,
                        "clothing_image": (io.BytesIO(b"new-clothing"), "clothing.png"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(clothing_response.status_code, 202)
                self.assertEqual(mock_thread.call_args.kwargs["kwargs"]["person_source"], asset_uri)
                clothing_response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(scene_source_job.id, None)
                    web_app.JOBS.pop(clothing_source_job.id, None)

    @patch("web_app.run_scene_extraction")
    @patch("web_app.run_person_triview_extraction")
    def test_clothing_only_preparation_extracts_person_then_scene(
        self,
        mock_person: Mock,
        mock_scene: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            person = directory / "original_person_triview.jpg"
            scene = directory / "scene_reference.jpg"
            person.write_bytes(b"person")
            scene.write_bytes(b"scene")
            mock_person.return_value = person
            mock_scene.return_value = scene
            job = web_app.WebJob(id="clothing-prepare", kind="clothing_prepare", run_dir=directory)
            returned_person, returned_scene = web_app.run_clothing_only_extraction(
                job,
                directory / "reference.mp4",
                person_prompt="person",
                scene_prompt="scene",
                model="seedream-test",
            )
            self.assertEqual(returned_person, person)
            self.assertEqual(returned_scene, scene)
            mock_person.assert_called_once()
            mock_scene.assert_called_once()
            self.assertEqual(mock_person.call_args.kwargs["recovery_action"], "retry_clothing_references")
            self.assertEqual(
                mock_person.call_args.kwargs["required_constraint"],
                web_app.CLOTHING_PERSON_TRIVIEW_REQUIRED_CONSTRAINT,
            )
            self.assertEqual(
                mock_person.call_args.kwargs["artifact_version"],
                web_app.CLOTHING_PERSON_TRIVIEW_VERSION,
            )
            self.assertEqual(mock_scene.call_args.kwargs["recovery_action"], "retry_clothing_references")

    @patch("web_app.threading.Thread")
    @patch("web_app.inspect_video")
    @patch("web_app.new_job")
    def test_clothing_only_generation_maps_original_person_new_clothing_and_original_scene(
        self,
        mock_new_job: Mock,
        mock_inspect: Mock,
        mock_thread: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source_dir = directory / "source"
            generate_dir = directory / "generate"
            source_dir.mkdir()
            generate_dir.mkdir()
            depth = source_dir / "depth.mp4"
            person = source_dir / "original_person_triview.jpg"
            scene = source_dir / "scene_reference.jpg"
            for path in (depth, person, scene):
                path.write_bytes(b"artifact")
            (source_dir / "original_person_triview.version").write_text(
                web_app.CLOTHING_PERSON_TRIVIEW_VERSION,
                encoding="utf-8",
            )
            source_job = web_app.WebJob(
                id="clothing-source",
                kind="clothing_prepare",
                project="clothing_only_replacement",
                run_dir=source_dir,
                depth_path=depth,
                person_path=person,
                scene_path=scene,
            )
            generation_job = web_app.WebJob(id="clothing-generation", kind="clothing_generate", run_dir=generate_dir)
            generation_job.project = "clothing_only_replacement"
            mock_new_job.return_value = generation_job
            mock_inspect.return_value = SimpleNamespace(duration=7.2)
            with web_app.JOBS_LOCK:
                web_app.JOBS[source_job.id] = source_job
            try:
                response = self.client.post(
                    "/api/clothing-only/generate",
                    data={
                        "source_job_id": source_job.id,
                        "clothing_image": (io.BytesIO(b"new-clothing"), "clothing.png"),
                        "prompt": "自定义换装提示词",
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 202)
                kwargs = mock_thread.call_args.kwargs["kwargs"]
                self.assertEqual(kwargs["person_source"], str(person))
                self.assertEqual(kwargs["clothing_source"], str(generate_dir / "new_clothing.png"))
                self.assertEqual(kwargs["scene_source"], str(scene))
                self.assertIn("@图片2", kwargs["options"]["prompt"])
                self.assertIn("自定义换装提示词", kwargs["options"]["prompt"])
                self.assertIn("@图片2是唯一服装依据", kwargs["options"]["prompt"])
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(source_job.id, None)

    @patch("web_app.threading.Thread")
    @patch("web_app.new_job")
    def test_long_video_analysis_starts_as_free_local_job(self, mock_new_job: Mock, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            job = web_app.WebJob(id="long-analysis", kind="long_analyze", run_dir=directory)
            job.project = "long_video_replication"
            mock_new_job.return_value = job
            response = self.client.post(
                "/api/long-video/analyze",
                data={"reference_video": (io.BytesIO(b"video"), "long.mp4"), "sensitivity": "0.42"},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["project"], "long_video_replication")
            mock_thread.assert_called_once()
            self.assertIn("long-analyze", mock_thread.call_args.kwargs["name"])
            response.close()

    @patch("web_app.threading.Thread")
    def test_long_video_mosaic_stage_starts_without_cloud_submission(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "source.mp4"
            source.write_bytes(b"video")
            job = web_app.WebJob(
                id="long-mosaic-stage",
                kind="long_analyze",
                project="long_video_replication",
                run_dir=directory,
                status="succeeded",
                shots=[{"index": 1, "source_path": str(source)}],
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                response = self.client.post(
                    "/api/long-video/mosaic",
                    data={"source_job_id": job.id, "block_size": "22", "face_score_threshold": "0.50"},
                )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.get_json()["kind"], "long_mosaic")
                self.assertEqual(mock_thread.call_args.kwargs["kwargs"]["block_size"], 22)
                self.assertEqual(mock_thread.call_args.kwargs["target"], web_app.run_long_mosaic_preparation)
                response.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    @patch("web_app.threading.Thread")
    def test_long_video_performance_stage_requires_explicit_consent(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "source.mp4"
            source.write_bytes(b"video")
            job = web_app.WebJob(
                id="long-performance-stage",
                kind="long_analyze",
                project="long_video_replication",
                run_dir=directory,
                status="succeeded",
                shots=[{"index": 1, "source_path": str(source)}],
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                rejected = self.client.post(
                    "/api/long-video/performance", data={"source_job_id": job.id}
                )
                self.assertEqual(rejected.status_code, 400)
                accepted = self.client.post(
                    "/api/long-video/performance",
                    data={"source_job_id": job.id, "performance_consent": "true"},
                )
                self.assertEqual(accepted.status_code, 202)
                self.assertEqual(mock_thread.call_args.kwargs["target"], web_app.run_long_performance_analysis)
                rejected.close()
                accepted.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    @patch("web_app.threading.Thread")
    def test_long_video_white_model_stage_requires_reviewed_mosaic(self, mock_thread: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / "source.mp4"
            mosaic = directory / "face_mosaic.mp4"
            source.write_bytes(b"video")
            mosaic.write_bytes(b"mosaic")
            job = web_app.WebJob(
                id="long-white-stage",
                kind="long_mosaic",
                project="long_video_replication",
                run_dir=directory,
                status="succeeded",
                shots=[{"index": 1, "source_path": str(source), "mosaic_path": str(mosaic)}],
            )
            with web_app.JOBS_LOCK:
                web_app.JOBS[job.id] = job
            try:
                rejected = self.client.post(
                    "/api/long-video/white-model",
                    data={"source_job_id": job.id},
                    content_type="multipart/form-data",
                )
                self.assertEqual(rejected.status_code, 400)
                accepted = self.client.post(
                    "/api/long-video/white-model",
                    data={
                        "source_job_id": job.id,
                        "privacy_review_confirmed": "true",
                        "white_model_prompt": "这段用户提示词应被后台忽略",
                        "model": "用户填写的其他模型",
                        "resolution": "4k",
                        "watermark": "true",
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(accepted.status_code, 202)
                self.assertTrue(job.shots[0]["mosaic_reviewed"])
                self.assertEqual(mock_thread.call_args.kwargs["target"], web_app.run_long_white_model_generation)
                thread_kwargs = mock_thread.call_args.kwargs["kwargs"]
                self.assertNotIn("prompt", thread_kwargs)
                self.assertEqual(thread_kwargs["options"]["model"], web_app.DEFAULT_SEEDANCE_MODEL)
                self.assertEqual(thread_kwargs["options"]["resolution"], "480p")
                self.assertFalse(thread_kwargs["options"]["generate_audio"])
                self.assertFalse(thread_kwargs["options"]["watermark"])
                rejected.close()
                accepted.close()
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.pop(job.id, None)

    def test_white_model_prompt_is_fixed_and_has_no_image_tokens(self) -> None:
        prompt = web_app.DEFAULT_WHITE_MODEL_PROMPT
        self.assertIn("@视频1", prompt)
        self.assertNotIn("@图片", prompt)
        self.assertIn("光滑光头", prompt)
        self.assertIn("不透明的纯白素体外壳", prompt)
        self.assertIn("标准绿幕 #00B140", prompt)
        self.assertIn("严禁出现或保留任何字幕", prompt)
        self.assertIn("台词只表现为嘴部动作", prompt)

    def test_real_person_white_model_prompt_is_separate_and_identity_free(self) -> None:
        prompt = web_app.REAL_PERSON_SAFE_WHITE_MODEL_PROMPT
        self.assertNotEqual(prompt, web_app.DEFAULT_WHITE_MODEL_PROMPT)
        self.assertIn("无五官", prompt)
        self.assertIn("光滑椭圆体", prompt)
        self.assertIn("抽象几何形变", prompt)
        self.assertIn("人物大小", prompt)
        self.assertIn("机位", prompt)
        self.assertIn("景别", prompt)
        self.assertIn("构图", prompt)
        self.assertIn("运镜轨迹", prompt)
        for risky_word in ("素体", "解剖", "裸露", "皮肤", "眼窝", "鼻梁"):
            self.assertNotIn(risky_word, prompt)

    def test_real_white_model_failure_state_distinguishes_upload_and_review(self) -> None:
        network = web_app.real_white_model_failure_state("tempfile.org connection timed out")
        self.assertEqual(network["status"], "retryable")
        self.assertTrue(network["retryable"])
        self.assertIn("未提交、未计费，可以安全重试", network["error"])

        review = web_app.real_white_model_failure_state(
            "output video may contain sensitive information",
            task_id="cgt-submitted",
        )
        self.assertEqual(review["status"], "failed")
        self.assertFalse(review["retryable"])
        self.assertIn("已经提交", review["error"])
        self.assertIn("禁止使用相同打码视频和相同提示词原样重试", review["error"])

        review_without_task_id = web_app.real_white_model_failure_state(
            "InputImageSensitiveContentDetected"
        )
        self.assertEqual(review_without_task_id["stage"], "审核失败：已提交，禁止原样重试")
        self.assertFalse(review_without_task_id["retryable"])

    def test_real_stable_tos_reference_is_verified_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "real-white.mp4"
            source.write_bytes(b"video")
            tos = Mock()
            tos.upload_video.return_value = Mock(
                signed_url="https://tos.example/real-white.mp4",
                object_key="seedance-inputs/real-white.mp4",
            )
            tos_factory = Mock(return_value=tos)
            tos_factory.configured.return_value = True
            verifier = Mock()
            with (
                patch.object(web_app, "TosMediaStore", tos_factory),
                patch.object(web_app, "TempFileMediaStore", return_value=verifier),
            ):
                reference = web_app.prepare_seedance_stable_video_reference(
                    source,
                    context_label="真实人物复刻重绘白膜",
                    verify_tos_public=True,
                )

            verifier._verify_public_video.assert_called_once_with(
                "https://tos.example/real-white.mp4",
                expected_size=5,
            )
            self.assertEqual(reference.channel, "tos")
            reference.close(delete_remote=True)

    def test_real_stable_reference_reports_missing_tos_bucket_then_uses_tunnel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "real-white.mp4"
            source.write_bytes(b"video")
            logs: list[str] = []
            video_server = Mock(local_origin="http://127.0.0.1:45678")
            video_server.route_path = "/media/token/real-white.mp4"
            tunnel = Mock()
            tunnel.start.return_value = "https://real-white.trycloudflare.com"
            tos_factory = Mock()
            tos_factory.configured.return_value = False
            with (
                patch.dict(
                    web_app.os.environ,
                    {
                        "TOS_ACCESS_KEY": "configured",
                        "TOS_SECRET_KEY": "configured",
                        "TOS_BUCKET": "",
                    },
                ),
                patch.object(web_app, "TosMediaStore", tos_factory),
                patch.object(web_app, "TemporaryVideoServer", return_value=video_server),
                patch.object(web_app, "TemporaryPublicTunnel", return_value=tunnel),
            ):
                reference = web_app.prepare_seedance_stable_video_reference(
                    source,
                    on_log=logs.append,
                    context_label="真实人物复刻重绘白膜",
                    verify_tos_public=True,
                )

            self.assertEqual(reference.channel, "project_tunnel")
            self.assertTrue(any("TOS_BUCKET 未配置" in message for message in logs))
            tunnel.wait_until_reachable.assert_called_once_with(reference.url)
            reference.close()

    def test_real_single_white_retry_allows_selected_failed_shot_without_output(self) -> None:
        job = web_app.WebJob(
            id="retry-missing-real-white",
            kind="real_long_white_model",
            project=web_app.REAL_PERSON_LONG_PROJECT,
            run_dir=Path("."),
            shots=[{"index": 1, "status": "retryable", "white_model_path": ""}],
        )
        with web_app.app.test_request_context(
            "/api/real-long-video/white-model",
            method="POST",
            data={
                "white_regeneration_mode": "selected",
                "force_white_shots": "[1]",
            },
        ):
            mode, indices = web_app.parse_long_white_model_regeneration_request(job)
        self.assertEqual(mode, "selected")
        self.assertEqual(indices, {1})

    def test_real_white_model_paid_count_ignores_pre_submit_network_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shot_dir = Path(temp_dir)
            source = shot_dir / "source.mp4"
            source.write_bytes(b"video")
            for sequence, task_id in ((1, "cgt-paid"), (2, ""), (3, "")):
                attempt = shot_dir / "white_model_task" / f"attempt_{sequence}"
                attempt.mkdir(parents=True)
                (attempt / "job.json").write_text(
                    json.dumps({"task_id": task_id}),
                    encoding="utf-8",
                )
            shot = {
                "source_path": str(source),
                "white_model_total_generation_count": 3,
            }
            self.assertEqual(web_app.real_long_white_model_paid_generation_count(shot), 1)
            self.assertEqual(web_app.real_long_white_model_attempt_count(shot), 3)

    @patch("web_app.resume_cloud_job")
    def test_long_shot_reuses_matching_succeeded_cloud_task_without_resubmitting(
        self,
        mock_resume: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shot_dir = Path(temp_dir)
            scene = shot_dir / "scene_plate.jpg"
            person = shot_dir / "person.png"
            clothing = shot_dir / "clothing.png"
            for path in (scene, person, clothing):
                path.write_bytes(b"asset")
            record = {
                "task_id": "cgt-existing",
                "status": "failed",
                "cloud_status": "succeeded",
                "submitted_at": 100.0,
                "requested_signature": "signature-1",
                "prompt": "same prompt",
                "model": "model-1",
                "resolution": "720p",
                "ratio": "adaptive",
                "duration": 4,
                "actor_count": 2,
                "scene": str(scene),
                "person": str(person),
                "clothing": str(clothing),
            }
            (shot_dir / "job.json").write_text(json.dumps(record), encoding="utf-8")
            job = web_app.WebJob(
                id="long-shot-recover",
                kind="long_shot",
                project="long_video_replication",
                run_dir=shot_dir,
            )

            def recover(existing_job: web_app.WebJob) -> None:
                output = shot_dir / "recovered.mp4"
                output.write_bytes(b"video")
                existing_job.output_path = output
                existing_job.update(status="succeeded", cloud_status="succeeded")

            mock_resume.side_effect = recover
            reused = web_app._resume_matching_long_shot_task(
                job,
                options={
                    "prompt": "same prompt",
                    "model": "model-1",
                    "resolution": "720p",
                    "ratio": "adaptive",
                    "duration": 4,
                },
                requested_signature="signature-1",
                actor_count=2,
                scene_path=scene,
                first_person=str(person),
                first_clothing=str(clothing),
            )
            self.assertTrue(reused)
            self.assertEqual(job.task_id, "cgt-existing")
            self.assertIn("不重新计费", job.logs[-1])
            mock_resume.assert_called_once_with(job)

    def test_temporary_video_server_supports_private_range_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "depth.mp4"
            video.write_bytes(b"tunnel-video")
            server = web_app.TemporaryVideoServer(video, "known-token")
            url = server.start()
            try:
                missing = requests.get(f"{server.local_origin}/media/wrong-token/depth.mp4", timeout=5)
                self.assertEqual(missing.status_code, 404)
                missing.close()
                response = requests.get(url, headers={"Range": "bytes=0-5"}, timeout=5)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, b"tunnel")
                response.close()
            finally:
                server.close()

    @patch("web_app.validate_seedance_reference_video")
    @patch("web_app.SEEDANCE_WEB.prepare_materials")
    def test_web_generation_uploads_four_local_assets_in_expected_roles(
        self,
        mock_prepare: Mock,
        _mock_validate: Mock,
    ) -> None:
        mock_prepare.return_value = {"submitted": True}
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            files = {}
            for name in ("depth.mp4", "person.png", "clothing.png", "scene.png"):
                path = directory / name
                path.write_bytes(b"test")
                files[name] = path
            job = web_app.WebJob(id="webjob", kind="full", run_dir=directory)
            web_app.run_web_generation(
                job,
                depth_path=files["depth.mp4"],
                person_source=str(files["person.png"]),
                clothing_source=str(files["clothing.png"]),
                scene_source=str(files["scene.png"]),
                options={"prompt": "参考@视频 1，人物为@图片 1。"},
            )

            self.assertEqual(job.status, "submitted")
            kwargs = mock_prepare.call_args.kwargs
            self.assertEqual(kwargs["depth_video"], files["depth.mp4"])
            self.assertEqual(Path(kwargs["person_image"]), files["person.png"])
            self.assertEqual(Path(kwargs["clothing_image"]), files["clothing.png"])
            self.assertEqual(Path(kwargs["scene_image"]), files["scene.png"])
            self.assertTrue(kwargs["submit"])
            self.assertTrue(kwargs["wait_for_result"])
            self.assertEqual(Path(kwargs["output_dir"]), directory)
            self.assertTrue(callable(kwargs["on_progress"]))

    @patch("web_app.validate_seedance_reference_video")
    @patch("web_app.SEEDANCE_WEB.prepare_materials")
    def test_web_generation_marks_success_after_automatic_download(
        self,
        mock_prepare: Mock,
        _mock_validate: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            files = {}
            for name in ("depth.mp4", "person.png", "clothing.png", "scene.png"):
                path = directory / name
                path.write_bytes(b"test")
                files[name] = path
            output = directory / "Seedance_最终成片.mp4"
            output.write_bytes(b"video-result")
            mock_prepare.return_value = {
                "submitted": True,
                "task_id": "cgt-web-test",
                "output_path": str(output),
            }
            job = web_app.WebJob(id="webdone", kind="generate", run_dir=directory)

            web_app.run_web_generation(
                job,
                depth_path=files["depth.mp4"],
                person_source=str(files["person.png"]),
                clothing_source=str(files["clothing.png"]),
                scene_source=str(files["scene.png"]),
                options={"prompt": "参考@视频1，人物为@图片1。"},
            )

            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.progress, 100)
            self.assertEqual(job.task_id, "cgt-web-test")
            self.assertEqual(job.output_path, output)

    @patch("web_app.save_job_record")
    @patch("web_app.download_file")
    @patch("web_app.api_client")
    @patch("web_app.build_seedance_payload")
    @patch("web_app.TempFileMediaStore")
    @patch("web_app.validate_seedance_reference_video")
    @patch("web_app.TosMediaStore.configured", return_value=False)
    def test_generation_uses_free_temporary_upload_without_tos(
        self,
        _mock_tos: Mock,
        mock_validate: Mock,
        mock_free_store_type: Mock,
        mock_build_payload: Mock,
        mock_api_client: Mock,
        mock_download: Mock,
        _mock_save_record: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            depth = directory / "depth.mp4"
            depth.write_bytes(b"depth")
            for filename in ("person.png", "clothing.png", "scene.png"):
                (directory / filename).write_bytes(b"image")
            mock_validate.return_value = SimpleNamespace(size_bytes=5)
            free_store = mock_free_store_type.return_value
            free_store.upload_video.return_value = SimpleNamespace(
                object_key="temporary123",
                signed_url="https://tempfile.org/temporary123/download",
            )
            client = mock_api_client.return_value
            client.create_task.return_value = "cgt-test"
            client.wait_for_task.return_value = {
                "content": {"video_url": "https://example.com/result.mp4"},
                "usage": {},
            }

            def write_output(_url: str, target: Path, **_kwargs) -> None:
                target.write_bytes(b"result")

            mock_download.side_effect = write_output
            mock_build_payload.return_value = {"content": []}
            job = web_app.WebJob(id="freejob", kind="generate", run_dir=directory)
            web_app.run_generation(
                job,
                depth_path=depth,
                depth_reference="",
                person_source=str(directory / "person.png"),
                clothing_source=str(directory / "clothing.png"),
                scene_source=str(directory / "scene.png"),
                options={
                    "prompt": "test",
                    "model": "test-model",
                    "resolution": "720p",
                    "ratio": "adaptive",
                    "duration": 5,
                    "generate_audio": False,
                    "watermark": False,
                    "delete_tos_after": True,
                },
            )

            self.assertEqual(job.status, "succeeded")
            reference = mock_build_payload.call_args.kwargs["depth_video_reference"]
            self.assertEqual(reference, "https://tempfile.org/temporary123/download")
            free_store.upload_video.assert_called_once_with(depth, expires_hours=1)
            free_store.delete.assert_called_once_with("temporary123")


if __name__ == "__main__":
    unittest.main()
