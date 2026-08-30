from __future__ import annotations

import atexit
import functools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

PATCH_VERSION = "1.0.0"
PATCH_NAME = "AVS SSD ReadAhead"
_PREFIX = "[AVS ReadAhead]"

_GIB = 1024 ** 3
_MIB = 1024 ** 2
_EXTENSIONS = (".safetensors", ".sft")
_MIN_FILE_BYTES = 2 * _GIB
_CHUNK_BYTES = 32 * _MIB
_BASE_RESERVE_BYTES = 3 * _GIB
_MODEL_RESERVE_RATIO = 0.50
_MODEL_RESERVE_MIN_BYTES = 2 * _GIB
_MODEL_RESERVE_MAX_BYTES = 7 * _GIB
_MIN_BUDGET_BYTES = 1 * _GIB
_STOP_TIMEOUT_S = 1.0
_PROCESS_WAIT_S = 0.35
_MONITOR_INTERVAL_S = 0.05

_CONFIG_PATH = Path(__file__).with_name("config.json")
_DEFAULT_ENABLED = True
_CONFIG_WRITE_LOCK = threading.Lock()
_SETTING_LOCK = threading.RLock()


def _log(level: int, message: str, *args: Any) -> None:
    logging.log(level, f"{_PREFIX} {message}", *args)


def _read_enabled() -> bool:
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return _DEFAULT_ENABLED
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        _log(logging.WARNING, "Could not read config.json: %s. Using default=%s.", exc, _DEFAULT_ENABLED)
        return _DEFAULT_ENABLED

    if not isinstance(data, dict):
        _log(logging.WARNING, "config.json must contain an object. Using default=%s.", _DEFAULT_ENABLED)
        return _DEFAULT_ENABLED
    value = data.get("enabled", _DEFAULT_ENABLED)
    if not isinstance(value, bool):
        _log(logging.WARNING, "config.json 'enabled' must be boolean. Using default=%s.", _DEFAULT_ENABLED)
        return _DEFAULT_ENABLED
    return value


def persist_enabled(enabled: bool) -> None:
    """Persist only the current production setting, atomically when possible."""
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a boolean")

    payload = json.dumps({"enabled": enabled}, indent=2) + "\n"
    tmp_path = _CONFIG_PATH.with_suffix(".json.tmp")
    with _CONFIG_WRITE_LOCK:
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, _CONFIG_PATH)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass


def _available_memory() -> int | None:
    try:
        return int(psutil.virtual_memory().available)
    except (OSError, RuntimeError, psutil.Error) as exc:
        _log(logging.WARNING, "RAM query failed; skipping/stopping read-ahead: %s", exc)
        return None


def _model_reserve(file_size: int) -> int:
    reserve = int(max(0, file_size) * _MODEL_RESERVE_RATIO)
    reserve = max(_MODEL_RESERVE_MIN_BYTES, reserve)
    return min(_MODEL_RESERVE_MAX_BYTES, reserve)


@dataclass(frozen=True)
class _Request:
    generation: int
    path: str
    size: int
    budget: int
    reserve: int


