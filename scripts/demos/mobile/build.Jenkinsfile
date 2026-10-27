// store-floor-build: builds and signs Store Floor for both platforms at the
// same time, iOS on the Mac agent and Android on the tools host, and archives
// the signed files. Started as release-bot by scripts/demos/mobile/build.py in
// the release's Build stage, and for a rollback or reset.py, to rebuild an
// earlier version's iOS app with a higher build number.
//
// The keys never reach the repository or the log: Jenkins binds them from
// its credentials (loaded from SSM at boot) for the one step that signs, and
// masks them in the log. Values from outside reach the shell as environment
// variables, never quoted into the script.

String version = params.VERSION
currentBuild.displayName = "#${env.BUILD_NUMBER} store-floor ${version} (${params.STORE_BUILD})"
currentBuild.description = "${params.PLATFORMS}, from ${params.SOURCE}, for ${params.REQUESTED_BY}"

// The code to build: a commit's services/store-floor, or a demo state's.
void prepareSource() {
    checkout scm
    sh '''
        case "$SOURCE" in
          demo-state:*) python3 scripts/stage.py store-floor "${SOURCE#demo-state:}" --no-commit ;;
          *) rm -rf services/store-floor && git checkout --quiet "$SOURCE" -- services/store-floor ;;
        esac
        test "$(cat services/store-floor/VERSION)" = "$VERSION"
    '''
}

Map parts = [:]

if (params.PLATFORMS.contains('ios')) {
    parts['iOS'] = {
        node('mac') {
            stage('iOS: build and sign') {
                prepareSource()
                // The Apple Developer Program stand-in needs no keys. The real
                // path binds the match password and the team id for this step.
                String standin = sh(returnStdout: true, script: 'python3 scripts/demos/mobile/mobile.py standin apple').trim()
                if (standin == 'yes') {
                    sh 'python3 scripts/demos/mobile/build.py store-floor --platform ios --build-number "$STORE_BUILD"'
                } else {
                    withCredentials([string(credentialsId: 'store-floor-match-password', variable: 'MATCH_PASSWORD'),
                                     string(credentialsId: 'store-floor-apple-team-id', variable: 'APPLE_TEAM_ID')]) {
                        sh 'python3 scripts/demos/mobile/build.py store-floor --platform ios --build-number "$STORE_BUILD"'
                    }
                }
            }
            archiveArtifacts artifacts: 'dist/store-floor/parts/ios/*', fingerprint: true
        }
    }
}

if (params.PLATFORMS.contains('android')) {
    parts['Android'] = {
        node('tools-host') {
            stage('Android: build') {
                prepareSource()
                sh 'python3 scripts/demos/mobile/build.py store-floor --platform android --build-number "$STORE_BUILD"'
            }
            stage('Android: sign') {
                // jarsigner, the tool Google names for signing app bundles, in
                // the pinned Flutter container. The upload key and its password
                // exist only for this step; Jenkins prints them as ****.
                withCredentials([file(credentialsId: 'store-floor-upload-keystore', variable: 'UPLOAD_KEYSTORE'),
                                 string(credentialsId: 'store-floor-upload-key-password', variable: 'UPLOAD_KEY_PASSWORD')]) {
                    sh '''
                        image=$(python3 scripts/demos/mobile/flutter.py image)
                        bundle=$(ls dist/store-floor/parts/android/*.aab)
                        docker run --rm --platform linux/amd64 --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work \\
                            -v "$UPLOAD_KEYSTORE:/keys/upload.p12:ro" "$image" \\
                            jarsigner -keystore /keys/upload.p12 -storetype PKCS12 -storepass "$UPLOAD_KEY_PASSWORD" \\
                            -sigalg SHA256withRSA -digestalg SHA-256 "$bundle" upload
                    '''
                }
            }
            stage('Android: verify') {
                sh 'python3 scripts/demos/mobile/build.py store-floor --verify-android dist/store-floor/parts/android/*.aab --build-number "$STORE_BUILD"'
            }
            archiveArtifacts artifacts: 'dist/store-floor/parts/android/*', fingerprint: true
        }
    }
}

parallel parts
