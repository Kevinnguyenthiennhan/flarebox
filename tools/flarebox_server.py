#!/usr/bin/env python3
"""Flarebox local server — serves the library page, runs GitHub imports and keeps importer settings.

  python3 tools/flarebox_server.py [--port 8790]
  → open http://localhost:8790/  ·  Repo scans › "Import from GitHub" › paste a link  ·  Settings tab for the token

POST /api/import {"url": "https://github.com/owner/repo", "name": "optional collection name", "branch": "" | "<name>" | "all"}
     runs tools/flarebox_scan_repo.py <url> --collection [name] in the background → {"job": id}
GET  /api/import/<id>      → {"state": "running|done|error", "log": [...], "collection": {...}}
GET  /api/branches?url=…   → {"default": "main", "branches": [...]} (branch picker)
GET  /api/cache            → git download cache size per repo · POST /api/cache/clear {"repo": dir?}
GET  /api/settings         → current settings (the token itself is never sent back, only its last 4 characters)
POST /api/settings         {"githubToken": "..." | null, "previewLimit": "all" | number}
POST /api/settings/test    {"githubToken": "..."?} → checks the given (or saved) token against the GitHub API
GET  /api/ping             → {"ok": true}

Settings live in ~/.flarebox/config.json (mode 600, outside the project so they are never committed).
A token saved there wins over the GITHUB_TOKEN / GH_TOKEN environment variables. Only listens on 127.0.0.1.
"""
import os, re, sys, json, time, shutil, argparse, threading, subprocess, http.server, urllib.request, urllib.error, urllib.parse

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
SCANNER = os.path.join(ROOT, 'tools', 'flarebox_scan_repo.py')
CONFIG = os.path.join(os.path.expanduser('~'), '.flarebox', 'config.json')
CACHE = os.path.join(os.path.expanduser('~'), '.flarebox', 'cache', 'github')
GH_RX = re.compile(r'^(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?(?:/tree/[^\s?#]+)?/?$')
BRANCH_RX = re.compile(r'^(?!-)[A-Za-z0-9._/-]{1,200}$')
LOCAL_RX = re.compile(r'^(localhost|127\.0\.0\.1)(:\d+)?$')
JOBS, LOCK = {}, threading.Lock()
DEFAULTS = {'githubToken': None, 'previewLimit': 'all'}


# ------------------------------------------------------------------ settings
def load_cfg():
    try:
        with open(CONFIG) as fh: return {**DEFAULTS, **json.load(fh)}
    except (OSError, ValueError): return dict(DEFAULTS)

def save_cfg(cfg):
    os.makedirs(os.path.dirname(CONFIG), mode=0o700, exist_ok=True)
    fd = os.open(CONFIG + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh: json.dump(cfg, fh, indent=1)
    os.replace(CONFIG + '.tmp', CONFIG)

def token_info(cfg):
    if cfg.get('githubToken'): return cfg['githubToken'], 'settings'
    env = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    return (env, 'env') if env else (None, None)

def public_settings():
    cfg = load_cfg(); tok, src = token_info(cfg)
    return {'token': {'set': bool(tok), 'source': src, 'hint': ('…' + tok[-4:]) if tok else None},
            'previewLimit': cfg.get('previewLimit') or 'all', 'configPath': CONFIG}

def check_token(tok):
    """-> dict describing what GitHub says about the token (never includes the token)."""
    def get(path):
        h = {'Accept': 'application/vnd.github+json', 'User-Agent': 'flarebox-local'}
        if tok: h['Authorization'] = 'Bearer ' + tok
        with urllib.request.urlopen(urllib.request.Request('https://api.github.com' + path, headers=h), timeout=20) as r:
            return json.loads(r.read()), r.headers
    try:
        rate, hdr = get('/rate_limit'); core = rate.get('resources', {}).get('core', {})
        out = {'ok': True, 'limit': core.get('limit'), 'remaining': core.get('remaining'), 'reset': core.get('reset')}
        scopes = hdr.get('X-OAuth-Scopes')
        if tok and scopes is not None:  # classic token: private repos need the full "repo" scope
            have = {s.strip() for s in scopes.split(',') if s.strip()}
            out['scopes'] = sorted(have)
            if 'repo' not in have:
                out['warning'] = ('This classic token has no "repo" scope (it has: ' + (', '.join(sorted(have)) or 'none') + '), so private repositories return "not found". '
                                  'Edit the token on GitHub and tick "repo", or create a fine-grained token with Contents: read.')
        if tok:
            try: me, _ = get('/user'); out['login'] = me.get('login')
            except urllib.error.HTTPError: pass  # some fine-grained tokens cannot read /user — the rate limit already proves it works
        return out
    except urllib.error.HTTPError as e:
        return {'ok': False, 'error': 'GitHub rejected the token (bad or expired credentials).' if e.code == 401 else f'GitHub answered HTTP {e.code}.'}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': f'Could not reach GitHub: {e}'}


def cache_info():
    """git download cache (~/.flarebox/cache/github/<owner>__<repo>.git): size per repository."""
    repos = []
    if os.path.isdir(CACHE):
        for d in sorted(os.listdir(CACHE)):
            full = os.path.join(CACHE, d)
            if not (d.endswith('.git') and os.path.isdir(full)): continue
            size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(full) for f in fs if not os.path.islink(os.path.join(dp, f)))
            repos.append({'dir': d, 'repo': d[:-4].replace('__', '/', 1), 'bytes': size, 'updated': os.path.getmtime(full)})
    return {'path': CACHE, 'bytes': sum(r['bytes'] for r in repos), 'repos': repos}


