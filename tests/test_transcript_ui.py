"""用 Node 执行真实 UI 函数，验证提示文案和原因展示；无需浏览器/网络。"""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'UI 函数检查需要 Node；后端测试只需 Python 标准库')
class TranscriptUiTests(unittest.TestCase):
    def test_partial_toast_and_detail_reason(self):
        source = (Path(__file__).resolve().parents[1] / 'web' / 'app.js').read_text()
        monitor = source[source.index('function monitorJob('):source.index('async function refreshJobs(')]
        detail = source[source.index('function renderDetail('):source.index('function formatTime(')]
        script = r'''
const assert = require('node:assert/strict');
let currentJob, notice;
const watchJob = (id, callback) => callback(currentJob);
const toast = (message, warning) => { notice = {message, warning}; };
const state = {};
const elements = {};
function node(tag, cls, text = '') {
  return {textContent: text, children: [], append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; }, addEventListener() {}};
}
const $ = id => elements[id] ||= node('div');
const titleOf = item => item.title || '';
const authorName = () => '';
const platformLabel = () => '';
function allText(element) { return element.textContent + element.children.map(allText).join(''); }
'''
        script += monitor + detail + r'''
for (const reason of ['未配置火山引擎转写 Key', '转写引擎已关闭（off）', '这条视频没有说话']) {
  currentJob = {state: 'partial', warnings: [reason]};
  monitorJob('test', 'batch');
  assert.equal(notice.warning, true);
  assert.ok(notice.message.includes(reason));
  assert.ok(notice.message.includes('已入库'));
  renderDetail({kind: 'video', transcript: {note: reason, segments: [{text: ' '}]}, warnings: [reason]});
  assert.ok(allText($('detailTranscript')).includes(reason));
  assert.ok($('detailCompleteness').textContent.includes(reason));
  assert.equal($('detailCompleteness').hidden, false);
}
currentJob = {state: 'done', warnings: []};
monitorJob('test', 'single');
assert.equal(notice.message, '已入库');
assert.ok(!notice.warning);
renderDetail({kind: 'video', transcript: {text: '说话内容'}, warnings: []});
assert.ok(allText($('detailTranscript')).includes('说话内容'));
assert.equal($('detailCompleteness').hidden, true);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
