import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location('ocr_webhook', Path(__file__).parents[1] / 'scripts' / 'ocr_webhook.py')
webhook = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = webhook
spec.loader.exec_module(webhook)

REPO = 'owner/ocr-review-smoke'
OTHER_REPO = 'owner/another-repo'
SECRET = b'a' * 64
OTHER_SECRET = b'b' * 64
BASE = 'a' * 40
HEAD = 'b' * 40
GITEA_URL = 'https://reviews.example.com:8443/gitea'
SSH_BASE = 'ssh://git@git.example.com:2222/prefix'


def review_data(state='complete', comments=None, head=HEAD, base=BASE):
    completed = [{'item_id': 'reviewed', 'path': 'average.py'}] if state in ('complete', 'partial') else []
    failed = [{'item_id': 'failed', 'path': 'other.py', 'classification': 'provider'}] if state in ('failed', 'partial') else []
    return {'status': state, 'comments': comments, 'manifest': {
        'schema_version': 'ocr.run-manifest/v1', 'operation': 'review',
        'terminal_state': state, 'input': {'mode': 'range', 'requested_from': base,
        'requested_head': head, 'resolved_base': base, 'resolved_head': head, 'exact_range': f'{base}..{head}'},
        'coverage': {'selected': completed + failed, 'completed': completed, 'reused': [],
                     'failed': failed, 'waived': [], 'excluded': []}}}


def finding(**changes):
    item = {'path': 'average.py', 'start_line': 3, 'end_line': 3,
            'content': '큰 입력의 합계가 먼저 오버플로해 inf를 반환합니다.', 'category': 'bug',
            'severity': 'medium', 'existing_code': 'return sum(values) / len(values)',
            'suggestion_code': 'return statistics.mean(values)'}
    item.update(changes)
    return item


def payload():
    return {'action': 'opened', 'repository': {'full_name': REPO}, 'pull_request': {
        'number': 7, 'base': {'repo': {'full_name': REPO}, 'sha': BASE, 'ref': 'main'},
        'head': {'repo': {'full_name': REPO}, 'sha': HEAD}}}


