# Published Penpot 2.18.5 linux/amd64 manifests, not mutable release tags.
FROM penpotapp/backend@sha256:3d3d1eeb5a488efd31ec22038374eb6b52dccc21532047c2e508a18193f3e71f AS backend
FROM penpotapp/frontend@sha256:d5bec7ab86328a2bce02b331291eab5833dcb56e403428ad07ccd6740be73735 AS frontend
FROM penpotapp/exporter@sha256:48b66f774d338f0ed25d58c6d42f9f107a99170f67bf75ce76361d3dc9e6e27b
USER root
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    python3 python3-venv fontforge woff-tools woff2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=backend /opt/jre /opt/jre
COPY --from=backend /opt/penpot/backend /opt/penpot/backend
COPY --from=frontend /var/www/app /var/www/app
WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /app/.venv && /app/.venv/bin/pip install \
    --no-cache-dir --require-hashes -r requirements.txt
COPY design ./design
COPY licenses ./licenses
COPY NOTICE ./NOTICE
ENV PATH="/app/.venv/bin:/opt/jre/bin:/opt/imagick/bin:$PATH" \
    JAVA_HOME=/opt/jre HOME=/tmp PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/python", "-m", "design.runtime"]
