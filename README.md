# Blender Render Batch

Render an ordered list of Blender animation shots in the background. Get preview and final review movies, lossless frame sequences, and per-frame verification records. The Blender sidebar can inspect the batch and request resume. Failed shots can leave marked placeholders in the ordered review while completed shots remain available.

## Install and run on Windows

1. Install the **Render Batch Workflow** ZIP from [GitHub Releases](https://github.com/tsist/blender-render-batch/releases) using Blender's **Preferences → Get Extensions → Install from Disk**. Use Blender 5.3.0 or 5.2.1 LTS on Windows.
2. Keep the release source or CLI bundle on disk. Use PowerShell 7, the selected Blender installation's bundled Python or Python 3.13, and FFmpeg plus FFprobe with `zscale`, `libx264`, and `drawtext`. Install Blender and FFmpeg separately.
3. Generate an entirely procedural demo. The argument after `--` is a new directory.

```powershell
$Blender = (Get-Command blender.exe).Source
$FFmpeg = (Get-Command ffmpeg.exe).Source
$FFprobe = (Get-Command ffprobe.exe).Source
& $Blender --background --factory-startup --offline-mode --disable-autoexec --python-exit-code 49 --python examples/create_demo.py -- demo-output
.\blenderctl.ps1 --blender $Blender --async render batch submit --manifest demo-output/render-request.json --ffmpeg $FFmpeg --ffprobe $FFprobe
.\blenderctl.ps1 render batch wait $BatchId --wait-seconds 30
```

You may set `BLENDERCTL_PYTHON` to the path of your Python interpreter. Use `--jobs-dir` with the same absolute path for every operation on the same batch.

Open the **渲染批次 (Render Batch)** tab in Blender's 3D View sidebar. Set the batch directory from the CLI receipt, or locate a batch for the saved project from the job root. To resume from the panel, select the release's `blenderctl.ps1` launcher.

See the [agent skill](skills/blender-render-batch/SKILL.md), [local manifest and commands](skills/blender-render-batch/references/local.md), and [Colab guide](skills/blender-render-batch/references/colab.md).

## Scope

The 1920×1080, 24 fps SDR profile produces lossless PNG frames and a BT.709 H.264 review. On Windows, the Blender 5.3.0 migration was tested with a CPU Cycles batch. The Blender 5.2.1 baseline covers Cycles and EEVEE. The separate Linux Cycles workflow keeps its verified Blender 5.2.1 pin. GPU admission uses driver telemetry when available.

## License

GPL-3.0-or-later. The local runner incorporates code from [blenderctl](https://github.com/tsist/blenderctl). See [LICENSE](LICENSE) and [NOTICE](NOTICE.md).
