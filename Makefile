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
VAPORTPM_SHA256_x86_64   := 8fa11b42cf12abf942eb14bee5be7613d97dc75642a62095fd03d90c8448d961
BOOT_DISK_SHA256_x86_64  := a1e4a4f791f356100fc33e218007dd02ad67168ae689ef4b6369548bdb3fe671
UKI_SHA256_x86_64        := 283c5b1726ebd10cc95c4cbc0d3c751260e642a505baa03ae197672e7ae6d9f2
EFI_VARS_SHA256_x86_64   := d55f52b13eba249a2d80ef85f63f1234be956c3fa609185f42188acbdf47ac1e

# ---- dev tools (project-local, in dist/tools/) ----
FOUNDRY_VERSION  := v1.8.3
FOUNDRY_TARBALL  := foundry_$(FOUNDRY_VERSION)_linux_amd64.tar.gz
FOUNDRY_SHA256   := 7ca48e6ca3cac1bce1403ca67e5bc1dc3bc1fd818199c9957c7165079c228568
FOUNDRY_URL      := https://github.com/foundry-rs/foundry/releases/download/$(FOUNDRY_VERSION)/$(FOUNDRY_TARBALL)
ANVIL            := dist/tools/anvil
ANVIL_PORT       ?= 8545
ANVIL_PID        := dist/tools/anvil.pid

# ---- local x402 facilitator (x402-rs, pinned by digest) ----
FACILITATOR_IMAGE     := ghcr.io/x402-rs/x402-facilitator@sha256:3a71fd6e3ff5ae00c30bcfbff35e5cf858bb37b9b97de032f46545c18e953d71
FACILITATOR_CONTAINER := teeswap-facilitator
FACILITATOR_PORT      ?= 18080
FACILITATOR_DIR       := dist/tools/facilitator
# anvil's first dev account: the facilitator pays gas for settlement from it
FACILITATOR_KEY       := 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80

SERVE_HOST ?= 10.0.2.1:8000
FORWARD_PORTS ?= 8402

# KVM passthrough (same pattern as lockboot's build.mk).
KVM_GID   := $(shell stat -c %g /dev/kvm 2>/dev/null || echo "")
KVM_MOUNT := $(shell test -e /dev/kvm && echo "-v /dev/kvm:/dev/kvm")
DOCKER_OPT_KVM := $(if $(KVM_GID),--group-add $(KVM_GID)) $(KVM_MOUNT)

.PHONY: help install uv-bootstrap lint format format-check typecheck test coverage coverage-run check ci build anvil-fetch anvil-run anvil-start anvil-stop facilitator-start facilitator-stop clean distclean

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

# the tests need anvil and the facilitator: started here, stopped after, as for check
coverage: anvil-start
	$(MAKE) facilitator-start && $(MAKE) coverage-run; rc=$$?; \
	  $(MAKE) facilitator-stop anvil-stop; exit $$rc

coverage-run:
	"$(PY)" -m coverage run --source=$(SRC) -m pytest $(SRC)/tests -q --tb=short --no-header
	"$(PY)" -m coverage report -m --fail-under=70

check: anvil-start
	$(MAKE) facilitator-start && $(MAKE) lint format-check typecheck test; rc=$$?; \
	  $(MAKE) facilitator-stop anvil-stop; exit $$rc

ci: check

build:
	"$(UV)" build --python "$(PY)" --out-dir dist

# ---- anvil: local EVM node for integration tests ----
anvil-fetch:
	@if test -x "$(ANVIL)"; then echo ">> anvil already at $(ANVIL)"; else \
	  mkdir -p dist/tools && \
	  curl -LsSf "$(FOUNDRY_URL)" -o dist/tools/$(FOUNDRY_TARBALL) && \
	  { [ "$$(sha256sum dist/tools/$(FOUNDRY_TARBALL) | cut -d' ' -f1)" = "$(FOUNDRY_SHA256)" ] || \
	    { echo "error: foundry tarball hash mismatch (expected $(FOUNDRY_SHA256))"; rm -f dist/tools/$(FOUNDRY_TARBALL); exit 1; }; } && \
	  tar -xzf dist/tools/$(FOUNDRY_TARBALL) -C dist/tools anvil && \
	  rm dist/tools/$(FOUNDRY_TARBALL) && \
	  echo ">> anvil $(FOUNDRY_VERSION) ready at $(ANVIL)"; \
	fi

anvil-run: anvil-fetch
	$(ANVIL) --port $(ANVIL_PORT) --accounts 10 --balance 1000

anvil-start: anvil-fetch
	@if test -f "$(ANVIL_PID)" && kill -0 $$(cat "$(ANVIL_PID)") 2>/dev/null; then \
	  echo ">> anvil already running (pid $$(cat "$(ANVIL_PID)"))"; \
	else \
	  nohup $(ANVIL) --port $(ANVIL_PORT) --accounts 10 --balance 1000 --silent \
	    > dist/tools/anvil.log 2>&1 & echo $$! > "$(ANVIL_PID)"; \
	  sleep 1; \
	  if kill -0 $$(cat "$(ANVIL_PID)") 2>/dev/null; then \
	    echo ">> anvil started on :$(ANVIL_PORT) (pid $$(cat "$(ANVIL_PID)"))"; \
	  else \
	    echo "error: anvil failed to start"; rm -f "$(ANVIL_PID)"; exit 1; \
	  fi; \
	fi
	@"$(PY)" -m $(SRC).tests.localchain http://127.0.0.1:$(ANVIL_PORT)

