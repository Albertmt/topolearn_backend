"""The database layer: transactions, constraints, placeholders, reconnects,
and loading/reloading the concept graph."""
import threading
import time

import psycopg
import pytest

from app import config, db, userdb
from conftest import TEST_URL, concepts, drop_graph, edges, load_graph, new_user, run_loader


def _add_user(conn, uid, email=None):
    now = time.time()
    conn.execute(
        "INSERT INTO users (id, name, email, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (uid, uid.title(), email, now, now),
    )


# ------------------------------------------------------------ user tables


def test_schema_setup_is_repeatable_and_keeps_data():
    with userdb.session() as conn:
        _add_user(conn, "ada")
    # A new server process runs the schema setup again on the same database.
    userdb.forget_schema()
    with userdb.session() as conn:
        assert userdb.user_exists(conn, "ada")
        row = conn.execute("SELECT id, name, onboarded FROM users WHERE id = ?", ("ada",)).fetchone()
        assert row["name"] == "Ada" and row[0] == "ada" and row["onboarded"] == 0
        assert dict(row) == {"id": "ada", "name": "Ada", "onboarded": 0}


def test_concurrent_first_requests_set_up_the_schema_once():
    errors = []

    def first_request():
        try:
            with userdb.session() as conn:
                conn.execute("SELECT count(*) FROM users").fetchone()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            db.reset_connections()

    userdb.forget_schema()
    threads = [threading.Thread(target=first_request) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_error_inside_a_session_rolls_everything_back():
    with pytest.raises(RuntimeError):
        with userdb.session() as conn:
            _add_user(conn, "grace")
            raise RuntimeError("boom")
    with userdb.session() as conn:
        assert not userdb.user_exists(conn, "grace")


def test_nested_sessions_share_one_transaction():
    with userdb.session() as outer:
        _add_user(outer, "outer")
        with pytest.raises(userdb.IntegrityError):
            with userdb.session() as inner:  # a savepoint
                _add_user(inner, "outer")  # duplicate id: only the inner block fails
        _add_user(outer, "second")
    with userdb.session() as conn:
        assert userdb.user_exists(conn, "outer") and userdb.user_exists(conn, "second")


def test_duplicates_and_bad_values_raise_integrity_error():
    with userdb.session() as conn:
        _add_user(conn, "a", email="same@example.com")
    with pytest.raises(userdb.IntegrityError):
        with userdb.session() as conn:
            _add_user(conn, "b", email="same@example.com")
    with pytest.raises(userdb.IntegrityError):
        with userdb.session() as conn:
            conn.execute(
                "INSERT INTO concept_progress (user_id, concept_id, understanding, updated_at) VALUES (?,?,?,?)",
                ("a", "x", 150, time.time()),  # CHECK understanding BETWEEN 0 AND 100
            )
    with pytest.raises(userdb.IntegrityError):
        with userdb.session() as conn:
            conn.execute(
                "INSERT INTO user_goals (user_id, concept_id, created_at) VALUES (?,?,?)",
                ("nobody", "algebra", time.time()),  # unknown user
            )


def test_deleting_a_user_cascades_to_their_data():
    now = time.time()
    with userdb.session() as conn:
        _add_user(conn, "alan")
        conn.execute(
            "INSERT INTO concept_progress (user_id, concept_id, understanding, updated_at) VALUES (?,?,?,?)",
            ("alan", "algebra", 50, now),
        )
        conn.execute("INSERT INTO user_goals (user_id, concept_id, created_at) VALUES (?,?,?)", ("alan", "algebra", now))
        conn.execute(
            "INSERT INTO progress_events (user_id, concept_id, understanding, at) VALUES (?,?,?,?)",
            ("alan", "algebra", 50, now),
        )
    with userdb.session() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", ("alan",))
    with userdb.session() as conn:
        for table in ("concept_progress", "user_goals", "progress_events"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_placeholders_lists_and_literal_percent_signs():
    with userdb.session() as conn:
        _add_user(conn, "u1")
        _add_user(conn, "u2")
        conn.executemany(
            "INSERT INTO user_goals (user_id, concept_id, created_at) VALUES (?,?,?)",
            [("u1", "a", 1.0), ("u1", "b", 2.0), ("u2", "a", 3.0)],
        )
    with userdb.session() as conn:
        rows = conn.execute(
            "SELECT concept_id FROM user_goals WHERE user_id = :uid ORDER BY created_at", {"uid": "u1"}
        ).fetchall()
        assert [r[0] for r in rows] == ["a", "b"]
        assert conn.execute("SELECT count(*) FROM user_goals WHERE concept_id = ANY(?)", (["a", "zz"],)).fetchone()[0] == 2
        # '%' and '?' inside string literals are text, not placeholders.
        row = conn.execute("SELECT '100%' AS pct, 'why?' AS q, ? AS p", ("x",)).fetchone()
        assert (row["pct"], row["q"], row["p"]) == ("100%", "why?", "x")
        assert conn.execute("SELECT '50%'").fetchone()[0] == "50%"


def test_unicode_and_large_text_round_trip():
    long_name = "Ադա Լավլեյս · 数学 · 🙂 " + "x" * 50_000
    with userdb.session() as conn:
        now = time.time()
        conn.execute("INSERT INTO users (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)", ("u", long_name, now, now))
    with userdb.session() as conn:
        assert conn.execute("SELECT name FROM users WHERE id = 'u'").fetchone()["name"] == long_name


# ------------------------------------------------------------ connections


def test_a_dropped_connection_is_replaced_transparently(make_client):
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    assert c.get("/api/v1/concepts/a").status_code == 200
    # Kill every server connection (what happens when Neon suspends the database).
    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = 'topolearn-api' AND pid <> pg_backend_pid()"
        )
    # Reads outside a transaction reconnect and retry by themselves.
    assert c.get("/api/v1/concepts/a").status_code == 200
    assert c.get("/api/v1/search", params={"q": "a"}).status_code == 200
    uid = new_user(c)
    assert c.get(f"/api/v1/users/{uid}").status_code == 200


def test_stale_idle_connection_is_checked_before_use(make_client, monkeypatch):
    c = make_client({"concepts": concepts("a"), "prerequisites": []})
    assert c.get("/api/v1/concepts/a").status_code == 200
    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = 'topolearn-api' AND pid <> pg_backend_pid()"
        )
    # Pretend every connection has been idle for a long time: it gets pinged,
    # found dead, and replaced before the request's query runs.
    monkeypatch.setattr(db, "_PING_AFTER", -1.0)
    assert c.get("/api/v1/concepts/a").status_code == 200


