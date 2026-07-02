FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY . /app/core
ENV CORE_CONFIG=/config/core.json
EXPOSE 8100
CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8100"]
