"""Rehydrate Blender on a fresh runtime and persist verified render frames."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import urllib.request

from checkpoint_archive import (batch_lock, digest, matches, publish, read_json, restore_bundle,
                                save_bundle, write_json)
from render_batch_media import encode_sequence, placeholder, preflight_tools


PACKAGE = Path(__file__).resolve().parent
RELEASE = json.loads((PACKAGE / "blender-release.json").read_text(encoding="utf-8"))
WORKER = PACKAGE / "blender_worker.py"


def checked_version(blender):
    output = subprocess.check_output([str(blender), "--version"], text=True, timeout=30)
    version = output.splitlines()[0]
    if not re.match(rf"^Blender {re.escape(RELEASE['version'])}(?:\s|$)", version):
        raise RuntimeError(f"Expected Blender {RELEASE['version']}, got {version}")
    build = re.search(r"^\s*build hash:\s*(\w+)", output, re.MULTILINE)
    if not build:
        raise RuntimeError("Blender did not report a build hash")
    return version, build.group(1)


def bootstrap(work, cache):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise RuntimeError("The pinned Blender archive is for Linux x86_64")
    installation = work / RELEASE["directory"]
    blender = installation / "blender"
    if blender.is_file():
        checked_version(blender)
        if read_json(installation / ".release.json") != RELEASE:
            raise RuntimeError("Local installation receipt differs from the pinned archive")
        return blender
    work.mkdir(parents=True, exist_ok=True)
    cached = cache / RELEASE["archive"]
    with TemporaryDirectory(dir=work, prefix="bootstrap-") as stage:
        stage = Path(stage)
        archive = stage / RELEASE["archive"]
        if cached.exists():
            shutil.copyfile(cached, archive)
        else:
            with urllib.request.urlopen(RELEASE["url"], timeout=60) as response, archive.open("wb") as file:
                if response.geturl() != RELEASE["url"]:
                    raise RuntimeError("Unexpected redirect from the pinned Blender release URL")
                shutil.copyfileobj(response, file, length=1024 * 1024)
        if not matches(archive, RELEASE):
            raise RuntimeError("Blender archive size or SHA-256 differs from the release pin")
        if not cached.exists():
            publish(archive, cached)
        with tarfile.open(archive, mode="r:xz") as bundle:
            bundle.extractall(stage, filter="data")
        extracted = stage / RELEASE["directory"]
        checked_version(extracted / "blender")
        write_json(extracted / ".release.json", RELEASE)
        extracted.rename(installation)
    return blender


def input_descriptor(descriptor):
    if descriptor.get("packaging") not in {"single_self_contained_blend", "self_contained_blend"}:
        raise ValueError("Inputs must be self-contained .blend files")
    filename = descriptor.get("file", "")
    source_hash = descriptor.get("sha256", "")
    if not re.fullmatch(r"[^/\\:\x00-\x1f]+\.blend", filename, re.IGNORECASE):
        raise ValueError("Each input must use a single .blend filename under inputs/")
    if not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise ValueError("Each .blend must have a frozen SHA-256")
    return filename, source_hash


def request_digest(request):
    return sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def read_request(batch):
    request = read_json(batch / "render-request.json")
    shots = {shot["shot_id"]: shot for shot in request["shots"]}
    order = request["shot_order"]
    if (not order or len(shots) != len(order) or len(shots) != len(request["shots"])
            or set(order) != set(shots) or len({name.casefold() for name in order}) != len(order)
            or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name) for name in order)):
        raise ValueError("Shot IDs must be unique and match shot_order")

    inputs = {}
    names = {}
    for name in order:
        shot = shots[name]
        if shot["fps"] != 24 or shot["settings"]["engine"] != "CYCLES":
            raise ValueError("This runner supports Cycles at 24 fps")
        if shot["settings"]["color_management"]["display_device"] != "sRGB":
            raise ValueError("Review frames must use the sRGB display device")
        if shot["resolution"] != {"width": 1920, "height": 1080, "percentage": 100}:
            raise ValueError("The full-quality delivery profile is 1920×1080 at 100%")
        for key in ("scene", "camera", "view_layer"):
            if not isinstance(shot.get(key), str) or not shot[key]:
                raise ValueError(f"{name} requires an explicit {key}")
        first, last = shot["frame_start"], shot["frame_end"]
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= 100000:
            raise ValueError(f"Invalid frame bounds for {name}")
        descriptor = shot.get("blend", request.get("blend"))
        if not isinstance(descriptor, dict):
            raise ValueError(f"Missing .blend descriptor for {name}")
        filename, source_hash = input_descriptor(descriptor)
        if names.setdefault(filename.casefold(), (filename, source_hash)) != (filename, source_hash):
            raise ValueError(f"Conflicting identities for input file {filename}")
        inputs[name] = (filename, source_hash)
    return request, [shots[name] for name in order], inputs


def stage_input(batch, work, descriptor):
    filename, source_hash = descriptor
    source = batch / "inputs" / filename
    if source.resolve().parent != (batch / "inputs").resolve() or digest(source) != source_hash:
        raise ValueError(f"Input differs from the frozen .blend SHA-256: {filename}")
    blend = work / (source_hash + ".blend")
    if not blend.is_file() or digest(blend) != source_hash:
        publish(source, blend)
    return blend


def shape(shot, stage, preview_percent):
    resolution = shot["resolution"]
    percent = preview_percent if stage == "preview" else resolution["percentage"]
    return [resolution[key] * percent // 100 for key in ("width", "height")], percent


def frame_path(folder, frame):
    return folder / f"frame_{frame:06d}.png"


def stored_frame(path, identity):
    try:
        record = read_json(path.with_suffix(".json"))
        return (record.get("identity") == identity and record.get("frame") == int(path.stem[6:])
                and matches(path, record))
    except (OSError, ValueError, AttributeError):
        return False


def check_png(path, expected, ffmpeg):
    with path.open("rb") as file:
        header = file.read(29)
    if (
        len(header) != 29
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
        or list(struct.unpack(">II", header[16:24])) != expected
        or header[24:26] != b"\x08\x02"
    ):
        raise ValueError(f"Unexpected PNG header: {path.name}")
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
                    "-i", str(path), "-frames:v", "1", "-f", "null", "-"], check=True, timeout=60)


def attestations(log, pending, shot, identity, percent):
    frames = {}
    match = {"status": "ok", "engine": "CYCLES", "scene": shot["scene"],
             "camera": shot["camera"], "view_layer": shot["view_layer"],
             "device": identity["device"], "backend": identity["device"],
             "blender_build": identity["build"], "resolution_percent": percent}
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.startswith("COLAB_FRAME_RESULT "):
                continue
            try:
                actual = json.loads(line.partition(" ")[2])
                frame = actual["frame"]
                if type(frame) is int and frame in pending and all(
                    actual.get(key) == value for key, value in match.items()
                ):
                    frames[frame] = actual
            except (ValueError, TypeError, KeyError):
                continue
    return frames


def last_log_line(log):
    with log.open("rb") as file:
        file.seek(max(0, log.stat().st_size - 6000))
        lines = file.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if line.startswith("WORKER_ERROR: "):
            return line[:500]
    return next((line[:500] for line in reversed(lines) if line.strip()), "no worker output")


def render_segment(blender, blend, shot, folder, frames, identity, device, gpu, threads, work, ffmpeg,
                   percent, expected, timeout):
    pending = [frame for frame in frames if not stored_frame(frame_path(folder, frame), identity)]
    reused = len(frames) - len(pending)
    errors = []
    if pending:
        with TemporaryDirectory(dir=work, prefix="segment-") as stage:
            stage = Path(stage)
            for attempt in (1, 2):
                attempt_dir = stage / str(attempt)
                attempt_dir.mkdir()
                job = {
                    "scene": shot["scene"], "camera": shot["camera"], "view_layer": shot["view_layer"],
                    "frames": pending, "output_dir": str(attempt_dir), "resolution_percent": percent,
                    "device": device, "expected_gpu": gpu, "fps": shot["fps"], "threads": threads,
                    "dimensions": [shot["resolution"]["width"], shot["resolution"]["height"]],
                    "expected_engine": "CYCLES",
                }
                write_json(attempt_dir / "work.json", job)
                log = attempt_dir / "blender.log"
                command = [str(blender), "-b", "--factory-startup", "--offline-mode", "--disable-autoexec", str(blend),
                           "--python-exit-code", "1", "--python", str(WORKER), "--", str(attempt_dir / "work.json")]
                reason = None
                with log.open("w", encoding="utf-8") as output:
                    try:
                        done = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
                        if done.returncode:
                            reason = f"Blender exited {done.returncode}"
                    except subprocess.TimeoutExpired:
                        reason = f"Blender segment timed out after {timeout} seconds"
                if reason:
                    errors.append(f"{reason}: {last_log_line(log)}")
                log_name = f"segment_{frames[0]:06d}_{frames[-1]:06d}_try{attempt}.log"
                publish(log, folder / log_name)
                for frame, runtime in attestations(log, pending, shot, identity, percent).items():
                    source = frame_path(attempt_dir, frame)
                    try:
                        check_png(source, expected, ffmpeg)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        errors.append(f"Invalid or missing frame {frame}; see {log_name}")
                        continue
                    destination = frame_path(folder, frame)
                    file_receipt = publish(source, destination)
                    write_json(destination.with_suffix(".json"), {
                        **file_receipt, "identity": identity, "frame": frame, "runtime": runtime,
                    })
                pending = [frame for frame in pending if not stored_frame(frame_path(folder, frame), identity)]
                if not pending:
                    break
    if pending and not errors:
        errors.append("No eligible per-frame attestation for the remaining frames")
    receipt = {"frames": frames, "reused": reused, "rendered": len(frames) - reused - len(pending),
               "missing": pending, "identity": identity, "errors": errors}
    write_json(folder / f"segment_{frames[0]:06d}_{frames[-1]:06d}.json", receipt)
    print(json.dumps({"shot": shot["shot_id"], "range": [frames[0], frames[-1]], **receipt}), flush=True)
    return receipt


def frame_identity(stage_identity, shot, descriptor):
    return {**stage_identity, "shot_id": shot["shot_id"], "source_sha256": descriptor[1]}


def expected_frames(shot):
    return list(range(shot["frame_start"], shot["frame_end"] + 1))


def frame_members(batch, stage, shot, frames):
    folder = batch / "checkpoints" / stage / shot["shot_id"]
    for frame in frames:
        path = frame_path(folder, frame)
        yield path.relative_to(batch).as_posix()
        yield path.with_suffix(".json").relative_to(batch).as_posix()


def snapshot_segment(batch, directory, stage, shot, receipt):
    verified = [frame for frame in receipt["frames"] if frame not in receipt["missing"]]
    if not verified:
        return
    identity = receipt["identity"]
    names = ["render-request.json", f"checkpoints/{stage}/identity.json",
             *frame_members(batch, stage, shot, verified)]
    first, last = receipt["frames"][0], receipt["frames"][-1]
    key = [(name, digest(batch / name)) for name in names]
    fingerprint = sha256(json.dumps(key).encode()).hexdigest()[:16]
    target = directory / f"{stage}-{shot['shot_id']}-{first:06d}-{last:06d}-{fingerprint}.zip"
    archived = save_bundle(batch, target, names)
    archived.update(stage=stage, shot_id=shot["shot_id"], frames=verified,
                    request_sha256=identity["request_sha256"])
    write_json(target.with_suffix(".zip.sha256.json"), archived)
    print("CHECKPOINT " + json.dumps(archived), flush=True)


def render_shots(shots, batch, stage, stage_identity, inputs, args, work, blender, ffmpeg):
    guard = batch / "checkpoints" / stage / "identity.json"
    if not guard.exists():
        write_json(guard, stage_identity)
    results = {}
    for shot in shots:
        name = shot["shot_id"]
        if args.shot and name != args.shot:
            continue
        frames = (list(range(args.frames[0], args.frames[1] + 1)) if args.frames else expected_frames(shot))
        result = {"state": "completed", "missing": [], "errors": [], "segments": 0}
        results[name] = result
        try:
            blend = stage_input(batch, work, inputs[name])
            identity = frame_identity(stage_identity, shot, inputs[name])
            expected, percent = shape(shot, stage, args.preview_percent)
            folder = batch / "checkpoints" / stage / name
            for start in range(0, len(frames), args.segment_frames):
                part = frames[start:start + args.segment_frames]
                receipt = render_segment(blender, blend, shot, folder, part, identity, args.device, args.gpu,
                                         args.threads, work, ffmpeg, percent, expected, args.timeout)
                result["segments"] += 1
                if args.checkpoint_dir:
                    snapshot_segment(batch, args.checkpoint_dir, stage, shot, receipt)
                if receipt["missing"]:
                    result.update(state="failed", missing=receipt["missing"] + frames[start + len(part):],
                                  errors=receipt["errors"])
                    break
        except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result.update(state="failed", errors=[str(error)])
            # Preserve a list of the actual missing frames if failure occurred
            # after earlier segments were already published.
            identity = frame_identity(stage_identity, shot, inputs[name])
            folder = batch / "checkpoints" / stage / name
            result["missing"] = [frame for frame in frames if not stored_frame(frame_path(folder, frame), identity)]
        if result["state"] != "completed":
            print("SHOT_FAILED " + json.dumps({"stage": stage, "shot": name, **result}), flush=True)
    return results


def review_shots(shots, batch, stage, stage_identity, inputs, preview_percent, work, ffmpeg, ffprobe):
    destination = batch / "delivery" / stage
    ordered = []
    slots = []
    width, height = shape(shots[0], stage, preview_percent)[0]
    with TemporaryDirectory(dir=work, prefix="review-") as scratch:
        scratch = Path(scratch)

        def encode(name, frames):
            movie = scratch / f"{name}.mp4"
            failures = []
            for _ in range(2):
                try:
                    info = encode_sequence(frames, movie, ffmpeg=ffmpeg, ffprobe=ffprobe, width=width, height=height)
                    published = destination / movie.name
                    publish(movie, published)
                    info.update(path=published.name)
                    write_json(published.with_suffix(".json"), info)
                    return info, failures
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    failures.append(str(error))
                    movie.unlink(missing_ok=True)
            return None, failures

        for shot in shots:
            name = shot["shot_id"]
            folder = batch / "checkpoints" / stage / name
            frames = expected_frames(shot)
            paths = [frame_path(folder, frame) for frame in frames]
            identity = frame_identity(stage_identity, shot, inputs[name])
            missing = [frame for frame, path in zip(frames, paths) if not stored_frame(path, identity)]
            errors = []
            video = None
            if not missing:
                video, errors = encode("shot_" + name, paths)
            slot = {"shot_id": name, "first_frame": len(ordered), "frames": len(frames),
                    "state": "complete", "video": video, "missing": missing, "errors": errors}
            if video is None:
                card = scratch / f"missing_{name}.png"
                placeholder(card, name, width=width, height=height)
                paths = [card] * len(frames)
                slot["state"] = "placeholder"
            ordered.extend(paths)
            slots.append(slot)
        all_good = all(slot["state"] == "complete" for slot in slots)
        combined, failures = encode("combined" if all_good else "combined-incomplete", ordered)
        state = "complete" if all_good and combined else "partial" if any(
            slot["state"] == "complete" for slot in slots
        ) else "failed"
        summary = {"state": state, "complete": state == "complete", "stage": stage,
                   "video": combined, "slots": slots, "errors": failures}
        write_json(destination / "review.json", summary)
        print("REVIEW " + json.dumps(summary), flush=True)
        return summary


def export_members(batch, kind, shots, inputs):
    names = ["render-request.json"]
    if kind in {"inputs", "all"}:
        for filename, expected in sorted(set(inputs.values())):
            source = batch / "inputs" / filename
            if source.resolve().parent != (batch / "inputs").resolve() or digest(source) != expected:
                raise ValueError(f"Input hash differs: {filename}")
            names.append(f"inputs/{filename}")
    if kind == "inputs":
        return names
    for stage in ("preview", "final"):
        guard = batch / "checkpoints" / stage / "identity.json"
        if not guard.is_file():
            continue
        identity = read_json(guard)
        names.append(guard.relative_to(batch).as_posix())
        for shot in shots:
            folder = guard.parent / shot["shot_id"]
            expected = frame_identity(identity, shot, inputs[shot["shot_id"]])
            frames = [frame for frame in expected_frames(shot) if stored_frame(frame_path(folder, frame), expected)]
            names.extend(frame_members(batch, stage, shot, frames))
        if kind not in {"results", "all"}:
            continue
        report = batch / "delivery" / stage / "review.json"
        if not report.is_file():
            continue
        info = read_json(report)
        names.append(report.relative_to(batch).as_posix())
        videos = [info["video"], *(slot["video"] for slot in info["slots"])]
        for video in filter(None, videos):
            filename = video["path"]
            if not re.fullmatch(r"(?:combined(?:-incomplete)?|shot_[A-Za-z0-9_-]+)\.mp4", filename):
                raise ValueError("Invalid review path in delivery receipt")
            path = report.parent / filename
            if path.resolve().parent != report.parent.resolve() or video["sha256"] != digest(path):
                raise ValueError(f"Review output hash differs: {path.name}")
            names.extend([path.relative_to(batch).as_posix(), path.with_suffix(".json").relative_to(batch).as_posix()])
    if (batch / "run-report.json").is_file() and kind in {"results", "all"}:
        names.append("run-report.json")
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "plan", "render", "review", "run", "export", "restore"))
    parser.add_argument("--work", type=Path, default=Path("render-work"))
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--blender", type=Path)
    parser.add_argument("--stage", choices=("preview", "final"), default="preview")
    parser.add_argument("--device", choices=("CPU", "CUDA"), default="CUDA")
    parser.add_argument("--gpu", default="")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--preview-percent", type=int, default=25)
    parser.add_argument("--segment-frames", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum seconds per Blender segment attempt")
    parser.add_argument("--shot")
    parser.add_argument("--frames", type=int, nargs=2, metavar=("FIRST", "LAST"))
    parser.add_argument("--checkpoint-dir", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--kind", choices=("inputs", "checkpoints", "results", "all"), default="results")
    args = parser.parse_args()
    work = args.work.resolve()
    if args.action == "bootstrap":
        with batch_lock(work):
            print(bootstrap(work, (args.cache or work / "cache").resolve()))
        return
    if not args.batch:
        parser.error("Specify the authorized batch directory with --batch")
    batch = args.batch.resolve()
    if args.action == "plan":
        execute_batch(args, parser, batch, work)
    else:
        with batch_lock(batch):
            execute_batch(args, parser, batch, work)


def execute_batch(args, parser, batch, work):
    if args.action in {"export", "restore"} and args.archive is None:
        parser.error("Export and restore require --archive")
    if args.action == "restore":
        if not args.sha256:
            parser.error("Restore requires the independently recorded --sha256")
        print(json.dumps(restore_bundle(batch, args.archive.resolve(), args.sha256)))
        return

    request, shots, inputs = read_request(batch)
    if args.action == "export":
        print(json.dumps(save_bundle(batch, args.archive, export_members(batch, args.kind, shots, inputs))))
        return
    if args.segment_frames < 1 or not 1 <= args.preview_percent <= 100 or args.threads < 0 or args.timeout < 1:
        parser.error("Invalid segment size, preview percentage, thread count or timeout")
    sizes = shape(shots[0], "preview", args.preview_percent)[0]
    if min(sizes) < 48 or sizes[0] < 240 or any(value % 2 for value in sizes):
        parser.error("Preview dimensions must be even and at least 240×48")
    if args.frames and not args.shot:
        parser.error("--frames requires --shot")
    chosen = next((shot for shot in shots if shot["shot_id"] == args.shot), None)
    if args.shot and chosen is None:
        parser.error("Unknown shot")
    if args.frames and not chosen["frame_start"] <= args.frames[0] <= args.frames[1] <= chosen["frame_end"]:
        parser.error("Requested frames are outside the frozen shot interval")
    if args.action == "plan":
        jobs = []
        for shot in [chosen] if chosen else shots:
            frames = list(range(args.frames[0], args.frames[1] + 1)) if args.frames else expected_frames(shot)
            for start in range(0, len(frames), args.segment_frames):
                part = frames[start:start + args.segment_frames]
                jobs.append({"shot": shot["shot_id"], "stage": args.stage, "first": part[0], "last": part[-1]})
        print(json.dumps({"request_sha256": request_digest(request), "segments": jobs}))
        return

    blender = (args.blender or work / RELEASE["directory"] / "blender").resolve()
    version, build = checked_version(blender)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe must be available in PATH")
    work.mkdir(parents=True, exist_ok=True)
    if args.action in {"run", "review"}:
        preflight_tools(ffmpeg, ffprobe, work)
    base = {"request_sha256": request_digest(request), "version": version, "build": build,
            "device": args.device, "gpu": args.gpu, "threads": args.threads, "worker": digest(WORKER)}
    stages = ("preview", "final") if args.action == "run" else (args.stage,)
    identities = {}
    # Inspect both guards before starting so a stale final identity cannot be
    # discovered only after paid preview rendering.
    for stage in stages:
        identity = {**base, "stage": stage}
        if stage == "preview":
            identity["resolution_percent"] = args.preview_percent
        guard = batch / "checkpoints" / stage / "identity.json"
        if guard.exists() and read_json(guard) != identity:
            raise RuntimeError("Render identity differs. Use a fresh batch for changed settings or legacy receipts")
        identities[stage] = identity
    result = {}
    complete = True
    for stage in stages:
        identity = identities[stage]
        result[stage] = {}
        if args.action in {"run", "render"}:
            rendered = render_shots(shots, batch, stage, identity, inputs, args, work, blender, ffmpeg)
            result[stage]["shots"] = rendered
            complete &= all(shot["state"] == "completed" for shot in rendered.values())
        if args.action in {"run", "review"}:
            review = review_shots(shots, batch, stage, identity, inputs, args.preview_percent, work, ffmpeg, ffprobe)
            result[stage]["review"] = review
            complete &= review["complete"]
    write_json(batch / "run-report.json", {"complete": complete, "stages": result})
    if not complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
