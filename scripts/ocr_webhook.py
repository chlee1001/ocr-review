#!/usr/bin/env python3
"""Loopback Gitea review receiver.

Required environment: OCR_WEBHOOK_SECRETS_FILE, OCR_GITEA_TOKEN_FILE,
OCR_BOT_LOGIN, OCR_GITEA_URL, OCR_GIT_SSH_BASE. The secrets file maps repository names to unique 64-digit hex secrets.
OCR_BIN defaults to ~/.local/bin/ocr. OCR's openai-responses provider and
model credentials must already be configured for the service account.
"""

import argparse
import hashlib
import hmac
import html
import ipaddress
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote, urlsplit

MAX_BODY = 1024 * 1024
MAX_API = 4 * 1024 * 1024
MAX_REPORT = 12000
MAX_JSON = 4 * 1024 * 1024
MARKER = "<!-- ocr-review-summary -->"
EN_SEPARATOR = '\n---OCR-EN---\n'
BACKGROUND = ('For each finding, write the content in Korean first, then the exact separator '
              '---OCR-EN--- on its own line, then an English translation of the same finding. '
              'Do not create separate findings for each language. Preserve all findings.')
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\Z")


def base_url(value, *, ssh=False):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if host and ':' in host:
            ipaddress.IPv6Address(host)
        elif not host or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
            raise ValueError('invalid host')
        if (parsed.scheme not in ({'ssh'} if ssh else {'http', 'https'})
                or (port is not None and not 1 <= port <= 65535)
                or parsed.password is not None or parsed.query or parsed.fragment
                or (ssh and (not parsed.username or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', parsed.username)))
                or (not ssh and parsed.username is not None)
                or not re.fullmatch(r'(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*/?', parsed.path)
                or any(part in {'.', '..'} for part in parsed.path.split('/'))
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or parsed.netloc.endswith(':') or '?' in value or '#' in value):
            raise ValueError('invalid URL')
        return value.rstrip('/')
    except ValueError as error:
        raise ValueError('invalid webhook URL configuration') from error


def load_secrets(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate webhook configuration key')
            result[key] = value
        return result

    try:
        with open(path, 'rb') as source:
            raw = source.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError('webhook configuration too large')
        entries = json.loads(raw, object_pairs_hook=unique_object)
        if (not isinstance(entries, dict) or not entries
                or any(not REPO.fullmatch(repo) or not isinstance(secret, str)
                       or not re.fullmatch(r'[0-9a-f]{64}', secret)
                       for repo, secret in entries.items())
                or len(set(entries.values())) != len(entries)):
            raise ValueError('invalid webhook configuration')
        return MappingProxyType({repo: secret.encode('ascii') for repo, secret in entries.items()})
    except (UnicodeError, RecursionError) as error:
        raise ValueError('invalid webhook configuration') from error


@dataclass(frozen=True)
class Job:
    repo: str
    number: int
    base: str
    head: str
    base_ref: str


def valid_ref(ref):
    return (isinstance(ref, str) and len(ref) <= 200 and REF.fullmatch(ref)
            and '..' not in ref and '//' not in ref and not ref.endswith(('.', '/', '.lock'))
            and all(not part.startswith('.') and not part.endswith('.lock') for part in ref.split('/')))


def parse_job(payload, allowed):
    try:
        if payload['action'] not in {'opened', 'reopened', 'synchronized', 'synchronize'}:
            return None
        pr = payload['pull_request']
        repo = payload['repository']['full_name']
        number = pr['number']
        base, head = pr['base'], pr['head']
        if (not isinstance(repo, str) or repo not in allowed or not REPO.fullmatch(repo)
                or type(number) is not int or not 0 < number <= 2147483647
                or base['repo']['full_name'] != repo or head['repo']['full_name'] != repo
                or not isinstance(base['sha'], str) or not SHA.fullmatch(base['sha'])
                or not isinstance(head['sha'], str) or not SHA.fullmatch(head['sha'])
                or not valid_ref(base['ref'])):
            return None
        return Job(repo, number, base['sha'], head['sha'], base['ref'])
    except (KeyError, TypeError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Gitea:
    def __init__(self, token, gitea_url):
        self.token = token
        self.api = gitea_url + '/api/v1'
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(self.api + path, data=body, method=method,
                                     headers={'Authorization': 'token ' + self.token,
                                              'Content-Type': 'application/json',
                                              'Accept': 'application/json'})
        with self.opener.open(req, timeout=15) as response:
            raw = response.read(MAX_API + 1)
            if len(raw) > MAX_API:
                raise ValueError('API response too large')
            return None if response.status == 204 else json.loads(raw)

    def current(self, job):
        pr = self.request('GET', f'/repos/{job.repo}/pulls/{job.number}')
        try:
            return (pr['state'] == 'open' and pr['base']['repo']['full_name'] == job.repo
                    and pr['head']['repo']['full_name'] == job.repo
                    and pr['base']['sha'] == job.base and pr['head']['sha'] == job.head
                    and pr['base']['ref'] == job.base_ref)
        except (KeyError, TypeError):
            return False

    def find_comment(self, job, login):
        for page in range(1, 21):
            comments = self.request('GET', f'/repos/{job.repo}/issues/{job.number}/comments?limit=50&page={page}')
            if not isinstance(comments, list):
                raise ValueError('invalid comments')
            for comment in comments:
                if (isinstance(comment, dict) and isinstance(comment.get('user'), dict)
                        and comment['user'].get('login') == login
                        and isinstance(comment.get('body'), str)
                        and comment['body'].startswith(MARKER)
                        and type(comment.get('id')) is int and comment['id'] > 0):
                    return comment
            if len(comments) < 50:
                return None
        raise ValueError('too many comments')

    def save_comment(self, job, comment, body):
        if comment:
            return self.request('PATCH', f"/repos/{job.repo}/issues/comments/{comment['id']}", {'body': body})
        return self.request('POST', f'/repos/{job.repo}/issues/{job.number}/comments', {'body': body})

    def review_reaction(self, job, method):
        return self.request(method, f'/repos/{job.repo}/issues/{job.number}/reactions', {'content': 'eyes'})

    def review_markers(self, job, login):
        root = f'/repos/{job.repo}/pulls/{job.number}/reviews'
        markers = set()
        pending = False
        def pages(path):
            for page in range(1, 21):
                items = self.request('GET', f'{path}?limit=50&page={page}')
                if not isinstance(items, list):
                    raise ValueError('invalid review list')
                yield from items
                if len(items) < 50:
                    return
            raise ValueError('too many reviews')
        for review in pages(root):
            if not isinstance(review, dict):
                raise ValueError('invalid review')
            if review.get('user') is None:
                continue
            if not isinstance(review['user'], dict):
                raise ValueError('invalid review user')
            if review['user'].get('login') != login:
                continue
            if type(review.get('id')) is not int or not isinstance(review.get('state'), str):
                raise ValueError('invalid review')
            if review['state'].upper() == 'PENDING':
                pending = True
                continue
            comments = self.request('GET', f"{root}/{review['id']}/comments")
            if not isinstance(comments, list):
                raise ValueError('invalid review comments')
            for comment in comments:
                if (isinstance(comment, dict) and isinstance(comment.get('user'), dict)
                        and comment['user'].get('login') == login and isinstance(comment.get('body'), str)):
                    markers.update(re.findall(r'<!-- ocr-inline ([0-9a-f]{64}) -->', comment['body']))
        return markers, pending

    def create_review(self, job, comments):
        return self.request('POST', f'/repos/{job.repo}/pulls/{job.number}/reviews',
                            {'commit_id': job.head, 'event': 'COMMENT', 'comments': comments})


@dataclass(frozen=True)
class ReviewResult:
    status: str
    markdown: str
    inline: tuple = ()


def safe_text(value, limit=1200):
    value = value[:limit] + ('…' if len(value) > limit else '')
    value = ' '.join(value.split())
    value = ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127)
    value = html.escape(value, quote=True)
    value = re.sub(r'([\\`*{}\[\]()#+\-.!_|~])', r'\\\1', value)
    return value.replace('@', '@\u200b').replace(':', ':\u200b').replace('.', '.\u200b')


def finding_text(content):
    parts = content.split(EN_SEPARATOR)
    if (len(parts) == 2 and all(part.strip() for part in parts)
            and re.search(r'[가-힣]', parts[0]) and not re.search(r'[가-힣]', parts[1])):
        korean, english = parts
        return (f'{safe_text(korean)}\n\n<details>\n<summary>English</summary>\n\n'
                f'{safe_text(english)}\n\n</details>')
    if not re.search(r'[가-힣]', content) and re.search(r'[A-Za-z]', content):
        return ('한국어 설명을 제공하지 못했습니다. 원문을 확인해 주세요.\n\n'
                f'<details>\n<summary>원문</summary>\n\n{safe_text(content)}\n\n</details>')
    return safe_text(content)


def render_review(job, merge_base, data, returncode, source_link):
    if not isinstance(data, dict) or not isinstance(data.get('manifest'), dict):
        raise ValueError('invalid review')
    manifest = data['manifest']
    state = manifest.get('terminal_state')
    expected_input = {'mode': 'range', 'requested_from': job.base, 'requested_head': job.head,
                      'resolved_base': merge_base, 'resolved_head': job.head,
                      'exact_range': f'{merge_base}..{job.head}'}
    review_input = manifest.get('input')
    if (manifest.get('schema_version') != 'ocr.run-manifest/v1'
            or manifest.get('operation') != 'review'
            or not isinstance(review_input, dict)
            or any(review_input.get(key) != value for key, value in expected_input.items())
            or not isinstance(state, str) or state not in {'complete', 'partial', 'failed', 'skipped'}
            or data.get('status') != state or 'comments' not in data):
        raise ValueError('invalid review manifest')
    coverage = manifest.get('coverage')
    if not isinstance(coverage, dict) or any(not isinstance(coverage.get(key), list) for key in
                                            ('selected', 'completed', 'reused', 'failed', 'waived', 'excluded')):
        raise ValueError('invalid review coverage')
    selected, failed = len(coverage['selected']), len(coverage['failed'])
    covered = sum(len(coverage[key]) for key in ('completed', 'reused', 'failed', 'waived'))
    expected_state = ('failed' if manifest.get('run_failure') or selected and selected == failed
                      else 'skipped' if not selected else 'partial' if failed else 'complete')
    if (selected != covered or state != expected_state or state == 'failed'
            or returncode not in (0, 1) or (returncode and state != 'partial')):
        raise ValueError('review failed')
    if state == 'partial':
        if any(not isinstance(item, dict) for item in coverage['failed']):
            raise ValueError('invalid failed item')
        budget_only = all(item.get('classification') == 'budget' for item in coverage['failed'])
        if not returncode and not budget_only:
            raise ValueError('inconsistent partial review')
    comments = data['comments']
    if comments is None:
        comments = []
    if not isinstance(comments, list):
        raise ValueError('review failed')
    if state == 'skipped' and comments:
        raise ValueError('invalid skipped review')
    for item in comments:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not isinstance(item.get('content'), str)
                or type(item.get('start_line')) is not int or type(item.get('end_line')) is not int
                or any(key in item and not isinstance(item[key], str)
                       for key in ('severity', 'category', 'existing_code', 'suggestion_code'))):
            raise ValueError('invalid finding')
    priority = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3, 'info': 4}
    comments.sort(key=lambda item: priority.get(item.get('severity', '').lower(), 5))
    labels = {'complete': '리뷰 완료', 'partial': '리뷰 일부 완료', 'skipped': '리뷰 생략'}
    header = f"## {labels[state]}\n\n대상 커밋: `{job.head[:12]}`"
    header += f' · 검토 범위: 선택 {selected}개, 완료 {len(coverage["completed"]) + len(coverage["reused"])}개'
    if comments:
        header += f' · 지적 {len(comments)}건'
        major = sum(item.get('severity', '').lower() in {'critical', 'high'} for item in comments)
        if major:
            header += f'\n\n**우선 확인:** 높음 이상 지적 {major}건'
    if state == 'partial':
        reason = '리뷰 오류' if returncode else '토큰 한도'
        header += f'\n\n{reason}로 선택 항목 {selected}개 중 {failed}개를 검토하지 못했습니다.'
    elif state == 'skipped':
        header += '\n\n검토할 파일이 선택되지 않았습니다.'
    elif not comments:
        header += '\n\n발견된 사항이 없습니다.'
    blocks = []
    inline = []
    occurrences = {}
    for item in comments:
        path = item['path']
        line = item.get('start_line', 0)
        location = safe_text(path, 300)
        if line > 0:
            location += f':{line}'
        source = (source_link(path, line, item.get('end_line', 0), item.get('existing_code', ''))
                if len(blocks) < 20 else None)
        if source:
            link, added = source
            location = f'<a href="{html.escape(link, quote=True)}" target="_blank" rel="noopener noreferrer">{location}</a>'
        severities = {'critical': '치명적', 'high': '높음', 'medium': '보통', 'low': '낮음', 'info': '정보'}
        categories = {'bug': '버그', 'security': '보안', 'performance': '성능',
                      'maintainability': '유지보수', 'style': '스타일', 'test': '테스트',
                      'documentation': '문서', 'other': '기타'}
        raw_severity = item.get('severity', '')
        raw_category = item.get('category', '')
        severity = safe_text(severities.get(raw_severity.lower(), raw_severity) or '미분류', 80)
        category = safe_text(categories.get(raw_category.lower(), raw_category), 80)
        text = finding_text(item['content'])
        number = f'{len(blocks) + 1}. '
        indent = ' ' * len(number)
        description = '\n'.join(indent + part if part else '' for part in text.split('\n'))
        block = f"\n\n{number}**{severity}** · {location}  \n{description}"
        if category:
            block += f'  \n{indent}분류: {category}'
        if len(header) + sum(map(len, blocks)) + len(block) + 100 > MAX_REPORT:
            break
        if source and added and raw_severity.lower() in {'critical', 'high'} and len(inline) < 3:
            identity = (path, item.get('existing_code', '').strip(), raw_category.lower())
            occurrence = occurrences.get(identity, 0)
            occurrences[identity] = occurrence + 1
            digest = hashlib.sha256(json.dumps([*identity, occurrence], ensure_ascii=False).encode()).hexdigest()
            body = f'<!-- ocr-inline {digest} -->\n**{severity}** · {category}\n\n{text}'
            if item.get('suggestion_code'):
                body += f"\n\n<details><summary>선택적 제안</summary>\n\n{safe_text(item['suggestion_code'])}\n\n</details>"
            inline.append((digest, {'path': path, 'new_position': line, 'body': body}))
        blocks.append(block)
    omitted = len(comments) - len(blocks)
    if omitted:
        blocks.append(f'\n\n나머지 {omitted}건은 길이 제한으로 생략했습니다.')
    return ReviewResult(state, header + ''.join(blocks), tuple(inline))


def run_review(job, ocr, gitea_url, ssh_base, provider='openai-responses'):
    with tempfile.TemporaryDirectory(prefix='ocr-review-') as tmp:
        repo_dir = Path(tmp) / 'repository'
        repo_dir.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('GIT_') and k not in {'OCR_WEBHOOK_SECRETS_FILE', 'OCR_GITEA_TOKEN_FILE'}}
        env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'})
        def git(*args):
            return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=repo_dir,
                                  env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=120, check=True).stdout.decode().strip()
        git('init', '--quiet', '--template=')
        git('remote', 'add', 'origin', f'{ssh_base}/{job.repo}.git')
        git('fetch', '--quiet', '--no-tags', 'origin',
            f'+refs/heads/{job.base_ref}:refs/ocr/base',
            f'+refs/pull/{job.number}/head:refs/ocr/head')
        if git('rev-parse', 'refs/ocr/base^{commit}') != job.base or git('rev-parse', 'refs/ocr/head^{commit}') != job.head:
            raise ValueError('revision changed')
        merge_base = git('merge-base', job.base, job.head)
        git('checkout', '--quiet', '--detach', job.base)
        output = Path(tmp) / 'review-output.json'
        process = subprocess.run([ocr, 'review', '--provider', provider, '--audience', 'agent',
                        '--color', 'never', '--effort', 'low', '--concurrency', '2', '--timeout', '5',
                        '--from', job.base, '--to', job.head, '--background', BACKGROUND,
                        '--format', 'json', '--output', str(output)],
                       cwd=repo_dir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=480, check=False)
        with output.open('rb') as f:
            data = f.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise ValueError('review too large')
        cache = {}
        def source_link(path, line, end, existing_code):
            if (len(path) > 300 or not path or path.startswith('/') or '\\' in path
                    or any(part in {'', '.', '..'} for part in path.split('/'))
                    or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or not 0 < line <= end or not existing_code.strip()):
                return None
            if path not in cache:
                try:
                    entry = git('ls-tree', job.head, '--', ':(literal)' + path)
                    if not entry.startswith(('100644 blob ', '100755 blob ')):
                        return None
                    size = int(git('cat-file', '-s', f'{job.head}:{path}'))
                    if size > 1024 * 1024:
                        return None
                    content = subprocess.run(['git', 'cat-file', 'blob', f'{job.head}:{path}'],
                                             cwd=repo_dir, env=env, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             timeout=120, check=True).stdout
                    cache[path] = content.decode('utf-8').split('\n')
                    if content.endswith(b'\n'):
                        cache[path].pop()
                except (ValueError, OSError, subprocess.SubprocessError):
                    return None
            lines = cache[path]
            if end > len(lines) or '\n'.join(lines[line - 1:end]).strip() != existing_code.strip():
                return None
            try:
                diff = git('diff', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames',
                           '--unified=0', merge_base, job.head, '--', ':(literal)' + path)
                added = set()
                new_line = None
                for row in diff.split('\n'):
                    match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', row)
                    if match:
                        new_line = int(match.group(1))
                    elif new_line is not None and row.startswith('+'):
                        added.add(new_line)
                        new_line += 1
                    elif new_line is not None and row.startswith(' '):
                        new_line += 1
                is_added = all(n in added for n in range(line, end + 1))
            except (ValueError, OSError, subprocess.SubprocessError):
                is_added = False
            return (f'{gitea_url}/{job.repo}/src/commit/{job.head}/{quote(path, safe="/")}?display=source#L{line}',
                    is_added)
        return render_review(job, merge_base, json.loads(data), process.returncode, source_link)