# ------------------------------------------------------------ concept graph


def test_reloading_the_graph_swaps_it_in_whole(make_client):
    c = make_client({"concepts": concepts("a", "b", "c"), "prerequisites": edges("a>b", "b>c")})
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/progress/b", json={"understanding": 50})
    load_graph({"concepts": concepts("a", "b", "d"), "prerequisites": edges("a>b", "b>d")})
    g = c.get("/api/v1/concepts/a/graph").json()
    assert {n["id"] for n in g["nodes"]} == {"a", "b", "d"}
    assert c.get("/api/v1/concepts/c").status_code == 404
    # User data survives a reload.
    assert c.get(f"/api/v1/users/{uid}/progress").json()[0]["understanding"] == 50
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        schemas = {r[0] for r in conn.execute("SELECT schema_name FROM information_schema.schemata")}
    assert "graph" in schemas and "graph_build" not in schemas and "graph_old" not in schemas


def test_a_failed_load_leaves_the_live_graph_alone(make_client, tmp_path):
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    res = run_loader({"concepts": concepts("x", "y"), "prerequisites": edges("x>y", "y>x")}, tmp_path)
    assert res.returncode != 0 and "cycle" in res.stderr.lower()
    assert c.get("/api/v1/concepts/a").status_code == 200
    assert c.get("/api/v1/concepts/x").status_code == 404


def test_reads_keep_working_while_a_reload_runs(make_client):
    big = {"concepts": concepts(*[f"n{i}" for i in range(3000)]),
           "prerequisites": [{"concept": f"n{i}", "requires": f"n{i + 1}"} for i in range(2999)]}
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    failures = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            r = c.get("/api/v1/search", params={"q": "a"})
            if r.status_code != 200:
                failures.append(r.status_code)
        db.reset_connections()

    t = threading.Thread(target=reader)
    t.start()
    try:
        load_graph(big)
    finally:
        stop.set()
        t.join()
    assert failures == []
    assert c.get("/api/v1/concepts/n2999").status_code == 200


