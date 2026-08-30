Run from the custom-node folder with:

```bash
python -m pytest -q tests
```

The tests do not require a GPU or ComfyUI installation. The helper-process lifecycle tests use small temporary files and reduced test-only thresholds.