# ------------------------------------------------------------------ imports
def run_job(jid, url, name, branch):
    job = JOBS[jid]; cfg = load_cfg(); tok, _ = token_info(cfg)
    cmd = [sys.executable, '-u', SCANNER, url, '--collection'] + ([name] if name else []) + ['--preview-limit', str(cfg.get('previewLimit') or 'all')] + (['--branch', branch] if branch else [])
    env = dict(os.environ)
    if tok: env['GITHUB_TOKEN'] = tok
    try:
        p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        job['pid'] = p.pid
        for line in p.stdout:
            line = line.rstrip()
            if tok: line = line.replace(tok, '***')
            if line.startswith('FLAREBOX_COLLECTION '):
                try: job['collection'] = json.loads(line.split(' ', 1)[1])
                except ValueError: pass
                continue
            with LOCK:
                job['log'].append(line); job['log'] = job['log'][-400:]
        rc = p.wait()
        job['state'] = 'done' if rc == 0 and job.get('collection') else 'error'
        if job['state'] == 'error':
            if any('HTTP Error 404' in l or 'HTTP Error 401' in l for l in job['log'][-8:]):
                job['log'].append('GitHub refused access — for a private repo, add a token with Contents: read in the Settings tab.')
            elif not any('Error' in l for l in job['log'][-5:]): job['log'].append(f'scanner exited with code {rc}')
    except Exception as e:  # noqa: BLE001 — report anything to the page
        job['state'] = 'error'; job['log'].append(f'{type(e).__name__}: {e}')
    job['ended'] = time.time()


