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
