# The SDK downloads RadioLib "master", which is now 8.0.0 and no longer matches launcher 0.6.105 (written for 7.x).
set -uo pipefail
cd ~/cz-launcher-build/launcher/SDK/github_source
TAG=$(git ls-remote --tags https://github.com/jgromes/RadioLib 2>/dev/null | awk '{print $2}' | sed 's#refs/tags/##' | grep -E '^7\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
echo "latest RadioLib 7.x tag: $TAG"; [ -n "$TAG" ] || exit 21
curl -fsSL -o /tmp/radiolib7.zip "https://github.com/jgromes/RadioLib/archive/refs/tags/$TAG.zip" || exit 22
rm -rf /tmp/rl7 && unzip -q /tmp/radiolib7.zip -d /tmp/rl7 && mv RadioLib RadioLib-8.0.0.unused && mv /tmp/rl7/RadioLib-* RadioLib
grep -m1 version RadioLib/library.properties
