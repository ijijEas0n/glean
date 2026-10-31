"""候选审阅的本地字段与持久化回归；不读取凭证、不请求平台。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from server import main, viral


class PrecisionTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix='.test-precision-'))
        self.path = Path(folder) / 'data' / 'viral-feedback.json'
        self.enterContext(patch.object(viral, 'FEEDBACK_PATH', self.path))
        self.get = self.enterContext(patch.object(viral.tikhub, 'get', side_effect=AssertionError('禁止 TikHub')))
        self.post = self.enterContext(patch.object(viral.tikhub, 'post', side_effect=AssertionError('禁止 TikHub')))

    def tearDown(self):
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_normalize_preserves_description_topics_and_existing_commerce(self):
        row = viral.normalize('douyin', {'aweme_id': '123', 'title': '标题', 'desc': '下载去同款App',
            'text_extra': [{'hashtag_name': '优惠'}], 'cha_list': [{'cha_name': '合作'}],
            'promotion': {'product_id': '123'}, 'is_ads': False, 'goods_info': {}, 'ad_info': []})
        self.assertEqual(row['title'], '标题')
        self.assertIn('下载去同款App', row['description'])
        self.assertIn('优惠', row['topics'])
        self.assertIn('合作', row['topics'])
        self.assertEqual(row['commerce_fields'], ['promotion'])
        red = viral.normalize('xiaohongshu', {'note_card': {'note_id': 'a'*24, 'title': '说明',
            'description': '正文', 'tag_list': [{'name': '同款'}], 'is_ad': True}})
        self.assertEqual(red['description'].strip(), '正文')
        self.assertIn('同款', red['topics'])
        self.assertEqual(red['commerce_fields'], ['is_ad'])
        plain = viral.local_fields({'is_ads': '0', 'is_ad': 'false', 'goods_info': {}, 'is_commerce': 0})
        self.assertEqual(plain['commerce_fields'], [])

    def test_feedback_persists_uses_platform_and_supports_undo(self):
        self.assertEqual(viral.load_feedback(), {'items': {}, 'authors': {}})
        for platform, mark in [('douyin', '不相关'), ('xiaohongshu', '值得跟')]:
            viral.save_feedback({'platform': platform, 'id': '123', 'mark': mark})
        viral.save_feedback({'platform': 'douyin', 'id': 'u', 'kind': 'author', 'mark': '屏蔽'})
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['items'], {'douyin:123': '不相关', 'xiaohongshu:123': '值得跟'})
        self.assertEqual(saved['authors'], {'douyin:u': '屏蔽'})
        self.assertEqual(viral.load_feedback(), saved)
        viral.save_feedback({'platform': 'douyin', 'id': '123', 'mark': ''})
        final = viral.save_feedback({'platform': 'douyin', 'id': 'u', 'kind': 'author', 'mark': ''})
        self.assertNotIn('douyin:123', final['items'])
        self.assertEqual(final['authors'], {})

    def test_concurrent_updates_do_not_lose_marks(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda i: viral.save_feedback({'platform': 'douyin', 'id': str(i), 'mark': '值得跟'}), range(20)))
        self.assertEqual(len(viral.load_feedback()['items']), 20)
        self.assertFalse(self.path.with_suffix('.tmp').exists())

    def test_invalid_feedback_and_corrupt_file_never_overwrite(self):
        for change in ({'platform': 'other'}, {'id': ''}, {'id': None}, {'kind': 'other'},
                       {'mark': 'other'}, {'kind': 'author', 'mark': '值得跟'}):
            with self.assertRaises(ValueError):
                viral.save_feedback({'platform': 'douyin', 'id': '123', 'mark': '不相关', **change})
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir()
        for value in ('broken', '[]', '{"items": [], "authors": {}}'):
            self.path.write_text(value)
            with self.assertRaises(ValueError):
                viral.save_feedback({'platform': 'douyin', 'id': '123', 'mark': '不相关'})
            self.assertEqual(self.path.read_text(), value)

    @unittest.skipUnless(shutil.which('node'), '需要 node 验证本地规则')
    def test_frontend_precision_rules(self):
        result = subprocess.run(['node', 'tests/viral_precision.cjs'], cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_feedback_routes_only_access_local_store(self):
        handler = main.Handler.__new__(main.Handler)
        handler.path = '/api/viral/feedback'
        handler._read_json = lambda: {'platform': 'douyin', 'id': '123', 'mark': '值得跟'}
        handler._json, handler._error = Mock(), Mock()
        handler.do_POST()
        handler._error.assert_not_called()
        self.assertEqual(handler._json.call_args.args[0]['feedback']['items']['douyin:123'], '值得跟')
        handler._json.reset_mock()
        handler.do_GET()
        self.assertEqual(handler._json.call_args.args[0]['feedback']['items']['douyin:123'], '值得跟')
        handler._read_json = lambda: {'platform': 'douyin', 'id': '123', 'mark': '未知标记'}
        handler.do_POST()
        handler._error.assert_called_once()


if __name__ == '__main__':
    unittest.main()
