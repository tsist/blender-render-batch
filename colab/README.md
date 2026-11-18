# Run Cycles batches on Colab

Use the notebook or the same Python runner through a Colab runtime file interface. The Linux bootstrap pins Blender 5.2.1 x86-64 and verifies the official archive by SHA-256. To use an existing matching executable, pass `--blender` to the render and review commands. The runner expects Python 3.12 or newer and FFmpeg with zscale, libx264 and ffprobe. CPU and CUDA use the saved scene settings.

A batch contains `render-request.json` and the exact source files under `inputs/`. See [the two-shot manifest](render-request.example.json) for fields. Replace the example filenames, hashes and scene identifiers with your own. Each shot can refer to a different self-contained `.blend`. An existing single-source request may use one top-level `blend` descriptor. Use a fresh batch for changes to source bytes, render settings, device, Blender build or runner. Legacy frame receipts predate per-frame camera verification and require rerendering under the new identity.

The current review profile is Cycles, 1920×1080 at 24 fps, SDR with sRGB display and RGB 8-bit PNG. Preview and final results are separated. Select shots and cameras explicitly in order. The worker binds each frame to the requested camera, including frames with other camera markers. Only self-contained Blender sources are supported.

## Prepare and transfer

Run the commands from this directory, with batch paths matching your system.

```text
python colab_environment.py plan --batch BATCH --stage final --segment-frames 3
python colab_environment.py export --batch BATCH --kind inputs --archive INPUT.zip
python colab_environment.py restore --batch RUNTIME_BATCH --archive INPUT.zip --sha256 INPUT_SHA256
python colab_environment.py bootstrap --work RUNTIME_WORK
```

The export receipt gives `path`, `bytes` and `sha256`. Record the digest outside the temporary runtime and use it for restore. Open [the notebook](Blender-Cycles-Colab.ipynb) in Colab or use the runner from your runtime session. Choose an authorized account, compute allowance and private storage. The notebook leaves compute disabled by default. Use `transfer` for the Files panel or provider API. Use `mounted-drive` only with the account holder's authorization and selected private destination.

## Render and recover

```text
python colab_environment.py run --batch RUNTIME_BATCH --work RUNTIME_WORK --device CUDA --gpu T4 --checkpoint-dir CHECKPOINTS
```

`run` renders and independently reviews preview and final. A segment writes a `CHECKPOINT` receipt and an immutable ZIP of any verified frames in that range. An attached persistent checkpoint directory can receive these packages as the runner produces them. With temporary runtime storage, copy and verify each package at the authorized destination. For a managed unattended transfer, use one command per segment from the plan and wait for persistent readback before proceeding to the next one.

```text
python colab_environment.py render --batch RUNTIME_BATCH --work RUNTIME_WORK --stage final --device CUDA --gpu T4 --shot SHOT --frames FIRST LAST --checkpoint-dir CHECKPOINTS
python colab_environment.py review --batch RUNTIME_BATCH --work RUNTIME_WORK --stage final --device CUDA --gpu T4
```

Use the same pattern for preview. A segment accepts valid frame attestations from a worker that exits after earlier successful frames and retries only missing frames once. Failure in one shot allows the next independent shot to run. The partial group has a title card in each failed shot's original slot. Queue a fresh runtime after interruption, restore the original input ZIP and checkpoint ZIPs using their recorded SHA-256, and repeat the same commands to reuse verified frames. Keep a single writer per batch on any shared remote destination. The local runtime prevents concurrent writers to the same batch.

## Take delivery

```text
python colab_environment.py export --batch RUNTIME_BATCH --kind results --archive RESULTS.zip
```

`delivery/<stage>/review.json` is the authoritative record of the combined video and shot slots. Each successful shot provides its own MP4, while `combined-incomplete.mp4` denotes a partial group. A nonzero render or review result can still contain valid deliverables. Read the state and errors in `run-report.json`, verify the archive after transfer and decode the delivery independently before releasing the runtime. Keep the input bundle separately or use `--kind all` to export inputs and results together.

Generate the public notebook from its bundled source with `python build_notebook.py`.
