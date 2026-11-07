"""Unity prefab (YAML) -> Flarebox realtime 3D effect data.

Reads ParticleSystem prefabs straight from a Unity project folder (no Unity needed):
hierarchy + nested prefabs, every common particle module, materials (blend / colour / main + mask
texture / UV scroll), textures (PIL, downscaled, embedded as data URIs so WebGL can use them on file://)
and FBX meshes (binary FBX parser). Output: <out>/<id>.js  ->  FLAREBOX_FX3D[id] = {...}

usage (needs: pip3 install pyyaml pillow):
  python3 tools/flarebox_export3d.py <unity_project> vfx/<pack>/manifest.js vfx/<pack>/3d
e.g.
  python3 tools/flarebox_export3d.py /Volumes/Supercent/Unity/hb_creative_xp_hero/unity_project/hb_creative_xp_hero vfx/xp-hero/manifest.js vfx/xp-hero/3d
"""
import sys, os, re, json, math, copy, io, base64, struct, zlib
import yaml
from PIL import Image

PROJ = sys.argv[1].rstrip('/') + '/'

# ---------------------------------------------------------------- Unity YAML + GUID index
def _guid_index():
    g = {}
    roots = [PROJ, PROJ + 'Library/PackageCache']
    for root in roots:
        for dp, dn, fn in os.walk(root):
            if root == PROJ: dn[:] = [d for d in dn if d not in ('.git', 'Library', 'Temp', 'Logs', 'obj', 'Build', 'Builds', 'UserSettings')]
            for f in fn:
                if not f.endswith('.meta'): continue
                if 'PackageCache' in dp and not (f.endswith('.shader.meta') or f.endswith('.mat.meta') or f.endswith('.shadergraph.meta')): continue
                try:
                    with open(os.path.join(dp, f), errors='ignore') as fh:
                        for line in fh:
                            if line.startswith('guid:'): g[line.split()[1]] = os.path.join(dp, f[:-5])[len(PROJ):]; break
                except OSError: pass
    return g
print('indexing GUIDs…', flush=True)
G = _guid_index()
_HDR = re.compile(r'^--- !u!(\d+) &(-?\d+)( stripped)?\s*$', re.M)
_cache = {}
def load(path):
    """Unity YAML asset -> {fileID: (classID, name, dict, stripped)}"""
    if path in _cache: return _cache[path]
    t = open(PROJ + path, encoding='utf-8', errors='ignore').read()
    out = {}; ms = list(_HDR.finditer(t))
    for i, m in enumerate(ms):
        body = t[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(t)]
        try: d = yaml.load(body.replace(': Infinity', ': .inf').replace(': -Infinity', ': -.inf'), Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
        except Exception: d = None
        if isinstance(d, dict) and len(d) == 1:
            k = next(iter(d)); out[int(m.group(2))] = (int(m.group(1)), k, d[k] or {}, bool(m.group(3)))
    _cache[path] = out
    return out

S = 32  # curve / gradient samples

def num(v, d=0.0):
    if isinstance(v, bool): return float(v)
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str):
        t = v.strip()
        if t in ('Infinity', 'inf', '.inf'): return float('inf')
        if t in ('-Infinity', '-inf', '-.inf'): return float('-inf')
        try: return float(t)
        except ValueError: return d
    return d

def R(x, n=4):
    if isinstance(x, list): return [R(v, n) for v in x]
    if not math.isfinite(x): return 0
    r = round(x, n)
    return int(r) if r == int(r) else r

# ---------------------------------------------------------------- curves & gradients
def eval_curve(keys, t):
    if not keys: return 0.0
    if t <= num(keys[0]['time']): return num(keys[0]['value'])
    if t >= num(keys[-1]['time']): return num(keys[-1]['value'])
    for a, b in zip(keys, keys[1:]):
        t0, t1 = num(a['time']), num(b['time'])
        if t0 <= t <= t1:
            dt = t1 - t0
            if dt <= 1e-9: return num(b['value'])
            m0, m1 = num(a.get('outSlope', 0)), num(b.get('inSlope', 0))
            v0, v1 = num(a['value']), num(b['value'])
            if not math.isfinite(m0) or not math.isfinite(m1): return v0
            u = (t - t0) / dt; u2 = u * u; u3 = u2 * u
            return (2*u3 - 3*u2 + 1)*v0 + (u3 - 2*u2 + u)*m0*dt + (-2*u3 + 3*u2)*v1 + (u3 - u2)*m1*dt
    return num(keys[-1]['value'])

def sample_curve(c):
    keys = (c or {}).get('m_Curve') or []
    keys = sorted(keys, key=lambda k: num(k['time']))
    return [R(eval_curve(keys, i / (S - 1))) for i in range(S)]

def mmc(c, scale=1.0):
    """MinMaxCurve -> {m, s, mn, k, k2}. m: 0 const, 1 curve, 2 two curves, 3 two consts."""
    if c is None: return {'m': 0, 's': 0}
    if not isinstance(c, dict): return {'m': 0, 's': R(num(c) * scale)}
    m = int(num(c.get('minMaxState', 0)))
    s = num(c.get('scalar', 0)) * scale; mn = num(c.get('minScalar', 0)) * scale
    if m == 0: return {'m': 0, 's': R(s)}
    if m == 3: return {'m': 3, 's': R(s), 'mn': R(mn)}
    o = {'m': m, 's': R(s), 'k': sample_curve(c.get('maxCurve'))}
    if m == 2: o['k2'] = sample_curve(c.get('minCurve'))
    return o

