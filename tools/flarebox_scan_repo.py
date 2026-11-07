#!/usr/bin/env python3
"""Flarebox repository scanner — finds, classifies and audits every Unity VFX in a repository.

  python3 tools/flarebox_scan_repo.py <github-url | git-url | local-folder> [--branch BR] [--out vfx/scans]

• GitHub URLs are read through the GitHub API (tree listing + raw files): only YAML/.meta/shader text is
  downloaded, textures/meshes are never fetched (their presence is known from the tree). Set GITHUB_TOKEN for
  private repos / higher rate limits. Other git URLs are shallow-cloned (LFS objects skipped). Local folders are read in place.
• Detection reads asset CONTENT, not names: ParticleSystem / VisualEffect (VFX Graph) / TrailRenderer / LineRenderer
  components, MeshRenderers whose materials use particle/additive/scrolling shaders, Animator/Animation on those
  meshes, nested prefab instances (a prefab is VFX if any nested prefab is), standalone .vfx graphs.
• Every reference {fileID, guid} is resolved through the .meta GUID index; dependencies are traced recursively
  (nested prefabs, materials, textures, shaders, meshes, animations/controllers, scripts, VFX graphs) and
  classified ok / missing / package / builtin. Also reports assets without .meta and .meta without asset.
Output: <out>/<slug>.json and <out>/<slug>.js (FLAREBOX_SCANS) + <out>/index.js for the "Repo scans" tab.
"""
import sys, os, re, json, time, base64, argparse, subprocess, tempfile, shutil, urllib.request, urllib.parse, concurrent.futures as cf
from collections import Counter, defaultdict

TEXT_EXT = {'.prefab', '.mat', '.controller', '.overridecontroller', '.vfx', '.vfxoperator', '.vfxblock', '.shadergraph', '.shadersubgraph', '.shader', '.asset', '.anim', '.unity', '.physicmaterial'}
READ_EXT = {'.prefab', '.mat', '.controller', '.overridecontroller', '.vfx', '.shadergraph', '.shadersubgraph', '.shader', '.asset'}
KIND = {'.prefab': 'prefab', '.mat': 'material', '.shader': 'shader', '.shadergraph': 'shader', '.shadersubgraph': 'shader', '.compute': 'shader', '.cginc': 'shader', '.hlsl': 'shader',
        '.png': 'texture', '.tga': 'texture', '.psd': 'texture', '.jpg': 'texture', '.jpeg': 'texture', '.exr': 'texture', '.tif': 'texture', '.tiff': 'texture', '.bmp': 'texture', '.gif': 'texture', '.hdr': 'texture', '.renderTexture': 'texture',
        '.fbx': 'mesh', '.obj': 'mesh', '.blend': 'mesh', '.dae': 'mesh', '.3ds': 'mesh', '.max': 'mesh', '.mesh': 'mesh',
        '.anim': 'animation', '.controller': 'animation', '.overridecontroller': 'animation', '.playable': 'animation',
        '.cs': 'script', '.vfx': 'vfx graph', '.vfxoperator': 'vfx graph', '.vfxblock': 'vfx graph', '.asset': 'asset', '.unity': 'scene', '.wav': 'audio', '.ogg': 'audio', '.mp3': 'audio'}
BUILTIN = {'0000000000000000e000000000000000': 'Unity built-in resources (meshes)', '0000000000000000f000000000000000': 'Unity built-in extra (shaders/textures)', '0000000000000000d000000000000000': 'Unity default resources'}
# class IDs
C_GO, C_PS, C_PSR, C_MR, C_SMR, C_TRAIL, C_LINE, C_ANIMATOR, C_ANIM, C_PI, C_VE, C_MONO, C_LIGHT, C_AUDIO = 1, 198, 199, 23, 137, 96, 120, 95, 111, 1001, 2083, 114, 108, 82
BUILTIN_PARTICLE_SHADERS = {200, 201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 10720, 10721, 10722, 10723}
CAT_RULES = [(r'muzzle', 'Muzzle Flash'), (r'slash|swing|sword|claw|\bcut', 'Slash'), (r'explo|blast|nuke|bomb|boom', 'Explosion'),
             (r'snow|blizzard|rain|waterfall|leaf|leaves|butterfl|weather|firefl|ambient', 'Environment'),
             (r'fire|flame|burn|torch|breath|ember', 'Fire'), (r'smoke|fog|steam|cloud|puff', 'Smoke'), (r'water|splash|wave|bubble|drop', 'Water'),
             (r'lightn|thunder|electr|spark|zap|volt|shock', 'Lightning'), (r'heal|buff|aura|shield|level ?up|levelup', 'Magic'),
             (r'magic|spell|arcane|portal|rune|cast|summon|teleport', 'Magic'), (r'hit|impact|crit|punch|strike', 'Impact'),
             (r'dust|debris|dirt|sand|rock', 'Dust'), (r'blood|gore', 'Blood'), (r'death|soul|ghost|spirit', 'Death & Souls'),
             (r'projectile|missile|bullet|arrow|beam|laser|trail', 'Projectile'), (r'coin|ui|reward|star|sparkle|glow|click|button|confetti', 'UI Effects')]
HDR_RX = re.compile(r'^--- !u!(\d+) &(-?\d+)( stripped)?', re.M)
REF_RX = re.compile(r'\{fileID: (-?\d+), guid: ([0-9a-f]{32}), type: (\d)\}')
GUID_RX = re.compile(r'"?guid"?\s*[:=]\s*"?([0-9a-f]{32})')

def log(*a): print(*a, file=sys.stderr, flush=True)

# ------------------------------------------------------------------ sources (local folder / git clone / GitHub API)
class LocalSource:
    def __init__(self, root, label=None): self.root = os.path.abspath(root); self.label = label or os.path.basename(self.root); self.meta = {}
    def files(self):
        out = {}
        for dp, dn, fn in os.walk(self.root):
            dn[:] = [d for d in dn if d not in ('.git', 'Library', 'Temp', 'Obj', 'obj', 'Logs', 'UserSettings', 'Build', 'Builds', 'node_modules')]
            for f in fn:
                full = os.path.join(dp, f); rel = os.path.relpath(full, self.root).replace(os.sep, '/')
                try: out[rel] = os.path.getsize(full)
                except OSError: pass
        return out
    def read(self, rel):
        try:
            with open(os.path.join(self.root, rel), 'rb') as fh: b = fh.read()
            return b.decode('utf-8', 'ignore')
        except OSError: return None
    def prefetch(self, rels): pass

