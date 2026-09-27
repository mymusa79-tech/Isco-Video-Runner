#!/usr/bin/env bash
set -euo pipefail

# Cairo is pinned to the exact upstream commit recorded by Google Fonts.
# We install only the variable TTF locally, then ask fontconfig/libass for
# the Bold instance (weight 700). No paid service and no provider call.
CAIRO_COMMIT="73d16933c6a0f341c27a69e401da83dcb0d53114"
CAIRO_URL="https://raw.githubusercontent.com/Gue3bara/Cairo/${CAIRO_COMMIT}/fonts/Cairo/variable/Cairo%5Bslnt%2Cwght%5D.ttf"
FONT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/fonts/isco-cairo"
FONT_PATH="${FONT_DIR}/Cairo.ttf"

if ! fc-list : family | grep -Eiq '(^|,| )Cairo(,|$)'; then
  mkdir -p "$FONT_DIR"
  temporary="${FONT_PATH}.tmp"
  rm -f "$temporary"
  curl --fail --location --silent --show-error     --retry 3 --retry-delay 2 --connect-timeout 20     "$CAIRO_URL" -o "$temporary"
  test -s "$temporary"
  test "$(stat -c %s "$temporary")" -gt 500000
  mv "$temporary" "$FONT_PATH"
  fc-cache -f "$FONT_DIR" >/dev/null
fi

family="$(fc-match -f '%{family}
' 'Cairo:weight=bold' | head -n1)"
test "$family" = "Cairo"
echo "Cairo Bold font ready for libass/fontconfig."
