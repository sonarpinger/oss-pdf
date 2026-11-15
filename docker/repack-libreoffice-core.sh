#!/bin/sh
set -eu

# Ubuntu bundles the Poppler-linked PDF import executable with Writer's core.
# DOCX-to-PDF only needs the export path. Keep the upstream package version so
# that Writer's exact-version dependency on the core package still resolves.
dpkg-deb --raw-extract "$1" core
importer=usr/lib/libreoffice/program/xpdfimport
control=core/DEBIAN/control

grep -aFq 'libpoppler.so' "core/$importer"
grep -Eq '^Depends:.*, libpoppler[0-9]+ \(' "$control"
unlink "core/$importer"

if grep -arFq 'libpoppler.so' core/usr/lib/libreoffice; then
    printf '%s\n' 'Another LibreOffice component references Poppler.' >&2
    exit 1
fi

sed -Ei '/^Depends:/s/, libpoppler[0-9]+ \([^)]*\)//' "$control"
sed -i "\\|  $importer\$|d" core/DEBIAN/md5sums
dpkg-deb -Zgzip -z1 --root-owner-group --build core libreoffice-core-writer-only.deb
