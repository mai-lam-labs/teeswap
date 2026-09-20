# developer toolchain — Astral uv + ruff (lint/format) + ty + pyrefly (typecheck).
#
# FULLY SELF-CONTAINED: uv installs into ./.uv (not ~/.local/bin), its cache is
# ./.uv/cache, Python interpreters go to ./.uv/python, and the venv is ./.venv —
# nothing touches $HOME or the system.
# The venv is VSCode-compatible (.vscode/settings.json points at .venv/bin/python).
#
#   make install          project-local uv + create .venv + install tools
#   make check            lint + format-check + typecheck + test (CI gate)
#   make bundle-x86_64    self-contained musl Python bundle image
#   make payload-x86_64   lockboot stage2 binary (bootstrap + erofs + dm-verity)
#   make boot-x86_64      boot the full lockboot chain under QEMU
#   make clean             remove .venv + caches  |  make distclean  also .uv + dist

SRC  := teeswap
PYVER := 3.14

TOOLS := $(CURDIR)/.uv
UV    := $(TOOLS)/uv
VENV  := $(CURDIR)/.venv
PY    := $(VENV)/bin/python
RUFF    := $(VENV)/bin/ruff
TY      := $(VENV)/bin/ty
PYREFLY := $(VENV)/bin/pyrefly

export UV_CACHE_DIR            := $(TOOLS)/cache
export UV_PYTHON_INSTALL_DIR   := $(TOOLS)/python

# ---- lockboot integration ----
# Copy lockboot artifacts into dist/<arch>/ manually, pinned by hash.
# TODO: download from lockboot release URLs.
BOOTSTRAP_SHA256_x86_64  := 6694c4bacfb8d81446364e3aa446e33cc6ad431ee48f760ba23169e001d03937
BOOTSTRAP_SHA256_aarch64 := 42ac5dcf2d11614c973bbc8acb14aa3e1976c8bc02ec65a4d4b67bc381629691
BOOT_DISK_SHA256_x86_64  := a1e4a4f791f356100fc33e218007dd02ad67168ae689ef4b6369548bdb3fe671
UKI_SHA256_x86_64        := 283c5b1726ebd10cc95c4cbc0d3c751260e642a505baa03ae197672e7ae6d9f2

SERVE_HOST ?= 10.0.2.1:8000

# KVM passthrough (same pattern as lockboot's build.mk).
KVM_GID   := $(shell stat -c %g /dev/kvm 2>/dev/null || echo "")
KVM_MOUNT := $(shell test -e /dev/kvm && echo "-v /dev/kvm:/dev/kvm")
DOCKER_OPT_KVM := $(if $(KVM_GID),--group-add $(KVM_GID)) $(KVM_MOUNT)

.PHONY: help install uv-bootstrap lint format format-check typecheck test coverage check build clean distclean

help:
	@echo "targets: install | check | bundle-<arch> | payload-<arch> | boot-<arch> | clean | distclean"

uv-bootstrap:
	@test -x "$(UV)" || { \
	  echo ">> installing project-local uv into $(TOOLS)"; \
	  mkdir -p "$(TOOLS)"; \
	  curl -LsSf https://astral.sh/uv/install.sh \
	    | env UV_UNMANAGED_INSTALL="$(TOOLS)" UV_NO_MODIFY_PATH=1 sh; }

install: uv-bootstrap
	"$(UV)" venv "$(VENV)" --python $(PYVER) --clear
	"$(UV)" pip install --python "$(PY)" -e . --group dev
	@echo ">> toolchain ready (project-local); 'make check' to run everything"

lint:
	"$(RUFF)" check $(SRC)

format:
	"$(RUFF)" format $(SRC)

format-check:
	"$(RUFF)" format --check $(SRC)

typecheck:
	"$(TY)" check $(SRC)
	"$(PYREFLY)" check $(SRC)

test:
	"$(PY)" -m pytest $(SRC)/tests -q --tb=short --no-header --durations=0 -vv

coverage:
	"$(PY)" -m coverage run --source=$(SRC) -m pytest $(SRC)/tests -q --tb=short --no-header
	"$(PY)" -m coverage report -m --fail-under=70

check: lint format-check typecheck test

build:
	"$(UV)" build --python "$(PY)" --out-dir dist