def test_graph_tables_have_their_indexes(make_client):
    make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        idx = {r[0] for r in conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'graph'")}
    joined = "\n".join(idx)
    for needle in ("(slug)", "gin (to_tsvector('simple'::regconfig, search_doc))", "name_key text_pattern_ops", "(prereq_pk, concept_pk)", "(name_norm)"):
        assert needle in joined, needle


def test_tests_use_the_test_database():
    assert config.DATABASE_URL == TEST_URL


def test_health_reports_postgres(make_client):
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    assert c.get("/health").json() == {"status": "ok", "concepts": 2, "search": "postgres"}
    drop_graph()
    assert c.get("/health").status_code == 503


def test_dotenv_reader_handles_quotes_ampersands_and_comments(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\n"
        'A_URL="postgresql://u:p@host/db?sslmode=require&channel_binding=require"\n'
        "export B_VAL='single'\n"
        "C_PLAIN=plain=with=equals\n"
        'D_COMMENTED="postgresql://x/db?a=1&b=2"   # inline comment after a quoted value\n'
        "E_COMMENTED=postgresql://y/db?a=1   # inline comment\n"
        "F_HASH=abc#notacomment\n"
        "ALREADY_SET=from-file\n"
        "not a setting\n"
    )
    for k in ("A_URL", "B_VAL", "C_PLAIN", "D_COMMENTED", "E_COMMENTED", "F_HASH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALREADY_SET", "from-shell")
    config._load_dotenv(env)
    import os

    assert os.environ["A_URL"] == "postgresql://u:p@host/db?sslmode=require&channel_binding=require"
    assert os.environ["B_VAL"] == "single"
    assert os.environ["C_PLAIN"] == "plain=with=equals"
    assert os.environ["D_COMMENTED"] == "postgresql://x/db?a=1&b=2"
    assert os.environ["E_COMMENTED"] == "postgresql://y/db?a=1"
    assert os.environ["F_HASH"] == "abc#notacomment"
    assert os.environ["ALREADY_SET"] == "from-shell"  # the shell wins over the file
    config._load_dotenv(tmp_path / "missing.env")  # no file: nothing happens


def _api_connections() -> int:
    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        return admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'topolearn-api'"
        ).fetchone()[0]


def test_connections_close_when_their_thread_ends(make_client):
    import gc

    make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    before = _api_connections()

    def worker():
        conn = db.get_conn()
        # A list parameter: the case that used to leave the connection to the garbage collector.
        conn.execute("SELECT count(*) FROM graph.concepts WHERE slug = ANY(?)", (["a", "b"],)).fetchone()

    gc.disable()
    try:
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        del threads
        deadline = time.time() + 5
        while _api_connections() > before and time.time() < deadline:
            time.sleep(0.05)
        assert _api_connections() <= before
    finally:
        gc.enable()


