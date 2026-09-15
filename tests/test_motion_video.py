from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import web_app as host
from motion_video import prepare_motion_video_reference, verify_motion_video_download
from workflow_core import WorkflowError


class MotionVideoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "compact.mp4"
        self.source.write_bytes(b"complete video bytes")

    def response(self, content, status=200):
        response = Mock(status_code=status)
        response.iter_content.return_value = [content]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_verification_reads_get_body_and_checks_exact_bytes(self):
        with patch("motion_video.requests.get", return_value=self.response(self.source.read_bytes())) as get, patch("motion_video.requests.head") as head:
            verify_motion_video_download("https://tos.example/get-signed.mp4", self.source)
            get.assert_called_once()
            self.assertTrue(get.call_args.kwargs["stream"])
            head.assert_not_called()

    def test_truncated_wrong_content_and_http_error_are_rejected(self):
        for content, status in ((b"partial",200),(b"x"*self.source.stat().st_size,200),(b"denied",403)):
            with self.subTest(status=status,content=content), patch("motion_video.requests.get", return_value=self.response(content,status)) as get:
                with self.assertRaises(WorkflowError):
                    verify_motion_video_download("https://tos.example/video.mp4",self.source)
                self.assertEqual(get.call_count,2)

    def test_download_network_error_is_bounded_and_signed_url_not_exposed(self):
        with patch("motion_video.requests.get", side_effect=requests.ConnectionError("https://tos.example/?signature=secret")) as get:
            with self.assertRaises(WorkflowError) as error:
                verify_motion_video_download("https://tos.example/?signature=secret",self.source)
            self.assertEqual(get.call_count,2)
            self.assertNotIn("secret",str(error.exception))

    def test_motion_uses_scoped_bucket_and_cleans_temporary_object(self):
        tos=Mock()
        tos.upload_video.return_value=Mock(signed_url="https://tos.example/video.mp4",object_key="motion/video.mp4")
        with patch.dict("os.environ",{"MOTION_TRANSFER_TOS_BUCKET":"motion-bucket","TOS_BUCKET":"existing-bucket"}), patch("motion_video.compact_motion_video",return_value=self.source), patch("motion_video.verify_motion_video_download") as verify, patch.object(host,"TosMediaStore",return_value=tos) as store, patch.object(host,"TemporaryPublicTunnel") as tunnel:
            result=prepare_motion_video_reference(self.source,host)
            store.assert_called_once_with(bucket="motion-bucket")
            tos.upload_video.assert_called_once_with(self.source,expires=3600)
            verify.assert_called_once_with("https://tos.example/video.mp4",self.source)
            tunnel.assert_not_called()
            result.close()
            tos.delete.assert_called_once_with("motion/video.mp4")

    def test_failed_verification_cleans_upload_and_never_uses_tunnel(self):
        tos=Mock()
        tos.upload_video.return_value=Mock(signed_url="https://tos.example/video.mp4",object_key="motion/bad.mp4")
        with patch.dict("os.environ",{"MOTION_TRANSFER_TOS_BUCKET":"motion-bucket"}), patch("motion_video.compact_motion_video",return_value=self.source), patch("motion_video.verify_motion_video_download",side_effect=WorkflowError("download failed")), patch.object(host,"TosMediaStore",return_value=tos), patch.object(host,"TemporaryPublicTunnel") as tunnel:
            with self.assertRaisesRegex(WorkflowError,"尚未提交生成任务"):
                prepare_motion_video_reference(self.source,host)
            tos.delete.assert_called_once_with("motion/bad.mp4")
            tunnel.assert_not_called()

    def test_missing_storage_stops_before_encoding_or_generation(self):
        with patch.dict("os.environ",{"MOTION_TRANSFER_TOS_BUCKET":"","TOS_BUCKET":""}), patch("motion_video.compact_motion_video") as encode:
            with self.assertRaisesRegex(WorkflowError,"TOS 存储桶"):
                prepare_motion_video_reference(self.source,host)
            encode.assert_not_called()

    def test_failed_transfer_does_not_create_paid_generation(self):
        child=host.WebJob(id="transfer-failure",kind="motion_white",project="character_motion_transfer",run_dir=self.source.parent)
        cloud=Mock()
        factory=Mock(side_effect=WorkflowError("TOS download validation failed"))
        with patch.object(host,"api_client",return_value=cloud), patch.object(host,"validate_seedance_reference_video"), patch.object(host,"prepare_seedance_stable_video_reference") as legacy:
            host.run_generation(child,depth_path=self.source,depth_reference="",person_source="",clothing_source="",scene_source="",options={},video_only=True,reference_source_factory=factory)
            cloud.create_task.assert_not_called()
            legacy.assert_not_called()
            self.assertEqual(child.status,"failed")
            self.assertIn("TOS download validation failed",child.error)


if __name__ == "__main__":
    unittest.main()