class _SequentialReadAhead:
    """Best-effort Windows file-cache warmer for large safetensors files.

    Model loading remains fully owned by ComfyUI. The sequential read runs in a
    short-lived helper process so it can be terminated independently without
    touching model residency, mmap objects, device transfers, or unload logic.
    """

    _CHILD_READER_CODE = r"""
import sys
path = sys.argv[1]
chunk = int(sys.argv[2])
limit = int(sys.argv[3])
buf = bytearray(chunk)
total = 0
with open(path, "rb", buffering=0) as handle:
    while total < limit:
        want = min(chunk, limit - total)
        count = handle.readinto(memoryview(buf)[:want])
        if not count:
            break
        total += count
print(total, flush=True)
"""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: _Request | None = None
        self._active: _Request | None = None
        self._active_process: subprocess.Popen[str] | None = None
        self._active_process_generation: int | None = None
        self._generation = 0
        self._cancel_generation: int | None = None
        self._cancel_reason = ""
        self._shutdown_requested = False
        self._worker_thread = threading.Thread(
            target=self._worker,
            name="AVS-Model-ReadAhead",
            daemon=True,
        )
        self._worker_thread.start()

    @staticmethod
    def _candidate(path_like: Any) -> tuple[str, int] | None:
        try:
            path = os.path.abspath(os.fspath(path_like))
        except (TypeError, ValueError):
            return None
        if not path.lower().endswith(_EXTENSIONS):
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        if size < _MIN_FILE_BYTES:
            return None
        return path, size

    @staticmethod
    def _plan(size: int) -> tuple[int, int, int] | None:
        available = _available_memory()
        if available is None:
            return None
        reserve = _BASE_RESERVE_BYTES + _model_reserve(size)
        budget = min(size, max(0, available - reserve))
        if budget < size:
            budget = (budget // _CHUNK_BYTES) * _CHUNK_BYTES
        if budget < _MIN_BUDGET_BYTES:
            return None
        return budget, reserve, available

    def request(self, path_like: Any) -> bool:
        candidate = self._candidate(path_like)
        if candidate is None:
            return False
        path, size = candidate
        plan = self._plan(size)
        if plan is None:
            return False
        budget, reserve, available = plan
        normalized = os.path.normcase(path)

        with self._cv:
            if self._shutdown_requested:
                return False
            if self._active is not None and os.path.normcase(self._active.path) == normalized:
                return False
            if self._pending is not None and os.path.normcase(self._pending.path) == normalized:
                return False

            if self._active is not None:
                self._cancel_generation = self._active.generation
                self._cancel_reason = "newer model requested"

            self._generation += 1
            self._pending = _Request(self._generation, path, size, budget, reserve)
            process = self._matching_active_process_locked(self._cancel_generation)
            self._cv.notify_all()

        if process is not None:
            self._terminate_process(process)

        _log(
            logging.INFO,
            "queued %s: file %.2f GiB, budget %.2f GiB, RAM reserve %.2f GiB (avail %.2f GiB)",
            os.path.basename(path),
            size / _GIB,
            budget / _GIB,
            reserve / _GIB,
            available / _GIB,
        )
        return True

    def _matching_active_process_locked(self, generation: int | None) -> subprocess.Popen[str] | None:
        if generation is None or self._active_process_generation != generation:
            return None
        return self._active_process

    def _cancel_active(self, reason: str, timeout_s: float, *, clear_pending: bool) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._cv:
            if clear_pending:
                self._pending = None
            active = self._active
            if active is None:
                self._cv.notify_all()
                return True
            generation = active.generation
            self._cancel_generation = generation
            self._cancel_reason = reason
            process = self._matching_active_process_locked(generation)
            self._cv.notify_all()

        # Never hold the condition while terminate()/kill() waits. The worker
        # needs the same condition to clear its active state.
        if process is not None:
            self._terminate_process(process)

        while True:
            with self._cv:
                if self._active is None or self._active.generation != generation:
                    return True
                remaining = deadline - time.monotonic()
                process = self._matching_active_process_locked(generation)
                if remaining <= 0:
                    break

            # Covers cancellation racing with helper Popen().
            if process is not None:
                self._terminate_process(process)

            with self._cv:
                # Re-check before waiting: the worker may have completed while
                # the condition was released for process termination.
                if self._active is None or self._active.generation != generation:
                    return True
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._cv.wait(timeout=min(_MONITOR_INTERVAL_S, remaining))

        if process is not None:
            self._terminate_process(process)

        final_deadline = time.monotonic() + _PROCESS_WAIT_S
        with self._cv:
            while self._active is not None and self._active.generation == generation:
                remaining = final_deadline - time.monotonic()
                if remaining <= 0:
                    _log(logging.WARNING, "helper did not stop after forced cancellation (%s)", reason)
                    return False
                self._cv.wait(timeout=remaining)
        return True

    def stop_for_sampling(self, timeout_s: float = _STOP_TIMEOUT_S) -> bool:
        return self._cancel_active("sampling starting", timeout_s, clear_pending=True)

    def shutdown(self, timeout_s: float = _STOP_TIMEOUT_S) -> bool:
        with self._cv:
            self._shutdown_requested = True
            self._pending = None
            worker = self._worker_thread
            self._cv.notify_all()

        stopped = self._cancel_active("shutdown", timeout_s, clear_pending=True)
        if worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, timeout_s))
        if worker.is_alive():
            _log(logging.WARNING, "worker thread did not exit during shutdown")
            return False
        return stopped

    def _cancel_state(self, request: _Request) -> tuple[bool, str]:
        with self._cv:
            if self._shutdown_requested:
                return True, "shutdown"
            if self._cancel_generation == request.generation:
                return True, self._cancel_reason or "cancelled"
            return False, ""

    def _worker(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._shutdown_requested:
                    self._cv.wait()
                if self._shutdown_requested and self._pending is None:
                    return
                request = self._pending
                self._pending = None
                if request is None:
                    continue
                self._active = request

            try:
                self._read_isolated(request)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                _log(logging.WARNING, "read failed for %s: %s", os.path.basename(request.path), exc)
            finally:
                with self._cv:
                    if self._active == request:
                        self._active = None
                    if self._active_process_generation == request.generation:
                        self._active_process = None
                        self._active_process_generation = None
                    if self._cancel_generation == request.generation:
                        self._cancel_generation = None
                        self._cancel_reason = ""
                    self._cv.notify_all()

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=_PROCESS_WAIT_S)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
            process.wait(timeout=_PROCESS_WAIT_S)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _read_isolated(self, request: _Request) -> None:
        cancelled, reason = self._cancel_state(request)
        if cancelled:
            _log(logging.INFO, "skip %s; %s", os.path.basename(request.path), reason)
            return

        started = time.perf_counter()
        command = [
            sys.executable,
            "-c",
            self._CHILD_READER_CODE,
            request.path,
            str(_CHUNK_BYTES),
            str(request.budget),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
        )

        with self._cv:
            if self._active == request:
                self._active_process = process
                self._active_process_generation = request.generation
            self._cv.notify_all()

        stop_reason = ""
        try:
            while process.poll() is None:
                cancelled, reason = self._cancel_state(request)
                if cancelled:
                    stop_reason = reason
                    self._terminate_process(process)
                    break

                available = _available_memory()
                if available is None:
                    stop_reason = "RAM query unavailable"
                    self._terminate_process(process)
                    break
                if available < request.reserve:
                    stop_reason = (
                        f"available RAM {available / _GIB:.2f} GiB < "
                        f"{request.reserve / _GIB:.2f} GiB reserve"
                    )
                    self._terminate_process(process)
                    break
                time.sleep(_MONITOR_INTERVAL_S)

            stdout, stderr = process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._terminate_process(process)
            stdout, stderr = process.communicate()
        finally:
            with self._cv:
                if self._active_process is process:
                    self._active_process = None
                    self._active_process_generation = None
                self._cv.notify_all()

        # Controller-side synchronous termination can happen between poll calls.
        if not stop_reason and process.returncode != 0:
            cancelled, reason = self._cancel_state(request)
            if cancelled:
                stop_reason = reason

        elapsed = max(time.perf_counter() - started, 1e-9)
        if stop_reason:
            _log(
                logging.INFO,
                "stop %s after %.2fs; %s",
                os.path.basename(request.path),
                elapsed,
                stop_reason,
            )
            return

        if process.returncode != 0:
            detail = (stderr or "").strip()[-500:]
            raise RuntimeError(f"helper reader exit={process.returncode}: {detail}")

        lines = (stdout or "").strip().splitlines()
        if not lines:
            raise RuntimeError("helper reader returned no byte count")
        try:
            total = int(lines[-1])
        except ValueError as exc:
            raise RuntimeError("helper reader returned an invalid byte count") from exc
        if total < 0 or total > request.budget:
            raise RuntimeError(f"helper reader returned invalid byte count: {total}")

        _log(
            logging.INFO,
            "done %s: warmed %.2f/%.2f GiB in %.2fs (%.2f GiB/s)",
            os.path.basename(request.path),
            total / _GIB,
            request.size / _GIB,
            elapsed,
            total / elapsed / _GIB,
        )


