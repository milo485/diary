#!/bin/sh
# Rebuilds the Diary Flatpak and installs it into the user scope.
# Usage:  ./flatpak/build.sh        (from the project root)
set -e
cd "$(dirname "$0")/.."   # change to project root

flatpak run org.flatpak.Builder \
  --user --force-clean --install --install-deps-from=flathub \
  build-dir flatpak/org.diary.Diary.yml

echo
echo "✓ Done. Launch with:  flatpak run org.diary.Diary"
echo "  (or from the app menu: \"Diary\")"
