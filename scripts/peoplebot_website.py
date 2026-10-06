"""Import or publish the public PeopleBot static website using explicit FTPS."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import quote, unquote, urlsplit

ROOT = Path('website')
BASE = ('index.html', 'robots.txt', 'sitemap.xml', 'llms.txt')
ASSETS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.css', '.js', '.woff', '.woff2'}

def safe_path(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or any(x.startswith('.') for x in p.parts) or '\\' in name or any(ord(c) < 32 for c in name):
        raise ValueError('Invalid website path')
    if name not in BASE and p.suffix.lower() not in ASSETS:
        raise ValueError('Only public static website files are allowed')
    return p.as_posix()

def transfer(name, output=None, upload=None):
    name = safe_path(name)
    password = os.environ.get('PEOPLEBOT_FTP_PASSWORD', '')
    if not password:
        raise RuntimeError('PEOPLEBOT_FTP_PASSWORD secret is missing')
    # Credentials go through stdin, never the process arguments or a shell.
    credential = 'peoplebot@peoplebot.me:' + password
    escaped = credential.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
    args = ['curl', '--disable', '--config', '-', '--silent', '--show-error',
            '--fail', '--ssl-reqd', '--tlsv1.2', '--ftp-pasv', '--connect-timeout', '30',
            '--max-time', '180', '--proto', '=ftp',
            'ftp://ftp.webcustoms.com:21/' + quote(name, safe='/')]
    if upload is not None:
        args += ['--ftp-create-dirs', '--upload-file', str(upload)]
    else:
        args += ['--output', str(output)]
    result = subprocess.run(args, input='user = "' + escaped + '"\n', text=True, capture_output=True)
    if result.returncode:
        # Avoid echoing server responses, credentials, or curl config into logs.
        raise RuntimeError(f'FTPS transfer failed for {name} (curl exit {result.returncode}); check certificate, credentials, and server connectivity')

def references(text):
    # The existing homepage embeds CSS and references its public image assets.
    # Include quoted asset URLs and CSS url() references, never navigation URLs.
    for value in re.findall(r'''["']([^"'<>\s]+)["']|url\(\s*([^\s)]+)\s*\)''', text):
        raw = value[0] or value[1]
        u = urlsplit(raw)
        if u.scheme not in ('', 'https', 'http') or (u.netloc and u.netloc not in ('peoplebot.me', 'www.peoplebot.me')):
            continue
        path = unquote(u.path).lstrip('/')
        if PurePosixPath(path).suffix.lower() in ASSETS:
            yield safe_path(path)

def import_site():
    if ROOT.exists():
        raise RuntimeError('website/ already exists; import is one-time and will not overwrite source')
    with tempfile.TemporaryDirectory() as temp:
        staging = Path(temp)
        pending = list(BASE)
        done = set()
        while pending:
            name = pending.pop(0)
            if name in done:
                continue
            if len(done) >= 100:
                raise RuntimeError('Unexpectedly large asset list; inspect before importing')
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            transfer(name, output=target)
            done.add(name)
            if target.suffix in ('.html', '.css'):
                # Nested relative CSS assets require explicit review.
                if target.suffix == '.css' and '/' in name:
                    raise RuntimeError('Nested CSS found; adapt asset paths before import')
                pending.extend(references(target.read_text()))
        html = (staging / 'index.html').read_text()
        if 'peoplebot' not in html.lower() or '<html' not in html.lower():
            raise RuntimeError('Imported homepage does not look like PeopleBot HTML')
        import shutil
        shutil.copytree(staging, ROOT)
    print(f'Imported {len(done)} current public website files; remote files were not changed.')

def deploy():
    for name in BASE:
        if not (ROOT / name).is_file():
            raise RuntimeError(f'Missing website/{name}; run import first')
    files = sorted(p for p in ROOT.rglob('*') if p.is_file())
    for p in ROOT.rglob('*'):
        if p.is_symlink():
            raise RuntimeError('Symlinks are not allowed in website/')
    for p in files:
        safe_path(p.relative_to(ROOT).as_posix())
    # Upload the entry page last. No remote deletions are performed.
    files.sort(key=lambda p: p.name == 'index.html')
    with tempfile.TemporaryDirectory() as temp:
        check = Path(temp) / 'verify'
        for p in files:
            name = p.relative_to(ROOT).as_posix()
            transfer(name, upload=p)
            transfer(name, output=check)
            if hashlib.sha256(p.read_bytes()).digest() != hashlib.sha256(check.read_bytes()).digest():
                raise RuntimeError(f'Remote verification failed: {name}')
            print(f'Published and verified {name}')
    print('FTPS byte verification passed. HTTP/browser verification is separate.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('import', 'deploy'))
    args = parser.parse_args()
    try:
        import_site() if args.mode == 'import' else deploy()
    except (RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from None
