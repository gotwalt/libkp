#!/usr/bin/env bash
#
# Build `MetersApp` as a double-clickable macOS app bundle.
#
# SwiftPM produces a bare executable; Gatekeeper, notarization and the local
# network permission prompt all want a bundle with an Info.plist, so assemble
# one by hand. The result is universal (arm64 + x86_64) and unsigned — signing
# and notarization happen in .github/workflows/metersapp-release.yml.
#
#   swift/Scripts/build_app.sh [output-dir]
#
# Environment:
#   MARKETING_VERSION  CFBundleShortVersionString (default: spec/version.toml)
#   BUILD_NUMBER       CFBundleVersion            (default: 0)
#   BUNDLE_ID          CFBundleIdentifier         (default: com.gotwalt.libkp.MetersApp)

set -euo pipefail

swift_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$(cd "$swift_root/.." && pwd)"
out_dir="${1:-$swift_root/.build/dist}"

version="${MARKETING_VERSION:-$(grep -oE '"[0-9]+\.[0-9]+\.[0-9]+"' "$repo_root/spec/version.toml" | head -1 | tr -d '"')}"
build="${BUILD_NUMBER:-0}"
bundle_id="${BUNDLE_ID:-com.gotwalt.libkp.MetersApp}"

cd "$swift_root"

# Universal, so the download runs on Apple silicon and Intel alike.
arch_flags=(--arch arm64 --arch x86_64)
swift build -c release "${arch_flags[@]}" --product MetersApp
bin_path="$(swift build -c release "${arch_flags[@]}" --show-bin-path)"

app="$out_dir/MetersApp.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin_path/MetersApp" "$app/Contents/MacOS/MetersApp"
printf 'APPL????' > "$app/Contents/PkgInfo"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>en</string>
	<key>CFBundleDisplayName</key>
	<string>KP Meters</string>
	<key>CFBundleExecutable</key>
	<string>MetersApp</string>
	<key>CFBundleIdentifier</key>
	<string>${bundle_id}</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>KP Meters</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>${version}</string>
	<key>CFBundleVersion</key>
	<string>${build}</string>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.music</string>
	<key>LSMinimumSystemVersion</key>
	<string>13.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSLocalNetworkUsageDescription</key>
	<string>KP Meters finds your Kemper Profiler on the local network and reads its live state.</string>
	<key>NSPrincipalClass</key>
	<string>NSApplication</string>
	<key>NSSupportsAutomaticTermination</key>
	<true/>
	<key>NSSupportsSuddenTermination</key>
	<false/>
</dict>
</plist>
PLIST

plutil -lint "$app/Contents/Info.plist" > /dev/null
echo "built $app ($version build $build)"
lipo -archs "$app/Contents/MacOS/MetersApp"
