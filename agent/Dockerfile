FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose the assistant API port
EXPOSE 8000
EXPOSE 5678

# Default command
CMD ["python", "-Xfrozen_modules=off", "-m", "uvicorn", "api:app", "--app-dir", "app", "--host", "0.0.0.0", "--port", "8000"]