def test_concurrent_goal_pins_respect_the_limit_and_never_fail(make_client):
    from app import learning

    ids = [f"g{i}" for i in range(8)]
    c = make_client({"concepts": concepts(*ids), "prerequisites": []})
    uid = new_user(c)
    results = []

    def pin(cid):
        try:
            learning.add_goal(uid, cid)
            results.append("ok")
        except Exception as exc:  # HTTPException(409) is expected past the limit
            results.append(getattr(exc, "status_code", repr(exc)))
        finally:
            db.reset_connections()

    threads = [threading.Thread(target=pin, args=(cid,)) for cid in ids + ["g0", "g0", "g1"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results) <= {"ok", 409}, results
    goals = c.get(f"/api/v1/users/{uid}/goals").json()
    assert len(goals) == learning.MAX_GOALS
    assert len({g["concept"]["id"] for g in goals}) == learning.MAX_GOALS


def test_text_postgres_cannot_store_gives_422_not_500(make_client):
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    uid = new_user(c)
    assert c.get("/api/v1/search", params={"q": "a\x00b"}).status_code == 422
    assert c.get("/api/v1/disciplines", params={"q": "\x00"}).status_code == 422
    assert c.get("/api/v1/concepts/a%00b").status_code == 422
    assert c.get("/api/v1/concepts/a/graph", params={"user_id": "x\x00"}).status_code == 422
    assert c.post("/api/v1/users", json={"name": "a\u0000b"}).status_code == 422
    r = c.post("/api/v1/reports", json={"user_id": uid, "kind": "other", "concept_id": "a", "message": "x\u0000"})
    assert r.status_code == 422
    # The connection is still fine afterwards.
    assert c.get("/api/v1/concepts/a").status_code == 200


def test_schema_check_does_not_wait_behind_open_writes(make_client):
    c = make_client({"concepts": concepts("a"), "prerequisites": []})
    uid = new_user(c)
    with psycopg.connect(TEST_URL) as writer:  # an uncommitted write, holding locks
        writer.execute(
            "INSERT INTO concept_progress (user_id, concept_id, understanding, updated_at) VALUES (%s, 'a', 10, 0)",
            (uid,),
        )
        userdb.forget_schema()  # like a brand-new server process
        done = []
        t = threading.Thread(target=lambda: (userdb.ensure_schema(), done.append(1), db.reset_connections()))
        t.start()
        t.join(timeout=5)
        assert done == [1], "schema check blocked behind an open transaction"
        writer.rollback()


def test_failed_load_leaves_no_trace(make_client, monkeypatch):
    from scripts import build_db

    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    real = build_db._schema_parts
    monkeypatch.setattr(build_db, "_schema_parts", lambda s: (real(s)[0], real(s)[1] + "\nSELECT 1/0;"))
    with pytest.raises(psycopg.Error):
        load_graph({"concepts": concepts("x", "y"), "prerequisites": edges("x>y")})
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        schemas = {r[0] for r in conn.execute("SELECT schema_name FROM information_schema.schemata")}
    assert "graph_build" not in schemas and "graph_old" not in schemas
    assert c.get("/api/v1/concepts/a").status_code == 200
    assert c.get("/api/v1/concepts/x").status_code == 404


def test_graph_request_survives_a_reload_between_its_queries(make_client, monkeypatch):
    from app import repository

    make_client({"concepts": concepts("a", "b", "c"), "prerequisites": edges("a>b", "b>c")})
    real = repository._rows_by_pk
    # Simulate a reload landing mid-request: one concept's row is gone by the last query.
    monkeypatch.setattr(repository, "_rows_by_pk",
                        lambda conn, pks: {k: v for k, v in real(conn, pks).items() if v["slug"] != "c"})
    conn = db.get_conn()
    g = repository.prerequisite_graph(conn, "a", 10, 100)
    assert {n["id"] for n in g["nodes"]} == {"a", "b"}
    assert all(e["requires"] != "c" for e in g["edges"])
    p = repository.learning_path(conn, "a")
    assert [s["id"] for s in p["steps"]] == ["b", "a"]


@pytest.mark.parametrize("query,expected", [
    ("tsne", "t-SNE"), ("t-sne", "t-SNE"), ("t sne", "t-SNE"),
    ("godels", "Gödel's Theorem"), ("godel", "Gödel's Theorem"), ("gödel's", "Gödel's Theorem"),
    ("strasse", "Straße Map"), ("oeuvre", "Œuvre Index"), ("naive bay", "Naïve Bayes"),
])
def test_search_finds_punctuated_and_accented_names(make_client, query, expected):
    names = ["t-SNE", "Gödel's Theorem", "Straße Map", "Œuvre Index", "Naïve Bayes", "Other"]
    c = make_client({"concepts": [{"id": f"c{i}", "name": n} for i, n in enumerate(names)], "prerequisites": []})
    results = [r["name"] for r in c.get("/api/v1/search", params={"q": query}).json()["results"]]
    assert expected in results, (query, results)


def test_a_read_retries_once_on_a_connection_that_died(make_client):
    make_client({"concepts": concepts("a"), "prerequisites": []})
    conn = db.get_conn()
    pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    with psycopg.connect(TEST_URL, autocommit=True) as admin:  # the server goes away under us
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    again = db.get_conn()  # same thread: same (now dead) connection
    assert again.raw is conn.raw
    assert again.execute("SELECT slug FROM graph.concepts").fetchone()[0] == "a"
    assert again.execute("SELECT pg_backend_pid()").fetchone()[0] != pid


def test_transactions_survive_a_dead_connection_at_the_start(make_client):
    make_client({"concepts": concepts("a"), "prerequisites": []})
    userdb.ensure_schema()
    conn = db.get_conn()
    pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    with userdb.session() as tx:  # BEGIN on the dead connection: replaced and retried
        _add_user(tx, "zed")
    with userdb.reader() as r:
        assert userdb.user_exists(r, "zed")


def test_a_connection_lost_mid_transaction_is_never_replayed(make_client):
    make_client({"concepts": concepts("a"), "prerequisites": []})
    userdb.ensure_schema()
    with pytest.raises(psycopg.OperationalError):
        with userdb.session() as tx:
            _add_user(tx, "half")
            pid = tx.execute("SELECT pg_backend_pid()").fetchone()[0]
            with psycopg.connect(TEST_URL, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            _add_user(tx, "other")
    with userdb.session() as tx:  # the next transaction works on a fresh connection
        _add_user(tx, "after")
    with userdb.reader() as r:
        assert not userdb.user_exists(r, "half") and not userdb.user_exists(r, "other")
        assert userdb.user_exists(r, "after")


def test_several_server_processes_starting_at_once_set_up_tables_safely():
    import subprocess
    import sys

    from conftest import ROOT, wipe_users

    wipe_users()
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from app import config, userdb\n"
        "config.DATABASE_URL = %r\n"
        "userdb.ensure_schema()\n"
        "with userdb.session() as c: c.execute('SELECT count(*) FROM users').fetchone()\n"
    ) % (str(ROOT), TEST_URL)
    procs = [subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE, text=True) for _ in range(6)]
    errors = [p.communicate(timeout=60)[1] for p in procs]
    assert all(p.returncode == 0 for p in procs), errors
