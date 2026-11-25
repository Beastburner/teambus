"""Read-only code index for the Memory view: a file tree, safe file reads, and diagrams built from the real code (stdlib only).

The hub never writes to the repo. Everything here is bounded (file count, file size, call depth) and refuses secrets, binaries and
anything that resolves outside the repo root. Diagrams come back as plain data (nodes/edges or participants/messages) plus Mermaid text,
so the page can draw them itself and the user can paste the text into any Mermaid renderer.
"""
import ast, os, re, time

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", ".next", ".nuxt", ".cache", "target", ".idea", ".vscode",
             ".mypy_cache", ".pytest_cache", ".tox", "site-packages", "coverage", ".gradle", "Pods", ".dart_tool", "vendor"}
SECRET_RE = re.compile(r"(^\.env|\.pem$|\.key$|\.p12$|\.pfx$|^id_(rsa|dsa|ecdsa|ed25519)|^tokens?(\.|$)|credential|secret|\.sqlite3?$|\.db$|\.keystore$|^\.npmrc$|^\.netrc$)", re.I)
TEXT_EXT = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".html", ".css", ".scss", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".bash",
            ".sql", ".go", ".rs", ".java", ".kt", ".swift", ".dart", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".vue", ".svelte", ".xml", ".gradle", ".proto", ".graphql"}
TEXT_NAMES = {"Dockerfile", "Makefile", "LICENSE", "README", "Procfile", "CLAUDE.md"}
LANG = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".sql": "sql", ".md": "markdown",
        ".html": "html", ".css": "css", ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".sh": "shell", ".go": "go", ".rs": "rust", ".java": "java"}
MAX_FILES, MAX_READ, MAX_PARSE, MAX_DEPTH, MAX_MSGS = 3000, 300_000, 400_000, 3, 40


class CodeError(Exception):
    def __init__(s, msg, status=400): super().__init__(msg); s.status = status


def _ext(p): return os.path.splitext(p)[1].lower()
def _is_text_name(n): return _ext(n) in TEXT_EXT or n in TEXT_NAMES


