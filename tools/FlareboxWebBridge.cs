// FlareboxWebBridge.cs — receives Inspector edits from the Flarebox web page and applies them to a live
// effect inside a Unity WebGL build.
//
// Setup (Unity WebGL build):
//   1. Put this file in Assets/Scripts, add an empty GameObject named "FlareboxBridge" with this component.
//   2. Assign `effectRoot` (the instantiated VFX prefab) or call LoadEffect("<prefab name>") from JS.
//      Prefabs to load by name must live in a Resources folder.
//   3. In the page that hosts the build:  createUnityInstance(canvas, config).then(u => FlareboxBridge.connect(u));
//      (or, if Flarebox runs in a parent frame: parent.postMessage({ flareboxConnect: true, unity: u }, '*') is not
//       possible across origins — host both on the same origin and call window.parent.FlareboxBridge.connect(u)).
//
// Messages (JSON string → OnWebMessage):
//   { type:"set", system:"<ParticleSystem GameObject name>", path:["sys",i,"main","life"], value:<MinMaxCurve|number|color|gradient> }
//   { type:"set", path:["g","size"], value:2 }                    global multipliers (tint, emission, opacity, size, speed, count)
//   { type:"texture", system, slot, prop:"_BaseMap", name, dataUrl:"data:image/png;base64,..." }   (dataUrl null = clear)
//   { type:"state", state:{ sys:[...], g:{...} } }                full snapshot (undo / redo / reset / variation)
// MinMaxCurve JSON: {m:0,s} constant · {m:3,mn,s} two constants · {m:1,s,k:[32 samples]} curve · {m:2,s,k,k2} two curves
// Gradient JSON:    {m:0,c:[r,g,b,a]} · {m:2,a,b} · {m:1|4,g:[32×rgba]} · {m:3,g,g2}
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using UnityEngine;

public class FlareboxWebBridge : MonoBehaviour
{
    public GameObject effectRoot;
    readonly Dictionary<ParticleSystem, Base> baseline = new Dictionary<ParticleSystem, Base>();
    Dictionary<string, object> globalState = new Dictionary<string, object>();

    class Base { public float size, speed, rate, burst; public Color[] colors; public Material[] mats; }

    public void LoadEffect(string prefabName)
    {
        if (effectRoot) Destroy(effectRoot);
        var prefab = Resources.Load<GameObject>(prefabName);
        if (!prefab) { Debug.LogWarning("[FlareboxBridge] prefab not found in Resources: " + prefabName); return; }
        effectRoot = Instantiate(prefab); baseline.Clear();
    }

    // ------------------------------------------------------------------ entry point
    public void OnWebMessage(string json)
    {
        if (!effectRoot) { Debug.LogWarning("[FlareboxBridge] no effectRoot"); return; }
        var msg = MiniJson.Parse(json) as Dictionary<string, object>; if (msg == null) return;
        var type = msg.TryGetValue("type", out var t) ? t as string : null;
        var systems = effectRoot.GetComponentsInChildren<ParticleSystem>(true);
        foreach (var ps in systems) Capture(ps);
        if (type == "state")
        {
            var st = msg["state"] as Dictionary<string, object>;
            if (st.TryGetValue("g", out var g)) globalState = g as Dictionary<string, object>;
            var list = st["sys"] as List<object>;
            for (int i = 0; i < list.Count; i++) { var sd = list[i] as Dictionary<string, object>; var ps = Find(systems, sd.TryGetValue("name", out var n) ? n as string : null, i); if (ps) ApplySystem(ps, sd); }
            ApplyGlobal(systems); return;
        }
        if (type == "set")
        {
            var path = (msg["path"] as List<object>).Select(o => o is string s ? s : Convert.ToString(o, CultureInfo.InvariantCulture)).ToArray();
            if (path[0] == "g") { globalState[path[1]] = msg["value"]; ApplyGlobal(systems); return; }
            var ps = Find(systems, msg.TryGetValue("system", out var sn) ? sn as string : null, int.Parse(path[1]));
            if (ps) ApplyPath(ps, path.Skip(2).ToArray(), msg["value"]);
            return;
        }
        if (type == "texture")
        {
            var ps = Find(systems, msg["system"] as string, msg.TryGetValue("sysIndex", out var si) ? Convert.ToInt32(si) : -1);
            if (!ps) return;
            var prop = msg.TryGetValue("prop", out var p) && p is string ps2 ? ps2 : "_MainTex";
            var mat = ps.GetComponent<ParticleSystemRenderer>().material;
            if (msg.TryGetValue("restore", out var r) && r is bool rb && rb) { mat.SetTexture(prop, baseline[ps].mats[0].GetTexture(prop)); return; }
            var url = msg.TryGetValue("dataUrl", out var du) ? du as string : null;
            if (string.IsNullOrEmpty(url)) { mat.SetTexture(prop, null); return; }
            var tex = new Texture2D(2, 2, TextureFormat.RGBA32, true);
            tex.LoadImage(Convert.FromBase64String(url.Substring(url.IndexOf(',') + 1)));
            tex.name = msg.TryGetValue("name", out var nm) ? nm as string : "web texture";
            mat.SetTexture(prop, tex);
        }
    }

