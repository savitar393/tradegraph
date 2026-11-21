# Stage 1: Compile the pinned OWLAPI / HermiT / SWRLAPI / Drools Java 21 semantic adapter
FROM maven:3.9.9-eclipse-temurin-21 AS java-builder

WORKDIR /build/semantic
COPY semantic/pom.xml ./pom.xml
RUN mvn -B -ntp dependency:copy-dependencies \
    -DincludeScope=runtime \
    -DoutputDirectory=target/lib

COPY semantic/src ./src
RUN mvn -B -ntp compile

# Stage 2: Extract a minimal Temurin Java 21 JRE
FROM eclipse-temurin:21-jre AS jre-base

# Stage 3: Lean production Python 3.12 + Java 21 JRE runtime
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JAVA_HOME=/opt/java/openjdk \
    PATH="/opt/java/openjdk/bin:${PATH}" \
    TRADEGRAPH_KB_PATH=/app/knowledge/tradegraph.ttl \
    TRADEGRAPH_MANIFEST_PATH=/app/sources/manifest.json \
    TRADEGRAPH_CASE_DB_PATH=/data/cases.db \
    TRADEGRAPH_CLASSPATH=/app/semantic/target/classes:/app/semantic/target/lib/*

COPY --from=jre-base /opt/java/openjdk /opt/java/openjdk

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY knowledge/tradegraph.ttl ./knowledge/tradegraph.ttl
COPY sources/manifest.json ./sources/manifest.json
COPY semantic/pom.xml ./semantic/pom.xml
COPY --from=java-builder /build/semantic/target/classes ./semantic/target/classes
COPY --from=java-builder /build/semantic/target/lib ./semantic/target/lib

RUN pip install --no-cache-dir ".[api,ingest]" && \
    useradd --create-home --shell /bin/bash tradegraph && \
    mkdir -p /data && \
    chown -R tradegraph:tradegraph /app /data

USER tradegraph

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ready', timeout=5).status == 200 else 1)"

CMD ["uvicorn", "tradegraph.api.app:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