class _ReadAheadController:
    """Keeps permanent hooks inert/active without patch removal or restart."""

    def __init__(self, enabled: bool) -> None:
        self._lock = threading.RLock()
        self._enabled = bool(enabled)
        self._reader: _SequentialReadAhead | None = None
        self._last_error = ""
        if self._enabled:
            self._start_reader_locked()

    @property
    def supported(self) -> bool:
        return os.name == "nt"

    def _start_reader_locked(self) -> bool:
        if not self.supported:
            self._last_error = "Windows only"
            return False
        if self._reader is not None:
            self._last_error = ""
            return True
        try:
            self._reader = _SequentialReadAhead()
            self._last_error = ""
            return True
        except (OSError, RuntimeError) as exc:
            self._reader = None
            self._last_error = str(exc)
            _log(logging.WARNING, "could not initialize; normal model loading will be used: %s", exc)
            return False

    def set_enabled(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")

        reader_to_stop: _SequentialReadAhead | None = None
        with self._lock:
            self._enabled = enabled
            if enabled:
                active = self._start_reader_locked()
            else:
                reader_to_stop = self._reader
                self._reader = None
                self._last_error = ""
                active = False

        if reader_to_stop is not None:
            reader_to_stop.shutdown()
        return active

    def request(self, path: Any) -> bool:
        with self._lock:
            if not self._enabled:
                return False
            reader = self._reader
        if reader is None:
            return False
        return reader.request(path)

    def stop_for_sampling(self) -> bool:
        with self._lock:
            reader = self._reader
        if reader is None:
            return True
        return reader.stop_for_sampling()

    def shutdown(self) -> bool:
        with self._lock:
            reader = self._reader
            self._reader = None
        if reader is None:
            return True
        return reader.shutdown()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "active": self._reader is not None,
                "supported": self.supported,
                "platform": sys.platform,
                "version": PATCH_VERSION,
                "error": self._last_error or None,
            }


