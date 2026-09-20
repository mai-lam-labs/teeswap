# developer toolchain — Astral uv + ruff (lint/format) + ty + pyrefly (typecheck).
#
# FULLY SELF-CONTAINED: uv installs into ./.uv (not ~/.local/bin), its cache is
# ./.uv/cache, and the venv is ./.venv — nothing touches the system.
# The venv is VSCode-compatible (.vscode/settings.json points at .venv/bin/python).
#
#   make install     install a project-local uv + create .venv + install tools
#   make lint        ruff check
#   make format      ruff format (writes)
#   make check       lint + format-check + typecheck + test  (CI-style, no writes)
#   make clean       remove .venv + caches   |   make distclean   also removes .uv

SRC := teeswap

TOOLS := $(CURDIR)/.uv
UV    := $(TOOLS)/uv
VENV  := $(CURDIR)/.venv
PY    := $(VENV)/bin/python
RUFF    := $(VENV)/bin/ruff
TY      := $(VENV)/bin/ty
PYREFLY := $(VENV)/bin/pyrefly

# Keep uv's cache inside the project too, so `make` creates nothing under $HOME.
export UV_CACHE_DIR := $(TOOLS)/cache

.PHONY: help install uv-bootstrap lint format format-check typecheck typecheck-strict test coverage check build clean distclean

help:
	@echo "targets: install | lint | format | format-check | typecheck | test | coverage | check | clean | distclean"

# Install uv as a standalone binary INTO THE PROJECT (./.uv), never touching
# ~/.local/bin or shell profiles (UV_NO_MODIFY_PATH=1). No-op if already present.
uv-bootstrap:
	@test -x "$(UV)" || { \
	  echo ">> installing project-local uv into $(TOOLS)"; \
	  mkdir -p "$(TOOLS)"; \
	  curl -LsSf https://astral.sh/uv/install.sh \
	    | env UV_UNMANAGED_INSTALL="$(TOOLS)" UV_NO_MODIFY_PATH=1 sh; }

install: uv-bootstrap
	"$(UV)" venv "$(VENV)" --python 3.14 --clear
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

# CI-style gate: no writes, fails on any issue.
check: lint format-check typecheck test

build:
	"$(UV)" build --python "$(PY)" --out-dir dist

clean:
	rm -rf "$(VENV)" "$(TOOLS)/cache" .ruff_cache

distclean: clean
	rm -rf "$(TOOLS)"