class FakeAPI:
    def __init__(self):
        self.comment = None
        self.events = []
        self.states = [True, True]
        self.markers = set()
        self.pending = False

    def current(self, job):
        self.events.append('current')
        return self.states.pop(0) if self.states else True

    def find_comment(self, job, login):
        self.events.append('find')
        return self.comment

    def review_reaction(self, job, method):
        self.events.append(method)

    def save_comment(self, job, comment, body):
        self.events.append(('save', comment and comment['id'], body))
        self.comment = {'id': 42, 'body': body, 'user': {'login': 'bot'}}
        return self.comment

    def review_markers(self, job, login):
        return self.markers, self.pending

    def create_review(self, job, comments):
        self.events.append(('review', job.head, comments))
        for comment in comments:
            self.markers.update(webhook.re.findall(r'<!-- ocr-inline ([0-9a-f]{64}) -->', comment['body']))


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.service = webhook.Service(SECRET, 'token', 'bot', '/usr/bin/ocr', GITEA_URL, SSH_BASE)
        self.service.api = FakeAPI()
        self.job = webhook.parse_job(payload())

    def test_progress_then_result_reuses_comment_and_duplicate_is_skipped(self):
        def review(job, binary, gitea_url, ssh_base):
            self.assertEqual(self.service.api.events[-1], 'POST')
            self.assertIsNone(self.service.api.comment)
            self.assertEqual((gitea_url, ssh_base), (GITEA_URL, SSH_BASE))
            return webhook.ReviewResult('complete', '&lt;finding&gt; @\u200ball')
        with patch.object(webhook, 'run_review', side_effect=review) as run:
            self.service.process(self.job)
            saves = [event for event in self.service.api.events if isinstance(event, tuple)]
            self.assertEqual([event[1] for event in saves], [None])
            self.assertIn('&lt;finding&gt;', saves[0][2])
            self.assertIn('ocr-success', saves[0][2])
            self.assertEqual(self.service.api.events[-1], 'DELETE')
            self.service.process(self.job)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(self.service.api.events.count('POST'), 1)

    def test_smoke_result_updates_same_comment_without_calling_model(self):
        self.service.comment_smoke = True
        with patch.object(webhook, 'run_review') as run:
            self.service.process(self.job)
            self.service.process(self.job)
            run.assert_not_called()
        saves = [event for event in self.service.api.events if isinstance(event, tuple)]
        self.assertEqual([event[1] for event in saves], [None])
        self.assertIn('[모의 결과]', saves[0][2])
        self.assertNotIn('ocr-success', saves[0][2])

    def test_stale_before_or_after_review_is_not_published(self):
        self.service.api.states = [False]
        with patch.object(webhook, 'run_review') as run:
            self.service.process(self.job)
            run.assert_not_called()
        self.assertIsNone(self.service.api.comment)
        self.service.api.states = [True, True, False]
        with patch.object(webhook, 'run_review', return_value=webhook.ReviewResult('complete', 'result')):
            self.service.process(self.job)
        self.assertIsNone(self.service.api.comment)
        self.assertEqual(self.service.api.events[-1], 'DELETE')

    def test_failure_is_sanitized_and_retryable(self):
        with patch.object(webhook, 'run_review', side_effect=ValueError('private token')):
            self.service.process(self.job)
        self.assertIn('실패', self.service.api.comment['body'])
        self.assertNotIn('private token', self.service.api.comment['body'])
        self.assertEqual(self.service.api.events[-1], 'DELETE')
        with patch.object(webhook, 'run_review', return_value=webhook.ReviewResult('complete', 'ok')):
            self.service.process(self.job)
        self.assertIn('ocr-success', self.service.api.comment['body'])
        self.assertEqual(self.service.api.comment['id'], 42)

    def test_forks_bad_revisions_and_mismatched_repositories_are_skipped(self):
        for change in ('fork', 'sha', 'number', 'ref', 'repo'):
            data = payload()
            if change == 'fork':
                data['pull_request']['head']['repo']['full_name'] = 'other/fork'
            elif change == 'sha':
                data['pull_request']['head']['sha'] = 'bad'
            elif change == 'number':
                data['pull_request']['number'] = True
            elif change == 'ref':
                data['pull_request']['base']['ref'] = '../escape'
            else:
                data['repository']['full_name'] = 'other/repo'
            self.assertIsNone(webhook.parse_job(data), change)

    def test_only_bot_owned_marker_is_selected(self):
        api = webhook.Gitea('token', GITEA_URL)
        comments = [
            {'id': 1, 'body': webhook.MARKER, 'user': {'login': 'other'}},
            {'id': 2, 'body': 'ordinary', 'user': {'login': 'bot'}},
            {'id': 3, 'body': webhook.MARKER + '\ntext', 'user': {'login': 'bot'}},
        ]
        with patch.object(api, 'request', return_value=comments):
            self.assertEqual(api.find_comment(self.job, 'bot')['id'], 3)

    def test_review_fetches_fixed_refs_and_checks_out_base(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if 'refs/ocr/base^{commit}' in args:
                return SimpleNamespace(stdout=(BASE + '\n').encode())
            if 'refs/ocr/head^{commit}' in args:
                return SimpleNamespace(stdout=(HEAD + '\n').encode())
            if 'merge-base' in args:
                return SimpleNamespace(stdout=(BASE + '\n').encode())
            if args[0] == '/usr/bin/ocr':
                Path(args[args.index('--output') + 1]).write_text(json.dumps(review_data()))
            return SimpleNamespace(stdout=b'', returncode=0)
        with patch.object(webhook.subprocess, 'run', side_effect=run):
            self.assertEqual(webhook.run_review(self.job, '/usr/bin/ocr', GITEA_URL, SSH_BASE).status, 'complete')
        self.assertTrue(any(f'{SSH_BASE}/{REPO}.git' in call for call in calls))
        self.assertTrue(any('refs/pull/7/head:refs/ocr/head' in ' '.join(call) for call in calls))
        checkout = next(call for call in calls if 'checkout' in call)
        self.assertIn(BASE, checkout)
        self.assertNotIn(HEAD, checkout)
        ocr = calls[-1]
        self.assertNotIn('--provider', ocr)
        self.assertNotIn('--model', ocr)
        self.assertEqual(ocr[ocr.index('--from') + 1], BASE)
        self.assertEqual(ocr[ocr.index('--to') + 1], HEAD)
        self.assertEqual(ocr[ocr.index('--format') + 1], 'json')
        self.assertIn('---OCR-EN---', ocr[ocr.index('--background') + 1])

    def test_render_finding_and_partial(self):
        link = f'{GITEA_URL}/{REPO}/src/commit/{HEAD}/average.py?display=source#L3'
        result = webhook.render_review(self.job, BASE, review_data(comments=[finding()]), 0,
                                       lambda *args: (link, False))
        self.assertEqual(result.status, 'complete')
        self.assertIn(link, result.markdown)
        self.assertIn('오버플로해 inf를 반환', result.markdown)
        self.assertNotIn('statistics', result.markdown)
        self.assertIn('보통', result.markdown)
        self.assertIn('분류: 버그', result.markdown)
        partial = webhook.render_review(self.job, BASE, review_data('partial', [finding()]), 1,
                                        lambda *args: None)
        self.assertIn('일부', partial.markdown)
        with patch.object(webhook, 'run_review', return_value=partial):
            self.service.process(self.job)
        self.assertNotIn('ocr-success', self.service.api.comment['body'])

    def test_bilingual_and_malformed_findings_remain_visible(self):
        bilingual = finding(content='합계가 넘칩니다.\n---OCR-EN---\nThe sum overflows.')
        english = finding(content='The result is wrong.')
        malformed = finding(content='한국어 설명\n---OCR-EN---\n한국어 반복')
        report = webhook.render_review(self.job, BASE, review_data(comments=[bilingual, english, malformed]),
                                       0, lambda *args: None).markdown
        self.assertIn('합계가 넘칩니다', report)
        self.assertIn('<summary>English</summary>\n\n   The sum overflows', report)
        self.assertIn('한국어 설명을 제공하지 못했습니다', report)
        self.assertIn('The result is wrong', report)
        self.assertIn('한국어 반복', report)
        self.assertEqual(report.count('<summary>English</summary>'), 1)

    def test_verified_source_is_new_tab_without_source_snippet(self):
        link = f'{GITEA_URL}/{REPO}/src/commit/{HEAD}/file.py?x=1&y=2'
        report = webhook.render_review(self.job, BASE, review_data(comments=[finding()]), 0,
                                       lambda *args: (link, False)).markdown
        self.assertIn('href="' + link.replace('&', '&amp;') + '" target="_blank" rel="noopener noreferrer"', report)
        self.assertNotIn('<table>', report)
        self.assertNotIn('return sum(values)', report)

    def test_old_success_marker_is_updated_once(self):
        self.service.api.comment = {'id': 42, 'user': {'login': 'bot'}, 'body':
            f'{webhook.MARKER}\n<!-- ocr-success v=2 base={BASE} head={HEAD} -->\nold'}
        with patch.object(webhook, 'run_review', return_value=webhook.ReviewResult('complete', 'new')) as run:
            self.service.process(self.job)
            self.service.process(self.job)
        self.assertEqual(run.call_count, 1)
        self.assertIn('ocr-success v=4', self.service.api.comment['body'])
        self.assertIn(('save', 42, self.service.api.comment['body']), self.service.api.events)

    def test_invalid_failed_skipped_and_clean(self):
        for data, code in [(review_data('failed'), 1), (review_data(), 1),
                           (review_data(head=BASE), 0), (review_data(base=HEAD), 0),
                           (review_data('partial'), -9), (review_data('partial'), 0),
                           ({'status': 'complete'}, 0),
                           (review_data(comments=[finding(start_line=True)]), 0)]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                webhook.render_review(self.job, BASE, data, code, lambda *args: None)
        clean = webhook.render_review(self.job, BASE, review_data(), 0, lambda *args: None)
        self.assertIn('발견된 사항이 없습니다', clean.markdown)
        skipped = webhook.render_review(self.job, BASE, review_data('skipped'), 0, lambda *args: None)
        self.assertNotIn('발견된 사항이 없습니다', skipped.markdown)

    def test_untrusted_text_and_bounded_whole_blocks(self):
        hostile = '<img src=x> @all [link](https://evil.test) https://evil.test'
        items = [finding(content=hostile, suggestion_code=hostile, severity='low'),
                 finding(content='urgent', severity='critical')]
        items += [finding(content='x' * 2000, severity='low') for _ in range(100)]
        report = webhook.render_review(self.job, BASE, review_data(comments=items), 0,
                                        lambda *args: None).markdown
        self.assertLess(len(report), webhook.MAX_REPORT)
        self.assertLess(report.index('urgent'), report.index('&lt;img'))
        self.assertNotIn('<img', report)
        self.assertNotIn('@all', report)
        self.assertNotIn('[link](https://evil.test)', report)
        self.assertIn('생략했습니다', report)

    def test_new_head_updates_same_comment(self):
        other = webhook.Job(REPO, 7, BASE, 'c' * 40, 'main')
        with patch.object(webhook, 'run_review', return_value=webhook.ReviewResult('complete', '완료')):
            self.service.process(self.job)
            self.service.process(other)
        saves = [event for event in self.service.api.events if isinstance(event, tuple)]
        self.assertEqual([event[1] for event in saves], [None, 42])
        self.assertIn(other.head, saves[-1][2])

    def test_stale_after_comment_lookup_is_not_published(self):
        self.service.api.states = [True, True, False]
        with patch.object(webhook, 'run_review', return_value=webhook.ReviewResult('complete', 'result')):
            self.service.process(self.job)
        self.assertIsNone(self.service.api.comment)
        self.assertEqual(self.service.api.events[-3:-1], ['find', 'current'])
        self.assertEqual(self.service.api.events[-1], 'DELETE')

    def test_only_verified_added_high_findings_get_inline_comments(self):
        comments = [finding(severity='critical', content='첫 문제\n---OCR-EN---\nFirst problem.'),
                    finding(severity='high', start_line=2, end_line=2, existing_code='line 2'),
                    finding(severity='medium'), finding(severity='high', existing_code='wrong'),
                    finding(severity='high', path=':(glob)*')]
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            out = b''
            if 'refs/ocr/base^{commit}' in args or 'merge-base' in args:
                out = (BASE + '\n').encode()
            elif 'refs/ocr/head^{commit}' in args:
                out = (HEAD + '\n').encode()
            elif 'ls-tree' in args and ':(literal)average.py' in args:
                out = b'100644 blob 123\taverage.py\n'
            elif 'cat-file' in args and '-s' in args:
                out = b'100\n'
            elif 'cat-file' in args and 'blob' in args:
                out = b'line 1\nline 2\nreturn sum(values) / len(values)\n'
            elif 'diff' in args:
                out = b'@@ -2,0 +3 @@\n+return sum(values) / len(values)\n'
            elif args[0] == '/usr/bin/ocr':
                Path(args[args.index('--output') + 1]).write_text(json.dumps(review_data(comments=comments)))
            return SimpleNamespace(stdout=out, returncode=0)
        with patch.object(webhook.subprocess, 'run', side_effect=run):
            report = webhook.run_review(self.job, '/usr/bin/ocr', GITEA_URL, SSH_BASE)
        self.assertEqual(len(report.inline), 1)
        entry = report.inline[0][1]
        self.assertEqual((entry['path'], entry['new_position']), ('average.py', 3))
        self.assertIn('<summary>English</summary>', entry['body'])
        diff = next(call for call in calls if 'diff' in call)
        self.assertIn('--no-ext-diff', diff)
        self.assertIn('--no-textconv', diff)
        self.assertIn(':(literal)average.py', diff)
        self.assertNotIn('return sum(values)', report.markdown)

    def test_inline_reconciles_new_head_and_uncertain_attempt(self):
        result = webhook.render_review(self.job, BASE, review_data(comments=[finding(severity='high')]), 0,
                                       lambda *args: ('https://example.test/file', True))
        with patch.object(webhook, 'run_review', return_value=result):
            self.service.process(self.job)
            other = webhook.Job(REPO, 7, BASE, 'c' * 40, 'main')
            shifted = webhook.render_review(other, BASE, review_data(head=other.head, comments=[
                finding(severity='high', start_line=4, end_line=4)]), 0,
                lambda *args: ('https://example.test/file', True))
            with patch.object(webhook, 'run_review', return_value=shifted):
                self.service.process(other)
        self.assertEqual(len([x for x in self.service.api.events if isinstance(x, tuple) and x[0] == 'review']), 1)
        self.assertNotIn('ocr-attempt', self.service.api.comment['body'])

        api = FakeAPI()
        self.service.api = api
        def ambiguous(job, comments):
            api.events.append(('review', job.head, comments))
            raise TimeoutError()
        with patch.object(webhook, 'run_review', return_value=result), patch.object(api, 'create_review', side_effect=ambiguous):
            self.service.process(self.job)
            self.service.process(self.job)
        self.assertEqual(len([x for x in api.events if isinstance(x, tuple) and x[0] == 'review']), 1)
        self.assertIn('ocr-attempt', api.comment['body'])
        self.assertIn('인라인 게시 여부를 확인하지 못했습니다', api.comment['body'])
        self.assertIn('오버플로', api.comment['body'])

    def test_inline_limit_order_and_review_api_payload(self):
        items = [finding(severity='high', start_line=n, end_line=n, existing_code=f'line {n}')
                 for n in range(1, 5)]
        items.append(finding(severity='critical', start_line=5, end_line=5, existing_code='line 5'))
        result = webhook.render_review(self.job, BASE, review_data(comments=items), 0,
                                       lambda *args: ('https://example.test/file', True))
        self.assertEqual([entry['new_position'] for _, entry in result.inline], [5, 1, 2])
        api = webhook.Gitea('token', GITEA_URL)
        with patch.object(api, 'request') as request:
            api.create_review(self.job, [entry for _, entry in result.inline])
        method, path, data = request.call_args.args
        self.assertEqual((method, path), ('POST', f'/repos/{REPO}/pulls/7/reviews'))
        self.assertEqual((data['event'], data['commit_id']), ('COMMENT', HEAD))
        self.assertEqual(len(data['comments']), 3)

    def test_pending_draft_and_stale_write_guard_fall_back(self):
        result = webhook.render_review(self.job, BASE, review_data(comments=[finding(severity='high')]), 0,
                                       lambda *args: ('https://example.test/file', True))
        self.service.api.pending = True
        with patch.object(webhook, 'run_review', return_value=result):
            self.service.process(self.job)
        self.assertIn('인라인 게시 여부를 확인하지 못했습니다', self.service.api.comment['body'])
        self.assertFalse(any(isinstance(x, tuple) and x[0] == 'review' for x in self.service.api.events))
        self.service.api = FakeAPI()
        self.service.api.states = [True, True, True, False]
        with patch.object(webhook, 'run_review', return_value=result):
            self.service.process(self.job)
        self.assertIn('ocr-attempt', self.service.api.comment['body'])
        self.assertFalse(any(isinstance(x, tuple) and x[0] == 'review' for x in self.service.api.events))

    def test_links_only_existing_head_blob_lines(self):
        comments = [finding(), finding(path='../escape.py'), finding(start_line=9, end_line=9),
                    finding(start_line=2, end_line=2),
                    finding(path='deleted.py')]
        def run(args, **kwargs):
            stdout = b''
            if 'refs/ocr/base^{commit}' in args:
                stdout = (BASE + '\n').encode()
            elif 'refs/ocr/head^{commit}' in args:
                stdout = (HEAD + '\n').encode()
            elif 'merge-base' in args:
                stdout = (BASE + '\n').encode()
            elif 'ls-tree' in args and ':(literal)average.py' in args:
                stdout = b'100644 blob 123\taverage.py\n'
            elif 'cat-file' in args and '-s' in args:
                stdout = b'6\n'
            elif 'cat-file' in args and 'blob' in args:
                stdout = b'1\n2\n    return sum(values) / len(values)\n'
            elif args[0] == '/usr/bin/ocr':
                Path(args[args.index('--output') + 1]).write_text(json.dumps(review_data(comments=comments)))
            return SimpleNamespace(stdout=stdout, returncode=0)
        with patch.object(webhook.subprocess, 'run', side_effect=run):
            report = webhook.run_review(self.job, '/usr/bin/ocr', GITEA_URL, SSH_BASE).markdown
        self.assertIn(f'{GITEA_URL}/{REPO}/src/commit/{HEAD}/average.py?display=source#L3', report)
        self.assertNotIn('<table>', report)
        self.assertNotIn('return sum(values)', report)
        self.assertNotIn('#L9', report)
        self.assertNotIn('#L2', report)
        self.assertNotIn('/src/commit/' + HEAD + '/../', report)
        self.assertNotIn('/src/commit/' + HEAD + '/deleted.py', report)

    def test_verified_ranges_use_head_context_and_bound_long_excerpts(self):
        lines = [f'line {n}' for n in range(1, 26)]
        comments = [finding(start_line=10, end_line=10, existing_code='line 10', content='short'),
                    finding(start_line=2, end_line=6, existing_code='\n'.join(lines[1:6]), content='five'),
                    finding(start_line=12, end_line=17, existing_code='\n'.join(lines[11:17]), content='long'),
                    finding(start_line=1, end_line=21, existing_code='\n'.join(lines[:21]), content='oversize'),
                    finding(start_line=8, end_line=8, existing_code='old side', content='mismatch'),
                    finding(start_line=9, end_line=9, existing_code='', content='missing provenance')]
        def run(args, **kwargs):
            stdout = b''
            if 'refs/ocr/base^{commit}' in args or 'merge-base' in args:
                stdout = (BASE + '\n').encode()
            elif 'refs/ocr/head^{commit}' in args:
                stdout = (HEAD + '\n').encode()
            elif 'ls-tree' in args:
                stdout = b'100644 blob 123\taverage.py\n'
            elif 'cat-file' in args and '-s' in args:
                stdout = b'200\n'
            elif 'cat-file' in args and 'blob' in args:
                stdout = ('\n'.join(lines) + '\n').encode()
            elif args[0] == '/usr/bin/ocr':
                Path(args[args.index('--output') + 1]).write_text(json.dumps(review_data(comments=comments)))
            return SimpleNamespace(stdout=stdout, returncode=0)
        with patch.object(webhook.subprocess, 'run', side_effect=run):
            report = webhook.run_review(self.job, '/usr/bin/ocr', GITEA_URL, SSH_BASE).markdown
        self.assertNotIn('<table>', report)
        self.assertIn('#L1', report)
        self.assertNotIn('#L8', report)
        self.assertNotIn('#L9', report)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.service = webhook.Service(SECRET, 'token', 'bot', '/usr/bin/ocr', GITEA_URL, SSH_BASE)
        self.server = HTTPServer(('127.0.0.1', 0), webhook.Handler)
        self.server.service = self.service
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, data, signature=None, event='pull_request', path='/gitea', secret=SECRET, raw=False):
        body = data if raw else json.dumps(data).encode()
        if signature is None:
            signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'X-Gitea-Event': event}
        if signature:
            headers['X-Gitea-Signature'] = signature
        conn.request('POST', path, body, headers)
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def test_authentication_event_and_queue_responses(self):
        with patch.object(self.service, 'enqueue', return_value=True) as enqueue:
            self.assertEqual(self.post(payload(), signature='0' * 64), 401)
            self.assertEqual(self.post(payload(), signature=''), 401)
            self.assertEqual(self.post(payload(), secret=OTHER_SECRET), 401)
            self.assertEqual(self.post(payload(), event='push'), 204)
            self.assertEqual(self.post({'repository': {'full_name': REPO}}, event='push'), 204)
            self.assertEqual(self.post(payload(), path='/other'), 404)
            self.assertEqual(self.post(payload()), 202)
            enqueue.assert_called_once_with(webhook.parse_job(payload()))
        with patch.object(self.service, 'enqueue', return_value=False):
            self.assertEqual(self.post(payload()), 503)

    def test_invalid_payload_and_action_do_not_enqueue(self):
        data = payload()
        data['action'] = 'closed'
        with patch.object(self.service, 'enqueue') as enqueue:
            self.assertEqual(self.post(data), 204)
            self.assertEqual(self.post({'action': 'opened'}), 204)
            enqueue.assert_not_called()

    def test_instance_secret_accepts_multiple_same_repository_pull_requests(self):
        second = payload()
        second['repository']['full_name'] = OTHER_REPO
        second['pull_request']['base']['repo']['full_name'] = OTHER_REPO
        second['pull_request']['head']['repo']['full_name'] = OTHER_REPO
        with patch.object(self.service, 'enqueue', return_value=True) as enqueue:
            self.assertEqual(self.post(payload()), 202)
            self.assertEqual(self.post(second), 202)
            self.assertEqual(enqueue.call_count, 2)
            self.assertEqual([call.args[0].repo for call in enqueue.call_args_list], [REPO, OTHER_REPO])

    def test_signed_forks_and_repository_mismatches_are_not_queued(self):
        with patch.object(self.service, 'enqueue') as enqueue:
            for side in ('base', 'head'):
                data = payload()
                data['pull_request'][side]['repo']['full_name'] = OTHER_REPO
                self.assertEqual(self.post(data), 204)
            enqueue.assert_not_called()

    def test_invalid_json_and_recursive_json_are_rejected(self):
        with patch.object(self.service, 'enqueue') as enqueue:
            self.assertEqual(self.post(b'{', raw=True), 400)
            self.assertEqual(self.post(b'{', raw=True, secret=OTHER_SECRET), 401)
            # Decoder nesting limits differ across Python versions. A decoded
            # array is still invalid because it has no pull request.
            self.assertIn(self.post(b'[' * 2000 + b']' * 2000, raw=True), (400, 204))
            self.assertEqual(self.post({'repository': {'full_name': []}}), 204)
            enqueue.assert_not_called()

    def test_oversized_body_is_rejected_before_reading(self):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request('POST', '/gitea', headers={'Content-Length': str(webhook.MAX_BODY + 1)})
        response = conn.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        conn.close()