_controller: _ReadAheadController | None = None
_installed = False


def get_controller() -> _ReadAheadController:
    global _controller
    if _controller is None:
        _controller = _ReadAheadController(_read_enabled())
    return _controller


def set_enabled(enabled: bool, *, persist: bool = True) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a boolean")
    with _SETTING_LOCK:
        if persist:
            # Persist before changing runtime state so a write failure cannot make
            # the UI claim a durable change that disappears after restart.
            persist_enabled(enabled)
        controller = get_controller()
        controller.set_enabled(enabled)
        status = controller.status()
    _log(
        logging.INFO,
        "v%s %s from Application Settings (active=%s)",
        PATCH_VERSION,
        "enabled" if enabled else "disabled",
        status["active"],
    )
    return status


def status() -> dict[str, Any]:
    return get_controller().status()


def _native_readahead_active() -> bool:
    """Avoid double warming if a future ComfyUI native implementation is opt-in active."""
    try:
        import comfy.model_readahead as native_readahead
    except (ImportError, AttributeError):
        return False
    return getattr(native_readahead, "_reader", None) is not None


def _install_load_hook(controller: _ReadAheadController) -> bool:
    try:
        import comfy.utils
    except (ImportError, AttributeError) as exc:
        _log(logging.ERROR, "could not import comfy.utils: %s", exc)
        return False

    original = getattr(comfy.utils, "load_torch_file", None)
    if not callable(original):
        _log(logging.ERROR, "comfy.utils.load_torch_file is unavailable")
        return False
    if getattr(original, "_avs_readahead_patch", False):
        return True
    if getattr(original, "_sequential_readahead_patch", False) or getattr(original, "_xpu_sequential_readahead_patch", False):
        _log(logging.WARNING, "another/older Sequential ReadAhead wrapper is already active; restart after removing the duplicate folder")
        return False

    @functools.wraps(original)
    def wrapped_load_torch_file(ckpt: Any, *args: Any, **kwargs: Any):
        # If ComfyUI later ships the PR natively and its own reader is explicitly
        # enabled, let native code be the only warmer. Otherwise the standalone
        # patch remains responsible for ReadAhead without any CLI argument.
        if not _native_readahead_active():
            try:
                controller.request(ckpt)
            except Exception as exc:
                # Integration boundary: ReadAhead is best-effort and must never
                # turn a helper bug into a ComfyUI model-load failure.
                _log(logging.WARNING, "request failed; continuing normal load: %s", exc)
        return original(ckpt, *args, **kwargs)

    setattr(wrapped_load_torch_file, "_avs_readahead_patch", True)
    setattr(wrapped_load_torch_file, "_avs_readahead_original", original)
    comfy.utils.load_torch_file = wrapped_load_torch_file
    return True