class GitHubSource:
    BLOBS = {}  # blob sha → text, shared by every branch of one run (files that are identical across branches are downloaded once)
    def __init__(self, owner, repo, branch=None):
        self.owner, self.repo = owner, repo; self.tok = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN'); self.cache = {}; self.shas = {}
        info = self.api(f'/repos/{owner}/{repo}')
        self.branch = branch or info['default_branch']
        self.commit = self.api(f'/repos/{owner}/{repo}/commits/{urllib.parse.quote(self.branch)}')['sha']
        self.label = f'{owner}/{repo}'; self.meta = {'url': f'https://github.com/{owner}/{repo}', 'branch': self.branch, 'commit': self.commit, 'private': info.get('private', False)}
    def req(self, url, accept='application/vnd.github+json'):
        h = {'Accept': accept, 'User-Agent': 'flarebox-scanner'}
        if self.tok: h['Authorization'] = 'Bearer ' + self.tok
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r: return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (403, 429) and attempt < 3: time.sleep(5 * (attempt + 1)); continue
                raise
            except urllib.error.URLError:
                if attempt < 3: time.sleep(2); continue
                raise
    def api(self, path): return json.loads(self.req('https://api.github.com' + path))
    def files(self):
        t = self.api(f'/repos/{self.owner}/{self.repo}/git/trees/{self.commit}?recursive=1')
        if t.get('truncated'): log('! GitHub tree listing truncated (very large repo) — consider a local clone')
        self.shas = {e['path']: e.get('sha') for e in t['tree'] if e['type'] == 'blob'}
        return {e['path']: e.get('size', 0) for e in t['tree'] if e['type'] == 'blob'}
    def branches(self):
        out, page = [], 1
        while True:
            b = self.api(f'/repos/{self.owner}/{self.repo}/branches?per_page=100&page={page}')
            out += [x['name'] for x in b]
            if len(b) < 100: return out
            page += 1
    def _raw(self, rel):
        sha = self.shas.get(rel)
        if sha and sha in GitHubSource.BLOBS: return GitHubSource.BLOBS[sha]
        txt = self._raw_fetch(rel)
        if sha and txt is not None: GitHubSource.BLOBS[sha] = txt
        return txt
    def _raw_fetch(self, rel):
        url = f'https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.commit}/' + urllib.parse.quote(rel)
        try: return self.req(url, accept='*/*').decode('utf-8', 'ignore')
        except Exception as e: log('  ! fetch failed', rel, e); return None
    def raw_bytes(self, rel):
        base = f'{self.owner}/{self.repo}/{self.commit}/' + urllib.parse.quote(rel)
        try:
            b = self.req('https://raw.githubusercontent.com/' + base, accept='*/*')
            if b[:40].startswith(b'version https://git-lfs'):
                b = self.req('https://media.githubusercontent.com/media/' + base, accept='*/*')
            return b
        except Exception as e: log('  ! fetch failed', rel, e); return None
    def prefetch(self, rels):
        rels = [r for r in rels if r not in self.cache]
        with cf.ThreadPoolExecutor(16) as ex:
            for i, (r, txt) in enumerate(zip(rels, ex.map(self._raw, rels))):
                self.cache[r] = txt
                if i % 200 == 199: log(f'  fetched {i + 1}/{len(rels)}')
    def read(self, rel):
        if rel not in self.cache: self.cache[rel] = self._raw(rel)
        return self.cache[rel]

class GitError(RuntimeError): pass

CACHE_ROOT = os.path.join(os.path.expanduser('~'), '.flarebox', 'cache', 'github')

