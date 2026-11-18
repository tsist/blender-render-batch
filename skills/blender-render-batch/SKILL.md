---
name: blender-render-batch
description: Submit and monitor ordered Blender animation shots, deliver lossless frames and review movies, and resume missing frames after an interruption. Use for saved Blender projects, local batch rendering, or a user-approved Colab run.
---

# Blender render batches

Use the saved projects and intended review format to plan the batch. Preserve the selected render engine, camera, color management, and quality settings. Use a separate batch for changed inputs.

## Start a batch

1. Resolve the ordered shots and each saved file, scene, view layer, camera, inclusive frame range, output format, and device. Expand timeline camera cuts into explicit shots. Identify external assets and caches before freezing the inputs.
2. For Windows, read [the local CLI and panel guide](references/local.md), then inspect `render batch submit --help` on the installed version. Use the actual device ID from `render devices` when the backend requires one.
3. Record the submitted manifest, batch ID and directory. A normal run makes a full-speed low-resolution preview and then renders the final with the saved settings. Read the same batch directory in the Blender panel.

## Monitor and deliver

- Use `render batch wait ID --wait-seconds 30` to wait for changes. Inspect the reported stage and messages before retrying. Device activity, disk space and queued work can affect completion time. Report remaining time as a range using same-device completed frame timings, remaining frames, and the time for initialization, transfer or encoding. If the deadline is at risk, offer scheduling, segmentation or supported device choices within the agreed render settings.
- On resume, use the same batch directory and software. Confirm the reported shot/frame identities, PNG checksums and actual missing frame set. For changed inputs, use a new batch and document why any old frames are safe to reuse. With no matching input and settings evidence, treat those frames as belonging to the old batch.
- Deliver each successful shot and the ordered review. An incomplete review retains failed shots' slots and durations. Verify the media decodes, timestamps are continuous and the agreed color tags are present. Watch at normal speed for flicker, detail dropouts and denoising trails, and inspect representative PNGs at full size.
- Compare speed with repeated same-device runs of the same source and frame range. Show a continuous comparison, original-size detail and timing. Save a quality preset only after the user approves the visual result and its applicability.

## Colab

Default to local execution. For a user-approved cloud run, follow [the Colab environment and checkpoint guide](references/colab.md). Use the authorized account, compute entitlement and private project storage for that request. Confirm enough policy evidence before requesting a runtime, retain verified checkpoints, and release only the runtime owned by the batch.
