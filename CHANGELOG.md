# Changelog

## 1.0.0 - 2026-08-27

Rebase of the standalone custom-node patch onto the current behavior and protections of ComfyUI PR #15538.

### Changed

- Reworked the ReadAhead core around the PR's current adaptive physical-RAM reserve/budget model.
- Uses `psutil.virtual_memory().available`, matching the PR.
- Uses the PR defaults: 2 GiB candidate threshold, 32 MiB chunks, 3 GiB base reserve, 50% model reserve clamped to 2–7 GiB, 1 GiB minimum useful budget.
- New model requests now supersede and terminate an active older ReadAhead request.
- Added tracked helper-process generations to make cancellation races safer.
- Added bounded terminate/kill handling and a final worker-state check.
- Added strict helper byte-count validation.
- Added stop-on-RAM-pressure and stop-on-RAM-query-failure behavior.
- Added clean worker/helper shutdown.
- Replaced the old `KSAMPLER.sample` cancellation hook with a `_prepare_sampling` wrapper so the helper is stopped after model preparation and before sampling proceeds.
- `config.json` is now intentionally minimal and contains only `enabled`.

### Added

- Application Settings category **AVS-ReadAhead**.
- **Enable AVS SSD ReadAhead** toggle.
- Runtime ON/OFF without deleting the folder or restarting ComfyUI.
- Backend setting endpoints with durable `config.json` persistence.
- Backend-to-frontend synchronization so a stale UI default cannot overwrite a deliberately disabled config on page load.
- Detection of an explicitly active future native ComfyUI ReadAhead reader to avoid double warming.
- Unit/lifecycle tests for candidate filtering, RAM planning, normal completion, sampling cancellation, newer-request cancellation, RAM pressure, shutdown, config migration, and disabled mode.

### Removed

- ReadAhead CLI activation from the standalone patch. In particular, no `--enable-model-readahead` line is included.
- Environment-variable activation/tuning overrides.
- `foreground_min_start_ram_gib` and `foreground_stop_ram_gib`.
- `foreground_ram_check_mib`.
- `foreground_max_budget_gib`.
- configurable extensions/chunk/budget policy.
- old `KSAMPLER.sample` hook.
- cache-mode compatibility warnings.
- XPU async-offload compatibility warnings.
- runtime backend/cache inspection used only for those warnings.

## 0.9.0

Production cleanup based on the validated v0.8.0 I/O algorithm.

- Removed inactive speculative switch-back code.
- Removed model-unload / partial-unload hook code.
- Removed `EmptyWorkingSet`, mmap-bounce and ComfyUI RAM-release experimental paths.
- Removed legacy in-process reader fallback; Windows helper-process reading became the single production path.
- Kept adaptive bounded foreground warmup.
- Kept ordinary cached I/O; no `FILE_FLAG_SEQUENTIAL_SCAN`.
- Kept sampler-start helper cancellation and discarded pending helper requests before sampling.
- Reduced `config.json` to active production options for that release.
- Added startup warnings for non-classic cache mode and XPU async-offload.
- No automatic changes to ComfyUI cache, async-offload, model residency, or GPU backend settings.

## 0.8.0

- Added adaptive RAM-bounded foreground warmup.
- Kept helper-process isolation and sampler-start cancellation.
- No active model-unload hook, working-set trim, mmap bounce, or custom device-transfer synchronization.
