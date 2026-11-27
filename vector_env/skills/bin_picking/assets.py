from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - not on POSIX
    fcntl = None

from .settings import ROOT, env_config

FORMAT = "vector-assets/1"
MANIFEST = "assets.json"
MARK = ".verified"
ARCHIVE = "bundle.tar"
SKIP = {MANIFEST, MARK}
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
log = logging.getLogger("vector_env.assets")


class AssetsError(RuntimeError):
    pass


def check_version(version) -> str:
    v = str(version or "")
    if not VERSION.match(v) or ".." in v:
        raise AssetsError(f"bad asset version {v[:80]!r}: letters, digits, '.', '_' and '-' only, starting with a letter or digit")
    return v


def check_path(path) -> str:
    p = path if isinstance(path, str) else ""
    parts = p.split("/")
    if (not p or "\\" in p or "\0" in p or p.startswith("/") or re.match(r"^[A-Za-z]:", p)
            or any(x in ("", ".", "..") for x in parts) or p in SKIP):
        raise AssetsError(f"bad asset path {str(path)[:120]!r} in the manifest")
    return p


def pinned() -> dict:
    a = env_config().get("assets") or {}
    return {"version": os.environ.get("VECTOR_ENV_ASSETS_VERSION") or a.get("version"),
            "sha256": os.environ.get("VECTOR_ENV_ASSETS_SHA256") or a.get("sha256"),
            "source": os.environ.get("VECTOR_ENV_ASSETS_SOURCE") or a.get("source")}


def cache_root() -> Path:
    p = os.environ.get("VECTOR_ENV_CACHE")
    return Path(p).expanduser() if p else Path.home() / ".cache" / "vector-env" / "assets"


def local_dir() -> Path | None:
    p = os.environ.get("VECTOR_ENV_BUNDLE") or os.environ.get("VECTOR_SIM_BUNDLE")
    if p:
        return Path(p).expanduser().resolve()
    dev = ROOT / "bundle"
    if (dev / "manifest.json").is_file():
        return dev.resolve()
    return None


def bundle_dir() -> Path:
    d = local_dir()
    if d is not None:
        return d
    v = pinned()["version"]
    if not v:
        raise AssetsError("no asset bundle: set VECTOR_ENV_BUNDLE, or pin assets.version and run vector-env assets pull")
    return cache_root() / check_version(v)


MISSING = ("no asset bundle: set VECTOR_ENV_BUNDLE to a bundle folder, or run "
           "VECTOR_ENV_ASSETS_SOURCE=<hf://repo or pack folder> vector-env assets pull "
           "(it fills VECTOR_ENV_CACHE, default ~/.cache/vector-env/assets/<version>)")


def find() -> Path | None:
    try:
        d = bundle_dir()
    except AssetsError:
        return None
    return d if (d / "manifest.json").is_file() else None


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _files(bundle: Path) -> list[str]:
    out = []
    for p in bundle.rglob("*"):
        if p.is_file() and not p.name.endswith(".tmp"):
            rel = p.relative_to(bundle).as_posix()
            if rel not in SKIP:
                out.append(rel)
    return sorted(out)


def read_manifest(bundle: Path) -> dict | None:
    p = Path(bundle) / MANIFEST
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def manifest_sha(bundle: Path) -> str | None:
    p = Path(bundle) / MANIFEST
    if not p.is_file():
        return None
    st = p.stat()
    return _sha_cached(str(p), st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=16)
def _sha_cached(path: str, mtime_ns: int, size: int) -> str:
    return _sha_file(Path(path))


def write_manifest(bundle: Path, version: str) -> dict:
    bundle = Path(bundle)
    core = json.loads((bundle / "manifest.json").read_text()).get("bundle")
    files = [[f, _sha_file(bundle / f), (bundle / f).stat().st_size] for f in _files(bundle)]
    man = {"format": FORMAT, "version": check_version(version), "cell": core, "files": files}
    tmp = bundle / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(man, separators=(",", ":")))
    tmp.replace(bundle / MANIFEST)
    sha = manifest_sha(bundle)
    (bundle / MARK).write_text(sha)
    return {"version": man["version"], "sha256": sha, "files": len(files), "bytes": sum(f[2] for f in files)}


