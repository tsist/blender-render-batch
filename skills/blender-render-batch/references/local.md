# Windows local batch

Get the matching CLI source bundle and add-on ZIP from [the public release](https://github.com/tsist/blender-render-batch/releases). Run the launcher in the release root using PowerShell 7 and Python 3.13. Point `--blender` at Blender 5.2.1 and provide FFmpeg and FFprobe. FFmpeg needs PNG, `drawtext`, `zscale`, and `libx264`.

```powershell
.\blenderctl.ps1 --blender $Blender --jobs-dir $Jobs render devices
.\blenderctl.ps1 --blender $Blender --jobs-dir $Jobs --async render batch submit --manifest $Request --ffmpeg $FFmpeg --ffprobe $FFprobe
.\blenderctl.ps1 --jobs-dir $Jobs render batch wait $BatchId --wait-seconds 30
.\blenderctl.ps1 --jobs-dir $Jobs render batch cancel $BatchId
.\blenderctl.ps1 --jobs-dir $Jobs --async render batch resume $BatchId
```

Global options precede `render`. Keep the job root and batch ID for handoff. The batch directory is `<jobs-dir>/b/<id>`. The case-sensitive device ID is returned by Blender. EEVEE uses `GRAPHICS`, Cycles accepts `CPU`, `CUDA`, or `OPTIX`.

## Request 1.0

- Set `version: "1.0"`, `backend: "local"` and `delivery: {"width":1920,"height":1080,"fps":[24,1],"color":"SDR","review_codec":"h264"}`. The saved scenes must use 24 fps and the delivery aspect ratio.
- Supply ordered `shots`. Each shot uses a unique short ID, absolute `project` path, `expected_sha256`, `scene`, `view_layer`, `camera`, `frame_start`, `frame_end`, `settings: "source"` and `device`.
- Add SHA-256 descriptors for relative `resources`, and a source-bound `cache_manifest` for managed geometry caches. The CLI freezes the inputs. An unsupported compositor, VSE timeline, pixel format or conflicting camera cut fails explicitly.
- `cpu_threads` and `min_free_mb` set job resources. Read each reported shortage before resuming. Device activity is an admission observation and remains dependent on available driver telemetry.

## Reading the results

Install the add-on in Blender and use **渲染批次 (Render Batch)** in the 3D View sidebar. Open the selected batch directory, or use project discovery to find a matching batch in the configured job root. Open the reported MP4 directly from the panel. For a resume request, point the add-on to this release's PowerShell launcher and read status and the launcher log.

Use `status.json` for stage, per-shot progress, failure and review paths. A multi-shot review manifest records each shot's original slot and whether that slot contains verified frames or a placeholder. Keep the source snapshot and the matching `preview-frames.json` / `final-frames.json` when handing over a batch.

If the supervisor stopped unexpectedly, check whether an old worker is still active before requesting resume. Keep private logs inside the project while sharing delivery artifacts.
