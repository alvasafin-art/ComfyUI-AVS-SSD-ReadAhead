from __future__ import annotations

import ctypes
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

PATCH_VERSION = "0.9.0"
PATCH_NAME = "Windows Adaptive Bounded Sequential ReadAhead"
_PREFIX = "[Sequential ReadAhead]"
_GIB = 1024 ** 3
_MIB = 1024 ** 2

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "require_windows": False,
    "min_file_gib": 2.0,
    "chunk_mib": 32,
    "extensions": [".safetensors", ".sft"],
    "log_requests": True,
    "foreground_adaptive_budget": True,
    "foreground_min_start_ram_gib": 3.5,
    "foreground_stop_ram_gib": 2.5,
    "foreground_ram_check_mib": 256,
    "foreground_base_reserve_gib": 3.0,
    "foreground_model_reserve_ratio": 0.50,
    "foreground_model_reserve_min_gib": 2.0,
    "foreground_model_reserve_max_gib": 7.0,
    "foreground_min_budget_gib": 1.0,
    "foreground_max_budget_gib": 0.0,
    "cancel_reader_at_sampler_start": True,
    "sampler_cancel_wait_ms": 350,
    "warn_nonclassic_cache": True,
    "warn_xpu_async_offload": True,
}


def _log(level: int, message: str, *args: Any) -> None:
    logging.log(level, f"{_PREFIX} {message}", *args)


def _read_config() -> dict[str, Any]:
    config = dict(_DEFAULT_CONFIG)
    path = Path(__file__).with_name("config.json")
    try:
        with path.open("r", encoding="utf-8") as handle:
            user_config = json.load(handle)
        if isinstance(user_config, dict):
            # Only recognized production keys are accepted. Old v0.5-v0.8.1
            # experimental options are intentionally ignored.
            for key, value in user_config.items():
                if key in _DEFAULT_CONFIG:
                    config[key] = value
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log(logging.WARNING, "Could not read config.json: %s. Using defaults.", exc)

    env_enabled = os.getenv("COMFY_READAHEAD")
    if env_enabled is None:
        env_enabled = os.getenv("COMFY_XPU_READAHEAD")
    if env_enabled is not None:
        config["enabled"] = env_enabled.strip().lower() not in {"0", "false", "no", "off"}

    env_min_gib = os.getenv("COMFY_READAHEAD_MIN_GIB") or os.getenv("COMFY_XPU_READAHEAD_MIN_GIB")
    if env_min_gib:
        try:
            config["min_file_gib"] = float(env_min_gib)
        except ValueError:
            _log(logging.WARNING, "Ignoring invalid COMFY_READAHEAD_MIN_GIB=%r", env_min_gib)

    env_chunk_mib = os.getenv("COMFY_READAHEAD_CHUNK_MIB") or os.getenv("COMFY_XPU_READAHEAD_CHUNK_MIB")
    if env_chunk_mib:
        try:
            config["chunk_mib"] = int(env_chunk_mib)
        except ValueError:
            _log(logging.WARNING, "Ignoring invalid COMFY_READAHEAD_CHUNK_MIB=%r", env_chunk_mib)

    return config


@dataclass(frozen=True)
class _MemoryStatus:
    available_phys: int
    available_commit: int
    memory_load_percent: int


@dataclass(frozen=True)
class _Request:
    generation: int
    path: str
    size: int
    budget: int
    stop_floor: int


if os.name == "nt":
    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]



def _linux_memory_status() -> _MemoryStatus | None:
    try:
        meminfo = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    meminfo[key] = int(val) * 1024  # kB to bytes
        total = meminfo.get("MemTotal", 0)
        avail = meminfo.get("MemAvailable", 0)
        swap_free = meminfo.get("SwapFree", 0)
        if total <= 0:
            return None
        load_pct = int(100 * (total - avail) / total)
        return _MemoryStatus(
            available_phys=avail,
            available_commit=avail + swap_free,
            memory_load_percent=load_pct,
        )
    except Exception:
        return None