class GitSource(GitHubSource):
    """GitHub repository read through a shared partial (blob:none) + shallow git cache in ~/.flarebox/cache.
    The file tree arrives in one fetch, file contents in a few batched fetches (instead of one HTTP request per file),
    and every downloaded blob stays cached: re-imports and other branches only download what changed."""
    def __init__(self, owner, repo, branch=None):
        super().__init__(owner, repo, branch)  # GitHub API: repo info, default branch, commit sha (clean errors for 404 / 409)
        self.dir = os.path.join(CACHE_ROOT, f'{owner}__{repo}.git'.lower())
        self.env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'}
        if self.tok:  # token goes in an http header through the environment — never in the URL or the process list
            self.env.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                             'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + base64.b64encode(f'x-access-token:{self.tok}'.encode()).decode()})
        if not os.path.isdir(os.path.join(self.dir, 'objects')):
            os.makedirs(self.dir, exist_ok=True)
            self.git('init', '-q', '--bare')
            self.git('remote', 'add', 'origin', f'https://github.com/{owner}/{repo}.git')
            for k, v in (('core.repositoryformatversion', '1'), ('extensions.partialClone', 'origin'), ('remote.origin.promisor', 'true'),
                         ('remote.origin.partialclonefilter', 'blob:none'), ('gc.auto', '0')):
                self.git('config', k, v)
        t0 = time.time()
        ref = 'refs/fb/' + re.sub(r'[^A-Za-z0-9._/-]', '_', self.branch)
        self.git('fetch', '-q', '--depth', '1', '--filter=blob:none', '--no-tags', 'origin', f'+{self.commit}:{ref}')
        log(f'git: {self.label} @ {self.branch} ({self.commit[:8]}) file tree fetched in {time.time() - t0:.1f}s')
        self._missing = None
    def git(self, *a, input=None, binary=False):
        p = subprocess.run(['git', '-C', self.dir, *a], input=input, capture_output=True, env=self.env)
        if p.returncode: raise GitError(((p.stderr or b'').decode('utf-8', 'replace').strip().splitlines() or [f'git {a[0]} failed'])[-1])
        return p.stdout if binary else p.stdout.decode('utf-8', 'replace')
    def files(self):
        # sizes + blob shas come from one GitHub API tree call; `git ls-tree -l` would download every blob just to size it
        try:
            t = self.api(f'/repos/{self.owner}/{self.repo}/git/trees/{self.commit}?recursive=1')
            if not t.get('truncated'):
                blobs = [e for e in t['tree'] if e['type'] == 'blob']
                self.shas = {e['path']: e['sha'] for e in blobs}
                return {e['path']: e.get('size', 0) for e in blobs}
            log('! GitHub tree listing truncated (very large repo) — listing files with git (sizes unknown)')
        except urllib.error.HTTPError as e: log(f'! tree listing via API failed ({e.code}) — listing files with git')
        out = self.git('ls-tree', '-r', '-z', '--full-tree', self.commit, binary=True); files = {}; self.shas = {}
        for rec in out.split(b'\0'):
            if not rec or b'\t' not in rec: continue
            meta, path = rec.split(b'\t', 1); parts = meta.split()
            if len(parts) < 3 or parts[1] != b'blob': continue
            pth = path.decode('utf-8', 'replace'); self.shas[pth] = parts[2].decode(); files[pth] = 0
        return files
    def missing(self):
        if self._missing is None:
            out = self.git('rev-list', '--objects', '--missing=print', self.commit)
            self._missing = {l[1:].strip() for l in out.splitlines() if l.startswith('?')}
        return self._missing
    def fetch_blobs(self, shas):
        need = sorted(set(shas) & self.missing())
        for i in range(0, len(need), 4000):
            chunk = need[i:i + 4000]
            self.git('-c', 'fetch.negotiationAlgorithm=noop', 'fetch', '-q', '--no-tags', '--no-write-fetch-head', '--recurse-submodules=no',
                     '--filter=blob:none', 'origin', '--stdin', input=('\n'.join(chunk) + '\n').encode())
            self._missing -= set(chunk)
        return len(need)
    def cat(self, shas):
        """sha -> bytes through one `git cat-file --batch` process (objects must already be local)."""
        shas = list(dict.fromkeys(x for x in shas if x)); out = {}
        if not shas: return out
        data = self.git('cat-file', '--batch', input=('\n'.join(shas) + '\n').encode(), binary=True); pos = 0
        while pos < len(data):
            nl = data.index(b'\n', pos); hdr = data[pos:nl].split(); pos = nl + 1
            if len(hdr) < 3: continue
            n = int(hdr[2]); out[hdr[0].decode()] = data[pos:pos + n]; pos += n + 1
        return out
    def prefetch(self, rels):
        rels = [r for r in dict.fromkeys(rels) if r not in self.cache and r in self.shas]
        if not rels: return
        t0 = time.time(); n = self.fetch_blobs(self.shas[r] for r in rels); blobs = self.cat(self.shas[r] for r in rels)
        for r in rels:
            b = blobs.get(self.shas[r]); self.cache[r] = b.decode('utf-8', 'ignore') if b is not None else None
        log(f'git: {len(rels)} files ready in {time.time() - t0:.1f}s ({n} downloaded, {len(rels) - n} already in the cache)')
    def read(self, rel):
        if rel not in self.cache: self.prefetch([rel])
        return self.cache.get(rel)
    def prefetch_bytes(self, rels):
        """binary files (textures / meshes) for the preview step — one batched fetch; Git LFS pointers are resolved over HTTPS."""
        rels = [r for r in dict.fromkeys(rels) if r in self.shas]
        self.fetch_blobs(self.shas[r] for r in rels); blobs = self.cat(self.shas[r] for r in rels); out, lfs = {}, []
        for r in rels:
            b = blobs.get(self.shas[r])
            if b is not None and b[:40].startswith(b'version https://git-lfs'): lfs.append(r)
            else: out[r] = b
        if lfs:
            log(f'  {len(lfs)} files are in Git LFS — downloading them over HTTPS …')
            with cf.ThreadPoolExecutor(12) as ex: out.update(zip(lfs, ex.map(self.raw_bytes, lfs)))
        return out

def github_source(owner, repo, branch=None):
    """git transport (fast, cached) when git is available; falls back to per-file GitHub API/raw downloads."""
    if os.environ.get('FLAREBOX_TRANSPORT', 'git') == 'git' and shutil.which('git'):
        try: return GitSource(owner, repo, branch)
        except GitError as e: log(f'! git transport failed ({e}) — falling back to per-file downloads')
    return GitHubSource(owner, repo, branch)

