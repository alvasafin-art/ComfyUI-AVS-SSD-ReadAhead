from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "readahead.py"
spec = importlib.util.spec_from_file_location("avs_readahead_test_module", MODULE_PATH)
assert spec is not None and spec.loader is not None
ra = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ra
spec.loader.exec_module(ra)


def configure_small_runtime(monkeypatch) -> None:
    monkeypatch.setattr(ra, "_MIN_FILE_BYTES", 1)
    monkeypatch.setattr(ra, "_CHUNK_BYTES", 64 * 1024)
    monkeypatch.setattr(ra, "_BASE_RESERVE_BYTES", 0)
    monkeypatch.setattr(ra, "_MODEL_RESERVE_RATIO", 0.0)
    monkeypatch.setattr(ra, "_MODEL_RESERVE_MIN_BYTES", 0)
    monkeypatch.setattr(ra, "_MODEL_RESERVE_MAX_BYTES", 0)
    monkeypatch.setattr(ra, "_MIN_BUDGET_BYTES", 1)
    monkeypatch.setattr(ra, "_PROCESS_WAIT_S", 0.2)
    monkeypatch.setattr(ra, "_MONITOR_INTERVAL_S", 0.01)
    monkeypatch.setattr(ra, "_available_memory", lambda: 1024 * 1024 * 1024)


def make_file(tmp_path: Path, name: str = "model.safetensors", size: int = 2 * 1024 * 1024) -> Path:
    path = tmp_path / name
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


def wait_for(reader, predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    with reader._cv:
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            reader._cv.wait(timeout=min(0.02, remaining))
    return True


def test_model_reserve_is_clamped():
    assert ra._model_reserve(1 * ra._GIB) == 2 * ra._GIB
    assert ra._model_reserve(8 * ra._GIB) == 4 * ra._GIB
    assert ra._model_reserve(100 * ra._GIB) == 7 * ra._GIB


def test_candidate_filters_extension_size_and_missing(tmp_path, monkeypatch):
    configure_small_runtime(monkeypatch)
    good = make_file(tmp_path, "good.safetensors", 1024)
    bad_ext = make_file(tmp_path, "bad.bin", 1024)

    assert ra._SequentialReadAhead._candidate(good) is not None
    assert ra._SequentialReadAhead._candidate(bad_ext) is None
    assert ra._SequentialReadAhead._candidate(tmp_path / "missing.safetensors") is None

    monkeypatch.setattr(ra, "_MIN_FILE_BYTES", 2048)
    assert ra._SequentialReadAhead._candidate(good) is None


def test_plan_respects_ram_reserve_rounding_and_minimum(monkeypatch):
    configure_small_runtime(monkeypatch)
    monkeypatch.setattr(ra, "_available_memory", lambda: None)
    assert ra._SequentialReadAhead._plan(1024) is None

    monkeypatch.setattr(ra, "_available_memory", lambda: 100)
    monkeypatch.setattr(ra, "_BASE_RESERVE_BYTES", 100)
    assert ra._SequentialReadAhead._plan(1024) is None

    monkeypatch.setattr(ra, "_BASE_RESERVE_BYTES", 32)
    monkeypatch.setattr(ra, "_CHUNK_BYTES", 16)
    budget, reserve, available = ra._SequentialReadAhead._plan(1024)
    assert (budget, reserve, available) == (64, 32, 100)


def test_sequential_read_completes_and_worker_shuts_down(tmp_path, monkeypatch):
    configure_small_runtime(monkeypatch)
    path = make_file(tmp_path)
    reader = ra._SequentialReadAhead()
    try:
        assert reader.request(path)
        assert wait_for(reader, lambda: reader._active is None and reader._pending is None)
    finally:
        assert reader.shutdown()
    assert not reader._worker_thread.is_alive()


def test_sampling_stop_terminates_active_helper(tmp_path, monkeypatch):
    configure_small_runtime(monkeypatch)
    path = make_file(tmp_path)
    reader = ra._SequentialReadAhead()
    reader._CHILD_READER_CODE = "import time; time.sleep(10)"
    try:
        assert reader.request(path)
        assert wait_for(reader, lambda: reader._active_process is not None)
        with reader._cv:
            process = reader._active_process
        assert process is not None
        assert reader.stop_for_sampling(timeout_s=1.0)
        assert process.poll() is not None
        assert wait_for(reader, lambda: reader._active is None)
    finally:
        reader.shutdown()


def test_newer_request_terminates_previous_helper(tmp_path, monkeypatch):
    configure_small_runtime(monkeypatch)
    first = make_file(tmp_path, "first.safetensors")
    second = make_file(tmp_path, "second.safetensors")
    reader = ra._SequentialReadAhead()
    reader._CHILD_READER_CODE = (
        "import sys,time; "
        "time.sleep(10) if sys.argv[1].endswith('first.safetensors') else None; "
        "print(1, flush=True)"
    )
    try:
        assert reader.request(first)
        assert wait_for(reader, lambda: reader._active_process is not None)
        with reader._cv:
            first_process = reader._active_process
        assert first_process is not None

        assert reader.request(second)
        assert wait_for(reader, lambda: reader._active is None and reader._pending is None)
        assert first_process.poll() is not None
    finally:
        reader.shutdown()


def test_ram_pressure_stops_active_helper(tmp_path, monkeypatch):
    configure_small_runtime(monkeypatch)
    path = make_file(tmp_path)
    monkeypatch.setattr(ra, "_BASE_RESERVE_BYTES", 1)
    low_memory = False

    def available_memory():
        return 0 if low_memory else 1024 * 1024 * 1024

    monkeypatch.setattr(ra, "_available_memory", available_memory)
    reader = ra._SequentialReadAhead()
    reader._CHILD_READER_CODE = "import time; time.sleep(10)"
    try:
        assert reader.request(path)
        assert wait_for(reader, lambda: reader._active_process is not None)
        low_memory = True
        assert wait_for(reader, lambda: reader._active is None and reader._pending is None)
    finally:
        reader.shutdown()


def test_shutdown_terminates_helper_and_worker(tmp_path, monkeypatch):
    configure_small_runtime(monkeypatch)
    path = make_file(tmp_path)
    reader = ra._SequentialReadAhead()
    reader._CHILD_READER_CODE = "import time; time.sleep(10)"
    try:
        assert reader.request(path)
        assert wait_for(reader, lambda: reader._active_process is not None)
        with reader._cv:
            process = reader._active_process
        assert process is not None
        assert reader.shutdown(timeout_s=1.0)
        assert process.poll() is not None
        assert not reader._worker_thread.is_alive()
    finally:
        reader.shutdown()


def test_persist_enabled_migrates_old_config_to_minimal(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"enabled": True, "old_option": 123}), encoding="utf-8")
    monkeypatch.setattr(ra, "_CONFIG_PATH", config)

    ra.persist_enabled(False)
    assert json.loads(config.read_text(encoding="utf-8")) == {"enabled": False}


def test_disabled_controller_is_noop():
    controller = ra._ReadAheadController(False)
    assert controller.request("anything.safetensors") is False
    assert controller.stop_for_sampling() is True
    assert controller.status()["enabled"] is False
