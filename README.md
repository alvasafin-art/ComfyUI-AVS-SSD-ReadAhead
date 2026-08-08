# ComfyUI-AVS-SSD-ReadAhead

**Speed up ComfyUI model loading and model switching on slower SSDs on Windows.**

On my test system, the measured model-loading time was reduced by roughly **39–47%** in tested Flux, Krea and Z-Image workflows.

This patch is mainly useful when storage I/O is a bottleneck — for example, SATA SSDs, older SSDs, external SSDs, or other relatively slow storage. Fast NVMe drives may see a smaller benefit.

> **Status:** Windows-only, GPU-vendor neutral in the ReadAhead code path.  
> **Tested hardware:** Intel Arc B580 12 GB.  
> **NVIDIA / AMD:** not tested yet — please treat those backends as experimental and report results.

## What it does

ComfyUI-AVS-SSD-ReadAhead speeds up loading of large `.safetensors` / `.sft` model files by reading them sequentially into the Windows file cache while ComfyUI is loading the same model.

In simple terms:

1. ComfyUI starts loading a large model.
2. The patch starts a small helper process.
3. The helper reads the model file sequentially from disk.
4. Windows keeps the recently read data in file cache / RAM.
5. ComfyUI can then obtain more of the model data from memory instead of repeatedly waiting on a slow SSD.
6. The helper stops before sampling begins.

The patch uses an **adaptive RAM budget**. If there is not enough free physical memory, it warms only part of the file or skips ReadAhead completely and lets stock ComfyUI load the model normally.

It does **not** modify model weights, image quality, sampling, GPU kernels, model unload logic, VRAM residency, Triton, CUDA/XPU transfers, or mmap objects.

## Who is this for?

This patch is mainly intended for Windows systems where model loading is limited by storage speed.

It may be useful for:

- SATA SSDs;
- older or slower SSDs;
- external SSDs;
- systems using large ComfyUI models where model loading and model switching cause noticeable disk I/O delays.

Fast NVMe SSDs may see a smaller improvement or no meaningful improvement.

The patch does **not** directly make sampling or inference faster. Its main goal is to reduce **model loading and model switching time**.

## Benchmark

### Test system

| Component | Tested configuration |
|---|---|
| GPU | Intel Arc B580 12 GB |
| System RAM | 32 GB |
| Storage | Crucial MX500 SATA SSD |
| OS | Windows |
| Page file | 32 GB |
| ComfyUI | 0.31.1 |
| PyTorch | 2.13.0+xpu |
| comfy-kitchen | 0.2.28 |
| comfy-aimdo | 0.4.13 |
| Python | 3.13.12 |
| Patch | v0.9.0 |

The comparison below uses the same machine and the same stable launch configuration. The baseline uses `--cache-classic` with ReadAhead disabled. The patched result uses v0.9.0.

### Model-loading results

| Model / workflow | Without ReadAhead | With v0.9.0 | Time reduction |
|---|---:|---:|---:|
| Flux 2 Klein 9B FP8 | 69.69 s | 36.60 s | **47.5%** |
| Krea 2 Turbo FP8 | 76.32 s | 45.05 s | **41.0%** |
| Z-Image Turbo FP8 AIO | 36.33 s | 22.16 s | **39.0%** |

**Important benchmark note:** Flux/Krea use the benchmark's aggregated model-load timing. Z-Image AIO reports its loading differently, so the table uses its loader-node time. These numbers are practical measurements from one system, not guaranteed performance for every PC. Windows file-cache state, RAM, storage speed, model format and workflow can all affect results.

## Tested launch arguments

Validated Intel Arc B580 configuration:

```text
--cache-classic --disable-async-offload --enable-triton-backend --oneapi-device-selector level_zero:gpu
```

### Which arguments are actually required?

| Argument | For the patch? | Notes |
|---|---|---|
| `--cache-classic` | **Strongly recommended for the tested ComfyUI 0.31.1 setup** | In heavy multi-model switching, the default RAM-pressure cache crashed on the test system even without ReadAhead. The same sequence was stable with `--cache-classic`. |
| `--disable-async-offload` | **Required for the validated Intel Arc B580 / ComfyUI 0.31.1 configuration** | On the tested XPU stack, ReadAhead + 2 async-offload streams repeatedly caused native crashes. One stream was not faster. |
| `--enable-triton-backend` | **No / optional** | Not used by ReadAhead. It was enabled in the benchmark because it improved compute performance on the test system. |
| `--oneapi-device-selector level_zero:gpu` | **No / Intel-only** | Selects the Intel Level Zero GPU. Not needed for NVIDIA or AMD. |
| `--enable-manager` | **No / optional** | Only needed if you want ComfyUI-Manager enabled. It is unrelated to ReadAhead. |

### NVIDIA / AMD note

The ReadAhead implementation itself does not contain Intel-specific GPU code, so it is designed to be GPU-vendor neutral on Windows.

