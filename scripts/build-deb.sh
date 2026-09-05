#!/bin/sh
# Build viewwall_<version>_all.deb into dist/.
#
# Deliberately uses dpkg-deb directly rather than dpkg-buildpackage: the package
# is architecture-independent Python plus a unit file, so the full Debian build
# machinery would add dependencies without adding correctness.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
[ -n "$version" ] || { echo "cannot read version from pyproject.toml" >&2; exit 1; }
revision=${DEB_REVISION:-1}
pkgdir=$(mktemp -d)
trap 'rm -rf "$pkgdir"' EXIT
# mktemp creates 0700; the package root must be world-readable like any other.
chmod 0755 "$pkgdir"

sitedir=$pkgdir/usr/lib/python3/dist-packages
install -d "$sitedir/viewwall" \
           "$pkgdir/DEBIAN" \
           "$pkgdir/usr/bin" \
           "$pkgdir/lib/systemd/system" \
           "$pkgdir/usr/share/viewwall" \
           "$pkgdir/usr/share/doc/viewwall"

install -m 0644 src/viewwall/*.py "$sitedir/viewwall/"

cat > "$pkgdir/usr/bin/viewwall" <<'EOF'
#!/usr/bin/python3
from viewwall.app import main

if __name__ == "__main__":
    main()
EOF
chmod 0755 "$pkgdir/usr/bin/viewwall"

install -m 0644 packaging/viewwall.service "$pkgdir/lib/systemd/system/viewwall.service"
install -m 0644 examples/viewwall.toml "$pkgdir/usr/share/viewwall/viewwall.toml"
install -m 0644 README.md "$pkgdir/usr/share/doc/viewwall/README.md"
gzip -9n -c packaging/debian/changelog > "$pkgdir/usr/share/doc/viewwall/changelog.Debian.gz"
# Machine-readable copyright rather than the licence text: LICENSE is the
# verbatim GPL-3.0, which is what GitHub and other tools match against, so
# the copyright holder is named here instead.
install -m 0644 packaging/debian/copyright "$pkgdir/usr/share/doc/viewwall/copyright"

# Binary control file, derived from the source control file's Depends so the
# two cannot drift.
deps=$(python3 - <<'PYEOF'
import re
text = open("packaging/debian/control").read()
block = re.search(r"^Depends:(.*?)(?=^[A-Z][A-Za-z-]*:)", text, re.S | re.M).group(1)
items = [d.strip() for d in block.replace("\n", " ").split(",")]
print(", ".join(d for d in items if d and d != "${misc:Depends}"))
PYEOF
)
recs=$(awk '/^Recommends:/{sub(/^Recommends: /,""); print}' packaging/debian/control)
{
    echo "Package: viewwall"
    echo "Version: $version-$revision"
    echo "Architecture: all"
    echo "Maintainer: $(awk -F': ' '/^Maintainer:/{print $2}' packaging/debian/control)"
    echo "Section: video"
    echo "Priority: optional"
    echo "Depends: $deps"
    [ -n "$recs" ] && echo "Recommends: $recs"
    echo "Installed-Size: $(du -sk "$pkgdir" | cut -f1)"
    awk '/^Description:/{f=1} f && !/^(Package|Architecture|Depends|Recommends):/{print}' \
        packaging/debian/control
} > "$pkgdir/DEBIAN/control"

install -m 0755 packaging/debian/postinst "$pkgdir/DEBIAN/postinst"
install -m 0755 packaging/debian/postrm "$pkgdir/DEBIAN/postrm"
# debhelper is not involved, so drop its substitution markers.
sed -i '/#DEBHELPER#/d' "$pkgdir/DEBIAN/postinst" "$pkgdir/DEBIAN/postrm"

# systemd integration that debhelper would normally generate.
cat >> "$pkgdir/DEBIAN/postinst" <<'EOF'
if [ "$1" = configure ] && [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi
EOF
cat > "$pkgdir/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ -d /run/systemd/system ] && [ "$1" = remove ]; then
    systemctl stop viewwall.service || true
fi
exit 0
EOF
chmod 0755 "$pkgdir/DEBIAN/prerm"

install -d dist
out="dist/viewwall_${version}-${revision}_all.deb"
dpkg-deb --root-owner-group --build "$pkgdir" "$out" >/dev/null
echo "$out"
