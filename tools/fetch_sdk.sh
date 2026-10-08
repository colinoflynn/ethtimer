#!/bin/bash
# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
#
# Fetch the ST sources the firmware needs into firmware/vendor/.
#
#   tools/fetch_sdk.sh            # everything every board needs
#   tools/fetch_sdk.sh f429       # just what one board needs
#   tools/fetch_sdk.sh f429 h723
#   tools/fetch_sdk.sh --force    # re-fetch, discarding what is there
#
# WHY NOT A SUBMODULE, AND WHY NOT A VENDORED COPY.
#
# A submodule of STM32CubeF4 is a multi-gigabyte clone to get a few megabytes of
# HAL, and three of them is a repository nobody clones twice. Copying the
# sources in instead would put several hundred thousand lines of someone else's
# code inside this repository, which is both a licensing question and an
# unreviewable diff.
#
# So: ST publishes each component as its own small repository, and this fetches
# those at PINNED tags. Pinned, not floating, because the firmware is a thing
# that gets flashed and measured -- a build that silently picks up a new HAL ETH
# driver is a build whose numbers cannot be compared with yesterday's. Bumping a
# pin is a commit, with a rebuild and a run behind it.
#
# The directory layout is not arbitrary: each component repository's own root
# matches the subtree the monolithic Cube package puts it under, which is what
# lets firmware/Makefile accept either with one set of variables (SDK=vendor or
# SDK=cube).
#
# WHAT THIS DOES NOT DO. It does not install a toolchain (arm-none-eabi-gcc is
# yours to provide), and it does not verify anything beyond what git does for a
# tag. It does not silently replace a component sitting at the wrong pin either:
# that is reported and refused, because a tree that builds from unknown sources
# is worse than one that does not build.
#
# The pin and per-board tables below are read through indirect expansion
# (${!var}), which ShellCheck cannot follow -- it reports every entry as unused.
# Disabled for the file rather than per line because the whole script is built
# that way, and because the alternative, associative arrays, needs bash 4 and
# would be the only thing in here that does.
# shellcheck disable=SC2034

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
VENDOR="$ROOT/firmware/vendor"

ORG="https://github.com/STMicroelectronics"

# ---------------------------------------------------------------- the pins ---
# Shell variable names cannot hold '-' or '.', so a component's pin lives in
# PIN_<name, with - and . turned into _>.
PIN_cmsis_core="v5.9.0_20250520"
PIN_stm32_mw_lwip="v2.1.2_20230828"
PIN_stm32_lan8742="v1.0.3"

PIN_cmsis_device_f4="v2.6.9"
PIN_stm32f4xx_hal_driver="v1.8.4"

PIN_cmsis_device_f7="v1.2.9"
PIN_stm32f7xx_hal_driver="v1.3.2"

PIN_cmsis_device_h7="v1.10.6"
PIN_stm32h7xx_hal_driver="v1.11.5"

# Shared by every board: the Cortex-M headers, the TCP/IP stack, the PHY driver.
COMMON=(cmsis-core stm32-mw-lwip stm32-lan8742)

# Per-board: the device headers and the HAL for that family. A board added under
# firmware/boards/ needs a NEED_<board> line here and nothing else.
NEED_f429=(cmsis-device-f4 stm32f4xx-hal-driver)
NEED_f746=(cmsis-device-f7 stm32f7xx-hal-driver)
NEED_h723=(cmsis-device-h7 stm32h7xx-hal-driver)

ALL_BOARDS=(f429 f746 h723)

usage() {
    sed -n '5,35p' "$0" | sed 's/^#\{1,\} \{0,1\}//'
}

force=0
boards=()
for arg in "$@"; do
    case "$arg" in
        --force) force=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown option $arg" >&2; usage >&2; exit 2 ;;
        *)  boards+=("$arg") ;;
    esac
done
if [ "${#boards[@]}" -eq 0 ]; then
    boards=("${ALL_BOARDS[@]}")
fi

want=("${COMMON[@]}")
for b in "${boards[@]}"; do
    need_var="NEED_${b}[@]"
    # ${!x+set} on an array element list is empty when the array does not
    # exist, which is how an unknown board is named here rather than two
    # screens later as a component with no pin.
    if [ -z "${!need_var+set}" ]; then
        echo "unknown board '$b'. Known: ${ALL_BOARDS[*]}" >&2
        echo "A new board needs a NEED_<board> line in this script." >&2
        exit 2
    fi
    want+=("${!need_var}")
done

# Deduplicate: two boards of one family share a HAL, and asking for both must
# not clone it twice. A read loop rather than `mapfile`, which is bash 4.
comps=()
while IFS= read -r line; do
    comps+=("$line")
done < <(printf '%s\n' "${want[@]}" | sort -u)

mkdir -p "$VENDOR"
for comp in "${comps[@]}"; do
    pin_var="PIN_${comp//[-.]/_}"
    tag="${!pin_var:-}"
    if [ -z "$tag" ]; then
        echo "no pin for component '$comp' -- add a ${pin_var} line" >&2
        exit 2
    fi
    dest="$VENDOR/$comp"
    if [ -d "$dest" ] && [ "$force" = 1 ]; then
        echo "remove $dest"
        rm -rf "$dest"
    fi
    if [ -d "$dest" ]; then
        have="$(git -C "$dest" describe --tags --exact-match 2>/dev/null || echo '?')"
        if [ "$have" = "$tag" ]; then
            echo "have  $comp $tag"
            continue
        fi
        # A directory at the wrong pin is the one case worth being loud about:
        # it builds, and it builds something other than what the pins say.
        echo "WRONG PIN: $dest is at '$have', pinned '$tag'." >&2
        echo "Re-run with --force to replace it." >&2
        exit 1
    fi
    echo "fetch $comp $tag"
    # --depth 1 on the tag: these repositories carry a full vendor history and
    # none of it is needed to compile. advice.detachedHead off because a tag
    # checkout is the intent here, not an accident.
    git -c advice.detachedHead=false \
        clone -q --depth 1 --branch "$tag" "$ORG/$comp" "$dest"
done

echo
echo "vendor tree ready in firmware/vendor:"
for d in "$VENDOR"/*/; do
    [ -d "$d" ] || continue
    printf '  %-26s %s\n' "$(basename "$d")" \
        "$(git -C "$d" describe --tags --exact-match 2>/dev/null || echo '?')"
done
