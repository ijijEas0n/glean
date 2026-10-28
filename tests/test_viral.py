"""禁网、无凭证的候选筛选回归；fixture 反映公开参考的已知包装。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from server import viral, main, store, fetcher, tikhub
from server.jobs import JobManager, wait_for

NOW = datetime.now(timezone.utc) - timedelta(minutes=1)
X1, X2, X3 = '6aad453b00000000110339cb', '6aad453b00000000110339cc', '6aad453b00000000110339cd'


def dy(biz='123', **extra):
    return {'item_id': biz, 'fans_cnt': 1000, 'like_cnt': 10000,
            'item_title': '示例', 'nick_name': '作者', 'publish_time': NOW.timestamp(), **extra}


def xhs(biz=X1, **extra):
    return {'id': biz, 'liked_count': 800, 'collected_count': 300,
            'timestamp': NOW.timestamp(), 'user': {'userid': 'user1', 'nickname': '作者'}, **extra}


class ViralTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.socket', side_effect=AssertionError('测试禁止网络')))
        self.enterContext(patch.object(viral, 'load_config', return_value={'viral': {}}))
        self.key = self.enterContext(patch.object(viral, 'tikhub_api_key', return_value='mock-key'))
        self.enterContext(patch.dict('os.environ', {'GLEAN_VIRAL_ENABLED': '1'}))
        self.local = self.enterContext(patch.object(store, 'find_same_work', return_value=''))
        self.get = self.enterContext(patch.object(tikhub, 'get', side_effect=AssertionError('必须指定 mock 响应')))
        self.post = self.enterContext(patch.object(tikhub, 'post', side_effect=AssertionError('必须指定 mock 响应')))

    def score(self, **patches):
        item = {'platform': 'douyin', 'fans': 1000, 'likes': 10000,
                'published_at': NOW.isoformat(), **patches}
        return viral.score_item(item, now=NOW)

    def test_multiplier_and_zero_fans_do_not_mutate(self):
        item = {'platform': 'douyin', 'fans': 0, 'likes': 10000, 'published_at': NOW.isoformat()}
        scored = viral.score_item(item, now=NOW)
        self.assertEqual(scored['multiplier'], 10000)
        self.assertTrue(scored['passed'])
        self.assertNotIn('multiplier', item)
        self.assertEqual(self.score(fans=3000)['multiplier'], 10000/3000)

    def test_all_thresholds_inclusive_and_unrounded(self):
        item = {'platform': 'douyin', 'fans': 50000, 'likes': 150000,
                'published_at': (NOW-timedelta(days=14)).isoformat()}
        self.assertTrue(viral.passes_threshold(item, now=NOW))
        for field, value in [('fans', 50001), ('likes', 149999),
                             ('published_at', (NOW-timedelta(days=14, seconds=1)).isoformat())]:
            with self.subTest(field=field):
                self.assertFalse(viral.passes_threshold({**item, field: value}, now=NOW))
        self.assertFalse(self.score(likes=9999)['passed'])
        self.assertFalse(self.score(published_at=(NOW+timedelta(seconds=1)).isoformat())['passed'])

    def test_missing_fields_are_unknown(self):
        for field in ('fans', 'likes', 'published_at'):
            self.assertFalse(self.score(**{field: None})['passed'])
        self.assertFalse(self.score(fans='invalid')['passed'])
        self.assertFalse(self.score(fans=-1)['passed'])
        self.assertIsNone(self.score(fans=None)['multiplier'])
        item = {'platform': 'douyin', 'fans': 1, 'likes': 10000}
        self.assertTrue(viral.passes_threshold(item, {'allow_missing_date': True}, now=NOW))
        self.assertIsNone(viral.count('nan'))
        self.assertEqual(viral.count('1.2万+'), 12000)

    def test_platform_metrics(self):
        row = {'fans': 1000, 'likes': 800, 'collects': 300, 'published_at': NOW.isoformat()}
        red = viral.score_item({**row, 'platform': 'xiaohongshu'}, now=NOW)
        self.assertEqual(red['interactions'], 1100)
        self.assertEqual(red['multiplier'], 1.1)
        self.assertTrue(red['passed'])
        self.assertEqual(viral.score_item({**row, 'platform': 'douyin'}, now=NOW)['interactions'], 800)
        self.assertFalse(viral.passes_threshold({**row, 'platform': 'xiaohongshu', 'collects': None}, now=NOW))

    def settings(self, platforms=None, **extra):
        return viral.prepare({'platforms': platforms or list(viral.PLATFORMS), **extra})

    def test_prepare_validates_before_network(self):
        for payload in ({'limit': 0}, {'limit': True}, {'platforms': ['other']}, {'keywords': []},
                        {'keywords': 'AI'}, {'thresholds': {'douyin': {'max_fans': -1}}},
                        {'thresholds': {'douyin': {'max_fans': float('nan')}}},
                        {'thresholds': {'douyin': {'allow_missing_date': 'false'}}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                viral.prepare(payload)
        self.get.assert_not_called(); self.post.assert_not_called()
        with patch.object(viral, 'load_config', return_value={'viral': {'enabled': False}}):
            self.assertRaisesRegex(ValueError, '关闭', self.settings)
        with patch.object(viral, 'load_config', return_value={'viral': {'douyin': {'path': 'https://evil.test'}}}):
            self.assertRaisesRegex(ValueError, 'path', self.settings)
        self.key.return_value = None
        self.assertRaisesRegex(tikhub.TikHubError, '设置', self.settings)

    def test_config_deep_merge_and_path_override(self):
        config = {'viral': {'douyin': {'max_fans': 2000, 'source': 'search',
                                       'path': '/api/v1/douyin/search/fetch_video_search_v2'}}}
        with patch.object(viral, 'load_config', return_value=config):
            settings = self.settings(['douyin'], thresholds={'douyin': {'min_multiplier': 4}})
        self.assertEqual(settings['douyin']['max_fans'], 2000)
        self.assertEqual(settings['douyin']['max_age_days'], 14)
        self.assertEqual(settings['douyin']['min_multiplier'], 4)
        self.post.side_effect = None; self.post.return_value = {'data': {'data': []}}
        viral.preview(settings)
        self.assertEqual(self.post.call_args.args, (settings['douyin']['path'], {'keyword': 'AI', 'cursor': 0}))

    def test_douyin_lowfan_pass_fail_dedup_and_local(self):
        self.post.side_effect = None
        self.post.return_value = {'data': {'data': {'objs': [dy(), dy(), dy('124', fans_cnt=90000),
                        dy('125', like_cnt=1), dy('126', publish_time=None)]}}}
        self.local.return_value = 'douyin-images-123'
        result = viral.preview(self.settings(['douyin']))
        self.assertEqual([x['key'] for x in result['candidates']], ['douyin:123'])
        self.assertEqual(result['candidates'][0]['item_id'], 'douyin-images-123')
        self.assertTrue(result['candidates'][0]['local'])
        self.assertEqual(result['calls'], 1)
        self.assertEqual(self.post.call_args.args[1]['date_window'], 168)
        self.get.assert_not_called()

    def test_xhs_must_enrich_and_cache_author(self):
        def get(path, params):
            if path.endswith('search_notes'):
                return {'data': {'data': {'items': [{'note': xhs()}, {'note': xhs(X2)},
                    {'note': xhs(X3, liked_count=1, collected_count=1)}]}}}
            return {'data': {'data': {'fans': 1000}}}
        self.get.side_effect = get
        result = viral.preview(self.settings(['xiaohongshu']))
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual(result['calls'], 2)
        self.assertEqual(result['candidates'][0]['interactions'], 1100)
        self.assertEqual(result['candidates'][0]['raw']['fans_source'], 'profile')
        self.assertEqual(self.get.call_args.args[1], {'user_id': 'user1'})
        self.post.assert_not_called()

    def test_xhs_over_fans_and_enrichment_failure_drop(self):
        for profile in ({'data': {'fans': 10001}}, {'data': {}}, tikhub.TikHubError('mock 403')):
            def get(path, params):
                if path.endswith('search_notes'):
                    return {'data': {'items': [{'note': xhs()}]}}
                if isinstance(profile, Exception):
                    raise profile
                return profile
            self.get.side_effect = get
            result = viral.preview(self.settings(['xiaohongshu']))
            self.assertEqual(result['candidates'], [])
            if isinstance(profile, Exception):
                self.assertIn('mock 403', ' '.join(result['warnings']))

    def test_douyin_search_wrapper_and_profile_fans(self):
        self.post.side_effect = None
        self.post.return_value = {'data': {'business_data': [{'aweme_info': {
            'aweme_id': '123', 'author': {'sec_uid': 'sec'}, 'statistics': {'digg_count': 10000},
            'create_time': NOW.timestamp()}}]}}
        self.get.side_effect = None; self.get.return_value = {'data': {'user': {'follower_count': 100}}}
        self.assertEqual(len(viral.preview(self.settings(['douyin']))['candidates']), 1)
        self.assertEqual(self.get.call_args.args[1], {'sec_user_id': 'sec'})
        self.assertEqual(viral.profile_fans({'data': {'data': {'interactions': {'list': [
            {'type': 'fans', 'count': '1千+'}]}}}}), 1000)

    def test_budget_and_bad_source_explicit(self):
        self.get.side_effect = None; self.get.return_value = {'data': {'items': [{'note': xhs()}]}}
        settings = self.settings(['xiaohongshu']); settings['max_calls'] = 1
        result = viral.preview(settings)
        self.assertEqual(result['calls'], 1)
        self.assertEqual(result['candidates'], [])
        self.assertIn('上限', ' '.join(result['warnings']))
        self.post.side_effect = tikhub.TikHubError('mock 402')
        self.assertRaisesRegex(tikhub.TikHubError, 'mock 402', viral.preview, self.settings(['douyin']))
        self.assertRaisesRegex(tikhub.TikHubError, '结构', viral.rows_from, {'data': {'unexpected': []}})

    def route(self, path, payload):
        handler = main.Handler.__new__(main.Handler)
        handler.path = path
        handler._read_json = lambda: payload
        handler._json = Mock()
        handler._error = Mock()
        handler.do_POST()
        return handler

    def test_preview_route_and_async_result_no_library_write(self):
        manager = JobManager()
        self.addCleanup(manager.shutdown)
        self.post.side_effect = None; self.post.return_value = {'data': {'data': {'objs': [dy()]}}}
        with patch.object(main, 'JOBS', manager), patch.object(store, 'save_item') as save:
            handler = self.route('/api/viral/preview', {'platforms': ['douyin']})
            self.assertEqual(handler._json.call_args.kwargs['status'], 202)
            job = manager.get(handler._json.call_args.args[0]['job_id']); wait_for(job, timeout=3)
            self.assertEqual(job.state, 'done')
            self.assertEqual(len(job.to_dict()['viral']['candidates']), 1)
            self.assertEqual(job.item_id, '')
            self.assertTrue(any(x['state'] == 'skipped' for x in job.stages))
            save.assert_not_called()

    def test_save_route_dedup_skip_existing_default_no_transcribe(self):
        def existing(p, biz):
            return 'xiaohongshu-images-'+biz if p == 'xiaohongshu' else ''
        self.local.side_effect = existing
        rows = [{'platform': 'douyin', 'id': '123'}, {'url': 'https://www.douyin.com/video/123'},
                {'platform': 'xiaohongshu', 'id': X1}]
        with patch.object(main, 'load_config', return_value={}), patch.object(main.JOBS, 'create_batch') as create:
            create.return_value.id = 'batch-mock'
            handler = self.route('/api/viral/save', {'items': rows})
            self.assertFalse(handler._error.called)
            self.assertEqual(len(create.call_args.args[0]), 1)
            self.assertFalse(create.call_args.args[1]['transcribe'])
            self.assertTrue(create.call_args.args[1]['skip_existing'])
            self.assertEqual(len(handler._json.call_args.args[0]['existing']), 1)
            create.reset_mock()
            self.route('/api/viral/save', {'items': [rows[-1]]})
            create.assert_not_called()
            for items in ([], [{'url': 'https://www.douyin.com/user/sec'}],
                          [{'url': 'https://evil-douyin.com/video/123'}], [{'platform': 'xiaohongshu', 'id': '../bad'}]):
                h = self.route('/api/viral/save', {'items': items})
                self.assertTrue(h._error.called)
            create.assert_not_called()

    def test_save_batch_rechecks_local_without_adapter_or_download(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix='.test-viral-') as tmp:
            root = Path(tmp)
            with patch.object(store, 'LIBRARY_ROOT', root), patch.object(store, 'ITEMS_DIR', root/'items'), \
                 patch.object(store, 'rebuild_index'), patch.object(fetcher, 'get_adapter') as adapter:
                self.local.return_value = 'douyin-video-123'
                result = fetcher.run_batch([{'url': 'https://www.douyin.com/video/123'}], {'skip_existing': True})
                adapter.assert_not_called()
                self.assertEqual(result['children_ok'], 1)

    def test_missing_key_route_does_not_enqueue(self):
        self.key.return_value = None
        with patch.object(main.JOBS, 'create') as create:
            handler = self.route('/api/viral/preview', {})
            self.assertIn('未配置', handler._error.call_args.args[0])
            create.assert_not_called()

    def test_save_missing_key_is_clear_before_enqueue(self):
        self.key.return_value = None
        with patch.object(main.JOBS, 'create_batch') as create:
            handler = self.route('/api/viral/save', {'items': [{'platform': 'douyin', 'id': '123'}]})
            self.assertIn('未配置', handler._error.call_args.args[0])
            create.assert_not_called()
