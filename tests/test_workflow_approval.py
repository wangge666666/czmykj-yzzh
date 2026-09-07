"""Synthetic request chains: a clicked workflow is approved once, never globally."""
import copy
import unittest
from unittest.mock import patch

from hybrid_shared import HybridError
from yzzh_local.workflow_approval import workflow_plan
from tests import test_platform_bridge as fixtures


class WorkflowApprovalTests(unittest.TestCase):
    setUp = fixtures.PlatformBridgeTests.setUp
    pending = fixtures.PlatformBridgeTests.pending
    request = fixtures.PlatformBridgeTests.request

    def configure(self, path):
        operation = self.bridge.operations[self.operation_id]
        operation.update(path=path, workflow=workflow_plan(path, "POST", "platform"))
        return operation

    def decide(self, key, approved=True, **extra):
        return self.bridge.decide({"id": key, "owner": 71, "session": self.runtime.session_revision,
            "approved": approved, "workflow_id": self.operation_id, **extra})

    def take(self, key):
        return self.bridge.network({"event": "take", "operation": self.operation_id, "id": key}, self.worker_key)["state"]

    def finish(self, key, ok=True):
        self.bridge.network({"event": "finish", "operation": self.operation_id, "id": key, "ok": ok}, self.worker_key)

    def approvals(self):
        return self.bridge.approvals(71, self.runtime.session_revision)

    def test_three_projects_each_send_two_uploads_and_generation_after_one_decision(self):
        for project in ("wardrobe-swap", "long-video", "real-long-video"):
            with self.subTest(project=project):
                self.operation_id = project
                self.bridge.operations[project] = copy.deepcopy(self.bridge.operations["synthetic-operation"])
                self.configure(f"/api/{project}/white-model")
                for index in range(3):
                    if index < 2:
                        source = self.worker_root / f"{project}-{index}.png"
                        source.write_bytes(b"synthetic reference bytes")
                        command, payload = "media.upload", {"path": str(source)}
                    else:
                        command, payload = "video.create", {"model": "fixture-video", "duration": 6}
                    key = self.pending(command, payload)
                    if index == 0:
                        self.assertEqual(self.take(key), "awaiting_approval")
                        self.assertEqual(len(self.approvals()["pending"]), 1)
                        self.decide(key)
                    self.assertEqual(self.take(key), "send_once")
                    self.assertEqual(self.take(key), "sending", "Taking again must not send twice")
                    self.assertEqual(self.approvals(), {"pending": [], "unresolved": []})
                    self.request(command, payload, key)
                    self.finish(key)
        self.assertEqual(self.platform.upload.call_count, 6)
        self.assertEqual(self.platform.operation.call_count, 3)

    def test_parallel_requests_share_one_card_and_one_decision(self):
        self.configure("/api/wardrobe-swap/extract-references")
        keys = [self.pending("image.generate", {"prompt": str(index)}) for index in range(3)]
        self.assertEqual(len(self.approvals()["pending"]), 1)
        self.decide(keys[0])
        for key in keys:
            self.assertEqual(self.take(key), "send_once")
            self.assertEqual(self.bridge.pending[key]["approved_by"], keys[0])

    def test_cancel_stops_waiting_siblings_and_future_requests(self):
        self.configure("/api/wardrobe-swap/extract-references")
        first = self.pending("image.generate", {})
        second = self.pending("image.generate", {})
        self.decide(first, False)
        self.assertEqual(self.take(second), "rejected")
        with self.assertRaisesRegex(HybridError, "WORKFLOW_CONSENT_ENDED"):
            self.pending("image.generate", {})
        self.platform.operation.assert_not_called()

    def test_old_upload_only_approval_does_not_grant_whole_workflow(self):
        operation = self.configure("/api/wardrobe-swap/white-model")
        key = self.pending("video.create", {}, decision=True, take=True)
        self.finish(key)
        self.assertNotIn("consent", operation)
        next_key = self.pending("video.create", {})
        self.assertEqual(self.take(next_key), "awaiting_approval")

    def test_new_click_never_reuses_previous_consent_and_unknown_routes_stay_per_request(self):
        self.configure("/api/wardrobe-swap/white-model")
        first = self.pending("video.create", {})
        self.decide(first)
        self.take(first)
        self.finish(first)
        with patch.object(self.bridge, "_worker", return_value=self.bridge.workers[71]):
            new_id, _ = self.bridge.operation(71, self.runtime.session_revision, "POST", "/api/wardrobe-swap/white-model")
        self.operation_id = new_id
        key = self.pending("video.create", {})
        self.assertEqual(self.take(key), "awaiting_approval")
        self.assertIsNone(workflow_plan("/api/unrecognized", "POST", "platform"))
        self.assertIsNone(workflow_plan("/api/wardrobe-swap/white-model", "POST", "byok"))
        self.assertIsNone(workflow_plan("/api/wardrobe-swap/white-model", "GET", "platform"))

    def test_out_of_scope_command_and_wrong_workflow_id_are_rejected(self):
        self.configure("/api/wardrobe-swap/white-model")
        key = self.pending("video.create", {})
        with self.assertRaisesRegex(HybridError, "WORKFLOW_CONSENT_CHANGED"):
            self.decide(key, workflow_id="other-operation")
        self.decide(key)
        with self.assertRaisesRegex(HybridError, "WORKFLOW_OPERATION_OUTSIDE_SCOPE"):
            self.pending("assets.DeleteAsset", {"Id": "fixture"})

    def test_uncertain_or_orphaned_sending_blocks_following_requests(self):
        self.configure("/api/wardrobe-swap/white-model")
        key = self.pending("video.create", {})
        self.decide(key)
        self.take(key)
        self.assertEqual(self.approvals()["unresolved"], [])
        self.finish(key, False)
        self.assertEqual(len(self.approvals()["unresolved"]), 1)
        with self.assertRaisesRegex(HybridError, "PREVIOUS_REQUEST_UNCERTAIN"):
            self.pending("video.create", {})
        self.bridge.pending[key]["state"] = "sending"
        self.bridge._record(self.bridge.pending[key])
        del self.bridge.pending[key]
        self.assertEqual(len(self.approvals()["unresolved"]), 1)

    def test_asset_batch_requires_explicit_rights_consent_before_upload(self):
        self.configure("/api/real-long-video/prepare-actors")
        source = self.worker_root / "portrait.png"
        source.write_bytes(b"synthetic portrait")
        key = self.pending("media.upload", {"path": str(source)})
        with self.assertRaisesRegex(HybridError, "ASSET_CONSENT_REQUIRED"):
            self.decide(key)
        self.assertEqual(self.take(key), "awaiting_approval")
        self.decide(key, compliance_confirmed=True)
        self.take(key)
        self.finish(key)
        asset = self.pending("assets.CreateAsset", {"Name": "synthetic portrait"})
        self.assertEqual(self.take(asset), "send_once")
        self.assertIs(self.bridge.pending[asset]["compliance_confirmed"], True)

    def test_account_session_settings_change_invalidates_workflow_grant(self):
        operation = self.configure("/api/wardrobe-swap/white-model")
        key = self.pending("video.create", {})
        self.decide(key)
        for field, changed in (("owner", 72), ("session", "different"), ("revision", "changed")):
            previous = operation[field]
            operation[field] = changed
            with self.assertRaises(HybridError):
                self.take(key)
            operation[field] = previous
        self.platform.operation.assert_not_called()