class Repo:
    def __init__(s, root):
        s.root = os.path.realpath(root); s._files = None; s._sig = None; s._an = None

    # ---- listing and reading ----
    def files(s):
        """[(relpath, size, mtime)] sorted, capped; skips noise dirs, secrets and non-text names."""
        out = []
        for dp, dns, fns in os.walk(s.root):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith(".") or d == ".github")
            for f in sorted(fns):
                if SECRET_RE.search(f) or not _is_text_name(f): continue
                full = os.path.join(dp, f)
                try:
                    if os.path.islink(full): continue
                    st = os.stat(full)
                except OSError: continue
                out.append((os.path.relpath(full, s.root).replace(os.sep, "/"), st.st_size, st.st_mtime))
                if len(out) >= MAX_FILES: return out
        return out

    def tree(s):
        fl = s.files()
        return {"root": os.path.basename(s.root), "truncated": len(fl) >= MAX_FILES,
                "files": [{"path": p, "size": z, "lang": LANG.get(_ext(p), "")} for p, z, _ in fl]}

    def safe(s, rel):
        if not isinstance(rel, str) or not rel or "\0" in rel or rel.startswith(("/", "\\")): raise CodeError("bad path")
        full = os.path.realpath(os.path.join(s.root, rel))
        if os.path.commonpath([full, s.root]) != s.root or full == s.root: raise CodeError("outside the repo", 403)
        parts = os.path.relpath(full, s.root).split(os.sep)
        if any(p in SKIP_DIRS or (p.startswith(".") and p != ".github") for p in parts[:-1]) or SECRET_RE.search(parts[-1]) or not _is_text_name(parts[-1]): raise CodeError("not available", 403)
        if not os.path.isfile(full): raise CodeError("no such file", 404)
        return full

    def read(s, rel):
        full = s.safe(rel); size = os.path.getsize(full)
        with open(full, "rb") as f: raw = f.read(MAX_READ + 1)
        if b"\0" in raw[:4096]: raise CodeError("binary file", 415)
        cut = len(raw) > MAX_READ
        return {"path": rel, "lang": LANG.get(_ext(rel), ""), "size": size, "truncated": cut, "text": raw[:MAX_READ].decode("utf-8", "replace")}

    # ---- analysis ----
    def analysis(s):
        fl = s.files(); sig = (len(fl), max((m for _, _, m in fl), default=0))
        if s._an is None or s._sig != sig: s._an = _analyze(s, fl); s._sig = sig
        return s._an

    def diagram(s, kind, entry=""):
        a = s.analysis()
        if kind == "flow": return _flow(a)
        if kind == "er": return _er(a)
        if kind == "classes": return _classes(a)
        if kind == "calls": return _calls(a, entry)
        raise CodeError("unknown diagram", 404)

    def graph(s):
        """Obsidian-style map: every file is a node; edges are imports, doc links / [[wikilinks]] and the tables files touch."""
        a = s.analysis(); edges = []
        for p, m in a["mods"].items(): edges += [{"from": p, "to": q, "kind": "import"} for q in m["imports"]]
        edges += [{"from": p, "to": q, "kind": "link"} for p, q in a["links"]]
        tb = a["tables"]
        edges += [{"from": t["file"], "to": "table:" + n, "kind": "table"} for n, t in tb.items()]
        edges += [{"from": p, "to": "table:" + n, "kind": "uses"} for p, n in a["uses"]]
        seen = set(); uniq = []
        for e in edges:
            k = (e["from"], e["to"], e["kind"])
            if k not in seen and e["from"] != e["to"]: seen.add(k); uniq.append(e)
        deg = {}
        for e in uniq: deg[e["from"]] = deg.get(e["from"], 0) + 1; deg[e["to"]] = deg.get(e["to"], 0) + 1
        paths = list(a["sizes"]); keep = set(sorted(paths, key=lambda p: (-deg.get(p, 0), p))[:700]) | {"table:" + n for n in tb}
        nodes = [{"id": p, "label": os.path.basename(p), "dir": os.path.dirname(p), "lang": LANG.get(_ext(p), ""), "size": a["sizes"][p], "deg": deg.get(p, 0), "kind": "file"} for p in paths if p in keep]
        nodes += [{"id": "table:" + n, "label": n, "dir": "", "lang": "", "size": 0, "deg": deg.get("table:" + n, 0), "kind": "table", "file": t["file"]} for n, t in tb.items()]
        ids = {n["id"] for n in nodes}
        return {"nodes": nodes, "edges": [e for e in uniq if e["from"] in ids and e["to"] in ids], "truncated": len(paths) > 700}

    def design_md(s, extra=None):
        a = s.analysis(); parts = [f"# Design notes: {os.path.basename(s.root)}", f"_Generated by TeamBus from the code on {time.strftime('%Y-%m-%d %H:%M')}. Mermaid blocks render on GitHub, GitLab and most docs tools._", ""]
        st = a["stats"]
        parts += ["## At a glance", f"- {st['files']} source files, {st['python']} Python, {st['js']} JS/TS", f"- {st['classes']} classes, {st['functions']} functions, {st['routes']} routes, {st['tables']} tables", ""]
        for title, kind in (("Module flow", "flow"), ("Classes and inheritance", "classes"), ("Data model (ER)", "er"), ("Request flow", "calls")):
            try: d = s.diagram(kind)
            except CodeError: continue
            if not d.get("mermaid"): continue
            parts += [f"## {title}", d.get("note", ""), "```mermaid", d["mermaid"], "```", ""]
        if a["routes"]:
            parts += ["## Routes", "| Method | Path | Handler | File |", "|---|---|---|---|"] + [f"| {r['method']} | `{r['path']}` | `{r['handler']}` | {r['file']}:{r['line']} |" for r in a["routes"][:200]] + [""]
        if a["tables"]:
            parts += ["## Tables (DDL as written in the code)"] + [f"### {t['name']}\n_{t['file']}_\n```sql\n{t['ddl']}\n```" for t in a["tables"].values()] + [""]
        for title, rows in (extra or {}).items():
            if rows: parts += [f"## {title}"] + [f"- {r}" for r in rows] + [""]
        return "\n".join(parts)


