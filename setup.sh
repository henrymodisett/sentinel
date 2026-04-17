#!/usr/bin/env bash
#
# setup.sh — one-command project setup.
#
# Run this after cloning the repo:
#   bash setup.sh
#
# Installs all dev tools, syncs touchstone files, sets up hooks, and installs
# project dependencies. Idempotent — safe to re-run anytime.
#
set -euo pipefail

# Colors.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { printf "${BOLD}==> %s${RESET}\n" "$*"; }
ok()    { printf "  ${GREEN}✓${RESET} %s\n" "$*"; }
warn()  { printf "  ${YELLOW}!${RESET} %s\n" "$*"; }
fail()  { printf "  ${RED}✗${RESET} %s\n" "$*"; }

DEPS_ONLY=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --deps-only) DEPS_ONLY=true; shift ;;
    -h|--help)
      echo "Usage: bash setup.sh [--deps-only]"
      exit 0
      ;;
    *) fail "Unknown argument: $1"; exit 1 ;;
  esac
done

PROJECT_NAME="$(basename "$(pwd)")"
echo ""
printf "${BOLD}Setting up ${PROJECT_NAME}${RESET}\n"
echo ""

if [ "$DEPS_ONLY" = false ]; then

# --------------------------------------------------------------------------
# 1. Homebrew (required foundation)
# --------------------------------------------------------------------------
info "Checking Homebrew"
if command -v brew >/dev/null 2>&1; then
  ok "brew installed"
else
  fail "Homebrew is required. Install from https://brew.sh"
  exit 1
fi

# --------------------------------------------------------------------------
# 2. Touchstone CLI
# --------------------------------------------------------------------------
info "Checking touchstone"
if command -v touchstone >/dev/null 2>&1; then
  TOUCHSTONE_VERSION_SUMMARY="$(touchstone version 2>&1 | awk 'NF { sub(/^touchstone /, ""); print; exit }')"
  ok "touchstone ${TOUCHSTONE_VERSION_SUMMARY:-installed}"
else
  warn "Installing touchstone..."
  brew tap autumngarage/touchstone 2>/dev/null || true
  brew install touchstone
  ok "touchstone installed"
fi

# --------------------------------------------------------------------------
# 3. Dev tools (brew)
# --------------------------------------------------------------------------
info "Checking dev tools"

brew_install_if_missing() {
  local cmd="$1"
  local formula="${2:-$1}"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd installed"
  else
    warn "Installing $formula..."
    brew install "$formula" 2>/dev/null
    ok "$cmd installed"
  fi
}

brew_install_if_missing "git"        "git"
brew_install_if_missing "gh"         "gh"
brew_install_if_missing "pre-commit" "pre-commit"
brew_install_if_missing "gitleaks"   "gitleaks"
brew_install_if_missing "shellcheck" "shellcheck"
brew_install_if_missing "shfmt"      "shfmt"

# --------------------------------------------------------------------------
# 4. Codex CLI (npm, optional but recommended)
# --------------------------------------------------------------------------
info "Checking Codex CLI"
if command -v codex >/dev/null 2>&1; then
  ok "codex installed"
elif command -v npm >/dev/null 2>&1; then
  warn "Installing codex CLI..."
  npm install -g @openai/codex 2>/dev/null && ok "codex installed" || warn "codex install failed (optional — install manually: npm install -g @openai/codex)"
else
  warn "codex not installed (requires npm). Install Node.js first, then: npm install -g @openai/codex"
fi

# --------------------------------------------------------------------------
# 5. Sync touchstone files to latest
# --------------------------------------------------------------------------
info "Syncing touchstone files"
# Skip update if this IS the Touchstone repo (it's the source, not a downstream project).
if [ -f "bin/touchstone" ] && [ -f "lib/auto-update.sh" ]; then
  ok "this is the Touchstone repo — skipping self-update"
elif [ -f ".touchstone-version" ]; then
  touchstone update 2>&1 | grep -E "added|updated|Already" | head -5 | while read -r line; do
    ok "$line"
  done
  ok "touchstone files up to date"
else
  warn "No .touchstone-version found — this project hasn't been bootstrapped."
  warn "Run: touchstone new $(pwd)"
