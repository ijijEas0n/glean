"""禁网、无凭证的候选筛选回归；fixture 反映公开参考的已知包装。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import json
import subprocess
import shutil
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
        return viral.score_item(item, viral.preset("douyin", 1), now=NOW)

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
        self.assertTrue(viral.passes_threshold(item, viral.preset("douyin", 1), now=NOW))
        for field, value in [('fans', 50001), ('likes', 149999),
                             ('published_at', (NOW-timedelta(days=14, seconds=1)).isoformat())]:
            with self.subTest(field=field):
                self.assertFalse(viral.passes_threshold({**item, field: value}, viral.preset("douyin", 1), now=NOW))
        self.assertFalse(self.score(likes=9999)['passed'])
        self.assertFalse(self.score(published_at=(NOW+timedelta(seconds=1)).isoformat())['passed'])

    def test_missing_fields_are_unknown(self):
        for field in ('fans', 'likes', 'published_at'):
            self.assertFalse(self.score(**{field: None})['passed'])
        self.assertFalse(self.score(fans='invalid')['passed'])
        self.assertFalse(self.score(fans=-1)['passed'])
        self.assertIsNone(self.score(fans=None)['multiplier'])
        item = {'platform': 'douyin', 'fans': 1, 'likes': 10000}
        self.assertFalse(viral.passes_threshold(item, {'allow_missing_date': True}, now=NOW))
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


    def test_cover_extracted_from_platform_payloads(self):
        dy = viral.normalize('douyin', {
            'aweme_id': '1234567890123456789', 'desc': '标题',
            'author': {'nickname': '作者', 'sec_uid': 'sec'},
            'statistics': {'digg_count': 10},
            'video': {'cover': {'url_list': ['https://cdn.example/douyin-cover.jpg']}},
            'create_time': NOW.timestamp(),
        })
        self.assertEqual(dy['cover'], 'https://cdn.example/douyin-cover.jpg')
        lowfan = viral.normalize('douyin', {
            'item_id': '1234567890123456787', 'item_title': '低粉榜',
            'nick_name': '作者', 'like_cnt': 10, 'fans_cnt': 100,
            'item_cover_url': 'https://p3-sign.douyinpic.com/cover.jpeg',
            'publish_time': NOW.timestamp(),
        })
        self.assertEqual(lowfan['cover'], 'https://p3-sign.douyinpic.com/cover.jpeg')
        red = viral.normalize('xiaohongshu', {
            'note_id': X1, 'title': '笔记',
            'user': {'nickname': '作者', 'userid': 'u1'},
            'liked_count': 1, 'collected_count': 1, 'timestamp': NOW.timestamp(),
            'images_list': [{'url_size_large': 'https://sns-img.xhscdn.com/cover.jpg'}],
        })
        self.assertEqual(red['cover'], 'https://sns-img.xhscdn.com/cover.jpg')
        empty = viral.normalize('douyin', {
            'aweme_id': '1234567890123456788', 'desc': '无封面',
            'author': {'nickname': '作者'}, 'statistics': {'digg_count': 1},
            'create_time': NOW.timestamp(),
        })
        self.assertEqual(empty['cover'], '')

    def settings(self, platforms=None, **extra):
        return viral.prepare({'platforms': platforms or list(viral.PLATFORMS), 'max_pages': 1, 'douyin_sources': ['lowfan'], 'publish_time': 0, **extra})

    def test_depth_defaults_and_validation(self):
        settings = viral.prepare({})
        self.assertEqual(settings['source_window_hours'], 168)
        self.assertEqual(settings['max_pages'], 2)
        for name, values in {'source_window_hours': [23, 721, True, 168.5, '168', None],
                             'max_pages': [0, 4, True, 1.5, '2', None]}.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.settings(**{name: value})
        for hours in (24, 72, 168):
            self.assertEqual(self.settings(source_window_hours=hours)['source_window_hours'], hours)
        with patch.object(viral, 'load_config', return_value={'viral': {'max_pages': 3, 'source_window_hours': 72}}):
            self.assertEqual(viral.prepare({})['max_pages'], 3)
            self.assertEqual(viral.prepare({})['source_window_hours'], 72)
            self.assertEqual(viral.prepare({'source_window_hours': 24})['source_window_hours'], 24)
        self.get.assert_not_called(); self.post.assert_not_called()

    def test_batch_keywords_limit_strip_and_dedup(self):
        words = [f'词{i}' for i in range(20)]
        self.assertEqual(self.settings(keywords=words)['douyin']['keywords'], words)
        self.assertEqual(self.settings(keywords=[' AI ', 'AI', ' 科普 '])['douyin']['keywords'], ['AI', '科普'])
        self.assertEqual(self.settings(keywords=[' ' + '词'*80 + ' '])['douyin']['keywords'], ['词'*80])
        for words in ([str(i) for i in range(21)], ['词'*81], [' '], [1]):
            with self.subTest(words=words), self.assertRaises(ValueError):
                self.settings(keywords=words)

    def test_multiple_pages_parameters_order_and_coverage(self):
        order = []
        def post(path, params):
            order.append(('douyin', params.copy()))
            return {'data': {'objs': [dy()]}}
        def get(path, params):
            if path.endswith('search_notes'):
                order.append(('xiaohongshu', params.copy()))
                return {'data': {'items': [xhs()]}}
            return {'data': {'fans': 1000}}
        self.post.side_effect = post; self.get.side_effect = get
        result = viral.preview(self.settings(keywords=['A', 'B'], max_pages=3, source_window_hours=72))
        expected = [(p, k, page) for page in (1, 2, 3) for p in viral.PLATFORMS for k in ('A', 'B') if page == 1 or p == 'xiaohongshu']
        self.assertEqual([(p, q['keyword'], q['page']) for p, q in order], expected)
        for p, query in order:
            if p == 'douyin':
                self.assertEqual(query['date_window'], 168)
                self.assertEqual(query['page_size'], 50)
            else:
                self.assertEqual(query['sort_type'], 'popularity_descending')
        self.assertEqual(result['scanned'], 8)
        self.assertEqual(len(result['pool']), 2)
        self.assertEqual(result['calls'], 9)
        self.assertEqual(result['coverage']['source_calls'], {'douyin': 2, 'xiaohongshu': 6})
        self.assertEqual(result['coverage']['pages_by_keyword']['douyin'], {'A': 1, 'B': 1})
        self.assertEqual(result['coverage']['source_window_hours'], {'douyin': 168, 'xiaohongshu': None})
        self.assertFalse(result['coverage']['incomplete'])

    def test_budget_prioritizes_keywords_before_deeper_pages_and_profiles(self):
        order = []
        def post(path, params):
            order.append(('douyin', params['keyword'], params['page']))
            return {'data': {'objs': [dy(fans_cnt=None, sec_uid='author')]}}
        def get(path, params):
            self.assertTrue(path.endswith('search_notes'), '不应先花预算补粉')
            order.append(('xiaohongshu', params['keyword'], params['page']))
            return {'data': {'items': [xhs()]}}
        self.post.side_effect = post; self.get.side_effect = get
        settings = self.settings(keywords=['A', 'B'], max_pages=3)
        settings['max_calls'] = 5
        result = viral.preview(settings)
        self.assertEqual(order, [('douyin', 'A', 1), ('douyin', 'B', 1),
                                ('xiaohongshu', 'A', 1), ('xiaohongshu', 'B', 1), ('xiaohongshu', 'A', 2)])
        self.assertEqual(result['calls'], 5)
        self.assertEqual(result['coverage']['authors_checked'], 0)
        self.assertTrue(result['coverage']['incomplete'])
        self.assertIn('上限', ' '.join(result['warnings']))

    def test_empty_page_stops_only_that_keyword(self):
        self.post.side_effect = lambda path, q: {'data': {'objs': [] if q['keyword'] == 'A' else [dy()]}}
        result = viral.preview(self.settings(['douyin'], keywords=['A', 'B'], max_pages=3))
        self.assertEqual([(c.args[1]['keyword'], c.args[1]['page']) for c in self.post.call_args_list],
                         [('A', 1), ('B', 1)])
        self.assertEqual(result['coverage']['pages_by_keyword']['douyin'], {'A': 1, 'B': 1})

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
            settings = self.settings(['douyin'], douyin_sources=['search'], thresholds={'douyin': {'min_multiplier': 4}})
        self.assertEqual(settings['douyin']['max_fans'], 2000)
        self.assertEqual(settings['douyin']['max_age_days'], 14)
        self.assertEqual(settings['douyin']['min_multiplier'], 4)
        self.post.side_effect = None; self.post.return_value = {'data': {'data': []}}
        viral.preview(settings)
        self.assertEqual(self.post.call_args.args, (settings['douyin']['path'], {'keyword': 'AI', 'cursor': 0, 'sort_type':'1', 'publish_time':'0', 'search_id':'', 'backtrace':''}))

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
        self.assertRaisesRegex(tikhub.TikHubError, 'TikHub 余额不足，请充值', viral.preview, self.settings(['douyin']))
        self.assertRaisesRegex(tikhub.TikHubError, '结构', viral.rows_from, {'data': {'unexpected': []}})

    def fixture(self, name):
        return json.loads((Path(__file__).parent / 'fixtures' / 'viral' / (name + '.json')).read_text())

    def test_real_search_shapes_title_and_zero_fans(self):
        rows = viral.rows_from(self.fixture('dy-general-0'))
        works = [viral.normalize('douyin', x) for x in rows]
        self.assertGreater(sum(x is None for x in works), 0)  # 真实用户卡被跳过
        works = [x for x in works if x]
        self.assertEqual(len(works), 16)
        self.assertTrue(all(x['fans'] == 0 for x in works))
        card = self.fixture('dy-week-0')['data']['business_data'][0]
        card['data']['aweme_info']['title'] = ''
        card['data']['aweme_info']['item_title'] = ''
        self.assertEqual(viral.normalize('douyin', card)['title'], card['data']['aweme_info']['desc'][:500])
        self.assertEqual(len([x for x in viral.rows_from(self.fixture('dy-likes-0')) if viral.normalize('douyin', x)]), 7)
        red = [viral.normalize('xiaohongshu', x) for x in viral.rows_from(self.fixture('xhs-monitor-0'))]
        self.assertEqual(len([x for x in red if x]), 20)
        self.assertTrue(all(x['published_at'] and x['likes'] is not None and x['collects'] is not None for x in red if x))

    def test_new_defaults_and_time_validation(self):
        settings = viral.prepare({})
        self.assertEqual(settings['publish_time'], 7)
        self.assertEqual(settings['douyin_sources'], ['search', 'lowfan'])
        self.assertEqual(settings['max_calls'], 24)
        self.assertTrue(settings['douyin']['path'].endswith('fetch_video_search_v2'))
        for value in (True, None, '7', 14, 7.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                viral.prepare({'publish_time': value})
        for value in ([], ['other'], 'search'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                viral.prepare({'douyin_sources': value})

    def test_real_continuation_and_both_time_parameters(self):
        for days, label in ((7, '一周内'), (180, '半年内'), (0, '不限')):
            self.post.side_effect = None; self.get.side_effect = None
            self.post.return_value = self.fixture('dy-likes-0')
            self.get.return_value = self.fixture('xhs-monitor-0')
            self.post.reset_mock(); self.get.reset_mock()
            result = viral.preview(self.settings(max_pages=2, publish_time=days, douyin_sources=['search']))
            queries = [c.args[1] for c in self.post.call_args_list]
            cfg = self.fixture('dy-likes-0')['data']['business_config']
            self.assertEqual(queries[0]['publish_time'], str(days))
            self.assertEqual(queries[0]['sort_type'], '1')
            self.assertEqual(queries[1]['cursor'], cfg['next_page']['cursor'])
            self.assertEqual(queries[1]['search_id'], cfg['next_page']['search_id'])
            self.assertEqual(queries[1]['backtrace'], cfg['backtrace'])
            red_queries = [c.args[1] for c in self.get.call_args_list if c.args[0].endswith('search_notes')]
            self.assertEqual(red_queries[0]['time_filter'], label)
            for key in ('search_id', 'search_session_id'):
                self.assertEqual(red_queries[1][key], self.fixture('xhs-monitor-0')['data'][key])
            self.assertEqual(result['coverage']['publish_time'], days)

    def test_legacy_path_cannot_redirect_search_to_board(self):
        config = {'viral':{'douyin':{'source':'lowfan', 'path':'/api/v1/douyin/billboard/fetch_hot_total_low_fan_list'}}}
        self.post.side_effect = None; self.post.return_value = {'data':{'business_data':[]}}
        with patch.object(viral, 'load_config', return_value=config):
            settings = viral.prepare({'platforms':['douyin'], 'douyin_sources':['search']})
        viral.preview(settings)
        self.assertTrue(self.post.call_args.args[0].endswith('fetch_video_search_v2'))
        config['viral']['douyin_sources'] = ['search']
        with patch.object(viral, 'load_config', return_value=config):
            self.assertEqual(viral.prepare({})['douyin_sources'], ['search'])

    def test_outside_publish_range_does_not_spend_on_profiles(self):
        self.post.side_effect = None
        self.post.return_value = {'data':{'objs':[dy(fans_cnt=None, sec_uid='u', publish_time=(NOW-timedelta(days=8)).timestamp())]}}
        result = viral.preview(self.settings(['douyin'], publish_time=7))
        self.get.assert_not_called()
        self.assertEqual(result['calls'], 1)

    def test_real_has_more_false_and_missing_cursor_stop(self):
        self.post.side_effect = None; self.post.return_value = self.fixture('dy-week-0')
        viral.preview(self.settings(['douyin'], max_pages=3, douyin_sources=['search']))
        self.assertEqual(self.post.call_count, 1)
        self.post.reset_mock(); self.post.return_value = {'data': {'business_data': [{'aweme_info': {
            'aweme_id':'123', 'statistics':{'digg_count':1}}}]}}
        result = viral.preview(self.settings(['douyin'], max_pages=3, douyin_sources=['search']))
        self.assertEqual(self.post.call_count, 1)
        self.assertTrue(result['coverage']['incomplete'])

    def test_zero_fans_pending_then_cached_profile(self):
        response = self.fixture('dy-general-0')
        cards = [c for c in response['data']['business_data'] if viral.normalize('douyin', c)][:2]
        for i, card in enumerate(cards):
            work = card['data']['aweme_info']
            work['aweme_id'] = str(100+i)
            work['author']['sec_uid'] = 'same-author'
            work['create_time'] = NOW.timestamp()
            work['statistics']['digg_count'] = 10000
        response['data']['business_data'] = cards
        self.post.side_effect = None; self.post.return_value = response
        self.get.side_effect = None; self.get.return_value = {'data': {'user': {'follower_count':1000}}}
        settings = self.settings(['douyin'], douyin_sources=['search'])
        settings['max_calls'] = 1
        result = viral.preview(settings)
        self.assertTrue(all(x['fans'] is None and x['pending'] for x in result['pool']))
        self.assertFalse(result['candidates'])
        self.get.assert_not_called()
        result = viral.preview(self.settings(['douyin'], douyin_sources=['search']))
        self.assertEqual(self.get.call_count, 1)
        self.assertEqual(len(result['candidates']), 2)
        self.assertTrue(all(x['raw']['fans_source'] == 'profile' for x in result['pool']))

    def test_cross_source_keyword_conflicts_keep_first_metrics(self):
        def post(path, params):
            if 'billboard' in path:
                return {'data': {'objs':[dy(like_cnt=1, fans_cnt=5, desc='仅补充来源的广告说明', cha_list=[{'cha_name':'优惠'}], promotion={'id':'product'})]}}
            return {'data': {'business_data':[{'data': {'aweme_info': {
                'aweme_id':'123', 'author':{'follower_count':1000},
                'statistics':{'digg_count':10000}, 'create_time':NOW.timestamp()}}}],
                'has_more':False}}
        self.post.side_effect = post
        result = viral.preview(self.settings(['douyin'], keywords=['A','B'], douyin_sources=['lowfan','search']))
        row, = result['pool']
        self.assertEqual((row['likes'],row['fans']), (10000,1000))
        self.assertEqual(row['source'], 'douyin:search')
        self.assertTrue(row['fetched_at'])
        self.assertEqual(row['raw']['metrics_source'], row['metrics_source'])
        self.assertEqual(len(row['raw']['alternatives']), 3)
        self.assertEqual(row['raw']['alternatives'][-1]['likes'], 1)
        self.assertIn('仅补充来源的广告说明', row['description'])
        self.assertIn('优惠', row['topics'])
        self.assertEqual(row['commerce_fields'], ['promotion'])
        self.assertEqual(result['calls'], 4)
        self.assertEqual(result['coverage']['pages_by_source']['douyin:lowfan'], {'A':1,'B':1})

    def test_local_date_recheck_caps_search_range(self):
        self.post.side_effect = None
        self.post.return_value = {'data': {'objs':[dy(publish_time=(NOW-timedelta(days=8)).timestamp())]}}
        result = viral.preview(self.settings(['douyin'], publish_time=7))
        self.assertEqual(result['candidates'], [])
        self.assertFalse(result['pool'][0]['strict_pass'])
        self.assertEqual(result['preset']['name'], '宽松')
        self.assertEqual(len(viral.preview(self.settings(['douyin'], publish_time=180))['candidates']), 1)

    def test_balance_error_response_and_exception_stop_all_calls(self):
        error = self.fixture('balance-402')
        for response in (error, {'code':402,'message':'Insufficient balance'}, {'data':error}):
            self.post.side_effect = None; self.post.return_value = response; self.post.reset_mock()
            with self.assertRaisesRegex(tikhub.TikHubError, 'TikHub 余额不足，请充值'):
                viral.preview(self.settings(['douyin'], keywords=['A','B'], douyin_sources=['search','lowfan']))
            self.assertEqual(self.post.call_count, 1)
        self.post.side_effect = tikhub.TikHubError('payment', status=402)
        with self.assertRaisesRegex(tikhub.TikHubError, 'TikHub 余额不足，请充值'):
            viral.preview(self.settings(['douyin']))
        self.post.side_effect = None
        self.post.return_value = {'data': {'objs':[dy(fans_cnt=None, sec_uid='u')]}}
        self.get.side_effect = tikhub.TikHubError('payment', status=402)
        with self.assertRaisesRegex(tikhub.TikHubError, 'TikHub 余额不足，请充值'):
            viral.preview(self.settings(['douyin']))
        # 作品 ID 中包含 402 不应误判成余额不足。
        self.assertEqual(len(viral.rows_from({'data':{'objs':[dy('14021')]}})), 1)

    def test_search_first_screens_then_depth_then_profiles_budget_and_scan(self):
        order = []
        def post(path, q):
            order.append(('lowfan' if 'billboard' in path else 'search', q['keyword'], q.get('cursor',0)))
            return {'data':{'objs':[dy(fans_cnt=None, sec_uid='u')], 'cursor': q.get('cursor',0)+8, 'has_more':1}}
        self.post.side_effect = post
        self.get.side_effect = None; self.get.return_value = {'data':{'fans':1000}}
        result = viral.preview(self.settings(['douyin'], keywords=['A','B'], max_pages=3, douyin_sources=['search','lowfan']))
        self.assertEqual(order, [('search','A',0),('search','B',0),('lowfan','A',0),('lowfan','B',0),
                                 ('search','A',8),('search','B',8),('search','A',16),('search','B',16)])
        self.assertEqual(result['calls'], 9)
        self.post.side_effect = None; self.post.return_value = {'data':{'objs':[dy(str(i)) for i in range(350)]}}
        result = viral.preview(self.settings(['douyin'], douyin_sources=['search','lowfan']))
        self.assertEqual(result['scanned'], 300)
        self.assertEqual(result['calls'], 1)
        self.assertTrue(result['coverage']['incomplete'])

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
            handler = self.route('/api/viral/preview', {'platforms': ['douyin'], 'douyin_sources':['lowfan']})
            self.assertEqual(handler._json.call_args.kwargs['status'], 202)
            job = manager.get(handler._json.call_args.args[0]['job_id']); wait_for(job, timeout=3)
            self.assertEqual(job.state, 'done')
            self.assertEqual(len(job.to_dict()['viral']['candidates']), 1)
            self.assertEqual([c.args[1]['page'] for c in self.post.call_args_list], [1])
            self.assertEqual(job.to_dict()['viral']['coverage']['max_pages'], 2)
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

    def test_presets_default_and_config_tier(self):
        expected = {
            'douyin': [(30000,20000,5,7),(50000,10000,3,14),(50000,3000,1,14),(50000,1000,.3,14),(100000,300,.1,30)],
            'xiaohongshu': [(5000,2000,2,7),(10000,1000,1,14),(10000,500,.5,14),(10000,300,.1,14),(30000,100,.05,30)]}
        self.assertEqual(self.settings()['tier'], 3)
        from server.config import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG['viral']['tier'], 3)
        for p, rows in expected.items():
            for tier, values in enumerate(rows):
                settings = self.settings(tier=tier)
                self.assertEqual(tuple(settings[p][k] for k in viral.THRESHOLDS), values)
        for tier in (-1, 5, True, '3', 1.5):
            with self.assertRaises(ValueError):
                self.settings(tier=tier)
        with patch.object(viral, 'load_config', return_value={'viral': {'tier': 2}}):
            self.assertEqual(self.settings()['douyin']['min_interactions'], 3000)
        with patch.object(viral, 'load_config', return_value={'viral': {'douyin': {'min_interactions': 99}}}):
            self.assertEqual(self.settings(tier=1)['douyin']['min_interactions'], 10000)

    def test_same_batch_strict_zero_loose_has_candidates(self):
        self.post.side_effect = None
        self.post.return_value = {'data': {'objs': [dy(like_cnt=1500), dy('124', like_cnt=10)]}}
        loose = viral.preview(self.settings(['douyin']))
        strict = viral.preview(self.settings(['douyin'], tier=1))
        self.assertEqual(len(loose['candidates']), 1)
        self.assertEqual(strict['candidates'], [])
        self.assertEqual(len(strict['pool']), 2)
        self.assertEqual(loose['stats']['strict_pass'], 0)
        self.assertEqual(loose['stats']['tier_pass'], 1)
        calls = self.post.call_count
        rerun = [viral.score_item(x, viral.preset('douyin', 3)) for x in strict['pool']]
        self.assertEqual(sum(x['passed'] for x in rerun), 1)
        self.assertEqual(self.post.call_count, calls)
        self.get.assert_not_called()
        self.assertNotIn('statistics', strict['pool'][0]['raw'])

    def test_missing_fans_and_date_stay_pending_in_pool(self):
        self.post.side_effect = None
        self.post.return_value = {'data': {'objs': [dy(fans_cnt=None), dy('124', publish_time=None)]}}
        result = viral.preview(self.settings(['douyin'], thresholds={'douyin': {'allow_missing_date': True}}))
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['stats']['pending'], 2)
        self.assertEqual(result['stats']['pending_fans'], 1)
        self.assertEqual(result['stats']['pending_date'], 1)
        self.assertTrue(all(x['pass_level'] == 'pending' for x in result['pool']))
        self.assertIsNone(result['pool'][0]['multiplier'])
        self.get.assert_not_called()

    def test_sources_reserved_before_author_budget(self):
        order = []
        def get(path, params):
            order.append(path)
            if path.endswith('search_notes'):
                return {'data': {'items': [xhs(params['keyword'].replace('A', X1).replace('B', X2))]}}
            return {'data': {'fans': 1000}}
        self.get.side_effect = get
        settings = self.settings(['xiaohongshu'], keywords=['A', 'B'])
        settings['max_calls'] = 2
        result = viral.preview(settings)
        self.assertEqual(result['calls'], 2)
        self.assertTrue(all(path.endswith('search_notes') for path in order))
        self.assertTrue(result['coverage']['incomplete'])
        self.assertEqual(result['coverage']['authors_checked'], 0)
        self.assertEqual(result['stats']['pending_fans'], 2)
        self.assertEqual(result['stats']['recognized'], 2)

    def test_scan_cap_and_limit_do_not_truncate_pool(self):
        self.post.side_effect = None
        self.post.return_value = {'data': {'objs': [dy(str(i)) for i in range(310)]}}
        result = viral.preview(self.settings(['douyin'], limit=1))
        self.assertEqual(len(result['pool']), 300)
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['stats']['tier_pass'], 300)
        self.assertTrue(result['coverage']['incomplete'])

    @unittest.skipUnless(shutil.which('node'), '需要 node 验证前端纯函数')
    def test_frontend_presets_and_local_filter_match_backend(self):
        rows = []
        for p in viral.PLATFORMS:
            for fans, likes, collects, age in [(1000,1500,300,1),(None,20000,1,1),
                    (1000,20000,100,None),(100000,100000,100,29),(0,300,100,3),
                    (1000,None,100,2),(50000,15000,0,14),(1,100000,1,-1)]:
                item = {'platform': p, 'fans': fans, 'likes': likes, 'collects': collects,
                        'published_at': (NOW-timedelta(days=age)).isoformat() if age is not None else None}
                rows.append(viral.score_item(item, now=NOW))
        root = Path(__file__).resolve().parents[1]
        code = """const f=require('./web/viral-filter.js');