def verify(bundle: Path) -> list[str]:
    bundle = Path(bundle)
    man = read_manifest(bundle)
    if man is None or man.get("format") != FORMAT or not isinstance(man.get("files"), list):
        return [MANIFEST]
    bad, listed = [], set()
    for row in man["files"]:
        try:
            path, sha, size = row
            check_path(path)
        except (AssetsError, TypeError, ValueError):
            bad.append(f"{MANIFEST}: bad entry {str(row)[:120]}")
            continue
        listed.add(path)
        p = bundle / path
        if p.is_symlink() or not p.is_file() or p.stat().st_size != size or _sha_file(p) != sha:
            bad.append(path)
    bad += [f"{f} (not in the manifest)" for f in _files(bundle) if f not in listed]
    if not bad:
        (bundle / MARK).write_text(manifest_sha(bundle))
    return bad


_ok: dict[tuple, tuple] = {}


def _stamp(bundle: Path) -> tuple | None:
    try:
        a, b = (bundle / MANIFEST).stat(), (bundle / MARK).stat()
    except OSError:
        return None
    return a.st_mtime_ns, a.st_size, b.st_mtime_ns, b.st_size


def _verified(bundle: Path, version: str | None, sha: str | None) -> bool:
    key, st = (str(bundle), version, sha), _stamp(Path(bundle))
    if st is None:
        return False
    if _ok.get(key) == st:
        return True
    have = manifest_sha(bundle)
    if sha and have != sha:
        return False
    mark = bundle / MARK
    if not (mark.is_file() and mark.read_text().strip() == have):
        return False
    if version:
        man = read_manifest(bundle)
        if man is None or man.get("version") != version:
            return False
    _ok[key] = st
    return True


def info(bundle=None) -> dict:
    d = resolve(bundle) if isinstance(bundle, dict) else Path(bundle) if bundle else bundle_dir()
    man = read_manifest(d)
    if man is None:
        raise AssetsError(f"no {MANIFEST} in {d} (vector-env assets pull, or vector-env assets manifest)")
    return {"version": man["version"], "sha256": manifest_sha(d)}


def ensure(version: str | None = None) -> Path:
    pin = pinned()
    want = version or pin["version"]
    if want:
        check_version(want)
    sha = pin["sha256"] if want == pin["version"] else None
    d = local_dir()
    if d is not None:
        man = read_manifest(d)
        if want is None or (man is not None and man.get("version") == want):
            return d
        if os.environ.get("VECTOR_ENV_BUNDLE"):
            raise AssetsError(f"VECTOR_ENV_BUNDLE={d} holds version {man and man.get('version')}, not {want}")
    if not want:
        raise AssetsError("no asset version pinned (assets.version or VECTOR_ENV_ASSETS_VERSION)")
    d = cache_root() / want
    if _verified(d, want, sha):
        return d
    return pull(want, dest=d, sha256=sha)


def _source(source: str | None) -> str:
    s = source or pinned()["source"]
    if not s:
        raise AssetsError("no asset source: set assets.source or VECTOR_ENV_ASSETS_SOURCE (hf://<repo>[@rev] or a folder)")
    return s


def _fetch(source: str, version: str, name: str, tmp: Path, token: str | None = None) -> Path:
    if source.startswith("hf://"):
        from huggingface_hub import hf_hub_download
        repo, _, rev = source[5:].partition("@")
        p = hf_hub_download(repo_id=repo, filename=f"{version}/{name}", repo_type="dataset", revision=rev or None,
                            token=token or os.environ.get("HF_TOKEN") or None, local_dir=str(tmp))
        return Path(p)
    base = Path(source[7:] if source.startswith("file://") else source).expanduser()
    p = base / version / name
    if not p.is_file():
        if (base / "manifest.json").is_file():
            raise AssetsError(f"{base} is a bundle folder, not a pack folder: use VECTOR_ENV_BUNDLE={base}, "
                              f"or vector-env assets pack --bundle {base} --out <dir> and use <dir> as the source")
        raise AssetsError(f"{p} not found (a source folder holds <version>/{MANIFEST} and <version>/{ARCHIVE}, "
                          "as written by vector-env assets pack --out)")
    return p