fi

# --------------------------------------------------------------------------
# 6. Pre-commit hooks
# --------------------------------------------------------------------------
info "Setting up git hooks"
if [ -f ".pre-commit-config.yaml" ]; then
  # Clear core.hooksPath if set — it conflicts with pre-commit.
  git config --unset-all core.hooksPath 2>/dev/null || true
  # Install hook shims (environments install lazily on first run).
  pre-commit install 2>&1 | tail -1 | while read -r line; do ok "$line"; done
  pre-commit install --hook-type pre-push 2>&1 | tail -1 | while read -r line; do ok "$line"; done
  pre-commit install --hook-type commit-msg 2>&1 | tail -1 | while read -r line; do ok "$line"; done
  ok "pre-commit hooks installed (pre-commit, pre-push, commit-msg)"
else
  warn "No .pre-commit-config.yaml found — skipping hooks"
fi

# --------------------------------------------------------------------------
# 7. gh CLI auth check
# --------------------------------------------------------------------------
info "Checking GitHub auth"
if gh auth status 2>&1 | grep -q "Logged in"; then
  ok "gh authenticated"
else
  warn "gh not authenticated. Run: gh auth login"
fi

fi

# --------------------------------------------------------------------------
# 8. Project dependencies
# --------------------------------------------------------------------------
info "Installing project dependencies"

select_python_for_venv() {
  local python_dir="${1:-.}"
  local pyenv_python

  if [ -n "${PYTHON:-}" ]; then
    if command -v "$PYTHON" >/dev/null 2>&1; then
      command -v "$PYTHON"
      return 0
    fi
    fail "PYTHON is set but not executable: $PYTHON"
    return 1
  fi

  if command -v pyenv >/dev/null 2>&1; then
    if [ -f "$python_dir/.python-version" ] || [ -f ".python-version" ]; then
      pyenv_python="$(cd "$python_dir" && pyenv which python 2>/dev/null || true)"
      if [ -n "$pyenv_python" ] && [ -x "$pyenv_python" ]; then
        printf '%s\n' "$pyenv_python"
        return 0
      fi
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  fail "Python is required to create a virtualenv"
  return 1
}

install_python_requirements() {
  local label="$1"
  local requirements_file="$2"
  local venv_dir="$3"
  local python_bin python_dir python_version
  python_dir="$(dirname "$requirements_file")"

  if [ ! -x "$venv_dir/bin/python" ]; then
    python_bin="$(select_python_for_venv "$python_dir")"
    python_version="$("$python_bin" --version 2>&1)"
    if [ ! -f "$python_dir/.python-version" ] && [ ! -f ".python-version" ]; then
      warn "No .python-version found — creating $venv_dir with $python_version"
    else
      ok "Using $python_version for $venv_dir"
    fi
    "$python_bin" -m venv "$venv_dir"
    ok "$venv_dir created"
  fi

  "$venv_dir/bin/python" -m pip install -r "$requirements_file" 2>&1 | tail -1 | while read -r line; do
    ok "$label dependencies installed: $line"
  done
}

install_uv_if_missing() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi

  if command -v brew >/dev/null 2>&1; then
    warn "Installing uv..."
    brew install uv 2>/dev/null
    ok "uv installed"
    return 0
  fi

  warn "uv is required for uv.lock/pyproject.toml projects. Install uv first."
  return 1
}

install_uv_project() {
  local label="$1"
  local project_dir="$2"

  if install_uv_if_missing; then
    (cd "$project_dir" && uv sync) 2>&1 | tail -1 | while read -r line; do
      ok "$label dependencies synced: $line"
    done
  fi
}

CONFIG_PROJECT_TYPE=""
CONFIG_PACKAGE_MANAGER=""
CONFIG_TARGETS=""

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

load_touchstone_config() {
  local line key value

  [ -f ".touchstone-config" ] || return 0

  while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim "$line")"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac

    key="$(trim "${line%%=*}")"
    value="$(trim "${line#*=}")"
    case "$key" in
      project_type|profile) CONFIG_PROJECT_TYPE="$value" ;;
      package_manager) CONFIG_PACKAGE_MANAGER="$value" ;;
      targets) CONFIG_TARGETS="$value" ;;
    esac
  done < ".touchstone-config"
}

