FROM python:3.14-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Expose port 8000 for network requests
EXPOSE 8000

# Run Uvicorn listening on all network interfaces inside the container
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]