FROM quay.io/pypa/manylinux2014_x86_64

ARG PYTHON_TAG=cp312-cp312
ARG NUITKA_VERSION=4.1.3

ENV PYTHON_BIN=/opt/python/${PYTHON_TAG}/bin/python
ENV PATH=/opt/python/${PYTHON_TAG}/bin:${PATH}
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /src

COPY pyproject.toml README.md ./
COPY src ./src

RUN ${PYTHON_BIN} -m pip install --upgrade pip wheel setuptools && \
    ${PYTHON_BIN} -m pip install . && \
    ${PYTHON_BIN} -m pip install "nuitka==${NUITKA_VERSION}" patchelf

RUN cd /opt/_internal && tar xf static-libs-for-embedding-only.tar.xz

RUN ${PYTHON_BIN} -m nuitka \
    --standalone \
    --onefile \
    --onefile-no-compression \
    --assume-yes-for-downloads \
    --output-dir=/build \
    --output-filename=systemd-compose \
    --nofollow-import-to=pytest \
    --python-flag=-m \
    src/systemd_compose

RUN mkdir -p /out && \
    cp /build/systemd-compose /out/systemd-compose-linux-x86_64 && \
    chmod 0755 /out/systemd-compose-linux-x86_64 && \
    /out/systemd-compose-linux-x86_64 --help >/out/help.txt && \
    ldd --version >/out/ldd-version.txt 2>&1 && \
    readelf --version-info /out/systemd-compose-linux-x86_64 >/out/glibc-symbols.txt 2>&1 || true && \
    sha256sum /out/systemd-compose-linux-x86_64 >/out/SHA256SUMS