# ------------------------------------------------------------------ http
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=ROOT, **k)

    def log_message(self, fmt, *a):
        if '/api/' in (self.path or ''): return
        super().log_message(fmt, *a)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')  # scan output changes while the page is open
        super().end_headers()

    def json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def api_allowed(self):
        """API calls only from this machine's own page: localhost Host header (no DNS rebinding) and same-site Origin."""
        if not LOCAL_RX.match(self.headers.get('Host') or ''): return False
        origin = self.headers.get('Origin')
        return not origin or bool(re.match(r'^https?://(localhost|127\.0\.0\.1)(:\d+)?$', origin))

    def body(self):
        try: return json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        except ValueError: return None

    def do_GET(self):
        if self.path.startswith('/api/') and not self.api_allowed(): return self.json(403, {'error': 'forbidden'})
        if self.path == '/api/ping': return self.json(200, {'ok': True, 'token': public_settings()['token']['set']})
        if self.path == '/api/settings': return self.json(200, public_settings())
        if self.path == '/api/cache': return self.json(200, cache_info())
        if self.path.startswith('/api/branches?'): return self.get_branches()
        m = re.match(r'^/api/import/([a-z0-9]+)$', self.path)
        if m:
            job = JOBS.get(m.group(1))
            if not job: return self.json(404, {'error': 'unknown job'})
            with LOCK: return self.json(200, {k: v for k, v in job.items() if k != 'pid'})
        return super().do_GET()

    def do_POST(self):
        if not self.api_allowed(): return self.json(403, {'error': 'forbidden'})
        if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json': return self.json(415, {'error': 'send JSON'})
        body = self.body()
        if not isinstance(body, dict): return self.json(400, {'error': 'invalid JSON'})
        if self.path == '/api/settings': return self.post_settings(body)
        if self.path == '/api/settings/test':
            tok = str(body.get('githubToken') or '').strip() or token_info(load_cfg())[0]
            return self.json(200, {**check_token(tok), 'withToken': bool(tok)})
        if self.path == '/api/import': return self.post_import(body)
        if self.path == '/api/cache/clear':
            with LOCK:
                if any(j['state'] == 'running' for j in JOBS.values()): return self.json(409, {'error': 'An import is running — clear the cache after it finishes.'})
                name = str(body.get('repo') or '')
                for d in cache_info()['repos']:
                    if not name or d['dir'] == name: shutil.rmtree(os.path.join(CACHE, d['dir']), ignore_errors=True)
            return self.json(200, cache_info())
        return self.json(404, {'error': 'not found'})

    def get_branches(self):
        """?url=<github link> → {"default": "main", "branches": [...]} using the saved token (for the branch picker)."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); url = (q.get('url') or [''])[0].strip()
        m = re.match(r'^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/tree/[^\s?#]+)?/?$', url)
        if not m: return self.json(400, {'error': 'not a GitHub repository link'})
        tok, _ = token_info(load_cfg()); o, r = m.group(1), m.group(2)
        def get(path):
            h = {'Accept': 'application/vnd.github+json', 'User-Agent': 'flarebox-local'}
            if tok: h['Authorization'] = 'Bearer ' + tok
            with urllib.request.urlopen(urllib.request.Request('https://api.github.com' + path, headers=h), timeout=20) as x: return json.loads(x.read())
        try:
            info = get(f'/repos/{o}/{r}'); names, page = [], 1
            while page <= 10:
                b = get(f'/repos/{o}/{r}/branches?per_page=100&page={page}'); names += [x['name'] for x in b]
                if len(b) < 100: break
                page += 1
            d = info.get('default_branch')
            return self.json(200, {'default': d, 'branches': ([d] if d in names else []) + sorted(n for n in names if n != d), 'private': info.get('private')})
        except urllib.error.HTTPError as e:
            return self.json(200, {'error': {404: 'Repository not found (or the token cannot see it).', 409: 'Repository is empty.', 401: 'Token rejected.'}.get(e.code, f'GitHub HTTP {e.code}')})
        except Exception as e:  # noqa: BLE001
            return self.json(200, {'error': f'Could not reach GitHub: {e}'})

    def post_settings(self, body):
        cfg = load_cfg()
        if 'githubToken' in body:
            t = body['githubToken']
            if t is None or str(t).strip() == '': cfg['githubToken'] = None
            else:
                t = str(t).strip()
                if not re.match(r'^[A-Za-z0-9_]{20,255}$', t): return self.json(400, {'error': 'That does not look like a GitHub token (ghp_… or github_pat_…).'})
                cfg['githubToken'] = t
        if 'previewLimit' in body:
            v = str(body['previewLimit']).strip().lower()
            if v != 'all' and not (v.isdigit() and 0 < int(v) <= 100000): return self.json(400, {'error': 'Preview limit must be "all" or a positive number.'})
            cfg['previewLimit'] = v
        save_cfg(cfg)
        return self.json(200, public_settings())

    def post_import(self, body):
        url = str(body.get('url') or '').strip(); name = str(body.get('name') or '').strip()[:80]; branch = str(body.get('branch') or '').strip()
        if not GH_RX.match(url): return self.json(400, {'error': 'Paste a GitHub repository link, e.g. https://github.com/owner/repo'})
        if branch and not BRANCH_RX.match(branch): return self.json(400, {'error': 'Invalid branch name.'})
        with LOCK:
            if any(j['state'] == 'running' for j in JOBS.values()): return self.json(409, {'error': 'Another import is still running — wait for it to finish.'})
            jid = format(int(time.time() * 1000), 'x')
            what = ' (all branches)' if branch.lower() == 'all' else (f' @ {branch}' if branch else '')
            JOBS[jid] = {'id': jid, 'url': url, 'branch': branch, 'state': 'running', 'log': [f'Importing {url}{what} …'], 'started': time.time()}
        threading.Thread(target=run_job, args=(jid, url, name, branch), daemon=True).start()
        return self.json(202, {'job': jid})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8790)
    a = ap.parse_args()
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    print(f'Flarebox → http://localhost:{a.port}/  (Ctrl+C to stop) · settings: {CONFIG}', flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == '__main__':
    main()
