"""Package this checkout's templates, dispatch Helm, and wait for that exact run."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request


# This standalone script is downloaded without sibling modules. Keep the image
# validator aligned with helm/scripts/image_updates.py and its documented contract.
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def parse_image_updates(encoded):
    if len(encoded) > 65535:
        raise ValueError("image_updates exceeds 65,535 characters")
    updates = json.loads(encoded, object_pairs_hook=unique_object,
                         parse_constant=reject_constant)
    if not isinstance(updates, list) or not 1 <= len(updates) <= 100:
        raise ValueError("image_updates must be a JSON array of 1 to 100 objects")
    targets = []
    for index, update in enumerate(updates):
        label = f"image_updates[{index}]"
        required = {'file', 'path', 'repository', 'digest'}
        if (not isinstance(update, dict) or not required <= update.keys()
                or update.keys() - (required | {'tag'})):
            raise ValueError(f"{label} requires file, path, repository and digest; only tag is optional")
        name = update['file']
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name):
            raise ValueError(f"{label}.file must be a lowercase file identifier")
        path = update['path']
        if (not isinstance(path, list) or not 1 <= len(path) <= 32
                or any(not isinstance(key, str) or not re.fullmatch(
                    r'[A-Za-z_][A-Za-z0-9_-]{0,127}', key) for key in path)):
            raise ValueError(f"{label}.path requires 1 to 32 literal mapping keys (letters, digits, _ and -)")
        repository = update['repository']
        if not isinstance(repository, str) or not 1 <= len(repository) <= 255:
            raise ValueError(f"{label}.repository must be a string of 1 to 255 characters")
        parts = repository.split('/')
        if ':' in parts[0] and len(parts) > 1:
            host, separator, port = parts.pop(0).partition(':')
            if (not separator or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', host)
                    or not re.fullmatch(r'[0-9]{1,5}', port) or not 1 <= int(port) <= 65535):
                raise ValueError(f"{label}.repository has an invalid registry host or port")
        if any(not re.fullmatch(r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*', part)
               for part in parts):
            raise ValueError(f"{label}.repository must be a lowercase repository without a tag or digest")
        if (not isinstance(update['digest'], str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', update['digest'])):
            raise ValueError(f"{label}.digest requires sha256 and 64 lowercase hexadecimal characters")
        if 'tag' in update and (not isinstance(update['tag'], str) or not re.fullmatch(
                r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', update['tag'])):
            raise ValueError(f"{label}.tag is invalid")
        target = tuple(path)
        for other_name, other_path in targets:
            common = min(len(target), len(other_path))
            if name == other_name and target[:common] == other_path[:common]:
                raise ValueError(f"{label} duplicates or overlaps another image target")
        targets.append((name, target))
    return updates


def package_templates(source):
    if not source.is_dir() or source.is_symlink():
        raise ValueError('helm-template must be a regular directory')
    output = io.BytesIO()
    count = 0
    total = 0
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for path in sorted(source.rglob('*')):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError('Template links and special files are forbidden')
            if path.is_dir():
                continue
            if path.suffix not in ('.yaml', '.yml', '.tpl') or path.name in ('Chart.yaml', 'values.yaml'):
                raise ValueError(f'Unsupported template: {path}')
            count += 1
            total += path.stat().st_size
            if count > 1000 or total > 10 * 1024 * 1024:
                raise ValueError('Templates exceed receiver size/count limits')
            archive.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
    if not count:
        raise ValueError('helm-template must not be empty')
    return base64.b64encode(output.getvalue()).decode('ascii')


class GitHubAPIError(RuntimeError):
    """A failed API request with bounded, credential-free diagnostics."""


def describe_api_error(req, error, token):
    details = {
        'method': req.get_method(),
        'url': req.full_url,
        'status': error.code,
    }
    if error.geturl() != req.full_url:
        details['response_url'] = error.geturl()
    try:
        body = json.loads(error.read(16384))
    except (ValueError, OSError):
        body = None
    finally:
        error.close()
    if isinstance(body, dict) and isinstance(body.get('message'), str):
        details['message'] = body['message'][:2000]
        # Validation errors may contain submitted values; only include field identifiers.
        if isinstance(body.get('errors'), list):
            details['errors'] = [
                {key: item[key][:200] for key in ('resource', 'field', 'code')
                 if isinstance(item.get(key), str)}
                for item in body['errors'][:10] if isinstance(item, dict)
            ]
    else:
        details['message'] = 'GitHub returned an empty, non-JSON or unreadable error response'
    for header, name in (
        ('X-GitHub-Request-Id', 'request_id'),
        ('X-Accepted-GitHub-Permissions', 'required_permissions'),
        ('X-OAuth-Scopes', 'token_scopes'),
        ('X-Accepted-OAuth-Scopes', 'accepted_scopes'),
    ):
        if error.headers.get(header):
            details[name] = error.headers[header][:1000]
    if error.headers.get('X-GitHub-SSO'):
        # Keep the SSO requirement, not the authorization URL or its query parameters.
        details['sso'] = error.headers['X-GitHub-SSO'].split(';', 1)[0][:200]
    text = json.dumps(details, ensure_ascii=True)
    if token:
        text = text.replace(json.dumps(token, ensure_ascii=True)[1:-1], '[REDACTED]')
    return re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)', '[REDACTED]', text)


def request(repository, endpoint, payload=None):
    token = os.environ['GH_TOKEN']
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f'https://api.github.com/repos/{repository}/actions' + endpoint, data=data, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2026-03-10',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise GitHubAPIError('GitHub API request failed: ' + describe_api_error(req, error, token)) from None


def wait_for_run(repository, run_id, timeout=3600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = request(repository, f'/runs/{run_id}')
        if run['status'] == 'completed':
            if run['conclusion'] != 'success':
                raise RuntimeError(f"Helm Action concluded: {run['conclusion']}")
            print('Helm Action succeeded; check its summary for synchronization and deployment status', flush=True)
            return
        time.sleep(10)
    raise TimeoutError('Timed out waiting for Helm Action; check its run URL before retrying')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='Target GitHub repository: owner/repository')
    parser.add_argument('--workflow', default='deploy.yml', help='Target workflow filename')
    parser.add_argument('--ref', default='main', help='Target Git ref')
    parser.add_argument('--image-info', default='', help='Optional <file>=<repository>[:<tag>]@sha256:<digest>; legacy tags are resolved by Helm')
    parser.add_argument('--image-updates-file', type=Path, help='JSON file of digest-only image updates; mutually exclusive with --image-info')
    parser.add_argument('--project', default='', help='Template directory owner in Helm')
    parser.add_argument('--templates-dir', type=Path, help='Local template directory; omit to leave templates unchanged')
    parser.add_argument('--timeout', type=int, default=3600, help='Maximum wait in seconds')
    args = parser.parse_args(argv)
    if args.image_info and args.image_updates_file is not None:
        parser.error('--image-info and --image-updates-file are mutually exclusive')
    if args.image_info:
        name, separator, reference = args.image_info.partition('=')
        if not separator or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name):
            parser.error('--image-info must start with a valid file identifier followed by =')
        image, at, digest = reference.partition('@')
        if at and not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            parser.error('--image-info requires a complete sha256 digest')
        repository = image
        tag = None
        if ':' in image.rsplit('/', 1)[-1]:
            repository, _, tag = image.rpartition(':')
        if (not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*', repository)
                or any(part in ('', '.', '..') for part in repository.split('/'))
                or ':' in repository.rsplit('/', 1)[-1]
                or (tag is not None and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag))
                or (not at and tag is None)):
            parser.error('--image-info must contain a valid repository and explicit digest or legacy tag')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', args.repo) or args.repo.split('/')[1] in ('.', '..'):
        parser.error('--repo must be owner/repository')
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml', args.workflow):
        parser.error('--workflow must be a YAML filename')
    if bool(args.project) != (args.templates_dir is not None):
        parser.error('--project and --templates-dir must be supplied together')
    if args.project and not re.fullmatch(r'[a-z0-9][a-z0-9-]*', args.project):
        parser.error('invalid --project')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    return args


def main(argv=None):
    args = parse_args(argv)
    inputs = {}
    if args.image_updates_file is not None:
        with args.image_updates_file.open(encoding='utf-8') as source:
            updates = parse_image_updates(source.read(65536))
        inputs['image_updates'] = json.dumps(updates, separators=(',', ':'))
    if not os.environ.get('GH_TOKEN'):
        raise ValueError('GH_TOKEN environment variable is required')
    if args.image_info:
        inputs['image_info'] = args.image_info
    if args.templates_dir is not None:
        inputs['project'] = args.project
        inputs['templates_archive'] = package_templates(args.templates_dir)
    if len(json.dumps(inputs)) > 65535:
        raise ValueError('Dispatch inputs exceed 65,535 characters')
    # Read with the exact CI credential before attempting a deployment.
    workflow = request(args.repo, f'/workflows/{args.workflow}')
    print(f"Helm workflow accessible: {args.repo}/{args.workflow}; "
          f"id={workflow.get('id')}; state={workflow.get('state')}", flush=True)
    # Do not retry POST: an uncertain response may already have queued a deployment.
    result = request(args.repo, f'/workflows/{args.workflow}/dispatches', {'ref': args.ref, 'inputs': inputs})
    run_id = result.get('workflow_run_id')
    if not isinstance(run_id, int) or run_id <= 0:
        raise RuntimeError('Dispatch did not return a valid workflow_run_id')
    url = f'https://github.com/{args.repo}/actions/runs/{run_id}'
    print(f'Helm Action: {url}', flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(f'Helm Action: [{run_id}]({url})\n')
    wait_for_run(args.repo, run_id, timeout=args.timeout)


if __name__ == '__main__':
    try:
        main()
    except GitHubAPIError as error:
        print(error, file=sys.stderr, flush=True)
        sys.exit(1)
