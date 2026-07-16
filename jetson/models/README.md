# Model artifacts

Weights live here but **never in git** (`*.pt`, `*.engine`, `*.onnx` are
gitignored) — this README keeps the directory in the repo and documents the
lifecycle.

## Files expected by `speed_limit_model.py`

| File | What it is | Where it comes from |
|---|---|---|
| `speed_limit_detector.pt` | YOLO26n fine-tune, single class `speed_limit_sign` | trained on desktop/Colab — see `jetson/training/README.md` |
| `speed_limit_value.pt` | YOLO11n-cls value reader (`25,30,…,75,other`) | same |
| `speed_limit_detector.engine` | TensorRT FP16 build of the detector @ imgsz 960 | built **on the Jetson** (below) |
| `speed_limit_value.engine` | TensorRT FP16 build of the classifier @ imgsz 96 | built **on the Jetson** |

Override paths with `VISION_SL_DETECTOR` / `VISION_SL_CLASSIFIER` — e.g. on a
desktop point them at the `.pt` files; `YOLO()` loads either format.

## Lifecycle

1. **`.pt` is the source of truth.** Train on the desktop/Colab, copy here:
   `scp speed_limit_*.pt jetson@192.168.100.2:~/maverick-telemetry-hub/jetson/models/`
2. **`.engine` is a build artifact, compiled on-device** (engines are tied to
   the exact GPU + TensorRT version — an engine built anywhere else will not
   load). One-time, takes minutes:

   ```bash
   cd ~/maverick-telemetry-hub/jetson
   yolo export model=models/speed_limit_detector.pt format=engine imgsz=960 half=True
   yolo export model=models/speed_limit_value.pt    format=engine imgsz=96  half=True
   ```

   `imgsz` here must match `VISION_SL_IMGSZ` (default 960) — a fixed-shape
   engine only accepts the size it was exported at.
3. **Rebuild engines after any JetPack/TensorRT upgrade** (step 2 again from
   the kept `.pt` files). If `speed_limit_model.init()` logs a load failure
   after an upgrade, this is why.

No model files present? That's a supported state — `vision_publisher` logs
one warning and runs the scene track only.