def _members(tf: tarfile.TarFile, listed: set[str]) -> None:
    dirs = {"/".join(p.split("/")[:k]) for p in listed for k in range(1, p.count("/") + 1)}
    for m in tf.getmembers():
        name = m.name[2:] if m.name.startswith("./") else m.name.rstrip("/")
        if m.isdir() and (name in dirs or name in ("", ".")):
            continue
        if not m.isfile() or name not in listed:
            raise AssetsError(f"the archive holds {m.name[:120]!r}, which the manifest does not list")


def pull(version: str, source: str | None = None, dest: Path | None = None, sha256: str | None = None,
         token: str | None = None) -> Path:
    version = check_version(version)
    src = _source(source)
    dest = Path(dest or cache_root() / version)
    if not sha256:
        log.warning("asset version %s has no pinned sha256: its manifest is trusted as downloaded", version)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pull-", dir=dest.parent) as t:
        tmp = Path(t)
        man_p = _fetch(src, version, MANIFEST, tmp / "dl", token)
        got = _sha_file(man_p)
        if sha256 and got != sha256:
            raise AssetsError(f"asset manifest {version}: sha256 {got} is not the pinned {sha256}")
        man = json.loads(man_p.read_text())
        if man.get("format") != FORMAT or man.get("version") != version or not isinstance(man.get("files"), list):
            raise AssetsError(f"asset manifest {version}: wrong format or version")
        try:
            listed = {check_path(row[0]) for row in man["files"]}
        except (TypeError, IndexError, KeyError) as e:
            raise AssetsError(f"asset manifest {version}: bad file entry") from e
        tar_p = _fetch(src, version, ARCHIVE, tmp / "dl", token)
        out = tmp / "bundle"
        out.mkdir()
        with tarfile.open(tar_p) as tf:
            _members(tf, listed)
            tf.extractall(out, filter="data")
        shutil.copyfile(man_p, out / MANIFEST)
        bad = verify(out)
        if bad:
            raise AssetsError(f"asset bundle {version}: {len(bad)} files missing or changed (first: {bad[0]})")
        if dest.exists():
            shutil.rmtree(dest)
        out.rename(dest)
    return dest


def pack(bundle: Path, out: Path, version: str | None = None) -> dict:
    bundle, out = Path(bundle), Path(out)
    man = read_manifest(bundle)
    if man is None or (version and man.get("version") != version):
        if not version:
            raise AssetsError("the bundle has no manifest yet: give --version")
        write_manifest(bundle, version)
        man = read_manifest(bundle)
    version = check_version(man["version"])
    d = out / version
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bundle / MANIFEST, d / MANIFEST)
    with tarfile.open(d / (ARCHIVE + ".tmp"), "w", format=tarfile.PAX_FORMAT) as tf:
        for path, _, _ in man["files"]:
            ti = tf.gettarinfo(str(bundle / path), arcname=path)
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname, ti.mode = 0, 0, 0, "", "", 0o644
            with open(bundle / path, "rb") as f:
                tf.addfile(ti, f)
    (d / (ARCHIVE + ".tmp")).replace(d / ARCHIVE)
    return {"version": version, "sha256": manifest_sha(bundle), "dir": str(d), "files": len(man["files"]),
            "archive_bytes": (d / ARCHIVE).stat().st_size}


# Other bundles in the same format, chosen by the caller: a bundle folder, or a pin {version, sha256, source}.

def check_pin(pin) -> dict:
    if not isinstance(pin, dict):
        raise AssetsError("an asset pin is a mapping {version, sha256, source}")
    sha = pin.get("sha256") or None
    if sha is not None and not SHA256.match(str(sha)):
        raise AssetsError("an asset pin's sha256 must be 64 lowercase hex digits")
    return {"version": check_version(pin.get("version")), "sha256": sha, "source": pin.get("source") or None,
            "token": pin.get("token") or None}


def _is_default(version: str, sha: str | None) -> bool:
    d = pinned()
    return version == d["version"] and (sha is None or sha == d["sha256"])


def pin_dir(version: str, sha256: str | None = None) -> Path:
    """The cache folder of a pinned bundle: <cache>/<version> for the env.yaml pin (or a pin without a sha256),
    <cache>/<version>~<sha256[:16]> for any other, so bundles with the same version name never share a folder."""
    v = check_version(version)
    if sha256 is None or _is_default(v, sha256):
        return cache_root() / v
    return cache_root() / f"{v}~{sha256[:16]}"