detect_node_package_manager() {
  local dir="${1:-.}" package_manager

  if [ -f "$dir/package.json" ]; then
    package_manager="$(sed -n 's/.*"packageManager"[[:space:]]*:[[:space:]]*"\([^@"]*\)@.*/\1/p' "$dir/package.json" | head -1)"
    if [ -z "$package_manager" ]; then
      package_manager="$(sed -n 's/.*"packageManager"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$dir/package.json" | head -1)"
    fi
    if [ -n "$package_manager" ]; then
      printf '%s\n' "$package_manager"
      return 0
    fi
  fi

  if [ -f "$dir/pnpm-lock.yaml" ] || [ -f "$dir/pnpm-workspace.yaml" ]; then
    printf 'pnpm\n'
  elif [ -f "$dir/yarn.lock" ]; then
    printf 'yarn\n'
  elif [ -f "$dir/bun.lock" ] || [ -f "$dir/bun.lockb" ]; then
    printf 'bun\n'
  else
    printf 'npm\n'
  fi
}

detect_profile() {
  local dir="${1:-.}"

  if [ -f "$dir/pnpm-workspace.yaml" ]; then
    printf 'node\n'
  elif [ -f "$dir/package.json" ] || [ -f "$dir/tsconfig.json" ]; then
    printf 'node\n'
  elif [ -f "$dir/Cargo.toml" ]; then
    printf 'rust\n'
  elif [ -f "$dir/Package.swift" ]; then
    printf 'swift\n'
  elif [ -f "$dir/go.mod" ]; then
    printf 'go\n'
  elif [ -f "$dir/uv.lock" ] || [ -f "$dir/pyproject.toml" ] || [ -f "$dir/requirements.txt" ]; then
    printf 'python\n'
  else
    printf 'generic\n'
  fi
}

install_node_dependencies() {
  local label="$1"
  local project_dir="$2"
  local package_manager="$3"

  [ -f "$project_dir/package.json" ] || [ -f "$project_dir/pnpm-workspace.yaml" ] || return 1

  if [ "$package_manager" = "auto" ] || [ -z "$package_manager" ]; then
    package_manager="$(detect_node_package_manager "$project_dir")"
  fi

  if { [ "$package_manager" = "pnpm" ] || [ "$package_manager" = "yarn" ]; } && command -v corepack >/dev/null 2>&1; then
    corepack enable 2>/dev/null || true
  fi

  case "$package_manager" in
    pnpm)
      if command -v pnpm >/dev/null 2>&1; then
        (cd "$project_dir" && pnpm install) 2>&1 | tail -1 | while read -r line; do ok "$label dependencies installed: $line"; done
      else
        warn "$label uses pnpm, but pnpm is not installed. Run: corepack enable or brew install pnpm"
      fi
      ;;
    yarn)
      if command -v yarn >/dev/null 2>&1; then
        (cd "$project_dir" && yarn install) 2>&1 | tail -1 | while read -r line; do ok "$label dependencies installed: $line"; done
      else
        warn "$label uses yarn, but yarn is not installed. Run: corepack enable"
      fi
      ;;
    bun)
      if command -v bun >/dev/null 2>&1; then
        (cd "$project_dir" && bun install) 2>&1 | tail -1 | while read -r line; do ok "$label dependencies installed: $line"; done
      else
        warn "$label uses bun, but bun is not installed."
      fi
      ;;
    npm|*)
      if command -v npm >/dev/null 2>&1; then
        (cd "$project_dir" && npm install) 2>&1 | tail -1 | while read -r line; do ok "$label dependencies installed: $line"; done
      else
        warn "$label uses npm, but npm is not installed. Install Node.js/npm first."
      fi
      ;;
  esac

  return 0
}

install_python_dependencies() {
  local label="$1"
  local project_dir="$2"

  if [ -f "$project_dir/uv.lock" ]; then
    install_uv_project "$label" "$project_dir"
  elif [ -f "$project_dir/pyproject.toml" ] && [ ! -f "$project_dir/requirements.txt" ]; then
    install_uv_project "$label" "$project_dir"
  elif [ -f "$project_dir/requirements.txt" ]; then
    if [ "$project_dir" = "." ]; then
      install_python_requirements "$label" "$project_dir/requirements.txt" ".venv"
    else
      install_python_requirements "$label" "$project_dir/requirements.txt" "$project_dir/.venv"
    fi
  else
    return 1
  fi

  return 0
}

