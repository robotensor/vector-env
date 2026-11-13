from __future__ import annotations

import json
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

FORMAT = "vector-library/1"
SORTS = {"id": "id", "mass": "mass", "size": "size_mm"}


def library_dir(bundle: Path) -> Path:
    return Path(bundle) / "library"


def min_sizes() -> dict:
    from .settings import cell_config
    L = cell_config().get("library") or {}
    return {k: float(L.get(k) or 0.0) for k in ("min_size_mm", "min_mid_mm", "min_mass_g")}


def _mid_mm(r: dict) -> float:
    return round(sorted(float(v) for v in r["extents_m"])[1] * 1000, 2)


def too_small(r: dict) -> str | None:
    T = min_sizes()
    if float(r["size_mm"]) < T["min_size_mm"]:
        return f"largest side {float(r['size_mm']):.0f} mm < {T['min_size_mm']:g} mm"
    if _mid_mm(r) < T["min_mid_mm"]:
        return f"second-largest side {_mid_mm(r):.1f} mm < {T['min_mid_mm']:g} mm"
    if float(r["mass"]) < T["min_mass_g"] / 1000:
        return f"mass {float(r['mass']) * 1000:.1f} g < {T['min_mass_g']:g} g"
    return None


def _search_text(r: dict) -> str:
    return " ".join(str(x) for x in (r["id"], r.get("label"), r.get("title"), r.get("class"), r.get("material"),
                                     (r.get("material") or "").replace("_", " "), r.get("surface"), r.get("source"))
                    if x).lower()


