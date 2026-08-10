# ComfyUI-AVS-SSD-ReadAhead

**Speed up ComfyUI model loading and model switching on slow SSDs on Linux (CachyOS/Arch/Ubuntu/etc.) and Windows.**

This patch speeds up loading of large `.safetensors` and `.sft` model files (Flux, SDXL, Wan2.1, HunyuanVideo, Z-Image, etc.) by warming their file blocks sequentially into the OS Page Cache (RAM) while ComfyUI is starting to open the model.

---

## Features

- **Cross-Platform**: Supports **Linux** (CachyOS, Arch, Ubuntu, Fedora, etc.) and **Windows**.
- **Linux Native Kernel Prefetching**: Uses Linux `os.posix_fadvise(..., POSIX_FADV_WILLNEED)` and background sequential chunk reading to populate the Linux Page Cache.
- **Linux RAM Budgeting**: Reads `/proc/meminfo` (`MemAvailable`) in real-time on Linux to dynamically calculate available physical memory headroom.
- **Windows File Cache Support**: Uses `GlobalMemoryStatusEx` and sequential chunk reading on Windows.
- **Adaptive Memory Guard**: Ensures read-ahead stops or skips if system RAM is low, preventing OOM crashes.
- **Sampler Cancellation Hook**: Cancels any background prefetching automatically before sampling begins.

---

## How It Works

1. ComfyUI begins loading a large `.safetensors` model file.
2. ReadAhead intercepts `comfy.utils.load_torch_file`.
3. A background process prefetches the model file sequentially into system RAM (Page Cache).
4. As ComfyUI reads the model weights into GPU/CPU memory, data is retrieved directly from RAM instead of waiting on disk I/O.
5. Model loading speed increases significantly on SATA SSDs, external drives, or older SSDs.

---

## Installation

Clone this repository into your ComfyUI `custom_nodes` directory:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/l33tm4st3r/ComfyUI-AVS-SSD-ReadAhead.git
```

No extra Python package dependencies are required!

---

## Configuration (`config.json`)

The default configuration works out of the box for both Linux and Windows:

```json
{
    "enabled": true,
    "require_windows": false,
    "min_file_gib": 2.0,
    "chunk_mib": 32,
    "extensions": [".safetensors", ".sft"],
    "log_requests": true,
    "foreground_adaptive_budget": true,
    "foreground_min_start_ram_gib": 3.5,
    "foreground_stop_ram_gib": 2.5,
    "foreground_ram_check_mib": 256,
    "foreground_base_reserve_gib": 3.0,
    "foreground_model_reserve_ratio": 0.50,
    "foreground_model_reserve_min_gib": 2.0,
    "foreground_model_reserve_max_gib": 7.0,
    "foreground_min_budget_gib": 1.0,
    "foreground_max_budget_gib": 0.0,
    "cancel_reader_at_sampler_start": true,
    "sampler_cancel_wait_ms": 350,
    "warn_nonclassic_cache": true,
    "warn_xpu_async_offload": true
}
```

### Environment Variables

You can also override settings via environment variables:
- `COMFY_READAHEAD=0` (or `1`) to disable/enable.
- `COMFY_READAHEAD_MIN_GIB=3.0` to set the minimum file size in GiB.
- `COMFY_READAHEAD_CHUNK_MIB=64` to change chunk size.

---

## Linux / CachyOS Block Device Optimization (Optional)

On Linux systems (including CachyOS), you can further boost sequential read performance by increasing the kernel readahead window on your storage drive from default 128 KB to 8 MB:

```bash
# Temporarily set 8MB read-ahead window for /dev/sdX
sudo blockdev --setra 16384 /dev/sdX
```

---

## Recommended Launch Options

```bash
python main.py --cache-classic
```

Using `--cache-classic` ensures maximum stability during multi-model workflow switching.

---

## License

GPL-3.0 License
