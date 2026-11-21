FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS python-base

FROM python-base AS python-build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra web --extra hosted
COPY src/orbit_sim/ ./src/orbit_sim/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra web --extra hosted

FROM maven:3.9-eclipse-temurin-21@sha256:99e61abcff91a9b1333463bd8451fb18495d6eba9250ac66a338b518f8278320 AS java-build
WORKDIR /build
COPY controller/pom.xml ./controller/pom.xml
COPY controller/src/ ./controller/src/
RUN --mount=type=cache,target=/root/.m2 \
    mvn --batch-mode --no-transfer-progress -f controller/pom.xml verify
RUN jlink --add-modules jdk.httpserver,java.logging \
    --strip-debug --no-header-files --no-man-pages --output /orbit-jre

FROM python-base
RUN groupadd --gid 10001 orbit && useradd --uid 10001 --gid orbit --no-create-home --shell /usr/sbin/nologin orbit \
    && install -d -m 0700 -o orbit -g orbit /var/lib/orbit
COPY --from=java-build /orbit-jre /opt/java
COPY --from=java-build /build/controller/target/orbit-controller-*.jar /opt/orbit/controller.jar
COPY --from=python-build /opt/venv /opt/venv
COPY deploy/container_entrypoint.py /opt/orbit/container_entrypoint.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
USER 10001:10001
WORKDIR /opt/orbit
ENTRYPOINT ["/opt/venv/bin/python", "/opt/orbit/container_entrypoint.py"]