# ---- bundle-<arch>: Docker image with stripped musl Python + teeswap ----
bundle-%:
	docker build -f Dockerfile.bundle --build-arg PYVER=$(PYVER) -t teeswap:bundle-$* .
	@echo ">> image built: teeswap:bundle-$*"

# ---- payload-<arch>: lockboot stage2 binary ----
payload-%:
	@test -f dist/$*/bootstrap || { echo "error: dist/$*/bootstrap not found — copy the lockboot stage2 loader into dist/$*/"; exit 1; }
	@[ "$$(sha256sum dist/$*/bootstrap | cut -d' ' -f1)" = "$(BOOTSTRAP_SHA256_$*)" ] || \
	  { echo "error: dist/$*/bootstrap hash mismatch (expected $(BOOTSTRAP_SHA256_$*))"; exit 1; }
	docker export "$$(docker create --rm teeswap:bundle-$*)" \
	  | docker run --rm -i \
	      -v "$(CURDIR)/dist/$*/bootstrap:/bootstrap:ro" \
	      lockboot:erofs-builder --bootstrap /bootstrap - - > dist/$*/stage2
	@chmod +x dist/$*/stage2
	@ls -lh dist/$*/stage2
	@echo ">> stage2 ready at dist/$*/stage2"

# ---- boot-<arch>: full lockboot chain under QEMU ----
# Requires dist/<arch>/{stage2, boot.disk, linux.efi} — copy lockboot artifacts manually.
boot-%:
	@test -f dist/$*/stage2    || { echo "error: dist/$*/stage2 not found — run 'make payload-$*'"; exit 1; }
	@test -f dist/$*/boot.disk || { echo "error: dist/$*/boot.disk not found — copy from lockboot stage0 build"; exit 1; }
	@test -f dist/$*/linux.efi || { echo "error: dist/$*/linux.efi not found — copy from lockboot stage1 build"; exit 1; }
	@[ "$$(sha256sum dist/$*/boot.disk | cut -d' ' -f1)" = "$(BOOT_DISK_SHA256_$*)" ] || \
	  { echo "error: dist/$*/boot.disk hash mismatch"; exit 1; }
	@[ "$$(sha256sum dist/$*/linux.efi | cut -d' ' -f1)" = "$(UKI_SHA256_$*)" ] || \
	  { echo "error: dist/$*/linux.efi hash mismatch"; exit 1; }
	@D="dist/$*/boot"; rm -rf "$$D"; mkdir -p "$$D"; H="http://$(SERVE_HOST)"; \
	cp dist/$*/linux.efi "$$D/linux.efi"; \
	cp dist/$*/stage2 "$$D/stage2"; \
	cp dist/$*/boot.disk "$$D/boot.disk"; \
	truncate -s +256M "$$D/boot.disk"; \
	cp dist/$*/efi-vars.ovmf "$$D/efi-vars.ovmf"; \
	UKI_SHA=$$(sha256sum "$$D/linux.efi" | cut -d' ' -f1); \
	S2_SHA=$$(sha256sum "$$D/stage2" | cut -d' ' -f1); \
	printf '{\n  "_stage1": { "$*": { "payload": { "url": "%s/linux.efi", "sha256": "%s" } } },\n  "_stage2": { "$*": { "payload": { "url": "%s/stage2", "sha256": "%s" } } },\n  "teeswap": { "bind_host": "0.0.0.0", "bind_port": 8402 }\n}\n' \
	  "$$H" "$$UKI_SHA" "$$H" "$$S2_SHA" > "$$D/user-data.json"; \
	echo "user-data: sha256 mode (UKI $$UKI_SHA, stage2 $$S2_SHA)"; \
	cat "$$D/user-data.json"; \
	docker run --rm --privileged \
	  $(DOCKER_OPT_KVM) \
	  -e YES_INSIDE_DOCKER_DO_DANGEROUS_IPTABLES=1 \
	  --cap-add=NET_ADMIN --device=/dev/net/tun \
	  -v "$(CURDIR)/dist/$*/boot:/boot" \
	  lockboot:harness --kind stage0 --arch $* \
	    --boot-disk /boot/boot.disk \
	    --serve-dir /boot \
	    --user-data /boot/user-data.json

clean:
	rm -rf "$(VENV)" "$(TOOLS)/cache" .ruff_cache

distclean: clean
	rm -rf "$(TOOLS)" dist
