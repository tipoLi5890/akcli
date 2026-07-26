#!/usr/bin/env bash
# CI-green gate for the release workflow (.github/workflows/release.yml).
#
# Answers one question about the tagged commit: "did the CI workflow run on
# this exact SHA, and did it conclude success?" — by asking the *workflow runs*
# API directly instead of aggregating check runs on the commit.
#
# Why not check-runs: this gate is ITSELF a check run on the tagged SHA, so a
# commit CI never saw still has >= 1 check run and a naive "no check runs at
# all" probe can never fire. Querying
# `actions/workflows/<file>/runs?head_sha=<sha>` separates the two states that
# matter and that check-runs conflates:
#
#   * NEVER RAN  — zero workflow runs for the SHA (the historical failure:
#                  tag pushed before/with the commit, matrix never saw it)
#   * RAN, RED   — a run exists and concluded != success
#
# Both are grace-polled first (a tag pushed together with its commit legitimately
# races its own CI), then failed on timeout — so the race is *waited out*, never
# insta-killed and never waved through.
#
# On top of the run-level conclusion, a required-job whitelist asserts the
# windows-latest matrix legs individually: every historically red release
# (0.4.0, 0.8.0, 0.9.0, 0.10.0) was windows-only, so a silently shrunk or
# renamed matrix must fail the gate rather than quietly weaken it.
# `tests/test_release_gate.py` keeps REQUIRED_JOBS in lock-step with ci.yml's
# matrix and exercises the three scenarios below with a mocked API.
#
# Inputs (env):
#   REPO           owner/name              (required)
#   SHA            tagged commit sha       (required)
#   WORKFLOW_FILE  workflow to require     (default: ci.yml)
#   POLL_INTERVAL  seconds between polls   (default: 30)
#   POLL_TIMEOUT   total grace seconds     (default: 1800)
#   REQUIRED_JOBS  newline-separated job names (default: the windows matrix)
#   GH_TOKEN       consumed by `gh api`
#
# Exit: 0 = green, 1 = not green / never ran / timed out.
set -euo pipefail

WORKFLOW_FILE="${WORKFLOW_FILE:-ci.yml}"
POLL_INTERVAL="${POLL_INTERVAL:-30}"
POLL_TIMEOUT="${POLL_TIMEOUT:-1800}"

# Required-job whitelist. Derived from ci.yml's `test` matrix (job `name:` is
# "test (${{ matrix.os }} / py${{ matrix.python-version }})"); the windows legs
# are the ones that historically caught release breakers. Kept in lock-step
# with ci.yml by tests/test_release_gate.py.
REQUIRED_JOBS_DEFAULT='test (windows-latest / py3.11)
test (windows-latest / py3.12)
test (windows-latest / py3.13)
test (windows-latest / py3.14)'
REQUIRED_JOBS="${REQUIRED_JOBS:-$REQUIRED_JOBS_DEFAULT}"

log() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }

# --------------------------------------------------------------------------- #
# GitHub API access. These two functions are the only `gh` calls; the bash
# unit tests override them to replay canned scenarios.
# --------------------------------------------------------------------------- #

# TSV: id, status, conclusion, created_at — newest run first is NOT assumed.
gh_runs() {
  gh api --paginate \
    "repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/runs?head_sha=${SHA}&per_page=100" \
    --jq '.workflow_runs[] | [(.id|tostring), .status, (.conclusion // "none"), .created_at] | @tsv'
}

# TSV: name, status, conclusion — for one run id.
gh_jobs() {
  gh api --paginate \
    "repos/${REPO}/actions/runs/$1/jobs?per_page=100" \
    --jq '.jobs[] | [.name, .status, (.conclusion // "none")] | @tsv'
}

# --------------------------------------------------------------------------- #

ci_green_gate() {
  : "${REPO:?REPO must be set}"
  : "${SHA:?SHA must be set}"

  local waited=0 runs run run_id status conclusion jobs

  while :; do
    runs="$(gh_runs || true)"
    if [ -n "$runs" ]; then
      # newest by created_at, id as tiebreaker
      run="$(printf '%s\n' "$runs" | sort -t"$(printf '\t')" -k4,4r -k1,1nr | head -n1)"
      run_id="$(printf '%s' "$run" | cut -f1)"
      status="$(printf '%s' "$run" | cut -f2)"
      conclusion="$(printf '%s' "$run" | cut -f3)"
    else
      run_id=""
      status="never-ran"
      conclusion="none"
    fi

    [ "$status" = "completed" ] && break

    if [ "$waited" -ge "$POLL_TIMEOUT" ]; then
      if [ -z "$run_id" ]; then
        err "::error::No ${WORKFLOW_FILE} run exists for ${SHA} after ${waited}s — CI never ran this commit. Push the commit to main, wait for green, then re-tag."
      else
        err "::error::${WORKFLOW_FILE} run ${run_id} for ${SHA} is still '${status}' after ${waited}s. Wait for it to finish, then re-run this workflow."
      fi
      return 1
    fi

    if [ -z "$run_id" ]; then
      log "no ${WORKFLOW_FILE} run for ${SHA} yet (waited ${waited}s/${POLL_TIMEOUT}s) — the tag may have been pushed with its commit; waiting..."
    else
      log "${WORKFLOW_FILE} run ${run_id} is '${status}' (waited ${waited}s/${POLL_TIMEOUT}s) — waiting..."
    fi
    sleep "$POLL_INTERVAL"
    waited=$(( waited + POLL_INTERVAL ))
  done

  jobs="$(gh_jobs "$run_id" || true)"

  if [ "$conclusion" != "success" ]; then
    err "::error::${WORKFLOW_FILE} run ${run_id} concluded '${conclusion}' on ${SHA} — refusing to release."
    printf '%s\n' "$jobs" | awk -F"$(printf '\t')" \
      'NF && $3 != "success" && $3 != "skipped" && $3 != "neutral" { print "  not green: " $1 " (" $3 ")" }' >&2
    err "Fix the failures on main, wait for green, then re-tag (or re-run this workflow with force=true to bypass)."
    return 1
  fi

  # Run-level success is not enough: assert the required matrix legs by name.
  local missing=0 want line got
  while IFS= read -r want; do
    [ -n "$want" ] || continue
    line="$(printf '%s\n' "$jobs" | awk -F"$(printf '\t')" -v n="$want" '$1 == n { print; exit }')"
    if [ -z "$line" ]; then
      err "::error::required job absent from ${WORKFLOW_FILE} run ${run_id}: '${want}'"
      missing=1
      continue
    fi
    got="$(printf '%s' "$line" | cut -f3)"
    if [ "$got" != "success" ]; then
      err "::error::required job not green in run ${run_id}: '${want}' (${got})"
      missing=1
    fi
  done <<EOF
$REQUIRED_JOBS
EOF

  if [ "$missing" -ne 0 ]; then
    err "The required-job whitelist pins the windows-latest matrix — every historically red release (0.4.0, 0.8.0, 0.9.0, 0.10.0) failed there and only there. A renamed or shrunk matrix fails this gate on purpose: update REQUIRED_JOBS in .github/scripts/ci_green_gate.sh together with ci.yml."
    return 1
  fi

  log "CI green on ${SHA}: ${WORKFLOW_FILE} run ${run_id} concluded success, all required jobs present and successful."
  return 0
}

# Sourced by the tests (which override gh_runs/gh_jobs) — only self-execute.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  ci_green_gate
fi