def col(c):
    c = c or {}
    return [R(num(c.get('r', 1))), R(num(c.get('g', 1))), R(num(c.get('b', 1))), R(num(c.get('a', 1)))]

def sample_grad(g):
    g = g or {}
    nc = max(1, int(num(g.get('m_NumColorKeys', 2)))); na = max(1, int(num(g.get('m_NumAlphaKeys', 2))))
    fixed = int(num(g.get('m_Mode', 0))) == 1
    ck = [(num(g.get('ctime%d' % i, 0)) / 65535, col(g.get('key%d' % i))) for i in range(nc)]
    ak = [(num(g.get('atime%d' % i, 0)) / 65535, col(g.get('key%d' % i))[3]) for i in range(na)]
    def ev(keys, t, f):
        if t <= keys[0][0]: return keys[0][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t <= t1:
                if fixed: return v1
                u = 0 if t1 - t0 < 1e-6 else (t - t0) / (t1 - t0)
                return f(v0, v1, u)
        return keys[-1][1]
    lerp3 = lambda a, b, u: [a[i] + (b[i] - a[i]) * u for i in range(3)]
    lerp1 = lambda a, b, u: a + (b - a) * u
    out = []
    for i in range(S):
        t = i / (S - 1)
        rgb = ev([(k[0], k[1][:3]) for k in ck], t, lerp3); a = ev(ak, t, lerp1)
        out += [R(rgb[0], 3), R(rgb[1], 3), R(rgb[2], 3), R(a, 3)]
    return out

def mmg(c):
    """MinMaxGradient -> {m, c|a,b|g|g,g2}. m: 0 color, 1 gradient, 2 two colors, 3 two gradients, 4 random color."""
    c = c or {}
    m = int(num(c.get('minMaxState', 0)))
    if m == 0: return {'m': 0, 'c': col(c.get('maxColor'))}
    if m == 2: return {'m': 2, 'a': col(c.get('minColor')), 'b': col(c.get('maxColor'))}
    if m == 3: return {'m': 3, 'g': sample_grad(c.get('maxGradient')), 'g2': sample_grad(c.get('minGradient'))}
    return {'m': m, 'g': sample_grad(c.get('maxGradient'))}

# ---------------------------------------------------------------- math
def qmul(a, b):
    ax, ay, az, aw = a; bx, by, bz, bw = b
    return (aw*bx + ax*bw + ay*bz - az*by, aw*by - ax*bz + ay*bw + az*bx, aw*bz + ax*by - ay*bx + az*bw, aw*bw - ax*bx - ay*by - az*bz)

def qeuler(e):  # Unity Quaternion.Euler (degrees): Z, then X, then Y
    def ax(v, deg):
        h = math.radians(deg) / 2; s = math.sin(h)
        return (v[0]*s, v[1]*s, v[2]*s, math.cos(h))
    return qmul(qmul(ax((0, 1, 0), e[1]), ax((1, 0, 0), e[0])), ax((0, 0, 1), e[2]))

def trs(p, q, s):
    x, y, z, w = q
    n = math.sqrt(x*x + y*y + z*z + w*w) or 1; x, y, z, w = x/n, y/n, z/n, w/n
    r = [[1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
         [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
         [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)]]
    return [[r[0][0]*s[0], r[0][1]*s[1], r[0][2]*s[2], p[0]],
            [r[1][0]*s[0], r[1][1]*s[1], r[1][2]*s[2], p[1]],
            [r[2][0]*s[0], r[2][1]*s[1], r[2][2]*s[2], p[2]], [0, 0, 0, 1]]

def mmul(a, b): return [[sum(a[i][k]*b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
def v3(d, dflt=0): d = d or {}; return [num(d.get('x', dflt)), num(d.get('y', dflt)), num(d.get('z', dflt))]
def q4(d): d = d or {}; return (num(d.get('x', 0)), num(d.get('y', 0)), num(d.get('z', 0)), num(d.get('w', 1)))

# ---------------------------------------------------------------- prefab graph (with nested prefab instances)
def set_path(d, path, value, objref):
    parts = re.findall(r'Array\.data\[(\d+)\]|([^.]+)', path)
    toks = [int(a) if a else b for a, b in parts if (a or b) and b != 'Array']
    cur = d
    for i, t in enumerate(toks):
        last = i == len(toks) - 1
        if isinstance(t, int):
            if not isinstance(cur, list): return
            while len(cur) <= t: cur.append({} if not last else None)
            if last:
                cur[t] = objref if (objref and objref.get('fileID')) else (num(value) if isinstance(cur[t], (int, float)) else value)
                return
            if cur[t] is None: cur[t] = {}
            cur = cur[t]
        else:
            if not isinstance(cur, dict): return
            if t == 'size' and last: return
            if last:
                old = cur.get(t)
                if objref and objref.get('fileID'): cur[t] = objref
                elif isinstance(old, (int, float)) or old is None: cur[t] = num(value) if value not in (None, '') and re.match(r'^-?[\d.eE+-]+$|^-?Infinity$', str(value)) else value
                else: cur[t] = value
                return
            if t not in cur or not isinstance(cur[t], (dict, list)): cur[t] = {}
            cur = cur[t]

def build(path, depth=0):
    """-> (objs {key: {'cid','name','d','father','go'}}, fidmap {local fid: key}, root_tf_key)"""
    docs = load(path)
    objs, remap, inst_maps = {}, {}, {}
    for fid, (cid, name, d, stripped) in docs.items():
        if stripped or name == 'PrefabInstance': continue
        objs[(fid,)] = {'cid': cid, 'name': name, 'd': copy.deepcopy(d)}
        remap[fid] = (fid,)
    for fid, (cid, name, d, stripped) in docs.items():
        if name != 'PrefabInstance' or depth > 5: continue
        src = G.get((d.get('m_SourcePrefab') or {}).get('guid'))
        if not src or not os.path.exists(PROJ + src): continue
        sobjs, sfid, sroot = build(src, depth + 1)
        sobjs = copy.deepcopy(sobjs)
        mod = d.get('m_Modification') or {}
        for m in mod.get('m_Modifications') or []:
            k = sfid.get((m.get('target') or {}).get('fileID'))
            if k is None or k not in sobjs: continue
            set_path(sobjs[k]['d'], m.get('propertyPath', ''), m.get('value'), m.get('objectReference'))
        for rc in mod.get('m_RemovedComponents') or []:
            k = sfid.get(rc.get('fileID'))
            if k in sobjs: del sobjs[k]
        pre = lambda k: None if k is None else (fid,) + k
        for k, o in sobjs.items():
            o = dict(o); o['father'] = pre(o.get('father')); o['go'] = pre(o.get('go'))
            objs[(fid,) + k] = o
        if sroot is not None:
            objs[(fid,) + sroot]['father_fid'] = (mod.get('m_TransformParent') or {}).get('fileID', 0)
        inst_maps[fid] = {sf: (fid,) + k for sf, k in sfid.items()}
    for fid, (cid, name, d, stripped) in docs.items():
        if stripped:
            inst = (d.get('m_PrefabInstance') or {}).get('fileID'); src = (d.get('m_CorrespondingSourceObject') or {}).get('fileID')
            k = inst_maps.get(inst, {}).get(src)
            if k: remap[fid] = k
    root = None
    for k, o in objs.items():
        if 'father_fid' in o:
            ff = o.pop('father_fid'); o['father'] = remap.get(ff) if ff else None
        elif len(k) == 1:
            d = o['d']
            if o['name'] in ('Transform', 'RectTransform'):
                ff = (d.get('m_Father') or {}).get('fileID', 0); o['father'] = remap.get(ff) if ff else None
            gf = (d.get('m_GameObject') or {}).get('fileID', 0); o['go'] = remap.get(gf) if gf else None
    for k, o in objs.items():
        if o['name'] in ('Transform', 'RectTransform') and o.get('father') is None and len(k) == 1: root = k
    if root is None:
        for k, o in objs.items():
            if o['name'] in ('Transform', 'RectTransform') and o.get('father') is None: root = k; break
    return objs, remap, root

# ---------------------------------------------------------------- textures
TEX = {}  # (guid, maxdim) -> id
TEXDATA = {}
TEXNAME = {}
def tex_id(guid, maxdim=256, fid=None):
    if not guid: return None
    if guid == '0000000000000000f000000000000000': return 'builtin:particle'
    key = (guid, maxdim)
    if key in TEX: return TEX[key]
    p = G.get(guid)
    if not p or not os.path.exists(PROJ + p): return None
    try:
        im = Image.open(PROJ + p)
        im.load()
    except Exception as e:
        print('  ! texture', p, e); return None
    meta = open(PROJ + p + '.meta', errors='ignore').read() if os.path.exists(PROJ + p + '.meta') else ''
    m = re.search(r'alphaSource: (\d)', meta); asrc = int(m.group(1)) if m else (2 if re.search(r'grayScaleToAlpha: 1', meta) else 1)
    im = im.convert('RGBA')
    if asrc == 2:
        im.putalpha(im.convert('L'))
    elif asrc == 0:
        im.putalpha(255)
    w, h = im.size; k = min(1.0, maxdim / max(w, h))
    if k < 1: im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    b = io.BytesIO(); im.save(b, 'PNG', optimize=True)
    tid = 't%d' % len(TEX)
    TEX[key] = tid; TEXDATA[tid] = 'data:image/png;base64,' + base64.b64encode(b.getvalue()).decode(); TEXNAME[tid] = os.path.basename(p)
    return tid

# ---------------------------------------------------------------- FBX meshes
def fbx_parse(path):
    data = open(path, 'rb').read()
    if not data.startswith(b'Kaydara FBX Binary'): return None
    ver = struct.unpack_from('<I', data, 23)[0]; wide = ver >= 7500
    def read_node(off):
        if wide: end, nprop, plen = struct.unpack_from('<QQQ', data, off); off += 24
        else: end, nprop, plen = struct.unpack_from('<III', data, off); off += 12
        nlen = data[off]; off += 1
        if end == 0: return None, off
        name = data[off:off + nlen].decode('ascii', 'ignore'); off += nlen
        props = []
        for _ in range(nprop):
            t = chr(data[off]); off += 1
            if t in 'YCIFDL':
                fmt = {'Y': '<h', 'C': '<?', 'I': '<i', 'F': '<f', 'D': '<d', 'L': '<q'}[t]
                props.append(struct.unpack_from(fmt, data, off)[0]); off += struct.calcsize(fmt)
            elif t in 'fdlibc':
                n, enc, clen = struct.unpack_from('<III', data, off); off += 12
                raw = data[off:off + clen]; off += clen
                if enc == 1: raw = zlib.decompress(raw)
                fmt = {'f': 'f', 'd': 'd', 'l': 'q', 'i': 'i', 'b': 'b', 'c': 'b'}[t]
                props.append(list(struct.unpack('<%d%s' % (n, fmt), raw[:n * struct.calcsize(fmt)])))
            elif t in 'SR':
                n = struct.unpack_from('<I', data, off)[0]; off += 4
                props.append(data[off:off + n]); off += n
        kids = []
        while off < end:
            k, off2 = read_node(off)
            if k is None: off = off2; break
            kids.append(k); off = off2
        return {'name': name, 'props': props, 'kids': kids}, end
    nodes, off = [], 27
    while off < len(data) - 13:
        n, nxt = read_node(off)
        if n is None: break
        nodes.append(n); off = nxt
    return nodes

def kid(n, name): return next((k for k in n['kids'] if k['name'] == name), None)

MESH = {}; MESHDATA = {}
def mesh_id(ref):
    if not ref or not ref.get('fileID'): return None
    guid, fid = ref.get('guid'), int(num(ref.get('fileID')))
    if guid == '0000000000000000e000000000000000':
        return {10202: 'builtin:cube', 10206: 'builtin:cylinder', 10207: 'builtin:sphere', 10208: 'builtin:capsule', 10209: 'builtin:plane', 10210: 'builtin:quad'}.get(fid, 'builtin:quad')
    key = (guid, fid)
    if key in MESH: return MESH[key]
    p = G.get(guid)
    if not p or not p.lower().endswith('.fbx'): MESH[key] = None; return None
    meta = open(PROJ + p + '.meta', errors='ignore').read()
    want = None
    m = re.search(r'- first:\s*\n\s*43: %d\s*\n\s*second: (.+)' % fid, meta)
    if m: want = m.group(1).strip()
    m2 = re.search(r'\n  4300\d\d\d: (.+)', meta) if not want else None
    gm = re.search(r'globalScale: ([\d.]+)', meta); gscale = float(gm.group(1)) if gm else 1.0
    nodes = fbx_parse(PROJ + p)
    if not nodes: MESH[key] = None; return None
    unit = 1.0
    gs = next((n for n in nodes if n['name'] == 'GlobalSettings'), None)
    if gs:
        p70 = kid(gs, 'Properties70')
        for pp in (p70 or {'kids': []})['kids']:
            if pp['props'] and pp['props'][0] == b'UnitScaleFactor': unit = float(pp['props'][-1])
    objs = kid(next(n for n in nodes if n['name'] == 'Objects'), 'Objects') if False else next((n for n in nodes if n['name'] == 'Objects'), None)
    geoms = [k for k in objs['kids'] if k['name'] == 'Geometry' and len(k['props']) > 2 and k['props'][2] == b'Mesh']
    models = {k['props'][0]: k['props'][1].split(b'\x00')[0].decode('utf-8', 'ignore') for k in objs['kids'] if k['name'] == 'Model'}
    conns = next((n for n in nodes if n['name'] == 'Connections'), {'kids': []})
    geo_model = {}
    for c in conns['kids']:
        if c['props'] and c['props'][0] == b'OO': geo_model[c['props'][1]] = models.get(c['props'][2])
    geo = None
    if want:
        geo = next((g for g in geoms if geo_model.get(g['props'][0]) == want or g['props'][1].split(b'\x00')[0].decode('utf-8', 'ignore') == want), None)
    geo = geo or (geoms[0] if geoms else None)
    if not geo: MESH[key] = None; return None
    V = kid(geo, 'Vertices')['props'][0]; PVI = kid(geo, 'PolygonVertexIndex')['props'][0]
    uvl = kid(geo, 'LayerElementUV'); uv = uvi = None; uvmap = b'ByPolygonVertex'; uvref = b'Direct'
    if uvl:
        uv = kid(uvl, 'UV')['props'][0]
        k = kid(uvl, 'UVIndex'); uvi = k['props'][0] if k else None
        uvmap = kid(uvl, 'MappingInformationType')['props'][0]; uvref = kid(uvl, 'ReferenceInformationType')['props'][0]
    sc = unit / 100.0 * gscale
    pos, uvs, idx = [], [], []
    poly, pvi_n = [], 0
    for n, raw in enumerate(PVI):
        vi = raw if raw >= 0 else ~raw
        if uv is not None:
            if uvmap in (b'ByPolygonVertex',):
                ui = uvi[n] if (uvref == b'IndexToDirect' and uvi) else n
            else:
                ui = uvi[vi] if (uvref == b'IndexToDirect' and uvi) else vi
            u, v = uv[ui * 2], uv[ui * 2 + 1]
        else: u = v = 0
        poly.append(len(pos) // 3)
        pos += [-V[vi * 3] * sc, V[vi * 3 + 1] * sc, V[vi * 3 + 2] * sc]; uvs += [u, v]
        if raw < 0:
            for j in range(1, len(poly) - 1): idx += [poly[0], poly[j + 1], poly[j]]
            poly = []
    mid = 'm%d' % len(MESHDATA)
    MESH[key] = mid
    MESHDATA[mid] = {'p': R(pos, 4), 'uv': R(uvs, 4), 'i': idx}
    return mid

# ---------------------------------------------------------------- materials
SG = {}
def sg_names(shader_path):
    """shadergraph reference name -> display name"""
    if shader_path in SG: return SG[shader_path]
    out = {}
    try:
        t = open(PROJ + shader_path, errors='ignore').read()
        for blk in re.split(r'\n\s*\n', t):
            try: o = json.loads(blk)
            except Exception: continue
            ref = o.get('m_OverrideReferenceName') or o.get('m_DefaultReferenceName')
            if ref and o.get('m_Name'): out[ref] = o['m_Name']
    except Exception: pass
    SG[shader_path] = out
    return out

def material(ref, tiles):
    if not ref or not ref.get('guid'): return None
    mp = G.get(ref['guid'])
    if not mp or not mp.endswith('.mat') or not os.path.exists(PROJ + mp): return None
    md = next((d for _, (c, n, d, s) in load(mp).items() if n == 'Material'), None)
    if not md: return None
    sh = md.get('m_Shader') or {}; sg = sh.get('guid'); sfid = int(num(sh.get('fileID')))
    spath = G.get(sg, '') if sg != '0000000000000000f000000000000000' else 'builtin:%d' % sfid
    props = md.get('m_SavedProperties') or {}
    F = {k: num(v) for x in props.get('m_Floats') or [] for k, v in x.items()}
    C = {k: v for x in props.get('m_Colors') or [] for k, v in x.items()}
    T = {k: v for x in props.get('m_TexEnvs') or [] for k, v in x.items()}
    kw = md.get('m_ShaderKeywords') or md.get('m_ValidKeywords') or ''
    kw = kw if isinstance(kw, str) else ' '.join(kw)
    names = sg_names(spath) if spath.endswith('.shadergraph') else {}
    disp = lambda k: (names.get(k, '') + ' ' + k).lower()
    # ---- blend
    src, dst = F.get('_SrcBlend'), F.get('_DstBlend')
    if src is None and '_SrcMode' in F: src, dst = F.get('_SrcMode'), F.get('_DstMode')
    blend = 'alpha'
    if spath == 'builtin:10720' or spath in ('builtin:200', 'builtin:202'): blend = 'add'
    elif spath in ('builtin:10721', 'builtin:203', 'builtin:201'): blend = 'alpha'
    elif dst is not None:
        if dst == 1: blend = 'add'
        elif dst == 10: blend = 'premul' if src == 1 else 'alpha'
        elif dst == 0 and src == 2: blend = 'mul'
        elif dst == 0 and src == 1: blend = 'opaque'
        elif dst == 3 or (src == 0 and dst == 3): blend = 'mul'
    elif re.search(r'add', mp, re.I): blend = 'add'
    if 'URP' in spath or 'ParticlesUnlit' in spath or 'ParticlesLit' in spath:
        if F.get('_Surface', 1) == 0: blend = 'opaque'
        else: blend = {0: 'alpha', 1: 'premul', 2: 'add', 3: 'mul'}.get(int(F.get('_Blend', 0)), blend)
    if F.get('_Mode') is not None and spath == 'builtin:211':
        blend = {0: 'opaque', 1: 'opaque', 2: 'alpha', 3: 'premul', 4: 'add', 5: 'add', 6: 'mul'}.get(int(F['_Mode']), blend)
    # custom ShaderLab shaders: a literal "Blend X Y" line wins over (possibly stale) material floats
    if spath.endswith('.shader') and os.path.exists(PROJ + spath):
        st = open(PROJ + spath, errors='ignore').read()
        bm = re.search(r'^\s*Blend\s+(\w+)\s+(\w+)', st, re.M | re.I)
        if bm and not bm.group(1).startswith('['):
            a_, b_ = bm.group(1).lower(), bm.group(2).lower()
            if b_ == 'one': blend = 'add'
            elif b_ == 'oneminussrcalpha': blend = 'premul' if a_ == 'one' else 'alpha'
            elif a_ == 'dstcolor' or b_ == 'srccolor': blend = 'mul'
            elif a_ == 'one' and b_ == 'zero': blend = 'opaque'
        elif not bm and re.search(r'"Queue"\s*=\s*"Transparent"', st): blend = 'alpha'
    cut = F.get('_Cutoff', 0.5) if (F.get('_AlphaClip') == 1 or 'ALPHATEST' in kw) else 0
    # ---- colour
    color = [1, 1, 1, 1]
    for k in ('_BaseColor', '_Color', '_MainColor', 'Color_', '_TintColor'):
        if k in C:
            color = col(C[k])
            if k == '_TintColor': color = [color[0] * 2, color[1] * 2, color[2] * 2, color[3] * 2]
            break
    if spath.startswith('builtin:10720') or spath == 'builtin:10721': color = [1, 1, 1, 1]
    if spath.endswith('.shadergraph'):
        for k, v in C.items():
            if re.search(r'colou?r', disp(k)) and not re.search(r'emiss|edge|rim|fresnel|add', disp(k)) and k != '_Color':
                if '_Color' not in C: color = col(v)
                break
        for k in ('_Emission', '_EmissionIntensity', '_Intensity', '_Brightness', '_Glow'):
            if k in F and F[k] > 1 and 'ParticlesUnlit' not in spath:
                color = [color[0] * F[k], color[1] * F[k], color[2] * F[k], color[3]]; break
    elif '_Emission' in F and F['_Emission'] > 1:
        color = [color[0] * F['_Emission'], color[1] * F['_Emission'], color[2] * F['_Emission'], color[3]]
    if 'URP' in spath and '_EMISSION' in kw and '_EmissionColor' in C:
        e = col(C['_EmissionColor'])
        color = [color[0] + e[0], color[1] + e[1], color[2] + e[2], color[3]]
    if 'ParticlesUnlit' in spath and F.get('_ColorMode', 0) == 1: pass
    # ---- textures
    def assigned(k): return (T.get(k) or {}).get('m_Texture', {}).get('guid')
    order = ['_BaseMap', '_MainTex', '_MainText', 'MainTex_', '_MainTexture', 'Main_tex', '_Main_Tex', '_Texture', '_Tex']
    main = next((k for k in order if assigned(k)), None)
    if not main:
        for k in T:
            if assigned(k) and re.search(r'main|base|albedo', disp(k)): main = k; break
    if not main:
        for k in T:
            if assigned(k) and not re.search(r'noise|dist|dissol|mask|flow|normal|ramp|offset|fade|vert|shape[23]', disp(k)): main = k; break
    used = {main}
    def pick(rx, nrx=None):
        k = next((k for k in T if k not in used and assigned(k) and re.search(rx, disp(k)) and not (nrx and re.search(nrx, disp(k)))), None)
        if k: used.add(k)
        return k
    mask = pick(r'mask', r'dissol')
    noise = pick(r'noise|dissol')
    dist = pick(r'dist|flow')
    maxd = 1024 if tiles > 4 else (512 if tiles > 1 else 256)
    out = {'blend': blend, 'color': R(color), 'shader': os.path.basename(spath), 'slots': {}}
    if cut: out['cut'] = R(cut)
    def slot(name, k, md):
        if not k: return
        t = tex_id(assigned(k), md)
        if not t: return
        st = T[k]; sc, of = v3(st.get('m_Scale'), 1), v3(st.get('m_Offset'))
        o = {'tex': t, 'prop': k, 'st': R([sc[0], sc[1], of[0], of[1]]), 'scroll': [0, 0]}
        base = re.sub(r'(Tex(ture)?|Map|_)$', '', k.strip('_'), flags=re.I).lower()
        dn = names.get(k, '').lower().replace(' ', '')
        for ck, cv in C.items():
            d = disp(ck).replace(' ', '')
            if not (base and base in d) and not (dn and dn[:6] in d) and not (name == 'main' and 'main' in d): continue
            if re.search(r'speed|pan|scroll', d):
                sp = col(cv)
                if sp[0] or sp[1]: o['scroll'] = R(sp[:2])
            elif re.search(r'tiling', d):
                tl = col(cv)
                if tl[0] and tl[1]: o['st'][0] *= tl[0]; o['st'][1] *= tl[1]
        if name == 'dist':
            amt = next((F[f] for f in F if re.search(r'dist.*(amount|strength|power|intens)|(amount|strength|power)', f, re.I) and 'dist' in f.lower()), 0.1)
            o['amt'] = R(amt)
        out['slots'][name] = o
    slot('main', main, maxd); slot('mask', mask, 256); slot('noise', noise, 256); slot('dist', dist, 256)
    return out

# ---------------------------------------------------------------- particle system
def shape_mod(sm):
    if not sm or not num(sm.get('enabled')): return None
    rad = sm.get('radius'); arc = sm.get('arc')
    o = {'type': int(num(sm.get('type'))), 'angle': R(num(sm.get('angle', 25))), 'len': R(num(sm.get('length', 5))),
         'radius': R(num(rad.get('value', 1) if isinstance(rad, dict) else rad if rad is not None else 1)),
         'thick': R(num(sm.get('radiusThickness', 1))),
         'arc': R(num(arc.get('value', 360) if isinstance(arc, dict) else arc if arc is not None else 360)),
         'arcMode': int(num(arc.get('mode', 0))) if isinstance(arc, dict) else 0,
         'donut': R(num(sm.get('donutRadius', 0.2))),
         'pos': R(v3(sm.get('m_Position'))), 'rot': R(v3(sm.get('m_Rotation'))), 'scale': R(v3(sm.get('m_Scale'), 1)),
         'box': R(v3(sm.get('boxThickness'))),
         'rdir': R(num(sm.get('randomDirectionAmount', 0))), 'sdir': R(num(sm.get('sphericalDirectionAmount', 0))),
         'rpos': R(num(sm.get('randomPositionAmount', 0))), 'align': int(num(sm.get('alignToDirection', 0)))}
    return o

def ps_export(ps, psr, mtx, lossy, local_scale, name):
    D = lambda k: ps.get(k) or {}
    im = D('InitialModule')
    mode = int(num(ps.get('scalingMode', 0)))
    size_mul = lossy if mode == 0 else (local_scale if mode == 1 else 1.0)
    rad = math.radians(1)
    main = {'dur': R(max(0.05, num(ps.get('lengthInSec', 5)))), 'loop': int(num(ps.get('looping', 1))), 'prewarm': int(num(ps.get('prewarm', 0))),
            'delay': mmc(ps.get('startDelay')), 'life': mmc(im.get('startLifetime')), 'speed': mmc(im.get('startSpeed')),
            'size': mmc(im.get('startSize')), 'rot': mmc(im.get('startRotation')), 'color': mmg(im.get('startColor')),
            'grav': mmc(im.get('gravityModifier')), 'max': int(num(im.get('maxNumParticles', 1000))),
            'simSpeed': R(num(ps.get('simulationSpeed', 1))), 'flip': R(num(im.get('randomizeRotationDirection', 0))),
            'world': int(num(ps.get('moveWithTransform', 0))) == 1}
    if num(im.get('size3D')): main['size3'] = [mmc(im.get('startSize')), mmc(im.get('startSizeY')), mmc(im.get('startSizeZ'))]
    if num(im.get('rotation3D')): main['rot3'] = [mmc(im.get('startRotationX')), mmc(im.get('startRotationY')), mmc(im.get('startRotation'))]
    em = D('EmissionModule'); emo = None
    if num(em.get('enabled')):
        bursts = []
        for b in em.get('m_Bursts') or []:
            if not isinstance(b, dict): continue
            cc = b.get('countCurve')
            if cc is None: cc = {'minMaxState': 3, 'scalar': num(b.get('maxCount', 0)), 'minScalar': num(b.get('minCount', 0))}
            bursts.append({'t': R(num(b.get('time', 0))), 'n': mmc(cc), 'cy': int(num(b.get('cycleCount', 1))), 'iv': R(num(b.get('repeatInterval', 0.01))), 'p': R(num(b.get('probability', 1)))})
        emo = {'rate': mmc(em.get('rateOverTime')), 'bursts': bursts}
    def on(m): return D(m) if num(D(m).get('enabled')) else None
    o = {'name': name, 'm': R([mtx[r][c] for c in range(4) for r in range(4)], 5), 'sm': R(size_mul), 'main': main, 'em': emo, 'shape': shape_mod(D('ShapeModule'))}
    v = on('VelocityModule')
    if v: o['vel'] = {'x': mmc(v.get('x')), 'y': mmc(v.get('y')), 'z': mmc(v.get('z')), 'world': int(num(v.get('inWorldSpace'))),
                      'orb': [mmc(v.get('orbitalX')), mmc(v.get('orbitalY')), mmc(v.get('orbitalZ'))], 'rad': mmc(v.get('radial')), 'spd': mmc(v.get('speedModifier') or {'minMaxState': 0, 'scalar': 1})}
    lv = on('ClampVelocityModule')
    if lv: o['lim'] = {'mag': mmc(lv.get('magnitude')), 'damp': R(num(lv.get('dampen', 0))), 'drag': mmc(lv.get('drag')), 'sep': int(num(lv.get('separateAxis'))),
                       'x': mmc(lv.get('x')), 'y': mmc(lv.get('y')), 'z': mmc(lv.get('z')), 'world': int(num(lv.get('inWorldSpace'))),
                       'dragSize': int(num(lv.get('multiplyDragByParticleSize', 1))), 'dragVel': int(num(lv.get('multiplyDragByParticleVelocity', 1)))}
    f = on('ForceModule')
    if f: o['force'] = {'x': mmc(f.get('x')), 'y': mmc(f.get('y')), 'z': mmc(f.get('z')), 'world': int(num(f.get('inWorldSpace')))}
    c = on('ColorModule')
    if c: o['col'] = mmg(c.get('gradient'))
    s = on('SizeModule')
    if s: o['size'] = {'x': mmc(s.get('curve')), 'y': mmc(s.get('y')), 'z': mmc(s.get('z')), 'sep': int(num(s.get('separateAxes')))}
    r = on('RotationModule')
    if r: o['rot'] = {'z': mmc(r.get('curve')), 'x': mmc(r.get('x')), 'y': mmc(r.get('y')), 'sep': int(num(r.get('separateAxes')))}
    n = on('NoiseModule')
    if n: o['noise'] = {'str': mmc(n.get('strength')), 'strY': mmc(n.get('strengthY')), 'strZ': mmc(n.get('strengthZ')), 'sep': int(num(n.get('separateAxes'))),
                        'freq': R(num(n.get('frequency', 0.5))), 'scroll': mmc(n.get('scrollSpeed')), 'damp': int(num(n.get('damping', 1))),
                        'oct': int(num(n.get('octaves', 1))), 'pos': mmc(n.get('positionAmount') or {'minMaxState': 0, 'scalar': 1}),
                        'rot': mmc(n.get('rotationAmount')), 'size': mmc(n.get('sizeAmount'))}
    uv = on('UVModule'); tiles = 1
    if uv and int(num(uv.get('mode', 0))) == 0:
        tiles = int(num(uv.get('tilesX', 1))) * int(num(uv.get('tilesY', 1)))
        o['uv'] = {'tx': int(num(uv.get('tilesX', 1))), 'ty': int(num(uv.get('tilesY', 1))), 'anim': int(num(uv.get('animationType', 0))),
                   'row': int(num(uv.get('rowIndex', 0))), 'rowMode': int(num(uv.get('rowMode', 1))), 'fot': mmc(uv.get('frameOverTime')),
                   'sf': mmc(uv.get('startFrame')), 'cycles': R(num(uv.get('cycles', 1))), 'time': int(num(uv.get('timeMode', 0))), 'fps': R(num(uv.get('fps', 30)))}
    mats = psr.get('m_Materials') or []
    mat = material(mats[0] if mats else None, tiles)
    if mat is None: return None
    rm = int(num(psr.get('m_RenderMode', 0)))
    rr = {'mode': rm, 'align': int(num(psr.get('m_RenderAlignment', 0))), 'sort': int(num(psr.get('m_SortMode', 0))), 'fudge': R(num(psr.get('m_SortingFudge', 0))),
          'order': int(num(psr.get('m_SortingOrder', 0))), 'minS': R(num(psr.get('m_MinParticleSize', 0))), 'maxS': R(num(psr.get('m_MaxParticleSize', 0.5))),
          'len': R(num(psr.get('m_LengthScale', 2))), 'vs': R(num(psr.get('m_VelocityScale', 0))), 'pivot': R(v3(psr.get('m_Pivot'))), 'flip': R(v3(psr.get('m_Flip'))),
          'mat': mat}
    if rm == 4:
        rr['mesh'] = mesh_id(psr.get('m_Mesh')) or 'builtin:quad'
    o['r'] = rr
    return o

def export(prefab):
    objs, fidmap, root = build(prefab)
    tf_of_go = {}
    for k, o in objs.items():
        if o['name'] in ('Transform', 'RectTransform') and o.get('go'): tf_of_go[o['go']] = k
    world = {}
    def wm(k, depth=0):
        if k in world: return world[k]
        o = objs.get(k)
        if o is None or depth > 64: return trs([0, 0, 0], (0, 0, 0, 1), [1, 1, 1])
        d = o['d']
        p = v3(d.get('m_LocalPosition')); q = q4(d.get('m_LocalRotation')); s = v3(d.get('m_LocalScale'), 1)
        if o.get('father') is None or o.get('father') not in objs: p = [0, 0, 0]
        m = trs(p, q, s)
        if o.get('father') in objs: m = mmul(wm(o['father'], depth + 1), m)
        world[k] = m
        return m
    def active(gok):
        tk = tf_of_go.get(gok)
        while tk is not None:
            to = objs.get(tk)
            if to is None: break
            go = objs.get(to.get('go'))
            if go and not num(go['d'].get('m_IsActive', 1)): return False
            tk = to.get('father')
        return True
    order = []
    def walk(tk):
        to = objs[tk]; order.append(to.get('go'))
        kids = [k for k, o in objs.items() if o['name'] in ('Transform', 'RectTransform') and o.get('father') == tk]
        rootorder = lambda k: num(objs[k]['d'].get('m_RootOrder', 0))
        children = (to['d'].get('m_Children') or [])
        for k in sorted(kids, key=rootorder): walk(k)
    if root: walk(root)
    comps = {}
    for k, o in objs.items():
        if o['name'] in ('ParticleSystem', 'ParticleSystemRenderer') and o.get('go'): comps.setdefault(o['go'], {})[o['name']] = o['d']
    systems = []
    for gok in order:
        c = comps.get(gok)
        if not c or 'ParticleSystem' not in c or 'ParticleSystemRenderer' not in c: continue
        if not active(gok) or not num(c['ParticleSystemRenderer'].get('m_Enabled', 1)): continue
        tk = tf_of_go[gok]; m = wm(tk)
        lossy = (math.sqrt(sum(m[i][0] ** 2 for i in range(3))) + math.sqrt(sum(m[i][1] ** 2 for i in range(3))) + math.sqrt(sum(m[i][2] ** 2 for i in range(3)))) / 3
        ls = v3(objs[tk]['d'].get('m_LocalScale'), 1)
        name = objs[gok]['d'].get('m_Name', '') if gok in objs else ''
        try:
            s = ps_export(c['ParticleSystem'], c['ParticleSystemRenderer'], m, lossy, sum(ls) / 3, name)
        except Exception as e:
            print('  ! system', name, e); s = None
        if s: systems.append(s)
    return systems

if __name__ == '__main__':
    man, out = sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    txt = open(man).read()
    items = re.findall(r'id: "([^"]+)".*?prefab: "([^"]+)"', txt)
    only = sys.argv[4:] if len(sys.argv) > 4 else None
    for iid, prefab in items:
        if only and not any(o in iid for o in only): continue
        TEX.clear(); TEXDATA.clear(); TEXNAME.clear(); MESH.clear(); MESHDATA.clear()
        try:
            systems = export(prefab)
        except Exception as e:
            import traceback; traceback.print_exc(); print('FAILED', prefab); continue
        if not systems: print('skip (no systems)', prefab); continue
        loop = any(s['main']['loop'] for s in systems)
        data = {'v': 1, 'gamma': 1, 'loop': int(loop), 'sys': systems, 'tex': dict(TEXDATA), 'texName': dict(TEXNAME), 'mesh': dict(MESHDATA)}
        js = '(window.FLAREBOX_FX3D=window.FLAREBOX_FX3D||{})[%s]=%s;\n' % (json.dumps(iid), json.dumps(data, separators=(',', ':')))
        open(os.path.join(out, iid + '.js'), 'w').write(js)
        print('%-70s %3d sys %4d KB  tex %d mesh %d' % (prefab[-70:], len(systems), len(js) // 1024, len(TEXDATA), len(MESHDATA)))
    # index of effects that have 3D data, so the web page never probes for missing files
    ids = sorted(f[:-3] for f in os.listdir(out) if f.endswith('.js') and not f.startswith('_'))
    base = 'vfx/' + os.path.basename(os.path.dirname(os.path.abspath(out))) + '/'
    open(os.path.join(out, '_index.js'), 'w').write('(window.FLAREBOX_FX3D_INDEX=window.FLAREBOX_FX3D_INDEX||{})[%s]=%s;\n' % (json.dumps(base), json.dumps(ids)))
    print('index: %d effects with 3D data -> %s' % (len(ids), os.path.join(out, '_index.js')))
