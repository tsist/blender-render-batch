# Colab render batches

The cloud runner supports self-contained Cycles projects on Linux. It validates the Blender download with the pinned upstream checksum and checks the source identity for every shot. Use the default no-mount notebook mode when the account permits only a separately authorized file-transfer path. The optional native Drive mount may require Google to ask for additional access.

## Prepare and run

1. Read the cloud runner help. Prepare a JSON request and put the saved, self-contained projects under `inputs/` in the batch directory. Run [the procedural example](https://github.com/tsist/blender-render-batch/blob/main/examples/create_demo.py) with `-- NEW_DIRECTORY --backend colab` to generate a multi-project manifest and measured hashes. Split intentional camera changes into separate requests.
2. Confirm the compute entitlement, the authorized account and the destination before creating a runtime. Select an available CUDA GPU. Never log signed URLs, headers, OAuth credentials or proxy tokens.
3. Use [the portable notebook](https://github.com/tsist/blender-render-batch/blob/main/colab/Blender-Cycles-Colab.ipynb) or use the official managed Colab MCP runtime and file APIs to transfer the runner and a hashed input archive. First run `bootstrap --work WORK`. Use only a single writer for each batch.

```bash
python colab/colab_environment.py plan --batch BATCH --stage final --segment-frames 3
python colab/colab_environment.py run --batch BATCH --work WORK --device CUDA --gpu T4 --checkpoint-dir CHECKPOINTS
```

Use `--help` on each action to confirm its accepted options. A routine run writes the full preview and final when the source is supported. A partial run returns a nonzero status and may still deliver successful shots. Read `delivery/<stage>/review.json` before retrying.

## Unattended storage and recovery with a transfer connector

- Export the frozen inputs with `export --batch BATCH --kind inputs --archive inputs.zip`. Persist the archive and its receipt in the authorized private storage. Transfer only the data this batch needs.
- Run the ranges from `plan` as separate calls to `render --batch BATCH --work WORK --stage STAGE --device CUDA --gpu T4 --shot SHOT --frames FIRST LAST --checkpoint-dir CHECKPOINTS`. For each call, take the resulting checkpoint ZIP and SHA-256 from the runtime file API, write into the private destination, then read it back or verify the remote checksum and length before advancing. If the platform rejects further compute, give the user the last durable receipts.
- On a fresh runtime, restore the verified inputs first, then the completed checkpoint ZIPs, using `restore --batch BATCH --archive FILE --sha256 DIGEST`. Recheck identities, resume only missing work and keep shot order. Legacy cloud render receipts without camera attestation require a fresh render. Old request layouts can still be read.
- Generate the group review using `review --batch BATCH --work WORK --stage final --device CUDA --gpu T4`. Export `--kind results` for the final media and receipts. Return the local originals and H.264 review, with a visibly incomplete group when needed. Release this batch's runtime after confirming durable storage and local recovery.

With hosted MCP, a local response timeout does not imply execution stopped. Keep the runtime, session, execution UUID and segment receipt. Use official reattachment semantics and inspect the segment before trying again. When moving to another Linux GPU service, recheck the Blender build, GPU/driver, durable storage, and checksum transfers. Cloud EEVEE needs independent validation.

[Hosted MCP reference](https://developers.google.com/colab/api/reference/mcp) · [Runtime file API](https://developers.google.com/colab/api/reference/mcp/tools_list/get_runtime) · [Colab FAQ](https://research.google.com/colaboratory/faq.html)