class Service:
    def __init__(self, secrets, token, login, ocr, gitea_url, ssh_base, *, comment_smoke=False):
        self.secrets, self.login, self.ocr = MappingProxyType(dict(secrets)), login, ocr
        self.gitea_url, self.ssh_base = gitea_url, ssh_base
        self.comment_smoke = comment_smoke
        self.api = Gitea(token, gitea_url)
        self.jobs = queue.Queue(maxsize=16)
        self.pending = set()
        self.lock = threading.Lock()
        threading.Thread(target=self.worker, daemon=True).start()

    def enqueue(self, job):
        with self.lock:
            if job in self.pending:
                return True
            try:
                self.jobs.put_nowait(job)
            except queue.Full:
                return False
            self.pending.add(job)
            return True

    def worker(self):
        while True:
            job = self.jobs.get()
            try:
                for attempt in range(3):
                    try:
                        self.process(job)
                        break
                    except urllib.error.HTTPError as error:
                        if error.code != 429 and error.code < 500:
                            raise
                    except (urllib.error.URLError, TimeoutError):
                        pass
                    if attempt < 2:
                        time.sleep(attempt + 1)
            except Exception:
                # No exception text: subprocess/API errors can contain private data.
                pass
            finally:
                with self.lock:
                    self.pending.discard(job)
                self.jobs.task_done()

    def process(self, job, *, provider='openai-responses'):
        if not self.api.current(job):
            return
        comment = self.api.find_comment(job, self.login)
        kind = 'smoke' if self.comment_smoke else 'success'
        success = f'<!-- ocr-{kind} v=4 base={job.base} head={job.head} -->'
        if comment and success in comment['body'] and '<!-- ocr-attempt ' not in comment['body']:
            return
        if not self.api.current(job):
            return
        self.api.review_reaction(job, 'POST')
        try:
            try:
                report = (ReviewResult('smoke', '[모의 결과] 댓글 연동 테스트입니다. OCR 모델을 호출하지 않았습니다.')
                          if self.comment_smoke else run_review(job, self.ocr, self.gitea_url, self.ssh_base, provider))
            except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
                report = ReviewResult('failed', f'## 리뷰 실패\n\n대상 커밋: `{job.head[:12]}`')
            comment = self.api.find_comment(job, self.login)
            attempts = set(re.findall(r'<!-- ocr-attempt ([0-9a-f]{64}) -->', comment['body'])) if comment else set()
            seen, pending, available = set(), False, True
            if report.inline or attempts:
                try:
                    seen, pending = self.api.review_markers(job, self.login)
                    if attempts <= seen:
                        attempts.clear()
                except (OSError, ValueError, urllib.error.URLError):
                    available = False
            candidates = [entry for entry in report.inline if entry[0] not in seen]
            def result():
                marker = f'\n{success}' if report.status in {'complete', 'smoke'} else ''
                intent = ''.join(f'\n<!-- ocr-attempt {digest} -->' for digest in sorted(attempts))
                notice = ('\n\n인라인 게시 여부를 확인하지 못했습니다. 아래 요약을 확인해 주세요.'
                          if attempts or (candidates and (pending or not available)) else '')
                return f'{MARKER}{marker}{intent}\n{report.markdown}{notice}'
            if candidates and available and not pending and not attempts:
                attempts.update(digest for digest, _ in candidates)
                if not self.api.current(job):
                    return
                comment = self.api.save_comment(job, comment, result())
                if not self.api.current(job):
                    return
                try:
                    self.api.create_review(job, [entry for _, entry in candidates])
                    seen, pending = self.api.review_markers(job, self.login)
                    if attempts <= seen:
                        attempts.clear()
                except (OSError, ValueError, urllib.error.URLError):
                    pass
            if not self.api.current(job):
                return
            self.api.save_comment(job, comment, result())
        finally:
            self.api.review_reaction(job, 'DELETE')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status):
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if self.path != '/gitea':
            self.reply(404)
            return
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get('Transfer-Encoding'):
            self.reply(400)
            return
        if len(lengths[0]) > 10:
            self.reply(413)
            return
        length = int(lengths[0])
        if length > MAX_BODY:
            self.reply(413)
            return
        body = self.rfile.read(length)
        signatures = self.headers.get_all('X-Gitea-Signature', [])
        if len(body) != length or len(signatures) != 1 or not re.fullmatch(r'[0-9a-f]{64}', signatures[0]):
            self.reply(401)
            return
        try:
            payload = json.loads(body)
            repo = payload['repository']['full_name']
            secret = self.server.service.secrets.get(repo) if isinstance(repo, str) else None
        except (ValueError, UnicodeError, RecursionError):
            self.reply(400)
            return
        except (KeyError, TypeError):
            secret = None
        if secret is None or not hmac.compare_digest(hmac.new(secret, body, hashlib.sha256).hexdigest(), signatures[0]):
            self.reply(401)
            return
        if self.headers.get_all('X-Gitea-Event', []) != ['pull_request']:
            self.reply(204)
            return
        job = parse_job(payload, self.server.service.secrets)
        if not job:
            self.reply(204)
        elif self.server.service.enqueue(job):
            self.reply(202)
        else:
            self.reply(503)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--comment-smoke', action='store_true', help='test comment delivery without calling OCR')
    args = parser.parse_args()
    secrets = load_secrets(os.environ['OCR_WEBHOOK_SECRETS_FILE'])
    gitea_url = base_url(os.environ['OCR_GITEA_URL'])
    ssh_base = base_url(os.environ['OCR_GIT_SSH_BASE'], ssh=True)
    token = Path(os.environ['OCR_GITEA_TOKEN_FILE']).read_text().strip()
    login = os.environ['OCR_BOT_LOGIN']
    if not token or not login:
        raise ValueError('invalid webhook configuration')
    if args.comment_smoke and len(secrets) != 1:
        parser.error('--comment-smoke requires exactly one registered repository')
    service = Service(secrets, token, login, os.path.expanduser(os.environ.get('OCR_BIN', '~/.local/bin/ocr')),
                      gitea_url, ssh_base, comment_smoke=args.comment_smoke)
    server = HTTPServer(('127.0.0.1', args.port), Handler)
    server.service = service
    server.serve_forever()


if __name__ == '__main__':
    main()