def _analyze(repo, fl):
    paths = {p for p, _, _ in fl}; mods = {}; tables = {}; routes = []; classes = []
    stats = {"files": len(fl), "python": 0, "js": 0, "classes": 0, "functions": 0, "routes": 0, "tables": 0}
    texts = {}
    for p, z, _ in fl:
        if z > MAX_PARSE or _ext(p) not in {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".sql", ".sh", ".md", ".html", ".vue", ".svelte"}: continue
        try:
            with open(os.path.join(repo.root, p), "rb") as f: raw = f.read(MAX_PARSE)
        except OSError: continue
        if b"\0" in raw[:4096]: continue
        texts[p] = raw.decode("utf-8", "replace")
    for p, t in texts.items():
        e = _ext(p)
        if e == ".py": stats["python"] += 1; mods[p] = _py(p, t, paths)
        elif e in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}: stats["js"] += 1; mods[p] = _js(p, t, paths)
        else: mods[p] = {"imports": [], "funcs": {}, "classes": [], "routes": [], "from": {}, "alias": {}}
        _tables(_py_sql(t) if e == ".py" else t, p, tables)
    for p, m in mods.items():
        stats["functions"] += len(m["funcs"]); stats["routes"] += len(m["routes"]); routes += m["routes"]
        for c in m["classes"]: classes.append({**c, "file": p}); stats["classes"] += 1
        for c in m.get("orm", []): _orm_table(c, p, tables)
    stats["tables"] = len(tables)
    sizes = {p: z for p, z, _ in fl}; stems = {}
    for p in sorted(paths):
        for k in (os.path.splitext(os.path.basename(p))[0].lower(), os.path.basename(p).lower()): stems.setdefault(k, p)
    links = []
    for p, t in texts.items():
        if _ext(p) in {".md", ".html", ".vue", ".svelte"}: links += [(p, q) for q in _links(p, t, paths, stems) if q != p]
    uses = []
    for name, tb in tables.items():
        if len(name) < 4: continue
        rx = re.compile(r"\b" + re.escape(name) + r"\b")
        for p, t in texts.items():
            if p != tb["file"] and _ext(p) in {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".sql"} and rx.search(t): uses.append((p, name))
    imported_by = {}
    for p, m in mods.items():
        for q in m["imports"]: imported_by.setdefault(q, []).append(p)
    return {"mods": mods, "tables": tables, "routes": sorted(routes, key=lambda r: (r["file"], r["line"])), "classes": classes, "imported_by": imported_by, "stats": stats, "sizes": sizes, "links": links, "uses": uses[:500]}


def _links(p, t, paths, stems):
    """Files a markdown/HTML file points at: [text](path), href/src="path" and Obsidian-style [[wikilinks]]."""
    out = set(); base = os.path.dirname(p)
    for m in re.finditer(r"""(?:\]\(|href=["']|src=["'])([^)"'\s#?]+)""", t):
        spec = m.group(1)
        if re.match(r"^[a-z][a-z0-9+.-]*:|^//", spec, re.I): continue
        for root in (base, ""):
            c = os.path.normpath(os.path.join(root, spec.lstrip("/") if root == "" else spec)).replace(os.sep, "/")
            if c in paths: out.add(c); break
    for m in re.finditer(r"\[\[([^\]|#]+)", t):
        q = stems.get(m.group(1).strip().lower())
        if q: out.add(q)
    return out


