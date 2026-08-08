# Changelog

## 0.9.0

Production cleanup based on the validated v0.8.0 I/O algorithm.

- Removed all inactive speculative switch-back code.
- Removed all model-unload / partial-unload hook code.
- Removed `EmptyWorkingSet`, mmap-bounce and ComfyUI RAM-release experimental paths.
- Removed legacy in-process reader fallback; Windows helper-process reading is now the single production path.
- Kept adaptive bounded foreground warmup unchanged in principle.
- Kept ordinary cached I/O; no `FILE_FLAG_SEQUENTIAL_SCAN`.
- Kept sampler-start helper cancellation, and tightened it to discard a still-pending helper request before sampling.
- `config.json` now exposes only active production options; stale experimental keys are ignored.
- Added startup detection/warnings for non-classic cache mode and XPU async-offload.
- Added startup summary with cache mode and detected async stream count.
- No automatic changes to ComfyUI cache, async-offload, model residency, or GPU backend settings.

### Validation finding carried into v0.9.0

On ComfyUI 0.31.1, a heavy multi-model sequence could crash with the default RAM-pressure cache even without ReadAhead. The same sequence completed with `--cache-classic`; adding ReadAhead v0.8.0 remained stable and substantially reduced cold model-load times. Therefore `--cache-classic` is the recommended baseline for the validated stack, but the patch only warns and never forces it.

## 0.8.0

- Added adaptive RAM-bounded foreground warmup.
- Kept helper-process isolation and sampler-start cancellation.
- No active model-unload hook, working-set trim, mmap bounce, or custom device-transfer synchronization.
