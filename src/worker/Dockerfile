FROM python:3.14-slim

WORKDIR /app

# Install dependencies first (leverages Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the execution scripts
COPY . .

# Set default command (can be overridden by systemd)
CMD ["python3", "main.py"]