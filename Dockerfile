FROM python:3.11-slim

WORKDIR /app

# Upgrade pip and install curl for healthchecks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Back4App supplies the PORT environment variable. We default to 8080 if not set.
ENV PORT=8080

EXPOSE $PORT

# Run the FastAPI app using Uvicorn
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