However, **only Intel Arc B580 has been tested so far**. NVIDIA and AMD can use different memory/offload defaults, so do not assume the Intel launch arguments are optimal for those GPUs. Please report your GPU, RAM, storage type, ComfyUI/PyTorch versions, launch arguments and results if you test another backend.

## Installation

No extra Python packages are required.

### Option 1 — Download ZIP

1. Open this GitHub repository.
2. Click **Code**.
3. Click **Download ZIP**.
4. Fully close ComfyUI.
5. Extract the repository folder into:

```text
ComfyUI/custom_nodes/
```

You should end up with something like:

```text
ComfyUI/custom_nodes/ComfyUI-AVS-SSD-ReadAhead/
```

6. Start ComfyUI.

### Option 2 — Install with Git from CMD

Open **Command Prompt** and go to your ComfyUI `custom_nodes` folder:

```bat
cd /d "PATH\TO\ComfyUI\custom_nodes"
```

Then clone the repository:

```bat
git clone https://github.com/alvasafin-art/ComfyUI-AVS-SSD-ReadAhead.git
```

Restart ComfyUI.

### Update with Git

```bat
cd /d "PATH\TO\ComfyUI\custom_nodes\ComfyUI-AVS-SSD-ReadAhead"
git pull
```

Then restart ComfyUI.

### Uninstall

Fully close ComfyUI and delete:

```text
ComfyUI/custom_nodes/ComfyUI-AVS-SSD-ReadAhead
```

## How to check that it is active

At startup you should see a line similar to:

```text
[Sequential ReadAhead] Installed v0.9.0 (...): isolated cached read-ahead=True, adaptive-budget=True, sampler-stop=True, ... unload/model-memory hooks=none
```

When a large model is loaded, you should see messages similar to:

```text
[Sequential ReadAhead] queued model.safetensors: file 8.79 GiB, warm budget 8.79 GiB ...
[Sequential ReadAhead] start isolated model.safetensors ...
[Sequential ReadAhead] done isolated model.safetensors: warmed 8.79/8.79 GiB ...
```

If RAM is limited, a smaller prefix may be warmed. This is normal.

## How the adaptive RAM protection works

For large files, the patch calculates approximately:

```text
warm_budget = available RAM - safety reserve
```

Default safety reserve:

```text
3 GiB + 50% of the current model-file size
```

The model-derived part is limited to 2–7 GiB.

Examples with the default settings:

- Flux 8.79 GiB file → about 7.4 GiB RAM reserve
- Krea 12.24 GiB file → about 9.1 GiB RAM reserve

If the useful ReadAhead budget would be below 1 GiB, the patch skips ReadAhead for that file and stock ComfyUI continues normally.

## Default behavior / safety

The production v0.9.0 path intentionally stays small.

It uses:

- ordinary cached sequential file reading;
- a short-lived helper process;
- adaptive physical-RAM budgeting;
- helper cancellation before sampling.

It does **not** use:

- model-unload hooks;
- partial-unload hooks;
- `EmptyWorkingSet`;
- mmap bouncing;
- custom GPU synchronization;
- sleeps/delays around model operations;
- GPU-vendor-specific ReadAhead code.

## Configuration

Advanced settings are in `config.json`.

For the tested 32 GB RAM system, the default values are recommended.

Useful options include:

- `min_file_gib` — minimum model file size to use ReadAhead;
- `chunk_mib` — sequential read chunk size;
- `foreground_base_reserve_gib` — fixed physical-RAM reserve;
- `foreground_model_reserve_ratio` — model-size-based reserve;
- `foreground_min_budget_gib` — skip ReadAhead if the useful warm prefix is too small;
- `foreground_max_budget_gib` — optional hard cap; `0` means no additional cap;
- `cancel_reader_at_sampler_start` — stop helper before sampling.

## Known limitations

- Windows only.
- Tested only on Intel Arc B580 so far.
- Best suited to systems where storage I/O is a bottleneck; fast NVMe systems may see a smaller benefit or no meaningful improvement.
- The validated ComfyUI 0.31.1 setup uses `--cache-classic`.
- The validated Intel XPU setup uses `--disable-async-offload`.
- Model files smaller than 2 GiB are ignored by default.
- This patch accelerates file/model loading, not the diffusion sampler itself.

## Reporting issues / useful test data

If you report a problem or benchmark, please include:

- GPU model;
- system RAM;
- SSD/HDD type;
- Windows version;
- ComfyUI version;
- PyTorch version;
- comfy-kitchen version;
- launch arguments;
- model names / quantization;
- exact model-switching sequence;
- relevant `[Sequential ReadAhead]` log lines.

For performance comparisons, test the same workflow and launch arguments with and without the patch.

## Credits

Created and tested as part of the AVS ComfyUI optimization project.

Development, debugging and documentation were assisted by OpenAI ChatGPT.

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0).

You are free to use, modify, redistribute, and commercially use this software under the terms of the GPL-3.0 license. Distributed modified versions must remain open source under the GPL-3.0 license.
