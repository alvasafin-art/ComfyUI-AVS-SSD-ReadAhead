# ComfyUI-AVS-SSD-ReadAhead

**Speed up ComfyUI model loading and model switching on slower SSDs on Windows.**

> **Status:** Windows-only, GPU-vendor neutral in the ReadAhead code path.  
> **Current patch:** v1.0.0, rebuilt around the current lifecycle and protections of ComfyUI PR #15538 as of 2026-08-27.  
> **Activation:** standalone custom node + Application Settings toggle. No ReadAhead CLI argument is added or required.

## What it does

AVS SSD ReadAhead starts a short-lived helper Python process when ComfyUI begins loading a large `.safetensors` or `.sft` file. The helper reads the file sequentially so Windows can populate its normal file cache while ComfyUI continues through its standard model-loading path.

ComfyUI remains authoritative. The helper never replaces the loader and never modifies model weights, sampling, VRAM residency, mmap ownership, device transfers, unload policy, GPU synchronization, or image quality.

The optimization is mainly useful when storage I/O is a bottleneck: SATA SSDs, older SSDs, external SSDs, or other relatively slow storage. Fast NVMe systems may see a smaller benefit or no meaningful benefit.

## v1.0 safety model

The old standalone implementation has been simplified to follow the current PR #15538 behavior instead of carrying separate tuning logic.

ReadAhead now uses:

- `.safetensors` / `.sft` only;
- minimum candidate file size: **2 GiB**;
- sequential read chunk: **32 MiB**;
- current physical RAM from `psutil.virtual_memory().available`;
- fixed RAM reserve: **3 GiB**;
- model-derived reserve: **50% of model file size**, clamped to **2–7 GiB**;
- minimum useful warm budget: **1 GiB**.

Lifecycle protections:

- a newer model request supersedes and terminates the active older helper;
- ReadAhead is stopped after model preparation and before sampling proceeds;
- RAM is monitored while the helper is active;
- if available RAM falls below the calculated reserve, the helper is terminated;
- if the RAM query is unavailable, ReadAhead stops/skips instead of guessing;
- helper terminate/kill waits are bounded;
- helper output is validated before a read is reported as completed;
- ComfyUI shutdown terminates the helper and worker;
- helper failures remain best-effort failures: normal ComfyUI model loading continues.

## Application Settings toggle

Open **Settings → AVS-ReadAhead** and use **Enable AVS SSD ReadAhead**.

- **ON** — the standalone ReadAhead controller is active on Windows.
- **OFF** — pending/active ReadAhead is stopped and future ReadAhead requests are no-ops.
- No custom-node folder removal is required.
- No ComfyUI restart is required to switch ON/OFF.
- The state is written to `config.json`, so it survives restart and also applies when ComfyUI is used headlessly/API-only.

The frontend setting is synchronized from the backend configuration on page load. This prevents an old browser-side default from accidentally re-enabling a patch that was deliberately disabled in `config.json`.

## Configuration

v1.0 intentionally reduces `config.json` to one production setting:

```json
{
  "enabled": true
}
```

The old advanced tuning keys are no longer used. Keeping the PR constants in code avoids a standalone configuration silently drifting away from the reviewed implementation.

## How the standalone hooks map to PR #15538

The upstream PR can modify ComfyUI core directly; a custom node cannot. v1.0 therefore uses two small permanent wrappers:

1. `comfy.utils.load_torch_file` requests ReadAhead immediately before the normal model-file load.
2. `comfy.sampler_helpers._prepare_sampling` stops the standalone helper after model preparation returns and before sampling proceeds.

The wrappers remain installed when the toggle is OFF, but their controller is inert. Live unpatch/repatch is intentionally avoided because changing function objects while workflows are running would create unnecessary race conditions.

If a future ComfyUI release ships the native ReadAhead implementation and its own native reader is explicitly active, the standalone wrapper detects it and avoids starting a second helper.

## What was removed from v0.9.x

The following standalone-only code/configuration was removed because it is not part of the current PR design:

