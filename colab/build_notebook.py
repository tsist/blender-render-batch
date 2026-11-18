"""Build the distributable notebook from the same Colab runner sources."""

import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from textwrap import dedent
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


PACKAGE = Path(__file__).resolve().parent
MEMBERS = ("blender-release.json", "blender_worker.py", "checkpoint_archive.py",
           "colab_environment.py", "render_batch_media.py")


def cell(kind, cell_id, source):
    value = {"cell_type": kind, "id": cell_id, "metadata": {},
             "source": dedent(source).lstrip("\n").splitlines(keepends=True)}
    if kind == "code":
        value.update(execution_count=None, outputs=[])
    return value


def package_cell():
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name in MEMBERS:
            archive.writestr(ZipInfo(name), (PACKAGE / name).read_bytes(),
                             compress_type=ZIP_DEFLATED, compresslevel=8)
    payload = buffer.getvalue()
    encoded = base64.b64encode(payload).decode()
    literal = "\n".join("    " + repr(encoded[start:start + 88]) for start in range(0, len(encoded), 88))
    return f"""# @title Load and verify the bundled runner
import base64
import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

expected_members = {MEMBERS!r}
payload = base64.b64decode((
{literal}
), validate=True)
if hashlib.sha256(payload).hexdigest() != {sha256(payload).hexdigest()!r}:
    raise RuntimeError("The embedded runner did not pass SHA-256 verification")
package_dir = Path("/content/render-batch-runner") / {sha256(payload).hexdigest()[:16]!r}
package_dir.mkdir(parents=True, exist_ok=True)
with ZipFile(BytesIO(payload)) as archive:
    if sorted(archive.namelist()) != sorted(expected_members) or archive.testzip():
        raise RuntimeError("The embedded runner did not pass member verification")
    for name in expected_members:
        target = package_dir / name
        contents = archive.read(name)
        if target.exists() and target.read_bytes() != contents:
            raise RuntimeError(f"Runner file differs on this runtime: {{name}}")
        if not target.exists():
            target.write_bytes(contents)
print("Verified runner:", package_dir)
"""


