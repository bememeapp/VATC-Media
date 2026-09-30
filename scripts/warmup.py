"""Download the small segmentation model during build, not the first paid job."""
import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
from rembg import new_session
new_session(os.getenv("SEGMENTATION_MODEL", "u2netp"), providers=["CPUExecutionProvider"])
print("Subject protection model is ready.")
