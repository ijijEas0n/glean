"""更新检查全程禁网；直接运行 HTTP handler，不启动或连接任何服务。"""
import io
import json
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import Mock, patch

from server import main, update_check as updates


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.socket', side_effect=AssertionError('测试禁止网络')))
        self.urlopen = self.enterContext(patch.object(updates.urllib.request, 'urlopen'))
        self.enterContext(patch.object(updates, '_cache', {}))
        self.enterContext(patch.object(updates, '_cache_until', 0.0))

    @staticmethod
    def response(payload):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        return response

    def release(self, tag='v1.1.0'):
        return self.response({'tag_name': tag, 'body': '更新说明' * 1000,
                              'html_url': updates.REPO_URL + '/releases/tag/' + tag})

    @staticmethod
    def missing():
        return urllib.error.HTTPError(updates.API_URL, 404, 'Not Found', {}, None)

    def test_new_release(self):
        self.urlopen.return_value = self.release()
        result = updates.check_update('1.0.0')
        self.assertTrue(result['ok'])
        self.assertTrue(result['update_available'])
        self.assertEqual(result['latest'], 'v1.1.0')
        self.assertEqual(result['release_url'], updates.REPO_URL + '/releases/tag/v1.1.0')
        self.assertEqual(result['html_url'], result['release_url'])
        self.assertEqual(result['source'], 'release')
        self.assertEqual(len(result['notes']), 2000)
        self.assertIsNone(result['error'])
        self.assertIn('+00:00', result['checked_at'])
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, updates.API_URL + '/releases/latest')
        self.assertTrue(request.get_header('User-agent').startswith('glean-update-check/'))
        self.assertLessEqual(self.urlopen.call_args.kwargs['timeout'], 4)

    def test_latest_and_local_ahead(self):
        self.urlopen.return_value = self.release('v1.0.0')
        for current in ('1.0', '1.0.0', '2.0.0'):
            with self.subTest(current=current):
                self.assertFalse(updates.check_update(current)['update_available'])

    def test_release_404_falls_back_to_tag(self):
        self.urlopen.side_effect = [self.missing(), self.response([{'name': 'V1.2.0'}])]
        result = updates.check_update('1.0')
        self.assertTrue(result['update_available'])
        self.assertEqual(result['source'], 'tag')
        self.assertEqual(result['latest'], 'V1.2.0')
        self.assertEqual(result['release_url'], updates.REPO_URL + '/releases/tag/V1.2.0')
        self.assertEqual(self.urlopen.call_args.args[0].full_url, updates.API_URL + '/tags?per_page=1')

    def test_tag_failure_falls_back_to_raw(self):
        self.urlopen.side_effect = [self.missing(), urllib.error.URLError('offline'), self.response('1.3.0\n')]
        result = updates.check_update('1.0')
        self.assertTrue(result['update_available'])
        self.assertEqual(result['latest'], '1.3.0')
        self.assertEqual(result['source'], 'raw_version')
        self.assertEqual(result['release_url'], updates.REPO_URL)
        self.assertEqual(self.urlopen.call_args.args[0].full_url, updates.RAW_URL)

    def test_empty_or_invalid_release_and_tag_fall_back(self):
        for release in ({}, {'tag_name': 'nightly'}, 'invalid json'):
            with self.subTest(release=release):
                self.urlopen.side_effect = [self.response(release), self.response([]), self.response('1.1')]
                self.assertEqual(updates.check_update('1.0', refresh=True)['source'], 'raw_version')

    def test_network_failures_return_error(self):
        for error in (TimeoutError(), urllib.error.URLError('offline')):
            with self.subTest(error=type(error).__name__):
                self.urlopen.side_effect = error
                result = updates.check_update('1.0', refresh=True)
                self.assertFalse(result['ok'])
                self.assertFalse(result['update_available'])
                self.assertIsNone(result['latest'])
                self.assertIn('暂时查不到更新', result['error'])
                self.assertIn('超时', result['error'])

    def test_network_failure_route_returns_http_json(self):
        self.urlopen.side_effect = TimeoutError()
        handler = main.Handler.__new__(main.Handler)
        handler.path = '/api/update?refresh=1'
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'GET /api/update?refresh=1 HTTP/1.1'
        handler.command = 'GET'
        handler.wfile = io.BytesIO()
        handler.do_GET()
        headers, body = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        self.assertIn(b'200 OK', headers)
        self.assertIn(b'application/json', headers)
        result = json.loads(body)
        self.assertFalse(result['update_available'])
        self.assertTrue(result['error'])
        self.assertEqual(result['current'], main.VERSION)

    def test_numeric_comparison(self):
        for a, b in (('1.0', '1.0.0'), ('v1.0', 'V1.0.0')):
            self.assertEqual(updates.version_parts(a), updates.version_parts(b))
        self.assertGreater(updates.version_parts('v1.10'), updates.version_parts('1.9.9'))
        with self.assertRaises(ValueError):
            updates.version_parts('nightly')

    def test_cache_and_refresh(self):
        self.urlopen.return_value = self.release()
        first = updates.check_update('1.0')
        cached = updates.check_update('1.0')
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(cached['source'], 'cache')
        self.assertEqual(cached['checked_at'], first['checked_at'])
        cached['latest'] = 'do not mutate cache'
        self.assertEqual(updates.check_update('1.0')['latest'], 'v1.1.0')
        updates.check_update('1.0', refresh=True)
        self.assertEqual(self.urlopen.call_count, 2)
        with patch.object(updates, '_cache_until', 0):
            updates.check_update('1.0')
        self.assertEqual(self.urlopen.call_count, 3)

    def test_total_deadline_stops_fallback_requests(self):
        self.urlopen.side_effect = TimeoutError()
        with patch.object(updates.time, 'monotonic', side_effect=[0, 0, 5, 5, 5]):
            result = updates.check_update('1.0', refresh=True)
        self.assertFalse(result['ok'])
        self.assertEqual(self.urlopen.call_count, 1)

    def test_single_version_source_and_fallback(self):
        self.assertEqual(main.VERSION, (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip())
        self.assertEqual(main.Handler.server_version, f'Glean/{main.VERSION}')
        with patch.object(Path, 'read_text', side_effect=OSError()):
            self.assertEqual(updates.read_version(), '0.0.0')
        with patch.object(Path, 'read_text', return_value='invalid'):
            self.assertEqual(updates.read_version(), '0.0.0')


if __name__ == '__main__':
    unittest.main()
