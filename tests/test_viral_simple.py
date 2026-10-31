"""新互动口径与缓存迁移：完全离线，不读取用户配置。"""
from datetime import datetime, timezone
from pathlib import Path
import json
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from server import viral, viral_r


class SimpleRulesTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.socket', side_effect=AssertionError('禁止网络')))
        folder = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix='.test-simple-'))
        self.path = Path(folder) / 'cache.json'
        self.enterContext(patch.object(viral_r, 'CACHE_PATH', self.path))

    def test_search_fixture_collection_and_sum(self):
        data = json.loads((Path(__file__).parent / 'fixtures/viral/dy-likes-0.json').read_text())
        row = viral.normalize('douyin', viral.rows_from(data)[0])
        self.assertEqual(row['collects'], 694950)
        self.assertEqual(viral.interactions(row), 849430 + 694950)
        self.assertEqual(viral.metric_notes(row), [])

    def test_real_offline_lowfan_missing_collect_is_not_favorite_flag(self):
        data = json.loads((Path(__file__).parent / 'fixtures/viral/dy-lowfan-missing-collect.json').read_text())
        row = viral.normalize('douyin', viral.rows_from(data)[0])
        self.assertIsNone(row['collects'])
        self.assertEqual(viral.interactions(row), row['likes'])
        self.assertEqual(viral.metric_notes(row), ['收藏缺失'])

    def test_collection_aliases_zero_missing_and_invalid(self):
        for field in ('collect_count', 'collect_cnt', 'collected_count', 'collected', 'collects'):
            for container in ('flat', 'statistics'):
                metrics = {'digg_count': 900, field: 400}
                row = viral.normalize('douyin', {'aweme_id': '123', **(metrics if container == 'flat' else {'statistics': metrics})})
                self.assertEqual(viral.interactions(row), 1300)
                self.assertEqual(viral.metric_notes(row), [])
        for value, notes in [(None, ['收藏缺失']), ('bad', ['收藏缺失']), (-1, ['收藏缺失']), (0, [])]:
            row = {'platform': 'douyin', 'fans': 1000, 'likes': 1200, 'collects': value,
                   'published_at': datetime.now(timezone.utc).isoformat()}
            result = viral.score_item(row)
            self.assertEqual(result['interactions'], 1200)
            self.assertTrue(result['passed'])
            self.assertEqual(result['metric_notes'], notes)
            self.assertEqual(viral_r.interactions(row), result['interactions'])
        self.assertIsNone(viral.interactions({'platform': 'douyin', 'likes': None, 'collects': 500}))
        self.assertIsNone(viral.interactions({'platform': 'xiaohongshu', 'likes': 500}))

    def test_author_posts_same_sum_median_and_missing_evidence(self):
        response = {'data': {'aweme_list': [
            {'aweme_id': str(i), 'statistics': {'digg_count': 50, 'collect_count': 50}} for i in range(5)
        ] + [{'aweme_id': '5', 'statistics': {'digg_count': 100}}]}}
        works, _, _ = viral_r._page('douyin', response)
        result = viral_r.score({'id': '999', 'platform': 'douyin', 'likes': 800, 'collects': 400}, works)
        self.assertEqual((result['median'], result['value'], result['grade']), (100, 12, '明显爆'))
        self.assertIn('近作收藏缺失 1 条（按 0）', result['metric_notes'])
        # 排除本条、重复、超出20条窗口的缺失，不污染依据计数。
        result = viral_r.score({'id': '5', 'platform': 'douyin', 'likes': 1000}, works + works)
        self.assertEqual(result['metric_notes'], ['收藏缺失'])

    def test_old_douyin_cache_cannot_mix_metric_but_red_cache_reuses(self):
        entry = {'fetched_at': time.time(), 'works': [], 'note': '旧缓存'}
        self.path.write_text(json.dumps({'douyin:u': entry, 'xiaohongshu:u': entry}))
        data = viral_r._cache()
        self.assertFalse(viral_r._fresh(data['douyin:u']))
        self.assertTrue(viral_r._fresh(data['xiaohongshu:u']))
        self.assertTrue(viral_r._fresh({**entry, 'platform': 'douyin', 'metric_version': 2}))
        self.assertEqual(json.loads(self.path.read_text())['douyin:u'], entry)

    def test_frontend_backend_presets_match_all_values(self):
        run = subprocess.run(['node', '-e', "const f=require('./web/viral-filter.js');console.log(JSON.stringify(Object.fromEntries(['douyin','xiaohongshu'].map(p=>[p,Array.from({length:5},(_,i)=>f.keys.map(k=>f.preset(p,i)[k]))]))));"], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(run.stdout), {p: [list(x) for x in values] for p, values in viral.PRESETS.items()})
        self.assertEqual(viral.DEFAULTS['douyin']['min_interactions'], viral.preset('douyin', 3)['min_interactions'])


if __name__ == '__main__':
    unittest.main()
