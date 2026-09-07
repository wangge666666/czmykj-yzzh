"""Real isolated engine startup/splitting with synthetic account and video only."""
import copy
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock
from werkzeug.serving import make_server
from workflow_core import resolve_ffmpeg
from tests import test_original_plugin as original
from tests import test_platform_bridge as platform


class PlatformWorkerTests(unittest.TestCase):
    setUp = original.OriginalTests.setUp
    login = original.OriginalTests.login
    call = original.OriginalTests.call

    def test_saved_analysis_format_failure_recovers_in_both_workbenches_without_cloud_calls(self):
        import json
        self.runtime.mode = "platform"
        self.runtime.platform = Mock()
        self.runtime.platform.capabilities.side_effect = lambda *a, **k: copy.deepcopy(platform.capabilities())
        self.runtime.platform.operation.side_effect = AssertionError("Recovery must not create a provider task")
        self.runtime.platform.upload.side_effect = AssertionError("Recovery must not upload")
        self.runtime.settings.load = Mock(side_effect=AssertionError("No BYOK credentials"))
        server = make_server("127.0.0.1", self.port, self.app, threaded=True, request_handler=original.Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.login()
        for page, project, prefix in (
            ("long-video", "long_video_replacement", "long"),
            ("real-long-video", "real_person_long_video", "real_long"),
        ):
            # Project identifiers come from the current implementation.
            import web_app
            project = web_app.REAL_PERSON_LONG_PROJECT if prefix == "real_long" else web_app.VIRTUAL_LONG_PROJECT
            job_id = prefix + "recoveryfixture"
            run = self.root / "data/original/71/platform/runs" / ("20260907_100000_web_" + prefix + "_analyze_" + job_id)
            run.mkdir(parents=True)
            source = run / "shot.mp4"
            source.write_bytes(b"synthetic-local-file")
            manifest = run / ("real_long_manifest.json" if prefix == "real_long" else "long_manifest.json")
            manifest.write_text(json.dumps({
                "local_job_id": job_id, "kind": "long_performance", "project": project,
                "status": "failed", "error": "跨镜人物连续性分析接口没有返回可解析的 JSON。",
                "shots": [{"index": 1, "start": 0, "end": 1, "duration": 1, "source_path": str(source),
                           "performance": {"dialogue": [], "performance": [{"actor_slot": 1, "core_intent": "synthetic"}]}}],
            }))
            html = self.call("/projects/" + page)
            self.assertEqual(html.status_code, 200)
            self.assertIn(b"continuityReviewNotice", html.data)
            response = self.call("/api/jobs/" + job_id)
            self.assertEqual(response.status_code, 200, response.data[:300])
            self.assertEqual(response.json["status"], "succeeded")
            self.assertTrue(response.json["cast_continuity"]["manual_review_required"])
            self.assertEqual(len(response.json["shots"]), 1)
        self.runtime.platform.upload.assert_not_called()
        self.runtime.platform.operation.assert_not_called()
        self.runtime.settings.load.assert_not_called()
        self.assertFalse(self.bridge.pending)

    def test_shared_library_metadata_and_read_failure_reach_original_pages_without_credentials(self):
        self.runtime.mode = 'platform'
        self.runtime.platform = Mock()
        self.runtime.platform.capabilities.side_effect = lambda *_args, **_kw: copy.deepcopy(platform.capabilities())
        self.runtime.settings.load = Mock(side_effect=AssertionError('No BYOK key access'))
        self.runtime.settings.public = Mock(side_effect=AssertionError('No BYOK key access'))
        self.runtime.platform.upload.side_effect = AssertionError('No uploads')
        def operation(_token, request):
            command = request['operation']
            if command == 'assets.ListAssetGroups':
                rows = [{'Id':'group-canvas-primary-12','Name':'公司角色','GroupType':'AIGC','Shared':True,'CanDelete':False,'CanUpload':False}]
            elif command == 'assets.ListAssets':
                rows = [{'Id':'asset-existing1','Name':'原角色','GroupId':'group-canvas-primary-12','GroupType':'AIGC','AssetType':'Image',
                         'Status':'Active','Shared':True,'CanDelete':False,'URL':''}]
            else:
                raise AssertionError('Unexpected cloud action '+command)
            return {'ResponseMetadata':{},'Result':{'Items':rows,'TotalCount':1,'LibrarySource':'canvas-shared-v1'}}
        self.runtime.platform.operation.side_effect = operation
        server = make_server('127.0.0.1', self.port, self.app, threaded=True, request_handler=original.Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.login()
        for project in ('wardrobe','long-video','real-long-video'):
            self.assertEqual(self.call('/projects/'+project).status_code, 200)
        for path in ('/api/character-library','/api/real-long-video/character-library'):
            response = self.call(path)
            self.assertEqual(response.status_code,200,response.data[:300])
            self.assertFalse(response.json['read_error'])
            self.assertTrue(response.json['assets'][0]['shared'])
            self.assertFalse(response.json['assets'][0]['can_delete'])
            self.assertFalse(response.json['groups'][0]['can_upload'])
            self.assertEqual(response.json['storage_mode'],'platform')
        self.runtime.platform.operation.side_effect = lambda *_: {'Result':{'Items':[]}}
        response = self.call('/api/character-library')
        self.assertTrue(response.json['read_error'])
        self.assertTrue(response.json['stale'])
        self.assertEqual(response.json['assets'][0]['id'],'asset-existing1')
        self.assertIn('管理员部署共享角色库更新',response.json['message'])
        self.assertTrue((self.root/'data/original/71/platform/runs/_platform_character_library_primary.json').is_file())
        self.assertFalse(self.bridge.pending)
        self.runtime.platform.upload.assert_not_called()
        self.runtime.settings.load.assert_not_called()

    def test_three_pages_share_platform_engine_and_both_long_projects_split_without_keys(self):
        self.runtime.mode='platform'
        self.runtime.platform=Mock()
        self.runtime.platform.capabilities.side_effect=lambda *_args, **_kw:copy.deepcopy(platform.capabilities())
        self.runtime.platform.operation.side_effect=AssertionError('No cloud operation expected')
        self.runtime.platform.upload.side_effect=AssertionError('No cloud upload expected')
        self.runtime.settings.load=Mock(side_effect=AssertionError('No BYOK key access permitted'))
        self.runtime.settings.public=Mock(side_effect=AssertionError('No BYOK key access permitted'))
        server=make_server('127.0.0.1',self.port,self.app,threaded=True,request_handler=original.Quiet)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.login()
        for page in ('wardrobe','long-video','real-long-video'):
            response=self.call('/projects/'+page)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'yzzh-service-mode" content="platform"',response.data)
        response=self.call('/api/config')
        self.assertEqual(response.status_code,200,response.data[:300])
        self.assertEqual(response.json['plugin_service_mode'],'platform')
        self.assertTrue(response.json['ark_ready'])
        self.assertEqual(response.json['model'],'fixture-white')
        self.assertTrue(response.json['ark_assets_ready'])
        self.assertTrue(response.json['temporary_upload_ready'])
        source=self.root/'synthetic.mp4'
        subprocess.run([str(resolve_ffmpeg()),'-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=12',
                        '-t','2','-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True)
        for project in ('long-video','real-long-video'):
            with source.open('rb') as stream:
                response=self.call('/api/'+project+'/analyze',method='POST',data={'reference_video':(stream,'synthetic.mp4'),'manual_cuts':'1'})
            self.assertEqual(response.status_code,202,response.data[:300])
            job=response.json;deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                job=self.call('/api/jobs/'+job['id']).json
                if job['status'] in {'succeeded','failed'}:break
                time.sleep(.1)
            self.assertEqual(job['status'],'succeeded',job.get('error'))
            self.assertEqual(len(job['shots']),2)
            media=self.call('/api/jobs/'+job['id']+'/shots/1/file/source')
            self.assertEqual(media.status_code,200)
        self.assertTrue((self.root/'data/original/71/platform/runs').is_dir())
        self.assertFalse((self.root/'data/original/71/runs').exists())
        self.assertFalse(self.bridge.pending)
        self.runtime.platform.operation.assert_not_called()
        self.runtime.platform.upload.assert_not_called()
        self.runtime.settings.load.assert_not_called()
