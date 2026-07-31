#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-systemd-compose-nuitka:manylinux2014}"
PLATFORM="${PLATFORM:-linux/amd64}"
DOCKERFILE="${DOCKERFILE:-packaging/nuitka/Dockerfile}"
OUT_DIR="${OUT_DIR:-dist/nuitka}"
NUITKA_VERSION="${NUITKA_VERSION:-4.1.3}"

if docker buildx version >/dev/null 2>&1; then
  docker buildx build \
    --load \
    --platform "${PLATFORM}" \
    --tag "${IMAGE}" \
    --build-arg "NUITKA_VERSION=${NUITKA_VERSION}" \
    --file "${DOCKERFILE}" \
    .
else
  docker build \
    --platform "${PLATFORM}" \
    --tag "${IMAGE}" \
    --build-arg "NUITKA_VERSION=${NUITKA_VERSION}" \
    --file "${DOCKERFILE}" \
    .
fi

mkdir -p "${OUT_DIR}"

container_id="$(docker create "${IMAGE}")"
cleanup() {
  docker rm -f "${container_id}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker cp "${container_id}:/out/." "${OUT_DIR}/"

printf 'Nuitka Linux artifacts copied to %s\n' "${OUT_DIR}"
