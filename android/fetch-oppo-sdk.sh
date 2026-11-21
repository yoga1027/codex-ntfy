#!/bin/sh
set -eu

cd "$(dirname "$0")"
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT
curl --fail --location --silent --show-error --retry 2 \
    'https://openfs.oppomobile.com/open/oop/202405/17/017e03d5ae79b4d04ac63d2a9a0941bd.zip' \
    --output "$archive"
printf '%s  %s\n' '60fbbf9a2a9a592cab29ec60f60a901df35d1f0cbc39f85232aba138956da33a' "$archive" |
    sha256sum --check
mkdir -p app/libs
unzip -p "$archive" com.heytap.msp_3.5.2-release-20240516-416001043.aar > app/libs/opush-3.5.2.aar