def _install_sampling_hook(controller: _ReadAheadController) -> bool:
    """Stop helper after ComfyUI finishes model preparation and before sampling."""
    try:
        import comfy.sampler_helpers
    except (ImportError, AttributeError) as exc:
        _log(logging.ERROR, "could not import comfy.sampler_helpers: %s", exc)
        return False

    original = getattr(comfy.sampler_helpers, "_prepare_sampling", None)
    if not callable(original):
        _log(logging.ERROR, "comfy.sampler_helpers._prepare_sampling is unavailable")
        return False
    if getattr(original, "_avs_readahead_sampling_patch", False):
        return True

    @functools.wraps(original)
    def wrapped_prepare_sampling(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        try:
            # Always stop our own controller, even if a future native reader is
            # active. This guarantees no standalone helper survives into sampling.
            controller.stop_for_sampling()
        except Exception as exc:
            _log(logging.WARNING, "could not stop helper before sampling: %s", exc)
        return result

    setattr(wrapped_prepare_sampling, "_avs_readahead_sampling_patch", True)
    setattr(wrapped_prepare_sampling, "_avs_readahead_sampling_original", original)
    comfy.sampler_helpers._prepare_sampling = wrapped_prepare_sampling
    return True


def install_patch() -> bool:
    global _installed
    if _installed:
        return True

    controller = get_controller()
    # Install the safety stop first. If ComfyUI ever changes and that hook is no
    # longer available, do not install a load hook that could start ReadAhead
    # without a guaranteed pre-sampling cancellation point.
    sampling_hook = _install_sampling_hook(controller)
    load_hook = _install_load_hook(controller) if sampling_hook else False
    _installed = load_hook and sampling_hook

    current = controller.status()
    if not current["supported"]:
        _log(logging.INFO, "v%s hooks installed but ReadAhead is Windows-only; runtime remains inert", PATCH_VERSION)
    else:
        _log(
            logging.INFO,
            "Installed v%s: enabled=%s, active=%s, load-hook=%s, pre-sampling-stop=%s, chunk=%d MiB",
            PATCH_VERSION,
            current["enabled"],
            current["active"],
            load_hook,
            sampling_hook,
            _CHUNK_BYTES // _MIB,
        )

    return _installed


def shutdown() -> bool:
    if _controller is None:
        return True
    return _controller.shutdown()


atexit.register(shutdown)