anvil-stop:
	@if test -f "$(ANVIL_PID)" && kill -0 $$(cat "$(ANVIL_PID)") 2>/dev/null; then \
	  kill $$(cat "$(ANVIL_PID)") && echo ">> anvil stopped"; \
	  rm -f "$(ANVIL_PID)"; \
	else \
	  echo ">> anvil not running"; \
	  rm -f "$(ANVIL_PID)"; \
	fi

# ---- facilitator: local x402 facilitator against anvil (needs anvil-start first) ----
facilitator-start:
	@curl -s -m 2 -o /dev/null -X POST -H 'content-type: application/json' \
	  --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
	  http://127.0.0.1:$(ANVIL_PORT) || { echo "error: anvil not running on :$(ANVIL_PORT) — run make anvil-start"; exit 1; }
	@# the contracts the facilitator requires are installed by anvil-start (tests/localchain.py)
	@mkdir -p $(FACILITATOR_DIR)
	@printf '%s\n' \
	  '{' \
	  '  "port": $(FACILITATOR_PORT),' \
	  '  "host": "127.0.0.1",' \
	  '  "chains": {' \
	  '    "eip155:31337": {' \
	  '      "eip1559": true,' \
	  '      "signers": ["$(FACILITATOR_KEY)"],' \
	  '      "rpc": [{"http": "http://127.0.0.1:$(ANVIL_PORT)"}]' \
	  '    }' \
	  '  },' \
	  '  "schemes": [{"id": "v2-eip155-exact", "chains": "eip155:31337"}]' \
	  '}' > $(FACILITATOR_DIR)/config.json
	@docker rm -f $(FACILITATOR_CONTAINER) >/dev/null 2>&1 || true
	@docker run -d --name $(FACILITATOR_CONTAINER) --network host \
	  -v "$(CURDIR)/$(FACILITATOR_DIR)/config.json:/app/config.json:ro" \
	  $(FACILITATOR_IMAGE) >/dev/null
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	  if curl -sf -m 2 -o /dev/null http://127.0.0.1:$(FACILITATOR_PORT)/supported; then \
	    echo ">> facilitator started on :$(FACILITATOR_PORT)"; exit 0; fi; \
	  sleep 1; \
	done; \
	echo "error: facilitator did not start:"; docker logs $(FACILITATOR_CONTAINER) 2>&1 | tail -5; \
	docker rm -f $(FACILITATOR_CONTAINER) >/dev/null; exit 1

facilitator-stop:
	@if docker rm -f $(FACILITATOR_CONTAINER) >/dev/null 2>&1; then \
	  echo ">> facilitator stopped"; else echo ">> facilitator not running"; fi

# ---- bundle-<arch>: Docker image with stripped musl Python + teeswap ----
bundle-%:
	@test -f dist/$*/vaportpm-attest || { echo "error: dist/$*/vaportpm-attest not found"; exit 1; }
	@[ "$$(sha256sum dist/$*/vaportpm-attest | cut -d' ' -f1)" = "$(VAPORTPM_SHA256_$*)" ] || \
	  { echo "error: dist/$*/vaportpm-attest hash mismatch (expected $(VAPORTPM_SHA256_$*))"; exit 1; }
	docker build -f Dockerfile.bundle --build-arg PYVER=$(PYVER) -t teeswap:bundle-$* .
	@echo ">> image built: teeswap:bundle-$*"

# ---- payload-<arch>: lockboot stage2 binary ----
payload-%: bundle-%
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
boot-%: 
	@test -f dist/$*/stage2      || { echo "error: dist/$*/stage2 not found — run 'make payload-$*'"; exit 1; }
	@test -f dist/$*/boot.disk   || { echo "error: dist/$*/boot.disk not found — copy from lockboot stage0 build"; exit 1; }
	@test -f dist/$*/linux.efi   || { echo "error: dist/$*/linux.efi not found — copy from lockboot stage1 build"; exit 1; }
	@test -f dist/$*/efi-vars.ovmf || { echo "error: dist/$*/efi-vars.ovmf not found — copy from lockboot stage0 build"; exit 1; }
	@[ "$$(sha256sum dist/$*/boot.disk | cut -d' ' -f1)" = "$(BOOT_DISK_SHA256_$*)" ] || \
	  { echo "error: dist/$*/boot.disk hash mismatch"; exit 1; }
	@[ "$$(sha256sum dist/$*/linux.efi | cut -d' ' -f1)" = "$(UKI_SHA256_$*)" ] || \
	  { echo "error: dist/$*/linux.efi hash mismatch"; exit 1; }
	@[ "$$(sha256sum dist/$*/efi-vars.ovmf | cut -d' ' -f1)" = "$(EFI_VARS_SHA256_$*)" ] || \
	  { echo "error: dist/$*/efi-vars.ovmf hash mismatch"; exit 1; }
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
	  $(foreach p,$(FORWARD_PORTS),-p $(p):$(p)) \
	  -v "$(CURDIR)/dist/$*/boot:/boot" \
	  lockboot:harness --kind stage0 --arch $* \
	    --boot-disk /boot/boot.disk \
	    --serve-dir /boot \
	    --user-data /boot/user-data.json \
	    $(foreach p,$(FORWARD_PORTS),--forward $(p))

clean:
	rm -rf "$(VENV)" "$(TOOLS)/cache" .ruff_cache

distclean: clean
	rm -rf "$(TOOLS)" dist