def write_index(bundle: Path, log=print) -> dict:
    bundle = Path(bundle)
    rows, seen = [], set()
    for aj in sorted((bundle / "assets").glob("*/*/asset.json")):
        for r in json.loads(aj.read_text())["rows"]:
            if r["id"] in seen:
                raise ValueError(f"duplicate asset id {r['id']} ({aj})")
            seen.add(r["id"])
            rows.append(r)
    rows.sort(key=lambda r: r["id"])
    d = library_dir(bundle)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "index.jsonl.tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    tmp.replace(d / "index.jsonl")

    tmp = d / "index.sqlite.tmp"
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        pragma journal_mode = off; pragma synchronous = off;
        create table assets (id text primary key, source text not null, unit text, variant text, label text,
                             class text, material text, surface text, mass real, size_mm real, mid_mm real,
                             search text, row text);
        create table info (key text primary key, value text);
    """)
    con.executemany("insert into assets values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(r["id"], r["source"], r.get("unit"), r.get("variant"), r.get("label"), r.get("class"),
                      r.get("material"), r.get("surface"), r.get("mass"), r.get("size_mm"), _mid_mm(r),
                      _search_text(r), json.dumps(r, separators=(",", ":"))) for r in rows])
    for c in ("source", "class", "material", "size_mm", "mass"):
        con.execute(f"create index assets_{c} on assets ({c})")
    facets = _facets(con)
    con.executemany("insert into info values (?, ?)", [("format", FORMAT)])
    con.commit()
    con.execute("vacuum")
    con.close()
    tmp.replace(d / "index.sqlite")
    tmp = d / "facets.json.tmp"
    tmp.write_text(json.dumps(facets, indent=1))
    tmp.replace(d / "facets.json")
    log(f"library index: {len(rows)} assets, {facets['count']} usable ({facets['too_small']} too small; "
        f"{', '.join(f'{k} {v}' for k, v in facets['sources'].items())})")
    return facets


def _facets(con: sqlite3.Connection) -> dict:
    where, args = _where({})

    def counts(col):
        cur = con.execute(f"select {col}, count(*) n from assets{where} and {col} is not null and {col} != '' "
                          f"group by {col} order by n desc, {col}", args)
        return {k: int(n) for k, n in cur}
    n, s0, s1, m0, m1 = con.execute(f"select count(*), min(size_mm), max(size_mm), min(mass), max(mass) from assets{where}",
                                    args).fetchone()
    total = con.execute("select count(*) from assets").fetchone()[0]
    return {"format": FORMAT, "count": int(n), "sources": counts("source"), "classes": counts("class"),
            "materials": counts("material"),
            "size_range": [s0, s1] if n else None, "mass_range": [m0, m1] if n else None,
            "too_small": int(total - n), "min_sizes": min_sizes()}


class LibraryError(LookupError):
    pass


_local = threading.local()


def _db(bundle: Path) -> tuple[sqlite3.Connection, int]:
    p = library_dir(bundle) / "index.sqlite"
    try:
        mt = p.stat().st_mtime_ns
    except FileNotFoundError:
        raise LibraryError(f"no asset index at {p} (python -m vector_env.build index)") from None
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    hit = conns.get(str(p))
    if hit and hit[1] == mt:
        return hit[0], mt
    if hit:
        hit[0].close()
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    if "mid_mm" not in {r[1] for r in con.execute("pragma table_info(assets)")}:
        con.close()
        raise LibraryError(f"the asset index {p} is older than this vector-env (python -m vector_env.build index)")
    conns[str(p)] = (con, mt)
    return con, mt


@lru_cache(4096)
def _row(path: str, mtime: int, aid: str) -> dict | None:
    con, _ = _db(Path(path))
    r = con.execute("select row from assets where id = ?", (aid,)).fetchone()
    return json.loads(r[0]) if r else None


def get(bundle: Path, aid: str) -> dict | None:
    _, mt = _db(bundle)
    return _row(str(bundle), mt, str(aid))


def _where(flt: dict) -> tuple[str, list]:
    T = min_sizes()
    sql = ["size_mm >= ?", "mid_mm >= ?", "mass >= ?"]
    args = [T["min_size_mm"], T["min_mid_mm"], T["min_mass_g"] / 1000]
    for key, col in (("source", "source"), ("class", "class"), ("material", "material")):
        v = flt.get(key)
        if v in (None, "", []):
            continue
        vs = [v] if isinstance(v, str) else list(v)
        sql.append(f"{col} in ({','.join('?' * len(vs))})")
        args += [str(x) for x in vs]
    for key, col in (("size_mm", "size_mm"), ("mass", "mass")):
        rng = flt.get(key)
        if rng is None:
            continue
        lo, hi = rng
        if lo is not None:
            sql.append(f"{col} >= ?")
            args.append(float(lo))
        if hi is not None:
            sql.append(f"{col} <= ?")
            args.append(float(hi))
    for w in str(flt.get("q") or "").lower().split()[:8]:
        sql.append("search like ? escape '\\'")
        args.append("%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return " where " + " and ".join(sql), args


def search(bundle: Path, flt: dict, sort: str = "id", page: int = 1, per_page: int = 48) -> tuple[int, list[dict]]:
    con, _ = _db(bundle)
    desc = sort.startswith("-")
    col = SORTS.get(sort.lstrip("-"))
    if col is None:
        raise LibraryError(f"sort must be one of {sorted(SORTS)} (prefix '-' for descending)")
    where, args = _where(flt)
    total = con.execute(f"select count(*) from assets{where}", args).fetchone()[0]
    order = f"{col} {'desc' if desc else 'asc'}" + (", id" if col != "id" else "")
    cur = con.execute(f"select row from assets{where} order by {order} limit ? offset ?",
                      args + [int(per_page), (int(page) - 1) * int(per_page)])
    return int(total), [json.loads(r[0]) for r in cur]


def ids(bundle: Path, flt: dict) -> list[str]:
    con, _ = _db(bundle)
    where, args = _where(flt)
    return [r[0] for r in con.execute(f"select id from assets{where} order by id", args)]


def facets(bundle: Path) -> dict:
    con, mt = _db(bundle)
    return _facets_cached(str(bundle), mt, json.dumps(min_sizes(), sort_keys=True))


@lru_cache(8)
def _facets_cached(path: str, mtime: int, sizes: str) -> dict:
    con, _ = _db(Path(path))
    return _facets(con)
