"""Focused regression tests for reported user-path failures; all IO is isolated."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import fetcher, media, store
from server.adapters import base, twitter
from server.resolver import resolve
from server.tikhub import TikHubError


class UserPathFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1], prefix=".test-userpath-"
        )
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "library"
        for name, value in {
            "LIBRARY_ROOT": root,
            "ITEMS_DIR": root / "items",
            "INDEX_JSON": root / "index.json",
            "INDEX_MD": root / "index.md",
            "_list_cache": {"at": 0.0, "rows": []},
        }.items():
            self.enterContext(patch.object(store, name, value))

    def test_image_download_failure_emits_warning(self):
        warnings = []
        with patch.object(media, "download_url", side_effect=OSError("mock 403")):
            result = media.save_remote_media(
                "test-item", "https://cdn.invalid/photo.jpg", "001", on_warn=warnings.append
            )
        self.assertFalse(result["path"])
        self.assertIn("mock 403", result["error"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("图片未下载", warnings[0])

    def test_empty_transcript_is_warned_for_video_track_on_mixed_kind(self):
        ctx = base.FetchContext(options={"transcribe": True, "transcribe_engine": "volcengine"})
        item = {
            "id": "mixed-post", "kind": "images", "media": [{"type": "video", "path": "video.mp4"}],
            "transcript": {"source": "volcengine", "text": "", "segments": []},
        }
        finalized = fetcher._finalize(item, ctx)
        self.assertEqual(finalized["transcript"]["note"], "已请求转写，但未生成文字稿（转写未返回有效文字）")
        self.assertTrue(finalized["warnings"])

    def test_video_without_download_is_marked_as_skipped(self):
        ctx = base.FetchContext(options={"download_media": False, "transcribe": False})
        item = {"id": "no-download", "kind": "video", "media": [
            {"type": "video", "path": "", "url": "https://cdn.invalid/video.mp4"}
        ]}
        finalized = fetcher._finalize(item, ctx)
        self.assertTrue(finalized["media"][0]["download_skipped"])

    def test_twitter_mixed_media_without_caption_is_kept_as_video(self):
        payload = {"data": {
            "text": "", "display_text": "", "author": {"screen_name": "mock"},
            "media": {
                "photos": [{"url": "https://cdn.invalid/photo.jpg"}],
                "videos": [{"variants": [{"url": "https://cdn.invalid/video.mp4", "content_type": "video/mp4"}]}],
            },
        }}
        ctx = base.FetchContext(options={"download_media": False, "transcribe": True})
        url = "https://x.com/mock/status/1234567890123456789"
        with patch.object(twitter, "get", return_value=payload), patch.object(
            twitter, "_thread_text", side_effect=lambda tweet, handle, prefix, text: (text, [])
        ):
            item = twitter.fetch_post(resolve(url), ctx)
        self.assertEqual(item["kind"], "video")
        self.assertEqual(item["text"], "")
        self.assertTrue(any(row["type"] == "video" for row in item["media"]))

    def test_batch_all_failures_explain_zero_saved(self):
        result = fetcher.run_batch([{"url": "not a link"}], options={})
        self.assertEqual(result["children_ok"], 0)
        self.assertEqual(result["children_failed"], 1)
        self.assertIn("0 条入库，1 条失败", result["warnings"][0])

    def test_expanded_child_retains_original_failure(self):
        child = {"id": "deleted", "url": "https://www.douyin.com/video/123"}
        with patch.object(fetcher, "get_adapter", side_effect=TikHubError("mock deleted child")):
            result = fetcher.fetch_one_child(child, {}, None)
        self.assertIsNone(result)
        self.assertEqual(child["batch_state"], "failed")
        self.assertIn("mock deleted child", child["error"])


if __name__ == "__main__":
    unittest.main()
