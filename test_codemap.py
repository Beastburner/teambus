"""The Memory view's code index: what it lists, what it must never read, and the diagrams it builds from real code."""
import os
import pytest
import codemap


@pytest.fixture()
def repo(tmp_path):
    w = lambda p, t: ((tmp_path / p).parent.mkdir(parents=True, exist_ok=True), (tmp_path / p).write_text(t))
    w("app/main.py", "from app.db import get_user\nimport app.util as util\n\n@router.get('/users')\ndef list_users():\n    util.clean()\n    return get_user()\n\nclass Base: pass\nclass Admin(Base):\n    def run(self): self.helper()\n    def helper(self): pass\n")
    w("app/db.py", "from app.util import fmt\nSQL = 'CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);' \\\n      'CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INTEGER, total REAL, FOREIGN KEY (user_id) REFERENCES users(id));'\ndef get_user(): return fmt()\n")
    w("app/util.py", "def fmt(): return 1\ndef clean(): return fmt()\n")
    w("schema.sql", "CREATE TABLE posts (id INTEGER PRIMARY KEY, author_id INTEGER, body TEXT);\nALTER TABLE posts ADD COLUMN likes INTEGER;\n")
    w("web/app.js", "import { x } from './lib.js';\nrequire('./lib');\n"); w("web/lib.js", "export const x = 1;\n")
    w("docs/a.md", "See [b](b.md) and [[c]] and [site](https://example.com).\n"); w("docs/b.md", "back to [[a]]\n"); w("docs/c.md", "# c\n")
    w(".env", "TOKEN=abc123\n"); w("tokens.json", "{}"); w("server.key", "x"); w("node_modules/x/index.js", "1"); w(".git/config", "[core]"); w("data.db", "binary")
    (tmp_path / "blob.png").write_bytes(b"\x89PNG\0\0")
    return codemap.Repo(str(tmp_path))


def paths(r): return {f["path"] for f in r.tree()["files"]}


def test_tree_lists_source_and_skips_secrets_and_noise(repo):
    p = paths(repo)
    assert {"app/main.py", "schema.sql", "docs/a.md", "web/app.js"} <= p
    assert not p & {".env", "tokens.json", "server.key", "node_modules/x/index.js", ".git/config", "data.db", "blob.png"}


@pytest.mark.parametrize("bad", [".env", "tokens.json", "server.key", ".git/config", "node_modules/x/index.js", "data.db", "../outside.py", "/etc/passwd", "app/../../x", "", "app", "blob.png"])
def test_read_refuses_secrets_escapes_and_non_text(repo, bad):
    with pytest.raises(codemap.CodeError): repo.read(bad)


def test_read_refuses_a_symlink_that_leaves_the_repo(repo, tmp_path):
    out = tmp_path.parent / "outside_secret.py"; out.write_text("TOP = 1"); os.symlink(out, tmp_path / "leak.py")
    assert "leak.py" not in paths(repo)
    with pytest.raises(codemap.CodeError): repo.read("leak.py")


def test_read_returns_text_and_caps_size(repo, tmp_path):
    d = repo.read("app/util.py"); assert d["lang"] == "python" and "def fmt" in d["text"] and not d["truncated"]
    (tmp_path / "big.py").write_text("x = 1\n" * 100_000); assert repo.read("big.py")["truncated"]


def test_imports_become_flow_edges_python_and_js(repo):
    d = repo.diagram("flow"); label = {n["id"]: n["path"] for n in d["nodes"]}; e = {(label[x["from"]], label[x["to"]]) for x in d["edges"]}
    assert ("app/main.py", "app/db.py") in e and ("app/main.py", "app/util.py") in e and ("app/db.py", "app/util.py") in e and ("web/app.js", "web/lib.js") in e
    assert d["mermaid"].startswith("flowchart LR")