    ParticleSystem Find(ParticleSystem[] all, string name, int index)
    {
        if (!string.IsNullOrEmpty(name)) { var m = all.Where(p => p.gameObject.name == name).ToArray(); if (m.Length == 1 || index < 0) return m.FirstOrDefault(); }
        return index >= 0 && index < all.Length ? all[index] : null;
    }

    void Capture(ParticleSystem ps)
    {
        if (baseline.ContainsKey(ps)) return;
        var r = ps.GetComponent<ParticleSystemRenderer>(); var main = ps.main; var em = ps.emission;
        var mats = r ? r.sharedMaterials : new Material[0];
        baseline[ps] = new Base { size = main.startSizeMultiplier, speed = main.startSpeedMultiplier, rate = em.rateOverTimeMultiplier, burst = em.burstCount > 0 ? em.GetBurst(0).count.constant : 0,
            mats = mats, colors = mats.Select(mm => mm ? ColorOf(mm) : Color.white).ToArray() };
    }

    // ------------------------------------------------------------------ apply
    void ApplySystem(ParticleSystem ps, Dictionary<string, object> sd)
    {
        foreach (var mod in new[] { "main", "em", "shape", "size", "col", "r" })
            if (sd.TryGetValue(mod, out var mv) && mv is Dictionary<string, object> md) foreach (var kv in md) Walk(ps, new[] { mod, kv.Key }, kv.Value);
    }
    void Walk(ParticleSystem ps, string[] path, object v)
    {
        if (path[0] == "r" && path.Length >= 2 && path[1] == "mat" && v is Dictionary<string, object> mat) { foreach (var kv in mat) Walk(ps, new[] { "r", "mat", kv.Key }, kv.Value); return; }
        if (path[0] == "col" && path.Length == 2) { ApplyPath(ps, new[] { "col" }, Parent(ps, v)); return; }
        ApplyPath(ps, path, v);
    }
    object Parent(ParticleSystem ps, object v) => v;

