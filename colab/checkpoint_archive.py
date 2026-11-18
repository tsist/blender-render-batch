"""Export and restore allowlisted render data with member hashes."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from zipfile import ZIP_STORED, ZipFile, ZipInfo

MANIFEST = "render-batch-bundle.json"
SHOT = r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}"
_INPUT = r"inputs/[^/\\:\x00-\x1f]+\.blend"
_CHECKPOINT = rf"checkpoints/(?:preview|final)/(?:identity\.json|{SHOT}/frame_[0-9]+\.((?:png|json)))"
_DELIVERY = rf"delivery/(?:preview|final)/(?:review\.json|(?:combined(?:-incomplete)?|shot_{SHOT})\.(?:mp4|json))"
_ALLOWED = re.compile(rf"(?:render-request\.json|run-report\.json|{_INPUT}|{_CHECKPOINT}|{_DELIVERY})\Z", re.IGNORECASE)


@contextmanager
def batch_lock(directory):
    """Serialize all writers that share the same runtime filesystem."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".render-batch.lock"
    if path.is_symlink():
        raise ValueError("Batch lock path must be an ordinary file")
    with path.open("a+b") as file:
        try:
            if os.name == "nt":
                import msvcrt
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"Batch storage is busy or does not support locking: {directory}") from error
        yield


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent, prefix=".writing-") as temp:
        candidate = Path(temp) / "receipt.json"
        candidate.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(candidate, path)


def publish(source, target):
    expected = {"bytes": source.stat().st_size, "sha256": digest(source)}
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent, prefix=".writing-") as temp:
        candidate = Path(temp) / target.name
        shutil.copyfile(source, candidate)
        os.replace(candidate, target)
    if not matches(target, expected):
        raise RuntimeError(f"Readback differs after publishing {target.name}")
    return expected


def matches(path, record):
    return (path.is_file() and path.stat().st_size == record.get("bytes")
            and digest(path) == record.get("sha256"))


def validate_names(names):
    if not names or not all(_ALLOWED.fullmatch(name) for name in names):
        raise ValueError("The bundle contains an unsupported member path")
    if len({name.casefold() for name in names}) != len(names) or "render-request.json" not in names:
        raise ValueError("Bundle needs a unique render-request.json and unique member names")


def validate_target(batch, relative):
    target = batch / relative
    if target.is_symlink() or not target.resolve().is_relative_to(batch):
        raise ValueError(f"Bundle path escapes the batch: {relative}")
    return target


def save_bundle(batch, target, names):
    """Write an immutable ZIP, then independently check its stored members."""
    batch = batch.resolve()
    names = sorted(set(names))
    validate_names(names)
    target = target.resolve()
    if target.suffix.casefold() != ".zip":
        raise ValueError("Bundle output must end in .zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent, prefix=".packing-") as temp:
        scratch = Path(temp) / "candidate.zip"
        records = {}
        with ZipFile(scratch, "w", compression=ZIP_STORED) as bundle:
            for name in names:
                source = validate_target(batch, name)
                if not source.is_file():
                    raise ValueError(f"Missing bundle input: {name}")
                result = hashlib.sha256()
                size = 0
                with source.open("rb") as file, bundle.open(ZipInfo(name), "w", force_zip64=True) as packed:
                    for chunk in iter(lambda: file.read(1024 * 1024), b""):
                        packed.write(chunk)
                        result.update(chunk)
                        size += len(chunk)
                records[name] = {"bytes": size, "sha256": result.hexdigest()}
            bundle.writestr(ZipInfo(MANIFEST), json.dumps({"version": 1, "files": records}, sort_keys=True))
        with ZipFile(scratch) as bundle:
            for name, record in records.items():
                with bundle.open(name) as file:
                    if hashlib.file_digest(file, "sha256").hexdigest() != record["sha256"]:
                        raise RuntimeError(f"Bundle readback differs: {name}")
        receipt = {"path": str(target), "bytes": scratch.stat().st_size, "sha256": digest(scratch)}
        if target.exists() or target.is_symlink():
            if not matches(target, receipt):
                raise ValueError(f"Bundle already exists with different contents: {target.name}")
        else:
            os.replace(scratch, target)
        if not matches(target, receipt):
            raise RuntimeError(f"Bundle destination readback differs: {target.name}")
    return receipt


def restore_bundle(batch, source, expected_sha):
    """Restore safe, exact members into a new batch or an identical partial batch."""
    batch = batch.resolve()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha) or digest(source) != expected_sha:
        raise ValueError("Bundle SHA-256 differs from the recorded value")
    batch.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=batch.parent, prefix=".restore-") as temp, ZipFile(source) as bundle:
        scratch = Path(temp)
        names = bundle.namelist()
        if names.count(MANIFEST) != 1 or bundle.getinfo(MANIFEST).file_size > 8 * 1024 * 1024:
            raise ValueError("Bundle identity manifest is missing or too large")
        manifest = json.loads(bundle.read(MANIFEST))
        records = manifest.get("files", {})
        members = [name for name in names if name != MANIFEST]
        validate_names(members)
        if manifest.get("version") != 1 or set(records) != set(members):
            raise ValueError("Archive members differ from their identity manifest")

        destinations = {}
        for name in members:
            info = bundle.getinfo(name)
            record = records[name]
            if (info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000
                    or info.file_size != record.get("bytes")):
                raise ValueError(f"Unsupported archive member or wrong byte count: {name}")
            target = validate_target(batch, name)
            if target.exists():
                if not matches(target, record):
                    raise ValueError(f"Restore would replace different bytes: {name}")
            else:
                destinations[name] = target
        required = sum(records[name]["bytes"] for name in destinations)
        if shutil.disk_usage(scratch).free < required:
            raise OSError("Insufficient space to restore the verified bundle")
        for name in members:
            record = records[name]
            candidate = scratch / name
            with bundle.open(name) as saved:
                if name in destinations:
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    with candidate.open("xb") as file:
                        shutil.copyfileobj(saved, file)
                    verified = matches(candidate, record)
                else:
                    verified = hashlib.file_digest(saved, "sha256").hexdigest() == record["sha256"]
            if not verified:
                raise ValueError(f"Bundle member checksum differs: {name}")
        if digest(source) != expected_sha:
            raise ValueError("Bundle changed while being restored")
        for name, target in destinations.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise ValueError(f"Restore target appeared while committing: {name}")
            os.replace(scratch / name, target)
    return {"archive_sha256": expected_sha, "restored": len(destinations), "reused": len(members) - len(destinations)}