const fs=require('fs'); const rows=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify({presets:Object.fromEntries(['douyin','xiaohongshu'].map(p=>[p,Array.from({length:5},(_,i)=>f.preset(p,i))])),
scores:rows.map(r=>Array.from({length:5},(_,i)=>f.score(r,f.preset(r.platform,i))))}));"""
        result = subprocess.run(['node', '-e', code], cwd=root, input=json.dumps(rows), text=True, capture_output=True, check=True)
        front = json.loads(result.stdout)
        self.assertEqual(front['presets'], {p: [viral.preset(p,t) for t in range(5)] for p in viral.PLATFORMS})
        for row, scores in zip(rows, front['scores']):
            for tier, score in enumerate(scores):
                backend = viral.score_item(row, viral.preset(row['platform'], tier), now=NOW)
                self.assertEqual(score['passed'], backend['passed'])
                self.assertEqual(score['fail_reasons'], backend['fail_reasons'])
                self.assertEqual(score['pending'], backend['pending'])

    @unittest.skipUnless(shutil.which('node'), '需要 node 验证页面交互')
    def test_slider_interactions_without_network(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', 'tests/viral_ui.cjs'], cwd=root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_current_tier_plans_enrichment_without_spending_on_low_rows(self):
        def get(path, params):
            if path.endswith('search_notes'):
                return {'data': {'items': [xhs(liked_count=300, collected_count=100),
                    xhs(X2, liked_count=10, collected_count=1, user={'userid': 'low'}),
                    xhs(X3, timestamp=None, user={'userid': 'no-date'})]}}
            return {'data': {'fans': 1000}}
        self.get.side_effect = get
        loose = viral.preview(self.settings(['xiaohongshu'], tier=3))
        self.assertEqual(loose['calls'], 2)
        self.assertEqual(loose['stats']['tier_pass'], 1)
        self.assertEqual(loose['stats']['pending_fans'], 2)
        self.assertEqual(loose['preset'], {'id': 3, 'name': '宽松'})
        self.get.reset_mock()
        strict = viral.preview(self.settings(['xiaohongshu'], tier=1))
        self.assertEqual(strict['calls'], 1)
        self.assertEqual(strict['stats']['pending_fans'], 3)
        self.assertEqual(len(strict['pool']), 3)
        self.assertEqual(self.get.call_count, 1)
