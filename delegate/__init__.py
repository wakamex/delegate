"""The agent delegation workflow's helper: config, ledger and task selection."""

import os

# The model's matrices are small; threaded BLAS spends far longer coordinating threads than computing, so numpy is
# held to one thread before any module imports it.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

__version__ = "0.2.0"
