#!/bin/sh

# Compatibility entry point for early user-space deployment drafts.
# The implementation and documentation live in the single heteroctl command.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
exec "${SCRIPT_DIR}/heteroctl" "$@"
