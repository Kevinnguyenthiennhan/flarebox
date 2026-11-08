#!/usr/bin/env python3
"""Flarebox local server — serves the library page, runs GitHub imports and keeps importer settings.

  python3 tools/flarebox_server.py [--port 8790]
  → open http://localhost:8790/  ·  Repo scans › "Import from GitHub" › paste a link  ·  Settings tab for the token

POST /api/import {"url": "https://github.com/owner/repo", "name": "optional collection name", "branch": "" | "<name>" | "all"}
     runs tools/flarebox_scan_repo.py <url> --collection [name] in the background → {"job": id}
GET  /api/import/<id>      → {"state": "running|done|error", "log": [...], "collection": {...}}
GET  /api/branches?url=…   → {"default": "main", "branches": [...]} (branch picker)
GET  /api/cache            → git download cache size per repo · POST /api/cache/clear {"repo": dir?}
GET  /api/unity/projects   → project open in Unity, Unity Hub recents, the chosen one · GET /api/unity/folders → Assets/ folders of it
POST /api/groups           {"key": "owner/repo", "name": "Folder name"} → renames a repo folder (vfx/scans/groups.js)
POST /api/unity/export     {"items": [{path, meta, data(base64)}]} → writes the files into the active Unity project's Assets/
GET  /api/unity/find?guids=a,b → where those asset GUIDs already live in the active Unity project
POST /api/source/bundle    {"scan", "prefab"} → the scanned effect's prefab + dependencies with their .meta (base64)
POST /api/unity/ls         {"paths": [...]} → names inside those Assets/ folders (to tell same-named folders apart)
POST /api/unity/probe(-clear) {"paths", "token"} → place / remove a hidden marker to identify a dropped folder exactly
GET  /api/settings         → current settings (the token itself is never sent back, only its last 4 characters)
POST /api/settings         {"githubToken": "..." | null, "previewLimit": "all" | number}
POST /api/settings/test    {"githubToken": "..."?} → checks the given (or saved) token against the GitHub API
GET  /api/ping             → {"ok": true}

Settings live in ~/.flarebox/config.json (mode 600, outside the project so they are never committed).
A token saved there wins over the GITHUB_TOKEN / GH_TOKEN environment variables. Only listens on 127.0.0.1.
"""
import os, re, sys, json, time, shutil, base64, argparse, threading, subprocess, http.server, urllib.request, urllib.error, urllib.parse

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
SCANNER = os.path.join(ROOT, 'tools', 'flarebox_scan_repo.py')
CONFIG = os.path.join(os.path.expanduser('~'), '.flarebox', 'config.json')
CACHE = os.path.join(os.path.expanduser('~'), '.flarebox', 'cache', 'github')
GH_RX = re.compile(r'^(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?(?:/tree/[^\s?#]+)?/?$')
BRANCH_RX = re.compile(r'^(?!-)[A-Za-z0-9._/-]{1,200}$')
LOCAL_RX = re.compile(r'^(localhost|127\.0\.0\.1)(:\d+)?$')
JOBS, LOCK = {}, threading.Lock()
DEFAULTS = {'githubToken': None, 'previewLimit': 'all', 'unityProject': None}  # unityProject None = follow the project open in Unity


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


# ------------------------------------------------------------------ Unity projects (for the export-folder picker)
HUB_PROJECTS = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'UnityHub', 'projects-v1.json')
FOLDER_CACHE = {}  # project -> (time, [folders])

def is_unity_project(path):
    return bool(path) and os.path.isdir(os.path.join(path, 'Assets')) and os.path.isdir(os.path.join(path, 'ProjectSettings'))

def running_projects():
    """Projects open in a running Unity Editor. Only the -projectPath value is kept from the command line
    (it also carries session tokens, which are never stored or returned)."""
    out = []
    try: ps = subprocess.run(['ps', '-axo', 'command'], capture_output=True, text=True, timeout=5).stdout
    except Exception: return out
    for line in ps.splitlines():
        if 'Unity' not in line or '-projectpath' not in line.lower(): continue
        m = re.search(r'-projectpath\s+(.+?)(?=\s+-[A-Za-z]|$)', line, re.I)
        if m:
            path = m.group(1).strip().strip('"')
            if is_unity_project(path) and path not in out: out.append(path)
    return out

def recent_projects():
    try:
        with open(HUB_PROJECTS) as fh: d = json.load(fh)
    except (OSError, ValueError): return []
    rows = []
    for v in (d.get('data', d) or {}).values():
        if isinstance(v, dict) and v.get('path'):
            rows.append({'path': v['path'], 'version': v.get('version'), 'lastModified': v.get('lastModified') or 0, 'ok': is_unity_project(v['path'])})
    return sorted(rows, key=lambda r: -(r['lastModified'] or 0))

