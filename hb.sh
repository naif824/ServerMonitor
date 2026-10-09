#!/usr/bin/env bash
# Heartbeat: record that a cron job SUCCEEDED (dead-man's-switch).
# Called as the last step of a job:  job.sh && hb.sh <name>
# The inventory generator flags any heartbeat older than its max age.
set -u
name="${1:?usage: hb.sh <name>}"
d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/state/hb"
mkdir -p "$d"
date +%s > "$d/$name"
