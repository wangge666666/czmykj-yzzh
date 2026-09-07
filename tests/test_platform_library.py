"""Library contracts use synthetic records only; never access account credentials."""
import copy
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock
from workflow_core import WorkflowError
from yzzh_local.platform import PlatformAssetsClient, PlatformTransport, install_platform, platform_library_status
from tests.test_platform_client import capabilities


class PlatformLibraryTests(unittest.TestCase):
    def client(self, responder):
        self.calls = []
        def rpc(command, payload):
            self.calls.append((command, copy.deepcopy(payload)))
            return responder(command, payload)
        return PlatformAssetsClient(PlatformTransport(rpc, Mock(side_effect=AssertionError('No uploads'))))

    def test_all_pages_are_read_without_uploads_and_duplicates_are_not_shown(self):
        rows = [{'Id': f'asset-fixture{i}'} for i in range(245)]
        def reply(command, payload):
            self.assertEqual(command, 'assets.ListAssets')
            start = (payload['PageNumber'] - 1) * 100
            return {'Result': {'Items': rows[start:start+100], 'TotalCount': len(rows), 'LibrarySource': 'canvas-shared-v1'}}
        result = self.client(reply).list_assets(group_type='AIGC', group_ids=['group-fixture'], statuses=['Active'])
        self.assertEqual(result, rows)
        self.assertEqual([request['PageNumber'] for _, request in self.calls], [1, 2, 3])

    def test_old_server_empty_response_is_explicit_upgrade_error(self):
        client = self.client(lambda *_: {'Result': {'Items': []}})
        with self.assertRaisesRegex(WorkflowError, '管理员部署共享角色库更新'):
            client.list_asset_groups(group_type='AIGC')
        self.assertEqual(len(self.calls), 1)

    def test_filtered_empty_page_does_not_hide_later_assets(self):
        client = self.client(lambda _, p: {'Result': {'Items': [] if p['PageNumber'] == 1 else [{'Id': 'asset-found'}],
            'TotalCount': 101, 'LibrarySource': 'canvas-shared-v1'}})
        self.assertEqual(client.list_assets(group_type='AIGC'), [{'Id': 'asset-found'}])

    def test_error_and_stale_cache_warning_survive_public_status(self):
        data = {'configured': True, 'groups': [], 'assets': [], 'read_error': True, 'stale': True, 'message': '旧部署需要更新；当前显示缓存'}
        result = platform_library_status(data)
        self.assertEqual(result['message'], data['message'])
        self.assertEqual(result['storage_mode'], 'platform')
        self.assertTrue(result['stale'])

    def test_cache_is_scoped_to_current_platform_channel_without_reading_old_cache(self):
        for channel in ['primary', 'secondary', 'invalid']:
            web = SimpleNamespace(REAL_CHARACTER_LIBRARY_CACHE_PATH=Path('/synthetic/account/platform/runs/_real_character_library_cache.json'))
            install_platform(web, SimpleNamespace(), Mock(), capabilities(channel=channel))
            suffix = channel if channel != 'invalid' else 'unverified'
            self.assertEqual(web.REAL_CHARACTER_LIBRARY_CACHE_PATH.name, f'_platform_character_library_{suffix}.json')

    def test_invalid_total_or_rows_is_never_a_successful_empty_library(self):
        for total, rows in [(True, []), (-1, []), (1, {}), (1, [{}])]:
            client = self.client(lambda *_: {'Result': {'Items': rows, 'TotalCount': total, 'LibrarySource': 'canvas-shared-v1'}})
            with self.assertRaises(WorkflowError): client.list_asset_groups(group_type='AIGC')


if __name__ == '__main__':
    unittest.main()
