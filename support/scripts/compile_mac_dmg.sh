# Prerequisite:
# - brew install create-dmg

VERSION=$(sed -n 's/^version: \([0-9]*\.[0-9]*\.[0-9]*\).*/\1/p' app/pubspec.yaml)
DMG="LocalDrop-$VERSION.dmg"

cd app
fvm flutter clean
fvm flutter pub get
fvm flutter build macos

# sign the app
echo
echo "Signing the app..."
echo
SIGN_ID="${LOCALDROP_SIGN_ID:?Set LOCALDROP_SIGN_ID to your own Developer ID identity}"
codesign --deep --force --verbose --options runtime --preserve-metadata=entitlements --sign "$SIGN_ID" build/macos/Build/Products/Release/LocalDrop.app

# create dmg
# brew install create-dmg
echo
echo "Creating dmg..."
echo
rm -f "$DMG"
create-dmg \
  --volname "LocalDrop" \
  --window-size 500 300 \
  --background "../support/build/dmg/background.png" \
  --icon LocalDrop.app 130 110 \
  --app-drop-link 360 110 \
  "$DMG" \
  build/macos/Build/Products/Release/LocalDrop.app

# sign the dmg
echo
echo "Signing the dmg..."
echo
codesign --force --verbose --sign "$SIGN_ID" "$DMG"

# send to apple for notarization
DEV_EMAIL="${LOCALDROP_APPLE_ID:?Set LOCALDROP_APPLE_ID}"
APP_PASSWORD="${LOCALDROP_APP_PASSWORD:?Set LOCALDROP_APP_PASSWORD}"
TEAM_ID="${LOCALDROP_TEAM_ID:?Set LOCALDROP_TEAM_ID}"

echo
echo "Sending to apple for notarization..."
echo
xcrun notarytool submit "$DMG" --wait --apple-id $DEV_EMAIL --password "$APP_PASSWORD" --team-id "$TEAM_ID"

# download notarization result and apply to the dmg
echo
echo "Run stapler..."
echo
xcrun stapler staple "$DMG"
cd ..
