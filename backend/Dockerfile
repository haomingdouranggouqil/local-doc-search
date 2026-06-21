FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG CAJ2PDF_REF=6c4bc32b15ce748d211f45d536f5d5511ef9f368

RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    ca-certificates \
    catdoc \
    curl \
    fonts-noto-cjk \
    fontconfig \
    libglib2.0-0 \
    libreoffice-writer \
    mupdf-tools \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://github.com/caj2pdf/caj2pdf/archive/${CAJ2PDF_REF}.tar.gz" -o /tmp/caj2pdf.tar.gz \
    && mkdir -p /opt/caj2pdf \
    && tar -xzf /tmp/caj2pdf.tar.gz -C /opt/caj2pdf --strip-components=1 \
    && printf '#!/bin/sh\nexec python /opt/caj2pdf/caj2pdf "$@"\n' > /usr/local/bin/caj2pdf \
    && chmod +x /usr/local/bin/caj2pdf \
    && rm /tmp/caj2pdf.tar.gz

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r /app/requirements.txt

COPY app /app/app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
