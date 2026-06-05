FROM python:3.11-slim

WORKDIR /app

# System dependencies for asyncpg (libpq + gcc) and OpenCV (used by albumentations/grad-cam)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only PyTorch first.
# Without this, pip would pull the ~2 GB CUDA build from PyPI.
# CPU build is ~350 MB and sufficient for inference.
RUN pip install --no-cache-dir \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/cpu

# Install the rest of the dependencies.
# We filter out torch/torchvision because they are already installed above
# and we don't want pip to replace them with the CUDA version from PyPI.
COPY requirements.txt .
RUN grep -vE "^(torch|torchvision)" requirements.txt > /tmp/req.txt && \
    pip install --no-cache-dir -r /tmp/req.txt

COPY . .

EXPOSE 8000

# Render injects $PORT at runtime; fall back to 8000 locally
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
