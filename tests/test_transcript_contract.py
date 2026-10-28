"""转写结果契约：隔离库/配置，禁网，不读取真实凭证。"""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import config, fetcher, media, store
from server.adapters import douyin, xiaohongshu
from server.jobs import Job, JobManager
from server.resolver import resolve


class TranscriptContractTests(unittest.TestCase):
    def setUp(self):
        # 文件也只写仓库内，两个目录都与真实用户数据隔离。
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix='.test-transcript-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.enterContext(patch.dict('os.environ', {
            'GLEAN_LIBRARY_DIR': str(root / 'library'),
            'GLEAN_CONFIG_DIR': str(root / 'config'),
        }, clear=True))
        for name, value in {
            'LIBRARY_ROOT': root / 'library',
            'ITEMS_DIR': root / 'library' / 'items',
            'INDEX_JSON': root / 'library' / 'index.json',
            'INDEX_MD': root / 'library' / 'index.md',
            '_list_cache': {'at': 0.0, 'rows': []},
        }.items():
            self.enterContext(patch.object(store, name, value))
        self.enterContext(patch.object(config, 'LIBRARY_ROOT', root / 'library'))
        self.enterContext(patch.object(config, 'CONFIG_DIR', root / 'config'))
        self.enterContext(patch.object(config, 'CONFIG_PATH', root / 'config' / 'config.json'))
        self.enterContext(patch('socket.socket', side_effect=AssertionError('测试禁止网络')))
        self.api = self.enterContext(patch('server.tikhub.get', side_effect=AssertionError('未 mock TikHub')))
        self.enterContext(patch('server.tikhub.download', side_effect=self.download))
        self.asr = self.enterContext(patch('server.media.transcribe_video', return_value={
            'source': 'none', 'text': '', 'segments': [], 'note': '未配置火山转写 Key'}))
        self.enterContext(patch('server.media.probe_duration', return_value=1))
        self.enterContext(patch('server.media.has_video_stream', return_value=True))
        self.enterContext(patch('server.media.probe_video_codec', return_value='h264'))
        self.enterContext(patch.object(douyin, 'fetch_play_counts', return_value={}))
        self.enterContext(patch.object(douyin, '补播放量'))
        self.manager = JobManager()
        self.addCleanup(self.manager._pool.shutdown, wait=True)

    @staticmethod
    def download(url, target, **kwargs):
        target.write_bytes(b'fake-video')
        return 10

    def run_video(self, platform='douyin', options=None):
        if platform == 'douyin':
            url = 'https://www.douyin.com/video/7401819680706448675'
            payload = {'data': {'aweme_detail': {'aweme_id': '7401819680706448675', 'video': {
                'play_addr': {'url_list': ['https://example.invalid/video.mp4']}}}}}
            target = douyin
        else:
            note_id = '6aad453b00000000110339cb'
            url = f'https://www.xiaohongshu.com/explore/{note_id}'
            payload = {'data': {'data': [{'id': note_id, 'video_info_v2': {'media': {'stream': {
                'h264': [{'master_url': 'https://example.invalid/video.mp4'}]}}}}]}}
            target = xiaohongshu
        with patch.object(target, 'get', return_value=payload):
            job = Job(url, {'transcribe': True, **(options or {})}, resolve(url))
            self.manager._run(job)
        self.assertNotEqual(job.state, 'failed', job.error)
        item = store.load_item(job.item_id)
        content = (config.LIBRARY_ROOT / 'items' / job.item_id / 'content.md').read_text()
        return job, item, content

    def test_empty_results_always_warn_on_both_platforms(self):
        for platform in ('douyin', 'xiaohongshu'):
            for note in ('未配置火山转写 Key', '音频提取失败', media.NO_SPEECH_NOTE, ''):
                with self.subTest(platform=platform, note=note):
                    self.asr.return_value = {'source': 'none', 'text': '  ', 'segments': [], 'note': note}
                    job, item, content = self.run_video(platform)
                    self.assertEqual(job.state, 'partial')
                    self.assertTrue(job.warnings)
                    self.assertTrue(item['transcript']['note'])
                    if note:
                        self.assertIn(note, job.warnings)
                    self.assertNotIn('## 字幕原文', content)
                    self.assertIn(item['transcript']['note'], content)
        self.assertTrue(self.asr.called)

    def test_off_reason(self):
        # mock 外呼及音频步骤，直接走真实 off 分支，不能探测音轨或配置。
        self.asr.side_effect = media_transcribe_video
        with patch('server.media.has_audio_stream', side_effect=AssertionError('off 不应读取音轨')):
            for platform in ('douyin', 'xiaohongshu'):
                job, item, _ = self.run_video(platform, {'transcribe_engine': 'off'})
                self.assertEqual(job.state, 'partial')
                self.assertIn('off', item['transcript']['note'])

    def test_success_has_subtitle_body(self):
        self.asr.return_value = {'source': 'local', 'text': '测试说话', 'segments': [{'start': 0, 'end': 1, 'text': '测试说话'}]}
        for platform in ('douyin', 'xiaohongshu'):
            job, _, content = self.run_video(platform)
            self.assertEqual(job.state, 'done')
            self.assertEqual(job.warnings, [])
            self.assertIn('## 字幕原文\n\n[00:00] 测试说话', content)

    def test_blank_segments_are_not_subtitles(self):
        self.asr.return_value = {'source': 'local', 'text': ' ', 'segments': [{'text': ' '}]}
        job, _, content = self.run_video()
        self.assertEqual(job.state, 'partial')
        self.assertNotIn('## 字幕原文', content)

    def test_real_missing_key_path_is_partial(self):
        self.asr.side_effect = media_transcribe_video
        with patch('server.media.volcengine_api_key', return_value=''), \
             patch('server.media.has_audio_stream', return_value=True), \
             patch('server.media.extract_audio', return_value=Path(self.tmp.name) / 'fake.wav'), \
             patch('server.media.load_config', return_value={'transcribe_fallback_local': False}):
            job, item, _ = self.run_video()
        self.assertEqual(job.state, 'partial')
        self.assertIn('未配置', item['transcript']['note'])

    def test_explicitly_disabled_is_not_transcription_failure(self):
        for platform in ('douyin', 'xiaohongshu'):
            job, item, content = self.run_video(platform, {'transcribe': False})
            self.assertEqual(job.state, 'done')
            self.assertEqual(item['transcript']['note'], '已关闭转写')
            self.assertNotIn('## 字幕原文', content)
        self.asr.assert_not_called()

    def child_adapter(self, resolved, ctx):
        ctx.begin('douyin-video-child')
        return {'id': ctx.item_id, 'kind': 'video', 'platform': 'douyin',
                'url': resolved.url, 'transcript': copy.deepcopy(self.asr.return_value)}

    def test_batch_propagates_child_reasons_and_flags(self):
        entries = [{'url': 'https://www.douyin.com/video/123', 'transcribe': True}]
        with patch.object(fetcher, 'get_adapter', return_value=self.child_adapter):
            job = Job('', {}, mode='batch', items=entries)
            self.manager._run(job)
        self.assertEqual(job.state, 'partial', job.error)
        self.assertIn('未配置', job.warnings[0])
        child = job.to_dict()['partial']['children'][0]
        self.assertEqual(child['batch_state'], 'partial')
        self.assertFalse(child['has_transcript'])
        self.assertEqual(child['item_id'], 'douyin-video-child')

    def test_expanded_profile_propagates_and_persists_reasons(self):
        child = {'id': 'douyin-video-child', 'url': 'https://www.douyin.com/video/123'}
        def profile_adapter(resolved, ctx):
            ctx.begin('douyin-profile-test')
            return {'id': ctx.item_id, 'kind': 'profile', 'platform': 'douyin', 'children': [child]}
        with patch.object(fetcher, 'get_adapter', side_effect=[profile_adapter, self.child_adapter]):
            url = 'https://www.douyin.com/user/test'
            job = Job(url, {'expand_children': True, 'transcribe_children': True}, resolve(url))
            self.manager._run(job)
        self.assertEqual(job.state, 'partial', job.error)
        self.assertIn('未配置', job.warnings[0])
        saved = store.load_item(job.item_id)
        self.assertEqual(saved['warnings'], job.warnings)
        self.assertFalse(saved['children'][0]['has_transcript'])
        self.assertEqual(saved['children'][0]['item_id'], 'douyin-video-child')


media_transcribe_video = media.transcribe_video

if __name__ == '__main__':
    unittest.main()
