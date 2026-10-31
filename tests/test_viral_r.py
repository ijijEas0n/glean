"""R 全部使用 mock；禁止真实网络、凭证和用户反馈读写。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from server import main, tikhub, viral, viral_r as r


def candidate(biz='999', author='author', platform='douyin', likes=100, collects=20):
    return dict(id=biz, key=f'{platform}:{biz}', platform=platform, author_id=author, likes=likes, collects=collects)


def page(values=(1, 2, 3, 4, 5), more=False, cursor='next'):
    return {'code': 200, 'data': {'aweme_list': [
        {'aweme_id': str(i+1), 'statistics': {'digg_count': v}, 'create_time': 1000-i} for i, v in enumerate(values)],
        'has_more': more, 'max_cursor': cursor}}


class RTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix='.test-r-'))
        self.path = Path(folder) / 'data/viral-r.json'
        self.enterContext(patch.object(r, 'CACHE_PATH', self.path))
        self.enterContext(patch.object(r, '_PLANS', {}))
        self.feedback = self.enterContext(patch.object(viral, 'load_feedback', return_value={'items': {}, 'authors': {}}))
        self.get = self.enterContext(patch.object(tikhub, 'get', side_effect=AssertionError('禁止真实 TikHub')))
        self.enterContext(patch.object(tikhub, 'post', side_effect=AssertionError('禁止真实 TikHub')))

    def plan(self, rows=None, **kw):
        return r.estimate({'items': rows if rows is not None else [candidate()], **kw})

    def run_plan(self, rows=None, **kw):
        return r.execute(self.plan(rows, **kw))

    def mock_page(self, payload=None):
        self.get.side_effect = None
        self.get.return_value = payload if payload is not None else page()

    def test_median_excludes_target_and_duplicates_odd_even_zero(self):
        history = [{'id': '999', 'interactions': 99999}, {'id': '0', 'interactions': 0},
                   *[{'id': str(i), 'interactions': i*10} for i in range(1, 6)],
                   {'id': '1', 'interactions': 99999}, {'id': 'missing', 'interactions': None}]
        value = r.score(candidate(likes=100), history)
        self.assertEqual((value['samples'], value['median'], value['value'], value['grade']), (6, 25, 4.8, '偏爆'))
        value = r.score(candidate(likes=300, collects=0), history[2:])
        self.assertEqual((value['median'], value['value'], value['grade']), (30, 10, '明显爆'))
        self.assertIn('零互动 1', r.score(candidate(), history)['distribution'])

    def test_small_sample_no_formal_r(self):
        for n in range(5):
            result = r.score(candidate(), [{'id': str(i), 'interactions': i} for i in range(n)])
            self.assertIsNone(result['value'])
            self.assertEqual(result['grade'], '样本不足')

    def test_zero_baseline_not_infinite_or_fake_ratio(self):
        result = r.score(candidate(), [{'id': str(i), 'interactions': 0} for i in range(6)])
        self.assertIsNone(result['value'])
        self.assertEqual(result['median'], 0)
        self.assertEqual(result['grade'], '基线为零')

    def test_twenty_sample_window_and_platform_metrics(self):
        result = r.score(candidate(), [{'id': str(i), 'interactions': i} for i in range(30)])
        self.assertEqual((result['samples'], result['median']), (20, 9.5))
        self.assertEqual(r.interactions(candidate()), 120)
        self.assertEqual(r.interactions(candidate(platform='xiaohongshu')), 120)
        self.assertIsNone(r.interactions(candidate(platform='xiaohongshu', collects=None)))
        result = r.score(candidate(likes=None), [{'id': str(i), 'interactions': 10} for i in range(5)])
        self.assertEqual(result['grade'], '互动待核')
        for likes, grade in [(0, '平常'), (29, '平常'), (30, '偏爆'), (100, '明显爆')]:
            self.assertEqual(r.score(candidate(likes=likes, collects=0), [{'id':str(i), 'interactions':10} for i in range(5)])['grade'], grade)

    def test_estimate_free_and_author_dedup(self):
        plan = self.plan([candidate(), candidate('998')])
        self.assertEqual((plan['authors'], plan['estimated_calls']), (1, 2))
        self.get.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_cache_hit_no_call_includes_timestamp(self):
        self.mock_page()
        first = self.run_plan()
        self.assertEqual(first['calls'], 1)
        saved = json.loads(self.path.read_text())
        self.assertIn('douyin:author', saved)
        self.assertIn('fetched_at', saved['douyin:author'])
        self.get.reset_mock()
        plan = self.plan([candidate('998')])
        self.assertEqual(plan['estimated_calls'], 0)
        second = r.execute(plan)
        self.assertEqual(second['calls'], 0)
        self.assertTrue(second['results']['douyin:998']['cache_hit'])
        self.get.assert_not_called()

    def test_expired_cache_and_expired_plan(self):
        self.mock_page()
        self.run_plan()
        saved = json.loads(self.path.read_text())
        saved['douyin:author']['fetched_at'] -= r.TTL + 1
        self.path.write_text(json.dumps(saved))
        self.assertEqual(self.plan()['estimated_calls'], 2)
        self.assertEqual(self.run_plan()['calls'], 1)
        plan = self.plan()
        r._PLANS[plan['plan_id']]['expires'] = 0
        with self.assertRaisesRegex(ValueError, '过期'):
            r.execute(plan)

    def test_budget_rejects_before_calls_even_direct_execute(self):
        plan = self.plan([candidate(biz=str(900+i), author='u'+str(i)) for i in range(11)])
        self.assertEqual(plan['estimated_calls'], 22)
        self.assertTrue(plan['over_budget'])
        with self.assertRaisesRegex(ValueError, '预算'):
            r.execute(plan)
        self.get.assert_not_called()
        for kwargs in ({'budget':21}, {'budget':0}, {'pages':4}, {'pages':True}, {'worth_only':'yes'}):
            with self.assertRaises(ValueError): self.plan(**kwargs)

    def test_exact_budget_pages_count_and_no_retry_flags(self):
        self.get.side_effect = [page(more=True), page()] * 10
        result = self.run_plan([candidate(biz=str(900+i), author='u'+str(i)) for i in range(10)], pages=2)
        self.assertEqual(result['calls'], 20)
        self.assertEqual(self.get.call_count, 20)
        for call in self.get.call_args_list:
            self.assertEqual(call.kwargs, {'max_attempts':1, 'probe_proxy':False})
        self.assertEqual(self.get.call_args_list[1].args[1]['max_cursor'], 'next')
        self.assertEqual(len(result['results']), 10)

    def test_pagination_target_exclusion_and_recent_order(self):
        self.get.side_effect = [page([999, 2, 4], True), {'code':200, 'data': {'aweme_list':[
            {'aweme_id':str(i), 'create_time':2000+i, 'statistics':{'digg_count':i}} for i in (4, 5, 6, 7)], 'has_more':False}}]
        result = self.run_plan([candidate('1')])
        value = result['results']['douyin:1']
        self.assertEqual((result['calls'], value['samples'], value['median']), (2, 6, 4.5))
        saved = json.loads(self.path.read_text())['douyin:author']['works']
        self.assertEqual(saved[0]['id'], '7')

    def test_xhs_endpoint_nested_response_and_complete_metrics(self):
        notes = [{'id':f'{i:024x}', 'like_count':i, 'collected_count':i*2, 'time':1000-i} for i in range(1, 7)]
        notes.append({'id':'a'*24, 'liked_count':999})  # 收藏缺失不能冒充零
        self.mock_page({'code':200,'data':{'data':{'notes':notes,'has_more':False}}})
        result = self.run_plan([candidate('f'*24, platform='xiaohongshu', likes=30, collects=60)])
        value = result['results']['xiaohongshu:'+'f'*24]
        self.assertEqual((value['samples'], value['median'], value['value']), (6, 10.5, 8.57))
        self.assertEqual(self.get.call_args.args[0], r.USER_POSTS_PATH)
        self.assertEqual(self.get.call_args.args[1], {'user_id':'author'})

    def test_feedback_requires_current_preview_author_and_lists_missing(self):
        self.feedback.return_value['items'] = {'douyin:999':'值得跟','douyin:998':'值得跟','douyin:997':'值得跟','douyin:996':'不相关'}
        plan = self.plan([candidate(),candidate('998', author=''),candidate('996')], worth_only=True)
        self.assertEqual(plan['authors'], 1)
        self.assertEqual(len(plan['missing']), 2)
        self.assertIn('douyin:997', ';'.join(plan['missing']))
        self.get.assert_not_called()

    def test_chinese_errors_stop_batch_and_never_cache_failed_author(self):
        for error, word in [(tikhub.TikHubError('English balance',status=402),'余额不足'),
                            (tikhub.TikHubError('rate limit',status=429),'限流'),
                            (tikhub.TikHubError('网络连接失败'),'网络')]:
            self.get.reset_mock(); self.get.side_effect = error
            result = self.run_plan([candidate(),candidate('998', author='other')])
            self.assertEqual(result['calls'], 1)
            self.assertIn(word, result['errors']['douyin:author'])
            self.assertEqual(len(result['uncomputed']),2)
            self.assertFalse(self.path.exists())
        for payload, word in [({'code':402},'余额不足'),({'code':429},'限流'),({'code':200,'data':{'unexpected':[]}},'结构未识别'),
                              (page(more=True,cursor=''),'分页结构未识别')]:
            self.mock_page(payload)
            self.assertIn(word, self.run_plan()['errors']['douyin:author'])
            self.assertFalse(self.path.exists())

    def test_one_shot_plan_and_concurrent_authors_share_cache(self):
        self.mock_page()
        plans = [self.plan(), self.plan()]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(r.execute, plans))
        self.assertEqual(sum(x['calls'] for x in results), 1)
        with self.assertRaisesRegex(ValueError,'已执行'): r.execute(plans[0])

    def test_corrupt_cache_and_failed_write_report_error(self):
        self.path.parent.mkdir()
        self.path.write_text('broken')
        with self.assertRaisesRegex(ValueError, '缓存格式错误'): self.plan()
        self.assertEqual(self.path.read_text(),'broken')
        self.path.unlink()
        self.mock_page()
        with patch.object(r, '_save', side_effect=OSError('写入失败')):
            result = self.run_plan()
        self.assertIn('写入失败', result['errors']['douyin:author'])
        self.assertEqual(result['results'], {})

    def test_missing_numeric_uid_and_null_author_are_listed(self):
        plan = self.plan([candidate(author='123'), candidate('998', author=None)])
        self.assertEqual(plan['authors'], 0)
        self.assertEqual(len(plan['missing']), 2)
        self.assertIn('sec_uid', plan['missing'][0])
        self.get.assert_not_called()

    def test_cache_expiring_after_estimate_cannot_spend_unconfirmed_calls(self):
        self.mock_page()
        self.run_plan()
        plan = self.plan()
        self.assertEqual(plan['estimated_calls'], 0)
        saved = json.loads(self.path.read_text())
        saved['douyin:author']['fetched_at'] -= r.TTL + 1
        self.path.write_text(json.dumps(saved))
        self.get.reset_mock()
        with self.assertRaisesRegex(ValueError, '重新估算'): r.execute(plan)
        self.get.assert_not_called()

    def test_routes_estimate_and_run(self):
        handler = main.Handler.__new__(main.Handler)
        handler._json, handler._error = Mock(), Mock()
        handler.path = '/api/viral/r/estimate'
        handler._read_json = lambda: {'items':[candidate()]}
        handler.do_POST()
        handler._error.assert_not_called()
        plan = handler._json.call_args.args[0]
        self.get.assert_not_called()
        self.mock_page()
        handler.path = '/api/viral/r'
        handler._read_json = lambda: {'plan_id':plan['plan_id']}
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[0]['calls'],1)
        handler.do_POST()
        handler._error.assert_called_once()

    def test_client_strict_request_never_retries_or_probes(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://mock',429,'rate limit',{},io.BytesIO(b'{}'))
        with patch.object(tikhub, 'tikhub_api_key', return_value='mock'), patch.object(tikhub, 'current_proxy', return_value='') as proxy, \
             patch.object(tikhub, '_build_opener', return_value=opener), patch.object(tikhub, 'detect_proxy', side_effect=AssertionError('禁止探测')), \
             patch.object(tikhub.time, 'sleep', side_effect=AssertionError('禁止重试')):
            with self.assertRaises(tikhub.TikHubError) as exc:
                tikhub.request('/mock', max_attempts=1, probe_proxy=False)
            self.assertEqual(exc.exception.status,429)
            self.assertEqual(opener.open.call_count,1)
            proxy.assert_called_once_with(probe=False)

    def test_no_probe_proxy_only_local_selection(self):
        with patch.object(tikhub, 'load_config', return_value={'proxy':'direct'}), \
             patch.object(tikhub, '_proxy_state', {'checked_at':0}), \
             patch.object(tikhub, 'detect_proxy', side_effect=AssertionError('禁止探测')):
            self.assertEqual(tikhub.current_proxy(probe=False), '')

    def test_frontend_r_flow(self):
        result = subprocess.run(['node', 'tests/viral_r_ui.cjs'], text=True,capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__ == '__main__': unittest.main()