def open_source(target, branch):
    m = re.match(r'^(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/#?]+?)(?:\.git)?(?:/tree/([^?#]+))?/?(?:[?#].*)?$', target)
    if m: return github_source(m.group(1), m.group(2), branch or (m.group(3) or None)), None
    if os.path.isdir(target):
        src = LocalSource(target)
        try: src.meta = {'path': os.path.abspath(target), 'commit': subprocess.run(['git', '-C', target, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip() or None}
        except Exception: src.meta = {'path': os.path.abspath(target)}
        return src, None
    tmp = tempfile.mkdtemp(prefix='flarebox-scan-')
    cmd = ['git', 'clone', '--depth', '1'] + (['--branch', branch] if branch else []) + [target, tmp]
    log('cloning', target, '…')
    subprocess.run(cmd, check=True, env={**os.environ, 'GIT_LFS_SKIP_SMUDGE': '1', 'GIT_TERMINAL_PROMPT': '0'})
    src = LocalSource(tmp, re.sub(r'\.git$', '', target.rstrip('/').split('/')[-1]))
    src.meta = {'url': target, 'branch': branch, 'commit': subprocess.run(['git', '-C', tmp, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()}
    return src, tmp

# ------------------------------------------------------------------ scan
def parse_docs(txt):
    """-> list of (classID, fileID, stripped, body)"""
    ms = list(HDR_RX.finditer(txt)); out = []
    for i, m in enumerate(ms):
        out.append((int(m.group(1)), int(m.group(2)), bool(m.group(3)), txt[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(txt)]))
    return out

def field(body, name):
    m = re.search(r'^\s*' + re.escape(name) + r': (.*)$', body, re.M); return m.group(1).strip() if m else None

def scan(src, args):
    t0 = time.time()
    files = src.files()
    log(f'{len(files)} files in {src.label}')
    unity_root = ''  # repos may keep the Unity project in a sub-folder
    pv = [p for p in files if p.endswith('ProjectSettings/ProjectVersion.txt')]
    if pv: unity_root = pv[0][:-len('ProjectSettings/ProjectVersion.txt')]
    metas = [p for p in files if p.endswith('.meta')]
    assets = set(p for p in files if not p.endswith('.meta'))
    readable = [p for p in files if os.path.splitext(p)[1].lower() in READ_EXT and files[p] < args.max_text_mb * 1e6]
    extra = [x for x in (pv[:1] + [unity_root + 'Packages/manifest.json']) if x in files]
    log(f'reading {len(metas)} .meta + {len(readable)} asset files …')
    src.prefetch(metas + readable + extra)
    # GUID index
    guid2path, path2guid, dup = {}, {}, defaultdict(list)
    for mp in metas:
        txt = src.read(mp) or ''
        m = re.search(r'^guid: ([0-9a-f]{32})', txt, re.M)
        if not m: continue
        g, ap = m.group(1), mp[:-5]
        if g in guid2path: dup[g].append(ap)
        guid2path[g] = ap; path2guid[ap] = g
    folders = set(os.path.dirname(p) for p in files)
    no_meta = sorted(p for p in assets if '/Assets/' in '/' + p and (p + '.meta') not in files and not os.path.basename(p).startswith('.'))
    no_asset = sorted(p[:-5] for p in metas if p[:-5] not in assets and p[:-5] not in folders)
    unity_version = (re.search(r'm_EditorVersion: (\S+)', src.read(pv[0]) or '') or [None, None])[1] if pv else None
    packages = {}
    if extra and extra[-1].endswith('manifest.json'):
        try: packages = json.loads(src.read(extra[-1]) or '{}').get('dependencies', {})
        except Exception: pass
    pipeline = 'URP' if any('render-pipelines.universal' in k for k in packages) else 'HDRP' if any('render-pipelines.high-definition' in k for k in packages) else 'Built-in'
    has_vfxgraph_pkg = any('visualeffectgraph' in k for k in packages)

    def kind_of(path): return KIND.get(os.path.splitext(path)[1].lower(), 'asset' if path else 'unknown')
    def status_of(guid):
        if guid in BUILTIN: return 'builtin'
        if guid in guid2path: return 'ok'
        return 'missing'
    text = {p: (src.read(p) or '') for p in readable}
    # Unity "Force Binary" / "Mixed" serialization → assets are not YAML and cannot be read as text
    binary = sorted(p for p, t in text.items() if t and os.path.splitext(p)[1].lower() in ('.prefab', '.mat', '.controller', '.asset') and not t.lstrip().startswith('%YAML') and ('\x00' in t[:400] or not t.startswith('%')))
    for p in binary: text[p] = ''
    if binary: log(f'! {len(binary)} assets are binary-serialized (Project Settings › Editor › Asset Serialization = Binary/Mixed) and cannot be analysed')

    # ---- shader / material VFX-ness
    shader_vfx = {}
    def shader_is_vfx(path):
        if path in shader_vfx: return shader_vfx[path]
        t = text.get(path) or ''
        v = bool(re.search(r'Blend\s+(SrcAlpha\s+One\b|One\s+One\b)|Shader\s+"[^"]*(Particle|VFX|FX/|Additive)', t, re.I))
        shader_vfx[path] = v; return v
    mat_info = {}
    def material(path):
        if path in mat_info: return mat_info[path]
        t = text.get(path) or ''
        m = REF_RX.search(field(t, 'm_Shader') or '') if t else None
        sh_guid, sh_fid = (m.group(2), int(m.group(1))) if m else (None, None)
        src_b = field(t, '- _SrcBlend') or ''; dst_b = field(t, '- _DstBlend') or ''
        additive = dst_b.strip() == '1'
        transparent = bool(re.search(r'_SURFACE_TYPE_TRANSPARENT|RenderType: Transparent|_ALPHABLEND_ON', t)) or dst_b.strip() in ('1', '10')
        sh_path = guid2path.get(sh_guid)
        shader_name = sh_path or (f'builtin #{sh_fid}' if sh_guid in BUILTIN else (sh_guid or '?'))
        is_particle = (sh_guid in BUILTIN and sh_fid in BUILTIN_PARTICLE_SHADERS) or bool(sh_path and re.search(r'particle', sh_path, re.I))
        fx_name = bool(sh_path and re.search(r'particle|vfx|(^|/)fx|_fx|additive|scroll|flow|distort|dissolve|beam|glow|laser|trail|aura', sh_path, re.I))
        fx_props = bool(re.search(r'- _?(MainTexSpeed|ScrollSpeed|Flow|Distortion|DistortTex|UVPan|Panner)\w*:', t))
        vfxish = is_particle or additive or bool(sh_path and shader_is_vfx(sh_path)) or (transparent and (fx_name or fx_props))
        mat_info[path] = r = {'shader': shader_name, 'vfx': vfxish, 'additive': additive, 'transparent': transparent}
        return r

    # ---- prefab analysis (with nested prefabs)
    pinfo = {}
    def prefab(path, stack=()):
        if path in pinfo: return pinfo[path]
        if path in stack: return {'c': Counter(), 'mats': [], 'nested': [], 'vfx_nested': False, 'scripts': []}
        t = text.get(path) or ''
        docs = parse_docs(t)
        c = Counter(); mats = []; nested = []; scripts = []; names = {}
        for cid, fid, stripped, body in docs:
            if stripped: continue
            c[cid] += 1
            if cid == C_GO: names[fid] = (field(body, 'm_Name') or '').strip()
            if cid in (C_MR, C_SMR, C_PSR, C_TRAIL, C_LINE):
                ms = re.search(r'm_Materials:\n((?:\s*- \{.*\}\n)+)', body)
                if ms:
                    for r in REF_RX.finditer(ms.group(1)):
                        mp = guid2path.get(r.group(2))
                        if mp and mp.endswith('.mat'): mats.append((cid, mp))
            if cid == C_PI:
                m = REF_RX.search(field(body, 'm_SourcePrefab') or '')
                if m and m.group(2) in guid2path: nested.append(guid2path[m.group(2)])
            if cid == C_MONO:
                m = REF_RX.search(field(body, 'm_Script') or '')
                if m: scripts.append(m.group(2))
        info = {'c': c, 'mats': mats, 'nested': nested, 'scripts': scripts, 'root': next(iter(names.values()), os.path.splitext(os.path.basename(path))[0])}
        pinfo[path] = info
        agg = Counter(c); amats = list(mats)
        for n in nested:
            if n.endswith('.prefab'):
                sub = prefab(n, stack + (path,)); agg.update(sub.get('agg', Counter())); amats += sub.get('amats', [])
        info['agg'] = agg; info['amats'] = amats
        return info

    def classify(path):
        info = prefab(path); a = info['agg']; c = info['c']
        mats = [material(m) for _, m in info['amats']]
        mr_mats = [material(m) for cid, m in info['amats'] if cid in (C_MR, C_SMR)]
        signals = []
        ps, ve, tr, ln = a[C_PS], a[C_VE], a[C_TRAIL], a[C_LINE]
        mr, smr, anim = a[C_MR], a[C_SMR], a[C_ANIMATOR] + a[C_ANIM]
        if ps: signals.append(f'{ps} Particle System' + ('s' if ps > 1 else ''))
        if ve: signals.append(f'{ve} Visual Effect (VFX Graph)')
        if tr: signals.append(f'{tr} Trail Renderer')
        if ln: signals.append(f'{ln} Line Renderer')
        vfx_mr = sum(1 for m in mr_mats if m['vfx'])
        if vfx_mr: signals.append(f'{vfx_mr} mesh renderer(s) with VFX materials')
        if anim: signals.append('animated (Animator/Animation)')
        if any(m['additive'] for m in mats): signals.append('additive blending')
        opaque_mr = len(mr_mats) - vfx_mr
        if not (ps or ve or tr or ln or vfx_mr): return None
        # environment / props whose only "VFX" is a few transparent meshes (water, glass…) are not VFX
        if not (ps or ve) and opaque_mr > 3 * max(1, vfx_mr): return None
        if ve and not ps: typ = 'VFX Graph'
        elif ps: typ = 'Particle System' + (' + VFX Graph' if ve else '')
        elif vfx_mr: typ = 'Mesh VFX' + (' (animated)' if anim else '')
        else: typ = 'Trail / Line'
        conf = 'high'
        heavy = smr > 0 and opaque_mr > 0 or opaque_mr > max(2, (ps + vfx_mr))
        if heavy: typ = 'Mixed (object with VFX)'; conf = 'medium'; signals.append(f'{opaque_mr} opaque mesh renderer(s) — likely a character/prop carrying VFX')
        elif typ.startswith('Mesh VFX') and not anim and vfx_mr == 1 and len(mr_mats) == 1: conf = 'medium'
        return typ, conf, signals

    # ---- shader property names (to tell live material references from stale leftovers)
    sprops = {}
    def shader_props(path):
        if path in sprops: return sprops[path]
        t = text.get(path)
        if t is None: sprops[path] = None; return None
        if path.endswith('.shader'):
            blk = re.search(r'Properties\s*\{(.*?)\n\s*\}', t, re.S)
            names = set(re.findall(r'^\s*(?:\[[^\]]*\]\s*)*([_A-Za-z][_A-Za-z0-9]*)\s*\(', blk.group(1), re.M)) if blk else set()
        else:
            names = set(re.findall(r'"m_(?:Override|Default)ReferenceName"\s*:\s*"([^"]+)"', t))
        sprops[path] = names or None; return sprops[path]
    def stale_tex(mat_path, guid):
        t = text.get(mat_path) or ''
        m = re.search(r'- ([^\n:]+):\n\s+m_Texture: \{fileID: -?\d+, guid: ' + guid, t)
        if not m: return None
        sh = REF_RX.search(field(t, 'm_Shader') or '')
        props = shader_props(guid2path.get(sh.group(2))) if sh and sh.group(2) in guid2path else None
        return m.group(1).strip() if props is not None and m.group(1).strip() not in props else None

    # ---- dependency tracing
    def refs_of(path):
        t = text.get(path)
        if t is None: return []
        out = []
        for r in REF_RX.finditer(t): out.append((r.group(2), int(r.group(1))))
        if path.endswith(('.shadergraph', '.shadersubgraph', '.vfx')):
            out += [(g, 0) for g in GUID_RX.findall(t)]
        return out
    def deps(root):
        seen, out, q = {}, [], [(root, None)]
        while q:
            p, via = q.pop(0)
            for g, fid in refs_of(p):
                if g == path2guid.get(p): continue
                key = g
                if key in seen: continue
                st = status_of(g); dp = guid2path.get(g)
                d = {'guid': g, 'path': dp, 'kind': kind_of(dp) if dp else ('builtin' if st == 'builtin' else 'unknown'), 'status': st, 'via': p}
                if st == 'builtin': d['note'] = BUILTIN[g]
                if st == 'ok' and dp not in files and dp not in folders: d['status'] = 'missing'; d['note'] = '.meta exists but the asset file is missing'
                if st == 'ok' and dp in files and files[dp] < 200 and kind_of(dp) in ('texture', 'mesh'):
                    head = (src.read(dp) or '')[:60] if isinstance(src, LocalSource) else ''
                    if head.startswith('version https://git-lfs'): d['note'] = 'Git LFS pointer (binary not downloaded)'
                seen[key] = d; out.append(d)
                if dp and os.path.splitext(dp)[1].lower() in READ_EXT and dp != root: q.append((dp, p))
                if st == 'missing' and p.endswith('.mat') and re.search(r'm_Texture: \{fileID: -?\d+, guid: ' + g, text.get(p) or ''): d['kind'] = 'texture'
                if st == 'missing' and p.endswith('.prefab') and re.search(r'm_SourcePrefab: \{fileID: -?\d+, guid: ' + g, text.get(p) or ''): d['kind'] = 'prefab'
                if st == 'missing' and re.search(r'm_Mesh: \{fileID: -?\d+, guid: ' + g, text.get(p) or ''): d['kind'] = 'mesh'
                if st == 'missing' and re.search(r'm_Materials:[^\n]*\n(?:\s*- \{[^\n]*\n)*?\s*- \{fileID: -?\d+, guid: ' + g, text.get(p) or ''): d['kind'] = 'material'
        # unresolved script/shader GUIDs usually come from Unity packages (Packages/ not in repo)
        for d in out:
            if d['status'] == 'missing' and not d['path']:
                refs = text.get(d['via']) or ''
                prop = stale_tex(d['via'], d['guid']) if d['via'].endswith('.mat') else None
                if prop: d['status'] = 'stale'; d['kind'] = 'texture'; d['note'] = f'material property {prop} is not used by its shader (leftover reference)'
                elif re.search(r'm_Script: \{fileID: -?\d+, guid: ' + d['guid'], refs): d['status'] = 'package'; d['kind'] = 'script'; d['note'] = 'script GUID not in repo — likely from a Unity package'
                elif d['via'].endswith(('.vfx', '.shadergraph', '.shadersubgraph', '.vfxoperator', '.vfxblock')) and packages:
                    d['status'] = 'unresolved'; d['note'] = 'referenced inside a graph asset but not in the repo — probably a resource from a Unity package'
                elif re.search(r'm_Shader: \{fileID: -?\d+, guid: ' + d['guid'], refs) and packages: d['status'] = 'package'; d['kind'] = 'shader'; d['note'] = 'shader GUID not in repo — likely from a render-pipeline package'
        return out

    def category(name, path, dep_list):
        hay = (name + ' ' + path + ' ' + ' '.join(os.path.basename(d['path'] or '') for d in dep_list if d['kind'] in ('texture', 'material'))).lower()
        for rx, cat in CAT_RULES:
            if re.search(rx, hay): return cat
        return 'Other'

    vfx = []
    prefabs = sorted(p for p in readable if p.endswith('.prefab'))
    log(f'analysing {len(prefabs)} prefabs …')
    for p in prefabs:
        r = classify(p)
        if not r: continue
        typ, conf, signals = r
        info = pinfo[p]
        dl = deps(p)
        name = os.path.splitext(os.path.basename(p))[0]
        cnt = Counter(d['kind'] for d in dl)
        vfx.append({'name': name, 'path': p, 'guid': path2guid.get(p), 'type': typ, 'confidence': conf, 'category': category(name, p, dl), 'signals': signals,
                    'counts': {'particleSystems': info['agg'][C_PS], 'vfxGraphs': info['agg'][C_VE], 'meshRenderers': info['agg'][C_MR], 'skinnedMeshes': info['agg'][C_SMR], 'trails': info['agg'][C_TRAIL], 'lines': info['agg'][C_LINE], 'animators': info['agg'][C_ANIMATOR] + info['agg'][C_ANIM]},
                    'depCounts': dict(cnt), 'missing': sum(1 for d in dl if d['status'] == 'missing'), 'deps': dl,
                    'variantOf': guid2path.get(next((m.group(2) for m in [REF_RX.search(field(text[p], 'm_SourcePrefab') or '')] if m), None)) if info['c'][C_PI] == 1 and info['c'][C_GO] == 0 else None})
    # standalone VFX graphs not used by any VFX prefab
    used = set(d['path'] for v in vfx for d in v['deps'])
    for p in sorted(f for f in readable if f.endswith('.vfx')):
        if p in used: continue
        dl = deps(p); name = os.path.splitext(os.path.basename(p))[0]
        vfx.append({'name': name, 'path': p, 'guid': path2guid.get(p), 'type': 'VFX Graph asset', 'confidence': 'high', 'category': category(name, p, dl), 'signals': ['Visual Effect Graph asset (not placed in a prefab)'],
                    'counts': {}, 'depCounts': dict(Counter(d['kind'] for d in dl)), 'missing': sum(1 for d in dl if d['status'] == 'missing'), 'deps': dl})
    # global missing list
    miss = defaultdict(lambda: {'refs': set()})
    for v in vfx:
        for d in v['deps']:
            if d['status'] == 'missing': m = miss[d['guid']]; m.update({k: d[k] for k in ('guid', 'path', 'kind') if True}); m['note'] = d.get('note'); m['refs'].add(v['path'])
    missing = [{**{k: v for k, v in m.items() if k != 'refs'}, 'referencedBy': sorted(m['refs'])} for m in miss.values()]
    stats = {'files': len(files), 'metas': len(metas), 'prefabs': len(prefabs), 'vfx': len(vfx), 'byType': dict(Counter(v['type'] for v in vfx)), 'byCategory': dict(Counter(v['category'] for v in vfx)),
             'binaryAssets': len(binary), 'binaryPrefabs': sum(1 for p in binary if p.endswith('.prefab')),
             'missingDeps': len(missing), 'staleRefs': sum(1 for v in vfx for d in v['deps'] if d['status'] == 'stale'), 'vfxWithMissing': sum(1 for v in vfx if v['missing']), 'assetsWithoutMeta': len(no_meta), 'metasWithoutAsset': len(no_asset), 'duplicateGuids': len(dup), 'seconds': round(time.time() - t0, 1)}
    return {'v': 1, 'repo': src.label, **src.meta, 'unityRoot': unity_root, 'scanned': time.strftime('%Y-%m-%d %H:%M'), 'unity': unity_version, 'pipeline': pipeline, 'vfxGraphPackage': has_vfxgraph_pkg,
            'stats': stats, 'vfx': vfx, 'missing': missing, 'binary': binary[:500], 'orphans': {'assetsWithoutMeta': no_meta[:500], 'metasWithoutAsset': no_asset[:500]},
            'duplicateGuids': [{'guid': g, 'paths': [guid2path[g]] + ps} for g, ps in list(dup.items())[:200]]}


# ------------------------------------------------------------------ preview step: realtime 3D data + library pack
def preview_step(src, report, slug, args):
    """Export the found Particle System VFX with tools/flarebox_export3d.py and register them as a Flarebox pack
    (vfx/<slug>/manifest.js + 3d/), so they appear in Projects / grid / Scene View like every other effect."""
    lib = os.path.abspath(os.path.join(args.out, '..')); pack_dir = os.path.join(lib, slug); ur = report.get('unityRoot') or ''
    # --collection imports every prefab that contains particle systems (also "Mixed" objects carrying VFX)
    wanted = (lambda v: (v.get('counts') or {}).get('particleSystems', 0) > 0) if args.collection is not None else (lambda v: v['type'].startswith('Particle System'))
    cand = [v for v in report['vfx'] if wanted(v) and v['path'].startswith(ur) and v['path'].endswith('.prefab')]
    # all-branches mode: a prefab identical (same path + same blob) to one already exported from an earlier branch is skipped
    seen = getattr(args, 'seen', None)
    if seen is not None and getattr(src, 'shas', None):
        keep = []
        for v in cand:
            k = (v['path'], src.shas.get(v['path']))
            if k in seen: continue
            seen.add(k); keep.append(v)
        if len(keep) < len(cand): log(f'preview: {len(cand) - len(keep)} VFX are identical to ones already taken from another branch — skipped')
        cand = keep
    cand.sort(key=lambda v: (v['confidence'] != 'high', v['missing'] > 0, v['path'].lower()))
    if args.preview_limit != 'all': cand = cand[:int(args.preview_limit)]
    if not cand: log('preview: no Particle System VFX to export'); return
    log(f'preview: exporting {len(cand)} VFX to realtime 3D …')
    mirror = None
    if isinstance(src, LocalSource): proj = os.path.join(src.root, ur)
    else:
        mirror = tempfile.mkdtemp(prefix='flarebox-mirror-'); proj = os.path.join(mirror, ur)
        bins = sorted(set(d['path'] for v in cand for d in v['deps'] if d['status'] == 'ok' and d['path'] and d['kind'] in ('texture', 'mesh')))
        log(f'  downloading {len(bins)} textures / meshes used by those VFX …')
        if hasattr(src, 'prefetch_bytes'): blobs = src.prefetch_bytes(bins)
        else:
            with cf.ThreadPoolExecutor(12) as ex: blobs = dict(zip(bins, ex.map(src.raw_bytes, bins)))
        for rel, txt in list(src.cache.items()) + list(blobs.items()):
            if txt is None: continue
            fp = os.path.join(mirror, rel); os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, 'wb') as fh: fh.write(txt if isinstance(txt, bytes) else txt.encode('utf-8'))
    os.makedirs(pack_dir, exist_ok=True)
    items = []
    for v in cand:
        rel = v['path'][len(ur):]
        iid = slug + '-' + re.sub(r'[^a-z0-9]+', '-', re.sub(r'^Assets/', '', rel)[:-len('.prefab')].lower()).strip('-')
        items.append((v, iid, rel))
    def write_manifest(rows):
        lines = []
        for v, iid, rel, loop in rows:
            lines.append('  { id: %s, name: %s, folder: %s, file: null, pkg: null, prefab: %s, scanPath: %s, cols: 1, rows: 1, fps: 30, dur: 1, loop: %s }'
                         % (json.dumps(iid), json.dumps(v['name']), json.dumps(os.path.dirname(re.sub(r'^Assets/', '', rel))), json.dumps(rel), json.dumps(v['path']), 'true' if loop else 'false'))
        open(os.path.join(pack_dir, 'manifest.js'), 'w').write(
            '/* Generated by tools/flarebox_scan_repo.py (repository scan preview). */\nwindow.FLAREBOX_PACKS = window.FLAREBOX_PACKS || [];\n'
            'window.FLAREBOX_PACKS.push({ pack: %s, project: true, scan: %s, base: %s, unity: %s, baked: %s, items: [\n%s\n]});\n'
            % (json.dumps(report.get('label') or report['repo']), json.dumps(slug), json.dumps('vfx/' + slug + '/'), json.dumps(report.get('unity') or ''), json.dumps(time.strftime('%Y-%m-%d')), ',\n'.join(lines)))
    write_manifest([(v, iid, rel, False) for v, iid, rel in items])
    exporter = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flarebox_export3d.py')
    try:
        subprocess.run([sys.executable, exporter, proj, os.path.join(pack_dir, 'manifest.js'), os.path.join(pack_dir, '3d')], check=False)
    finally:
        if mirror: shutil.rmtree(mirror, ignore_errors=True)
    done = set(f[:-3] for f in os.listdir(os.path.join(pack_dir, '3d')) if f.endswith('.js') and not f.startswith('_')) if os.path.isdir(os.path.join(pack_dir, '3d')) else set()
    rows = []
    for v, iid, rel in items:
        if iid not in done: continue
        head = open(os.path.join(pack_dir, '3d', iid + '.js')).read(200)
        rows.append((v, iid, rel, '"loop":1' in head)); v['preview'] = iid
    write_manifest(rows)
    idx = os.path.join(lib, 'index.js'); line = "document.write('<script src=\"vfx/%s/manifest.js\"><\\/script>');\n" % slug
    cur = open(idx).read() if os.path.exists(idx) else '/* Lists every VFX pack. */\n'
    if line not in cur: open(idx, 'w').write(cur + ('' if cur.endswith('\n') else '\n') + line)
    report['preview'] = {'pack': report.get('label') or report['repo'], 'base': 'vfx/' + slug + '/', 'exported': len(rows), 'candidates': len(cand), 'ids': [iid for _, iid, _, _ in rows]}
    log(f'preview: {len(rows)}/{len(cand)} VFX ready in vfx/{slug}/ (open the "Projects" tab or the scan report)')

def write_review_collection(report, slug, args, ids=None, found=None, branches=None):
    """Register the exported VFX as a review collection: vfx/scans/collections.js → FLAREBOX_SCAN_COLS.
    The page merges these into the user's collections on load (one collection per repository + branch choice, updated on re-scan)."""
    if ids is None: ids = (report.get('preview') or {}).get('ids') or []
    if found is None: found = report['stats']['vfx']
    path = os.path.join(args.out, 'collections.js'); cols = []
    if os.path.exists(path):
        m = re.search(r'=\s*(\[.*\])\s*;?\s*$', open(path).read(), re.S)
        if m:
            try: cols = json.loads(m.group(1))
            except Exception: cols = []
    cid = 'scan-' + slug
    cols = [c for c in cols if c.get('id') != cid]
    label = f" @ all branches ({len(branches)})" if branches else (f" @ {report['branch']}" if report.get('label') else '')
    cols.append({'id': cid, 'name': args.collection or f"Review · {report['repo']}{label}", 'scan': report.get('id') or slug, 'repo': report['repo'], 'url': report.get('url'),
                 'branch': ('all: ' + ', '.join(branches)) if branches else report.get('branch'), 'commit': None if branches else report.get('commit'),
                 'ids': ids, 'found': found, 'created': int(time.time() * 1000)})
    open(path, 'w').write('/* Generated by tools/flarebox_scan_repo.py --collection: review collections created from repository scans. */\n'
                          'window.FLAREBOX_SCAN_COLS=%s;\n' % json.dumps(cols, separators=(',', ':')))
    report['collection'] = cid
    log(f'collection: "{cols[-1]["name"]}" with {len(ids)} VFX → {path}')
    print('FLAREBOX_COLLECTION ' + json.dumps({'id': cid, 'scan': report.get('id') or slug, 'count': len(ids), 'found': found}), flush=True)

def github_error(e, args):
    """Readable explanation of a GitHub API failure while opening the repository."""
    try: msg = json.loads(e.read() or b'{}').get('message') or ''
    except Exception: msg = ''
    has_tok = bool(os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN'))
    if e.code == 409 and 'empty' in msg.lower(): return 'this repository is empty — it has no commits yet. Push the Unity project (Assets/, ProjectSettings/ and the .meta files) and import again.'
    if e.code in (404, 422) and '/commits/' in e.url: return f'branch "{urllib.parse.unquote(e.url.split("/commits/")[-1])}" was not found in this repository.'
    if e.code == 404: return ('repository not found. Check the link — if the repo is private, the token cannot see it: a classic token needs the "repo" scope; a fine-grained token needs this organization as resource owner and Contents: read for this repo. If the organization uses SSO, also authorize the token for it on GitHub. Press Test in the Settings tab to check.' if has_tok
                              else 'repository not found. Check the link — if the repo is private, add a GitHub token in the Settings tab.')
    if e.code == 401: return 'GitHub rejected the token (bad or expired). Update it in the Settings tab.'
    if e.code in (403, 429): return 'GitHub refused or rate-limited the request' + ('' if has_tok else ' — add a token in the Settings tab to raise the limit') + (f' ({msg})' if msg else '') + '.'
    return f'GitHub answered HTTP {e.code}' + (f': {msg}' if msg else '') + '.'

def slugify(x): return re.sub(r'[^a-z0-9]+', '-', x.lower()).strip('-')

def write_report(report, slug, args):
    open(os.path.join(args.out, slug + '.json'), 'w').write(json.dumps(report, indent=1))
    # compact copy for the web page: every path stored once in a table, deps as [path, guid, kind, status, via, note]
    P, PI = [], {}
    def pi(x):
        if x is None: return -1
        if x not in PI: PI[x] = len(P); P.append(x)
        return PI[x]
    web = dict(report); web['P'] = P
    web['vfx'] = [{**{k: v for k, v in x.items() if k != 'deps'}, 'deps': [[pi(d['path']), '' if d['status'] == 'ok' else d['guid'], d['kind'], d['status'], pi(d['via']), d.get('note') or ''] for d in x['deps']]} for x in report['vfx']]
    open(os.path.join(args.out, slug + '.js'), 'w').write('(window.FLAREBOX_SCANS=window.FLAREBOX_SCANS||{})[%s]=%s;\n' % (json.dumps(slug), json.dumps(web, separators=(',', ':'))))
    s = report['stats']
    print(f"{report.get('label') or report['repo']}: {s['vfx']} VFX in {s['prefabs']} prefabs · types {s['byType']} · {s['missingDeps']} missing dependencies ({s['vfxWithMissing']} VFX affected) · "
          f"{s['assetsWithoutMeta']} assets without .meta · {s['metasWithoutAsset']} orphan .meta · {s['seconds']}s → {os.path.join(args.out, slug + '.json')}", flush=True)

def write_index(args):
    """index of all scans (summaries only — full reports load on demand)"""
    summ = []
    for f in sorted(os.listdir(args.out)):
        if not f.endswith('.json'): continue
        try: r = json.load(open(os.path.join(args.out, f)))
        except Exception: continue
        s = {k: r.get(k) for k in ('id', 'repo', 'label', 'url', 'path', 'branch', 'commit', 'scanned', 'unity', 'pipeline', 'stats', 'preview', 'collection')}
        if s['preview']: s['preview'] = {k: v for k, v in s['preview'].items() if k != 'ids'}
        summ.append(s)
    open(os.path.join(args.out, 'index.js'), 'w').write('/* Generated by tools/flarebox_scan_repo.py */\nwindow.FLAREBOX_SCAN_INDEX=%s;\n' % json.dumps(summ, separators=(',', ':')))

def scan_one(src, args, default_branch=None):
    """scan + preview one source; non-default branches get their own report / pack id (owner-repo--branch)."""
    report = scan(src, args)
    base = slugify(report['repo']) or 'repo'
    br = report.get('branch')
    if br and default_branch and br != default_branch:
        slug = base + '--' + slugify(br); report['label'] = f"{report['repo']} @ {br}"
    else: slug = base
    report['id'] = slug
    if args.preview_limit not in ('0', 0): preview_step(src, report, slug, args)
    return report, slug

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('target'); ap.add_argument('--branch', help="branch to scan (default: the repository's default branch); 'all' scans every branch of a GitHub repo")
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vfx', 'scans'))
    ap.add_argument('--max-text-mb', type=float, default=8)
    ap.add_argument('--max-branches', type=int, default=50, help='safety limit for --branch all')
    ap.add_argument('--preview-limit', default=None, help="how many Particle System VFX to export for realtime 3D preview ('all' or a number, 0 = skip; default 200, or all with --collection)")
    ap.add_argument('--collection', nargs='?', const='', default=None, metavar='NAME',
                    help='import mode: export every VFX containing particle systems and put them in a review collection (default name "Review · owner/repo")')
    args = ap.parse_args()
    if args.preview_limit is None: args.preview_limit = 'all' if args.collection is not None else '200'
    all_br = (args.branch or '').lower() == 'all'
    try: src, tmp = open_source(args.target, None if all_br else args.branch)
    except urllib.error.HTTPError as e: sys.exit('Error: ' + github_error(e, args))
    except urllib.error.URLError as e: sys.exit(f'Error: could not reach GitHub ({e.reason}). Check the internet connection.')
    except subprocess.CalledProcessError: sys.exit('Error: git clone failed — check the link and that you have access to the repository.')
    os.makedirs(args.out, exist_ok=True)
    try:
        if not all_br:
            default = None
            if isinstance(src, GitHubSource):
                try: default = src.api(f'/repos/{src.owner}/{src.repo}')['default_branch']
                except Exception: default = None
            report, slug = scan_one(src, args, default)
            if args.collection is not None: write_review_collection(report, slug, args)
            write_report(report, slug, args)
        else:
            if not isinstance(src, GitHubSource): sys.exit('Error: --branch all is only supported for GitHub links.')
            if src.branch != src.api(f'/repos/{src.owner}/{src.repo}')['default_branch']: src = github_source(src.owner, src.repo)
            default = src.api(f'/repos/{src.owner}/{src.repo}')['default_branch']
            names = src.branches()
            names = [default] + sorted(n for n in names if n != default)
            if len(names) > args.max_branches:
                log(f'! {len(names)} branches — only the first {args.max_branches} are scanned (raise --max-branches to scan more)')
                names = names[:args.max_branches]
            log(f'scanning {len(names)} branch(es): {", ".join(names)}')
            args.seen = set(); ids, done, first, uniq = [], [], None, set()
            for i, br in enumerate(names):
                log(f'━━ branch {i + 1}/{len(names)}: {br}')
                try: bsrc = src if br == default else github_source(src.owner, src.repo, br)
                except urllib.error.HTTPError as e: log(f'  ! skipped {br}: ' + github_error(e, args)); continue
                try: report, slug = scan_one(bsrc, args, default)
                except Exception as e: log(f'  ! branch {br} failed: {type(e).__name__}: {e}'); continue
                write_report(report, slug, args)
                ids += (report.get('preview') or {}).get('ids') or []; done.append(br)
                uniq.update((v['path'], bsrc.shas.get(v['path'])) for v in report['vfx'])
                first = first or report
            if not first: sys.exit('Error: no branch could be scanned.')
            if args.collection is not None:
                agg = dict(first); agg['id'] = slugify(first['repo'])
                write_review_collection(agg, slugify(first['repo']) + '--all-branches', args, ids=ids, found=len(uniq), branches=done)
    finally:
        if tmp: shutil.rmtree(tmp, ignore_errors=True)
    write_index(args)

if __name__ == '__main__':
    main()
