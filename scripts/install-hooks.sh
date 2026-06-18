#!/usr/bin/env bash
# Install the repo's versioned git hooks (lives in .githooks/) by pointing
# git at that directory via core.hooksPath. One-time setup after clone.
#
# Why core.hooksPath: hooks inside .git/hooks/ are not version-controlled and
# don't travel with clones. .githooks/ is versioned, so everyone gets the same
# hooks on `git pull`.

set -euo pipefail

# Run from the repo root regardless of where the script is invoked from.
cd "$(git rev-parse --show-toplevel)"

# Make all hooks executable.
chmod +x .githooks/*

# Point git at the versioned hooks directory.
git config core.hooksPath .githooks

echo "✅ Git hooks installed (core.hooksPath = .githooks)."
echo "   pre-push gate: ruff check, ruff format --check, pyright, pytest."
echo "   Bypass a push with 'git push --no-verify' (only when CI will run)."
