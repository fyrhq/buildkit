import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

import deploy_helm


class GitHubDiagnosticsTests(unittest.TestCase):
    token = 'github_pat_test_credential'

    def setUp(self):
        self.env = patch.dict(os.environ, {'GH_TOKEN': self.token}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def http_error(self, status, body, headers=None, url=None):
        message = Message()
        for key, value in (headers or {}).items():
            message[key] = value
        return urllib.error.HTTPError(
            url or 'https://api.github.com/repos/fyrhq/helm/actions/workflows/deploy.yml',
            status, 'API error', message, io.BytesIO(body),
        )

    def test_404_preserves_message_and_request_context_without_retry(self):
        error = self.http_error(404, b'{"message":"Not Found"}', {
            'X-GitHub-Request-Id': 'request-404',
            'X-Accepted-GitHub-Permissions': 'actions=read',
        })
        with patch('deploy_helm.urllib.request.urlopen', side_effect=error) as opener:
            with self.assertRaises(deploy_helm.GitHubAPIError) as caught:
                deploy_helm.request('fyrhq/helm', '/workflows/deploy.yml')
        text = str(caught.exception)
        for expected in ('GET', '404', 'Not Found', 'request-404', 'actions=read'):
            self.assertIn(expected, text)
        self.assertNotIn(self.token, text)
        self.assertEqual(opener.call_count, 1)

    def test_post_failure_preserves_permissions_and_validation_fields(self):
        payload = {'ref': 'main', 'inputs': {'templates_archive': 'private-template-content'}}
        body = json.dumps({
            'message': 'Resource not accessible by personal access token',
            'errors': [{'resource': 'Workflow', 'field': 'ref', 'code': 'invalid',
                        'value': payload, 'message': self.token}],
        }).encode()
        error = self.http_error(403, body, {
            'X-Accepted-GitHub-Permissions': 'actions=write',
            'X-OAuth-Scopes': 'repo, workflow',
            'X-GitHub-SSO': 'required; url=https://github.com/orgs/fyrhq/sso?secret=private',
        }, url='https://api.github.com/repos/fyrhq/helm/actions/workflows/deploy.yml/dispatches')
        with patch('deploy_helm.urllib.request.urlopen', side_effect=error) as opener:
            with self.assertRaises(deploy_helm.GitHubAPIError) as caught:
                deploy_helm.request('fyrhq/helm', '/workflows/deploy.yml/dispatches', payload)
        text = str(caught.exception)
        for expected in ('POST', '403', 'actions=write', 'required', 'Workflow', 'ref', 'invalid'):
            self.assertIn(expected, text)
        for private in (self.token, 'private-template-content', 'secret=private'):
            self.assertNotIn(private, text)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(json.loads(opener.call_args.args[0].data), payload)

    def test_tokens_are_redacted_even_if_echoed_in_message_or_headers(self):
        body = json.dumps({'message': self.token + ' ghp_another_token'}).encode()
        error = self.http_error(401, body, {'X-GitHub-Request-Id': self.token})
        with patch('deploy_helm.urllib.request.urlopen', side_effect=error):
            with self.assertRaises(deploy_helm.GitHubAPIError) as caught:
                deploy_helm.request('fyrhq/helm', '/workflows/deploy.yml')
        self.assertNotIn(self.token, str(caught.exception))
        self.assertNotIn('ghp_another_token', str(caught.exception))
        self.assertIn('[REDACTED]', str(caught.exception))

    def test_non_json_empty_and_unexpected_json_bodies_do_not_leak_raw_data(self):
        for body in (b'', b'<html>private error page</html>', b'[]', b'{"message":null}',
                     b'{"message":"' + b'x' * 20000 + b'"}'):
            with self.subTest(body_length=len(body)):
                error = self.http_error(502, body)
                with patch('deploy_helm.urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(deploy_helm.GitHubAPIError) as caught:
                        deploy_helm.request('fyrhq/helm', '/workflows/deploy.yml')
                self.assertIn('502', str(caught.exception))
                self.assertIn('unreadable error response', str(caught.exception))
                self.assertNotIn('private error page', str(caught.exception))

    def test_redirected_error_includes_original_and_response_urls(self):
        error = self.http_error(404, b'{"message":"Not Found"}',
            url='https://api.github.com/repositories/123/actions/workflows/deploy.yml/dispatches')
        with patch('deploy_helm.urllib.request.urlopen', side_effect=error):
            with self.assertRaises(deploy_helm.GitHubAPIError) as caught:
                deploy_helm.request('yuihjk/helm', '/workflows/deploy.yml/dispatches', {'ref': 'main'})
        self.assertIn('/repos/yuihjk/helm/', str(caught.exception))
        self.assertIn('/repositories/123/', str(caught.exception))

    def test_read_failure_stops_before_dispatch(self):
        error = self.http_error(404, b'{"message":"Not Found"}')
        with patch('deploy_helm.urllib.request.urlopen', side_effect=error) as opener:
            with self.assertRaises(deploy_helm.GitHubAPIError):
                deploy_helm.main(['--repo', 'fyrhq/helm'])
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.call_args.args[0].get_method(), 'GET')

    def test_read_success_followed_by_dispatch_404_is_reported_without_retry(self):
        workflow = io.BytesIO(b'{"id":123,"state":"active"}')
        error = self.http_error(404, b'{"message":"Not Found"}', {
            'X-GitHub-Request-Id': 'dispatch-request-id',
        }, url='https://api.github.com/repos/fyrhq/helm/actions/workflows/deploy.yml/dispatches')
        with patch('deploy_helm.urllib.request.urlopen', side_effect=[workflow, error]) as opener:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(deploy_helm.GitHubAPIError) as caught:
                    deploy_helm.main(['--repo', 'fyrhq/helm'])
        self.assertIn('Helm workflow accessible:', output.getvalue())
        self.assertIn('POST', str(caught.exception))
        self.assertIn('dispatch-request-id', str(caught.exception))
        self.assertEqual(opener.call_count, 2)

    def test_cli_exits_with_redacted_diagnostic_without_traceback(self):
        stub = '''
import io, json, os, runpy, sys, urllib.error
from email.message import Message
from unittest.mock import patch
headers = Message()
headers['X-GitHub-Request-Id'] = 'cli-request-id'
body = json.dumps({'message': 'Forbidden: ' + os.environ['GH_TOKEN']}).encode()
error = urllib.error.HTTPError('https://api.github.com/repos/fyrhq/helm/actions/workflows/deploy.yml',
    403, 'Forbidden', headers, io.BytesIO(body))
with patch('urllib.request.urlopen', side_effect=error):
    sys.argv = ['deploy_helm.py', '--repo', 'fyrhq/helm']
    runpy.run_path('deploy_helm.py', run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-B', '-c', stub],
            cwd=Path(deploy_helm.__file__).parent, capture_output=True, text=True,
            env={**os.environ, 'GH_TOKEN': self.token}, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('403', result.stderr)
        self.assertIn('cli-request-id', result.stderr)
        self.assertIn('[REDACTED]', result.stderr)
        self.assertNotIn(self.token, result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_success_reads_dispatches_and_waits_for_exact_run(self):
        responses = [
            {'id': 123, 'state': 'active'},
            {'workflow_run_id': 456},
            {'status': 'completed', 'conclusion': 'success'},
        ]
        with patch('deploy_helm.urllib.request.urlopen',
                   side_effect=[io.BytesIO(json.dumps(value).encode()) for value in responses]) as opener:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                deploy_helm.main(['--repo', 'fyrhq/helm', '--image-info', 'auth=registry/auth:217b61b'])
        requests = [call.args[0] for call in opener.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ['GET', 'POST', 'GET'])
        self.assertTrue(requests[2].full_url.endswith('/runs/456'))
        self.assertEqual(json.loads(requests[1].data), {
            'ref': 'main', 'inputs': {'image_info': 'auth=registry/auth:217b61b'},
        })
        for request in requests:
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.token)
            self.assertEqual(request.get_header('X-github-api-version'), '2026-03-10')
        self.assertIn('Helm workflow accessible:', output.getvalue())
        self.assertIn('actions/runs/456', output.getvalue())
        self.assertNotIn(self.token, output.getvalue())


if __name__ == '__main__':
    unittest.main()