def _memory_status() -> _MemoryStatus | None:
    if os.name == "nt":
        return _memory_status()
    elif os.name == "posix":
        return _linux_memory_status()
    return None

def _memory_status() -> _MemoryStatus | None:
    if os.name != "nt":
        return None
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return _MemoryStatus(
            available_phys=int(status.ullAvailPhys),
            available_commit=int(status.ullAvailPageFile),
            memory_load_percent=int(status.dwMemoryLoad),
        )
    except Exception:
        return None


class SequentialReadAhead:
    """Warm large model-file prefixes into the Windows file cache.

    The reader is intentionally I/O-only. It never touches model residency,
    device transfers, ComfyUI unload logic, mmap objects, or process working sets.
    """

    _CHILD_READER_CODE = r"""
import os
import sys

path = sys.argv[1]
chunk = int(sys.argv[2])
limit = int(sys.argv[3])

if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_WILLNEED"):
    try:
        fd = os.open(path, os.O_RDONLY)
        os.posix_fadvise(fd, 0, limit, os.POSIX_FADV_WILLNEED)
        os.close(fd)
    except Exception:
        pass

buf = bytearray(chunk)
total = 0
with open(path, "rb", buffering=0) as handle:
    while total < limit:
        want = min(chunk, limit - total)
        n = handle.readinto(memoryview(buf)[:want])
        if not n:
            break
        total += n
print(total, flush=True)
"""

    def __init__(
        self,
        *,
        min_file_gib: float,
        chunk_mib: int,
        extensions: list[str],
        log_requests: bool,
        min_start_ram_gib: float,
        stop_ram_gib: float,
        ram_check_mib: int,
        adaptive_budget: bool,
        base_reserve_gib: float,
        model_reserve_ratio: float,
        model_reserve_min_gib: float,
        model_reserve_max_gib: float,
        min_budget_gib: float,
        max_budget_gib: float,
    ) -> None:
        if min_file_gib < 0:
            raise ValueError("min_file_gib must be >= 0")
        if not 1 <= chunk_mib <= 512:
            raise ValueError("chunk_mib must be between 1 and 512")

        self._min_bytes = int(min_file_gib * _GIB)
        self._chunk_bytes = int(chunk_mib * _MIB)
        self._extensions = tuple(str(ext).lower() for ext in extensions)
        self._log_requests = bool(log_requests)
        self._min_start_ram = max(0, int(float(min_start_ram_gib) * _GIB))
        self._stop_ram = max(0, int(float(stop_ram_gib) * _GIB))
        self._ram_check_bytes = max(self._chunk_bytes, int(max(1, int(ram_check_mib)) * _MIB))
        self._adaptive_budget = bool(adaptive_budget)
        self._base_reserve = max(0, int(float(base_reserve_gib) * _GIB))
        self._model_reserve_ratio = max(0.0, float(model_reserve_ratio))
        self._model_reserve_min = max(0, int(float(model_reserve_min_gib) * _GIB))
        self._model_reserve_max = max(0, int(float(model_reserve_max_gib) * _GIB))
        if self._model_reserve_max and self._model_reserve_max < self._model_reserve_min:
            raise ValueError("model_reserve_max_gib must be >= model_reserve_min_gib or 0")
        self._min_budget = max(self._chunk_bytes, int(float(min_budget_gib) * _GIB))
        self._max_budget = max(0, int(float(max_budget_gib) * _GIB))

        self._cv = threading.Condition()
        self._pending: _Request | None = None
        self._active: _Request | None = None
        self._active_process: subprocess.Popen[str] | None = None
        self._generation = 0
        self._cancel_generation: int | None = None
        self._cancel_reason = ""
        self._thread = threading.Thread(
            target=self._worker,
            name="Comfy-Sequential-ReadAhead",
            daemon=True,
        )
        self._thread.start()

    @property
    def chunk_mib(self) -> int:
        return self._chunk_bytes // _MIB

    def candidate(self, path_like: Any) -> tuple[str, int] | None:
        try:
            path = os.path.abspath(os.fspath(path_like))
        except (TypeError, ValueError):
            return None
        if not path.lower().endswith(self._extensions):
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        if size < self._min_bytes:
            return None
        return path, size

    def _model_reserve_bytes(self, file_size: int) -> int:
        reserve = int(max(0, file_size) * self._model_reserve_ratio)
        reserve = max(self._model_reserve_min, reserve)
        if self._model_reserve_max:
            reserve = min(self._model_reserve_max, reserve)
        return reserve

    def _plan_request(self, path: str, size: int) -> tuple[int, int, _MemoryStatus | None] | None:
        status = _memory_status()
        if status is not None and self._min_start_ram and status.available_phys < self._min_start_ram:
            if self._log_requests:
                _log(
                    logging.INFO,
                    "skip read-ahead %s: available RAM %.2f GiB < %.2f GiB start floor",
                    os.path.basename(path),
                    status.available_phys / _GIB,
                    self._min_start_ram / _GIB,
                )
            return None

        if not self._adaptive_budget or status is None:
            budget = size
            if self._max_budget:
                budget = min(budget, self._max_budget)
            return max(0, budget), self._stop_ram, status

        model_reserve = self._model_reserve_bytes(size)
        reserve = max(self._stop_ram, self._base_reserve + model_reserve)
        budget = min(size, max(0, status.available_phys - reserve))
        if self._max_budget:
            budget = min(budget, self._max_budget)
        if budget < size:
            budget = (budget // self._chunk_bytes) * self._chunk_bytes
        if budget < self._min_budget:
            if self._log_requests:
                _log(
                    logging.INFO,
                    "skip bounded read-ahead %s: avail %.2f GiB, reserve %.2f GiB, budget %.2f GiB < %.2f GiB minimum",
                    os.path.basename(path),
                    status.available_phys / _GIB,
                    reserve / _GIB,
                    budget / _GIB,
                    self._min_budget / _GIB,
                )
            return None
        return budget, reserve, status

    def request(self, path_like: Any) -> bool:
        candidate = self.candidate(path_like)
        if candidate is None:
            return False
        path, size = candidate
        plan = self._plan_request(path, size)
        if plan is None:
            return False
        budget, stop_floor, status = plan

        norm = os.path.normcase(path)
        with self._cv:
            if self._active is not None and os.path.normcase(self._active.path) == norm:
                return False
            if self._pending is not None and os.path.normcase(self._pending.path) == norm:
                return False
            self._generation += 1
            self._pending = _Request(self._generation, path, size, budget, stop_floor)
            self._cv.notify_all()

        if self._log_requests:
            if status is not None and self._adaptive_budget:
                _log(
                    logging.INFO,
                    "queued %s: file %.2f GiB, warm budget %.2f GiB, RAM reserve %.2f GiB (avail %.2f GiB)",
                    os.path.basename(path),
                    size / _GIB,
                    budget / _GIB,
                    stop_floor / _GIB,
                    status.available_phys / _GIB,
                )
            else:
                _log(
                    logging.INFO,
                    "queued %s: file %.2f GiB, warm budget %.2f GiB",
                    os.path.basename(path),
                    size / _GIB,
                    budget / _GIB,
                )
        return True

    def cancel_for_sampler(self, *, timeout_s: float) -> bool:
        """Prevent any current or queued helper read from surviving into sampling."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._cv:
            # A queued request has not warmed anything yet; discard it outright.
            self._pending = None
            active = self._active
            if active is None:
                self._cv.notify_all()
                return True
            self._cancel_generation = active.generation
            self._cancel_reason = "sampler starting"
            self._cv.notify_all()
            while self._active is not None and self._active.generation == active.generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(timeout=remaining)
            return True

    def _cancel_state(self, request: _Request) -> tuple[bool, str]:
        with self._cv:
            if self._cancel_generation == request.generation:
                return True, self._cancel_reason or "cancelled"
            if self._generation != request.generation and self._pending is not None:
                return True, "newer file requested"
            return False, ""

    def _worker(self) -> None:
        while True:
            with self._cv:
                while self._pending is None:
                    self._cv.wait()
                request = self._pending
                self._pending = None
                self._active = request
                if self._cancel_generation == request.generation:
                    self._cancel_generation = None
                    self._cancel_reason = ""

            try:
                self._read_isolated(request)
            except Exception as exc:
                _log(logging.WARNING, "read-ahead failed for %s: %s", os.path.basename(request.path), exc)
            finally:
                with self._cv:
                    if self._active == request:
                        self._active = None
                    if self._cancel_generation == request.generation:
                        self._cancel_generation = None
                        self._cancel_reason = ""
                    self._cv.notify_all()

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
            return
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=0.5)
        except Exception:
            pass

    def _read_isolated(self, request: _Request) -> None:
        started = time.perf_counter()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        cmd = [
            sys.executable,
            "-c",
            self._CHILD_READER_CODE,
            request.path,
            str(self._chunk_bytes),
            str(request.budget),
        ]
        _log(
            logging.INFO,
            "start isolated %s (budget %.2f/%.2f GiB)",
            os.path.basename(request.path),
            request.budget / _GIB,
            request.size / _GIB,
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
        )
        with self._cv:
            if self._active == request:
                self._active_process = proc

        stop_reason = ""
        try:
            while proc.poll() is None:
                cancelled, reason = self._cancel_state(request)
                if cancelled:
                    stop_reason = reason
                    self._terminate_process(proc)
                    break

                if request.stop_floor:
                    status = _memory_status()
                    if status is not None and status.available_phys < request.stop_floor:
                        stop_reason = (
                            f"available RAM {status.available_phys / _GIB:.2f} GiB "
                            f"< {request.stop_floor / _GIB:.2f} GiB adaptive reserve"
                        )
                        self._terminate_process(proc)
                        break
                time.sleep(0.05)

            stdout, stderr = proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._terminate_process(proc)
            stdout, stderr = proc.communicate()
        finally:
            with self._cv:
                if self._active_process is proc:
                    self._active_process = None
                self._cv.notify_all()

        elapsed = max(time.perf_counter() - started, 1e-9)
        if stop_reason:
            _log(
                logging.INFO,
                "stop isolated %s after %.2fs; %s",
                os.path.basename(request.path),
                elapsed,
                stop_reason,
            )
            return

        if proc.returncode != 0:
            detail = (stderr or "").strip()[-500:]
            raise RuntimeError(f"helper reader exit={proc.returncode}: {detail}")

        try:
            total = int((stdout or "0").strip().splitlines()[-1])
        except Exception:
            total = request.budget
        _log(
            logging.INFO,
            "done isolated %s: warmed %.2f/%.2f GiB in %.2fs (%.2f GiB/s)",
            os.path.basename(request.path),
            total / _GIB,
            request.size / _GIB,
            elapsed,
            total / elapsed / _GIB,
        )


def _install_sampler_cancel_hook(reader: SequentialReadAhead, config: dict[str, Any]) -> bool:
    if not bool(config.get("cancel_reader_at_sampler_start", True)):
        return False
    try:
        import comfy.samplers

        ksampler = getattr(comfy.samplers, "KSAMPLER", None)
        original = getattr(ksampler, "sample", None)
        if not callable(original):
            _log(logging.WARNING, "Sampler reader-stop hook unavailable: KSAMPLER.sample missing")
            return False
    except Exception as exc:
        _log(logging.WARNING, "Sampler reader-stop hook unavailable: %s", exc)
        return False

    if getattr(original, "_sequential_readahead_foreground_stop", False):
        return True

    wait_s = max(0, int(config.get("sampler_cancel_wait_ms", 350))) / 1000.0

    @functools.wraps(original)
    def wrapped(self: Any, *args: Any, **kwargs: Any):
        stopped = reader.cancel_for_sampler(timeout_s=wait_s)
        if not stopped:
            _log(logging.WARNING, "foreground helper did not stop within %.0f ms before sampler", wait_s * 1000)
        return original(self, *args, **kwargs)

    setattr(wrapped, "_sequential_readahead_foreground_stop", True)
    setattr(wrapped, "_sequential_readahead_foreground_stop_original", original)
    comfy.samplers.KSAMPLER.sample = wrapped
    return True


def _runtime_description() -> tuple[str, str, str]:
    comfy_version = "unknown"
    backend = "unknown"
    device_name = ""
    try:
        import comfyui_version
        comfy_version = str(getattr(comfyui_version, "__version__", "unknown"))
    except Exception:
        pass

    try:
        import torch
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            backend = "XPU"
            try:
                device_name = str(torch.xpu.get_device_name(0))
            except Exception:
                pass
        elif torch.cuda.is_available():
            backend = "CUDA"
            try:
                device_name = str(torch.cuda.get_device_name(0))
            except Exception:
                pass
        elif getattr(torch.version, "hip", None):
            backend = "ROCm"
        else:
            backend = "CPU/other"
    except Exception:
        pass
    return comfy_version, backend, device_name


def _cache_mode_from_argv() -> str:
    argv = [str(arg).lower() for arg in sys.argv[1:]]
    if "--cache-classic" in argv:
        return "classic"
    if "--cache-none" in argv:
        return "none"
    if "--cache-lru" in argv or any(arg.startswith("--cache-lru=") for arg in argv):
        return "lru"
    return "default"


def _async_streams() -> int | None:
    # Prefer the actual ComfyUI runtime value; it captures backend defaults as
    # well as command-line overrides. Fall back to argv if model_management is
    # not importable at custom-node initialization time.
    try:
        import comfy.model_management as mm
        value = getattr(mm, "NUM_STREAMS", None)
        if value is not None:
            return int(value)
    except Exception:
        pass

    argv = [str(arg).lower() for arg in sys.argv[1:]]
    if "--disable-async-offload" in argv:
        return 0
    for index, arg in enumerate(argv):
        if arg == "--async-offload":
            if index + 1 < len(argv):
                try:
                    return int(argv[index + 1])
                except ValueError:
                    return 2
            return 2
        if arg.startswith("--async-offload="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                return 2
    return None


def _emit_compatibility_warnings(config: dict[str, Any], comfy_version: str, backend: str) -> None:
    cache_mode = _cache_mode_from_argv()
    if bool(config.get("warn_nonclassic_cache", True)):
        if cache_mode == "default":
            _log(
                logging.WARNING,
                "Default/non-classic ComfyUI cache detected. Heavy multi-model stress testing on ComfyUI 0.31.1 was unstable with the RAM-pressure cache but stable with --cache-classic. ReadAhead remains enabled; --cache-classic is recommended until this ComfyUI cache path is validated on your version.",
            )
        elif cache_mode == "lru":
            _log(
                logging.WARNING,
                "--cache-lru detected. Heavy multi-model ReadAhead testing validated --cache-classic, not LRU cache; monitor RAM headroom during model switching.",
            )
        elif cache_mode == "none":
            _log(logging.INFO, "--cache-none detected: low cache retention is memory-safe but may cause extra node recomputation.")

    streams = _async_streams()
    if bool(config.get("warn_xpu_async_offload", True)) and backend == "XPU" and streams is not None and streams > 0:
        _log(
            logging.WARNING,
            "XPU async-offload is active with %d stream(s). On the validated Arc B580 / PyTorch 2.13 / ComfyUI 0.31.1 stack, ReadAhead + 2 streams repeatedly caused native partial-unload crashes; 1 stream did not improve performance. Recommended XPU baseline: --disable-async-offload.",
            streams,
        )


def _guard_environment(config: dict[str, Any]) -> tuple[bool, str, str, str]:
    if not bool(config.get("enabled", True)):
        return False, "disabled by config/environment", "unknown", "unknown"
    if bool(config.get("require_windows", True)) and os.name != "nt":
        return False, "Windows file-cache optimization", "unknown", "unknown"
    comfy_version, backend, device_name = _runtime_description()
    detail = f"ComfyUI {comfy_version}, {backend}"
    if device_name:
        detail += f" {device_name}"
    return True, detail, comfy_version, backend


def install_patch() -> bool:
    config = _read_config()
    allowed, reason, comfy_version, backend = _guard_environment(config)
    if not allowed:
        _log(logging.INFO, "v%s not installed: %s", PATCH_VERSION, reason)
        return False

    try:
        reader = SequentialReadAhead(
            min_file_gib=float(config.get("min_file_gib", 2.0)),
            chunk_mib=int(config.get("chunk_mib", 32)),
            extensions=list(config.get("extensions", [".safetensors", ".sft"])),
            log_requests=bool(config.get("log_requests", True)),
            min_start_ram_gib=float(config.get("foreground_min_start_ram_gib", 3.5)),
            stop_ram_gib=float(config.get("foreground_stop_ram_gib", 2.5)),
            ram_check_mib=int(config.get("foreground_ram_check_mib", 256)),
            adaptive_budget=bool(config.get("foreground_adaptive_budget", True)),
            base_reserve_gib=float(config.get("foreground_base_reserve_gib", 3.0)),
            model_reserve_ratio=float(config.get("foreground_model_reserve_ratio", 0.50)),
            model_reserve_min_gib=float(config.get("foreground_model_reserve_min_gib", 2.0)),
            model_reserve_max_gib=float(config.get("foreground_model_reserve_max_gib", 7.0)),
            min_budget_gib=float(config.get("foreground_min_budget_gib", 1.0)),
            max_budget_gib=float(config.get("foreground_max_budget_gib", 0.0)),
        )
    except Exception as exc:
        _log(logging.ERROR, "v%s not installed: invalid configuration: %s", PATCH_VERSION, exc)
        return False

    try:
        import comfy.utils
        original = getattr(comfy.utils, "load_torch_file", None)
        if not callable(original):
            _log(logging.ERROR, "v%s not installed: comfy.utils.load_torch_file unavailable", PATCH_VERSION)
            return False
    except Exception as exc:
        _log(logging.ERROR, "v%s not installed: could not import comfy.utils: %s", PATCH_VERSION, exc)
        return False

    if getattr(original, "_sequential_readahead_patch", False) or getattr(original, "_xpu_sequential_readahead_patch", False):
        _log(logging.WARNING, "Another Sequential ReadAhead version is already active; remove the old folder and restart ComfyUI")
        return False

    @functools.wraps(original)
    def wrapped_load_torch_file(ckpt: Any, *args: Any, **kwargs: Any):
        try:
            reader.request(ckpt)
        except Exception as exc:
            _log(logging.WARNING, "request failed; continuing normal load: %s", exc)
        return original(ckpt, *args, **kwargs)

    setattr(wrapped_load_torch_file, "_sequential_readahead_patch", True)
    setattr(wrapped_load_torch_file, "_sequential_readahead_original", original)
    setattr(wrapped_load_torch_file, "_sequential_readahead_reader", reader)
    comfy.utils.load_torch_file = wrapped_load_torch_file

    sampler_stop_hook = _install_sampler_cancel_hook(reader, config)
    _emit_compatibility_warnings(config, comfy_version, backend)

    cache_mode = _cache_mode_from_argv()
    streams = _async_streams()
    stream_text = "unknown" if streams is None else str(streams)
    _log(
        logging.INFO,
        "Installed v%s (%s): isolated cached read-ahead=True, adaptive-budget=%s, sampler-stop=%s, chunk=%d MiB, RAM floor %.1f/%.1f GiB, cache=%s, async-streams=%s, unload/model-memory hooks=none",
        PATCH_VERSION,
        reason,
        bool(config.get("foreground_adaptive_budget", True)),
        sampler_stop_hook,
        reader.chunk_mib,
        float(config.get("foreground_min_start_ram_gib", 3.5)),
        float(config.get("foreground_stop_ram_gib", 2.5)),
        cache_mode,
        stream_text,
    )
    return True
