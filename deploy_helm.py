"""Package this checkout's templates, dispatch Helm, and wait for that exact run."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import re
import tarfile
import time
import urllib.request


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


def request(repository, endpoint, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f'https://api.github.com/repos/{repository}/actions' + endpoint, data=data, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2026-03-10',
    })
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def wait_for_run(repository, run_id, timeout=3600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = request(repository, f'/runs/{run_id}')
        if run['status'] == 'completed':
            if run['conclusion'] != 'success':
                raise RuntimeError(f"Helm Action concluded: {run['conclusion']}")
            print('Helm Action succeeded (asynchronous deployment request submitted)', flush=True)
            return
        time.sleep(10)
    raise TimeoutError('Timed out waiting for Helm Action; check its run URL before retrying')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='Target GitHub repository: owner/repository')
    parser.add_argument('--workflow', default='deploy.yml', help='Target workflow filename')
    parser.add_argument('--ref', default='main', help='Target Git ref')
    parser.add_argument('--image-info', default='', help='Optional <file>=<repository>:<tag>')
    parser.add_argument('--project', default='', help='Template directory owner in Helm')
    parser.add_argument('--templates-dir', type=Path, help='Local template directory; omit to leave templates unchanged')
    parser.add_argument('--timeout', type=int, default=3600, help='Maximum wait in seconds')
    args = parser.parse_args(argv)
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
    if not os.environ.get('GH_TOKEN'):
        raise ValueError('GH_TOKEN environment variable is required')
    inputs = {}
    if args.image_info:
        inputs['image_info'] = args.image_info
    if args.templates_dir is not None:
        inputs['project'] = args.project
        inputs['templates_archive'] = package_templates(args.templates_dir)
    if len(json.dumps(inputs)) > 65535:
        raise ValueError('Dispatch inputs exceed 65,535 characters')
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
    main()
