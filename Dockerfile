# Predator Browser - Enterprise Agentic Browser
# Multi-stage Docker build for minimal image size

# ============================================
# Stage 1: Build stage
# ============================================
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ============================================
# Stage 2: Runtime stage
# ============================================
FROM python:3.11-slim as runtime

WORKDIR /app

# Install runtime dependencies for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libxshmfence1 \
    # Fonts for proper rendering
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-dejavu-core \
    # Utilities
    ca-certificates \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Playwright browsers
RUN playwright install chromium

# Copy application code
COPY app/ ./app/

# Create non-root user for security
RUN useradd -m -u 1000 predator && \
    chown -R predator:predator /app

USER predator

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PREDATOR_HEADLESS=true \
    PREDATOR_STEALTH=true \
    PREDATOR_VIEWPORT_WIDTH=1920 \
    PREDATOR_VIEWPORT_HEIGHT=1080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "from app.core.predator import PredatorBrowser; print('OK')"

# Expose MCP server via stdio (default)
# For network-based MCP, you would expose a port here

# Default command: Run MCP server
CMD ["python", "-m", "app.server"]