    void ApplyPath(ParticleSystem ps, string[] p, object v)
    {
        var main = ps.main; var em = ps.emission; var sh = ps.shape;
        string a = p.Length > 0 ? p[0] : "", b = p.Length > 1 ? p[1] : "";
        try
        {
            switch (a)
            {
                case "main":
                    switch (b)
                    {
                        case "dur": bool play = ps.isPlaying; ps.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear); main.duration = F(v); if (play) ps.Play(true); break;
                        case "loop": main.loop = F(v) > 0; break;
                        case "prewarm": main.prewarm = F(v) > 0; break;
                        case "delay": main.startDelay = Curve(v); break;
                        case "life": main.startLifetime = Curve(v); break;
                        case "speed": main.startSpeed = Curve(v); break;
                        case "size": main.startSize = Curve(v); break;
                        case "size3": if (p.Length > 2) { int i = int.Parse(p[2]); if (i == 0) main.startSizeX = Curve(v); else if (i == 1) main.startSizeY = Curve(v); else main.startSizeZ = Curve(v); } break;
                        case "rot": main.startRotation = Curve(v); break;
                        case "rot3": if (p.Length > 2) { int i = int.Parse(p[2]); if (i == 0) main.startRotationX = Curve(v); else if (i == 1) main.startRotationY = Curve(v); else main.startRotationZ = Curve(v); } break;
                        case "grav": main.gravityModifier = Curve(v); break;
                        case "simSpeed": main.simulationSpeed = F(v); break;
                        case "max": main.maxParticles = Mathf.RoundToInt(F(v)); break;
                        case "color": main.startColor = Grad(v); break;
                    }
                    break;
                case "em":
                    if (b == "rate") em.rateOverTime = Curve(v);
                    if (b == "bursts" && p.Length > 3 && p[3] == "n") { int i = int.Parse(p[2]); if (i < em.burstCount) { var bu = em.GetBurst(i); bu.count = Curve(v); em.SetBurst(i, bu); } }
                    if (b == "bursts" && p.Length == 2 && v is List<object> bl) for (int i = 0; i < bl.Count && i < em.burstCount; i++) { var bd = bl[i] as Dictionary<string, object>; var bu = em.GetBurst(i); bu.count = Curve(bd["n"]); em.SetBurst(i, bu); }
                    break;
                case "shape":
                    if (b == "radius") sh.radius = F(v); if (b == "angle") sh.angle = F(v); if (b == "arc") sh.arc = F(v); if (b == "thick") sh.radiusThickness = F(v);
                    break;
                case "size": { var s = ps.sizeOverLifetime; if (b == "x") { var c = Curve(v); s.sizeMultiplier = c.curveMultiplier != 0 ? c.curveMultiplier : c.constant; } break; }
                case "col": { var c = ps.colorOverLifetime; c.color = Grad(v); break; }
                case "r":
                    var rend = ps.GetComponent<ParticleSystemRenderer>();
                    if (b == "mode") rend.renderMode = (ParticleSystemRenderMode)Mathf.RoundToInt(F(v));
                    if (b == "mat" && p.Length > 2)
                    {
                        var mat = rend.material;
                        if (p[2] == "color") { var c = Col(v); foreach (var prop in new[] { "_BaseColor", "_Color", "_TintColor" }) if (mat.HasProperty(prop)) { mat.SetColor(prop, prop == "_TintColor" ? c * .5f : c); break; } }
                        if (p[2] == "slots" && p.Length > 4 && v is List<object> st && st.Count == 4)
                        {
                            var slotMsg = p[3]; var prop = SlotProp(mat, slotMsg);
                            if (prop != null && p[4] == "st") { mat.SetTextureScale(prop, new Vector2(F(st[0]), F(st[1]))); mat.SetTextureOffset(prop, new Vector2(F(st[2]), F(st[3]))); }
                        }
                    }
                    break;
            }
        }
        catch (Exception e) { Debug.LogWarning("[FlareboxBridge] " + string.Join(".", p) + ": " + e.Message); }
    }

    string SlotProp(Material m, string slot)
    {
        var names = m.GetTexturePropertyNames();
        string Pick(string rx) => names.FirstOrDefault(n => System.Text.RegularExpressions.Regex.IsMatch(n, rx, System.Text.RegularExpressions.RegexOptions.IgnoreCase));
        switch (slot) { case "main": return Pick("^_?(BaseMap|MainTex)$") ?? Pick("main"); case "mask": return Pick("mask"); case "noise": return Pick("noise|dissol"); case "dist": return Pick("dist|flow"); }
        return null;
    }

    void ApplyGlobal(ParticleSystem[] all)
    {
        float G(string k, float d) => globalState.TryGetValue(k, out var v) ? F(v) : d;
        Color tint = Color.white; if (globalState.TryGetValue("tint", out var tv) && tv is List<object> tl) tint = new Color(F(tl[0]), F(tl[1]), F(tl[2]), 1);
        float emis = G("emission", 1), op = G("opacity", 1), size = G("size", 1), speed = G("speed", 1), count = G("count", 1);
        foreach (var ps in all)
        {
            var b = baseline[ps]; var main = ps.main; var em = ps.emission;
            main.startSizeMultiplier = b.size * size; main.startSpeedMultiplier = b.speed * speed; em.rateOverTimeMultiplier = b.rate * count;
            var rend = ps.GetComponent<ParticleSystemRenderer>(); if (!rend) continue;
            var mats = rend.materials;
            for (int i = 0; i < mats.Length; i++)
            {
                var c = b.colors[i]; c = new Color(c.r * tint.r * emis, c.g * tint.g * emis, c.b * tint.b * emis, c.a * op);
                foreach (var prop in new[] { "_BaseColor", "_Color", "_TintColor" }) if (mats[i].HasProperty(prop)) { mats[i].SetColor(prop, c); break; }
            }
        }
    }

    static Color ColorOf(Material m) { foreach (var p in new[] { "_BaseColor", "_Color", "_TintColor" }) if (m.HasProperty(p)) return m.GetColor(p); return Color.white; }

    // ------------------------------------------------------------------ JSON → Unity types
    static float F(object o) => o == null ? 0 : Convert.ToSingle(o, CultureInfo.InvariantCulture);
    static Color Col(object o) { var l = o as List<object>; return new Color(F(l[0]), F(l[1]), F(l[2]), l.Count > 3 ? F(l[3]) : 1); }
    static AnimationCurve Samples(object o)
    {
        var l = o as List<object>; var c = new AnimationCurve();
        for (int i = 0; i < l.Count; i++) c.AddKey(i / (float)(l.Count - 1), F(l[i]));
        return c;
    }
    static ParticleSystem.MinMaxCurve Curve(object o)
    {
        if (!(o is Dictionary<string, object> d)) return new ParticleSystem.MinMaxCurve(F(o));
        int m = Mathf.RoundToInt(F(d["m"])); float s = F(d["s"]);
        switch (m)
        {
            case 3: return new ParticleSystem.MinMaxCurve(F(d["mn"]), s);
            case 1: return new ParticleSystem.MinMaxCurve(s, Samples(d["k"]));
            case 2: return new ParticleSystem.MinMaxCurve(s, Samples(d["k2"]), Samples(d["k"]));
            default: return new ParticleSystem.MinMaxCurve(s);
        }
    }
    static Gradient GradFrom(object samples)
    {
        var l = (samples as List<object>).Select(F).ToArray(); int n = l.Length / 4; var g = new Gradient();
        int K = Mathf.Min(8, n); var ck = new GradientColorKey[K]; var ak = new GradientAlphaKey[K];
        for (int i = 0; i < K; i++) { int j = Mathf.RoundToInt(i * (n - 1) / (float)(K - 1)); float t = j / (float)(n - 1); ck[i] = new GradientColorKey(new Color(l[j * 4], l[j * 4 + 1], l[j * 4 + 2]), t); ak[i] = new GradientAlphaKey(l[j * 4 + 3], t); }
        g.SetKeys(ck, ak); return g;
    }
    static ParticleSystem.MinMaxGradient Grad(object o)
    {
        var d = o as Dictionary<string, object>; int m = Mathf.RoundToInt(F(d["m"]));
        switch (m)
        {
            case 0: return new ParticleSystem.MinMaxGradient(Col(d["c"]));
            case 2: return new ParticleSystem.MinMaxGradient(Col(d["a"]), Col(d["b"]));
            case 3: return new ParticleSystem.MinMaxGradient(GradFrom(d["g2"]), GradFrom(d["g"]));
            case 4: { var mg = new ParticleSystem.MinMaxGradient(GradFrom(d["g"])); mg.mode = ParticleSystemGradientMode.RandomColor; return mg; }
            default: return new ParticleSystem.MinMaxGradient(GradFrom(d["g"]));
        }
    }

    // ------------------------------------------------------------------ minimal JSON parser (objects, arrays, numbers, strings, bools, null)
    static class MiniJson
    {
        public static object Parse(string s) { int i = 0; return Val(s, ref i); }
        static void Ws(string s, ref int i) { while (i < s.Length && char.IsWhiteSpace(s[i])) i++; }
        static object Val(string s, ref int i)
        {
            Ws(s, ref i); char c = s[i];
            if (c == '{') { var d = new Dictionary<string, object>(); i++; Ws(s, ref i); if (s[i] == '}') { i++; return d; } while (true) { Ws(s, ref i); var k = (string)Str(s, ref i); Ws(s, ref i); i++; d[k] = Val(s, ref i); Ws(s, ref i); if (s[i++] == '}') return d; } }
            if (c == '[') { var l = new List<object>(); i++; Ws(s, ref i); if (s[i] == ']') { i++; return l; } while (true) { l.Add(Val(s, ref i)); Ws(s, ref i); if (s[i++] == ']') return l; } }
            if (c == '"') return Str(s, ref i);
            if (s.Substring(i).StartsWith("true")) { i += 4; return true; }
            if (s.Substring(i).StartsWith("false")) { i += 5; return false; }
            if (s.Substring(i).StartsWith("null")) { i += 4; return null; }
            int st = i; while (i < s.Length && "+-0123456789.eE".IndexOf(s[i]) >= 0) i++;
            return double.Parse(s.Substring(st, i - st), CultureInfo.InvariantCulture);
        }
        static object Str(string s, ref int i)
        {
            var sb = new StringBuilder(); i++;
            while (s[i] != '"') { if (s[i] == '\\') { i++; char e = s[i]; if (e == 'u') { sb.Append((char)Convert.ToInt32(s.Substring(i + 1, 4), 16)); i += 4; } else sb.Append(e == 'n' ? '\n' : e == 't' ? '\t' : e == 'r' ? '\r' : e == 'b' ? '\b' : e == 'f' ? '\f' : e); } else sb.Append(s[i]); i++; }
            i++; return sb.ToString();
        }
    }
}