install_rust_dependencies() {
  local label="$1"
  local project_dir="$2"

  [ -f "$project_dir/Cargo.toml" ] || return 1
  if command -v cargo >/dev/null 2>&1; then
    (cd "$project_dir" && cargo fetch) 2>&1 | tail -1 | while read -r line; do ok "$label crates fetched: $line"; done
  else
    warn "$label is Rust, but cargo is not installed."
  fi
  return 0
}

install_swift_dependencies() {
  local label="$1"
  local project_dir="$2"

  [ -f "$project_dir/Package.swift" ] || return 1
  if command -v swift >/dev/null 2>&1; then
    (cd "$project_dir" && swift package resolve) 2>&1 | tail -1 | while read -r line; do ok "$label packages resolved: $line"; done
  else
    warn "$label is Swift, but swift is not installed."
  fi
  return 0
}

install_go_dependencies() {
  local label="$1"
  local project_dir="$2"

  [ -f "$project_dir/go.mod" ] || return 1
  if command -v go >/dev/null 2>&1; then
    (cd "$project_dir" && go mod download) 2>&1 && ok "$label modules downloaded"
  else
    warn "$label is Go, but go is not installed."
  fi
  return 0
}

install_profile_dependencies() {
  local label="$1"
  local project_dir="$2"
  local profile="$3"

  if [ "$profile" = "auto" ] || [ -z "$profile" ]; then
    profile="$(detect_profile "$project_dir")"
  fi

  case "$profile" in
    node|typescript|ts) install_node_dependencies "$label" "$project_dir" "$CONFIG_PACKAGE_MANAGER" ;;
    python) install_python_dependencies "$label" "$project_dir" ;;
    rust) install_rust_dependencies "$label" "$project_dir" ;;
    swift) install_swift_dependencies "$label" "$project_dir" ;;
    go) install_go_dependencies "$label" "$project_dir" ;;
    generic|"") return 1 ;;
    *) warn "Unknown project_type '$profile' in .touchstone-config"; return 1 ;;
  esac
}

install_configured_targets() {
  local entry name path profile
  local -a target_entries=()

  [ -n "$CONFIG_TARGETS" ] || return 1

  IFS=',' read -r -a target_entries <<< "$CONFIG_TARGETS"
  for entry in "${target_entries[@]}"; do
    entry="$(trim "$entry")"
    [ -z "$entry" ] && continue
    name="${entry%%:*}"
    path="${entry#*:}"
    profile="${path#*:}"
    path="${path%%:*}"
    if [ "$path" = "$profile" ]; then
      profile="auto"
    fi
    if [ ! -d "$path" ]; then
      warn "target '$name' path not found: $path"
      continue
    fi
    install_profile_dependencies "$name" "$path" "$profile" || true
  done
}

load_touchstone_config

DEPS_FOUND=false
ROOT_PROFILE="${CONFIG_PROJECT_TYPE:-auto}"
if [ "$ROOT_PROFILE" = "generic" ] && [ "$(detect_profile ".")" != "generic" ]; then
  ROOT_PROFILE="$(detect_profile ".")"
fi
if install_profile_dependencies "Project" "." "$ROOT_PROFILE"; then
  DEPS_FOUND=true
fi

# Backward-compatible nested Python agent support.
if install_python_dependencies "agent Python" "agent"; then
  DEPS_FOUND=true
fi

if [ "$DEPS_FOUND" = false ] && install_configured_targets; then
  DEPS_FOUND=true
fi

if [ "$DEPS_FOUND" = false ]; then
  ok "No recognized dependency file — skipping"
fi

# --------------------------------------------------------------------------
# 9. Summary
# --------------------------------------------------------------------------
echo ""
info "Setup complete"
echo ""
printf "  Run ${BOLD}touchstone doctor${RESET} to verify everything.\n"
printf "  Run ${BOLD}touchstone status${RESET} to see project health.\n"
echo ""
