// Video 24's clock: from the first search that finds a holder to the closed
// change that removes the last one. Nothing here can fail a release. With the
// switch off (scripts/demos/patch_clock/switch.py) clock.py does nothing.
//
//   patchClock.stop()       in Record, after change.py close successful

void stop() {
    try {
        int status = sh(returnStatus: true, script: 'python3 scripts/demos/patch_clock/clock.py stop')
        if (status != 0) {
            echo "Patch clock: clock.py exited ${status}; the release carries on"
        }
    } catch (error) {
        echo "Patch clock: not run (${error.message}); the release carries on"
    }
}
