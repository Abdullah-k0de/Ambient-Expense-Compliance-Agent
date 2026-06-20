FROM python:3.12-slim

WORKDIR /app

# Install uv for fast package installation
RUN pip install uv

# Copy package config
COPY pyproject.toml README.md ./

# Compile and install dependencies
RUN uv pip compile pyproject.toml -o requirements.txt && \
    uv pip install --system -r requirements.txt

# Copy source code and templates
COPY main.py ./
COPY templates ./templates

# Expose port (Cloud Run defaults to 8080)
EXPOSE 8080

# Run uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