_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


class _Lock:
    def __init__(self, dest: Path):
        self.dest = dest
        with _locks_lock:
            self.t = _locks.setdefault(str(dest), threading.Lock())

    def __enter__(self):
        self.t.acquire()
        self.f = None
        if fcntl is not None:
            try:
                self.dest.parent.mkdir(parents=True, exist_ok=True)
                self.f = open(self.dest.parent / f".{self.dest.name}.lock", "w")
                fcntl.flock(self.f, fcntl.LOCK_EX)
            except OSError:
                self.f = None
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            self.f.close()
        self.t.release()


def _hide(msg: str, source: str | None, token: str | None) -> str:
    if token:
        msg = msg.replace(token, "<token>")
    for s in (source, source[5:], source[5:].partition("@")[0]) if source and source.startswith("hf://") else (source,):
        if s:
            msg = msg.replace(s, "<source>")
    return msg


def _folder_source(src: str, version: str, sha: str | None) -> Path | None:
    p = Path(src[7:] if src.startswith("file://") else src).expanduser()
    if src.startswith("hf://") or not (p / "manifest.json").is_file():
        return None
    p = p.resolve()
    man = read_manifest(p)
    if man is None or man.get("version") != version:
        raise AssetsError(f"the pinned bundle folder holds version {man and man.get('version')}, not {version}")
    if sha and manifest_sha(p) != sha:
        raise AssetsError(f"asset manifest {version}: sha256 {manifest_sha(p)} is not the pinned {sha}")
    if not _verified(p, version, sha):
        bad = verify(p)
        if bad:
            raise AssetsError(f"asset bundle {version}: {len(bad)} files missing or changed (first: {bad[0]})")
    return p


def ensure_pin(pin: dict) -> Path:
    """The folder of a pinned bundle, pulled from the pin's own source into the cache on first use.
    The manifest's sha256 must equal the pin's; the source never falls back to env.yaml's (unless the pin is
    the env.yaml pin itself)."""
    P = check_pin(pin)
    v, sha, src, token = P["version"], P["sha256"], P["source"], P["token"]
    d = local_dir()
    if d is not None and sha and manifest_sha(d) == sha:
        return d
    dest = pin_dir(v, sha)
    if _verified(dest, v, sha):
        return dest
    if not src and _is_default(v, sha):
        src = pinned()["source"]
    if not src:
        raise AssetsError(f"asset bundle {v} is not in the cache and its pin has no source")
    with _Lock(dest):
        if _verified(dest, v, sha):
            return dest
        try:
            folder = _folder_source(src, v, sha)
            return folder if folder is not None else pull(v, src, dest, sha, token)
        except Exception as e:
            raise AssetsError(_hide(f"asset bundle {v}: {e}", src, token)) from None


def resolve(bundle=None) -> Path:
    """The bundle folder for None (the env.yaml bundle, pulled if needed), a folder (str or Path) holding
    manifest.json, or a pin {version, sha256, source}."""
    if bundle is None:
        return ensure()
    if isinstance(bundle, dict):
        return ensure_pin(bundle)
    p = Path(bundle).expanduser().resolve()
    if not (p / "manifest.json").is_file():
        raise AssetsError(f"{p} is not an asset bundle folder (no manifest.json)")
    return p


def for_scene(scene: dict, *bundles) -> Path:
    """The bundle a scene came from: the first of the given bundles (folders or pins), then the env.yaml one,
    whose manifest sha256 equals the scene's env.assets_sha256. A scene without one gets the first candidate."""
    want = (scene.get("env") or {}).get("assets_sha256") if isinstance(scene, dict) else None
    tried = []
    for b in (*bundles, None):
        try:
            d = resolve(b)
        except AssetsError:
            if b is not None:
                raise
            continue
        if want is None or manifest_sha(d) == want:
            return d
        tried.append(d)
    raise AssetsError(f"the scene is from asset bundle {(scene.get('env') or {}).get('assets_version')} "
                      f"(sha256 {str(want)[:16]}...), which none of the {len(tried)} given bundles is")