# ---------- Python ----------
def _resolve_py(rel, dotted, level, paths):
    base = os.path.dirname(rel)
    if level:
        for _ in range(level - 1): base = os.path.dirname(base)
        roots = [base]
    else: roots = [base, ""]
    sub = dotted.replace(".", "/") if dotted else ""
    for r in roots:
        for cand in ((os.path.join(r, sub) + ".py"), os.path.join(r, sub, "__init__.py")) if sub else (os.path.join(r, "__init__.py"),):
            c = cand.replace(os.sep, "/").lstrip("/")
            if c in paths: return c
    return None


def _py(p, text, paths):
    out = {"imports": [], "funcs": {}, "classes": [], "routes": [], "from": {}, "alias": {}, "orm": []}
    try: tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError): return out
    imps = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                f = _resolve_py(p, a.name, 0, paths)
                if f and f != p: imps.add(f); out["alias"][a.asname or a.name.split(".")[0]] = f
        elif isinstance(n, ast.ImportFrom):
            f = _resolve_py(p, n.module or "", n.level, paths)
            for a in n.names:
                sub = _resolve_py(p, ((n.module + ".") if n.module else "") + a.name, n.level, paths)
                if sub and sub != p: imps.add(sub); out["alias"][a.asname or a.name] = sub
                elif f and f != p: imps.add(f); out["from"][a.asname or a.name] = (f, a.name)
    out["imports"] = sorted(imps)

    def calls_of(fn):
        cs = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name): cs.append((n.lineno, n.col_offset, "name", f.id, ""))
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name): cs.append((n.lineno, n.col_offset, "self" if f.value.id in ("self", "cls") else "attr", f.value.id, f.attr))
        return [c[2:] for c in sorted(cs)]

    def route_of(fn, qual):
        for d in fn.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post", "put", "delete", "patch", "route", "custom_route", "websocket", "api_route") and d.args \
               and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
                meth = d.func.attr.upper() if d.func.attr in ("get", "post", "put", "delete", "patch") else "ANY"
                for kw in d.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)): meth = "/".join(e.value for e in kw.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
                out["routes"].append({"method": meth, "path": d.args[0].value, "handler": qual, "file": p, "line": fn.lineno})

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out["funcs"][node.name] = {"line": node.lineno, "calls": calls_of(node)}; route_of(node, node.name)
        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases][:4]; meths = []; fields = []
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{node.name}.{b.name}"; out["funcs"][q] = {"line": b.lineno, "calls": calls_of(b)}; meths.append(b.name); route_of(b, q)
                elif isinstance(b, ast.Assign) and len(b.targets) == 1 and isinstance(b.targets[0], ast.Name): fields.append((b.targets[0].id, b.value))
            c = {"name": node.name, "bases": bases, "methods": meths[:12], "line": node.lineno}
            out["classes"].append(c)
            if any(re.search(r"(Base|Model|DeclarativeBase)$", b) for b in bases): out["orm"].append({"name": node.name, "fields": fields})
    # Route("/x", handler) lists (Starlette) and add_url_rule-style calls
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Route" and len(n.args) >= 2 and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            h = n.args[1]; hn = h.id if isinstance(h, ast.Name) else ast.unparse(h)[:40]
            meth = "ANY"
            for kw in n.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)): meth = "/".join(e.value for e in kw.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
            out["routes"].append({"method": meth, "path": n.args[0].value, "handler": hn, "file": p, "line": n.lineno})
    return out


# ---------- JS / TS (imports only) ----------
JS_IMP = re.compile(r"""(?:import\s+(?:[^'"]*?\s+from\s+)?|export\s+[^'"]*?\s+from\s+|require\(\s*|import\(\s*)['"]([^'"]+)['"]""")


def _js(p, text, paths):
    out = {"imports": [], "funcs": {}, "classes": [], "routes": [], "from": {}, "alias": {}}
    base = os.path.dirname(p); imps = set()
    for m in JS_IMP.finditer(text):
        spec = m.group(1)
        if not spec.startswith("."): continue
        t = os.path.normpath(os.path.join(base, spec)).replace(os.sep, "/")
        for cand in [t] + [t + e for e in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")] + [t + "/index" + e for e in (".ts", ".tsx", ".js", ".jsx")]:
            if cand in paths and cand != p: imps.add(cand); break
    out["imports"] = sorted(imps)
    for m in re.finditer(r"^\s*(?:export\s+)?class\s+(\w+)(?:\s+extends\s+([\w.]+))?", text, re.M): out["classes"].append({"name": m.group(1), "bases": [m.group(2)] if m.group(2) else [], "methods": [], "line": text.count("\n", 0, m.start()) + 1})
    for m in re.finditer(r"""\b(?:app|router|server)\.(get|post|put|delete|patch)\(\s*['"`]([^'"`]+)['"`]""", text): out["routes"].append({"method": m.group(1).upper(), "path": m.group(2), "handler": "(handler)", "file": p, "line": text.count("\n", 0, m.start()) + 1})
    return out


def _py_sql(text):
    """SQL text hidden in Python: string constants (so implicit concatenation is joined) and {table: "col defs"} schema dicts."""
    try: tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError): return text
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.search(r"create\s+table|alter\s+table", n.value, re.I): out.append(n.value + ";")
        elif isinstance(n, ast.Dict) and n.keys and len(n.keys) <= 80 and all(isinstance(k, ast.Constant) and isinstance(k.value, str) and re.fullmatch(r"\w+", k.value) for k in n.keys if k is not None) \
                and all(isinstance(v, ast.Constant) and isinstance(v.value, str) and re.match(r"\s*\w+\s+(INTEGER|INT|TEXT|REAL|BLOB|NUMERIC|VARCHAR|BOOLEAN)\b", v.value, re.I) for v in n.values):
            out += [f"CREATE TABLE {k.value}({v.value});" for k, v in zip(n.keys, n.values)]
    return "\n".join(out)


# ---------- tables (SQL in .sql files or strings in code, and ORM models) ----------
CT = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?(\w+)[`\"\]]?\s*\(", re.I)
CONSTRAINT = re.compile(r"(primary\s+key|unique|check|constraint|foreign\s+key|key|index|exclude)\b", re.I)


def _split_top(body):
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(": depth += 1
        elif ch == ")": depth -= 1
        if ch == "," and depth == 0: parts.append("".join(cur)); cur = []
        else: cur.append(ch)
    parts.append("".join(cur)); return parts


def _tables(text, p, tables):
    for m in CT.finditer(text):
        i = m.end(); depth = 1; j = i
        while j < len(text) and depth:
            depth += (text[j] == "(") - (text[j] == ")"); j += 1
        if depth or j - i > 6000: continue
        name = m.group(1)
        if name in tables: continue
        cols, fks = [], []
        for part in _split_top(text[i:j - 1]):
            part = " ".join(part.split()).strip()
            if not part: continue
            if CONSTRAINT.match(part):
                fk = re.search(r"foreign\s+key\s*\(([^)]*)\)\s*references\s+[`\"]?(\w+)", part, re.I)
                if fk: fks.append(([c.strip(" `\"") for c in fk.group(1).split(",")], fk.group(2)))
                pk = re.match(r"primary\s+key\s*\(([^)]*)\)", part, re.I)
                if pk:
                    for c in cols:
                        if c["name"] in [x.strip(" `\"") for x in pk.group(1).split(",")]: c["pk"] = True
                continue
            toks = part.split(" ")
            col = {"name": toks[0].strip("`\"[]"), "type": (toks[1] if len(toks) > 1 else "").strip(",").lower()[:20], "pk": bool(re.search(r"primary\s+key", part, re.I)), "fk": None}
            r = re.search(r"references\s+[`\"]?(\w+)", part, re.I)
            if r: col["fk"] = r.group(1)
            if re.match(r"^[A-Za-z_]\w*$", col["name"]): cols.append(col)
        for cs, tgt in fks:
            for c in cols:
                if c["name"] in cs and not c["fk"]: c["fk"] = tgt
        if cols: tables[name] = {"name": name, "file": p, "cols": cols, "ddl": " ".join(text[m.start():j].split())[:1500] + (";" if True else "")}
    for m in re.finditer(r"alter\s+table\s+[`\"]?(\w+)[`\"]?\s+add\s+(?:column\s+)?[`\"]?(\w+)[`\"]?\s+(\w+)", text, re.I):
        t = tables.get(m.group(1))
        if t and not any(c["name"] == m.group(2) for c in t["cols"]): t["cols"].append({"name": m.group(2), "type": m.group(3).lower()[:20], "pk": False, "fk": None})


def _orm_table(c, p, tables):
    cols = []; name = None
    for fname, val in c["fields"]:
        if fname == "__tablename__" and isinstance(val, ast.Constant): name = str(val.value)
    for fname, val in c["fields"]:
        if not isinstance(val, ast.Call): continue
        fn = ast.unparse(val.func).split(".")[-1]
        if fn not in ("Column", "mapped_column", "ForeignKey", "OneToOneField", "CharField", "IntegerField", "TextField", "BooleanField", "DateTimeField", "FloatField", "JSONField", "UUIDField", "DateField", "DecimalField", "AutoField", "BigAutoField", "EmailField", "SlugField"): continue
        typ, fk, pk = fn.replace("Field", "").lower(), None, False
        for a in val.args:
            if isinstance(a, ast.Call) and ast.unparse(a.func).endswith("ForeignKey") and a.args and isinstance(a.args[0], ast.Constant): fk = str(a.args[0].value).split(".")[0]
            elif isinstance(a, (ast.Name, ast.Attribute)) and typ in ("column", "mapped_column"): typ = ast.unparse(a).split(".")[-1].lower()[:20]
        if fn in ("ForeignKey", "OneToOneField") and val.args:
            a0 = val.args[0]; fk = (a0.value if isinstance(a0, ast.Constant) else ast.unparse(a0)).split(".")[0].lower(); typ = "fk"
        for kw in val.keywords:
            if kw.arg == "primary_key" and isinstance(kw.value, ast.Constant) and kw.value.value: pk = True
        cols.append({"name": fname, "type": typ, "pk": pk, "fk": fk})
    if cols:
        t = name or c["name"].lower()
        if t not in tables: tables[t] = {"name": t, "file": p, "cols": cols, "ddl": f"-- ORM model {c['name']} ({p})"}


# ---------- diagrams ----------
def _id(prefix, i): return f"{prefix}{i}"
def _lab(s): return re.sub(r"[^\w .\-/]", "", s)[:40]


def _flow(a):
    deg = {}
    for p, m in a["mods"].items():
        for q in m["imports"]: deg[p] = deg.get(p, 0) + 1; deg[q] = deg.get(q, 0) + 1
    keep = sorted(deg, key=lambda k: (-deg[k], k))[:60]; ks = set(keep)
    idx = {p: i for i, p in enumerate(keep)}
    nodes = [{"id": _id("n", i), "label": os.path.basename(p), "path": p, "lines": [os.path.dirname(p) or "."]} for p, i in idx.items()]
    edges = [{"from": _id("n", idx[p]), "to": _id("n", idx[q]), "label": ""} for p in keep for q in a["mods"][p]["imports"] if q in ks]
    mer = ["flowchart LR"] + [f'  {n["id"]}["{_lab(n["path"])}"]' for n in nodes] + [f'  {e["from"]} --> {e["to"]}' for e in edges]
    note = "Each arrow means: this file imports that one." + (" Showing the 60 most connected files." if len(deg) > 60 else "")
    return {"kind": "flow", "nodes": nodes, "edges": edges, "mermaid": "\n".join(mer) if nodes else "", "note": note if nodes else "No imports between source files were found."}


def _classes(a):
    cl = [c for c in a["classes"]][:60]; names = {c["name"] for c in cl}
    nodes = [{"id": _id("c", i), "label": c["name"], "path": c["file"], "lines": [m + "()" for m in c["methods"][:8]] or [""]} for i, c in enumerate(cl)]
    ix = {c["name"]: _id("c", i) for i, c in enumerate(cl)}
    edges = [{"from": ix[b.split(".")[-1]], "to": ix[c["name"]], "label": "extends"} for c in cl for b in c["bases"] if b.split(".")[-1] in names and b.split(".")[-1] != c["name"]]
    mer = ["classDiagram"]
    for c in cl:
        mer.append(f"  class {re.sub(r'[^A-Za-z0-9_]', '_', c['name'])} {{")
        mer += [f"    +{re.sub(r'[^A-Za-z0-9_]', '_', m)}()" for m in c["methods"][:8]]
        mer.append("  }")
    for e in edges:
        f = next(c["name"] for c in cl if ix[c["name"]] == e["from"]); t = next(c["name"] for c in cl if ix[c["name"]] == e["to"])
        mer.append(f"  {re.sub(r'[^A-Za-z0-9_]', '_', f)} <|-- {re.sub(r'[^A-Za-z0-9_]', '_', t)}")
    return {"kind": "classes", "nodes": nodes, "edges": edges, "mermaid": "\n".join(mer) if cl else "", "note": "Arrow points from a class to the classes that extend it." if cl else "No classes found."}


def _fk_target(c, t, names):
    """(table, inferred): a declared foreign key, else a guess from a column named <table>_id (or <table>s_id)."""
    if c["fk"] and c["fk"] in names and c["fk"] != t["name"]: return c["fk"], False
    m = re.fullmatch(r"(\w+?)_id", c["name"])
    if m and not c["pk"]:
        for cand in (m.group(1), m.group(1) + "s", m.group(1) + "es"):
            if cand in names and cand != t["name"]: return cand, True
    return None, False


def _er(a):
    tb = list(a["tables"].values())[:60]; names = {t["name"] for t in tb}; ix = {t["name"]: _id("t", i) for i, t in enumerate(tb)}
    nodes = [{"id": ix[t["name"]], "label": t["name"], "path": t["file"], "lines": [("PK " if c["pk"] else ("FK " if _fk_target(c, t, names)[0] else "")) + f'{c["name"]} {c["type"]}'.strip() for c in t["cols"][:14]] + (["…"] if len(t["cols"]) > 14 else [])} for t in tb]
    edges = []
    for t in tb:
        for c in t["cols"]:
            tgt, inf = _fk_target(c, t, names)
            if tgt: edges.append({"from": ix[tgt], "to": ix[t["name"]], "label": c["name"] + (" ?" if inf else ""), "dashed": inf})
    mer = ["erDiagram"]
    san = lambda s: re.sub(r"[^A-Za-z0-9_]", "_", s)
    for t in tb:
        mer.append(f"  {san(t['name'])} {{")
        for c in t["cols"][:25]: mer.append(f"    {san(c['type'] or 'text')} {san(c['name'])}" + (" PK" if c["pk"] else (" FK" if _fk_target(c, t, names)[0] else "")))
        mer.append("  }")
    for t in tb:
        for c in t["cols"]:
            tgt, inf = _fk_target(c, t, names)
            if tgt: mer.append(f'  {san(tgt)} ||--o{{ {san(t["name"])} : "{san(c["name"])}{" (inferred)" if inf else ""}"')
    return {"kind": "er", "nodes": nodes, "edges": edges, "mermaid": "\n".join(mer) if tb else "", "note": "Found by reading CREATE TABLE statements and ORM models in the code." if tb else "No tables found (looked for CREATE TABLE in code and .sql files, and SQLAlchemy/Django models)."}


def _trace(mods, start):
    """Follow calls from `start` through functions the repo defines: (messages, participant files)."""
    msgs, parts = [], [start[0]]

    def resolve(f, cur_fn, call):
        m = mods[f]; kind, a1, a2 = call
        if kind == "name":
            if a1 in m["funcs"]: return f, a1
            if a1 in m["from"]:
                g, orig = m["from"][a1]
                if orig in mods.get(g, {}).get("funcs", {}): return g, orig
        elif kind == "attr":
            g = m["alias"].get(a1)
            if g and a2 in mods.get(g, {}).get("funcs", {}): return g, a2
        elif kind == "self" and "." in cur_fn:
            q = cur_fn.split(".")[0] + "." + a2
            if q in m["funcs"]: return f, q
        return None

    def walk(f, fn, depth, seen):
        if depth > MAX_DEPTH or len(msgs) >= MAX_MSGS: return
        for call in mods[f]["funcs"][fn]["calls"]:
            if len(msgs) >= MAX_MSGS: return
            t = resolve(f, fn, call)
            if not t or t in seen: continue
            if t[0] not in parts: parts.append(t[0])
            msgs.append({"from": f, "to": t[0], "label": t[1] + "()", "depth": depth})
            walk(t[0], t[1], depth + 1, seen | {t})
    walk(start[0], start[1], 1, {start})
    return msgs, parts


def _calls(a, entry):
    """Sequence of calls starting at an entry function (default: the first route handler, else main), followed through functions the repo defines."""
    mods = a["mods"]; start = None
    if entry and ":" in entry:
        f, fn = entry.split(":", 1)
        if f in mods and fn in mods[f]["funcs"]: start = (f, fn)
    if not start:
        best = (-1, -1)
        for r in a["routes"][:80]:
            if r["handler"] in mods.get(r["file"], {}).get("funcs", {}):
                ms, ps = _trace(mods, (r["file"], r["handler"])); n = (len(ps), len(ms))
                if n > best: best, start = n, (r["file"], r["handler"])
    if not start:
        for f, m in mods.items():
            if "main" in m["funcs"]: start = (f, "main"); break
    if not start: return {"kind": "calls", "participants": [], "messages": [], "mermaid": "", "note": "No entry point found (no route handler or main())."}
    msgs, parts = _trace(mods, start)
    pid = {p: f"P{i}" for i, p in enumerate(parts)}
    mer = ["sequenceDiagram"] + [f"  participant {pid[p]} as {_lab(os.path.basename(p))}" for p in parts] + [f"  {pid[m['from']]}->>{pid[m['to']]}: {_lab(m['label'])}" for m in msgs]
    return {"kind": "calls", "entry": f"{start[0]}:{start[1]}", "participants": [{"id": pid[p], "label": os.path.basename(p), "path": p} for p in parts],
            "messages": [{**m, "from": pid[m["from"]], "to": pid[m["to"]]} for m in msgs], "mermaid": "\n".join(mer),
            "note": f"Calls followed from {start[1]}() in {start[0]}, up to {MAX_DEPTH} levels, only through functions defined in this repo." + ("" if msgs else " It calls nothing else defined in the repo.")}


def entries(a):
    """Entry points the page offers for the call sequence: routes first, then main()."""
    out = [{"id": f"{r['file']}:{r['handler']}", "label": f"{r['method']} {r['path']}"} for r in a["routes"] if r["handler"] in a["mods"].get(r["file"], {}).get("funcs", {})][:60]
    out += [{"id": f"{f}:main", "label": f"main() in {f}"} for f, m in a["mods"].items() if "main" in m["funcs"]][:10]
    return out


def file_info(a, rel):
    m = a["mods"].get(rel)
    if not m: return {"imports": [], "imported_by": a["imported_by"].get(rel, []), "classes": [], "functions": [], "routes": [], "tables": []}
    return {"imports": m["imports"], "imported_by": sorted(a["imported_by"].get(rel, [])), "classes": [{"name": c["name"], "line": c["line"]} for c in m["classes"]],
            "functions": [{"name": n, "line": v["line"]} for n, v in m["funcs"].items()][:80], "routes": [r for r in m["routes"]][:40],
            "tables": [t["name"] for t in a["tables"].values() if t["file"] == rel]}
