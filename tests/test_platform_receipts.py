"""Only synthetic durable receipts; no cloud or provider requests."""
import hashlib
import unittest
from hybrid_shared import HybridError
from yzzh_local.platform_bridge import reconcile_request
from tests import test_platform_bridge as fixtures


class ReceiptTests(unittest.TestCase):
    setUp = fixtures.PlatformBridgeTests.setUp
    pending = fixtures.PlatformBridgeTests.pending

    def uncertain(self, command='video.create'):
        key = self.pending(command, {}, decision=True, take=True)
        self.bridge.network({'event':'finish', 'operation':self.operation_id, 'id':key, 'ok':False}, self.worker_key)
        self.data = {'id':key, 'owner':71, 'session':self.runtime.session_revision}
        self.receipt = {'request_id':hashlib.sha256(key.encode()).hexdigest(), 'operation':command,
                        'state':'running', 'task_id':'original-platform-task'}
        self.platform.receipt.side_effect = lambda *_: {'result':dict(self.receipt)}
        return key

    def check(self):
        return reconcile_request(self.bridge, self.data)

    def test_confirmed_video_receipt_preserves_journal_and_original_task(self):
        key = self.uncertain()
        result = self.check()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['task_id'], 'original-platform-task')
        self.assertEqual(list(self.bridge.unresolved(71)), [])
        item = self.bridge.pending[key]
        self.assertEqual(item['state_before_reconciliation'], 'uncertain')
        self.assertEqual(item['summary']['operation'], 'video.create')
        self.assertEqual(item['task_id'], 'original-platform-task')
        self.platform.operation.assert_not_called()
        self.platform.upload.assert_not_called()

    def test_definitive_rejection_and_successful_upload_need_no_new_submission(self):
        for state, command in [('failed','video.create'),('succeeded','media.upload')]:
            # A recorded synthetic upload requires byte metadata in request gate;
            # the receipt case reuses the already-reviewed record and changes only command.
            key=self.uncertain()
            self.bridge.pending[key]['summary']['operation']=command
            self.bridge._record(self.bridge.pending[key])
            self.receipt.update(state=state,operation=command)
            self.assertTrue(self.check()['confirmed'])
        self.platform.operation.assert_not_called()
        self.platform.upload.assert_not_called()

    def test_pending_unknown_and_incomplete_video_receipts_do_not_unlock(self):
        self.uncertain()
        for state in ['submitting','uncertain','billing','download_pending','reconciliation','provider_succeeded']:
            self.receipt['state']=state
            self.assertFalse(self.check()['confirmed'])
            self.assertEqual(len(list(self.bridge.unresolved(71))),1)
        self.receipt.update(state='running', task_id='')
        self.assertFalse(self.check()['confirmed'])

    def test_mismatched_receipt_owner_or_session_cannot_update_or_read(self):
        self.uncertain()
        for field, value in [('request_id','0'*64),('operation','image.generate')]:
            before=self.receipt[field]; self.receipt[field]=value
            with self.assertRaises(HybridError):self.check()
            self.receipt[field]=before
        self.platform.receipt.reset_mock()
        for field,value in [('owner',72),('session','different-session')]:
            before=self.data[field];self.data[field]=value
            with self.assertRaises(HybridError):self.check()
            self.data[field]=before
        self.platform.receipt.assert_not_called()
        self.assertEqual(len(list(self.bridge.unresolved(71))),1)

    def test_inflight_request_cannot_be_reconciled_before_its_response(self):
        key=self.pending('video.create', {}, decision=True, take=True)
        with self.assertRaises(HybridError) as caught:
            reconcile_request(self.bridge, {'id':key,'owner':71,'session':self.runtime.session_revision})
        self.assertEqual(caught.exception.code,'PLATFORM_REQUEST_STILL_RUNNING')
        self.platform.receipt.assert_not_called()

    def test_account_switch_during_query_keeps_original_journal(self):
        self.uncertain()
        def response(*_):
            self.runtime.logout()
            return {'result':self.receipt}
        self.platform.receipt.side_effect=response
        with self.assertRaises(HybridError):self.check()
        self.assertEqual(len(list(self.bridge.unresolved(71))),1)

    def test_not_found_keeps_unknown_request_for_future_query(self):
        self.uncertain()
        self.platform.receipt.side_effect=HybridError('REQUEST_NOT_FOUND',404)
        result = self.check()
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['state'], 'not_found')
        self.assertIn('不会重新', result['message'])
        self.assertEqual(len(list(self.bridge.unresolved(71))),1)
