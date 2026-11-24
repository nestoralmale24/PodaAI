# Poda AI: imagen de ejecución (Python 3.11 + dependencias fijadas con hashes).
# El código se monta como volumen (docker-compose.yml): cambiarlo no obliga a reconstruir.
FROM python:3.11-slim-bookworm

# curl y unzip: para instalar SonarScanner al construir la imagen.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip less nano nodejs npm \
 && rm -rf /var/lib/apt/lists/*

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
 && rm /tmp/requirements.lock

# SonarScanner CLI (lleva su propio JRE): primera fase de cada limpieza. Se comprueba su SHA-256.
ARG SONAR_SCANNER_VERSION=6.2.1.4610
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) arch=linux-x64 ;; arm64) arch=linux-aarch64 ;; \
      *) echo "Arquitectura no soportada por SonarScanner: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    url="https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SONAR_SCANNER_VERSION}-${arch}.zip"; \
    curl -fsSL "$url" -o /tmp/scanner.zip; \
    curl -fsSL "$url.sha256" -o /tmp/scanner.sha256; \
    echo "$(cut -d' ' -f1 /tmp/scanner.sha256)  /tmp/scanner.zip" | sha256sum -c -; \
    unzip -q /tmp/scanner.zip -d /opt; \
    mv /opt/sonar-scanner-${SONAR_SCANNER_VERSION}-${arch} /opt/sonar-scanner; \
    rm -f /tmp/scanner.zip /tmp/scanner.sha256
ENV PATH="/opt/sonar-scanner/bin:${PATH}"

# Claude Code (CLI oficial): permite usar tu plan Pro/Max como proveedor. Versión fija.
ARG CLAUDE_CODE_VERSION=2.1.331
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force

RUN useradd -m -u 1000 poda \
 && printf '#!/bin/sh\nexec python -m poda "$@"\n' > /usr/local/bin/poda \
 && chmod +x /usr/local/bin/poda
USER poda
ENV PYTHONPATH=/home/poda/workspace \
    LITELLM_LOCAL_MODEL_COST_MAP=True
WORKDIR /home/poda/workspace
CMD ["/bin/bash"]
