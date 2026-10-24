#!/usr/bin/env python3
"""小红书视频 CDN 协议升级。无 Key、无网络。"""

from __future__ import annotations

import unittest

from server.adapters.xiaohongshu import _video_candidates, normalize_xhs_video_url


def _note(streams: dict) -> dict:
    return {"video_info_v2": {"media": {"stream": streams}}}


class NormalizeXhsVideoUrlTests(unittest.TestCase):
    def test_upgrades_both_cdns_and_subdomains(self) -> None:
        cases = {
            "http://rednotecdn.com/a.mp4": "https://rednotecdn.com/a.mp4",
            "http://xhscdn.com/a.mp4": "https://xhscdn.com/a.mp4",
            "http://sns-video-bd.xhscdn.com/stream/1.mp4": "https://sns-video-bd.xhscdn.com/stream/1.mp4",
            "http://a.b.rednotecdn.com/v.mp4": "https://a.b.rednotecdn.com/v.mp4",
            "http://cdn.xhscdn.com./a.mp4?sig=1": "https://cdn.xhscdn.com./a.mp4?sig=1",
            "HTTP://SNS-VIDEO-BD.XHSCDN.COM/A.mp4": "https://SNS-VIDEO-BD.XHSCDN.COM/A.mp4",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_xhs_video_url(raw), expected)

    def test_strips_explicit_port_80_only(self) -> None:
        raw = "http://sns-video-bd.xhscdn.com:80/stream/1.mp4?sign=abc%2Fdef&a=1&a=2"
        expected = "https://sns-video-bd.xhscdn.com/stream/1.mp4?sign=abc%2Fdef&a=1&a=2"
        self.assertEqual(normalize_xhs_video_url(raw), expected)
        padded = "http://XHSCDN.COM:0080/%2Fstream/a.mp4?sign=%2F"
        self.assertEqual(
            normalize_xhs_video_url(padded),
            "https://XHSCDN.COM/%2Fstream/a.mp4?sign=%2F",
        )

    def test_preserves_path_query_and_signature_literally(self) -> None:
        raw = (
            "http://sns-video-bd.xhscdn.com/stream/1/110/01%2Fabc.mp4"
            "?sign=abc%2Fdef&a=1&a=2&b=%20&c=+&d=%#t=1"
        )
        upgraded = normalize_xhs_video_url(raw)
        self.assertTrue(upgraded.startswith("https://"))
        self.assertEqual(upgraded.split("://", 1)[1], raw.split("://", 1)[1])
        self.assertIn("sign=abc%2Fdef", upgraded)
        self.assertEqual(upgraded.count("a="), 2)
        self.assertIn("%2Fabc.mp4", upgraded)

    def test_leaves_https_other_hosts_ports_and_userinfo_unchanged(self) -> None:
        unchanged = [
            "https://sns-video-bd.xhscdn.com/a.mp4?sign=abc%2Fdef&a=1&a=2",
            "https://SNS-VIDEO-BD.XHSCDN.COM:80/A.mp4?SIG=AbC%2F",
            "http://xhscdn.com.example.org/a.mp4?x=1",
            "http://notxhscdn.com/a.mp4",
            "http://fakexhscdn.com/a.mp4",
            "http://rednotecdn.com.attacker.test/a.mp4",
            "http://evil.com/xhscdn.com/a.mp4",
            "http://example.com/a.mp4",
            "http://sns-video-bd.xhscdn.com:443/a.mp4",
            "http://sns-video-bd.xhscdn.com:8080/a.mp4?sign=1",
            "http://xhscdn.com:abc/x",
            "http://xhscdn.com:/a.mp4",
            "http://user:pass@xhscdn.com/a.mp4?x=1",
            "http://user@rednotecdn.com/a.mp4",
            "",
        ]
        for raw in unchanged:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_xhs_video_url(raw), raw)

    def test_non_string_is_returned_as_is(self) -> None:
        self.assertIsNone(normalize_xhs_video_url(None))  # type: ignore[arg-type]


class VideoCandidateTests(unittest.TestCase):
    def test_master_and_backup_collapse_to_one_https_url(self) -> None:
        master = "http://sns-video-bd.xhscdn.com/stream/clip.mp4?sign=abc%2Fdef&a=1&a=2"
        https_same = "https://sns-video-bd.xhscdn.com/stream/clip.mp4?sign=abc%2Fdef&a=1&a=2"
        other = "http://sns-video-hw.rednotecdn.com/stream/clip.mp4?sign=abc%2Fdef&a=1&a=2"
        note = _note({
            "h264": [{
                "master_url": master,
                "default_stream": 1,
                "width": 1080,
                "height": 1920,
                "backup_urls": [
                    https_same,
                    master,
                    other,
                    "http://cdn.example.com/keep.mp4?a=1",
                ],
            }],
        })
        urls = _video_candidates(note)
        self.assertEqual(urls, [
            https_same,
            "https://sns-video-hw.rednotecdn.com/stream/clip.mp4?sign=abc%2Fdef&a=1&a=2",
            "http://cdn.example.com/keep.mp4?a=1",
        ])
        self.assertNotIn(master, urls)
        self.assertEqual(urls[0].split("://", 1)[1], master.split("://", 1)[1])

    def test_h264_stays_ahead_of_later_codecs(self) -> None:
        note = _note({
            "h265": [{
                "master_url": "http://v.xhscdn.com/h265.mp4",
                "default_stream": 1,
                "width": 2160,
                "height": 3840,
                "weight": 100,
                "backup_urls": [],
            }],
            "av1": [{
                "master_url": "http://v.rednotecdn.com/av1.mp4",
                "default_stream": 1,
                "width": 1920,
                "height": 1920,
                "backup_urls": [],
            }],
            "h264": [
                {
                    "master_url": "http://v.xhscdn.com/h264-low.mp4?x=1",
                    "default_stream": 0,
                    "width": 720,
                    "height": 1280,
                    "backup_urls": ["http://backup.rednotecdn.com/h264-low.mp4?x=1"],
                },
                {
                    "master_url": "http://v.xhscdn.com/h264-high.mp4",
                    "default_stream": 1,
                    "width": 1080,
                    "height": 1920,
                    "backup_urls": [
                        "https://v.xhscdn.com/h264-high.mp4",
                        "http://other.example.com/keep-http.mp4",
                    ],
                },
            ],
        })
        self.assertEqual(_video_candidates(note), [
            "https://v.xhscdn.com/h264-high.mp4",
            "http://other.example.com/keep-http.mp4",
            "https://v.xhscdn.com/h264-low.mp4?x=1",
            "https://backup.rednotecdn.com/h264-low.mp4?x=1",
            "https://v.xhscdn.com/h265.mp4",
            "https://v.rednotecdn.com/av1.mp4",
        ])

    def test_no_video_stream_returns_empty(self) -> None:
        self.assertEqual(_video_candidates({}), [])
        self.assertEqual(_video_candidates({"video_info_v2": {"media": {"stream": []}}}), [])


if __name__ == "__main__":
    unittest.main()
