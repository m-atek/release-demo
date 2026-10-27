# The Flutter toolchain store-floor builds and tests its Android side in:
# Flutter 3.44.0 with the Android SDK (platform 36, build tools 36.0.0) and
# JDK 21, pinned by digest. The Android SDK licences are the ones the image
# ships with. scripts/demos/mobile/flutter.py builds it on the agent and tags
# it with a hash of this file; the Mac agent runs the same Flutter version.
FROM ghcr.io/cirruslabs/flutter:3.44.0@sha256:46691e311715845de03a3ba4753a475476936805b29431b1f00f1816981033f8

# Into the image, so builds don't fetch them each time: the NDK the Android
# Gradle plugin strips native libraries with, and the packages Flutter itself
# pins, cached by resolving a throwaway app.
RUN sdkmanager --install "ndk;28.2.13676358" > /dev/null \
 && cd /tmp && flutter create --suppress-analytics --platforms android warmup > /dev/null \
 && cd warmup && flutter pub get --suppress-analytics > /dev/null && cd / && rm -rf /tmp/warmup

# Builds run as the agent's user, not root, so Flutter, the Android SDK and
# the pub cache Flutter's own tool resolves from (in root's home) must be
# usable by any user, and git must accept the SDK's checkout. Directories
# and the cache's stamp files only: changing every file would copy the whole
# SDK into a new layer.
RUN chmod 755 /root \
 && find /sdks/flutter /opt/android-sdk-linux /root/.pub-cache -type d -exec chmod a+rwx {} + \
 && find /sdks/flutter/bin/cache -maxdepth 1 -type f -exec chmod a+rw {} + \
 && git config --system --add safe.directory '*'

ENV PUB_CACHE=/root/.pub-cache \
    FLUTTER_SUPPRESS_ANALYTICS=true \
    DART_SUPPRESS_ANALYTICS=true \
    CI=true \
    HOME=/tmp/home