- separate `foreground_min_start_ram_gib` and `foreground_stop_ram_gib` floors;
- `foreground_ram_check_mib`;
- `foreground_max_budget_gib`;
- configurable chunk/extensions/budget policy;
- ReadAhead environment-variable activation/tuning overrides;
- the old `KSAMPLER.sample` cancellation wrapper;
- cache-mode warnings;
- XPU async-offload warnings;
- old runtime/cache/backend inspection used only by those warnings.

No `--enable-model-readahead` argument is added by this standalone patch.

## Installation

### ZIP

1. Fully close ComfyUI.
2. Put the repository folder at:

```text
ComfyUI/custom_nodes/ComfyUI-AVS-SSD-ReadAhead/
```

3. Start ComfyUI.
4. Open **Settings → AVS-ReadAhead** to verify the toggle.

### Git

```bat
cd /d "PATH\TO\ComfyUI\custom_nodes"
git clone https://github.com/alvasafin-art/ComfyUI-AVS-SSD-ReadAhead.git
```

Restart ComfyUI after installing or updating repository files. After that, normal ON/OFF changes from Application Settings are immediate and do not require another restart.

No extra Python package needs to be installed on a current ComfyUI build; `psutil` is already part of ComfyUI's requirements.

## Safe update from v0.9.x

For a clean update, fully stop ComfyUI and replace the old repository files with v1.0.0. In particular, replace the old multi-option `config.json` with the new minimal one.

Make sure there is only one AVS/Sequential ReadAhead custom-node directory. A duplicate old folder can otherwise wrap the same ComfyUI functions twice; v1.0 detects the known old wrapper marker and refuses to stack on top of it.

## How to check that it is active

At startup on Windows, expect a line similar to:

```text
[AVS ReadAhead] Installed v1.0.0: enabled=True, active=True, load-hook=True, pre-sampling-stop=True, chunk=32 MiB
```

When a candidate model is requested, typical messages are:

```text
[AVS ReadAhead] queued model.safetensors: file ... GiB, budget ... GiB, RAM reserve ... GiB ...
[AVS ReadAhead] done model.safetensors: warmed ... GiB in ...s (... GiB/s)
```

Cancellation is also explicit, for example `newer model requested`, `sampling starting`, RAM reserve pressure, or shutdown.

## Historical benchmark from v0.9.0

The original patch was benchmarked on one system and showed roughly **39–47% lower measured model-loading time** in the tested workflows. These are historical v0.9.0 measurements, not a performance guarantee for v1.0.0.

| Component | Tested configuration |
|---|---|
| GPU | Intel Arc B580 12 GB |
| System RAM | 32 GB |
| Storage | Crucial MX500 SATA SSD |
| OS | Windows |
| Page file | 32 GB |
| ComfyUI | 0.31.1 |
| PyTorch | 2.13.0+xpu |
| Python | 3.13.12 |
| Patch | v0.9.0 |

| Model / workflow | Without ReadAhead | With v0.9.0 | Time reduction |
|---|---:|---:|---:|
| Flux 2 Klein 9B FP8 | 69.69 s | 36.60 s | **47.5%** |
| Krea 2 Turbo FP8 | 76.32 s | 45.05 s | **41.0%** |
| Z-Image Turbo FP8 AIO | 36.33 s | 22.16 s | **39.0%** |

Windows file-cache state, available RAM, storage speed, model format, and workflow can materially change the result. The patch accelerates model/file loading, not diffusion sampling itself.

## Reporting issues / useful test data

Please include:

- GPU model;
- system RAM;
- SSD/HDD type;
- Windows version;
- ComfyUI version;
- PyTorch version;
- model names / quantization;
- exact model-switching sequence;
- whether `AVS-ReadAhead` is ON or OFF;
- relevant `[AVS ReadAhead]` log lines.

## Credits

Created and tested as part of the AVS ComfyUI optimization project.

Development, debugging and documentation were assisted by OpenAI ChatGPT.

## License

GNU General Public License v3.0 (GPL-3.0).