def unity_info():
    cfg = load_cfg(); running = running_projects(); chosen = cfg.get('unityProject')
    active = chosen if chosen and is_unity_project(chosen) else (running[0] if running else None)
    return {'active': active, 'mode': 'manual' if chosen else 'auto', 'chosen': chosen, 'chosenMissing': bool(chosen) and not is_unity_project(chosen),
            'running': running, 'recent': recent_projects()}

def unity_folders(project):
    hit = FOLDER_CACHE.get(project)
    if hit and time.time() - hit[0] < 20: return hit[1]
    root = os.path.join(project, 'Assets'); out = ['Assets']
    for dp, dn, _ in os.walk(root):
        dn[:] = sorted(d for d in dn if not d.startswith('.') and not d.endswith('~') and d not in ('CVS',))
        depth = os.path.relpath(dp, root).count(os.sep)
        if depth >= 10: dn[:] = []
        for d in dn: out.append('Assets/' + os.path.relpath(os.path.join(dp, d), root).replace(os.sep, '/'))
        if len(out) > 30000: break
    FOLDER_CACHE[project] = (time.time(), out)
    return out


GUID_CACHE = {}  # project -> (time, {guid: Assets/... path})

def project_guids(project):
    hit = GUID_CACHE.get(project)
    if hit and time.time() - hit[0] < 30: return hit[1]
    out = {}; root = os.path.join(project, 'Assets')
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith('.') and not d.endswith('~')]
        for f in fn:
            if not f.endswith('.meta'): continue
            try:
                with open(os.path.join(dp, f), errors='ignore') as fh:
                    for _ in range(3):
                        line = fh.readline()
                        if line.startswith('guid:'): out[line.split()[1]] = os.path.relpath(os.path.join(dp, f[:-5]), project).replace(os.sep, '/'); break
            except OSError: pass
    GUID_CACHE[project] = (time.time(), out)
    return out

def unity_export(items, on_guid='move', on_name='error'):
    """Write prefab / material / texture (+ their .meta, so GUID links survive) straight into the active Unity project.
    Only inside Assets/; an existing file is replaced only when it carries the same GUID (an earlier export of this effect);
    an earlier export of the same effect in another folder is moved away so Unity never sees a duplicate GUID."""
    info = unity_info(); pr = info['active']
    if not pr: return 400, {'error': 'No Unity project — open one in Unity or choose one in Settings.'}
    root = os.path.realpath(os.path.join(pr, 'Assets')); guids = project_guids(pr); plan = []; skipped = []; renamed = []
    for it in items:
        rel = str(it.get('path') or ''); meta = str(it.get('meta') or '')
        if not rel.startswith('Assets/') or '\\' in rel or any(x in ('', '.', '..') for x in rel.split('/')) or not re.match(r'^[A-Za-z0-9 _\-./()]+$', rel):
            return 400, {'error': f'Refusing unsafe path: {rel}'}
        full = os.path.realpath(os.path.join(pr, rel))
        if not full.startswith(root + os.sep): return 400, {'error': f'Path leaves the Assets folder: {rel}'}
        m = re.search(r'^guid: ([0-9a-f]{32})', meta, re.M)
        if not m: return 400, {'error': f'Missing .meta GUID for {rel}'}
        g = m.group(1)
        old = guids.get(g)
        if on_guid == 'skip' and old:  # this exact asset is already in the project (e.g. a shared texture) → reuse it
            skipped.append({'path': rel, 'existing': old}); continue
        taken = lambda f: os.path.exists(f) and f'guid: {g}' not in (open(f + '.meta', errors='ignore').read() if os.path.exists(f + '.meta') else '')
        if taken(full):
            if on_name != 'rename': return 409, {'error': f'{rel} already exists in the project and is a different asset — pick another folder or rename it in Unity first.'}
            stem, ext = os.path.splitext(rel); n = 1
            while taken(os.path.realpath(os.path.join(pr, f'{stem}_{n}{ext}'))) or any(p[0] == f'{stem}_{n}{ext}' for p in plan): n += 1
            rel = f'{stem}_{n}{ext}'; full = os.path.realpath(os.path.join(pr, rel)); renamed.append(rel)
        try: data = base64.b64decode(it.get('data') or '')
        except Exception: return 400, {'error': f'Bad file data for {rel}'}
        plan.append((rel, full, meta, data, old if old and old != rel and on_guid == 'move' else None))
    written, moved = [], []
    for rel, full, meta, data, old in plan:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as fh: fh.write(data)
        with open(full + '.meta', 'w') as fh: fh.write(meta)
        written.append(rel)
        if old:
            for ext in ('', '.meta'):
                op = os.path.realpath(os.path.join(pr, old)) + ext
                if op.startswith(root + os.sep) and os.path.isfile(op): os.remove(op)
            moved.append({'from': old, 'to': rel})
    GUID_CACHE.pop(pr, None); FOLDER_CACHE.pop(pr, None)
    return 200, {'project': pr, 'name': os.path.basename(pr.rstrip('/')), 'written': written, 'moved': moved, 'skipped': skipped, 'renamed': renamed}


