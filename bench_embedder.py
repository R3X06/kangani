"""Batch 0 -- run this on YOUR machine. Do not commit it.

Two jobs:
  1. Prove the model weights actually download on your network.
  2. Measure the two numbers I cannot produce from my sandbox, because
     huggingface.co is not in its egress allowlist: per-text embed latency,
     and how much resident memory a loaded ONNX session costs.

Usage (PowerShell, from anywhere):

    pip install fastembed psutil
    python bench_embedder.py

psutil is only for the memory reading. If you would rather not install it the
script still runs and just skips that line.

Paste the whole output back, including a failure. A failure here is a useful
result, not a wasted run.
"""

import gc
import platform
import statistics
import sys
import time

SAMPLES = [
    "Backpropagation computes gradients by applying the chain rule backwards "
    "through the network, reusing the forward pass activations.",
    "Tutorial for CZ1103 moved to LT19 from week 7 onwards.",
    "Remember to submit the hackathon devpost before Friday midnight.",
    "The registrar timetable comes from STARS, not from NTULearn, so the "
    "Blackboard API cannot replace the PDF parser.",
    "Gym session Tuesday and Thursday evenings, 7pm, keep it to 45 minutes.",
]


def rss_mb():
    try:
        import psutil
    except ImportError:
        return None
    return psutil.Process().memory_info().rss / 1e6


def main() -> int:
    print(f"python  : {sys.version.split()[0]}")
    print(f"platform: {platform.platform()}  ({platform.machine()})")

    baseline = rss_mb()
    print(f"rss before import: {baseline:.1f} MB" if baseline else
          "rss before import: (psutil not installed, skipping memory)")

    t0 = time.perf_counter()
    from fastembed import TextEmbedding
    t_import = (time.perf_counter() - t0) * 1000
    print(f"import fastembed : {t_import:.0f} ms")

    after_import = rss_mb()
    if after_import:
        print(f"rss after import : {after_import:.1f} MB "
              f"(+{after_import - baseline:.1f})")

    # This is the line that downloads ~67 MB the first time. Run the script
    # twice: the second run reads the cache, so the delta between the two
    # timings separates download cost from session-init cost.
    print("\nloading model (downloads ~67 MB on first run)...")
    t0 = time.perf_counter()
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    t_load = time.perf_counter() - t0
    print(f"model load       : {t_load:.2f} s")

    gc.collect()
    after_load = rss_mb()
    if after_load:
        print(f"rss after load   : {after_load:.1f} MB "
              f"(+{after_load - after_import:.1f} for the session)")

    # Warm up once -- the first inference pays one-off allocation costs that
    # would otherwise be charged to the measurement.
    list(model.embed(["warmup"]))

    print("\nper-text embed latency (single text, 20 runs):")
    timings = []
    for _ in range(20):
        t0 = time.perf_counter()
        vectors = list(model.embed([SAMPLES[0]]))
        timings.append((time.perf_counter() - t0) * 1000)
    print(f"  median {statistics.median(timings):.1f} ms | "
          f"min {min(timings):.1f} | max {max(timings):.1f}")
    print(f"  vector dim: {len(vectors[0])}")

    t0 = time.perf_counter()
    batch = list(model.embed(SAMPLES))
    t_batch = (time.perf_counter() - t0) * 1000
    print(f"batch of {len(SAMPLES)}      : {t_batch:.1f} ms "
          f"({t_batch / len(SAMPLES):.1f} ms/text)")

    gc.collect()
    peak = rss_mb()
    if peak:
        print(f"\nrss after inference: {peak:.1f} MB")
        print(f"TOTAL embedding cost: +{peak - baseline:.1f} MB resident")

    print("\nOK -- weights downloaded and inference ran.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