def main():
    overview = """
    # Blender Cycles Render Batch

    Copy this notebook to a workspace you control. Use the Google account and compute allowance authorized for this batch.
    Select the desired runtime before rendering. The configuration leaves compute disabled until you affirm the selected account and allowance.

    1. Create an input bundle from a frozen `render-request.json` and its self-contained `.blend` files using the
       runner's `export --kind inputs` command. Upload the ZIP through the Colab Files panel and record the source SHA-256.
    2. Enter the uploaded basename and independently recorded SHA-256 in `ARCHIVES_JSON`. For recovery, list the input ZIP
       first and any saved segment bundles afterward. A new runtime restores the same files from these ZIPs.
    3. Choose `transfer` to work through Colab Files or the runtime file API. Each segment publishes a verified ZIP
       and JSON receipt in `/content/render-transfer`. Copy those bundles to authorized persistent storage before
       releasing the runtime. For managed runs, submit one segment per call and wait for verified durable transfer.
    4. Choose `mounted-drive` only when the account holder has authorized the required mount and supplied a private
       subdirectory. This mode saves verified segment bundles and review results there during processing.
    """
    configuration = """
    # @title Batch configuration
    ALLOW_COMPUTE = False  # @param {type:"boolean"}
    STORAGE = "transfer"  # @param ["transfer", "mounted-drive"]
    DRIVE_SUBDIRECTORY = ""  # @param {type:"string"}
    ARCHIVES_JSON = "[]"  # @param {type:"string"}
    ACTION = "plan"  # @param ["plan", "run", "render", "review"]
    STAGE = "final"  # @param ["preview", "final"]
    DEVICE = "CUDA"  # @param ["CUDA", "CPU"]
    GPU_NAME = ""  # @param {type:"string"}
    SEGMENT_FRAMES = 3  # @param {type:"integer"}
    SHOT = ""  # @param {type:"string"}
    LIMIT_FRAME_RANGE = False  # @param {type:"boolean"}
    FIRST = 1  # @param {type:"integer"}
    LAST = 1  # @param {type:"integer"}
    """
    execute = """
    # @title Restore, render and export
    from datetime import datetime, timezone
    import json
    from pathlib import Path
    import re
    import subprocess
    import sys

    batch = Path("/content/render-batch")
    work = Path("/content/render-work")
    checkpoints = Path("/content/render-transfer")
    cache = work / "cache"
    if ACTION != "plan" and not ALLOW_COMPUTE:
        raise RuntimeError("Confirm the authorized account and compute allowance before running a job")
    if STORAGE == "mounted-drive":
        if not DRIVE_SUBDIRECTORY or Path(DRIVE_SUBDIRECTORY).is_absolute():
            raise ValueError("Enter an authorized subdirectory relative to My Drive")
        from google.colab import drive
        drive.mount("/content/drive")
        my_drive = Path("/content/drive/MyDrive").resolve()
        target = (my_drive / DRIVE_SUBDIRECTORY).resolve()
        if target == my_drive or not target.is_relative_to(my_drive):
            raise ValueError("The selected folder must be inside My Drive")
        checkpoints = target / "checkpoints"
        cache = target / "cache"

    cli = [sys.executable, str(package_dir / "colab_environment.py")]
    archive_list = json.loads(ARCHIVES_JSON)
    if not isinstance(archive_list, list):
        raise ValueError("ARCHIVES_JSON must be a list of file and sha256 objects")
    for item in archive_list:
        name, digest = item["file"], item["sha256"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*[.]zip", name, re.IGNORECASE):
            raise ValueError("Supply uploaded archive basenames")
        archive = Path("/content") / name
        subprocess.run([*cli, "restore", "--batch", str(batch), "--archive", str(archive),
                        "--sha256", digest], check=True)

    command = [*cli, ACTION, "--batch", str(batch), "--work", str(work),
               "--stage", STAGE, "--device", DEVICE, "--gpu", GPU_NAME,
               "--segment-frames", str(SEGMENT_FRAMES), "--checkpoint-dir", str(checkpoints)]
    if SHOT:
        command.extend(["--shot", SHOT])
    if LIMIT_FRAME_RANGE:
        command.extend(["--frames", str(FIRST), str(LAST)])
    if ACTION == "plan":
        subprocess.run(command, check=True)
    else:
        subprocess.run([*cli, "bootstrap", "--work", str(work), "--cache", str(cache)], check=True)
        result = subprocess.run(command, check=False)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        exported = subprocess.run([*cli, "export", "--batch", str(batch), "--kind", "results",
                                   "--archive", str(checkpoints / f"results-{stamp}.zip")], check=False)
        if exported.returncode:
            raise RuntimeError("Result export failed. Inspect the stage reports and preserve verified bundles.")
        if result.returncode:
            print("At least one stage is incomplete. Inspect run-report.json and delivery/<stage>/review.json.")
    """
    handoff = """
    ## Read the handoff

    `CHECKPOINT` records the archive path, SHA-256, byte count and verified frames. A transfer-mode
    runtime file is durable only after it is copied to the authorized persistent destination
    and verified there. A failed shot retains its verified frames, while completed shots and
    the duration-preserving incomplete review are available in the results ZIP. Read each
    `delivery/<stage>/review.json` for the authoritative group path, shot order and slot status.
    """
    notebook = {
        "cells": [cell("markdown", "overview", overview), cell("code", "configuration", configuration),
                  cell("code", "load-package", package_cell()), cell("code", "execute", execute),
                  cell("markdown", "handoff", handoff)],
        "metadata": {"colab": {"provenance": []},
                     "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python"}},
        "nbformat": 4, "nbformat_minor": 5,
    }
    output = PACKAGE / "Blender-Cycles-Colab.ipynb"
    output.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(output.name)


if __name__ == "__main__":
    main()