class ConfigurationTests(unittest.TestCase):
    def test_base_urls_and_rejected_configuration(self):
        self.assertEqual(webhook.base_url(GITEA_URL + '/'), GITEA_URL)
        self.assertEqual(webhook.base_url(SSH_BASE + '/', ssh=True), SSH_BASE)
        self.assertEqual(webhook.base_url('http://localhost:3000'), 'http://localhost:3000')
        self.assertEqual(webhook.base_url('ssh://git@[::1]:2222', ssh=True), 'ssh://git@[::1]:2222')
        for value in ('', 'file://example.com', 'https://user@example.com',
                      'https://example.com:99999', 'https://example.com:',
                      'https://example.com/a/../b', 'https://example.com/?x=1',
                      'https://example.com/#fragment', 'https://example.com\n.evil'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                webhook.base_url(value)
        for value in ('git@example.com:repo', 'ssh://example.com', 'ssh://-o@example.com',
                      'ssh://git:password@example.com', 'ssh://git@example.com:0',
                      'ssh://git@example.com:bad', 'ssh://git@example.com/-option',
                      'ssh://git@example.com?option=x', 'ssh://git@example.com/#fragment'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                webhook.base_url(value, ssh=True)

    def test_api_uses_configured_url(self):
        api = webhook.Gitea('token', GITEA_URL)
        response = MagicMock(status=204)
        response.read.return_value = b''
        response.__enter__.return_value = response
        with patch.object(api.opener, 'open', return_value=response) as request:
            api.request('GET', '/repos/owner/repo')
        self.assertEqual(request.call_args.args[0].full_url, GITEA_URL + '/api/v1/repos/owner/repo')

    def test_smoke_uses_instance_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('token')
            env = {'OCR_WEBHOOK_SECRET_FILE': 'unused', 'OCR_GITEA_TOKEN_FILE': str(token),
                   'OCR_BOT_LOGIN': 'bot', 'OCR_GITEA_URL': GITEA_URL, 'OCR_GIT_SSH_BASE': SSH_BASE}
            with patch.dict(webhook.os.environ, env, clear=True), patch.object(sys, 'argv', ['ocr', '--comment-smoke']):
                with patch.object(webhook, 'load_secret', return_value=SECRET), \
                        patch.object(webhook, 'Service') as service, patch.object(webhook, 'HTTPServer'):
                    webhook.main()
                    self.assertTrue(service.call_args.kwargs['comment_smoke'])
                    self.assertEqual(service.call_args.args[0], SECRET)

    def load(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secret'
            path.write_bytes(content)
            return webhook.load_secret(path)

    def test_valid_secret(self):
        self.assertEqual(self.load(SECRET), SECRET)
        self.assertEqual(self.load(SECRET + b'\n'), SECRET)

    def test_invalid_configurations_fail_closed(self):
        cases = [b'', b'\xff' * 64, b'a' * 63, b'a' * 65, b'A' * 64,
                 b'g' * 64, SECRET + b'\n\n', SECRET + b'\ntrailing', b' ' * (webhook.MAX_JSON + 1)]
        for content in cases:
            with self.subTest(length=len(content)):
                with self.assertRaises(ValueError) as error:
                    self.load(content)
                self.assertEqual(str(error.exception), 'invalid webhook secret configuration')

    def test_missing_secret_file_error_redacts_path(self):
        with self.assertRaises(ValueError) as error:
            webhook.load_secret('/missing/private-secret-path')
        self.assertEqual(str(error.exception), 'invalid webhook secret configuration')


if __name__ == '__main__':
    unittest.main()