def test_tables_come_from_joined_sql_strings_and_sql_files_with_relations(repo):
    t = repo.analysis()["tables"]; assert {"users", "orders", "posts"} <= set(t)
    assert next(c for c in t["orders"]["cols"] if c["name"] == "user_id")["fk"] == "users" and any(c["name"] == "likes" for c in t["posts"]["cols"])
    d = repo.diagram("er"); names = {n["id"]: n["label"] for n in d["nodes"]}
    rel = {(names[e["from"]], names[e["to"]], e.get("dashed", False)) for e in d["edges"]}
    assert ("users", "orders", False) in rel and ("users", "posts", True) is not None
    assert d["mermaid"].startswith("erDiagram") and "orders {" in d["mermaid"] and "||--o{" in d["mermaid"]


def test_classes_and_inheritance(repo):
    d = repo.diagram("classes"); names = {n["id"]: n["label"] for n in d["nodes"]}
    assert ("Base", "Admin") in {(names[e["from"]], names[e["to"]]) for e in d["edges"]} and "Base <|-- Admin" in d["mermaid"]


def test_call_sequence_follows_calls_across_files(repo):
    d = repo.diagram("calls", "app/main.py:list_users")
    assert [p["label"] for p in d["participants"]][0] == "main.py" and {"db.py", "util.py"} <= {p["label"] for p in d["participants"]}
    labels = [m["label"] for m in d["messages"]]; assert "clean()" in labels and "get_user()" in labels and "fmt()" in labels
    assert d["mermaid"].startswith("sequenceDiagram") and "->>" in d["mermaid"]
    assert repo.diagram("calls")["entry"] == "app/main.py:list_users"            # a route is the default entry point


def test_graph_links_imports_doc_links_wikilinks_and_tables(repo):
    g = repo.graph(); kinds = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
    assert ("app/main.py", "app/db.py", "import") in kinds and ("docs/a.md", "docs/b.md", "link") in kinds and ("docs/a.md", "docs/c.md", "link") in kinds and ("docs/b.md", "docs/a.md", "link") in kinds
    assert ("app/db.py", "table:users", "table") in kinds and not any(e["to"] == "https://example.com" for e in g["edges"])
    assert {n["id"] for n in g["nodes"]} >= {"app/main.py", "table:users", "docs/c.md"}


def test_design_pack_holds_every_diagram_and_ddl(repo):
    md = repo.design_md({"Decisions": ["**db**: sqlite"]})
    assert "```mermaid" in md and "erDiagram" in md and "flowchart LR" in md and "CREATE TABLE users" in md and "GET" in md and "sqlite" in md


def test_empty_repo_gives_notes_not_errors(tmp_path):
    r = codemap.Repo(str(tmp_path)); assert r.tree()["files"] == []
    for k in ("flow", "er", "classes", "calls"): assert r.diagram(k)["mermaid"] == "" and r.diagram(k)["note"]
    with pytest.raises(codemap.CodeError): r.diagram("nope")


def test_broken_python_is_skipped_not_fatal(tmp_path):
    (tmp_path / "bad.py").write_text("def (:\n"); (tmp_path / "ok.py").write_text("def a(): pass\n"); assert codemap.Repo(str(tmp_path)).analysis()["stats"]["files"] == 2


def test_text_search_is_literal_case_insensitive_and_skips_secrets(repo, tmp_path):
    r = repo.search("get_USER"); assert {h["path"] for h in r["results"]} == {"app/main.py", "app/db.py"} and all(h["line"] >= 1 and "get_user" in h["text"].lower() for h in r["results"])
    assert repo.search("abc123")["results"] == []                                   # the .env value is never indexed
    assert repo.search(".*")["results"] == [] and repo.search("def (")["results"] == []   # regex characters are plain text
    with pytest.raises(codemap.CodeError): repo.search("a")
    (tmp_path / "many.py").write_text("hit\n" * 50); assert len([h for h in repo.search("hit")["results"] if h["path"] == "many.py"]) == 3