# ------------------------------------------------------------------ original assets of a scanned effect (for export)
SKIP_KINDS = {'script', 'scene', 'builtin', 'unknown'}
KIND_BY_EXT = {'.prefab': 'prefab', '.mat': 'material', '.fbx': 'mesh', '.obj': 'mesh', '.blend': 'mesh', '.dae': 'mesh', '.mesh': 'mesh',
               '.shader': 'shader', '.shadergraph': 'shader', '.shadersubgraph': 'shader', '.cginc': 'shader', '.hlsl': 'shader',
               '.anim': 'animation', '.controller': 'animation', '.overridecontroller': 'animation'}
TEX_EXT = {'.png', '.tga', '.psd', '.jpg', '.jpeg', '.exr', '.tif', '.tiff', '.bmp', '.gif', '.hdr'}

def source_bundle(scan_id, prefab):
    """The effect's prefab + every dependency the scan resolved (status ok), read from where the scan came from
    (local folder, or the GitHub repo through the git cache), each with its original .meta so GUID links are kept."""
    if not re.match(r'^[a-z0-9-]{1,200}$', scan_id or ''): return 400, {'error': 'bad scan id'}
    try: rep = json.load(open(os.path.join(ROOT, 'vfx', 'scans', scan_id + '.json')))
    except (OSError, ValueError): return 404, {'error': 'Scan report not found — import the repository again.'}
    v = next((x for x in rep.get('vfx', []) if x.get('path') == prefab), None)
    if not v: return 404, {'error': 'That prefab is not in the scan report.'}
    want, skipped = [(prefab, 'prefab')], []
    for d in v.get('deps', []):
        pth = d.get('path')
        if not pth: continue
        ext = os.path.splitext(pth)[1].lower()
        kind = KIND_BY_EXT.get(ext) or ('texture' if ext in TEX_EXT else d.get('kind') or 'other')
        if d.get('status') != 'ok' or kind in SKIP_KINDS or ext in ('.cs', '.dll', '.asmdef', '.unity'):
            if d.get('status') == 'ok' and (kind == 'script' or ext in ('.cs', '.dll')): skipped.append({'path': pth, 'why': 'script'})
            continue
        if kind not in ('prefab', 'material', 'texture', 'mesh', 'shader', 'animation'): kind = 'other'
        if pth not in [w[0] for w in want]: want.append((pth, kind))
    rels = [w[0] for w in want] + [w[0] + '.meta' for w in want]
    data = {}
    if rep.get('path') and os.path.isdir(rep['path']):
        for r in rels:
            try: data[r] = open(os.path.join(rep['path'], r), 'rb').read()
            except OSError: data[r] = None
    elif rep.get('url') and rep.get('commit'):
        m = re.match(r'https://github\.com/([^/]+)/([^/]+)', rep['url'])
        if not m: return 400, {'error': 'unsupported source'}
        tok, _ = token_info(load_cfg())
        if tok: os.environ['GITHUB_TOKEN'] = tok
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import flarebox_scan_repo as S
        try:
            src = S.github_source(m.group(1), m.group(2), rep['commit']); src.files()
            if hasattr(src, 'prefetch_bytes'): data = src.prefetch_bytes(rels)
            else: data = {r: src.raw_bytes(r) for r in rels}
        except Exception as e:  # noqa: BLE001
            return 502, {'error': f'Could not read the repository: {e}'}
    else: return 400, {'error': 'The scan does not say where its files are.'}
    items = []
    for pth, kind in want:
        body, meta = data.get(pth), data.get(pth + '.meta')
        if body is None or meta is None: skipped.append({'path': pth, 'why': 'missing in source'}); continue
        meta = meta.decode('utf-8', 'replace') if isinstance(meta, bytes) else meta
        if not re.search(r'^guid: [0-9a-f]{32}', meta, re.M): skipped.append({'path': pth, 'why': 'no .meta GUID'}); continue
        items.append({'name': os.path.basename(pth), 'kind': kind, 'source': pth, 'meta': meta, 'data': base64.b64encode(body if isinstance(body, bytes) else body.encode()).decode()})
    return 200, {'repo': rep.get('repo'), 'commit': rep.get('commit'), 'items': items, 'skipped': skipped}


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
        if self.path == '/api/unity/projects': return self.json(200, unity_info())
        if self.path.startswith('/api/unity/find?'):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); want = [g for g in (q.get('guids') or [''])[0].split(',') if re.match(r'^[0-9a-f]{32}$', g)][:500]
            pr = unity_info()['active']
            if not pr: return self.json(200, {'project': None, 'found': {}})
            GUID_CACHE.pop(pr, None); idx = project_guids(pr)
            return self.json(200, {'project': pr, 'name': os.path.basename(pr.rstrip('/')), 'found': {g: idx[g] for g in want if g in idx}})
        if self.path == '/api/unity/folders':
            info = unity_info(); pr = info['active']
            if not pr: return self.json(200, {'project': None, 'folders': [], 'error': 'No Unity project — open one in Unity or pick one in Settings.'})
            return self.json(200, {'project': pr, 'name': os.path.basename(pr.rstrip('/')), 'mode': info['mode'], 'folders': unity_folders(pr)})
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
        if self.path == '/api/unity/export':
            items = body.get('items')
            if not isinstance(items, list) or not 1 <= len(items) <= 400: return self.json(400, {'error': 'nothing to export'})
            code, out = unity_export(items, 'skip' if body.get('onGuid') == 'skip' else 'move', 'rename' if body.get('onName') == 'rename' else 'error'); return self.json(code, out)
        if self.path in ('/api/unity/probe', '/api/unity/probe-clear'):
            # identify a dropped folder exactly: drop a hidden marker (Unity ignores dot-files) into each same-named candidate,
            # the page checks which one it can see inside the folder it was given, then the markers are removed
            tok = str(body.get('token') or '')
            if not re.match(r'^[a-z0-9]{12,40}$', tok): return self.json(400, {'error': 'bad token'})
            pr = unity_info()['active']
            if not pr: return self.json(200, {'placed': []})
            root = os.path.realpath(os.path.join(pr, 'Assets')); placed = []
            for rel in [str(x) for x in (body.get('paths') or [])][:40]:
                full = os.path.realpath(os.path.join(pr, rel))
                if not (full == root or full.startswith(root + os.sep)) or not os.path.isdir(full): continue
                mk = os.path.join(full, '.flarebox-probe-' + tok)
                try:
                    if self.path.endswith('clear'):
                        if os.path.isfile(mk): os.remove(mk)
                    else:
                        with open(mk, 'w') as fh: fh.write(rel)
                    placed.append(rel)
                except OSError: pass
            return self.json(200, {'placed': placed})
        if self.path == '/api/unity/ls':  # names inside a few project folders (to tell same-named folders apart)
            pr = unity_info()['active']; out = {}
            if not pr: return self.json(200, {'entries': {}})
            root = os.path.realpath(os.path.join(pr, 'Assets'))
            for rel in [str(x) for x in (body.get('paths') or [])][:40]:
                full = os.path.realpath(os.path.join(pr, rel))
                if not (full == root or full.startswith(root + os.sep)) or not os.path.isdir(full): continue
                try: out[rel] = sorted(n for n in os.listdir(full) if not n.endswith('.meta') and not n.startswith('.'))[:2000]
                except OSError: pass
            return self.json(200, {'entries': out})
        if self.path == '/api/source/bundle':
            code, out = source_bundle(str(body.get('scan') or ''), str(body.get('prefab') or '')); return self.json(code, out)
        if self.path == '/api/groups':
            key = str(body.get('key') or '').strip(); name = str(body.get('name') or '').strip()[:60]
            if not re.match(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', key): return self.json(400, {'error': 'bad folder key'})
            path = os.path.join(ROOT, 'vfx', 'scans', 'groups.js'); groups = {}
            try:
                m = re.search(r'=\s*(\{.*\})\s*;?\s*$', open(path).read(), re.S); groups = json.loads(m.group(1)) if m else {}
            except (OSError, ValueError): groups = {}
            if name: groups[key] = name
            else: groups.pop(key, None)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, 'w').write('/* Folder names for repositories with several scans (one per branch). Written by the Flarebox page (Rename folder). */\n'
                                  'window.FLAREBOX_GROUPS=%s;\n' % json.dumps(groups, ensure_ascii=False))
            return self.json(200, {'groups': groups})
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
        if 'unityProject' in body:
            v = body['unityProject']
            if v in (None, ''): cfg['unityProject'] = None
            else:
                v = os.path.abspath(os.path.expanduser(str(v).strip()))
                if not is_unity_project(v): return self.json(400, {'error': 'That folder is not a Unity project (it needs Assets/ and ProjectSettings/).'})
                cfg['unityProject'] = v; FOLDER_CACHE.pop(v, None)
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
